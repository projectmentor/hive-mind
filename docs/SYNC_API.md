# HiveMind Sync API Reference

The sync daemon (`hive_sync_daemon.py`) exposes a minimal JSON/HTTP API on port
`:9876` (default). Tailscale (WireGuard) is still the transport perimeter — only
admit Tailscale peers — but the daemon no longer trusts network reachability
alone. As of **protocol version 2**, remote sync **reads** are authenticated with
a signed-request envelope (see [Request authentication](#request-authentication)),
loopback (`127.0.0.1`) requests from the local operator stay unauthenticated, and
the daemon binds its own tailnet address rather than `0.0.0.0`. This closes
[GHSA-242f-7fxg-f7wm](ADVISORIES.md) — an unauthenticated remote peer could
previously read the entire journal off `/sync/chunk`. Writes (`POST /sync/ingest`)
were already gated by per-entry signatures and remain so.

Start the daemon:
```bash
./hv sync daemon          # serve + periodic outbound sync (every 5 min)
python3 hive_sync_daemon.py    # same, direct
```

---

## Request authentication

Since protocol version 2, a **remote** request to an authenticated endpoint must
carry a signed-request envelope. The client signs, with its Ed25519 **device
key**, over the canonical string

```
method + "\n" + path + "\n" + sorted-query + "\n" + sha256(body) + "\n" + timestamp + "\n" + nonce
```

and presents the signature and its identity in HTTP headers:

| Header | Value |
|---|---|
| `Hive-Auth-Alg` | `hive-sig-v1` (the envelope scheme id) |
| `Hive-Auth-Device` | Signer's device id (`k1:…`) |
| `Hive-Auth-Pub` | Signer's Ed25519 public key, base64 |
| `Hive-Auth-Ts` | Unix timestamp (seconds) the request was signed |
| `Hive-Auth-Nonce` | A per-request random nonce (replay guard) |
| `Hive-Auth-Sig` | base64 Ed25519 signature over the canonical string above |

On receipt the daemon:

1. Verifies `Hive-Auth-Sig` against `Hive-Auth-Pub`, and that
   `Hive-Auth-Device` is the fingerprint of that pubkey (`k1:` + first 16 hex of
   `sha256(pub)`) — so a caller can't sign under another device's id.
2. Requires the signer to be in the governance **admitted set** (an owner or
   admitted device). A sterile/unknown device is refused.
3. Checks the timestamp is **fresh** — within `±HIVE_SYNC_AUTH_WINDOW` seconds
   (default `300`) — and that the nonce hasn't been seen inside that window
   (replay guard).

**Loopback bypass.** Requests arriving on `127.0.0.1` (the local `hv` CLI, the
dashboard, or a signed peer proxy) are trusted without an envelope — the local
operator already holds the keys. Only remote peers must sign.

**Enforcement mode (phased rollout).** Each node runs one of three modes,
independent of the protocol version it advertises:

| Mode | Behavior |
|---|---|
| `off` | Read-auth disabled — legacy behavior, all reads open. |
| `permissive` (default) | Clients sign every request; the daemon verifies and **logs** a failure but still serves. Nothing breaks while the fleet upgrades. |
| `enforce` | An unauthenticated or invalid remote request to an authenticated endpoint is rejected (**401**). |

Set the mode with `hv sync auth [off|permissive|enforce]` or the `HIVE_SYNC_AUTH`
env var. See [Rolling out enforcement](#rolling-out-enforcement) below.

---

## Endpoint authentication classes

The daemon binds **both** loopback (`127.0.0.1`) and its primary tailnet address.
Every endpoint falls into one of three access classes:

| Class | Endpoints | Who may call |
|---|---|---|
| **Open discovery** | `GET /hive/info`, `GET /sync/merkle-root`, `GET /api/verify` | Anyone reachable — no auth. These return metadata only (no journal content), so listing a hive stays open. |
| **Remote-auth** | `GET /sync/hello`, `GET /sync/chunk`, `POST /sync/ingest` | Loopback (unauthenticated) **or** a signed request from an admitted device. Carries journal content, so remote callers must sign. |
| **Loopback-only** | all `GET/POST /api/*` (except `/api/verify`) and the dashboard SPA | The local operator on `127.0.0.1`, or a signed peer proxy. Not served to an unauthenticated remote host. |

`POST /sync/ingest` additionally verifies the per-entry signatures on the payload
(unchanged) — the request envelope authenticates the *caller*, the entry
signatures authenticate the *content*.

---

## Endpoints

### `GET /sync/hello`

Node identity and journal summary. Used as the handshake and peer-discovery
step during a sync round.

**Auth class: remote-auth** — a remote caller must present a signed request
envelope from an admitted device (loopback is exempt). See
[Request authentication](#request-authentication).

**Response:**
```json
{
  "node_id": "node-a",
  "hive_id": "k1:2a2110f3d8963a9e",
  "protocol_version": 2,
  "advertised_addr": "100.64.0.2:9876",
  "journal_summary": {
    "total": 48,
    "by_node": {
      "node-a": 46,
      "node-b": 2
    }
  },
  "chunks": {
    "node-a": [
      "sha256:d46957a7b133e438...",
      "sha256:4bff2b51c9e3a2f0..."
    ],
    "node-b": [
      "sha256:7a3f9c12e8b4d510..."
    ]
  }
}
```

| Field | Description |
|---|---|
| `node_id` | This device's identity — its device-key fingerprint (`HIVE_NODE_ID` overrides) |
| `hive_id` | The hive this node belongs to (the founding owner's device id). A client refuses to merge across differing hive ids |
| `protocol_version` | Sync wire-protocol version (currently `2` — read-authentication capable). Bumped for additive handshake changes so they can be negotiated without a journal-schema break |
| `advertised_addr` | This node's reachable `host:port` (its tailnet address), so a peer learns where to reach it back without a config round-trip |
| `journal_summary.by_node` | Highest seq seen per source node — used for quick divergence detection |
| `chunks` | Per-node array of 100-entry chunk hashes (Merkle leaf hashes). Used to localize which windows need syncing |

---

### `GET /sync/merkle-root`

Global Merkle root over the entire journal. The root is computed over the journal
as a **G-Set** — entries are de-duped by `(node_id, seq)` before hashing, so two
nodes that hold the same logical set produce the same root even if one has a
physically-repeated journal line. The O(1) fast-path: if two nodes have identical
root hashes, they hold the same set of entries and no sync work is needed.

**Response:**
```json
{
  "root_hash": "sha256:4bff2b51c9e3a2f0d1e8b7a6c5f4e3d2..."
}
```

**Sync flow usage:** Client calls this first. If roots match → done (0 bytes
transferred). If not → call `/sync/hello` to localize differing chunks.

---

### `GET /sync/chunk`

Fetch a specific 100-entry window of the journal for a given source node.

**Auth class: remote-auth** — this endpoint returns journal content, so a remote
caller must present a signed request envelope from an admitted device (loopback
is exempt). This is the read that GHSA-242f-7fxg-f7wm closed.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `node` | string | Source node ID to fetch entries for |
| `start` | integer | First seq in range (1-indexed, inclusive) |
| `end` | integer | Last seq in range (inclusive) |

**Response:**
```json
{
  "entries": [
    {
      "node_id": "node-a",
      "seq": 1,
      "type": "fact",
      "timestamp": "2026-06-04T08:30:00Z",
      "payload": {
        "content": "...",
        "tags": ["infrastructure"],
        "source": "hermes:primary/claude-sonnet/abc12345"
      },
      "prev_hash": "sha256:genesis",
      "hash": "sha256:..."
    }
  ],
  "hash": "sha256:..."
}
```

`hash` is the Merkle hash of the returned entries — clients verify this matches
the chunk hash from `/sync/hello` to detect transmission errors.

---

### `POST /sync/ingest`

Append foreign journal entries to this device's journal (G-Set union merge).
Deduplicates by `(node_id, seq)`. After accepting entries, triggers
`rebuild_db()` to recompute SQLite + confidence projection.

**Request body:**
```json
{
  "hive_id": "k1:2a2110f3d8963a9e",
  "entries": [
    {
      "node_id": "node-b",
      "seq": 1,
      "type": "fact",
      "timestamp": "2026-06-04T10:00:00Z",
      "payload": { ... },
      "prev_hash": "sha256:genesis",
      "hash": "sha256:..."
    }
  ]
}
```

The top-level `hive_id` scopes the push. If both sides carry a `hive_id` and
they differ, the daemon refuses the merge and returns **409** (see Error
Responses). An empty `hive_id` on either side is allowed, so the genesis owner
declaration can propagate during bootstrap.

**Response:**
```json
{
  "accepted": 3,
  "duplicates": 0
}
```

| Field | Description |
|---|---|
| `accepted` | Number of new entries appended to the journal |
| `duplicates` | Number of entries already present (skipped) |

**Concurrency:** Ingest is serialized by a lock inside the daemon. Concurrent
CLI writes during an ingest are a theoretical race at this scale — noted
hardening for a future release.

---

### `GET /hive/info`

Discovery endpoint: minimal hive metadata plus the signed genesis (owner
declaration) for verification. Never returns journal entries — so listing a
hive (and confirming its protocol version during a rollout) stays open.

**Auth class: open discovery** — unauthenticated. Returns metadata only, no
journal content.

**Response:**
```json
{
  "hive_id": "k1:2a2110f3d8963a9e",
  "owner_id": "k1:2a2110f3d8963a9e",
  "node_id": "k1:10f6b761dd1c2a90",
  "label": "node-a",
  "node_count": 2,
  "protocol_version": 2,
  "advertised_addr": "100.64.0.2:9876",
  "genesis": { "node_id": "k1:...", "seq": 1, "type": "governance", "payload": { ... } }
}
```

| Field | Description |
|---|---|
| `hive_id` | The hive's identity (the founding owner's device id) — scopes all sync; cross-hive merges are refused |
| `owner_id` | Current owner's device id from governance state |
| `node_id` | This device's own identity (its device-key fingerprint). Moved here so discovery can name the responder without a `/sync/hello` round-trip |
| `label` | This node's human-readable label (`HIVE_NODE_LABEL`) |
| `node_count` | Number of admitted nodes (falls back to the count of distinct authoring nodes if no admit set exists) |
| `protocol_version` | Sync wire-protocol version (see `/sync/hello`). `2` = read-authentication capable — poll this across peers to confirm a fleet is ready to `enforce` |
| `advertised_addr` | This node's reachable `host:port` (its tailnet address) |
| `genesis` | The signed owner declaration entry, for independent verification of the hive's origin |

---

## Sync Protocol Flow

```
Client                          Server (peer)
  |                                |
  |-- GET /sync/merkle-root -----> |
  |<- {root_hash} ---------------- |
  |                                |
  | [roots match] -> DONE          |
  |                                |
  | [roots differ]                 |
  |-- GET /sync/hello -----------> |
  |<- {node_id, chunks, summary}-- |
  |                                |
  | diff local chunks vs remote    |
  |                                |
  | for each differing window:     |
  |-- GET /sync/chunk?node=X... -> |
  |<- {entries, hash} ------------ |
  | append_foreign_entries(...)    |
  | rebuild_db()                   |
  |                                |
  | compute windows peer lacks:    |
  |-- POST /sync/ingest ---------> |
  |<- {accepted, duplicates} ----- |
```

The sync is **bidirectional in one round**: PULL what we're missing, PUSH what
the peer is missing. No leader, no Raft, no coordinator.

---

## Journal Entry Format

All journal entries share this structure. The journal is the source of truth —
`store.db` is a derived cache rebuilt from it.

```json
{
  "node_id": "k1:2a2110f3d8963a9e",
  "seq": 42,
  "type": "fact | decision | entity | entity_fact | retract | governance",
  "timestamp": "2026-06-04T12:00:00Z",
  "payload": { ... },
  "prev_hash": "sha256:...",
  "pub": "<base64 Ed25519 public key>",
  "sig": "<base64 signature over the entry minus sig>"
}
```

| Field | Description |
|---|---|
| `node_id` | Authoring node's **device identity**: `k1:` + first 16 hex of `sha256(pubkey)`, not a self-declared name |
| `seq` | Per-node monotonic sequence number. `(node_id, seq)` is the global unique identity for cross-row links |
| `type` | Entry type (see below) |
| `timestamp` | ISO8601 UTC |
| `payload` | Type-specific data (see below) |
| `prev_hash` | Hash of the previous entry from this device — forms a per-node hash chain |
| `pub` | Signer's Ed25519 public key (present on signed entries) |
| `sig` | Ed25519 signature over the canonical entry minus `sig`; the chain commits to it |

A receiver verifies a signed entry on ingest: the `node_id` must be the fingerprint
of its embedded `pub`, and the `sig` must check out, or the entry is rejected —
so a node cannot author entries under another node's `node_id`. Unsigned entries
(pre-migration history, legacy peers) are accepted as-is. This is the cryptographic
identity that the earlier "self-declared; Phase B1 transport-attribution" note
anticipated; identity now travels in the entry itself.

### Entry Types and Payloads

**`fact`**
```json
{
  "content": "fact text",
  "tags": ["tag1", "tag2"],
  "source": "hermes:primary/claude-sonnet/abc12345",
  "importance": 1
}
```

**`decision`**
```json
{
  "content": "decision text",
  "rationale": "why",
  "source": "manual",
  "supersedes_ref": ["node-a", 3]
}
```

**`entity`**
```json
{
  "name": "EntitlementService",
  "type": "concept",
  "attributes": {"project": "realsparkz"}
}
```

**`entity_fact`**
```json
{
  "entity_ref": ["node-a", 10],
  "fact_ref": ["node-b", 1],
  "confidence": 0.9
}
```

**`retract`**
```json
{
  "fact_ref": ["node-a", 4],
  "reason": "test probe",
  "source": "hermes:primary/claude-sonnet/abc12345",
  "owner": false
}
```

**`governance`**
```json
{
  "action": "nominate-successor"
}
```

Carries owner-resilience actions in `payload.action`. The action is one of:
`standby`, `owner-escrow`, `revoke-escrow`, `nominate-successor`,
`revoke-nomination`, `claim-succession`, `transfer`, `heartbeat`,
`propose-election`, `vote-election`. Additional action-specific fields travel
alongside `action`.

---

## Configuration: `.peers.json`

```json
{
  "peers": [
    {
      "url": "http://100.64.0.2:9876",
      "node_id": "k1:10f6b761dd1c2a90"
    },
    {
      "url": "http://100.64.0.1:9876",
      "node_id": "k1:597b3e0f5fb92d37"
    }
  ],
  "port": 9876
}
```

| Field | Default | Description |
|---|---|---|
| `peers[].url` | required | Base URL of the peer's sync daemon |
| `peers[].node_id` | optional | The peer's device id (`k1:…`), for logging and the admitted-peer set |
| `bind` | auto | Interface to bind the daemon to. **Default is the node's own locally-bindable Tailscale IP (`100.64.0.0/10`), else `127.0.0.1` — never `0.0.0.0`.** Omit it and the daemon picks the right address. Set it only to override; `HIVE_BIND` in the environment takes precedence. A legacy `"bind": "0.0.0.0"` left in this file is auto-upgraded to the tailnet address on restart. |
| `port` | `9876` | Port the daemon listens on |

The daemon always binds **loopback in addition to** its primary address, so the
local `hv` CLI and dashboard reach it on `127.0.0.1` regardless of `bind`.
`0.0.0.0` is still available as a deliberate escape hatch via `HIVE_BIND=0.0.0.0`
(e.g. an unusual NAT/interface setup) — it is no longer the default and the
installer no longer hardcodes it.

Get a node's device id and public key with `hv config identity show` on that machine.

`.peers.json` is gitignored (contains Tailscale IPs). Copy from
`config/.peers.json.example` and edit per node.

**WSL + Tailscale note:** Each WSL2 instance gets its own Tailscale IP (appears
as a separate machine on the tailnet, e.g. `node-a-1`). The sync daemon binds
that WSL Tailscale IP (auto-detected) plus loopback, and is reachable directly
at the WSL Tailscale IP. No portproxy or mirrored networking needed. Get the WSL
IP with `tailscale ip`.

**macOS note:** Install Tailscale via the app (App Store / standalone) or
`brew install tailscale`; the app or `brew services` owns `tailscaled` (the
installer never `sudo systemctl`-starts it on macOS). The daemon runs as a
launchd LaunchAgent (`com.projectmentor.hive-sync`), logging to
`~/Library/Logs/hive-mind/`. Get the node's IP with `tailscale ip`.

---

## Error Responses

All endpoints return JSON on error:

```json
{"error": "description"}
```

HTTP status codes: `200` success, `401` request-authentication failure (a remote
call to a remote-auth or loopback-only endpoint with a missing/invalid/stale
envelope, or a signer not in the admitted set — returned only under
`HIVE_SYNC_AUTH=enforce`; `permissive` logs and serves), `404` unknown path,
`409` cross-hive push refused (`POST /sync/ingest` when the sender's `hive_id`
differs from this node's), `500` internal error. A 409 body carries
`{"error": "different hive", "hive_id": "<local>", "accepted": 0}`. The daemon
never crashes a handler thread — all exceptions are caught and returned as 500.

---

## Rolling out enforcement

Read-authentication ships behind a per-node enforcement mode so a mixed-version
fleet never breaks mid-upgrade. The rollout is:

1. **Land the fix on every node.** Each updated daemon advertises
   `protocol_version: 2` and defaults to `HIVE_SYNC_AUTH=permissive` — clients
   sign every request, the daemon verifies and logs failures but still serves.
   Nothing breaks while nodes update at their own pace (an offline node keeps
   syncing under `permissive` when it returns).
2. **Confirm the whole fleet reports protocol 2.** Poll each peer:
   ```bash
   curl -s http://<peer>:9876/hive/info | jq .protocol_version    # expect 2
   ```
   `/hive/info` is open discovery, so this needs no auth.
3. **Flip each node to `enforce`, one at a time.** Once every peer reports
   protocol 2 (so every client is signing), run on each node:
   ```bash
   hv sync auth enforce
   ```
   From then on that node rejects an unauthenticated or invalid remote read with
   `401`. Local (`127.0.0.1`) access is unaffected.

To pause enforcement on a node, `hv sync auth permissive` (or `off`). See
`docs/CLI_REFERENCE.md` for the `hv sync auth` subcommand and the
`HIVE_SYNC_AUTH` / `HIVE_SYNC_AUTH_WINDOW` / `HIVE_BIND` environment variables.
