#!/usr/bin/env bash
#
# smoke.sh — end-to-end smoke test for the `hv` CLI.
#
# Runs the full lifecycle (remember/dedup, decide/supersede, entity add+link,
# FTS5 search, journal format + hash chain, WAL, Merkle, rebuild round-trip)
# against an ISOLATED temp HIVE_HOME, asserts each outcome, and exits non-zero
# if anything fails. Your real corpus is never touched.
#
# Usage:
#   scripts/smoke.sh        # run the smoke suite
#   scripts/smoke.sh -q     # quiet: only show failures + summary
#
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HV="$PROJECT/hv"

QUIET=0
[ "${1:-}" = "-q" ] && QUIET=1

# Isolated sandbox; cleaned up on exit.
SANDBOX="$(mktemp -d)"
export HIVE_HOME="$SANDBOX"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0
if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; B=$'\033[1m'; N=$'\033[0m'; else G=""; R=""; B=""; N=""; fi

ok()  { pass=$((pass+1)); [ "$QUIET" -eq 1 ] || printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
no()  { fail=$((fail+1)); printf '  %s✗ %s%s\n' "$R" "$1" "$N"; }
sect(){ [ "$QUIET" -eq 1 ] || printf '\n%s── %s%s\n' "$B" "$1" "$N"; }

# assert_contains <desc> <haystack> <needle>
assert_contains() { case "$2" in *"$3"*) ok "$1";; *) no "$1 — expected to contain: $3";; esac; }
# assert_eq <desc> <actual> <expected>
assert_eq()       { if [ "$2" = "$3" ]; then ok "$1"; else no "$1 — got '$2', want '$3'"; fi; }

# Run a small Python snippet with HIVE_HOME on PATH-of-data; prints its stdout.
py() { python3 - "$@"; }

printf '%sHive Mind CLI smoke%s  (sandbox: %s)\n' "$B" "$N" "$SANDBOX"

# ── remember + dedup ────────────────────────────────────────────────────────
sect "remember + dedup"
"$HV" remember "David prefers parallel delegation" --tags workflow,preference >/dev/null
DUP="$("$HV" remember "David prefers parallel delegation" --tags workflow,preference)"
assert_contains "dedup boosts trust instead of duplicating" "$DUP" "boosted"
"$HV" remember "TBHH is a real estate team at Dalton Wade" --tags business,real-estate >/dev/null
ROWS="$(py <<'PY'
import os,sqlite3
c=sqlite3.connect(os.path.join(os.environ["HIVE_HOME"],"store.db"))
print(c.execute("SELECT count(*) FROM facts WHERE content LIKE 'David prefers%'").fetchone()[0])
PY
)"
assert_eq "duplicate content collapses to one row" "$ROWS" "1"

# ── FTS5 search ─────────────────────────────────────────────────────────────
sect "FTS5 search"
assert_contains "single-term search hits"  "$("$HV" search parallel)"      "parallel delegation"
assert_contains "multi-term (implicit AND)" "$("$HV" search 'real estate')" "real estate"
"$HV" remember "uses C++ and (parens)" >/dev/null
if "$HV" search 'C++ (parens) & punctuation!' >/dev/null 2>&1; then
  ok "punctuation query does not crash"
else
  no "punctuation query crashed (FTS5 syntax not sanitized)"
fi

# ── decide + supersede ──────────────────────────────────────────────────────
sect "decide + supersede"
"$HV" decide "Use Sonnet for coding" --rationale cost >/dev/null
"$HV" decide "Use Opus only for architecture" --rationale quality --supersedes 1 >/dev/null
ACTIVE="$(py <<'PY'
import os,sqlite3
c=sqlite3.connect(os.path.join(os.environ["HIVE_HOME"],"store.db"))
print(c.execute("SELECT count(*) FROM decisions WHERE superseded_by IS NULL").fetchone()[0])
PY
)"
assert_eq "superseded decision is no longer active" "$ACTIVE" "1"

# ── entity add + link are journaled ─────────────────────────────────────────
sect "entity add + link (journal-first)"
"$HV" entity add --name David --type person --attr '{"role":"RE agent"}' >/dev/null
"$HV" entity link --name David --fact-id 1 --confidence 0.9 >/dev/null
TYPES="$(py <<'PY'
import os,json,glob
types=set()
for f in glob.glob(os.path.join(os.environ["HIVE_HOME"],"journal","*.jsonl")):
    for line in open(f):
        line=line.strip()
        if line: types.add(json.loads(line).get("type"))
print(",".join(sorted(types)))
PY
)"
assert_contains "entity add writes a journal entry"  "$TYPES" "entity"
assert_contains "entity link writes a journal entry" "$TYPES" "entity_fact"

# ── journal format + hash chain ─────────────────────────────────────────────
sect "journal format + hash chain"
CHAIN="$(py <<'PY'
import os,json,glob,hashlib
def canon(o): return json.dumps(o,sort_keys=True,separators=(",",":")).encode()
def h(e):    return "sha256:"+hashlib.sha256(canon(e)).hexdigest()
es=[]
for f in glob.glob(os.path.join(os.environ["HIVE_HOME"],"journal","*.jsonl")):
    for line in open(f):
        line=line.strip()
        if line: es.append(json.loads(line))
es.sort(key=lambda e:(e["node_id"],e["seq"]))
req={"node_id","seq","type","timestamp","payload","prev_hash"}
fields = all(req<=set(e) for e in es)
seqs   = [e["seq"] for e in es]==list(range(1,len(es)+1))
genesis= es and es[0]["prev_hash"]=="sha256:genesis"
linked = all(c["prev_hash"]==h(p) for p,c in zip(es,es[1:]))
print("ok" if (fields and seqs and genesis and linked) else f"FAIL fields={fields} seqs={seqs} genesis={genesis} linked={linked}")
PY
)"
assert_eq "entries have full format, monotonic seq, valid prev_hash chain" "$CHAIN" "ok"

# ── WAL ─────────────────────────────────────────────────────────────────────
sect "WAL + PRAGMAs"
MODE="$(py <<'PY'
import os,sqlite3
c=sqlite3.connect(os.path.join(os.environ["HIVE_HOME"],"store.db"))
print(c.execute("PRAGMA journal_mode").fetchone()[0].lower())
PY
)"
assert_eq "journal_mode is WAL" "$MODE" "wal"

# ── Merkle ──────────────────────────────────────────────────────────────────
sect "Merkle index"
MROOT="$("$HV" merkle | awk '/^Root:/{print $2}')"
case "$MROOT" in
  sha256:genesis) no "Merkle root is genesis (expected real hash over entries)";;
  sha256:?*)      ok "Merkle root computed over journal ($MROOT)";;
  *)              no "Merkle root missing/malformed: '$MROOT'";;
esac

# ── rebuild round-trip ──────────────────────────────────────────────────────
sect "rebuild round-trip (SQLite is derived)"
BEFORE="$("$HV" stats | grep -E 'Facts:|Decisions:|Entities:')"
rm -f "$SANDBOX"/store.db*            # nuke the derived index entirely
"$HV" rebuild >/dev/null
AFTER="$("$HV" stats | grep -E 'Facts:|Decisions:|Entities:')"
assert_eq "counts identical after rebuild from journal" "$AFTER" "$BEFORE"
assert_contains "FTS index survives rebuild" "$("$HV" search parallel)" "parallel delegation"

# ── summary ─────────────────────────────────────────────────────────────────
printf '\n%s%d passed, %d failed%s\n' "$B" "$pass" "$fail" "$N"
[ "$fail" -eq 0 ] || exit 1
