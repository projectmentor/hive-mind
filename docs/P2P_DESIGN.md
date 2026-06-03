# Hive Mind P2P Redundancy & Performance Design Document

**Status:** PROPOSED  
**Date:** 2026-06-03  
**Author:** Sonnet 4 (architectural design)

---

## 1. Problem Statement

**Current state:** Asymmetric push/pull requires one node to run `php artisan serve`. If the serving node dies, the other loses access to shared memory. Violates "house burns down, one laptop survives" requirement.

### Requirements:
- Full corpus on every node (no sharding)
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
- `hv rebuild` reconstructs everything from journal alone
- Copy journal/ to a new machine = full restoration

---

## 4. Data Model Changes

### 4.1 Enhanced Journal Entry Format:

```json
{
  "node_id": "desktop-egmbl5a",
  "seq": 147,
  "type": "fact|decision|entity|entity_fact",
  "timestamp": "2026-06-03T14:30:00.123Z",
  "payload": { "... type-specific ..." },
  "prev_hash": "sha256:abc123..."
}
```

Composite key `(node_id, seq)` = globally unique, no coordination.

### 4.2 Merkle Index:

Chunks of 100 journal entries, each with a SHA-256 hash.  
Tree depth: `log2(total_entries / 100)`.  
For 10,000 entries: ~7 levels, 128 chunks. Comparing two trees takes 7 round-trips worst case.

### 4.3 Peer Registry (.peers.json):

```json
{
  "self": "desktop-egmbl5a",
  "peers": [
    {"id": "gregorius", "ip": "100.114.200.119", "port": 9876}
  ]
}
```

---

## 5. Sync Protocol (4 endpoints per node)

```
GET /sync/hello
  -> {"node_id": "...", "journal_summary": {"total": 250,
      "by_node": {"desktop-egmbl5a": 147, "gregorius": 89}}}

GET /sync/merkle-root
  -> {"root_hash": "sha256:..."}

GET /sync/chunk?node=X&start=1&end=100
  -> {"hash": "sha256:...", "entries": [...]}

POST /sync/ingest
  <- {"entries": [...]}
  -> {"accepted": 42, "duplicates": 3}
```

**Implementation:** FastAPI (async, 4 endpoints, ~100 lines).  
Runs on Tailscale interface only (port 9876).

---

## 6. Conflict Resolution

| Type         | Mutability         | Strategy                          |
|--------------|--------------------|-----------------------------------|
| Facts        | Immutable          | Different UUIDs, no conflict      |
| Decisions    | Superseded (chain) | LWW on supersedence timestamp     |
| Entities     | Name/tags mutable  | LWW by update timestamp           |
| Entity_Facts | Link/unlink        | OR-Set (presence wins)            |
| Journal      | Never modified     | G-Set union (impossible to conflict) |

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
- [ ] `hv rebuild` command
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

```python
# sync_daemon.py (~100 lines core)

from fastapi import FastAPI
import hashlib, uvicorn

app = FastAPI()

@app.get("/sync/merkle-root")
def merkle_root():
    return {"root_hash": compute_merkle_root()}

@app.get("/sync/chunks")
def all_chunk_hashes():
    return {"chunks": compute_chunk_hashes()}

@app.get("/sync/chunk")
def get_chunk(node: str, start: int, end: int):
    entries = read_journal_range(node, start, end)
    return {"entries": entries, "hash": hash_entries(entries)}

@app.post("/sync/ingest")
def ingest(body: dict):
    accepted = 0
    for entry in body["entries"]:
        key = (entry["node_id"], entry["seq"])
        if not journal_has_entry(key):
            append_to_journal(entry)
            accepted += 1
    if accepted > 0:
        rebuild_sqlite_from_journal()
    return {"accepted": accepted}

# Merkle tree construction:
leaves = [hash(chunk) for chunk in sorted_chunks]
while len(leaves) > 1:
    leaves = [sha256(leaves[i] + leaves[i+1])
              for i in range(0, len(leaves), 2)]
root = leaves[0]
```

---

## Summary

### ARCHITECTURE: Journal (G-Set) + Merkle Sync + LWW Resolution

- **Journal/ directory IS the full backup** (copy it = restore everything)
- **SQLite and Merkle index are derivable** (rebuilt with `hv rebuild`)
- **Sync** = compare hash trees, exchange missing entries, merge
- **No server, no leader, no consensus** — any node works alone
- **~28 hours implementation**, FastAPI daemon + existing Python stack

### PERFORMANCE (do immediately):

1. **SQLite WAL mode** (5 min, 5-10x reads)
2. **FTS5 search index** (3 hrs, 100x search)
3. **Connection reuse** (30 min, eliminates per-query overhead)

### FIRST COMMAND TO RUN ON BOTH NODES TODAY:

```bash
sudo apt install sqlite3  # if not already installed
sqlite3 ~/projects/hive-mind/store.db "PRAGMA journal_mode=WAL;"
sqlite3 ~/projects/hive-mind/store.db "PRAGMA synchronous=NORMAL;"
sqlite3 ~/projects/hive-mind/store.db "PRAGMA cache_size=-64000;"
```

---

**END**
