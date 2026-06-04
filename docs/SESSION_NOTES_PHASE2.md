# Session Notes — Phase 2 (P2P Sync Daemon)

**Status:** code complete + locally validated; **pending real two-node Tailnet acceptance.**
Commits: `4af8b71` (M1+M2), `49521b7` (M3–M5). Tests: pytest 10/10, smoke 13/13, sync_smoke 11/11.

Builds on Phase 1. Read alongside `CLAUDE.md` and `docs/P2P_DESIGN.md` §3–7.

## What shipped
- **Journal-identity references (M1).** Cross-row links no longer use local SQLite
  ids (which are reassigned every rebuild and differ per node). New `journal_index`
  table maps `(node_id, seq) → (kind, local_id)`. `entity_fact` stores
  `entity_ref`/`fact_ref`; `decide` stores `supersedes_ref` — all `(node_id, seq)`.
  `rebuild` is two-pass: pass 1 inserts rows + records the index, pass 2 resolves
  links/supersede. Resolvers keep back-compat with legacy local-id fields.
  `migrate_journal_v2.py` converted the live journal in place (recomputing the
  per-node prev_hash chain). This is the fix that makes sync correct — without it
  a merge silently mispoints links.
- **Sync primitives (M2).** `merkle.node_max_seq`, `node_chunk_hashes` (per-node
  100-seq windows matching `/sync/chunk`), `hash_entries`, `entries_in_range`.
  `hv.append_foreign_entries` (one in-memory `(node_id,seq)` dedup index, append
  only) replaces per-entry ingest; caller rebuilds once if `accepted>0`.
- **Daemon + client (M3–M5).** Stdlib `http.server` (no FastAPI) on **:9876**,
  separate process from Laravel (:8000). Four endpoints: `/sync/hello`,
  `/sync/merkle-root`, `/sync/chunk`, `/sync/ingest`. Client does merkle-root
  equality fast-path → per-node chunk-hash diff → PULL+rebuild → PUSH. `hv sync
  now` (one-shot) and `hv sync daemon` (serve + 5-min outbound loop). Reads
  `.peers.json`; `HIVE_NODE_ID` overrides the node id.

## Key decisions / sharp edges
- **Transport = stdlib, not FastAPI** (FastAPI/uvicorn aren't installed, no pip;
  4 trivial JSON endpoints on a WireGuard'd Tailnet).
- **`(node_id, seq)` is the dedup key**, never content. Same hostname on two
  instances collides — hence `HIVE_NODE_ID`. Real nodes (desktop-egmbl5a,
  gregorius) differ, so no collision there.
- **Rebuild after ingest**, once per session, not per entry (avoids FK/order
  issues; an entity_fact can arrive before its fact).
- Ingest is serialized by a lock in the daemon; concurrent CLI writes during a
  sync are still a theoretical race (low risk at this scale) — noted hardening.

## Remaining (needs gregorius / SSH — can't be done from one box)
1. **M0 — tailnet bind verification.** Tailscale isn't visible inside this WSL
   shell. Determine whether the daemon binds the node's `100.x` directly
   (Tailscale-in-WSL) or needs a Windows `netsh portproxy` (Tailscale-on-host).
   Probe: run the daemon here, `curl http://<this-node-100.x>:9876/sync/hello`
   from gregorius.
2. **Deploy to gregorius:** `git pull`, then run `migrate_journal_v2.py` on its
   own journal (one-time), `./hv rebuild`.
3. **Create `.peers.json`** on both nodes (see `.peers.json.example`).
4. **Real acceptance:** `hv sync daemon` on both; `hv sync now`; assert identical
   `hv merkle` roots; kill one node (other still serves); restart → re-converge.

## Verify (local)
```bash
cd ~/projects/hive-mind
python3 -m pytest -q        # 10 passed (incl. two-node convergence)
./scripts/smoke.sh          # 13/13
./scripts/sync_smoke.sh     # 11/11 — convergence, dedup, cross-node link resolution
```
