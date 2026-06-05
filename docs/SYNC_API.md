# HiveMind Sync API Reference

The sync daemon (`sync_daemon.py`) exposes a minimal JSON/HTTP API on port
`:9876` (default). All endpoints are unauthenticated — transport security is
provided by Tailscale (WireGuard). Only admit Tailscale peers.

Start the daemon:
```bash
./hv sync daemon          # serve + periodic outbound sync (every 5 min)
python3 sync_daemon.py    # same, direct
```

---

## Endpoints

### `GET /sync/hello`

Node identity and journal summary. Used as the handshake and peer-discovery
step during a sync round.

**Response:**
```json
{
  "node_id": "DESKTOP-EGMBL5A",
  "journal_summary": {
    "total": 48,
    "by_node": {
      "DESKTOP-EGMBL5A": 46,
      "gregorius": 2
    }
  },
  "chunks": {
    "DESKTOP-EGMBL5A": [
      "sha256:d46957a7b133e438...",
      "sha256:4bff2b51c9e3a2f0..."
    ],
    "gregorius": [
      "sha256:7a3f9c12e8b4d510..."
    ]
  }
}
```

| Field | Description |
|---|---|
| `node_id` | This node's identity (`HIVE_NODE_ID` or hostname) |
| `journal_summary.by_node` | Highest seq seen per source node — used for quick divergence detection |
| `chunks` | Per-node array of 100-entry chunk hashes (Merkle leaf hashes). Used to localize which windows need syncing |

---

### `GET /sync/merkle-root`

Global Merkle root over the entire journal. The O(1) fast-path: if two nodes
have identical root hashes, their journals are byte-identical and no sync work
is needed.

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
      "node_id": "DESKTOP-EGMBL5A",
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

Append foreign journal entries to this node's journal (G-Set union merge).
Deduplicates by `(node_id, seq)`. After accepting entries, triggers
`rebuild_db()` to recompute SQLite + confidence projection.

**Request body:**
```json
{
  "entries": [
    {
      "node_id": "gregorius",
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
  "node_id": "DESKTOP-EGMBL5A",
  "seq": 42,
  "type": "fact | decision | entity | entity_fact | retract | dispute | verify",
  "timestamp": "2026-06-04T12:00:00Z",
  "payload": { ... },
  "prev_hash": "sha256:...",
  "hash": "sha256:..."
}
```

| Field | Description |
|---|---|
| `node_id` | Authoring node (self-declared; Phase B1 adds transport-attribution sidecar) |
| `seq` | Per-node monotonic sequence number. `(node_id, seq)` is the global unique identity for cross-row links |
| `type` | Entry type (see below) |
| `timestamp` | ISO8601 UTC |
| `payload` | Type-specific data (see below) |
| `prev_hash` | Hash of the previous entry from this node — forms a per-node hash chain |
| `hash` | SHA256 of `(prev_hash + json(payload))` |

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
  "supersedes_ref": ["DESKTOP-EGMBL5A", 3]
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
  "entity_ref": ["DESKTOP-EGMBL5A", 10],
  "fact_ref": ["gregorius", 1],
  "confidence": 0.9
}
```

**`retract`**
```json
{
  "fact_ref": ["DESKTOP-EGMBL5A", 4],
  "reason": "test probe",
  "source": "hermes:primary/claude-sonnet/abc12345",
  "owner": false
}
```

---

## Configuration: `.peers.json`

```json
{
  "peers": [
    {
      "url": "http://100.114.200.119:9876",
      "node_id": "gregorius"
    },
    {
      "url": "http://100.95.128.118:9876",
      "node_id": "DESKTOP-EGMBL5A"
    }
  ],
  "bind": "0.0.0.0",
  "port": 9876
}
```

| Field | Default | Description |
|---|---|---|
| `peers[].url` | required | Base URL of the peer's sync daemon |
| `peers[].node_id` | optional | Human label for logging |
| `bind` | `0.0.0.0` | Interface to bind the daemon to |
| `port` | `9876` | Port the daemon listens on |

`.peers.json` is gitignored (contains Tailscale IPs). Copy from
`.peers.json.example` and edit per node.

**WSL + Tailscale note:** On Windows/WSL2 nodes, Tailscale is on the Windows
host (NAT). A `netsh portproxy` rule is required to forward the Tailscale IP
to the WSL IP on port 9876. See `scripts/install/01-windows-setup.bat`.

---

## Error Responses

All endpoints return JSON on error:

```json
{"error": "description"}
```

HTTP status codes: `200` success, `404` unknown path, `500` internal error.
The daemon never crashes a handler thread — all exceptions are caught and
returned as 500.
