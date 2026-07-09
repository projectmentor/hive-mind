#!/usr/bin/env bash
#
# sync_auth_smoke.sh — two-node convergence with sync read-auth ENFORCED (GHSA-242f-7fxg-f7wm).
#
# Unlike sync_smoke.sh (loopback binds → all traffic is trusted-local), this binds both nodes to a
# NON-loopback address, gives each a REAL device key (so node_id == its Ed25519 fingerprint), has the
# owner admit both, and runs the daemons in HIVE_SYNC_AUTH=enforce. Every cross-node request must
# therefore carry a valid Hive-Auth-* signature from an admitted device. Asserts the nodes still
# converge — proving the signed sync client (GET + POST) works end-to-end under enforcement.
#
# Skips cleanly when the host has no usable non-loopback address (e.g. a locked-down CI runner).
#
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HV="$PROJECT/hv"
cd "$PROJECT"

LAN="$(python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
except Exception:
    ip = ""
finally:
    s.close()
print("" if (not ip or ip.startswith("127.")) else ip)
PY
)"
if [ -z "$LAN" ]; then
  echo "sync_auth_smoke: no non-loopback address on this host — skipping (enforce path needs a remote source)."
  exit 0
fi

A=$(mktemp -d); B=$(mktemp -d)
PA=19886; PB=19887
DA=""; DB=""
cleanup() { [ -n "$DA" ] && kill "$DA" 2>/dev/null; [ -n "$DB" ] && kill "$DB" 2>/dev/null; rm -rf "$A" "$B"; }
trap cleanup EXIT

if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; B_=$'\033[1m'; N=$'\033[0m'; else G=""; R=""; B_=""; N=""; fi
pass=0; fail=0
ok() { pass=$((pass+1)); printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
no() { fail=$((fail+1)); printf '  %s✗ %s%s\n' "$R" "$1" "$N"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1 — got '$2' want '$3'"; fi; }
root() { HIVE_HOME="$1" "$HV" merkle | awk '/^Root:/{print $2}'; }
count() { HIVE_HOME="$1" python3 -c "import sqlite3,os;print(sqlite3.connect(os.path.join('$1','store.db')).execute('SELECT count(*) FROM $2').fetchone()[0])"; }

printf '%ssync-auth smoke (ENFORCE)%s  LAN=%s  A=:%s  B=:%s\n' "$B_" "$N" "$LAN" "$PA" "$PB"

# Real device identities (node_id == Ed25519 fingerprint — no HIVE_NODE_ID override).
export HIVE_OWNER_PASSPHRASE=smoke-pass
HIVE_HOME="$A" "$HV" key init >/dev/null
HIVE_HOME="$B" "$HV" key init >/dev/null
DEVA="$(cat "$A/.device-id")"; DEVB="$(cat "$B/.device-id")"

# A is the owner and admits BOTH device fingerprints.
HIVE_HOME="$A" "$HV" owner init >/dev/null
HIVE_HOME="$A" "$HV" group admit "$DEVA" --principal me >/dev/null
HIVE_HOME="$A" "$HV" group admit "$DEVB" --principal me >/dev/null

# Peer configs: bind the LAN IP (so peer traffic is non-loopback), point at each other.
cat > "$A/.peers.json" <<JSON
{"self":"nodeA","bind":"$LAN","port":$PA,"sync_auth":"enforce","peers":[{"id":"nodeB","url":"http://$LAN:$PB"}]}
JSON
cat > "$B/.peers.json" <<JSON
{"self":"nodeB","bind":"$LAN","port":$PB,"sync_auth":"enforce","peers":[{"id":"nodeA","url":"http://$LAN:$PA"}]}
JSON

# Seed distinct data.
HIVE_HOME="$A" "$HV" remember "alpha from A" --tags a >/dev/null
HIVE_HOME="$B" "$HV" remember "beta from B" --tags b >/dev/null

# Start both daemons under enforce.
HIVE_HOME="$A" HIVE_SYNC_AUTH=enforce python3 -c "import hive_sync_daemon as d; d.serve_forever()" >/dev/null 2>&1 & DA=$!
HIVE_HOME="$B" HIVE_SYNC_AUTH=enforce python3 -c "import hive_sync_daemon as d; d.serve_forever()" >/dev/null 2>&1 & DB=$!
curl -sf --retry 60 --retry-connrefused --retry-delay 0 "http://$LAN:$PA/sync/merkle-root" >/dev/null || { no "daemon A failed to start"; exit 1; }
curl -sf --retry 60 --retry-connrefused --retry-delay 0 "http://$LAN:$PB/sync/merkle-root" >/dev/null || { no "daemon B failed to start"; exit 1; }

# Sanity: an UNSIGNED remote read is refused under enforce (the fix is actually on).
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://$LAN:$PA/sync/chunk?node=$DEVA&start=1&end=9")
eq "unsigned remote /sync/chunk refused (enforce on)" "$CODE" "401"

printf '\n%s── signed two-way sync under enforce ──%s\n' "$B_" "$N"
# Round 1: A→B propagates A's governance to (pre-owner) B and A's fact; B learns it is admitted.
HIVE_HOME="$A" "$HV" sync now | sed 's/^/  /'
# Round 2: B (now owner-aware + admitted) pushes/pulls the rest; converge.
HIVE_HOME="$B" "$HV" sync now | sed 's/^/  /'
HIVE_HOME="$A" "$HV" sync now >/dev/null

eq "Merkle roots converge"  "$(root "$A")" "$(root "$B")"
eq "A has both facts"       "$(count "$A" facts)" "2"
eq "B has both facts"       "$(count "$B" facts)" "2"

printf '\n%s%d passed, %d failed%s\n' "$B_" "$pass" "$fail" "$N"
[ "$fail" -eq 0 ] || exit 1
