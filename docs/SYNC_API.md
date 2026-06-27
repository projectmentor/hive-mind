# HiveMind Sync API Reference

The sync daemon (`hive_sync_daemon.py`) exposes a minimal JSON/HTTP API on port
`:9876` (default). All endpoints are unauthenticated — transport security is
provided by Tailscale (WireGuard). Only admit Tailscale peers.

Start the daemon:
```bash
./hv sync daemon          # serve + periodic outbound sync (every 5 min)
python3 hive_sync_daemon.py    # same, direct
```

---

## Endpoints

### `GET /sync/hello`

Node identity and journal summary. Used as the handshake and peer-discovery
step during a sync round.

**Response:**
```json
{
  "node_id": "node-a",
  "hive_id": "k1:2a2110f3d8963a9e",
  "protocol_version": 1,
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
| `protocol_version` | Sync wire-protocol version (currently `1`). Bumped for additive handshake changes so they can be negotiated without a journal-schema break |
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
hive stays open even if reads are gated later.

**Response:**
```json
{
  "hive_id": "k1:2a2110f3d8963a9e",
  "owner_id": "k1:2a2110f3d8963a9e",
  "label": "node-a",
  "node_count": 2,
  "protocol_version": 1,
  "genesis": { "node_id": "k1:...", "seq": 1, "type": "governance", "payload": { ... } }
}
```

| Field | Description |
|---|---|
| `hive_id` | The hive's identity (the founding owner's device id) — scopes all sync; cross-hive merges are refused |
| `owner_id` | Current owner's device id from governance state |
| `label` | This node's human-readable label (`HIVE_NODE_LABEL`) |
| `node_count` | Number of admitted nodes (falls back to the count of distinct authoring nodes if no admit set exists) |
| `protocol_version` | Sync wire-protocol version (see `/sync/hello`) |
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
  "bind": "0.0.0.0",
  "port": 9876
}
```

| Field | Default | Description |
|---|---|---|
| `peers[].url` | required | Base URL of the peer's sync daemon |
| `peers[].node_id` | optional | The peer's device id (`k1:…`), for logging and the admitted-peer set |
| `bind` | `0.0.0.0` | Interface to bind the daemon to |
| `port` | `9876` | Port the daemon listens on |

Get a node's device id and public key with `hv config identity show` on that machine.

`.peers.json` is gitignored (contains Tailscale IPs). Copy from
`config/.peers.json.example` and edit per node.

**WSL + Tailscale note:** Each WSL2 instance gets its own Tailscale IP (appears
as a separate machine on the tailnet, e.g. `node-a-1`). The sync daemon
binds `0.0.0.0:9876` and is reachable directly at the WSL Tailscale IP. No
portproxy or mirrored networking needed. Get the WSL IP with `tailscale ip`.

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

HTTP status codes: `200` success, `404` unknown path, `409` cross-hive push
refused (`POST /sync/ingest` when the sender's `hive_id` differs from this
node's), `500` internal error. A 409 body carries `{"error": "different hive",
"hive_id": "<local>", "accepted": 0}`. The daemon never crashes a handler
thread — all exceptions are caught and returned as 500.
