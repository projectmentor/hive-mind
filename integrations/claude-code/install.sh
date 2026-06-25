#!/usr/bin/env bash
# Install the Claude Code <-> Hive Mind integration on THIS machine:
#   1. symlink the `hive-memory` SKILL into ~/.claude/skills/ (user-level, cross-project)
#   2. register the stable DISPATCH SHIM in ~/.claude/settings.json (one `hive_dispatch.sh <event>`
#      per lifecycle event) — what each event actually runs (telemetry, digest, save/audit nudge)
#      lives in source, so behaviors update by `git pull`, never by re-editing ~/.claude. The wiring
#      is delegated to `hv doctor wire-agent`, the single source of truth shared with the self-heal.
#
# Idempotent (migrates any older inline hooks to the shim). A RUNNING Claude Code session live-reloads
# the skill (no restart) — so after this you can tell the running CC: "start using the hive-memory
# skill" (or /hive-memory). Hook changes take effect next session.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"          # integrations/claude-code/ -> repo root
SKILL_SRC="$REPO/integrations/claude-code/hive-memory"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"   # honor Claude Code's own config-dir override

[ -f "$SKILL_SRC/SKILL.md" ] || { echo "ERROR: $SKILL_SRC/SKILL.md missing"; exit 1; }

echo "== symlink skills -> $CLAUDE_DIR/skills/{hive-memory,wire-up} =="
mkdir -p "$CLAUDE_DIR/skills"
ln -sfn "$SKILL_SRC" "$CLAUDE_DIR/skills/hive-memory"
ln -sfn "$REPO/integrations/claude-code/wire-up" "$CLAUDE_DIR/skills/wire-up"
ls -ld "$CLAUDE_DIR/skills/hive-memory" "$CLAUDE_DIR/skills/wire-up"

echo "== register telemetry + nudge/audit hooks (idempotent, additive) =="
# Delegate to hv's canonical hook spec — the SINGLE source of truth shared with `hv doctor`
# (the check + the 15-min `--fix` self-heal). Keeping the wiring in one place is why a node updated
# from before a hook existed now self-heals instead of silently drifting. `_wire_claude_hooks` honors
# CLAUDE_CONFIG_DIR, so point it at the dir we resolved above.
CLAUDE_CONFIG_DIR="$CLAUDE_DIR" python3 "$REPO/hv" wire claude

echo
echo "== done =="
echo "Skill installed user-level (cross-project). In a RUNNING Claude Code session you can now:"
echo "  • say:  start using the hive-memory skill      (or type /hive-memory)"
echo "  • it reads/writes the shared corpus via $REPO/hv  (always --source claude-code)"
echo "Telemetry + capture-and-audit nudge hooks active on the NEXT session start."
echo "Integration spec (any agent): $REPO/docs/AGENT_INTEGRATION.md — self-updates via 'hv version'."
echo "Note: newly-added hook EVENTS (UserPromptSubmit/PreCompact) may need one '/hooks' open to register."
