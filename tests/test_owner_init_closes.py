"""#135 part (1) (contract 1.28): `hv owner init` leaves a new hive CLOSED.

Before there is an owner key a forget can only be TAGGED by source, never signed, so governance
grandfathered every owner-source forget positioned before the genesis owner — upgrading must not
resurrect a deliberately forgotten fact. 1.25 (#122) let an owner close that grandfather with
`hv config set forget_writers owner`, but `legacy` stayed the default, so every hive depended on its
owner remembering to run it. 1.28 makes genesis do it: at the end of `hv owner init`, every forget
that is in effect ONLY because it precedes genesis is re-issued as an ordinary owner-signed `retract`
(the shape `hv retract <fact> --owner` writes), and only THEN is `forget_writers=owner` set.

The order is the whole design. `_config_set`'s #122 guard refuses to close while a fact would come
back, so re-issuing first empties that list and the flip has nothing left to decide — and the flip
still goes through the guard. Every retract is prepared and SIGNED before any of them is appended, so
a signing failure writes nothing at all; an append that fails partway leaves what landed (an
append-only journal cannot be rolled back) and still does not set the config. The one state that must
be impossible is `forget_writers=owner` together with a fact silently back.

Dangling grandfathered forgets (target not a fact in this journal) are NOT re-issued: they hide
nothing, and minting owner authority over a target that does not exist would be a new claim.

The projection cases run `owner_cmd` in process against a temp HIVE_HOME, which is what lets the
failure paths be injected at the exact instruction; `hv doctor` and the #122 guard are driven through
the real CLI.
"""

import glob
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _fact, _project  # noqa: E402
from test_unforget import _act, _hive, _row, FORGET_FLOOR, PRE  # noqa: E402
from test_forget_writers import _backdate_forget, _cli, _legacy_hive, _policy_acts  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────

def _init(hv, force=False):
    hv.owner_cmd(SimpleNamespace(owner_action="init", force=force))


def _entries(hv):
    return hv.merkle.read_all_entries(hv.JOURNAL_DIR)


def _gov(hv):
    return hv._governance_state(_entries(hv))


def _policy(hv):
    return (_gov(hv).get("config") or {}).get("forget_writers", "legacy")


def _signed_forgets(hv):
    """Every owner-SIGNED `retract` in the journal, as (entry, payload), in journal order."""
    return [(e, e["payload"]) for e in _entries(hv)
            if e.get("type") == "retract" and "owner_sig" in (e.get("payload") or {})]


def _genesis_pos(hv):
    """The (timestamp, node_id, seq) position of the genesis act the projection resolved."""
    ref = (_gov(hv).get("genesis") or {})["ref"]
    node, seq = ref.rsplit(":", 1)
    e = next(x for x in _entries(hv) if str(x.get("node_id")) == node and x.get("seq") == int(seq))
    return (e["timestamp"], str(e["node_id"]), e["seq"])


def _pos(e):
    return (e["timestamp"], str(e["node_id"]), e["seq"])


def _conf(hv, content):
    conn = sqlite3.connect(hv.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT confidence FROM facts WHERE content = ?", (content,)).fetchone()
        return None if r is None else r["confidence"]
    finally:
        conn.close()


def _preowner(hv, forgotten=(), kept=(), ghosts=0):
    """The journal of a hive with NO owner: one fact per `forgotten`/`kept`, an UNSIGNED owner-source
    forget dated before genesis for each `forgotten` one (the only shape available with no owner key to
    sign with), and `ghosts` more such forgets whose target is not in the journal (grandfathered but
    dangling). Returns {content: [node_id, seq]}."""
    Path(hv.HIVE_HOME).mkdir(parents=True, exist_ok=True)
    (Path(hv.HIVE_HOME) / "journal").mkdir(parents=True, exist_ok=True)
    refs = {}
    for i, c in enumerate(list(forgotten) + list(kept)):
        e = hv.append_journal("fact", {"content": c, "tags": [], "importance": 0.5, "source": "manual"},
                              timestamp=f"2026-01-02T00:00:{i:02d}Z")
        refs[c] = [e["node_id"], e["seq"]]
    for c in forgotten:
        hv.append_journal("retract", {"retracts_ref": refs[c], "reason": "", "source": "owner:owner/owner"},
                          timestamp=PRE)
    for i in range(ghosts):
        hv.append_journal("retract", {"retracts_ref": ["DESKTOP-OLD", 5 + i], "reason": "",
                                      "source": "owner:owner/owner"}, timestamp=PRE)
    hv.init_db()
    hv.rebuild_db()
    return refs


def _doctor(home):
    env = dict(os.environ, HIVE_HOME=str(home))
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "doctor", "--format", "json"],
                       env=env, capture_output=True, text=True)
    return {c["name"]: c for c in json.loads(r.stdout)["checks"]}


def _journal_bytes(home):
    return {f: Path(f).read_bytes() for f in sorted(glob.glob(str(Path(home) / "journal" / "*.jsonl")))}


def _appended_since(home, before):
    """The entries appended to `home`'s journal since the `before` snapshot, asserting append-only."""
    added = []
    for f, blob in _journal_bytes(home).items():
        old = before.get(f, b"")
        assert blob.startswith(old), f"{f} was REWRITTEN, not appended to"
        added += [json.loads(l) for l in blob[len(old):].decode().splitlines() if l.strip()]
    return added


# ── the happy path ────────────────────────────────────────────────────────────────────────────────

def test_an_in_effect_grandfathered_forget_is_re_issued_and_the_hive_closes(tmp_path, monkeypatch, capsys):
    """The case #135 exists for: a fact forgotten before there was any key to sign the forget with."""
    hv = _loadhv(tmp_path, monkeypatch)
    refs = _preowner(hv, forgotten=["the backup runs at 02:00"], kept=["the vpn uses wireguard"])
    assert _conf(hv, "the backup runs at 02:00") == FORGET_FLOOR          # in effect, on the grandfather
    assert _policy(hv) == "legacy"

    _init(hv)
    out = capsys.readouterr().out

    assert _conf(hv, "the backup runs at 02:00") == FORGET_FLOOR          # still forgotten, on its own authority
    assert _conf(hv, "the vpn uses wireguard") != FORGET_FLOOR            # and nothing else moved
    assert _policy(hv) == "owner"
    gov = _gov(hv)
    assert hv._forgets_grandfathered(_entries(hv), gov)[0] == []          # no fact depends on the grandfather
    # exactly one re-issue: an owner-signed retract of that fact, positioned after genesis
    acts = _signed_forgets(hv)
    assert len(acts) == 1
    entry, payload = acts[0]
    assert payload["retracts_ref"] == refs["the backup runs at 02:00"]
    assert payload["source"] == "owner:owner/owner" and "unretracts_ref" not in payload
    assert hv._verify_governance(payload) == gov["owner_id"]
    assert _pos(entry) > _genesis_pos(hv)
    # and it says which fact it re-issued
    assert "re-issued 1 pre-genesis forget" in out
    assert hv._short_id(*refs["the backup runs at 02:00"]) in out
    # the old unsigned forget is untouched in the append-only journal, and now counts for nothing
    assert sum(1 for e in _entries(hv) if e.get("type") == "retract") == 2

    fa = _doctor(tmp_path)["forget-authz"]
    assert fa["status"] == "ok" and fa["detail"].startswith("closed (forget_writers=owner)")


def test_a_dangling_grandfathered_forget_closes_the_hive_and_is_not_re_issued(tmp_path, monkeypatch, capsys):
    """A forget whose target is not a fact here hides nothing; re-issuing it would mint owner authority
    over a target that does not exist."""
    hv = _loadhv(tmp_path, monkeypatch)
    _preowner(hv, kept=["a fact nobody forgot"], ghosts=1)
    n = len(_entries(hv))

    _init(hv)
    out = capsys.readouterr().out

    assert _policy(hv) == "owner"
    assert _signed_forgets(hv) == []
    assert len(_entries(hv)) == n + 2                                     # genesis + the set-config act only
    assert "re-issued" not in out
    _hides, dangling = hv._forgets_grandfathered(_entries(hv), _gov(hv))
    assert _hides == [] and len(dangling) == 1                            # still reported, still honoured by nobody
    assert "closed (forget_writers=owner): 1 unsigned" in _doctor(tmp_path)["forget-authz"]["detail"]


def test_an_empty_journal_closes_with_no_re_issue(tmp_path, monkeypatch, capsys):
    """The common new-hive case: one set-config act on top of genesis, nothing else."""
    hv = _loadhv(tmp_path, monkeypatch)
    Path(hv.HIVE_HOME).mkdir(parents=True, exist_ok=True)
    hv.init_db()

    _init(hv)
    out = capsys.readouterr().out

    assert _policy(hv) == "owner"
    assert _signed_forgets(hv) == []
    assert "re-issued" not in out
    kinds = [(e["type"], (e.get("payload") or {}).get("action")) for e in _entries(hv)]
    assert kinds == [("governance", "owner"), ("governance", "set-config")]


def test_owner_init_force_closes_a_legacy_owned_hive_too(tmp_path, monkeypatch, capsys):
    """A forced re-genesis is already a fork; NOT re-issuing there would silently reopen facts the
    previous grandfather was keeping forgotten."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the backup runs at 02:00", "2026-01-02T00:00:00Z")
    conn = _project(hv, tmp_path, base + [f, _act(hv, d1, f, PRE)])
    assert _row(conn, "the backup runs at 02:00")[0] == FORGET_FLOOR
    conn.close()
    assert _policy(hv) == "legacy"

    _init(hv, force=True)
    out = capsys.readouterr().out

    gov = _gov(hv)
    assert gov["owner_id"] != a[2]                                        # a new genesis owner took over
    assert _policy(hv) == "owner"
    assert _conf(hv, "the backup runs at 02:00") == FORGET_FLOOR          # and the fact did NOT come back
    acts = _signed_forgets(hv)
    assert len(acts) == 1
    assert acts[0][1]["retracts_ref"] == [f["node_id"], f["seq"]]
    assert hv._verify_governance(acts[0][1]) == gov["owner_id"]           # signed by the NEW owner
    assert _pos(acts[0][0]) > _genesis_pos(hv)
    assert "re-issued 1 pre-genesis forget" in out
    assert hv._forgets_grandfathered(_entries(hv), gov)[0] == []


# ── the failure paths ─────────────────────────────────────────────────────────────────────────────

def test_a_signing_failure_appends_nothing_and_leaves_the_hive_legacy(tmp_path, monkeypatch, capsys):
    """Everything is signed before anything is appended, so a preparation failure writes NOTHING: the
    journal keeps only the genesis act `owner init` had already made, and the config is not set."""
    hv = _loadhv(tmp_path, monkeypatch)
    refs = _preowner(hv, forgotten=["the backup runs at 02:00"])
    real = hv._sign_governance_payload

    def boom(payload, seed, pub):
        if "retracts_ref" in payload:                                     # the genesis act still signs
            raise RuntimeError("the owner key went away")
        return real(payload, seed, pub)

    monkeypatch.setattr(hv, "_sign_governance_payload", boom)
    before = _journal_bytes(tmp_path)

    _init(hv)
    out = capsys.readouterr().out

    assert _policy(hv) == "legacy"                                        # the config is NOT set
    assert _signed_forgets(hv) == []                                      # and NOTHING was appended
    assert [(e["type"], (e.get("payload") or {}).get("action"))
            for e in _appended_since(tmp_path, before)] == [("governance", "owner")]
    assert _conf(hv, "the backup runs at 02:00") == FORGET_FLOOR          # the grandfather still holds it
    sid = hv._short_id(*refs["the backup runs at 02:00"])
    assert "COULD NOT re-issue" in out and "NOTHING was written" in out and sid in out
    assert f"hv retract {sid} --owner" in out and f"hv unforget {sid}" in out


def test_an_append_that_fails_partway_never_sets_the_config(tmp_path, monkeypatch, capsys):
    """An append-only journal cannot be rolled back, so what landed stays — as ordinary post-genesis
    owner forgets, which resurrect nothing. What must NOT happen is the config being set."""
    hv = _loadhv(tmp_path, monkeypatch)
    refs = _preowner(hv, forgotten=["fact one", "fact two"])
    real = hv.append_journal
    retracts = []

    def flaky(entry_type, payload, timestamp=None):
        if entry_type == "retract":
            retracts.append(payload)
            if len(retracts) == 2:
                raise OSError("no space left on device")
        return real(entry_type, payload, timestamp=timestamp)

    monkeypatch.setattr(hv, "append_journal", flaky)

    _init(hv)
    out = capsys.readouterr().out

    assert _policy(hv) == "legacy"                                        # the config is NOT set
    assert len(_signed_forgets(hv)) == 1                                  # the first landed and stays
    assert not [e for e in _entries(hv) if e.get("type") == "governance"
                and (e["payload"] or {}).get("action") == "set-config"]
    assert _conf(hv, "fact one") == FORGET_FLOOR and _conf(hv, "fact two") == FORGET_FLOOR
    assert "PARTLY re-issued" in out and "re-issued owner-signed:" in out and "NOT re-issued:" in out
    for c in ("fact one", "fact two"):                                    # both lists are reported
        assert hv._short_id(*refs[c]) in out
    landed = "fact one" if retracts[0]["retracts_ref"] == refs["fact one"] else "fact two"
    left = "fact two" if landed == "fact one" else "fact one"
    assert f"re-issued owner-signed: {hv._short_id(*refs[landed])}" in out
    assert f"NOT re-issued: {hv._short_id(*refs[left])}" in out


# ── convergence, and the #122 guard is untouched ──────────────────────────────────────────────────

def test_a_second_node_derives_the_same_forgotten_set(tmp_path, monkeypatch):
    """The two-node differential: node 2 ingests node 1's journal in the opposite order and reaches the
    same facts table. The re-issued retracts are owner-signed post-genesis acts, so any peer that
    honours an owner-signed retract agrees. (A pre-1.25 peer ignores the `forget_writers` key and keeps
    its own default — harmless precisely because the re-issues do not depend on it.)"""
    hv = _loadhv(tmp_path / "n1", monkeypatch)
    _preowner(hv, forgotten=["the backup runs at 02:00"], kept=["the vpn uses wireguard"])
    _init(hv)
    entries = _entries(hv)
    rows = [[tuple(r) for r in sqlite3.connect(hv.DB_PATH).execute(
        "SELECT content, confidence FROM facts ORDER BY content")]]

    hv2 = _loadhv(tmp_path / "n2", monkeypatch)
    conn = _project(hv2, tmp_path / "n2", list(reversed(entries)))
    rows.append([tuple(r) for r in conn.execute("SELECT content, confidence FROM facts ORDER BY content")])
    conn.close()

    assert rows[0] == rows[1]
    assert [r[1] == FORGET_FLOOR for r in rows[0]] == [True, False]
    assert (hv2._governance_state(entries).get("config") or {}).get("forget_writers") == "owner"


def test_the_122_guard_still_refuses_where_a_fact_would_come_back(tmp_path):
    """Regression: 1.28 re-issues BEFORE flipping precisely so it never meets this guard. The guard
    itself is unchanged — on a hive put back to `legacy` with a grandfathered forget it still refuses
    and still names both remedies."""
    run = _cli(tmp_path)
    base = _legacy_hive(tmp_path, run)
    sid = _backdate_forget(tmp_path, run, "the backup runs at 02:00")
    r = run("config", "set", "forget_writers", "owner")
    assert "Not set" in r.stdout and sid in r.stdout
    assert f"hv retract {sid} --owner" in r.stdout and f"hv unforget {sid}" in r.stdout
    assert len(_policy_acts(tmp_path)) == base                            # nothing new written
