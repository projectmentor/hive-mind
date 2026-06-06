#!/usr/bin/env bash
# Hive Mind session telemetry hook.
#
# Records Claude Code session start/stop events into the shared hive-mind
# corpus via `hv remember`, dogfooding the product as institutional memory
# for multi-agent systems. Wired from the GLOBAL ~/.claude/settings.json so it
# fires for every Claude Code session in every project; `hv` resolves an
# absolute HIVE_HOME, so all events land in the one corpus regardless of cwd.
#
# Contract: best-effort and NON-BLOCKING. It must never fail a session start,
# so every path exits 0 and the hv write is capped with `timeout`.
#
# Usage (from settings.json):  session_hook.sh start   |   session_hook.sh end
# Reads the hook JSON on stdin (we use the `cwd` field).

event="${1:-start}"
HV="${HIVE_HOME:-$HOME/projects/hive-mind}/hv"

# If hv isn't present/executable here, silently do nothing.
[ -x "$HV" ] || exit 0

ts=$(date '+%Y-%m-%d %H:%M:%S %Z')

# Pull cwd out of the hook's stdin JSON (best-effort; python3 is always present
# since hv itself is python). Falls back gracefully if stdin is empty/malformed.
cwd=$(python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("cwd", ""))
except Exception:
    pass' 2>/dev/null)
[ -n "$cwd" ] || cwd="(unknown dir)"

# Timestamp in the content guarantees uniqueness so remember() does not collapse
# successive sessions into a single trust-boosted row.
timeout 10 "$HV" remember \
    "Claude Code session ${event}: ${ts} in ${cwd}" \
    --tags session,telemetry \
    --source claude-code \
    >/dev/null 2>&1 || true

exit 0
