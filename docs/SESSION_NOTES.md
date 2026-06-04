# Session Notes — Phase 1 Foundation

**Date:** 2026-06-03 · **Commit:** `2cc1da0` · **Status:** Phase 1 complete, 9/9 tests passing

Handoff record for the build that took the MVP through Phase 1 of
`docs/P2P_DESIGN.md` §7. Read alongside `CLAUDE.md`.

## What shipped
- **WAL + PRAGMAs** via `get_conn()` (hv) — applied on every connection open.
- **Enhanced journal format + core append API.** `append_journal()` mints
  `node_id` / per-node monotonic `seq` / `type` / `timestamp` / `payload` /
  `prev_hash` (sha256 chain). All local writes route through it;
  `ingest_journal()` takes foreign entries verbatim with G-Set dedup by
  `(node_id, seq)`.
- **Journal-first gaps closed:** `entity add`, `entity link`, and `sync pull`
  now write the journal (previously SQLite-only).
- **FTS5 search** — external-content `facts_fts` + sync triggers, sanitized
  `MATCH`, `LIKE` fallback.
- **`merkle.py`** — `read_all_entries` / `chunk_hashes` / `merkle_root` in
  canonical `(node_id, seq)` order; exposed as `hv merkle`.
- **`hv rebuild`** — reconstruct SQLite entirely from the journal.
- **`migrate_journal.py`** — one-time fresh-start regeneration of the journal
  from the old 4-field format (already run on this machine).
- **`tests/`** — offline pytest suite driving the real CLI in a temp `HIVE_HOME`.

## Two non-obvious bugs fixed (don't reintroduce)
1. **FTS5 backfill never ran.** `SELECT count(*)` on an *external-content* FTS5
   table returns the **content table's** row count, so a `ftscount != fcount`
   guard is always false. Fix: backfill once, gated on table creation detected
   via `sqlite_master` (`_init_fts` in hv).
2. **`rebuild` raised "database disk image is malformed".** Bulk
   `DELETE FROM facts` fires the external-content FTS *delete* triggers row by
   row and desyncs the index. Fix: `rebuild` drops the FTS table+triggers,
   does the bulk delete + replay, then recreates and backfills the index.

## Migration outcome
- Facts went **9 → 8**: the old DB held a true duplicate row (same content
  stored twice); replay dedup correctly collapses it to one fact with trust
  preserved (1.10). `entity_facts` links were remapped across the collapse, so
  both links still point at the TBHH fact.
- Backups (gitignored, on this machine only):
  `journal.bak.pre-phase1/` (original old-format journal) and
  `store.db.bak.premigrate` (pre-rebuild SQLite).

## Verify
```bash
cd ~/projects/hive-mind
python3 -m pytest -q                 # 9 passed
./hv stats                           # 8 facts / 3 decisions / 2 entities / 2 links
./hv search "real estate"            # FTS hit
./hv merkle | head -3                # root hash
```

## Next: Phase 2 — FastAPI sync daemon (P2P_DESIGN.md §7)
Merkle delta detection + G-Set merge; `hv sync now` / `hv sync daemon`.

**Gotcha:** the Laravel app currently registers only `GET /`, `/dashboard`, and
`/api/sync/facts`. The sync endpoints `/api/sync/receive` and
`/api/sync/entries` are **not wired** (`routes/api.php` isn't loaded in
`bootstrap/app.php`), so `hv sync push/pull` have no live target yet — wiring
them (or building the standalone FastAPI daemon) is part of Phase 2. Do **not**
start Phase 5+ modularity before Phases 2–4.
