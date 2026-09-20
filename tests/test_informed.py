"""1.19 PR3 — `informed_by` on decisions (design §3; hive #52, plan v2).

`hv decide --informed <ref>…` records what a decision RELIED ON: an additive `informed_by` payload
(list of journal refs) plus one `informed` link per ref. The stable input form is `node_id:seq` (the
`ref` every `hv search` row now carries); bare local ids (`118`, `d17`, `i5`) are a kind-checked CLI
convenience. Every reference is resolved BEFORE anything is journaled — a bad one aborts the whole
write. These tests drive the real CLI through the shared `hive` fixture.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _json(hive, q):
    return json.loads(hive.run("search", q, "--format", "json").stdout)


def _seed(hive):
    hive.run("remember", "the deploy succeeded at commit abc123", "--source", "alice")
    hive.run("remember", "the deploy took eleven minutes", "--source", "alice")
    hive.run("decide", "base decision about deploys", "--rationale", "r")
    facts = {r["content"]: r for r in _json(hive, "deploy") if r["kind"] == "fact"}
    dec = [r for r in _json(hive, "base decision") if r["kind"] == "decision"][0]
    return facts, dec


def test_search_rows_carry_stable_ref(hive):
    facts, dec = _seed(hive)
    for r in list(facts.values()) + [dec]:
        assert r["ref"] and r["ref"].count(":") >= 1
        node, _, seq = r["ref"].rpartition(":")
        assert seq.isdigit() and node
    # the ref is the journal identity, not the rowid
    entries = {(e["node_id"], e["seq"]): e for e in hive.entries()}
    node, _, seq = dec["ref"].rpartition(":")
    assert entries[(node, int(seq))]["payload"]["content"] == "base decision about deploys"
    # text output shows it too
    assert dec["ref"] in hive.run("search", "base decision").stdout


def test_informed_by_ref_and_bare_id_round_trip(hive):
    facts, dec = _seed(hive)
    f1 = facts["the deploy succeeded at commit abc123"]
    f2 = facts["the deploy took eleven minutes"]
    r = hive.run("decide", "ship it", "--rationale", "because", "--tags", "t",
                 "--informed", f1["ref"], str(f2["id"]), f"d{dec['id']}", f1["ref"])   # dup is de-duplicated
    assert "informed by 3 ref(s)" in r.stdout
    # payload: informed_by is a list of [node_id, seq] refs, in order, de-duplicated
    d = [e for e in hive.entries() if e["type"] == "decision" and e["payload"]["content"] == "ship it"][0]
    refs = d["payload"]["informed_by"]
    assert [f"{n}:{s}" for n, s in refs] == [f1["ref"], f2["ref"], dec["ref"]]
    # exactly one `informed` link per ref, from this decision
    links = [e for e in hive.entries() if e["type"] == "link"]
    assert len(links) == 3 and all(e["payload"]["kind"] == "informed" for e in links)
    assert all(e["payload"]["from_ref"] == [d["node_id"], d["seq"]] for e in links)
    assert [f"{e['payload']['to_ref'][0]}:{e['payload']['to_ref'][1]}" for e in links] == [f1["ref"], f2["ref"], dec["ref"]]
    # projected: decisions.informed_by + links rows
    assert json.loads(hive.query("SELECT informed_by FROM decisions WHERE content='ship it'")[0]["informed_by"]) == refs
    rows = hive.query("SELECT kind, from_kind, to_kind FROM links ORDER BY to_kind")
    assert [tuple(x) for x in rows] == [("informed", "decision", "decision"), ("informed", "decision", "fact"), ("informed", "decision", "fact")]
    # search JSON: refs + this node's local ids; text: `informed by:` line
    out = [r for r in _json(hive, "ship it") if r["kind"] == "decision"][0]
    assert [x["ref"] for x in out["informed_by"]] == [f1["ref"], f2["ref"], dec["ref"]]
    assert [x["kind"] for x in out["informed_by"]] == ["fact", "fact", "decision"]
    assert all(x["id"] is not None for x in out["informed_by"])
    assert "informed by:" in hive.run("search", "ship it").stdout


def test_bad_reference_aborts_with_nothing_journaled(hive):
    facts, dec = _seed(hive)
    before = len(hive.entries())
    # unresolvable bare id
    r = hive.run("decide", "nope", "--informed", "99", check=False)
    assert r.returncode != 0 and "does not resolve" in r.stderr
    # kind mismatch: a FACT id given with the decision prefix (pick an id that is NOT also a decision id)
    fid = max(f["id"] for f in facts.values())
    assert hive.query("SELECT count(*) c FROM decisions WHERE id = ?", (fid,))[0]["c"] == 0
    r = hive.run("decide", "nope", "--informed", f"d{fid}", check=False)
    assert r.returncode != 0 and "does not resolve to a decision" in r.stderr
    # malformed token
    r = hive.run("decide", "nope", "--informed", "x7", check=False)
    assert r.returncode != 0 and "expected" in r.stderr
    # unresolvable ref (well-formed, unknown)
    r = hive.run("decide", "nope", "--informed", "k1:0000000000000000:7", check=False)
    assert r.returncode != 0 and "does not resolve" in r.stderr
    # one bad among good aborts the whole write
    r = hive.run("decide", "nope", "--informed", facts["the deploy took eleven minutes"]["ref"], "99", check=False)
    assert r.returncode != 0
    assert len(hive.entries()) == before                     # no decision, no links
    assert hive.query("SELECT count(*) c FROM decisions WHERE content='nope'")[0]["c"] == 0


def test_pre_1_19_decision_reads_back_empty_and_plain_decide_unchanged(hive):
    hive.run("decide", "plain", "--rationale", "r")
    d = [e for e in hive.entries() if e["type"] == "decision"][0]
    assert "informed_by" not in d["payload"]                  # additive: absent when unused
    assert [e for e in hive.entries() if e["type"] == "link"] == []
    out = [r for r in _json(hive, "plain") if r["kind"] == "decision"][0]
    assert out["informed_by"] == []
    hive.run("doctor", "rebuild")
    assert json.loads(hive.query("SELECT informed_by FROM decisions")[0]["informed_by"]) == []


def test_informed_combines_with_supersedes_and_survives_rebuild(hive):
    facts, dec = _seed(hive)
    f1 = facts["the deploy succeeded at commit abc123"]
    hive.run("decide", "v2 of base", "--rationale", "r", "--supersedes", str(dec["id"]), "--informed", f1["ref"])
    hive.run("doctor", "rebuild")
    row = hive.query("SELECT superseded_by FROM decisions WHERE content='base decision about deploys'")[0]
    assert row["superseded_by"] is not None
    assert hive.query("SELECT count(*) c FROM links WHERE kind='informed'")[0]["c"] == 1
    assert [e["type"] for e in hive.entries()].count("link") == 1     # --supersedes still legacy (no link), PR2b


def test_mcp_hive_decide_informed_by_matches_cli(hive, monkeypatch):
    """MCP parity: hive_decide(informed_by="ref,ref") forwards `--informed`; hive_search passes `ref` through.
    The source-level checks always run; the live call runs only where the `mcp` package is installed."""
    src = (PROJECT / "integrations" / "mcp" / "hive_mcp.py").read_text()
    assert 'def hive_decide(content: str, rationale: str = "", tags: str = "", informed_by: str = "")' in src
    assert '"--informed"' in src and "`ref`" in src
    import pytest
    pytest.importorskip("mcp")
    facts, dec = _seed(hive)
    f1 = facts["the deploy succeeded at commit abc123"]
    monkeypatch.setenv("HIVE_HOME", str(hive.home))
    sys.path.insert(0, str(PROJECT / "integrations" / "mcp"))
    import importlib
    mod = importlib.import_module("hive_mcp")
    importlib.reload(mod)
    out = mod.hive_decide("via mcp", rationale="r", tags="t", informed_by=f"{f1['ref']}, d{dec['id']}")
    assert "informed by 2 ref(s)" in out
    d = [e for e in hive.entries() if e["type"] == "decision" and e["payload"]["content"] == "via mcp"][0]
    assert [f"{n}:{s}" for n, s in d["payload"]["informed_by"]] == [f1["ref"], dec["ref"]]
    rows = mod.hive_search("via mcp")
    assert any(r.get("kind") == "decision" and r.get("ref") and len(r.get("informed_by", [])) == 2 for r in rows)
