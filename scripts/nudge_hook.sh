#!/usr/bin/env bash
# Hive Mind nudge/audit hook — best-effort, NON-BLOCKING: must NEVER fail a session.
# Thin Claude Code adapter; all logic lives in `hv nudge`.
#
# Usage (from a settings.json hook): nudge_hook.sh {user-prompt|precompact|sessionend|session-start}
# Reads the hook's JSON on stdin, extracts session id / cwd / prompt, and forwards the
# prompt text to `hv nudge` on stdin. `hv nudge` prints a terse hint (injected into context) or nothing.

event="${1:-user-prompt}"
HV="${HIVE_HOME:-$HOME/projects/hive-mind}/hv"
[ -x "$HV" ] || exit 0

payload="$(cat)"   # consume stdin once; re-parse from the variable

parse() {  # parse <key1> [key2 ...] -> first present, non-empty value
  printf '%s' "$payload" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k in sys.argv[1:]:
        v = d.get(k)
        if v:
            print(v); break
except Exception:
    pass
' "$@" 2>/dev/null
}

sid="$(parse session_id sessionId)"
cwd="$(parse cwd)"
text="$(parse prompt user_prompt)"

printf '%s' "$text" | timeout 8 "$HV" nudge --event "$event" --session "$sid" --cwd "$cwd" 2>/dev/null || true
exit 0
