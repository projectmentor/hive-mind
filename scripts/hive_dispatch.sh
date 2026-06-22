#!/usr/bin/env bash
# Hive Mind hook dispatcher — the ONE stable shim registered in ~/.claude/settings.json.
#
# The foreign config (which we do not own) holds only "hive_dispatch.sh <event>", one per lifecycle
# event. WHICH behaviors run for each event is decided HERE, in source control — and so under
# `hv verify`'s signature. Adding, removing, or reordering a behavior is a git change, never a
# re-wire of ~/.claude. That is the whole point: old nodes can drift only on the (rarely-changing)
# set of event TYPES, never on behavior. `hv doctor` keeps the shims present; this file owns the rest.
#
# Contract: best-effort and NON-BLOCKING. Every path exits 0; each behavior is timeout-capped inside
# its own script. The hook's stdin is consumed ONCE and replayed to each behavior; behaviors' stdout
# passes through unchanged, so SessionStart / UserPromptSubmit context injection still works.
#
# Usage (from settings.json):  hive_dispatch.sh {session-start|user-prompt|precompact|sessionend}

event="${1:-}"
SCRIPTS="${HIVE_HOME:-$HOME/projects/hive-mind}/scripts"
payload="$(cat 2>/dev/null)"   # consume the hook's stdin once; each behavior gets its own copy

# run <script> [args...] — feed the captured payload on stdin, let stdout pass through to the model,
# never let a failure propagate (the session must not notice a broken behavior).
run() { [ -x "$SCRIPTS/$1" ] && printf '%s' "$payload" | "$SCRIPTS/$1" "${@:2}" || true; }

case "$event" in
  session-start) run session_hook.sh start; run nudge_hook.sh session-start ;;
  sessionend)    run session_hook.sh end;   run nudge_hook.sh sessionend ;;
  user-prompt)   run nudge_hook.sh user-prompt ;;
  precompact)    run nudge_hook.sh precompact ;;
esac
exit 0
