"""#207 — the read side of #196/#205. `hv doctor`, `hv audit` and the pure functions behind them read payloads
that an admitted device signed and the journal keeps for good, so a malformed ref or content must never raise
there: a diagnostic that fails when a bad entry is present fails exactly when it is needed. One shared fixture
puts every malformed shape in one journal (written past ingest, as an older or hostile peer's would be)."""

import pytest

from test_links import _loadhv, _entry, _fact, _link, _project, _owned_hive

_BAD = [[["x"], 1], [[1], [2]], {"a": 1}, ["x"], "xy", [True, 1], ["x", 2**64], [{"a": 1}, 1]]
_T = "2026-01-05T00:00:00Z"


def _hostile_journal(hv):
    owner, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, b, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    bad = []
    for ref in _BAD:
        bad.append(_entry(hv, b, "retract", {"retracts_ref": ref}, _T))
        bad.append(_entry(hv, b, "retract", {"retracts_ref": ref, "source": "owner"}, _T))
        bad.append(_entry(hv, b, "retract", {"unretracts_ref": ref, "source": "owner"}, _T))
        for kind in ("supersedes", "resolves", "contradicts", "supports"):
            bad.append(_entry(hv, b, "link", {"kind": kind, "from_ref": ref, "to_ref": ref, "data": {},
                                              "source": "manual"}, _T))
            bad.append(_entry(hv, b, "link", {"kind": kind, "from_ref": [f1["node_id"], f1["seq"]], "to_ref": ref,
                                              "data": {}, "source": "manual"}, _T))
        bad.append(_entry(hv, b, "fact", {"content": "c", "source": "manual", "tags": [], "resolves_ref": ref}, _T))
        bad.append(_entry(hv, b, "decision", {"content": "d", "rationale": "r", "source": "manual", "tags": [],
                                              "supersedes_ref": ref}, _T))
    for content in ([1], {"a": 1}, 7):
        bad.append(_entry(hv, b, "fact", {"content": content, "source": "manual", "tags": []}, _T))
    return base + [f1, f2] + bad


def test_pure_functions_survive_malformed_refs(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    entries = _hostile_journal(hv)
    _project(hv, tmp_path, entries)
    gov = hv._governance_state(entries)
    assert hv._links_unauthorized(entries, gov) is not None
    assert hv._forgets_grandfathered(entries, gov) is not None
    assert hv._signer_reliability(entries, gov, 365, "2026-01-10T00:00:00Z") is not None


def test_doctor_and_audit_survive_malformed_refs(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _project(hv, tmp_path, _hostile_journal(hv)).close()
    assert isinstance(hv._doctor_status(), list)
    for depth in ("light", "deep"):
        assert hv._compute_audit(depth=depth) is not None


@pytest.mark.parametrize("ref", _BAD)
def test_a_malformed_to_ref_does_not_hide_a_well_formed_link(tmp_path, monkeypatch, ref):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, b, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    bad = _entry(hv, b, "link", {"kind": "supersedes", "from_ref": [f2["node_id"], f2["seq"]], "to_ref": ref,
                                 "data": {}, "source": "manual"}, _T)
    good = _link(hv, b, "supersedes", f2, f1, _T)
    entries = base + [f1, f2, bad, good]
    gov = hv._governance_state(entries)
    out = hv._links_unauthorized(entries, gov)
    assert len(out) == 1 and "supersedes" in out[0]
