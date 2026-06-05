#!/usr/bin/env bash
#
# sync_smoke.sh — local two-node convergence test for the Phase 2 sync daemon.
#
# Spins up two isolated HIVE_HOMEs + two daemons on 127.0.0.1, seeds each with
# distinct data, runs `hv sync now`, and asserts the nodes converge (equal
# Merkle roots, equal counts), that re-sync is a no-op, and that a cross-node
# entity link resolves correctly on BOTH nodes (proves journal-identity refs).
#
# Loopback only — this is NOT a substitute for the real two-machine Tailnet
# acceptance test (see docs/SESSION_NOTES.md).
#
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HV="$PROJECT/hv"
cd "$PROJECT"

A=$(mktemp -d); B=$(mktemp -d)
PA=19876; PB=19877
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
conf() { HIVE_HOME="$1" python3 -c "import sqlite3,os,sys;print(sqlite3.connect(os.path.join(sys.argv[1],'store.db')).execute('SELECT confidence FROM facts WHERE content=?',(sys.argv[2],)).fetchone()[0])" "$1" "$2"; }

printf '%sSync smoke%s  (A=%s:%s  B=%s:%s)\n' "$B_" "$N" "$A" "$PA" "$B" "$PB"

cat > "$A/.peers.json" <<JSON
{"self":"nodeA","bind":"127.0.0.1","port":$PA,"peers":[{"id":"nodeB","url":"http://127.0.0.1:$PB"}]}
JSON
cat > "$B/.peers.json" <<JSON
{"self":"nodeB","bind":"127.0.0.1","port":$PB,"peers":[{"id":"nodeA","url":"http://127.0.0.1:$PA"}]}
JSON

# Each instance needs a distinct node id (both run on the same host, so the
# default hostname would collide their (node_id, seq) keys).
EA="HIVE_HOME=$A HIVE_NODE_ID=nodeA"
EB="HIVE_HOME=$B HIVE_NODE_ID=nodeB"

# Seed distinct data on each node.
env $EA "$HV" remember "alpha fact from A" --tags a >/dev/null
env $EA "$HV" decide "A decides X" >/dev/null
env $EB "$HV" remember "beta fact from B" --tags b >/dev/null
env $EB "$HV" entity add --name Bravo --type project >/dev/null
# A claim independently asserted on BOTH nodes with DISTINCT sources — after sync
# its derived confidence must be identical on both and reflect 2 distinct sources.
env $EA "$HV" remember "shared truth" --source obs-a >/dev/null
env $EB "$HV" remember "shared truth" --source obs-b >/dev/null
# Same structured agent, TWO sessions (D0): one identity → must stay 0.45 and converge.
env $EA "$HV" remember "agent self truth" --source hermes:primary/default/sess1aaa >/dev/null
env $EA "$HV" remember "agent self truth" --source hermes:primary/default/sess2bbb >/dev/null

# Start both daemons (serve-only).
env $EA python3 -c "import sync_daemon as d; d.serve_forever()" >/dev/null 2>&1 & DA=$!
env $EB python3 -c "import sync_daemon as d; d.serve_forever()" >/dev/null 2>&1 & DB=$!

# Wait for readiness via curl retry (no sleep).
curl -sf --retry 50 --retry-connrefused --retry-delay 0 "http://127.0.0.1:$PA/sync/merkle-root" >/dev/null || { no "daemon A failed to start"; exit 1; }
curl -sf --retry 50 --retry-connrefused --retry-delay 0 "http://127.0.0.1:$PB/sync/merkle-root" >/dev/null || { no "daemon B failed to start"; exit 1; }

printf '\n%s── one-shot sync from A ──%s\n' "$B_" "$N"
env $EA "$HV" sync now | sed 's/^/  /'

eq "Merkle roots converge"      "$(root "$A")" "$(root "$B")"
eq "A has all facts"            "$(count "$A" facts)" "4"
eq "B has all facts"            "$(count "$B" facts)" "4"
eq "A has the decision"         "$(count "$A" decisions)" "1"
eq "B has the decision"         "$(count "$B" decisions)" "1"
eq "A has the entity"           "$(count "$A" entities)" "1"
eq "B has the entity"           "$(count "$B" entities)" "1"
eq "confidence converges"       "$(conf "$A" "shared truth")" "$(conf "$B" "shared truth")"
eq "shared fact = 2-source conf" "$(conf "$A" "shared truth")" "0.675"
eq "same-agent 2-sessions = 1 identity" "$(conf "$A" "agent self truth")" "0.45"
eq "same-agent 2-sessions converges (B)" "$(conf "$B" "agent self truth")" "0.45"

printf '\n%s── re-sync is a no-op ──%s\n' "$B_" "$N"
OUT="$(env $EA "$HV" sync now)"
case "$OUT" in *"in sync"*) ok "second sync reports in-sync";; *) no "second sync not idempotent: $OUT";; esac

printf '\n%s── cross-node link resolves on both nodes ──%s\n' "$B_" "$N"
# On A: link B's entity 'Bravo' to A's 'alpha' fact, then sync back to B.
AFID="$(HIVE_HOME="$A" python3 -c "import sqlite3,os;print(sqlite3.connect(os.path.join('$A','store.db')).execute(\"SELECT id FROM facts WHERE content LIKE 'alpha%'\").fetchone()[0])")"
env $EA "$HV" entity link --name Bravo --fact-id "$AFID" >/dev/null
env $EA "$HV" sync now >/dev/null
eq "A shows the link"  "$(count "$A" entity_facts)" "1"
eq "B shows the link"  "$(count "$B" entity_facts)" "1"
# And the link points to the alpha fact on B (resolved to B's local ids).
BLINK="$(HIVE_HOME="$B" python3 -c "import sqlite3,os;c=sqlite3.connect(os.path.join('$B','store.db'));print(c.execute('SELECT f.content FROM entity_facts ef JOIN facts f ON f.id=ef.fact_id JOIN entities e ON e.id=ef.entity_id WHERE e.name=\"Bravo\"').fetchone()[0])")"
case "$BLINK" in alpha*) ok "B's link resolves to the alpha fact";; *) no "B's link mispoints: '$BLINK'";; esac

printf '\n%s%d passed, %d failed%s\n' "$B_" "$pass" "$fail" "$N"
[ "$fail" -eq 0 ] || exit 1
