# Claude Code Project Context — Hive Mind

## What This Is
Institutional memory as observable middleware for multi-agent AI systems
(Hermes, Claude Code, Codex, etc.). Each agent contributes to a shared
memory corpus via a Python CLI (`hv`) that any agent can shell out to.

## Current State (P2P_DESIGN.md Phase 2 code COMPLETE — pending two-node Tailnet acceptance)
- `~/projects/hive-mind/hv` — Python CLI: remember, search, decide, entity, stats, rebuild, merkle, **sync now|daemon**
- `store.db` — SQLite (WAL + PRAGMAs, FTS5 search), DERIVED from the journal
- `journal/YYYY-MM-DD.jsonl` — append-only event log, the SOURCE OF TRUTH;
  entries carry node_id / per-node seq / type / timestamp / payload / prev_hash chain
- **Cross-row links use journal identity `(node_id, seq)`** (not local ids) via the
  `journal_index` table, so entity_fact / decision-supersedes survive cross-node merge
- `merkle.py` — global root + per-node chunk hashes (`node_chunk_hashes`) for delta sync
- `sync_daemon.py` / `sync_client.py` / `sync_common.py` — **stdlib http.server** P2P sync on :9876
  (NO FastAPI). Daemon is a SEPARATE process from Laravel (:8000). Reads `.peers.json` (gitignored;
  see `.peers.json.example`). `HIVE_NODE_ID` env overrides the node id.
- `migrate_journal.py` (Phase 1) + `migrate_journal_v2.py` (Phase 2 link refs) — both already run here
- `tests/` — offline pytest suite (10 passing incl. two-node convergence): `python3 -m pytest -q`
- `scripts/smoke.sh` (CLI) + `scripts/sync_smoke.sh` (two-node sync) — self-verifying
- `hive-dashboard/` — Laravel 13 app, UI-only (only `/`, `/dashboard`, `/api/sync/facts` are wired)
- `hermes_integration.py` — Hermes memory provider shells out to hv (unchanged; uses remember/search)
- Two Win11+WSL nodes: desktop-egmbl5a (100.95.128.118) and gregorius (100.114.200.119) on Tailscale
- GitHub: projectmentor/hive-mind (public)

**Handoff detail: see `docs/SESSION_NOTES.md` (Phase 1) and `docs/SESSION_NOTES_PHASE2.md`.**
**Remaining for Phase 2: M0 tailnet-bind verification + real two-node acceptance over Tailscale (needs gregorius/SSH).**

## READ THIS FIRST — The Architecture Design
**docs/P2P_DESIGN.md** is the full architectural design doc written
for the next phase of work. READ IT COMPLETELY before writing code.

It covers:
- **§1-6**: P2P sync architecture (Journal G-Set + Merkle + LWW)
- **§7**: Migration path from current MVP (Phases 1-4, ~28 hrs)
- **§8**: Performance optimization tiers (WAL, FTS5, connection pooling)
- **§11-16**: FUTURE design-level capabilities (modularity, hot-swap DB/sync,
  dashboard terminal). **DO NOT BUILD THESE YET.** They exist to shape
  current decisions, not to be implemented. They are Phases 5-11.

Key principles from the design:
1. "The Journal IS the database. SQLite is a cache/index."
2. G-Set CRDT for the journal (append-only union merge)
3. Merkle tree for efficient delta sync
4. LWW with node_id tiebreaker for mutable fields
5. Any single node's journal/ = full corpus that can be restored to a fresh machine
6. The journal is the contract between future modules (keep writes going
   through core's append API so modular extraction later is clean)

## Phase 1 Tasks (the current build)
See docs/P2P_DESIGN.md §7 for the full Phase 1 checklist (~6 hours):

1. **SQLite WAL mode + PRAGMAs** — add to `init_db()` in hv:
   ```sql
   PRAGMA journal_mode=WAL;
   PRAGMA synchronous=NORMAL;
   PRAGMA cache_size=-64000;
   PRAGMA mmap_size=268435456;
   ```

2. **Enhanced journal format** — add `node_id`, `seq` (per-node monotonic),
   `type`, `payload`, `prev_hash` to every entry. Use hostname for node_id.

3. **Merkle index generator** — new file `merkle.py` or module in hv.
   Hashes chunks of 100 journal entries. Used for sync Phase 2.

4. **FTS5 for search** — replace `LIKE '%...%'` with FTS5 virtual table.
   Create `facts_fts` in init_db, update `search()` function.

5. **Journal-first writes** — every write to SQLite should also go to
   the journal (it does now, but verify and strengthen).

## Node ID Convention
Use `socket.gethostname()` for node_id. Current nodes:
- desktop-egmbl5a
- gregorius

## Testing
The `hv` CLI is executable directly:
```bash
cd ~/projects/hive-mind
./hv remember "test fact" --tags test
./hv search "test"
./hv stats
./hv entity list
./hv decide "decision" --rationale "why"
```

Journal files live in `journal/` — you can inspect them directly.

## Constraints
- DO NOT remove SQLite in favor of Redis. SQLite is the derived index
  and we need JOINs, FTS5, FK constraints, ACID.
- DO NOT rewrite in Rust/Go. Python is the right choice (PyInstaller
  for packaging if needed later).
- Keep the Laravel dashboard working (`hive-dashboard/`).
- Tests should run without internet (no external APIs).
- Commit and push to git after each meaningful change.

## User Context
- David Faith, RE agent + building this as a personal infrastructure project
- Cost-conscious — that's why we're using Claude Code Max instead of Opus here
- Prefers working code over design discussions once the design doc exists
- Morning sessions, ~8:30 AM CT
- Two machines connected via Tailscale (~5ms RTT, direct peering)

## What to Do First
1. Read `docs/P2P_DESIGN.md` completely
2. Start with task 1 (WAL/PRAGMAs) — simplest, verifiable, no risk
3. Then task 4 (FTS5 search) — most user-facing impact
4. Then tasks 2, 3, 5 in order
5. After each task: run `./hv stats` and `./hv search "test"` to verify
6. Commit with clear messages
7. Push when a phase is coherent

## Anti-patterns (from design doc)
- No Raft/consensus (kills "one laptop survives")
- No sharding (50MB doesn't need it)
- No custom binary protocols (JSON/HTTP is fine on Tailscale)
- No encryption layer (Tailscale already WireGuard)
- No Docker (just a background process)
- No web UI for sync management (CLI + logs for 2-10 nodes)
