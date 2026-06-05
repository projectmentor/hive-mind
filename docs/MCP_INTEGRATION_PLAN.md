# Hive Mind MCP server — Claude AI on-ramp (local stdio, both nodes)

> **Status: BUILT — pending Claude Desktop verification.** Server + installer live under
> `integrations/mcp/` (branch `feat/mcp-server`). All WSL-side checks pass (stdio handshake,
> login-shell bridge command, installer dep-warm/smoke/Windows-user-detect/config-merge).
> Remaining: the outer `wsl.exe` hop from Claude Desktop on Windows — restart Desktop, confirm
> `hive-memory` under Settings → Developer, run `hive_stats`.
> Companion to the CC integration (`integrations/claude-code/`, shipped) and `hermes_plugin`.

## Context / goal
David currently relays by hand between **Claude AI** and **Claude Code**. CC writes to the hive via a
skill; Hermes via its plugin. The missing on-ramp is Claude AI, which can't shell out to `hv`. Fix: an
**MCP server** exposing hive tools so Claude AI reads/writes the shared corpus directly — closing the
CC ↔ hive ↔ Claude-AI loop and removing the human relay.

Owner decisions: **Claude Desktop + LOCAL stdio MCP** (no public endpoint, no auth — keeps the
external-poisoning surface closed, fits Tailnet-only), installed on **BOTH nodes** (each Desktop talks to
its node's local corpus, which syncs P2P).

## Architecture (no network exposure)
Claude Desktop (Windows app) —stdio→ `wsl.exe` —→ **FastMCP stdio server in WSL** —subprocess→
`~/projects/hive-mind/hv` —→ local corpus → syncs via the existing `hive-sync` daemon. stdio only; same
trust boundary as the machine.

## The server — `integrations/mcp/hive_mcp.py` (Python FastMCP, stdio)
Thin wrapper that shells out to `hv` (decoupled, like `hermes_plugin` — `hv` is the contract). Tools
(docstrings carry the tension-holding + disciplined-write contract):
- `hive_search(query, min_confidence=0.0)` → `hv search <q> --format json`; return facts WITH confidence +
  provenance; surface conflict, don't rank-pick.
- `hive_remember(content, tags="", epistemic_status="observation")` → `hv remember … --source claude-ai`;
  decisions/outcomes/corrections only; search first (novelty); never set a confidence number.
- `hive_decide(content, rationale="")` → `hv decide …`.
- `hive_stats()` → `hv stats`.  (Optional `hive_sync()`.)
Server-level FastMCP `instructions` = the same contract as the CC skill / hermes_plugin. Identity:
`--source claude-ai` (v0 flat; D0 upgrades to structured agent identity). Three distinct sources now →
genuine corroboration when they independently agree.

## Dependency / isolation
Uses the official `mcp` SDK (FastMCP). Isolated via **`uv`** (`~/.local/bin/uv`): launch with
`uv run --with mcp python …` so the dep is ephemeral/cached and the stdlib core (`hv`/`sync_daemon`) is
untouched. (`mcp` SDK is NOT currently installed; uv warms it on first run.)

## Files
- NEW `integrations/mcp/hive_mcp.py` — FastMCP stdio server.
- NEW `integrations/mcp/install.sh` — WSL-side: warm the dep; detect the Windows user via `/mnt/c/Users`
  (Admin on desktop, david on gregorius); print/merge the `claude_desktop_config.json` entry + Windows path.
- Desktop config entry (Windows `%APPDATA%\Claude\claude_desktop_config.json`):
  ```json
  {"mcpServers": {"hive-memory": {
     "command": "wsl.exe",
     "args": ["-e","bash","-lc","uv run --with mcp python ~/projects/hive-mind/integrations/mcp/hive_mcp.py"]
  }}}
  ```

## Rollout / verify
1. Build server + install.sh on desktop; commit; push.
2. Each node: `git -C ~/projects/hive-mind pull`; run `integrations/mcp/install.sh`; add the snippet to
   `claude_desktop_config.json`; **restart Claude Desktop** (loads MCP on restart — no live reload).
3. In Desktop: ask it to search/record → calls the MCP tools → write lands with `source_agent=claude-ai`,
   syncs (roots match). Cross-agent corroboration only when claude-ai/claude-code/hermes genuinely agree.

## Risks
- **Windows-Desktop → WSL stdio bridge** (`wsl.exe` + `bash -lc` for `uv` on PATH) is the main risk — test
  the launcher manually first; fallback = a pre-made uv venv + absolute python path.
- Desktop needs a restart to load (not "just tell him").
- No exposure / no auth (local stdio). If claude.ai **web** is ever needed, the same FastMCP server adds a
  Streamable-HTTP transport behind Tailscale **Funnel** + bearer token (future option, not now).
