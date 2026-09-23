# Hive Mind P2P Redundancy & Performance Design Document

**Status:** Historical design (written 2026-06-03). Sections 1–10 describe the peer-to-peer sync design
that shipped, with changes. Where this document and the code disagree, the code is authoritative, as are
`docs/SYNC_API.md` (the current HTTP API, including read authentication), `docs/INTERNALS.md` and
`docs/THREAT_MODEL.md`. The current contract version is reported by `hv version`.  
**Date:** 2026-06-03  
**Author:** Sonnet 4 (architectural design)

> Sections 11–16 (an earlier modular plugin, hot-swap and dashboard-terminal design) were removed on
> 2026-09-23: they no longer describe the project's direction. Sections 1–10 keep their numbers.

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

The index is built over the journal **as a G-Set**: entries are de-duped by
`(node_id, seq)` before chunking, so a physically-repeated journal line (recovery
artifact) cannot shift a chunk hash and fake a divergence that sync can't heal
(ingest de-dups by the same key, so the duplicate is never removed by syncing). A
deterministic tie-break (smallest canonical encoding) keeps the chosen
representative identical across nodes.

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
Runs on Tailscale interface only (port 9876). See `hive_sync_daemon.py`.

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

The shipped implementation lives in `hive_sync_daemon.py` and uses the Python stdlib
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

## Summary

### ORIGINAL ARCHITECTURE: Journal (G-Set) + Merkle Sync + LWW

- Journal/ = full backup; SQLite = derived
- Any node reconstructs alone; ~28 hrs in Phases 1-4

---

**END**
