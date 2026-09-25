"""#122 step 2 (contract 1.25): the governed `forget_writers` policy closes the pre-genesis grandfather.

`legacy` (the default) keeps today's rule: an owner-source forget positioned before the genesis owner is
honoured without a signature. `owner` honours only a forget signed by the owner as of its own position, so a
backdated unsigned forget from an admitted device erases nothing. The policy is a set-config governance act,
applied only when the owner current at that position signed it; the latest such act wins; an unknown value is
ignored (fail closed to the default). It stores no journal refs, so a future re-keying cannot orphan it (#130).
A legitimate legacy forget is kept by re-issuing it signed (`hv retract --owner`). `hv config set
forget_writers owner` refuses while closing would bring a fact back, and names each one to decide.
"""

import base64
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owner_key, _device, _gov, _entry, _fact, _project, T0  # noqa: E402
from test_unforget import _act, _hive, _transfer, _row, FORGET_FLOOR, PRE  # noqa: E402


def _policy(hv, owner, value, ts, seq):
    return _gov(hv, {"action": "set-config", "key": "forget_writers", "value": value}, owner[0], owner[1], ts, seq)


def test_legacy_by_default_owner_closes_the_grandfather(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the backup runs at 02:00", "2026-01-02T00:00:00Z")
    forged = _act(hv, d1, f, PRE)                                          # the #122 attack: backdated, unsigned
    assert _row(_project(hv, tmp_path, base + [f, forged]), "the backup runs at 02:00")[0] == FORGET_FLOOR
    closed = base + [f, forged, _policy(hv, a, "owner", "2026-01-05T00:00:00Z", 20)]
    assert _row(_project(hv, tmp_path, closed), "the backup runs at 02:00")[0] != FORGET_FLOOR


def test_a_forget_backdated_after_the_close_erases_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "port 443 is open", "2026-01-02T00:00:00Z")
    close = _policy(hv, a, "owner", "2026-01-03T00:00:00Z", 20)
    late = _act(hv, d1, f, PRE)                                            # appended after the close, dated before
    assert _row(_project(hv, tmp_path, base + [f, close, late]), "port 443 is open")[0] != FORGET_FLOOR


def test_signed_forgets_and_unforgets_are_unaffected(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "vendor X is approved", "2026-01-02T00:00:00Z")
    g = _fact(hv, d0, "vendor Y is approved", "2026-01-02T00:00:01Z")
    close = _policy(hv, a, "owner", "2026-01-03T00:00:00Z", 20)
    entries = base + [f, g, close,
                      _act(hv, d1, f, "2026-01-04T00:00:00Z", owner=a[:2]),                 # signed forget
                      _act(hv, d1, g, "2026-01-04T00:00:00Z", owner=a[:2]),
                      _act(hv, d1, g, "2026-01-05T00:00:00Z", owner=a[:2], unforget=True)]  # then lifted
    conn = _project(hv, tmp_path, entries)
    assert _row(conn, "vendor X is approved")[0] == FORGET_FLOOR
    assert _row(conn, "vendor Y is approved")[0] != FORGET_FLOOR


def test_only_the_owner_at_that_position_sets_the_policy(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    b = _owner_key(hv)
    stranger = _owner_key(hv)
    f = _fact(hv, d0, "the cache is safe", "2026-01-02T00:00:00Z")
    forged = _act(hv, d1, f, PRE)
    for i, bad in enumerate((_policy(hv, stranger, "owner", "2026-01-03T00:00:00Z", 20),     # not an owner
                             _policy(hv, b, "owner", "2026-01-03T00:00:00Z", 20))):           # B, before B's term
        entries = base + [f, forged, bad, _transfer(hv, a, b[1], "2026-01-04T00:00:00Z", 21)]
        assert _row(_project(hv, tmp_path, entries), "the cache is safe")[0] == FORGET_FLOOR, i
    # after the transfer, retired owner A's act is ignored and B's counts
    xfer = _transfer(hv, a, b[1], "2026-01-04T00:00:00Z", 21)
    late_a = _policy(hv, a, "owner", "2026-01-05T00:00:00Z", 22)
    assert _row(_project(hv, tmp_path, base + [f, forged, xfer, late_a]), "the cache is safe")[0] == FORGET_FLOOR
    ok_b = _policy(hv, b, "owner", "2026-01-05T00:00:00Z", 22)
    assert _row(_project(hv, tmp_path, base + [f, forged, xfer, ok_b]), "the cache is safe")[0] != FORGET_FLOOR


def test_latest_act_wins_and_an_unknown_value_is_ignored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "dns is on 10.0.0.2", "2026-01-02T00:00:00Z")
    forged = _act(hv, d1, f, PRE)
    close = _policy(hv, a, "owner", "2026-01-03T00:00:00Z", 20)
    reopen = _policy(hv, a, "legacy", "2026-01-04T00:00:00Z", 21)
    assert _row(_project(hv, tmp_path, base + [f, forged, close, reopen]), "dns is on 10.0.0.2")[0] == FORGET_FLOOR
    bogus = _policy(hv, a, "nobody", "2026-01-05T00:00:00Z", 22)          # fails closed: the owner setting stands
    assert _row(_project(hv, tmp_path, base + [f, forged, close, bogus]), "dns is on 10.0.0.2")[0] != FORGET_FLOOR
    assert hv._governance_state(base + [f, forged, bogus])["config"].get("forget_writers", "legacy") == "legacy"


def test_two_nodes_project_identically(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    fs = [_fact(hv, d0, f"fact {i}", f"2026-01-02T00:00:0{i}Z") for i in range(3)]
    entries = base + fs + [_act(hv, d1, fs[0], PRE), _act(hv, d1, fs[1], PRE),
                           _act(hv, d1, fs[1], "2026-01-03T00:00:00Z", owner=a[:2]),
                           _policy(hv, a, "owner", "2026-01-04T00:00:00Z", 20)]
    rows = []
    for home, order in ((tmp_path / "n1", entries), (tmp_path / "n2", list(reversed(entries)))):
        conn = _project(_loadhv(home, monkeypatch), home, order)
        rows.append([tuple(r) for r in conn.execute("SELECT content, confidence, last_evidence_at FROM facts ORDER BY content")])
    assert rows[0] == rows[1]
    assert [r[1] == FORGET_FLOOR for r in rows[0]] == [False, True, False]   # only the signed forget holds


def test_a_pre_1_25_node_ignores_the_policy(tmp_path, monkeypatch):
    """A 1.24 node lands the set-config (owner-signed) and ignores the unknown key: projection skew only."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the disk is 2TB", "2026-01-02T00:00:00Z")
    entries = base + [f, _act(hv, d1, f, PRE), _policy(hv, a, "owner", "2026-01-03T00:00:00Z", 20)]
    src = subprocess.run(["git", "-C", str(PROJECT), "show", "v1.24.0:hv"], capture_output=True, text=True)
    if src.returncode != 0:
        pytest.skip("v1.24.0 is not in this checkout (shallow clone)")
    old_hv = tmp_path / "hv_1_24"
    old_hv.write_text(src.stdout)
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("hv_1_24", str(old_hv))
    spec = importlib.util.spec_from_loader("hv_1_24", loader)
    old = importlib.util.module_from_spec(spec)
    loader.exec_module(old)
    assert old._content_evidence(entries, old._governance_state(entries))["the disk is 2TB"]["forget"] is True
    assert hv._content_evidence(entries, hv._governance_state(entries))["the disk is 2TB"]["forget"] is False
    (tmp_path / "journal").mkdir(exist_ok=True)
    accepted, _dups = old.append_foreign_entries(entries)
    assert accepted == len(entries)


# ── the CLI guard: closing never silently brings a fact back ──────────────────────────────────────

def _cli(tmp_path):
    def run(*args, check=True):
        env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, r.stderr
        return r
    return run


def _backdate_forget(tmp_path, run, text):
    """An owner hive with a fact, then an unsigned owner-source forget of it dated before genesis (#122's shape)."""
    out = run("remember", text).stdout
    sid = out.split()[2]
    ref = [x for x in json.loads(run("search", text.split()[1], "--format", "json").stdout)
           if x.get("kind") == "fact"][0]["ref"]
    node, seq = ref.rsplit(":", 1)
    f = sorted(glob.glob(f"{tmp_path}/journal/*.jsonl"))[0]
    with open(f, "a") as fh:
        fh.write(json.dumps({"node_id": "legacy-dev", "seq": 1, "type": "retract", "timestamp": "2020-01-01T00:00:00Z",
                             "payload": {"retracts_ref": [node, int(seq)], "reason": "", "source": "owner:owner/owner"}}) + "\n")
    run("doctor", "rebuild")
    return sid


def _journal(tmp_path):
    out = []
    for f in sorted(glob.glob(f"{tmp_path}/journal/*.jsonl")):
        out += [json.loads(l) for l in open(f).read().splitlines() if l.strip()]
    return out


def _policy_acts(tmp_path):
    return [e for e in _journal(tmp_path) if e["type"] == "governance"
            and e["payload"].get("action") == "set-config" and e["payload"].get("key") == "forget_writers"]


def test_config_set_refuses_while_a_fact_would_come_back_and_names_it(tmp_path):
    run = _cli(tmp_path)
    run("owner", "init")
    sid = _backdate_forget(tmp_path, run, "the backup runs at 02:00")
    r = run("config", "set", "forget_writers", "owner")
    assert "Not set" in r.stdout and sid in r.stdout and "hv retract" in r.stdout and "hv unforget" in r.stdout
    assert _policy_acts(tmp_path) == []                                    # nothing written
    doc = json.loads(run("doctor", "--format", "json", check=False).stdout)
    fa = {c["name"]: c for c in doc["checks"]}["forget-authz"]
    assert fa["status"] == "warn" and "forget_writers owner" in fa["detail"]


@pytest.mark.parametrize("decision", ["keep", "release"])
def test_after_the_owner_decides_each_fact_the_close_is_written(tmp_path, decision):
    run = _cli(tmp_path)
    run("owner", "init")
    sid = _backdate_forget(tmp_path, run, "the backup runs at 02:00")
    if decision == "keep":
        run("retract", sid, "--owner", "--reason", "keep it")
    else:
        run("unforget", sid, "--reason", "it was right")
    r = run("config", "set", "forget_writers", "owner")
    assert "set forget_writers = owner" in r.stdout
    acts = _policy_acts(tmp_path)
    assert len(acts) == 1 and acts[0]["payload"]["value"] == "owner" and "owner_sig" in acts[0]["payload"]
    import sqlite3
    conf = sqlite3.connect(tmp_path / "store.db").execute("SELECT confidence FROM facts").fetchone()[0]
    assert (conf == FORGET_FLOOR) == (decision == "keep")


def test_config_set_refuses_an_unknown_value_and_needs_no_decision_when_nothing_is_hidden(tmp_path):
    run = _cli(tmp_path)
    run("owner", "init")
    run("remember", "a fact nobody forgot")
    assert "must be 'legacy'" in run("config", "set", "forget_writers", "sometimes").stdout
    assert _policy_acts(tmp_path) == []
    assert "set forget_writers = owner" in run("config", "set", "forget_writers", "owner").stdout
