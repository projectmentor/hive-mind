#!/usr/bin/env bash
# Hive Mind session telemetry hook.
#
# Records Claude Code session start/end into the LOCAL telemetry lane via
# `hv telemetry record` — NOT into the facts corpus. Telemetry is observability
# (who/where/when + token usage + cost), not knowledge; it lives in its own
# local-only store (.telemetry/) and never enters the journal/merkle. Wired from
# the GLOBAL ~/.claude/settings.json so it fires for every Claude Code session.
#
# Contract: best-effort and NON-BLOCKING. Every path exits 0 and the hv call is
# capped with `timeout`, so it can never fail or slow a session.
#
# Usage (from settings.json):  session_hook.sh start  |  session_hook.sh end
# Reads the hook JSON on stdin (cwd + session_id).

event="${1:-start}"
HV="${HIVE_HOME:-$HOME/projects/hive-mind}/hv"

# If hv isn't present/executable here, silently do nothing.
[ -x "$HV" ] || exit 0

# Pull cwd + session id out of the hook's stdin JSON (best-effort; python3 is
# always present since hv itself is python). Two lines so a cwd with spaces is safe.
parsed=$(python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("cwd", ""))
    print(d.get("session_id") or d.get("sessionId") or "")
except Exception:
    print(); print()' 2>/dev/null)
cwd=$(printf '%s\n' "$parsed" | sed -n '1p')
sid=$(printf '%s\n' "$parsed" | sed -n '2p')
[ -n "$cwd" ] || cwd="$PWD"

node="$(hostname 2>/dev/null || echo node)"
user="$(id -un 2>/dev/null || echo "${USER:-user}")"

# Record into the telemetry lane. --identity keeps this instance distinct from
# other agents (and other machines) sharing the "claude-code" name; hv also
# captures node/os_user/ip and, on end, token usage + cost from the transcript.
timeout 12 "$HV" telemetry record \
    --event "$event" \
    --agent claude-code \
    --identity "${user}@${node}" \
    --session "$sid" \
    --cwd "$cwd" \
    >/dev/null 2>&1 || true

exit 0
