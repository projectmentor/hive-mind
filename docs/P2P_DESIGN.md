# Hive Mind P2P Redundancy & Performance Design Document

**Status:** PROPOSED  
**Date:** 2026-06-03  
**Author:** Sonnet 4 (architectural design)

> **Shipped-since note.** This document remains the design narrative; much of it has since shipped. Contracts 1.1 → 1.9 landed, including the P2P sync daemon (Phase 1-4) and an owner-resilience governance arc: owner key backup/escrow/restore, nominated succession + immediate transfer, and quorum election + dead-man switch. These are exposed via `hv owner`, `hv group`, and `hv config` (`quorum_m` / `quorum_by` / `dead_man_days`). The code's current `CONTRACT_VERSION = "1.9"`. For the shipped command surface and internals, see `docs/CLI_REFERENCE.md` and `docs/INTERNALS.md`.

---

## 1. Problem Statement

**Current state:** Asymmetric push/pull requires one device to run `php artisan serve`. If the serving node dies, the other loses access to shared memory. Violates "house burns down, one laptop survives" requirement. (Resolved: the stdlib sync daemon shipped in Phase 3 removed this dependency.)

### Requirements:
- Full corpus on every device (no sharding)
- Any single survivor reconstructs everything
- No central server / no primary
- Offline writes allowed, merged later
- Deterministic conflict resolution
- Scale: 2-10 nodes, ~50MB corpus over 1 year
- Network: Tailscale, ~5ms RTT

---

## 2. P2P Architecture Options Evaluated

### OPTION A: Gossip Protocol (Epidemic Replication)
- **Pros:** Extremely resilient, handles churn, well-understood
- **Cons:** Convergence probabilistic, bandwidth overhead, moderate complexity. Overkill for 2-10 nodes.
- **Verdict:** GOOD conceptually, but gossip shines at 100+ nodes.

### OPTION B: CRDT-Based Merge
- **Pros:** Mathematically guaranteed convergence, no conflict code
- **Cons:** Tombstone accumulation, must structure ALL data as CRDTs
- **Verdict:** EXCELLENT for journal (already a G-Set). Overkill for decisions which have explicit supersedence chains.

### OPTION C: Merkle Tree Sync + LWW
- **Pros:** Bandwidth-efficient (delta-only), deterministic, simple, natural integrity verification. Used by git/rsync/IPFS.
- **Cons:** Needs tree maintenance, sync sessions required.
- **Verdict:** EXCELLENT — maps directly onto JSONL journal + SQLite.

### OPTION D: Raft Consensus
- **Pros:** Strong consistency
- **Cons:** Requires LEADER (violates no-primary), needs quorum (can't work with 1-of-2 surviving), high complexity.
- **Verdict:** TERRIBLE — exactly wrong tradeoff for this use case.

### RECOMMENDATION: Hybrid of B + C
- **Journal layer:** G-Set CRDT (trivial — append-only union)
- **Sync layer:** Merkle tree for efficient delta detection
- **Mutable fields:** LWW with node_id tiebreaker

---

## 3. Recommended Architecture

**PRINCIPLE: The Journal Is the database. SQLite is a cache/index.**

```
    Node A                    Node B                   Node C
  [hv CLI]                  [hv CLI]                 [hv CLI]
      |                         |                        |
  [Journal/]                [Journal/]               [Journal/]
  (JSONL - SOURCE OF TRUTH) (JSONL - SOURCE OF TRUTH)
      |                         |                        |
  [SQLite]                  [SQLite]                 [SQLite]
  (derived index/cache)     (derived index/cache)
      |                         |                        |
  [Merkle Index]            [Merkle Index]           [Merkle Index]
      |                         |                        |
      +------ SYNC (HTTP over Tailscale) ----------------+
```

### Sync Flow:
1. Compare Merkle root hashes (1 round-trip)
2. If match: done (0 bytes)
3. If mismatch: binary-search tree for differing chunks
4. Exchange only missing journal entries
5. Append to local journal (G-Set union — trivial merge)
6. Rebuild SQLite derived state from merged journal
7. Recompute Merkle index

### Why Any Single Node Suffices:
- Journal/ directory contains ALL entries from ALL nodes
- SQLite and Merkle index are derivable from journal
- `hv doctor rebuild` reconstructs everything from journal alone
- Copy journal/ to a new machine = full restoration

---

## 4. Data Model Changes

### 4.1 Enhanced Journal Entry Format:

```json
{
  "node_id": "k1:597b3e0f5fb92d37",
  "seq": 147,
  "type": "fact|decision|entity|entity_fact",
  "timestamp": "2026-06-03T14:30:00.123Z",
  "payload": { "... type-specific ..." },
  "prev_hash": "sha256:abc123...",
  "pub": "<base64 Ed25519 pubkey>",
  "sig": "<base64 signature>"
}
```

Composite key `(node_id, seq)` = globally unique, no coordination. As implemented,
`node_id` is a cryptographic **device identity** (`k1:` + `sha256(pubkey)[:16]`), not
a self-declared name, and entries are signed and verified on ingest so a peer cannot
forge another node's `node_id`. See `docs/INTERNALS.md` "Device identity". The LWW
tiebreaker below (`max(node_id)` lexicographically) is unaffected — device ids are
ordinary comparable strings.

### 4.2 Merkle Index:

Chunks of 100 journal entries, each with a SHA-256 hash.  
Tree depth: `log2(total_entries / 100)`.  
For 10,000 entries: ~7 levels, 128 chunks. Comparing two trees takes 7 round-trips worst case.

### 4.3 Peer Registry (.peers.json):

```json
{
  "self": "node-a",
  "bind": "0.0.0.0",
  "port": 9876,
  "peers": [
    {"id": "node-b", "url": "http://100.64.0.2:9876"}
  ]
}
```

---

## 5. Sync Protocol (5 endpoints per node)

All endpoints advertise `protocol_version` (`PROTOCOL_VERSION = 1`) so additive
handshake changes can be negotiated without a journal-schema break.

```
GET /sync/hello
  -> {"node_id": "...", "hive_id": "...", "protocol_version": 1,
      "journal_summary": {"total": 250,
        "by_node": {"node-a": 147, "node-b": 89}},
      "chunks": {"node-a": ["sha256:...", ...], "node-b": [...]}}
  (chunk hashes are folded into hello — no separate chunks endpoint)

GET /sync/merkle-root
  -> {"root_hash": "sha256:..."}

GET /sync/chunk?node=X&start=1&end=100
  -> {"entries": [...], "hash": "sha256:..."}

POST /sync/ingest
  <- {"entries": [...]}
  -> {"accepted": 42, "duplicates": 3}

GET /hive/info   (discovery; never returns the journal)
  -> {"hive_id": "...", "owner_id": "...", "label": "...",
      "node_count": 2, "protocol_version": 1, "genesis": {...}}
```

**Implementation:** Python stdlib `http.server` (`ThreadingHTTPServer` +
`BaseHTTPRequestHandler`), synchronous, 5 endpoints — no FastAPI dependency.
Runs on Tailscale interface only (port 9876). See `sync_daemon.py`.

---

## 6. Conflict Resolution

| Type         | Mutability         | Strategy                          |
|--------------|--------------------|-----------------------------------|
| Facts        | Immutable          | Different UUIDs, no conflict      |
| Decisions    | Superseded (chain) | LWW on supersedence timestamp     |
| Entities     | Name/tags mutable  | LWW by update timestamp           |
| Entity_Facts | Link/unlink        | OR-Set (presence wins)            |
| Journal      | Never modified     | G-Set union (impossible to conflict) |
| Governance   | Owner-signed acts  | Deterministic projection (`_governance_state`) |
| Owner        | Succession chain   | Replay `(ts,node_id,seq)`; first valid act wins |

**Owner succession convergence.** Ownership is not LWW — it is a chain replayed in
`(timestamp, node_id, seq)` order from the term-0 TOFU owner. Each handoff requires a
signed act by the *then-current* owner (`nominate-successor`/`transfer`) plus, for
nomination, the nominee's self-signed `claim-succession` against an open nomination.
Because the inputs are the converged G-Set journal and the order is total, every node
computes the identical current owner regardless of sync arrival order. Two claims racing
for one nomination resolve to the first in sort order (the same one on every node); a
handed-off owner's later acts are ignored, and a live owner cannot be unseated.

**Quorum-election convergence.** When the owner is lost with no backup/nominee, admitted
devices elect a successor with *device-signed* `propose-election` + `vote-election` acts,
tallied in the same total-order replay. The election installs only when `quorum_m` distinct
admitted voter-units endorse one (content-addressed) proposal **and** the owner's last
activity is older than `dead_man_days` measured against the proposal's signed `basis_ts` —
a deterministic, journal-only test, so every node installs the same owner at the same log
position (earliest quorum-crossing wins; later racing elections see a fresh owner and can't
arm). The dead-man switch is what reconciles "elect when the owner is gone" with "never
unseat a live owner": any owner-signed act, including `heartbeat`, refreshes last-activity.
`quorum_m=0` (default) disables elections, leaving the projection identical to succession-only.

**TIE-BREAKER when timestamps identical:**  
`winner = max(node_id) lexicographically`  
(Deterministic across all nodes = guaranteed convergence)

**Practical note:** ISO8601 timestamps with millisecond precision make same-timestamp collisions astronomically unlikely.

---

## 7. Migration Path (from current MVP)

### PHASE 1 — Foundation (Week 1, ~6 hrs):
- [ ] Enable SQLite WAL mode + PRAGMA tuning (5 min)
- [ ] Add node_id + seq to all journal entries
- [ ] Build Merkle index generator over journal chunks
- [ ] Replace SQL LIKE with FTS5 for search
- [ ] Ensure ALL writes go to journal first

### PHASE 2 — Sync Daemon (Week 2, ~10 hrs):
- [ ] FastAPI sync daemon (4 endpoints above)
- [ ] Merkle-based delta detection logic
- [ ] Journal ingestion (G-Set merge with dedup index)
- [ ] `hv sync now` — one-shot bidirectional sync
- [ ] `hv sync daemon` — background auto-sync (5min interval)

### PHASE 3 — Journal-First (Week 3, ~8 hrs):
- [ ] Make SQLite fully derived (rebuilt from journal)
- [ ] `hv doctor rebuild` command
- [ ] All CLI commands write journal-first, update SQLite async
- [ ] Remove Laravel sync dependency
- [ ] Auto-rebuild on ingest

### PHASE 4 — Hardening (Week 4, ~4 hrs):
- [ ] `hv sync status` — peer divergence report
- [ ] Integrity verification on startup
- [ ] Graceful degradation when peers offline
- [ ] Documentation

**TOTAL: ~28 hours of focused implementation.**

---

## 8. Performance Optimizations (Ranked by ROI)

### TIER 1 — DO NOW (high impact, minimal effort):

#### 1a. SQLite WAL Mode + PRAGMAs (5 minutes)
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;      -- 64MB page cache
PRAGMA mmap_size=268435456;    -- 256MB mmap
```
**Impact:** 5-10x concurrent reads. **Risk:** NONE.

#### 1b. FTS5 Full-Text Search Index (3 hours)
```sql
CREATE VIRTUAL TABLE facts_fts USING fts5(
    content, tags, content='facts', content_rowid='id'
);
```
**Impact:** 100-1000x faster search. **Risk:** LOW.

#### 1c. Persistent SQLite Connection (30 minutes)
Keep connection alive per process instead of open/close per query. Eliminates 5-20ms overhead per call.  
**Impact:** Noticeable for repeated CLI usage. **Risk:** NONE.

---

### TIER 2 — DO AT SCALE (measure first):

#### 2a. Precomputed Materialized Views (3 hours)
Maintain `mv_recent_facts`, `mv_entity_summary` tables refreshed on journal ingest.  
**Impact:** Instant dashboard response.

#### 2b. Redis Read-Cache (6 hours + operational cost)
Cache recent search results (TTL 60s), entity lookups.  
**Impact:** Sub-ms for repeated queries.  
**VERDICT:** OVERKILL — SQLite FTS5 on 50MB is <10ms. Only add if corpus exceeds 500MB or latency >200ms.

#### 2c. Compiled CLI via PyInstaller (1 hour, NOT full rewrite)
Bundle Python CLI into single binary.  
**Impact:** 3-5x faster startup. **Effort:** trivial.  
**DO NOT rewrite in Rust/Go** (80+ hours for no user benefit).

---

### TIER 3 — AVOID:

- 3a. Vector embeddings / semantic search (separate project)
- 3b. Bloom filters (dedup by indexed composite key is O(1))
- 3c. Wire compression (50MB over Tailscale = <1 second uncompressed)
- 3d. Custom binary protocol (JSON over HTTP is debuggable & fine)

---

## 9. Anti-Patterns to Avoid

1. **Raft/Paxos** — you need availability, not consensus
2. **Sharding** — 50MB doesn't justify partitioning
3. **Replacing SQLite with Redis** — you need joins/FTS/transactions
4. **Custom binary protocols** — JSON over HTTP is fine on Tailscale
5. **Docker/orchestration for daemon** — it's a background process
6. **Web UI for sync management** — CLI + logs for 2-10 nodes
7. **Full CRDT library** — only journal needs it (and it's trivial)
8. **Encryption on top of Tailscale** — already WireGuard-encrypted
9. **Blockchain/git for journal** — JSONL + prev_hash chain suffices
10. **Optimizing before measuring** — profile `hv search` FIRST

---

## 10. Appendix: Sync Daemon Sketch

The shipped implementation lives in `sync_daemon.py` and uses the Python stdlib
`http.server` (`ThreadingHTTPServer` + `BaseHTTPRequestHandler`) — synchronous,
no FastAPI/uvicorn dependency. A `Handler` dispatches on `self.path`:

- `GET /sync/hello` → `node_id`, `hive_id`, `protocol_version`, journal summary
  (`total` + per-node `by_node` maxima), and per-node `chunks` hashes (chunk
  hashes are folded into hello; there is no `/sync/chunks` endpoint).
- `GET /sync/merkle-root` → `{"root_hash": merkle_root(chunk_hashes(entries))}`.
- `GET /sync/chunk?node=X&start=1&end=100` → `{"entries": [...], "hash": ...}`.
- `POST /sync/ingest` → append foreign entries (G-Set dedup by `(node_id, seq)`),
  rebuild SQLite if anything was accepted, return `{"accepted", "duplicates"}`.
  Refuses a cross-hive push when both sides carry a differing `hive_id` (HTTP 409).
- `GET /hive/info` → discovery metadata (`hive_id`, `owner_id`, `label`,
  `node_count`, `protocol_version`, signed `genesis`); never returns the journal.

```python
# Ingest core (G-Set union — append + dedup, then rebuild derived state):
accepted, duplicates = append_foreign_entries(body["entries"])  # dedup on (node_id, seq)
if accepted:
    rebuild_db()

# Merkle tree construction:
leaves = [hash(chunk) for chunk in sorted_chunks]
while len(leaves) > 1:
    leaves = [sha256(leaves[i] + leaves[i+1])
              for i in range(0, len(leaves), 2)]
root = leaves[0]
```

---

## 11. Modular Architecture (Design)

The system is evolving from a monolith (core Python script + Laravel UI)
into a **plugin-based framework** where every capability is a module.
This is the meta-architecture that enables all the hot-swap features
below.

### 11.1 Core vs Modules

The core's ONLY job is to load modules and route messages. Everything
else is a module:

```
CORE (never changes without a core release):
  ├── Journal format (G-Set, append-only — immutable contract)
  ├── Module loader + registry
  ├── Module manifest schema
  ├── CLI dispatcher (routes commands to declaring module)
  ├── Dashboard shell (Laravel + HTMX — hosts module views)
  └── HTTP/WebSocket transport (Tailscale, module-agnostic)

MODULE TYPES (each a plugin):
  ├── db-driver        → SQLite, Postgres, MySQL, etc.
  ├── sync-algorithm   → Merkle+LWW, CRDT, gossip, enterprise variants
  ├── cli-command      → remember, search, decide, entity, sync, ...
  ├── dashboard-view   → stats, search UI, terminal, module manager
  ├── api-endpoint     → /sync/*, /modules/*, /terminal/*, ...
  └── integration      → Hermes memory provider, Claude Code hooks, etc.
```

### 11.2 Module Manifest

Each module declares its identity, capabilities, and lifecycle hooks:

```yaml
# modules/db-sqlite/module.yaml
name: db-sqlite
version: 1.0.0
type: db-driver
description: "SQLite backend with WAL + FTS5"
requires:
  core: ">=1.0"
provides:
  db-backend: sqlite
exposes:
  cli: []                    # this type doesn't add CLI commands
  dashboard_views: []        # maybe a config screen later
  api_endpoints: []
hooks:
  on_activate: migrate_from_journal   # rebuild state from journal
  on_deactivate: export_state         # dump to portable format
  on_ingest: persist_journal_entry    # called after journal append
  on_rebuild: from_journal
```

### 11.3 Module Lifecycle

```
  AVAILABLE  ──install──▶  INSTALLED  ──activate──▶  ACTIVE   ─┐
        ▲                        ▲                       │       │
        │                        │                       │       │
    uninstall              update               deactivate       │
        │                        │                       │       │
        └────────────────────────┴───────────────────────┘       │
                                                                 │
  Per-type exclusivity: only one `db-driver` can be ACTIVE    ───┘
  Per-type multiplicity: many `cli-command` can be ACTIVE simultaneously
```

**Activation rules:**
- `db-driver`: exclusive (one active at a time; switching triggers migration)
- `sync-algorithm`: exclusive
- `cli-command`: non-exclusive (stacking)
- `dashboard-view`: non-exclusive (each gets a menu entry)
- `api-endpoint`: non-exclusive
- `integration`: non-exclusive

### 11.4 Dashboard Module Manager

A menu item "Modules" in the dashboard exposes:
- **Installed**: list with activate/deactivate/uninstall/update buttons
- **Available**: registry feed (URL configured in settings, default: official)
- **Logs**: module install/uninstall/activate events
- **Conflicts**: warnings when activating a module that conflicts with active ones
- **Dependencies**: auto-resolve missing deps on install

The CLI exposes equivalent commands for SSH/automation contexts:
```bash
hv module list
hv module install <name>[@version]
hv module activate <name>
hv module deactivate <name>
hv module update <name>
hv module repo add <url>
```

---

## 12. Hot-Swappable Database Layer (Design)

**Why:** SQLite is right for 1-10 node deployments. Enterprise users may
want Postgres for multi-TB corpora, shared infrastructure, or existing
DBA teams. The layer between CLI and storage must be driver-agnostic.

### 12.1 Driver Interface

Every `db-driver` module implements this contract:

```python
class HivemindDBDriver(Protocol):
    # Lifecycle
    def initialize(self) -> None: ...
    def teardown(self) -> None: ...
    def health_check(self) -> dict: ...   # {"status": "ok", "latency_ms": 3, ...}

    # Write path (called after journal append)
    def persist_fact(self, entry: JournalEntry) -> int: ...
    def persist_decision(self, entry: JournalEntry) -> int: ...
    def persist_entity(self, entry: JournalEntry) -> int: ...

    # Read path
    def search_facts(self, query: str, limit: int) -> list[dict]: ...
    def get_fact(self, fact_id: int) -> dict | None: ...
    def list_decisions(self, active_only: bool) -> list[dict]: ...
    def get_entity(self, name: str) -> dict | None: ...
    def stats(self) -> dict: ...   # fact_count, decision_count, etc.

    # Hot-swap support
    def import_from_journal(self, journal_path: Path) -> None: ...
    def export_full(self, output_path: Path) -> None: ...

    # Driver-specific config
    @classmethod
    def config_schema(cls) -> dict: ...   # JSON schema for .env / UI form
```

### 12.2 Hot-Swap Flow

```
  [db-sqlite ACTIVE]
           │
           ▼  user clicks "Activate db-postgres"
  [core runs activation sequence]:
      1. install+load db-postgres module
      2. db-postgres.initialize()   → connect, run migrations
      3. db-postgres.import_from_journal(journal/)
         (reads every journal line, calls persist_* for each)
         progress bar in dashboard: "Migrating 2,847 entries..."
      4. db-sqlite.deactivate()    → export_state + release connection
      5. registry marks db-postgres ACTIVE
      6. all future reads go to Postgres
           │
           ▼
  [db-postgres ACTIVE, db-sqlite INSTALLED]
```

### 12.3 Rollback Safety

If migration fails mid-way (e.g., Postgres OOM), the system:
- Keeps the old driver ACTIVE
- Logs partial state under `modules/db-postgres/migrations/failed/<ts>/`
- Dashboard shows "activation failed" with the error
- No data loss: journal is unchanged, old driver still works

### 12.4 Default Bundled Drivers

- `db-sqlite` (shipped with core, always available)
- `db-postgres` (official module, installable)
- `db-memory` (dev/testing, in-memory only, no persistence)

Third-party drivers can provide MySQL, ClickHouse, etc.

---

## 13. Hot-Swappable Sync/Reconstruction Algorithm (Design)

**Why:** The Merkle+LWW recipe in §3-6 is tuned for small teams. An
enterprise deployment with 5,000 nodes needs a different sync algorithm
(gossip + vector clocks). A regulated deployment might want
operational-transform merges with audit trails.

### 13.1 Sync Algorithm Interface

```python
class HivemindSyncAlgorithm(Protocol):
    name: str
    version: str

    # Peer handshake (replaces §5 protocol)
    def handshake_request(self, local_state: dict) -> dict: ...
    def handshake_response(self, remote_request: dict, local_state: dict) -> dict: ...

    # Delta detection (what to exchange)
    def compute_needed_entries(self, handshake, local_state) -> list[EntryKey]: ...

    # Merge policy (conflict resolution)
    def merge_entries(self, local, remote) -> tuple[list[JournalEntry], list[ConflictRecord]]: ...

    # Rebuild derived state (called after merge)
    def rebuild_derived(self, journal_path: Path, db: HivemindDBDriver) -> None: ...

    # Optional: streaming sync for large corpora
    def sync_stream(self, peer: PeerInfo, chunk_size: int) -> Iterator[JournalEntry]: ...
```

### 13.2 Default Algorithm

`sync-merkle-lww` ships with core. It implements §3-6 of this document.
Suitable for 2-10 nodes, < 500MB corpus, low-latency networks.

### 13.3 Negotiation

On `/sync/hello`, peers exchange algorithm metadata:
```json
{"algorithms": ["sync-merkle-lww/1.0", "sync-gossip-v2/1.0"]}
```
Both sides fall back to the highest common protocol. If no overlap,
sync is refused with an error suggesting which module to install.

### 13.4 Available Algorithms (Future)

| Module               | Use case                                    |
|----------------------|---------------------------------------------|
| `sync-merkle-lww`    | Small teams (2-10 nodes) — default          |
| `sync-merkle-crdt`   | Offline-heavy nodes, need strict CRDT merge |
| `sync-gossip`        | Many nodes (50+), eventual consistency      |
| `sync-raft-lite`     | Strong consistency for small cluster        |
| `sync-audit`         | Regulated: every conflict logged, human-resolved |
| `sync-enterprise-*`  | Vendor-published for specific deployments   |

### 13.5 Hot-Swap Flow

Activating a new sync algorithm takes effect on the **next sync
session** (no migration needed — the algorithm is stateless; the
journal is unchanged). Peers negotiate protocol version at each
handshake, so different algorithms can coexist across peers during
a gradual rollout.

---

## 14. Dashboard Terminal (Design)

**Why:** Every `hv` CLI command should be invocable from the dashboard
with real-time output, for testing, debugging, and ad-hoc operations on
headless nodes where SSH isn't convenient.

### 14.1 UX

A new dashboard view: "Terminal" (or "Console").

```
  ┌─────────────────────────────────────────────────────────┐
  │  $ hv search "parallel delegation"           [Run ▶]   │
  ├─────────────────────────────────────────────────────────┤
  │  [2] Sam prefers parallel delegation over sequential │
  │       Tags: workflow, preference  Trust: 1.10           │
  │       Source: manual                                    │
  │                                                         │
  │  [1] Sam prefers parallel delegation over sequential │
  │       Tags: workflow, preference  Trust: 1.10           │
  │       Source: manual                                    │
  └─────────────────────────────────────────────────────────┘
  
  [Command history]  [Saved snippets]  [Export log]
```

### 14.2 Security Model

Terminal access is NOT unrestricted shell. It's scoped:

- **ONLY `hv` commands**: input is validated as `hv <subcommand> <args>`
  and dispatched via the CLI dispatcher (same path as real CLI).
- **No raw shell**: `hv shell`, `!`, `|`, `;`, `` ` `` ` are all rejected.
- **Audit log**: every command + output stored to `logs/terminal.csvl`
  (command, user, timestamp, exit code, full output).
- **Permission per module**: each `cli-command` module declares a
  required permission level (`read`, `write`, `admin`). Dashboard users
  are assigned roles.
- **No secrets leakage**: terminal output is filtered against a denylist
  of patterns (`API_KEY=`, `password=`, etc.) before being rendered.

### 14.3 Implementation Shape

- **Backend**: Laravel controller + SSE (Server-Sent Events) stream
  OR WebSocket via Laravel Reverb — choice depends on what's already
  in the stack when this is built.
- **Frontend**: xterm.js (proven terminal emulator component) rendered
  in a dashboard view.
- **Execution**: the controller invokes `hv` via `proc_open()` with
  stdout/stderr piped to the stream, then closes the process.

### 14.4 Long-running Commands

Some commands take time (`hv sync now`, `hv doctor rebuild`). The terminal
must stream partial output incrementally:
- Progress bars render live (ANSI escape codes passed through xterm.js)
- Ctrl+C sent from the dashboard sends SIGINT to the child process
- Timeout: each command has a configurable max duration (default 5m)

---

## 15. Updated Phase Plan

The original Phases 1-4 (P2P sync) proceed **unblocked** by these
new capabilities. The modular system is a separate stream.

### Stream A — P2P Sync (original §7, ~28 hrs)

- **Phase 1** Foundation: WAL, journal format, Merkle index, FTS5
- **Phase 2** Sync Daemon: FastAPI daemon, delta detection, merge
- **Phase 3** Journal-First: SQLite derived, `hv doctor rebuild`
- **Phase 4** Hardening: status, integrity, docs

### Stream B — Modularity (new, ~40 hrs)

- **Phase 5** Plugin foundation (~12 hrs): manifest schema, loader,
                registry, activation hooks, CLI `hv module` commands
- **Phase 6** Extract to modules (~14 hrs): break current code into
                `db-sqlite`, `sync-merkle-lww`, `cli-remember`,
                `cli-search`, `cli-decide`, `cli-entity`, `cli-sync`,
                `db-dashboard`, `api-sync`, `integration-hermes` modules.
                **Migration guide required.**
- **Phase 7** Dashboard module manager (~8 hrs): list/install/activate UI
- **Phase 8** Dashboard terminal (~6 hrs): xterm.js view + SSE/WS backend

### Stream C — Hot-Swap Capabilities (after Phase 6, ~20 hrs)

- **Phase 9** DB driver interface (~8 hrs): refactor init_db, abstract
                read/write, ship `db-postgres` reference implementation
- **Phase 10** Sync algo interface (~8 hrs): refactor sync daemon, ship
                `sync-merkle-crdt` reference alternative
- **Phase 11** Dashboard integration polish (~4 hrs): settings per module,
                health checks, module logs viewer

### Recommended Execution Order

1. Build Phase 1-4 (P2P sync) on the monolith first
2. THEN implement Phase 5 (plugin foundation)
3. THEN Phase 6 (extraction) — this is the breaking change
4. THEN Phase 7-8 (UI) and Phase 9-10 (hot-swap) in parallel

Doing modularity BEFORE P2P sync would slow both streams. Building
sync first on the monolith, then extracting it into a module, gives a
clean test suite for the extraction.

---

## 16. Design Principles (Revised)

In addition to §3-6 and §9, these hold for modules and hot-swap:

1. **The journal is the contract between modules.** Every module reads
   from or writes to the journal. This is what makes hot-swap safe.
2. **Modules are isolated processes or in-process plugins.** Default
   in-process; enterprise may want out-of-process for sandboxing.
3. **Default modules always ship with core.** Users should never need
   internet to get a working system on first install.
4. **No module can corrupt the journal.** Journal writes go through
   core's append-only API; modules never touch the raw file.
5. **Activation is atomic.** If a module fails to activate, the system
   rolls back to the previously-active module of that type.
6. **Dashboard terminal is a VIEW of the CLI, not a shell.** It invokes
   the same code path as the real CLI and has the same security model.

---

## Summary

### ORIGINAL ARCHITECTURE: Journal (G-Set) + Merkle Sync + LWW

- Journal/ = full backup; SQLite = derived
- Any node reconstructs alone; ~28 hrs in Phases 1-4

### NEW DESIGN-LEVEL CAPABILITIES (Phases 5-11, ~60 hrs after Phase 4)

| Capability                         | Enabler                          |
|------------------------------------|----------------------------------|
| Hot-swap DB (SQLite → Postgres)    | Driver interface + journal rebuild |
| Hot-swap sync algorithm            | Sync protocol interface + negotiation |
| Modular install/activate via UI    | Plugin loader + manifest + registry |
| Every CLI command runnable in UI   | xterm.js + streamed proc_open    |

### KEY DEPENDENCIES

- Modularity (§11) must be done BEFORE the hot-swap features make sense
- P2P sync (§3-6) is UNBLOCKED by modularity — build first, extract later
- Dashboard terminal (§14) is independent — can ship any time after Phase 7

### ANTI-PATTERNS (expanded list)

Original 1-10 from §9 remain. Additional ones for this design:

11. **Putting module registry inside the journal** — registry is local
    state; journal is replicated state. Keep them separate.
12. **Allowing modules to write to journal directly** — core's append
    API only; modules call `core.append_journal(entry)`.
13. **Dashboard terminal as a raw shell** — huge security surface.
    Scope to `hv` commands only.
14. **Bundling every database driver by default** — ship `db-sqlite`
    only; users install Postgres when they need it (avoids binary size
    / dependency bloat).
15. **Hot-swapping mid-sync** — require current sync session to
    complete before switching sync algorithms.

### FIRST COMMAND TO RUN TODAY (unchanged):

```bash
sudo apt install sqlite3  # if not already installed
sqlite3 ~/projects/hive-mind/store.db "PRAGMA journal_mode=WAL;"
sqlite3 ~/projects/hive-mind/store.db "PRAGMA synchronous=NORMAL;"
sqlite3 ~/projects/hive-mind/store.db "PRAGMA cache_size=-64000;"
```

---

**END**
