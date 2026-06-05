<!-- See SESSION_NOTES.md for Phase 1 notes -->

# Session Notes — Phase 2 (P2P sync)

<!-- truncated for brevity — full history in git log -->

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
- **`cfg[\"self\"]` in `.peers.json` is dead config** — sync identity is purely
  `hv.NODE_ID` (= `gethostname()`), and the diff is keyed by the `node_id`
  strings inside journal entries. This node's real id is **`DESKTOP-EGMBL5A`**
  (uppercase); `.peers.json` self was corrected to match (cosmetic only).

## M0 RESOLVED (2026-06-04) — Tailscale-on-host confirmed, fixed with portproxy
On EACH node, a Windows `netsh portproxy` forwards host `0.0.0.0:9876` → that
node's WSL eth0:9876 (desktop WSL 172.17.105.236; gregorius WSL 172.26.224.236),
plus a firewall rule for 9876. SSH between nodes also brought up (see corpus facts
15–18 for the OpenSSH DefaultShell / authorized_keys gotchas). `hv sync now`
converges; 30 entries on both. Mirrored networking was NOT used.

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

---

## Session 2026-06-05 (morning) — Tailscale SSH + new installer

### What changed
- **Win OpenSSH torn down on both nodes.** Portproxy rules, `C:\ssh-wsl.bat`,
  `administrators_authorized_keys`, and junk files in `C:\Users\Public\` removed.
  Standalone cleanup script: `scripts/windows/remove-portproxy.bat`.
- **New architecture:** Tailscale installed inside WSL on both nodes. Each WSL
  instance gets its own `100.x` tailnet IP (shows as a separate machine on the
  tailnet, e.g. `desktop-egmbl5a-1`). No portproxy, no mirrored networking, no
  Win OpenSSH. Sync daemon binds `0.0.0.0:9876`, reachable directly at the WSL
  Tailscale IP. SSH between nodes: `tailscale ssh david@<wsl-100.x>`.
- **New installer** (`scripts/installer/`) replaces `scripts/install/`:
  - Composer-style bootstrap: `curl -fsSL .../install.sh | bash && hive-mind install`
  - 12 steps, fully idempotent
  - Installs Tailscale in WSL; detects auth state; browser prompt only if needed
  - Init system detection: `systemctl` → `systemd PID1` → `init.d` → cron fallback
  - `loginctl enable-linger` baked in for user service persistence
  - Auto-detects and removes stale portproxy rules (stages elevated bat, waits for confirm)
  - Clear no-peers-yet messaging; peers can be added later via `.peers.json`
- **Repo set to private** (still in development)
- **README.md** added — install one-liner + multi-node setup guide front and centre

### Current node IPs (WSL Tailscale)
- DESKTOP-EGMBL5A: `100.123.162.114`
- gregorius: `100.84.84.100`

### Open issues
- Decision #9 / Fact #57: `peers.json` auto-update when peer Tailscale IP changes
  (assigned CC2 — on `/sync/hello` response, compare self-reported IP vs peers.json,
  update on mismatch + write journal entry)

### Verify
```bash
# From EGMBL5A WSL:
tailscale ssh david@100.84.84.100 "cd ~/projects/hive-mind && ./hv sync now && ./hv stats"
# Both nodes should show 76 entries, 57 facts, 9 decisions
```
