"""#205 — a list, dict or other wrong-shaped value in a payload field the projection binds or reads must not make
`rebuild_db` or a reader raise. Two layers: ingest (`append_foreign_entries`, `append_journal`) refuses the entry,
and the projection defaults or skips a bad value already in a journal (a journal is permanent)."""

import json
import sqlite3
from pathlib import Path

import pytest

from test_links import _loadhv, _device, _entry, _fact, _link, _project

BAD = [[1], {"a": 1}]
TS = "2026-01-01T00:00:0%dZ"


def _jd(home):
    (Path(home) / "journal").mkdir(parents=True, exist_ok=True)


def _base(typ):
    return {"fact": {"content": "f", "tags": [], "importance": 0.5, "source": "manual"},
            "decision": {"content": "d", "rationale": "r", "source": "manual", "tags": []},
            "entity": {"name": "box", "type": "host", "attributes": {}}}[typ]


FIELDS = [("fact", f) for f in ("importance", "confidence", "source_session", "created_at", "access_count", "source", "tags")] + \
         [("decision", f) for f in ("rationale", "created_at", "source", "tags")] + \
         [("entity", f) for f in ("type", "created_at")] + [("entity", "attributes")]


@pytest.mark.parametrize("typ,field", FIELDS)
@pytest.mark.parametrize("bad", BAD, ids=["list", "dict"])
def test_bad_field_is_refused_at_ingest_and_survives_rebuild(tmp_path, monkeypatch, typ, field, bad):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    good = _fact(hv, d, "other row", TS % 1)
    p = _base(typ)
    if field == "attributes" and isinstance(bad, dict):
        pytest.skip("a dict is the valid shape for attributes")
    p[field] = bad
    e = _entry(hv, d, typ, p, TS % 2)
    assert hv._payload_problem(e) is not None
    later = _fact(hv, d, "later", TS % 3)
    assert hv.append_foreign_entries([e])[0] == 0
    # the entry is already in a journal: rebuild succeeds and the other rows stay
    conn = _project(hv, tmp_path, [good, e, later])
    assert {r["content"] for r in conn.execute("SELECT content FROM facts")} >= {"other row", "later"}
    hv.rebuild_db()


def test_ingest_refuses_bad_entry_and_keeps_later_ones(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    bad = _entry(hv, d, "fact", dict(_base("fact"), importance=[1]), TS % 1)
    ok = _entry(hv, d, "fact", _base("fact"), TS % 2)
    accepted, _ = hv.append_foreign_entries([bad, ok])
    assert accepted == 1
    on_disk = [json.loads(l) for f in (tmp_path / "journal").glob("*.jsonl") for l in f.read_text().splitlines() if l]
    assert [e["seq"] for e in on_disk] == [ok["seq"]]


def test_local_writer_refuses_a_bad_payload(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    with pytest.raises(ValueError):
        hv.append_journal("fact", dict(_base("fact"), importance={"a": 1}))
    with pytest.raises(ValueError):
        hv.append_journal("entity", {"name": "x", "type": [1]})
    assert hv.append_journal("fact", _base("fact"))["type"] == "fact"


@pytest.mark.parametrize("bad", [5, [5], "abc", {"a": 1}, [["k1:x"]]], ids=str)
def test_malformed_informed_by_is_refused_and_searches_still_work(tmp_path, monkeypatch, bad):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    e = _entry(hv, d, "decision", dict(_base("decision"), informed_by=bad), TS % 1)
    assert hv._payload_problem(e) is not None
    assert hv.append_foreign_entries([e])[0] == 0
    conn = _project(hv, tmp_path, [e, _fact(hv, d, "still here", TS % 2)])
    conn.close()
    res = hv.api_search("")
    assert any(r["content"] == "still here" for r in res["facts"])
    assert [r["informed_by"] for r in res["decisions"]] == [[]]


@pytest.mark.parametrize("data", [[1], "x", {"confidence": [1]}, {"confidence": {"a": 1}}, {"polarity": [1]}, {"polarity": "x"}],
                         ids=str)
def test_malformed_link_data_is_refused_and_pass2_survives(tmp_path, monkeypatch, data):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent = _entry(hv, d, "entity", _base("entity"), TS % 1)
    fact = _fact(hv, d, "a fact", TS % 2)
    dec = _entry(hv, d, "decision", _base("decision"), TS % 3)
    l1 = _link(hv, d, "entity", ent, fact, TS % 4, data=data)
    l2 = _link(hv, d, "supports", fact, dec, TS % 5, data=data)
    assert hv._payload_problem(l1) is not None
    assert hv.append_foreign_entries([l1])[0] == 0
    conn = _project(hv, tmp_path, [ent, fact, dec, l1, l2])
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    assert [r[0] for r in conn.execute("SELECT confidence FROM entity_facts")] in ([], [1.0])


def test_module_link_kind_keeps_its_own_data(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    fact = _fact(hv, d, "a fact", TS % 1)
    e = _link(hv, d, "x-module-kind", fact, fact, TS % 2, data=[1, 2])
    assert hv._payload_problem(e) is None


def test_valid_entries_and_absent_or_null_fields_are_untouched(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    es = [_entry(hv, d, "fact", {"content": "min"}, TS % 1),
          _entry(hv, d, "fact", dict(_base("fact"), source_session=None, access_count=3, created_at="2026-01-01T00:00:00Z"), TS % 2),
          _entry(hv, d, "decision", {"content": "dec"}, TS % 3),
          _entry(hv, d, "entity", {"name": "e1"}, TS % 4)]
    assert all(hv._payload_problem(e) is None for e in es)
    assert hv.append_foreign_entries(es)[0] == 4
    conn = _project(hv, tmp_path, es)
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 2
    assert conn.execute("SELECT access_count FROM facts WHERE content='f'").fetchone()[0] == 3
    assert hv.rebuild_db()["malformed"] == 0


def test_rebuild_reports_what_it_defaulted(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    bad = _entry(hv, d, "fact", dict(_base("fact"), importance=[1], created_at={"a": 1}), TS % 1)
    conn = _project(hv, tmp_path, [bad])
    assert isinstance(conn.execute("SELECT importance FROM facts").fetchone()[0], float)
    assert hv.rebuild_db()["malformed"] == 2


# --- a journal that already holds an unprojectable entry (a 2.0.2 node stored it): no pass or reader raises ---

def _journaled(hv, d, typ, payload, n=2):
    return _entry(hv, d, typ, payload, TS % n)


UNPROJECTABLE = [
    ("fact", ["not", "a", "dict"]),
    ("fact", {"content": [1], "tags": [], "importance": 0.5, "source": "manual"}),
    ("fact", {"content": {"a": 1}, "tags": [], "importance": 0.5, "source": "manual"}),
    ("decision", ["not", "a", "dict"]),
    ("decision", {"content": [1], "rationale": "r"}),
    ("entity", "not a dict"),
    ("entity", {"name": {"a": 1}}),
    ("link", ["not", "a", "dict"]),
    ("entity_fact", [1]),
]


@pytest.mark.parametrize("typ,payload", UNPROJECTABLE, ids=[f"{t}-{i}" for i, (t, _) in enumerate(UNPROJECTABLE)])
def test_unprojectable_journaled_entry_never_raises_in_any_pass_or_reader(tmp_path, monkeypatch, typ, payload):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    good = _fact(hv, d, "kept", TS % 1)
    bad = _journaled(hv, d, typ, payload)
    later = _fact(hv, d, "later", TS % 3)
    conn = _project(hv, tmp_path, [good, bad, later])
    assert {r["content"] for r in conn.execute("SELECT content FROM facts")} == {"kept", "later"}
    c = hv.rebuild_db()
    assert c["malformed"] == 1 and c["entries"] == 3
    hv.api_search("")


def test_nan_string_importance_defaults_but_a_float_nan_entry_is_skipped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    es = [_journaled(hv, d, "fact", {"content": f"f{i}", "tags": [], "importance": v, "source": "manual"}, i + 1)
          for i, v in enumerate([0.5, float("nan"), "nan"])]
    conn = _project(hv, tmp_path, es)
    imp = {r["content"]: r["importance"] for r in conn.execute("SELECT content, importance FROM facts")}
    assert set(imp) == {"f0", "f2"} and imp["f2"] == imp["f0"] and imp["f2"] <= 0.3     # f1 (a float NaN) is skipped whole


# --- an out-of-range number: `_is_number` returns False, never raises; ingest keeps going; a journal defaults it ---

OVERFLOW = [2**63, 2**1024, -2**1024]


def _overflow_entries(hv, d, v):
    fact = _fact(hv, d, "a fact", TS % 1)
    dec = _entry(hv, d, "decision", _base("decision"), TS % 2)
    ent = _entry(hv, d, "entity", _base("entity"), TS % 3)
    imp = _entry(hv, d, "fact", dict(_base("fact"), content="imp", importance=v), TS % 4)
    lc = _link(hv, d, "supports", fact, dec, TS % 5, data={"confidence": v})
    ef = _link(hv, d, "entity", ent, fact, TS % 6, data={"confidence": v})
    oo = _link(hv, d, "outcome-of", fact, dec, TS % 7, data={"polarity": v}, channel="sense")
    return fact, dec, ent, [imp, lc, ef, oo]


@pytest.mark.parametrize("v", OVERFLOW, ids=["2**63", "2**1024", "-2**1024"])
def test_is_number_never_raises_on_an_overflowing_value(tmp_path, monkeypatch, v):
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._is_number(v) is False
    assert hv._is_number(2**63 - 1) is True and hv._is_number(1.5) is True and hv._is_number(True) is False


@pytest.mark.parametrize("v", OVERFLOW, ids=["2**63", "2**1024", "-2**1024"])
def test_overflowing_number_is_refused_at_ingest_and_the_batch_continues(tmp_path, monkeypatch, v):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    fact, dec, ent, bad = _overflow_entries(hv, d, v)
    ok = _fact(hv, d, "after", TS % 8)
    assert all(hv._payload_problem(e) is not None for e in bad)
    accepted, _ = hv.append_foreign_entries(bad + [ok])
    assert accepted == 1


@pytest.mark.parametrize("v", OVERFLOW, ids=["2**63", "2**1024", "-2**1024"])
def test_journaled_hostile_number_skips_the_whole_entry_and_the_other_rows_survive(tmp_path, monkeypatch, v):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    fact, dec, ent, bad = _overflow_entries(hv, d, v)
    later = _fact(hv, d, "later", TS % 8)
    conn = _project(hv, tmp_path, [fact, dec, ent] + bad + [later])
    assert {r["content"] for r in conn.execute("SELECT content FROM facts")} == {"a fact", "later"}   # "imp" is skipped, not defaulted
    assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 0          # no hostile link projected, not even defaulted
    assert conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0] == 0
    c = hv.rebuild_db()
    assert c["malformed"] == 4 and c["entries"] == 8
    hv.api_search("")


def test_ordinary_shape_mistake_is_still_defaulted_not_skipped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    conn = _project(hv, tmp_path, [_entry(hv, d, "fact", dict(_base("fact"), content="kept", importance=[1]), TS % 1)])
    assert conn.execute("SELECT content FROM facts").fetchone()[0] == "kept"


# --- hostile values: refused at ingest into a local, bounded quarantine; doctor names the signer ---

def _quarantined(home):
    p = Path(home) / ".quarantine.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l] if p.exists() else []


@pytest.mark.parametrize("v", OVERFLOW + [float("nan"), float("inf")], ids=["2**63", "2**1024", "-2**1024", "nan", "inf"])
def test_hostile_value_in_a_synced_batch_is_quarantined_with_reason_and_signer_and_the_next_entry_lands(
        tmp_path, monkeypatch, v):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    bad = _entry(hv, d, "fact", dict(_base("fact"), importance=v), TS % 1)
    ok = _fact(hv, d, "after", TS % 2)
    accepted, _ = hv.append_foreign_entries([bad, ok])
    assert accepted == 1
    q = _quarantined(tmp_path)
    assert len(q) == 1 and json.dumps(q[0]["entry"]) == json.dumps(bad)    # verbatim
    assert "importance" in q[0]["reason"] and q[0]["signer"]["node_id"] == d["id"] and q[0]["at"]
    hv.append_foreign_entries([bad])                                    # a peer re-pushes it: not written twice
    assert len(_quarantined(tmp_path)) == 1


def test_local_writer_quarantines_a_hostile_payload(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    with pytest.raises(ValueError):
        hv.append_journal("fact", dict(_base("fact"), importance=2**63))
    q = _quarantined(tmp_path)
    assert len(q) == 1 and q[0]["entry"]["payload"]["importance"] == 2**63 and q[0]["signer"]["node_id"] == hv.NODE_ID


def test_quarantine_stays_within_its_bound_and_keeps_the_newest(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    monkeypatch.setattr(hv, "QUARANTINE_MAX", 5)
    d = _device(hv)
    bad = [_entry(hv, d, "fact", dict(_base("fact"), content=f"b{i}", importance=2**63), TS % 1) for i in range(12)]
    hv.append_foreign_entries(bad)
    q = _quarantined(tmp_path)
    assert [r["entry"]["payload"]["content"] for r in q] == [f"b{i}" for i in range(7, 12)]


def test_doctor_names_the_signer_of_a_skipped_entry_and_reports_the_quarantine(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    bad = _entry(hv, d, "fact", dict(_base("fact"), importance=2**63), TS % 1)
    odd = _entry(hv, d, "fact", dict(_base("fact"), content="odd", importance=[1]), TS % 2)
    _project(hv, tmp_path, [bad, odd])
    hv.append_foreign_entries([_entry(hv, d, "fact", dict(_base("fact"), importance=2**1024), TS % 3)])
    checks = {c["name"]: c for c in hv._doctor_status()}
    shape = checks["payload-shape"]["detail"]
    assert f"signed by {d['id']}" in shape and "skipped" in shape and "defaulted" in shape
    assert "1 refused entry" in checks["quarantine"]["detail"] and "importance" in checks["quarantine"]["detail"]


# --- the live writers read the journal through the rebuild's one filter (#205) ---

import os
import subprocess
import sys

from test_links import _owned_hive
import _planes


def _cli(home, *args):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"))
    r = subprocess.run([sys.executable, str(_planes.entry_for(args)), *args], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _store(home, sql, *params):
    conn = sqlite3.connect(Path(home) / "store.db")
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _write_journal(home, entries):
    jd = Path(home) / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _fact_state(home, content):
    return _store(home, "SELECT confidence, importance FROM facts WHERE content = ?", content)


def test_remember_agrees_with_rebuild_on_a_hostile_entry_already_on_disk(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    honest = _entry(hv, a, "fact", dict(_base("fact"), content="shared", importance=0.1), TS % 1)
    hostile = _entry(hv, b, "fact", dict(_base("fact"), content="shared", importance=2**63), TS % 2)
    _write_journal(tmp_path, base + [honest, hostile])
    _cli(tmp_path, "doctor", "rebuild")
    first = _fact_state(tmp_path, "shared")
    _cli(tmp_path, "remember", "shared")          # a live write recomputes confidence and importance
    live = _fact_state(tmp_path, "shared")
    _cli(tmp_path, "doctor", "rebuild")
    assert live == _fact_state(tmp_path, "shared")
    _cli(tmp_path, "remember", "another fact")    # and a write of different content recomputes every importance
    other = _fact_state(tmp_path, "shared")
    _cli(tmp_path, "doctor", "rebuild")
    assert other == _fact_state(tmp_path, "shared")
    assert first[0][1] == 0.1                      # the hostile writer's 2**63 never reaches the row


def test_decide_and_outcome_of_agree_with_rebuild_on_a_hostile_polarity(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    dec = _entry(hv, a, "decision", _base("decision"), TS % 1)
    good = _fact(hv, a, "it worked", TS % 2)
    seen_c, seen_b = _fact(hv, c, "seen too", TS % 5), _fact(hv, b, "seen", TS % 3)
    honest = _link(hv, c, "outcome-of", seen_c, dec, TS % 6, data={"polarity": 1})
    hostile = _link(hv, b, "outcome-of", seen_b, dec, TS % 4, data={"polarity": 2**63})
    _write_journal(tmp_path, base + [dec, good, seen_c, seen_b, honest, hostile])
    _cli(tmp_path, "doctor", "rebuild")
    ref = f"{dec['node_id']}:{dec['seq']}"
    _cli(tmp_path, "remember", "the plan held", "--outcome-of", ref)      # live: _recompute_decision_outcome
    live = _store(tmp_path, "SELECT outcome_score FROM decisions")
    assert live[0][0] > 0                          # the honest outcome counts
    _cli(tmp_path, "doctor", "rebuild")
    assert live == _store(tmp_path, "SELECT outcome_score FROM decisions")
    _cli(tmp_path, "decide", "next plan", "--rationale", "r", "--informed", f"{good['node_id']}:{good['seq']}")
    live = _store(tmp_path, "SELECT content, outcome_score FROM decisions ORDER BY content")
    _cli(tmp_path, "doctor", "rebuild")
    assert live == _store(tmp_path, "SELECT content, outcome_score FROM decisions ORDER BY content")


def test_the_quarantine_record_of_a_local_refusal_carries_the_principal_when_admitted(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    monkeypatch.setattr(hv, "_governance_state",
                        lambda entries: {"admitted": [hv.NODE_ID], "principals": {hv.NODE_ID: "p0"}})
    with pytest.raises(ValueError):
        hv.append_journal("fact", dict(_base("fact"), importance=2**63))
    q = _quarantined(tmp_path)
    assert q[0]["signer"] == {"node_id": hv.NODE_ID, "principal": "p0"}
