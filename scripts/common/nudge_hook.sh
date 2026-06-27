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

# Cheap pre-filter for the per-turn path: most turns are neither a cadence checkpoint nor a
# phrase hit, so skip the hv (Python) spawn entirely. A quiet turn costs a grep, not a process.
# hv still owns the precise decision (MIN_GAP etc.) when we do spawn it.
if [ "$event" = "user-prompt" ]; then
  HM="${HIVE_HOME:-$HOME/projects/hive-mind}"
  ENVF="$HM/nudge.env"; [ -f "$ENVF" ] || ENVF="$HM/config/nudge.env.example"
  cfg() { grep -E "^$1=" "$ENVF" 2>/dev/null | tail -1 | cut -d= -f2- \
          | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//'; }
  save_every="$(cfg SAVE_EVERY)"; save_every="${save_every:-0}"
  on_phrase="$(cfg SAVE_ON_PHRASE)"; on_phrase="${on_phrase:-1}"
  spawn=0
  if [ "$save_every" != "0" ]; then
    spawn=1                                    # cadence on: hv must count every turn
  elif [ "$on_phrase" = "1" ]; then
    phrases="$(cfg HIVE_NUDGE_PHRASES)"
    if [ -z "$phrases" ]; then
      spawn=1                                  # no phrase list here: defer to hv's defaults
    else
      low="$(printf '%s' "$text" | tr '[:upper:]' '[:lower:]')"
      OLDIFS="$IFS"; IFS=','
      for p in $phrases; do
        p="$(printf '%s' "$p" | sed -E 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
        [ -n "$p" ] || continue
        case "$low" in *"$p"*) spawn=1; break;; esac
      done
      IFS="$OLDIFS"
    fi
  fi
  [ "$spawn" = "1" ] || exit 0
fi

printf '%s' "$text" | timeout 8 "$HV" nudge --event "$event" --session "$sid" --cwd "$cwd" --agent claude-code 2>/dev/null || true
exit 0
