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
  with `hv doctor rebuild`.

The journal is the contract. If `store.db` is corrupt or missing, `hv doctor rebuild`
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
| `type` | Entry type: `fact`, `retract`, `decision`, `entity`, `entity_fact`, `governance` |
| `timestamp` | ISO8601 wall clock at write time |
| `payload` | Type-specific data |
| `prev_hash` | Hash of the previous entry — forms a hash chain per node |
| `pub` | The signer's Ed25519 public key (present on signed entries) |
| `sig` | Ed25519 signature over the canonical entry minus `sig`; the hash chain commits to it |

The `governance` type carries owner-resilience actions (owner declaration, admit,
set-config, standby, escrow/revoke-escrow, nominated succession/transfer, quorum
election proposals/votes, and the dead-man heartbeat); it's projected by
`_governance_state` (see *Confidence model* below).

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
`_recompute_confidence` during `hv doctor rebuild` and after every write.

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

### Owner resolution + succession

`_governance_state` resolves the owner over a single deterministic pass in
`(timestamp, node_id, seq)` order — so every node converges on the same owner:

1. **Genesis (TOFU).** The first valid self-signed `owner` declaration wins and
   becomes the term-0 owner; it also mints the `hive_id` and fixes `owner_ts`
   (which still anchors the grandfathering of pre-owner forgets, even after a handoff).
2. **Succession chain.** Walking forward, the *current* owner is carried along and
   advances when an act is authorized by the then-current owner:
   `nominate-successor` (owner-signed) opens a nomination over a successor pubkey;
   the nominee's `claim-succession` (the CLI verb is `hv owner claim`; self-signed by the
   NEW key, like genesis) takes
   effect only against an *open matching* nomination; `transfer` (owner-signed) is an
   immediate handoff. Every other act (admit/config/escrow/standby) is applied only
   when signed by the owner current *at that log position* — so a handed-off owner's
   later acts drop, and a live owner can never be unseated (only their own signed
   nominate/transfer, or a nominee's claim, moves ownership). Racing claims against one
   nomination resolve deterministically: the first in sort order wins and closes it.
3. **Quorum election (dead-man switch).** Interleaved in the same pass are two
   *device-signed* acts (authority is hive membership, not the owner key):
   `propose-election` carries a content-addressed `proposal_id` (= hash of the proposed
   `new_owner_pub` + the proposer's `basis_ts`) and `vote-election` (the CLI verb is
   `hv owner vote <proposal_id>`) references it. Only
   acts from currently-**admitted** devices count; a vote that sorts before its proposal
   is buffered and applied when the proposal appears (clock-skew safe). The instant a
   proposal reaches `quorum_m` distinct voter-units (`quorum_by` = device or principal)
   **and** the current owner's last activity is older than `dead_man_days` (measured
   against the proposal's `basis_ts`), the proposal installs its key as the new owner at
   that log position — exactly like a `transfer`, so the elected owner's later acts are
   honored from there. The earliest such crossing wins; once it installs, the new owner's
   activity is fresh, so a racing election can no longer arm. **A live owner is never
   unseated:** any owner-signed act — including an explicit `heartbeat` — refreshes
   last-activity and re-shuts the window.
4. **Back-compat.** A hive with no succession/election entries (and the default
   `quorum_m=0`) resolves to exactly the term-0 owner — identical to the pre-succession
   projection.

**Escrow tombstones.** `owner-escrow` entries (the in-hive passphrase-encrypted key)
are collected during the same walk; an owner-signed `revoke-escrow` (a specific
`node_id:seq` ref, or `all`) marks earlier escrows revoked. `gov["escrows"]` is the
live, revocation-filtered, sorted list `hv owner restore` draws from. The tombstone is
logical only — the ciphertext is permanent in the append-only journal, so a leaked
escrow passphrase is truly remediated only by rotating the owner key via succession.

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
- **Owner resilience** (shipped): pt.1 backup/restore + escrow + standby; pt.2 nominated
  succession + transfer + escrow tombstone (the owner chain above); pt.3 quorum election +
  dead-man switch (contract 1.9) — shipped

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
6. Run `hv doctor rebuild` once after all ingestion

### Device identity

A node is identified by an Ed25519 **device key**, not a hostname. `device_id`
is `"k1:" + sha256(pubkey)[:16hex]`; the 32-byte seed lives at
`HIVE_HOME/.device-key` (mode 0600, gitignored, excluded from the source
manifest), and the fingerprint is cached at `.device-id` so resolving `NODE_ID`
stays a cheap file read. `NODE_ID` resolves to: `HIVE_NODE_ID` override → the
device fingerprint if a key is present → the hostname (legacy, pre-migration).

A key is minted only by `hv config identity init` (fresh install) or the migration — importing
`hv` never creates one, so a legacy hostname node keeps its identity until it is
deliberately migrated. `hv migrate-device-identity --map` (the canonical user command; also reachable as the
folded `hv doctor migrate-identity --map`) re-stamps an existing
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
