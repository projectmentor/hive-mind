#!/usr/bin/env bash
# Install the Claude Code <-> Hive Mind integration on THIS machine:
#   1. symlink the `hive-memory` SKILL into ~/.claude/skills/ (user-level, cross-project)
#   2. additively register the SessionStart/SessionEnd telemetry hook in ~/.claude/settings.json
#
# Idempotent. A RUNNING Claude Code session live-reloads both (no restart needed) — so after
# this you can just tell the running CC: "start using the hive-mind skill" (or /hive-memory).
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

echo "== register SessionStart/SessionEnd telemetry hook (idempotent, additive) =="
SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os
settings_path = os.environ["SETTINGS"]
# Use the same literal-$HOME command form as desktop so re-runs don't duplicate the hook.
base = "$HOME/projects/hive-mind/scripts/session_hook.sh"
try:
    with open(settings_path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
hooks = cfg.setdefault("hooks", {})

def ensure(event, arg):
    cmd = f"{base} {arg}"
    entries = hooks.setdefault(event, [])
    for grp in entries:
        for h in grp.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == cmd:
                return False
    entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 15}]})
    return True

a = ensure("SessionStart", "start")
b = ensure("SessionEnd", "end")
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"  SessionStart hook: {'added' if a else 'already present'}")
print(f"  SessionEnd hook:   {'added' if b else 'already present'}")
PY

echo
echo "== done =="
echo "Skill installed user-level (cross-project). In a RUNNING Claude Code session you can now:"
echo "  • say:  start using the hive-memory skill      (or type /hive-memory)"
echo "  • it reads/writes the shared corpus via $REPO/hv  (always --source claude-code)"
echo "Telemetry hook logs session start/end to the hive on the NEXT session start."
