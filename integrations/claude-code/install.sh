#!/usr/bin/env bash
# Install the Claude Code <-> Hive Mind integration on THIS machine:
#   1. symlink the `hive-memory` SKILL into ~/.claude/skills/ (user-level, cross-project)
#   2. additively register the telemetry + capture-and-audit nudge hooks in ~/.claude/settings.json
#      (SessionStart/End telemetry; SessionStart digest+spec-self-update, UserPromptSubmit save-nudge,
#       PreCompact/SessionEnd audit-nudge — the reference adapter for docs/AGENT_INTEGRATION.md).
#
# Idempotent. A RUNNING Claude Code session live-reloads both (no restart needed) — so after
# this you can just tell the running CC: "start using the hive-memory skill" (or /hive-memory).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"          # integrations/claude-code/ -> repo root
SKILL_SRC="$REPO/integrations/claude-code/hive-memory"
CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"

[ -f "$SKILL_SRC/SKILL.md" ] || { echo "ERROR: $SKILL_SRC/SKILL.md missing"; exit 1; }

echo "== symlink skill -> $CLAUDE_DIR/skills/hive-memory =="
mkdir -p "$CLAUDE_DIR/skills"
ln -sfn "$SKILL_SRC" "$CLAUDE_DIR/skills/hive-memory"
ls -ld "$CLAUDE_DIR/skills/hive-memory"

echo "== register telemetry + nudge/audit hooks (idempotent, additive) =="
SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os
settings_path = os.environ["SETTINGS"]
# Literal-$HOME command form so re-runs (and the hand-wired equivalents) dedupe exactly.
TEL = "$HOME/projects/hive-mind/scripts/session_hook.sh"   # session telemetry
NUD = "$HOME/projects/hive-mind/scripts/nudge_hook.sh"      # capture-and-audit loop (reference adapter)
try:
    with open(settings_path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
hooks = cfg.setdefault("hooks", {})

def ensure(event, cmd, timeout=15):
    entries = hooks.setdefault(event, [])
    for grp in entries:
        for h in grp.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == cmd:
                return False
    entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
    return True

results = [
    ("SessionStart telemetry", ensure("SessionStart", f"{TEL} start")),
    ("SessionEnd telemetry",   ensure("SessionEnd", f"{TEL} end")),
    ("SessionStart nudge",     ensure("SessionStart", f"{NUD} session-start")),
    ("SessionEnd nudge",       ensure("SessionEnd", f"{NUD} sessionend", 10)),
    ("UserPromptSubmit nudge", ensure("UserPromptSubmit", f"{NUD} user-prompt", 10)),
    ("PreCompact nudge",       ensure("PreCompact", f"{NUD} precompact", 10)),
]
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
for name, added in results:
    print(f"  {name}: {'added' if added else 'already present'}")
PY

echo
echo "== done =="
echo "Skill installed user-level (cross-project). In a RUNNING Claude Code session you can now:"
echo "  • say:  start using the hive-memory skill      (or type /hive-memory)"
echo "  • it reads/writes the shared corpus via $REPO/hv  (always --source claude-code)"
echo "Telemetry + capture-and-audit nudge hooks active on the NEXT session start."
echo "Integration spec (any agent): $REPO/docs/AGENT_INTEGRATION.md — self-updates via 'hv version'."
echo "Note: newly-added hook EVENTS (UserPromptSubmit/PreCompact) may need one '/hooks' open to register."
