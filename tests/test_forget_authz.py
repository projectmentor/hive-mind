"""#122 step 1: the advisory `forget-authz` doctor check.

An owner forget positioned before the genesis owner is honoured without a signature (the grandfather in
`_content_evidence`), and ingest does not check timestamps, so an admitted device can erase a fact with a
backdated unsigned owner forget. `_forgets_grandfathered` lists such forgets in two groups: `hides` (it is
what keeps a fact forgotten now) and `dangling` (its target is not in the journal). It changes nothing in
the projection; it only makes the silent state visible.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owner_key, _device, _gov, _entry, _fact, _project, T0  # noqa: E402
from test_unforget import _act, _hive, PRE  # noqa: E402


def _check(hv, entries):
    gov = hv._governance_state(entries)
    return hv._forgets_grandfathered(entries, gov), hv._content_evidence(entries, gov)


def test_no_owner_means_nothing_to_report(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    f = _fact(hv, d, "a fact", "2026-01-02T00:00:00Z")
    (hides, dangling), _ = _check(hv, [f, _act(hv, d, f, PRE)])
    assert hides == [] and dangling == []


def test_a_backdated_unsigned_forget_is_reported_as_hiding_the_fact(tmp_path, monkeypatch):
    """The #122 attack: an admitted device appends an unsigned owner-source forget dated before genesis."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the backup runs at 02:00", "2026-01-02T00:00:00Z")
    forged = _act(hv, d1, f, PRE)
    (hides, dangling), ev = _check(hv, base + [f, forged])
    assert ev["the backup runs at 02:00"]["forget"] is True           # the projection honours it (the hole)
    assert hides == [f"forget {d1['id']}:{forged['seq']}→{f['node_id']}:{f['seq']}"] and dangling == []


def test_a_forget_whose_target_is_missing_is_dangling(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    ghost = {"node_id": "DESKTOP-OLD", "seq": 5}                      # a pre-device-identity ref, not in the journal
    (hides, dangling), _ = _check(hv, base + [_fact(hv, d0, "x", "2026-01-02T00:00:00Z"), _act(hv, d1, ghost, PRE)])
    assert hides == [] and dangling == [f"forget {d1['id']}:1→DESKTOP-OLD:5"]


def test_signed_forgets_and_lifted_or_superseded_grandfathered_forgets_are_not_reported(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "signed", "2026-01-02T00:00:00Z")
    g = _fact(hv, d0, "lifted", "2026-01-02T00:00:01Z")
    h = _fact(hv, d0, "then signed", "2026-01-02T00:00:02Z")
    entries = base + [f, g, h,
                      _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=a[:2]),              # properly authorised
                      _act(hv, d1, g, PRE),
                      _act(hv, d1, g, "2026-01-04T00:00:00Z", owner=a[:2], unforget=True),  # owner lifted it
                      _act(hv, d1, h, PRE),
                      _act(hv, d1, h, "2026-01-05T00:00:00Z", owner=a[:2])]              # owner forgot it too
    (hides, dangling), ev = _check(hv, entries)
    assert hides == [] and dangling == []
    assert [ev[c]["forget"] for c in ("signed", "lifted", "then signed")] == [True, False, True]


def test_every_reported_fact_is_forgotten_in_the_projection(tmp_path, monkeypatch):
    """The check and `_content_evidence` apply the same rules: each fact it names is forgotten there."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, d2), base = _hive(hv)
    fs = [_fact(hv, d0, f"fact {i}", f"2026-01-02T00:00:0{i}Z") for i in range(4)]
    entries = base + fs + [_act(hv, d1, fs[0], PRE), _act(hv, d2, fs[1], PRE),
                           _act(hv, d1, fs[2], "2026-01-03T00:00:00Z")]                  # unsigned, post-genesis: ignored
    (hides, _), ev = _check(hv, entries)
    by_label = {f"{f['node_id']}:{f['seq']}": f["payload"]["content"] for f in fs}
    named = {by_label[l.split("→")[1]] for l in hides}
    assert named == {"fact 0", "fact 1"}
    assert all(ev[c]["forget"] for c in named) and not ev["fact 2"]["forget"] and not ev["fact 3"]["forget"]


def test_doctor_reports_forget_authz(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the vpn uses wireguard", "2026-01-02T00:00:00Z")
    _project(hv, tmp_path, base + [f, _act(hv, d1, f, PRE)])
    env = dict(os.environ, HIVE_HOME=str(tmp_path))
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "doctor", "--format", "json"], env=env,
                       capture_output=True, text=True)
    checks = {c["name"]: c for c in json.loads(r.stdout)["checks"]}
    assert checks["forget-authz"]["status"] == "warn"
    assert "hv unforget" in checks["forget-authz"]["detail"] and "#122" in checks["forget-authz"]["detail"]
