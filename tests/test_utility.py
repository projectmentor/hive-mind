"""1.19 PR6 — importance (salience L3) + utility projections, per-class half-life knobs (hive #55).

`facts.importance` is LEARNED: prior = min(asserted --importance, importance_self_cap), raised only by
link in-degree from OTHER principals (governed like confidence: admission, same-device discount).
`utility` = squash(governed Σ signer-weight × (0.25 + 0.75 × max(0, outcome_score))) over `informed`
links from decisions. Both are projection-written, stored undecayed, decayed at query time under the
entry's CLASS half-life (halflife_fact 180 / halflife_idea 90 / halflife_volatile 14 — governed knobs
that now drive confidence decay too). `access_count` is never read. Pinned numbers at fixed $HIVE_NOW.
"""

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _decision, _link, _project, _entry, _gov)  # noqa: E402

NOW = "2026-06-30T00:00:00Z"
CONTENT = "the deploy succeeded at commit abc123"


def _hv(home, monkeypatch):
    hv = _loadhv(home, monkeypatch)
    monkeypatch.setenv("HIVE_NOW", NOW)
    return hv


def _f(hv, dev, content, ts, importance=0.5, tags=()):
    return _entry(hv, dev, "fact", {"content": content, "tags": list(tags), "importance": importance,
                                    "source": "manual"}, ts)


def _idea(hv, dev, content, ts):
    return _entry(hv, dev, "idea", {"content": content, "tags": [], "source": "manual", "channel": "introspect"}, ts)


def _fact_row(conn, content):
    return conn.execute("SELECT importance, utility, last_link_at, confidence FROM facts WHERE content = ?",
                        (content,)).fetchone()


def _squash(x):
    return 1 - 2 ** (-x)


def _json(hive, *args):
    return json.loads(hive.run("search", *args, "--format", "json").stdout)


# ── importance: capped self-assertion, other-identity links only ─────────────────────────────────

def test_self_assertion_is_capped_and_only_other_principals_raise_it(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f = _f(hv, a, CONTENT, "2026-06-01T00:00:00Z", importance=0.9)
    conn = _project(hv, tmp_path, base + [f])
    assert _fact_row(conn, CONTENT)["importance"] == 0.3                         # min(0.9, cap 0.3), no links
    assert _fact_row(conn, CONTENT)["last_link_at"] is None
    # a `supports` link from another principal (b) → one full-weight unit of attention
    other = _fact(hv, b, "iostat agrees", "2026-06-02T00:00:00Z")
    sup = _link(hv, b, "supports", other, f, "2026-06-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [f, other, sup])
    row = _fact_row(conn, CONTENT)
    assert abs(row["importance"] - (0.3 + 0.6 * _squash(1.0))) < 1e-6           # 0.6
    assert row["last_link_at"] == "2026-06-03T00:00:00Z"
    # the SAME principal (a itself, and a second agent on a's device) adds nothing
    own = _fact(hv, a, "my own corroboration", "2026-06-02T00:00:01Z")
    self_sup = _link(hv, a, "supports", own, f, "2026-06-03T00:00:01Z")
    agent2 = _link(hv, a, "supports", own, f, "2026-06-03T00:00:02Z", source="hermes:primary/other/s1")
    conn = _project(hv, tmp_path, base + [f, own, self_sup, agent2])
    assert _fact_row(conn, CONTENT)["importance"] == 0.3 and _fact_row(conn, CONTENT)["last_link_at"] is None
    # one identity's repeated links of one kind count once; a second KIND from it adds
    sup2 = _link(hv, b, "supports", other, f, "2026-06-04T00:00:00Z")
    conn = _project(hv, tmp_path, base + [f, other, sup, sup2])
    assert abs(_fact_row(conn, CONTENT)["importance"] - 0.6) < 1e-6
    d = _decision(hv, b, "act on it", "2026-06-05T00:00:00Z")
    inf = _link(hv, b, "informed", d, f, "2026-06-05T00:00:01Z")
    conn = _project(hv, tmp_path, base + [f, other, sup, d, inf])
    assert abs(_fact_row(conn, CONTENT)["importance"] - (0.3 + 0.6 * _squash(2.0))) < 1e-6   # 0.75


def test_volatile_term_and_per_class_half_lives(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    _, (a, b, _c), base = _owned_hive(hv)
    vol = _f(hv, a, "portproxy is currently down", "2026-06-16T00:00:00Z", tags=["volatile"])   # 14 d old
    plain = _f(hv, a, "the sync daemon listens on 8787", "2026-06-16T00:00:00Z")                # 14 d old
    idea = _idea(hv, a, "the macos runner is slow because of disk io", "2026-01-01T00:00:00Z")
    obs = _fact(hv, b, "iostat showed 95% disk busy", "2026-04-01T00:00:00Z")
    sup = _link(hv, b, "supports", obs, idea, "2026-04-01T00:00:00Z")                            # 90 d old
    conn = _project(hv, tmp_path, base + [vol, plain, idea, obs, sup])
    assert abs(_fact_row(conn, "portproxy is currently down")["importance"] - 0.4) < 1e-6      # 0.3 + w_volatile
    assert _fact_row(conn, "the sync daemon listens on 8787")["importance"] == 0.3
    s = hv.api_search("")
    by = {f["content"]: f for f in s["facts"]}
    assert by["portproxy is currently down"]["confidence"] == round(0.45 * 0.5 ** (14 / 14), 2)   # volatile: 14 d
    assert by["the sync daemon listens on 8787"]["confidence"] == round(0.45 * 0.5 ** (14 / 180), 2)
    ideas = {i["content"]: i for i in s["ideas"]}
    assert ideas["the macos runner is slow because of disk io"]["confidence"] == round(0.45 * 0.5 ** (90 / 90), 2)
    # idea importance: no asserted prior, one other-principal link → 0.6 × squash(1) = 0.3, decayed 90 d at hl 90
    assert ideas["the macos runner is slow because of disk io"]["importance"] == round(0.3 * 0.5, 2)


# ── utility ──────────────────────────────────────────────────────────────────────────────────────

def test_utility_prior_then_monotone_in_outcome_and_floored_on_negative(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    f = _f(hv, a, CONTENT, "2026-06-01T00:00:00Z")
    d = _decision(hv, b, "ship it", "2026-06-02T00:00:00Z")
    inf = _link(hv, b, "informed", d, f, "2026-06-02T00:00:01Z")
    conn = _project(hv, tmp_path, base + [f, d, inf])
    u0 = _fact_row(conn, CONTENT)["utility"]
    assert abs(u0 - _squash(0.25)) < 1e-6                                          # used, outcome unknown
    # one sense outcome (+1) by c → outcome_score 0.45 → multiplier 0.25 + 0.75×0.45
    o1 = _fact(hv, c, "it worked", "2026-06-03T00:00:00Z")
    ok = _link(hv, c, "outcome-of", o1, d, "2026-06-03T00:00:01Z", data={"polarity": 1})
    conn = _project(hv, tmp_path, base + [f, d, inf, o1, ok])
    u1 = _fact_row(conn, CONTENT)["utility"]
    assert abs(u1 - _squash(0.25 + 0.75 * 0.45)) < 1e-6 and u1 > u0
    # a second independent +1 (a) → 0.675 → higher still (monotone)
    o2 = _fact(hv, a, "it worked for me too", "2026-06-04T00:00:00Z")
    ok2 = _link(hv, a, "outcome-of", o2, d, "2026-06-04T00:00:01Z", data={"polarity": 1})
    conn = _project(hv, tmp_path, base + [f, d, inf, o1, ok, o2, ok2])
    u2 = _fact_row(conn, CONTENT)["utility"]
    assert abs(u2 - _squash(0.25 + 0.75 * 0.675)) < 1e-6 and u2 > u1
    # a negative outcome floors at the prior — being used badly is still being used, not less than unused
    bad = _fact(hv, c, "it failed", "2026-06-03T00:00:00Z")
    nok = _link(hv, c, "outcome-of", bad, d, "2026-06-03T00:00:01Z", data={"polarity": -1})
    conn = _project(hv, tmp_path, base + [f, d, inf, bad, nok])
    assert abs(_fact_row(conn, CONTENT)["utility"] - _squash(0.25)) < 1e-6
    # an idea earns utility the same way; a fact nobody decided on has none
    idea = _idea(hv, a, "maybe rollback", "2026-06-01T00:00:01Z")
    d2 = _decision(hv, c, "try the rollback", "2026-06-05T00:00:00Z")
    inf2 = _link(hv, c, "informed", d2, idea, "2026-06-05T00:00:01Z")
    other = _f(hv, a, "unrelated observation", "2026-06-01T00:00:02Z")
    conn = _project(hv, tmp_path, base + [f, d, inf, idea, d2, inf2, other])
    assert abs(conn.execute("SELECT utility FROM ideas").fetchone()[0] - _squash(0.25)) < 1e-6
    assert _fact_row(conn, "unrelated observation")["utility"] == 0.0


# ── governance: knobs change projections identically on two nodes ───────────────────────────────

def test_knobs_are_governed_and_identical_on_two_nodes(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "n1", monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    f = _f(hv, a, CONTENT, "2026-06-01T00:00:00Z", importance=0.9)
    other = _fact(hv, b, "iostat agrees", "2026-06-02T00:00:00Z")
    sup = _link(hv, b, "supports", other, f, "2026-06-03T00:00:00Z")
    knobs = [_gov(hv, {"action": "set-config", "key": "importance_self_cap", "value": 0.5}, oseed, opub, "2026-01-01T00:00:09Z", 9),
             _gov(hv, {"action": "set-config", "key": "w_links", "value": 0.2}, oseed, opub, "2026-01-01T00:00:10Z", 10),
             _gov(hv, {"action": "set-config", "key": "halflife_fact", "value": 30}, oseed, opub, "2026-01-01T00:00:11Z", 11)]
    entries = base + knobs + [f, other, sup]
    gov = hv._governance_state(entries)
    assert (gov["config"]["importance_self_cap"], gov["config"]["w_links"], gov["config"]["halflife_fact"]) == (0.5, 0.2, 30.0)
    assert hv._halflife(gov, "fact") == 30.0 and hv._halflife(gov, "idea") == 90.0 and hv._halflife(gov, "volatile") == 14.0

    def snap(home, order):
        h = _hv(home, monkeypatch)
        conn = _project(h, home, list(order))
        r = _fact_row(conn, CONTENT)
        return (round(r["importance"], 6), round(r["utility"], 6), r["last_link_at"])

    s1, s2 = snap(tmp_path / "n1", entries), snap(tmp_path / "n2", reversed(entries))
    assert s1 == s2 and abs(s1[0] - (0.5 + 0.2 * _squash(1.0))) < 1e-6              # 0.6 under the new knobs
    # the fact half-life knob reaches the read path: last evidence = b's supports link (27 days old) under
    # hl 30 → ×0.5^(27/30); a's assertion + b's link = 2 identities → 0.675 base
    monkeypatch.setenv("HIVE_HOME", str(tmp_path / "n1"))
    h1 = _hv(tmp_path / "n1", monkeypatch)
    _project(h1, tmp_path / "n1", entries)
    conf = {x["content"]: x["confidence"] for x in h1.api_search("")["facts"]}[CONTENT]
    assert conf == round(0.675 * 0.5 ** (27 / 30), 2)
    # default config: unchanged defaults
    cfg = hv._governance_state([])["config"]
    assert (cfg["importance_self_cap"], cfg["w_links"], cfg["w_volatile"]) == (0.3, 0.6, 0.1)
    assert (cfg["halflife_fact"], cfg["halflife_idea"], cfg["halflife_volatile"]) == (180.0, 90.0, 14.0)


# ── node-local state never enters the projection ────────────────────────────────────────────────

def test_access_count_never_alters_importance_or_utility(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f = _f(hv, a, CONTENT, "2026-06-01T00:00:00Z", importance=0.9)
    d = _decision(hv, b, "ship it", "2026-06-02T00:00:00Z")
    inf = _link(hv, b, "informed", d, f, "2026-06-02T00:00:01Z")
    entries = base + [f, d, inf]
    conn = _project(hv, tmp_path, entries)
    before = tuple(_fact_row(conn, CONTENT))
    conn.execute("UPDATE facts SET access_count = 999, last_accessed = ?", (NOW,))
    conn.commit()
    hv._recompute_importance(conn, entries)
    hv._recompute_utility(conn, entries)
    conn.commit()
    assert tuple(_fact_row(conn, CONTENT)) == before
    src = (PROJECT / "hv").read_text()
    for fn in ("_link_attention", "_utility_evidence", "_recompute_importance", "_recompute_utility", "_salience_rows"):
        body = src.split(f"def {fn}(")[1].split("\ndef ")[0]
        assert "access_count" not in body.replace("`access_count` / `last_accessed` are never read", "") \
            and "last_accessed" not in body.replace("`access_count` / `last_accessed` are never read", "")


# ── search ranking (CLI) ────────────────────────────────────────────────────────────────────────

def test_search_sort_importance_utility_and_default_unchanged(hive):
    hive.run("remember", "alpha claim about the build", "--importance", "0.9", "--source", "alice")
    hive.run("remember", "beta claim about the build", "--source", "alice")
    hive.run("remember", "gamma status of the build is down", "--tags", "volatile", "--source", "alice")
    hive.run("decide", "act on alpha", "--rationale", "r", "--informed", "1")
    rows = _json(hive, "build")
    facts = [r for r in rows if r["kind"] == "fact"]
    assert all("importance" in r and "effective_importance" in r and "utility" in r and "effective_utility" in r for r in facts)
    # default: effective confidence (all 0.45) then newest first — unchanged behaviour
    assert [r["content"].split()[0] for r in facts] == ["gamma", "beta", "alpha"]
    by_imp = [r["content"].split()[0] for r in _json(hive, "build", "--sort", "importance") if r["kind"] == "fact"]
    assert by_imp[0] == "gamma"                                                       # 0.4 (volatile) > 0.3
    assert {r["content"].split()[0]: r["importance"] for r in facts}["alpha"] == 0.3   # 0.9 hint capped
    by_util = [r["content"].split()[0] for r in _json(hive, "build", "--sort", "utility") if r["kind"] == "fact"]
    assert by_util[0] == "alpha"                                                      # the only one a decision relied on
    assert {r["content"].split()[0]: round(r["utility"], 6) for r in facts}["alpha"] == round(_squash(0.25), 6)
    txt = hive.run("search", "build", "--sort", "utility").stdout
    assert "Imp: " in txt and "Util: " in txt


def test_api_search_sorts_and_salience_alias(hive, monkeypatch):
    hive.run("remember", "alpha claim about the build", "--importance", "0.9", "--source", "alice")
    hive.run("remember", "gamma status of the build is down", "--tags", "volatile", "--source", "alice")
    hive.run("decide", "act on alpha", "--rationale", "r", "--informed", "1")
    hv = _loadhv(hive.home, monkeypatch)
    monkeypatch.setenv("HIVE_HOME", str(hive.home))
    assert hv.api_search("build", sort="salience")["facts"] == hv.api_search("build", sort="confidence")["facts"]
    assert hv.api_search("build", sort="importance")["facts"][0]["content"].startswith("gamma")
    assert hv.api_search("build", sort="utility")["facts"][0]["content"].startswith("alpha")
    assert all("importance" in f and "utility" in f for f in hv.api_search("build")["facts"])
    html = (PROJECT / "dashboard" / "index.html").read_text()
    assert 'value="importance"' in html and 'value="utility"' in html and "sort:'confidence'" in html
    assert 'q.get("sort", ["confidence"])' in (PROJECT / "hive_sync_daemon.py").read_text()


# ── two-node differential ───────────────────────────────────────────────────────────────────────

def test_two_node_differential_importance_and_utility(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "n1", monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    f = _f(hv, a, CONTENT, "2026-06-01T00:00:00Z", importance=0.9)
    f_dup = _f(hv, c, CONTENT, "2026-06-01T00:00:05Z", importance=0.1)              # corroboration → one row
    idea = _idea(hv, a, "maybe rollback", "2026-06-01T00:00:01Z")
    other = _fact(hv, b, "iostat agrees", "2026-06-02T00:00:00Z")
    sup = _link(hv, b, "supports", other, f_dup, "2026-06-03T00:00:00Z")            # links the SECOND entry
    d = _decision(hv, b, "ship it", "2026-06-02T00:00:00Z")
    inf = _link(hv, b, "informed", d, f, "2026-06-02T00:00:01Z")
    inf_i = _link(hv, b, "informed", d, idea, "2026-06-02T00:00:02Z")
    o1 = _fact(hv, c, "it worked", "2026-06-03T00:00:00Z")
    ok = _link(hv, c, "outcome-of", o1, d, "2026-06-03T00:00:01Z", data={"polarity": 1})
    entries = base + [f, f_dup, idea, other, sup, d, inf, inf_i, o1, ok]

    def snap(home, order):
        h = _hv(home, monkeypatch)
        conn = _project(h, home, list(order))
        return ([tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in
                 conn.execute("SELECT content, importance, utility, last_link_at FROM facts ORDER BY content")],
                [tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in
                 conn.execute("SELECT content, importance, utility, last_link_at FROM ideas ORDER BY content")])

    s1, s2 = snap(tmp_path / "n1", entries), snap(tmp_path / "n2", reversed(entries))
    assert s1 == s2
    facts = {r[0]: r for r in s1[0]}
    # the corroborated row aggregates BOTH entries: prior = min(max(0.9, 0.1), 0.3); attention from b via
    # the second entry (supports) + the informed link on the first → two kinds from one identity
    assert abs(facts[CONTENT][1] - (0.3 + 0.6 * _squash(2.0))) < 1e-6
    assert abs(facts[CONTENT][2] - _squash(0.25 + 0.75 * 0.45)) < 1e-6
    assert facts[CONTENT][3] == "2026-06-03T00:00:00Z"
    ideas = {r[0]: r for r in s1[1]}
    assert abs(ideas["maybe rollback"][1] - 0.6 * _squash(1.0)) < 1e-6 and abs(ideas["maybe rollback"][2] - _squash(0.25 + 0.75 * 0.45)) < 1e-6
