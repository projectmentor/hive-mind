"""1.19 PR4 — outcomes → `decisions.outcome_score` (design §4, §4.1; hive #53, plan v2).

An outcome is an ordinary fact plus an `outcome-of` link carrying a ternary polarity.
`_decision_evidence` (a clone of `_content_evidence` keyed by decision) scores it through the
unchanged `_content_confidence` into `decisions.outcome_score` — a VINDICATION axis. Decisions still
carry no confidence; `--min-confidence` still excludes them. Only sense-channel links count (absent
= sense); the LINK's channel decides. Both evidence projections retain an ordered evidence sequence.

Governance cases (cap_self, admission, owner transfer) reuse the in-memory entry builders from
test_links.py so the numbers are checked against the same fact-confidence machinery.
"""

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _decision, _link, _project, _entry, _device)  # noqa: E402


def _json(hive, q):
    return json.loads(hive.run("search", q, "--format", "json").stdout)


def _dec(hive, q):
    return [r for r in _json(hive, q) if r["kind"] == "decision"][0]


def _outcome(hv, dev, fact, decision, ts, polarity=1, channel=None, source="manual"):
    return _link(hv, dev, "outcome-of", fact, decision, ts, data={"polarity": polarity}, channel=channel, source=source)


# ── CLI surface ──────────────────────────────────────────────────────────────────────────────────

def test_outcome_of_ref_and_id_forms_emit_fact_plus_link(hive):
    hive.run("decide", "ship on friday", "--rationale", "r")
    d = _dec(hive, "ship on friday")
    r1 = hive.run("remember", "CI green after the friday ship", "--source", "alice", "--outcome-of", d["ref"])
    assert "↗ outcome (+1) of decision" in r1.stdout
    r2 = hive.run("remember", "customers were happy after the friday ship", "--source", "bob",
                  "--outcome-of", f"d{d['id']}", "--polarity", "1")
    r3 = hive.run("remember", "load stayed flat after the friday ship", "--source", "carol",
                  "--outcome-of", str(d["id"]))              # bare integer = decision id here
    assert r2.returncode == 0 and r3.returncode == 0
    links = [e for e in hive.entries() if e["type"] == "link"]
    assert len(links) == 3 and all(e["payload"]["kind"] == "outcome-of" for e in links)
    node, _, seq = d["ref"].rpartition(":")
    assert all(e["payload"]["to_ref"] == [node, int(seq)] for e in links)
    assert all(e["payload"]["data"]["polarity"] == 1 for e in links)
    facts = {e["seq"]: e for e in hive.entries() if e["type"] == "fact"}
    assert all(facts[e["payload"]["from_ref"][1]]["payload"]["content"].endswith("friday ship") for e in links)
    assert "channel" not in links[0]["payload"]                       # absent = sense


def test_bad_outcome_reference_aborts_with_nothing_journaled(hive):
    hive.run("decide", "ship on friday", "--rationale", "r")
    hive.run("remember", "a plain fact about deploys", "--source", "alice")
    before = len(hive.entries())
    fid = hive.query("SELECT id FROM facts")[0]["id"]
    fref = [r for r in _json(hive, "plain fact") if r["kind"] == "fact"][0]["ref"]
    for bad in ("99", "d99", "x7", fref):                              # unknown, unknown, malformed, a FACT ref
        r = hive.run("remember", "nope", "--outcome-of", bad, check=False)
        assert r.returncode != 0, bad
        assert "--outcome-of" in r.stderr
    assert len(hive.entries()) == before
    assert hive.query("SELECT count(*) c FROM facts WHERE content='nope'")[0]["c"] == 0
    # the CLI refuses a non-ternary polarity
    r = hive.run("remember", "nope", "--outcome-of", "1", "--polarity", "0.5", check=False)
    assert r.returncode != 0


def test_polarities_score_and_neutral_moves_timestamp_only(hive):
    hive.run("decide", "ship on friday", "--rationale", "r")
    d = _dec(hive, "ship on friday")
    assert d["last_outcome_at"] is None and d["outcome_score"] == 0.0 and d["effective_outcome_score"] is None
    hive.run("remember", "neutral observation after ship", "--source", "dave", "--outcome-of", d["ref"], "--polarity", "0")
    d0 = _dec(hive, "ship on friday")
    assert d0["last_outcome_at"] is not None and d0["outcome_score"] == 0.0     # observed, neutral ≠ no outcome
    hive.run("remember", "CI green after the friday ship", "--source", "alice", "--outcome-of", d["ref"])
    assert _dec(hive, "ship on friday")["outcome_score"] > 0
    hive.run("remember", "rollback needed after the friday ship", "--source", "bob", "--outcome-of", d["ref"], "--polarity", "-1")
    hive.run("remember", "second rollback after the friday ship", "--source", "erin", "--outcome-of", d["ref"], "--polarity", "-1")
    assert _dec(hive, "ship on friday")["outcome_score"] < 0
    # text shows the outcome; a negative outcome NEVER touches superseded_by
    assert "outcome -" in hive.run("search", "ship on friday").stdout
    assert hive.query("SELECT superseded_by FROM decisions")[0]["superseded_by"] is None
    # rebuild reproduces the same numbers (projection-written, single writer)
    before = hive.query("SELECT outcome_score, last_outcome_at FROM decisions")[0]
    hive.run("doctor", "rebuild")
    after = hive.query("SELECT outcome_score, last_outcome_at FROM decisions")[0]
    assert tuple(before) == tuple(after)


def test_introspect_channel_outcome_is_recorded_but_never_counted(hive):
    hive.run("decide", "ship on friday", "--rationale", "r")
    d = _dec(hive, "ship on friday")
    r = hive.run("remember", "I think friday went fine", "--source", "alice", "--outcome-of", d["ref"], "--channel", "introspect")
    assert "not counted" in r.stdout
    link = [e for e in hive.entries() if e["type"] == "link"][0]
    fact = [e for e in hive.entries() if e["type"] == "fact"][0]
    assert link["payload"]["channel"] == "introspect" and fact["payload"]["channel"] == "introspect"   # same channel on both
    d2 = _dec(hive, "ship on friday")
    assert d2["last_outcome_at"] is None and d2["outcome_score"] == 0.0
    assert hive.query("SELECT channel FROM links")[0]["channel"] == "introspect"                    # recorded


def test_min_confidence_still_excludes_decisions_and_pre_1_19_read_back_empty(hive):
    hive.run("decide", "ship on friday", "--rationale", "r")
    d = _dec(hive, "ship on friday")
    hive.run("remember", "CI green after the friday ship", "--source", "alice", "--outcome-of", d["ref"])
    rows = json.loads(hive.run("search", "friday", "--format", "json", "--min-confidence", "0.1").stdout)
    assert all(r["kind"] == "fact" for r in rows) or not any(r["kind"] == "decision" and r.get("confidence") for r in rows)
    # decisions carry no confidence key semantics: no `confidence` column ever
    assert "confidence" not in {r[1] for r in hive.query("PRAGMA table_info(decisions)")}
    hive.run("decide", "older decision with no outcomes", "--rationale", "r")
    o = _dec(hive, "older decision")
    assert o["outcome_score"] == 0.0 and o["last_outcome_at"] is None and o["effective_outcome_score"] is None


# ── governance numbers via the projection (in-memory entries, real rebuild) ─────────────────────

def test_single_principal_capped_two_identities_exceed(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    dec = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    f1 = _fact(hv, a, "ci green after ship", "2026-01-03T00:00:00Z")
    f2 = _fact(hv, a, "customers happy after ship", "2026-01-03T00:00:01Z", source="hermes:primary/other/s1")
    f3 = _fact(hv, b, "load flat after ship", "2026-01-03T00:00:02Z")
    # two agents on ONE device (principal p0) report +1 → capped by cap_self (0.70) via same-device discount
    same = [_outcome(hv, a, f1, dec, "2026-01-04T00:00:00Z"),
            _outcome(hv, a, f2, dec, "2026-01-04T00:00:01Z", source="hermes:primary/other/s1")]
    conn = _project(hv, tmp_path, base + [dec, f1, f2, f3] + same)
    s1 = conn.execute("SELECT outcome_score FROM decisions").fetchone()[0]
    assert 0 < s1 <= hv.CONF_CAP_SELF + 1e-9
    # a second admitted principal reporting +1 → exceeds the single-principal figure
    conn = _project(hv, tmp_path, base + [dec, f1, f2, f3] + same + [_outcome(hv, b, f3, dec, "2026-01-04T00:00:02Z")])
    s2 = conn.execute("SELECT outcome_score FROM decisions").fetchone()[0]
    assert s2 > s1
    # a non-admitted device's outcome is ignored (governed like corroboration)
    ghost = _device(hv)
    f4 = _fact(hv, ghost, "ghost says it went well", "2026-01-03T00:00:03Z")
    conn = _project(hv, tmp_path, base + [dec, f1, f2, f3, f4] + same + [_outcome(hv, ghost, f4, dec, "2026-01-04T00:00:03Z")])
    assert abs(conn.execute("SELECT outcome_score FROM decisions").fetchone()[0] - s1) < 1e-9


def test_float_polarity_in_schema_is_honoured_and_clamped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    dec = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    f1 = _fact(hv, b, "partial success after ship", "2026-01-03T00:00:00Z")
    half = _outcome(hv, b, f1, dec, "2026-01-04T00:00:00Z", polarity=0.5)       # a machine writer's scalar
    full = _outcome(hv, b, f1, dec, "2026-01-04T00:00:00Z", polarity=1)
    big = _outcome(hv, b, f1, dec, "2026-01-04T00:00:00Z", polarity=7)          # clamped to +1
    s_half = _project(hv, tmp_path, base + [dec, f1, half]).execute("SELECT outcome_score FROM decisions").fetchone()[0]
    s_full = _project(hv, tmp_path, base + [dec, f1, full]).execute("SELECT outcome_score FROM decisions").fetchone()[0]
    s_big = _project(hv, tmp_path, base + [dec, f1, big]).execute("SELECT outcome_score FROM decisions").fetchone()[0]
    assert 0 < s_half < s_full and abs(s_big - s_full) < 1e-9


def test_ordered_evidence_sequence_retained_in_both_projections(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    dec = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:01Z")
    f2 = _fact(hv, b, "the deploy succeeded at commit abc123", "2026-01-02T00:00:03Z")   # corroboration, later
    entries = base + [dec, f1, f2,
                      _link(hv, b, "contradicts", f2, f1, "2026-01-02T00:00:02Z"),           # out of journal order on purpose
                      _outcome(hv, b, f2, dec, "2026-01-05T00:00:00Z", polarity=-1),
                      _outcome(hv, a, f1, dec, "2026-01-04T00:00:00Z", polarity=1)]
    gov = hv._governance_state(entries)
    cev = hv._content_evidence(entries, gov)[f1["payload"]["content"]]
    assert [x[1] for x in cev["evidence"]] == [1, -1, 1]                                    # sorted by ts, signs kept
    assert cev["evidence"] == sorted(cev["evidence"])
    dev_ = hv._decision_evidence(entries, gov)[(dec["node_id"], dec["seq"])]
    assert [x[1] for x in dev_["evidence"]] == [1, -1] and dev_["evidence"] == sorted(dev_["evidence"])


def test_two_node_differential_outcome_columns(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path / "n1", monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    dec = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    f1 = _fact(hv, b, "ci green after ship", "2026-01-03T00:00:00Z")
    f2 = _fact(hv, c, "rollback after ship", "2026-01-03T00:00:01Z")
    f3 = _fact(hv, c, "reflecting on the ship", "2026-01-03T00:00:02Z")
    entries = base + [dec, f1, f2, f3,
                      _outcome(hv, b, f1, dec, "2026-01-04T00:00:00Z", polarity=1),
                      _outcome(hv, c, f2, dec, "2026-01-04T00:00:01Z", polarity=-1),
                      _outcome(hv, c, f3, dec, "2026-01-04T00:00:02Z", polarity=1, channel="introspect")]

    def snap(home, order):
        h = _loadhv(home, monkeypatch)
        conn = _project(h, home, list(order))
        return [tuple(r) for r in conn.execute("SELECT content, round(outcome_score, 6), last_outcome_at FROM decisions")]

    s1, s2 = snap(tmp_path / "n1", entries), snap(tmp_path / "n2", reversed(entries))
    assert s1 == s2 and s1[0][2] == "2026-01-04T00:00:01Z"      # introspect outcome did not move last_outcome_at
