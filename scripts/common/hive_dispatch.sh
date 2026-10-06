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
# Usage (from settings.json):  hive_dispatch.sh {session-start|user-prompt|precompact|sessionend|notification|stop}
#
# Modules (2.1, M4): after the core behaviors for an event, each INSTALLED module's
# $HIVE_MODULES_DIR/<name>/hooks/<event> runs, in name order, with the payload on stdin. Those files are NOT
# under `hv verify` (they are outside the checkout); `hive-mind module add|update` verified them against the
# signed manifest and `hv doctor` re-verifies them, and this loop does not hash per event. It does run a hook
# only if the module is recorded in .modules.json and the file is a regular, executable, own-user,
# not group/world-writable file in a real directory. A hook's stdout and stderr are discarded (adding to the
# digest is M6); it is capped (HIVE_MODULE_HOOK_TIMEOUT, default 3s) and the event's modules share a budget
# (HIVE_MODULE_EVENT_BUDGET, default 6s); an overrun or a skipped hook is logged to $HIVE_HOME/.bus/modules.log.
# The 1s kill grace (`timeout -k 1`) is counted INSIDE the cap (TERM at cap-1, never below 1s) and the budget,
# and the budget is also clamped to
# the shim's own timeout (20s session events, else 10s) minus 1s, measured from the dispatcher's start, so the
# core behaviors (e.g. the nudge) are never cut off by Claude Code. A hook's detached children are killed with
# its process group when the hook exits.
#
# Bus (1.20): `hv` appends `introspect`-channel events to $HIVE_HOME/.bus/introspect.log (today:
# `idea-arrived <node_id:seq> <text>` when a PEER's idea lands on ingest). Non-journaled, local,
# best-effort. No consumer is wired here yet — session-start surfaces open ideas from store.db via
# `hv nudge --event session-start` regardless. A future consumer is a git change to this file.

event="${1:-}"
now_ms() { if [ -n "${EPOCHREALTIME:-}" ]; then local t="${EPOCHREALTIME/[.,]/}"; echo $(( t / 1000 )); else echo $(( SECONDS * 1000 )); fi; }
t0_ms="$(now_ms)"
SCRIPTS="${HIVE_HOME:-$HOME/projects/hive-mind}/scripts/common"
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

# ── module hooks (M4) ──────────────────────────────────────────────────────────────────────────────
# Everything below is best-effort: no path writes to stdout or changes the exit status.
case "$event" in session-start|user-prompt|precompact|sessionend|notification|stop) ;; *) exit 0 ;; esac

MODULES_DIR="${HIVE_MODULES_DIR:-$HOME/.hive/modules}"
HOOK_CAP="${HIVE_MODULE_HOOK_TIMEOUT:-3}"
EVENT_BUDGET="${HIVE_MODULE_EVENT_BUDGET:-6}"
case "$HOOK_CAP$EVENT_BUDGET" in *[!0-9]*|"") HOOK_CAP=3; EVENT_BUDGET=6 ;; esac

bus_log() {  # bus_log <what> <module> — a non-journaled local line; never shown
  { mkdir -p "${HIVE_HOME:-$HOME/projects/hive-mind}/.bus" \
      && printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$event" \
         >> "${HIVE_HOME:-$HOME/projects/hive-mind}/.bus/modules.log"; } 2>/dev/null || true
}

# a hook may run only if it is a regular file (not a link), executable, ours, and not writable by group or other
hook_ok() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ -x "$1" ] && [ -O "$1" ] || return 1
  local m; m="$(stat -c %A "$1" 2>/dev/null)" || return 1
  [ "${m:5:1}${m:8:1}" = "--" ]   # no group or other write bit
}

STATE="${HIVE_HOME:-$HOME/projects/hive-mind}/.modules.json"
if [ -d "$MODULES_DIR" ] && [ ! -L "$MODULES_DIR" ] && [ -f "$STATE" ]; then
  case "$event" in session-start|sessionend) shim=20 ;; *) shim=10 ;; esac
  end_ms=$(( $(now_ms) + EVENT_BUDGET * 1000 ))
  shim_end_ms=$(( t0_ms + shim * 1000 - 1000 ))
  [ "$end_ms" -le "$shim_end_ms" ] || end_ms=$shim_end_ms
  for dir in "$MODULES_DIR"/*/; do
    name="$(basename "$dir")"
    case "$name" in ""|.*|*[!a-z0-9-]*) continue ;; esac
    hook="$MODULES_DIR/$name/hooks/$event"
    [ -e "$hook" ] || [ -L "$hook" ] || continue
    [ ! -L "$MODULES_DIR/$name" ] && [ ! -L "$MODULES_DIR/$name/hooks" ] || { bus_log hook-skipped "$name"; continue; }
    grep -q "^ \"$name\": {" "$STATE" 2>/dev/null || { bus_log hook-skipped "$name"; continue; }
    hook_ok "$hook" || { bus_log hook-skipped "$name"; continue; }
    left=$(( (end_ms - $(now_ms)) / 1000 - 1 ))   # whole seconds, less the 1s kill grace
    [ "$left" -ge 1 ] || { bus_log hook-skipped "$name"; continue; }
    cap=$(( HOOK_CAP < left ? HOOK_CAP : left ))
    term=$(( cap > 1 ? cap - 1 : 1 ))   # TERM at cap-1, KILL 1s later; never 0, which would mean no limit
    printf '%s' "$payload" | timeout -k 1 "$term" "$hook" >/dev/null 2>&1 &
    tpid=$!
    wait "$tpid"
    rc=$?
    kill -KILL -- "-$tpid" 2>/dev/null   # timeout leads its own group: reap anything the hook detached
    { [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; } && bus_log hook-timeout "$name"
  done 2>/dev/null
fi
exit 0
