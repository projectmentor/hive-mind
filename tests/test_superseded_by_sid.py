"""#157: a superseded decision names the entry that superseded it, by stable id, on every surface.

Additive fields beside the unchanged node-local `superseded_by` rowid: text `⚠ SUPERSEDED by h:… (hard)`,
JSON (`hv search`, `/api/item`, `/api/search`) `superseded_by_sid` / `superseded_by_ref` /
`superseded_by_authority`, the dashboard pill linking to the superseder, and `hv search --chain` walking the
hops to the live entry. A `supersedes` link recorded only as evidence is shown as NOT in effect."""
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_link_write_path import _as, _first_sid, _db  # noqa: E402


def _chain3(tmp_path):
    """d1 <- d2 <- d3 <- d4 (each superseded by the next, all by its author: hard). Returns [sid1..sid4]."""
    sids = [_first_sid(_as(tmp_path, "nodeA", "decide", "plan v1", "--rationale", "r").stdout)]
    for n in (2, 3, 4):
        sids.append(_first_sid(_as(tmp_path, "nodeA", "decide", f"plan v{n}", "--rationale", "r",
                                   "--supersedes", sids[-1]).stdout))
    return sids


def _rows(tmp_path, q):
    return {r["sid"]: r for r in json.loads(_as(tmp_path, "nodeA", "search", q, "--kind", "decision",
                                                "--format", "json").stdout)}


def test_text_and_json_name_the_superseding_entry(tmp_path):
    sids = _chain3(tmp_path)
    out = _as(tmp_path, "nodeA", "search", "plan", "--kind", "decision").stdout
    assert f"{sids[0]} " in out and f"⚠ SUPERSEDED by {sids[1]} (hard)" in out
    assert out.count("⚠ SUPERSEDED by") == 3                       # d4 is live
    rows = _rows(tmp_path, "plan")
    d1, d4 = rows[sids[0]], rows[sids[3]]
    assert d1["superseded_by_sid"] == sids[1] and d1["superseded_by_ref"] == rows[sids[1]]["ref"]
    assert d1["superseded_by_authority"] == "hard" and d1["supersede_evidence"] == []
    assert d1["superseded_by"] is not None                         # the node-local rowid stays
    assert d4["superseded_by_sid"] is None and d4["superseded_by"] is None


def test_same_after_a_rebuild(tmp_path):
    sids = _chain3(tmp_path)
    keep = lambda rs: {s: (r["superseded_by_sid"], r["superseded_by_ref"], r["superseded_by_authority"])
                       for s, r in rs.items()}
    before = keep(_rows(tmp_path, "plan"))
    _as(tmp_path, "nodeA", "doctor", "rebuild")
    after = keep(_rows(tmp_path, "plan"))
    assert before == after and after[sids[0]][0] == sids[1]


def test_lookup_and_api_item_carry_the_fields(tmp_path, monkeypatch):
    sids = _chain3(tmp_path)
    look = json.loads(_as(tmp_path, "nodeA", "search", "--id", sids[1], "--format", "json").stdout)
    assert look["item"]["superseded_by_sid"] == sids[2] and look["item"]["superseded_by_authority"] == "hard"
    assert f"SUPERSEDED by {sids[2]} (hard)" in _as(tmp_path, "nodeA", "search", "--id", sids[1]).stdout
    assert "current" in _as(tmp_path, "nodeA", "search", "--id", sids[3]).stdout
    import importlib.machinery, importlib.util
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    loader = importlib.machinery.SourceFileLoader("hv_157", str(PROJECT / "hv"))
    hv = importlib.util.module_from_spec(importlib.util.spec_from_loader("hv_157", loader))
    loader.exec_module(hv)
    item = hv.api_item(sids[0])["item"]
    assert item["superseded"] is True and item["superseded_by_sid"] == sids[1]
    assert item["superseded_by_ref"] == _rows(tmp_path, "plan")[sids[0]]["superseded_by_ref"]
    assert hv.api_item(sids[3])["item"]["superseded_by_sid"] is None
    api = {d["sid"]: d for d in hv.api_search("plan")["decisions"]}
    assert api[sids[2]]["superseded_by_sid"] == sids[3] and api[sids[3]]["supersede_evidence"] == []


def test_an_evidence_only_supersede_is_shown_as_not_in_effect(tmp_path):
    sid = _first_sid(_as(tmp_path, "nodeA", "decide", "deploy from laptops", "--rationale", "r").stdout)
    _as(tmp_path, "nodeB", "decide", "--revoke", sid, "--rationale", "no", "--source", "claude-code")
    row = _rows(tmp_path, "laptops")[sid]
    assert row["superseded_by"] is None and row["superseded_by_sid"] is None
    assert row["superseded_by_authority"] is None
    (ev,) = row["supersede_evidence"]
    assert ev["authority"] == "evidence" and ev["sid"].startswith("h:")
    out = _as(tmp_path, "nodeA", "search", "laptops", "--kind", "decision").stdout
    assert "⚠ SUPERSEDED" not in out.split("Revoke")[0]
    assert "evidence only, not in effect" in out and ev["sid"] in out
    chain = json.loads(_as(tmp_path, "nodeA", "search", "--chain", sid, "--format", "json").stdout)
    assert chain["live"] == sid and chain["chain"][0]["supersede_evidence"][0]["sid"] == ev["sid"]


def test_chain_follows_three_hops_to_the_live_entry(tmp_path):
    sids = _chain3(tmp_path)
    out = _as(tmp_path, "nodeA", "search", "--chain", sids[0]).stdout.splitlines()
    assert len(out) == 4
    for i in range(3):
        assert f"{sids[i]}  superseded by {sids[i + 1]} (hard)" in out[i]
    assert sids[3] in out[3] and "LIVE" in out[3]
    j = json.loads(_as(tmp_path, "nodeA", "search", "--chain", sids[0], "--format", "json").stdout)
    assert [h["sid"] for h in j["chain"]] == sids and j["live"] == sids[3]
    mid = json.loads(_as(tmp_path, "nodeA", "search", "--chain", sids[2], "--format", "json").stdout)
    assert [h["sid"] for h in mid["chain"]] == sids[2:]


@pytest.mark.parametrize("ident", ["h:0000000000", "garbage"])
def test_chain_on_an_id_that_names_no_decision_exits_1(tmp_path, ident):
    _chain3(tmp_path)
    assert _as(tmp_path, "nodeA", "search", "--chain", ident, check=False).returncode == 1


def test_chain_refuses_a_fact(tmp_path):
    fsid = _first_sid(_as(tmp_path, "nodeA", "remember", "a plain fact about things").stdout)
    r = _as(tmp_path, "nodeA", "search", "--chain", fsid, check=False)
    assert r.returncode == 1 and "not a decision" in r.stderr


def test_dashboard_pill_links_to_the_superseding_entry():
    html = (PROJECT / "dashboard" / "index.html").read_text()
    assert 'href="#${esc(d.superseded_by_sid)}"' in html and "SUPERSEDED by ${esc(d.superseded_by_sid)}" in html
    assert "supPill(d)" in html and "supPill(x)" in html and "supEvidence(" in html
    assert "evidence only, not in effect" in html
