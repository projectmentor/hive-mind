# Session Notes — Phase 2 (P2P Sync Daemon)

**Status: ACCEPTED 2026-06-04 — two-node sync confirmed over Tailscale** between
DESKTOP-EGMBL5A (100.95.128.118) and gregorius (100.114.200.119); 30 journal
entries converged on both. M0 resolved exactly as diagnosed (Tailscale-on-host →
`netsh portproxy` on each node). Infra state is documented IN the corpus (facts
13–22; `./hv search "portproxy"|"openssh"|"gregorius"`). Two hardening TODOs remain
(daemon persistence, install-script portability) — see end of file.
Commits: `4af8b71` (M1+M2), `49521b7` (M3–M5), `6b606ad` (install scripts).
Tests: pytest 10/10, smoke 13/13, sync_smoke 11/11.

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

## M0 findings (probed 2026-06-04 on desktop-egmbl5a)
- **Tailscale runs on the Windows HOST, not inside WSL.** No `tailscale` CLI and
  no `100.x` interface in the WSL shell; `powershell.exe tailscale status` lists
  both nodes. WSL is in **default NAT mode** (no `~/.wslconfig`; eth0 is
  `172.17.x` behind host gateway `172.17.96.1`). So gregorius hitting
  `100.95.128.118:9876` lands on the **host**, which does NOT forward into WSL by
  default. **This is the Tailscale-on-host case.**
- **Daemon side is healthy regardless of transport:** binds `0.0.0.0:9876`
  (all interfaces), `/sync/hello` + `/sync/merkle-root` serve correctly,
  `requests` 2.34.2 is installed so the client (`hv sync now`) won't crash.
- **`cfg["self"]` in `.peers.json` is dead config** — sync identity is purely
  `hv.NODE_ID` (= `gethostname()`), and the diff is keyed by the `node_id`
  strings inside journal entries. This node's real id is **`DESKTOP-EGMBL5A`**
  (uppercase); `.peers.json` self was corrected to match (cosmetic only).
- **M0 resolution still open — depends on the VPN approach in progress:**
  - *Tailscale-in-WSL* (installing tailscaled inside WSL on both nodes): daemon's
    `0.0.0.0` bind is reachable on the WSL `100.x` directly — nothing more needed.
  - *Tailscale-on-host (current)*: add a Windows `netsh interface portproxy`
    from host `:9876` → WSL eth0 `:9876`, OR switch WSL to **mirrored** networking
    (`~/.wslconfig` `[wsl2] networkingMode=mirrored` + `wsl --shutdown`) so WSL
    shares the host's `100.x`. Mirrored is cleaner but the shutdown kills live
    sessions. NAT portproxy is brittle (WSL eth0 IP changes across restarts).

## M0 RESOLVED (2026-06-04) — Tailscale-on-host confirmed, fixed with portproxy
On EACH node, a Windows `netsh portproxy` forwards host `0.0.0.0:9876` → that
node's WSL eth0:9876 (desktop WSL 172.17.105.236; gregorius WSL 172.26.224.236),
plus a firewall rule for 9876. SSH between nodes also brought up (see corpus facts
15–18 for the OpenSSH DefaultShell / authorized_keys gotchas). `hv sync now`
converges; 30 entries on both. Mirrored networking was NOT used.

## Hardening TODOs (post-acceptance; corpus facts #19, #21)
1. **Daemon startup persistence.** Daemon currently launched manually
   (`setsid python3 sync_daemon.py </dev/null >/tmp/hive.log 2>&1 & disown`); dies
   on WSL restart. **This WSL has systemd (PID 1) → install a systemd service.**
2. **`01-windows-setup.bat` hardcodes the `Admin` username** (line 20, DefaultShell
   path). Needs dynamic detection — AND must handle gregorius's case where
   `bash.exe` is a 0-byte stub (use the `C:\ssh-wsl.bat` → `wsl.exe bash %*` wrapper
   when bash.exe is missing/empty).
3. **(found this session) `02-wsl-setup.sh` writes the wrong authorized_keys path.**
   Step 4 writes `C:\Users\<user>\.ssh\authorized_keys`, but per fact #16 admin-group
   users read `C:\ProgramData\ssh\administrators_authorized_keys` (needs the
   `icacls` SYSTEM+Administrators / inheritance-removed perms). The manual setup had
   to override this; the script should target the right file.

## Also this session (non-acceptance)
- **Session telemetry hook (dogfooding).** Global `~/.claude/settings.json` now has
  `SessionStart`/`SessionEnd` hooks calling `scripts/session_hook.sh`, which records
  each Claude Code session into the corpus via `hv remember` (tags `session,telemetry`,
  source `claude-code`). Best-effort/non-blocking (exits 0, `timeout` guarded). hv's
  absolute `HIVE_HOME` means every project's sessions land in the one corpus.
  Caveat: lands as `type=fact` (shows in `hv search`); clean follow-up is a
  first-class `session` event type — deferred until after acceptance (touches core
  append/rebuild). `SessionEnd` is best-effort (abrupt WSL kill won't fire it).

## Verify (local)
```bash
cd ~/projects/hive-mind
python3 -m pytest -q        # 10 passed (incl. two-node convergence)
./scripts/smoke.sh          # 13/13
./scripts/sync_smoke.sh     # 11/11 — convergence, dedup, cross-node link resolution
```
