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


def test_nan_importance_in_a_journal_projects_as_the_default(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    es = [_journaled(hv, d, "fact", {"content": f"f{i}", "tags": [], "importance": v, "source": "manual"}, i + 1)
          for i, v in enumerate([0.5, float("nan"), "nan"])]
    conn = _project(hv, tmp_path, es)
    imp = {r["content"]: r["importance"] for r in conn.execute("SELECT content, importance FROM facts")}
    assert imp["f1"] == imp["f0"] == imp["f2"] and imp["f1"] <= 0.3
