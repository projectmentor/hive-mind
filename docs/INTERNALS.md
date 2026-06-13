# HiveMind Internals

Technical reference for contributors and agent integrators. Covers storage
architecture, the confidence model, sync protocol, and data formats.

For user-facing CLI docs see [CLI_REFERENCE.md](CLI_REFERENCE.md).
For the sync HTTP API see [SYNC_API.md](SYNC_API.md).
For the full architectural design see [P2P_DESIGN.md](P2P_DESIGN.md).

---

## Storage architecture

HiveMind uses a two-layer storage model:

- **Journal** (`journal/YYYY-MM-DD.jsonl`) — the source of truth. Append-only.
  Every write goes here first. Never modified after being written.
- **SQLite** (`store.db`) — a derived index built from the journal. Used for
  FTS5 search, JOINs, and fast reads. Can be thrown away and rebuilt at any time
  with `hv rebuild`.

The journal is the contract. If `store.db` is corrupt or missing, `hv rebuild`
restores full functionality. The journal alone is sufficient to recover any node
from scratch.

### Journal entry format

Each line in a `.jsonl` file is a JSON object:

```json
{
  "node_id": "k1:2a2110f3d8963a9e",
  "seq": 42,
  "type": "fact",
  "timestamp": "2026-06-05T09:15:00Z",
  "payload": { ... },
  "prev_hash": "sha256:abc123...",
  "pub": "<base64 Ed25519 public key>",
  "sig": "<base64 signature over the entry sans sig>"
}
```

| Field | Description |
|---|---|
| `node_id` | The authoring node's **device identity**: `k1:` + first 16 hex of `sha256(pubkey)` |
| `seq` | Per-node monotonic sequence number |
| `type` | Entry type: `fact`, `retract`, `decision`, `entity`, `entity_fact` |
| `timestamp` | ISO8601 wall clock at write time |
| `payload` | Type-specific data |
| `prev_hash` | Hash of the previous entry — forms a hash chain per node |
| `pub` | The signer's Ed25519 public key (present on signed entries) |
| `sig` | Ed25519 signature over the canonical entry minus `sig`; the hash chain commits to it |

`node_id` is an unforgeable device fingerprint, not a hostname (see *Device
identity* below). Signed entries are verified on ingest: an entry whose `node_id`
isn't the fingerprint of its embedded `pub`, or whose `sig` fails, is rejected,
so no one can attribute an entry to a device they don't hold the key for.
Unsigned entries (pre-migration history, legacy peers) are grandfathered.

Cross-row links (e.g. a retraction pointing at a fact, a decision superseding
another) use `(node_id, seq)` journal identity — not local SQLite IDs. This
ensures links survive cross-node merge correctly.

---

## Confidence model

Confidence is a derived value — never stored directly. It's computed by
`_recompute_confidence` during `hv rebuild` and after every write.

### Formula

```
confidence(n) = 0.90 × (1 − 0.5ⁿ)
```

Where `n` = number of distinct corroborating identities asserting identical
content.

| n (sources) | Confidence |
|---|---|
| 1 | 0.45 |
| 2 | 0.675 |
| 3 | 0.7875 |
| ∞ | → 0.90 (ceiling) |

### What counts as a distinct source

The model counts distinct `(node_id, app, instance)` tuples — **not** full
source strings. `node_id` here is the device identity (a key fingerprint), so
"two nodes" means two devices that each hold a distinct key, which a peer cannot
fake. Session IDs (`session8`) are ignored for corroboration purposes.

- Same agent, two sessions → **one** identity (idempotent)
- Same agent, two **devices** → two independent corroborators
- Two agents on the **same** device → correlated, **discounted** (see below)

This prevents session churn from inflating confidence.

### Same-device discount, admission, CAP_self (D0-v2)

Confidence is a *governed* projection. Three rules refine the raw identity count
(all derived from the journal, so they stay identical on every device):

- **Same-device discount.** Agents on one machine are correlated, not
  independent. Each device contributes its strongest identity in full plus
  `λ·(sum of its other identities)`; `λ = same_device_lambda` (default `0.5`).
  So two agents on one box net `1.5`, not `2`; on two boxes, `2`.
- **Admission gate.** Cryptographic identity stops *impersonation*, not *Sybil*
  (one actor minting many keys). Once an **owner** is established (`hv owner
  init`), only **admitted** devices (`hv group admit`) count toward confidence;
  unadmitted writes are still stored and synced but contribute zero.
- **CAP_self.** When every device behind a fact maps to one **principal**
  (`hv group admit --principal`), confidence is clamped to `cap_self` (default
  `0.70`): your own machines agreeing isn't independent corroboration.

Governance lives in owner-signed `governance` journal entries
(owner/admit/set-config), projected by `_governance_state`. Because it's
journaled (not per-node config), every device derives the same admitted set,
principal map, and config. No owner yet → discount applies, gate + CAP_self off.

### Retraction effects

- Standard retraction (`hv retract`) → reduces confidence by excluding the
  retractor's identity from the projection
- Owner retraction (`hv retract --owner`) → drives confidence to the floor.
  Once an owner exists it must be **owner-signed** (you can't forge a forget with
  a bare source tag); forgets predating the owner are grandfathered.

### Phase roadmap

- **Phase A** (shipped): derived corroboration confidence, multi-source
- **Phase B / D0-v2** (shipped): journaled governance, same-device discount,
  admission gate, principal weighting (CAP_self)
- **Phase C** (shipped): contested flag + decay for contradicted facts

---

## Merkle sync

The sync protocol uses a Merkle tree over journal chunks (100 entries per chunk)
to efficiently identify which entries a peer is missing.

### Flow

1. Client fetches peer's Merkle root (`GET /sync/merkle-root`)
2. If roots match → in sync, done
3. If roots differ → compare per-node chunk hashes
4. For each differing chunk → fetch missing entries (`GET /sync/chunk`)
5. Ingest missing entries locally (`POST /sync/ingest`)
6. Run `hv rebuild` once after all ingestion

### Device identity

A node is identified by an Ed25519 **device key**, not a hostname. `device_id`
is `"k1:" + sha256(pubkey)[:16hex]`; the 32-byte seed lives at
`HIVE_HOME/.device-key` (mode 0600, gitignored, excluded from the source
manifest), and the fingerprint is cached at `.device-id` so resolving `NODE_ID`
stays a cheap file read. `NODE_ID` resolves to: `HIVE_NODE_ID` override → the
device fingerprint if a key is present → the hostname (legacy, pre-migration).

A key is minted only by `hv config identity init` (fresh install) or the migration — importing
`hv` never creates one, so a legacy hostname node keeps its identity until it is
deliberately migrated. `hv migrate-device-identity --map` re-stamps an existing
journal from hostnames to device_ids: a deterministic transform (same map on every
node → byte-identical journals → peers stay converged). Two instances on one box
still need distinct `HIVE_NODE_ID` or distinct keys.

### Hives and onboarding

A **hive** is the set of nodes sharing one queen bee's journal. `hv owner init`
mints a `hive_id` (`"h1:" + 8 random bytes`, public, in the owner-signed genesis)
that **scopes sync**: `/sync/hello` and `/sync/ingest` advertise/check it, and a
node refuses to merge a journal from a different `hive_id`. Without that, two hives
on one tailnet would merge into one confused journal. An empty `hive_id` (no owner
yet) syncs with other empty ones, so the genesis can propagate during bootstrap.

Discovery uses Tailscale as the directory: `hv discover` reads `tailscale status`
and probes each device's `GET /hive/info` (metadata + the signed genesis, never the
journal — so listing stays open even if reads are later gated). A new node either
**bootstraps** (first node → `owner init`, becomes queen bee) or **joins** (`hv join`
emits a self-signed `join-request`; the owner sees it in their session-start digest
and runs `hv group admit`). Joining is non-blocking — the joiner syncs immediately; its
writes are stored but count zero until admitted.

---

## SQLite schema

Key tables in `store.db`:

| Table | Description |
|---|---|
| `facts` | Stored facts with FTS5 index (`facts_fts`) |
| `decisions` | Decisions with supersession links |
| `entities` | Named entities |
| `entity_facts` | Many-to-many fact-to-entity links |
| `journal_index` | Index of ingested journal entries by `(node_id, seq)` |
| `node_chunk_hashes` | Merkle chunk hashes per node, used by sync |

### WAL mode

`store.db` runs with:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
PRAGMA mmap_size=268435456;
```

This allows concurrent reads during writes and is safe for the single-writer
model HiveMind uses.

---

## Source identity format

Used in `--source` arguments and stored in journal entries.

```
<app>:<context_class>/<instance>/<session8>
```

The confidence model extracts `(app, instance)` from this string and pairs it
with the entry's `node_id` (the device identity). `--source` is a **human label**
on top of the cryptographic device identity: it distinguishes agents/apps on one
device, but it is self-asserted and not what proves who wrote an entry — the
device signature is. `session8` is for human/log readability only and does not
affect confidence.

**Do not change this format** without updating `_recompute_confidence` in `hv`
and coordinating with any live agent integrations (Hermes plugin, CC hooks).

---

## Testing

```bash
cd ~/projects/hive-mind

# Full test suite (offline, no network)
python3 -m pytest -q

# CLI smoke test
./scripts/smoke.sh

# Two-node sync smoke test
./scripts/sync_smoke.sh
```

Tests use `HIVE_NOW` and `HIVE_NODE_ID` env vars to pin time and identity for
deterministic results.
