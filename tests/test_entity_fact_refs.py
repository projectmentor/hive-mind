"""#196 — `rebuild_db` must not raise on an `entity_fact` (or a `supersedes`) whose ref points at the wrong kind
of entry or at no row. Content of these types is accepted from any admitted device, and the journal is permanent,
so a crash here would stop every node's rebuild for good. Each case is a no-op: the entry lands, projects to nothing."""

import json
import sqlite3
from pathlib import Path

import pytest

from test_links import _loadhv, _device, _entry, _fact, _decision, _project


def _rebuild(hv, home, entries):
    conn = _project(hv, home, entries)          # raises on the crash
    hv.rebuild_db()                             # and idempotent
    return conn


def _no_dangling(conn):
    assert conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM decisions WHERE superseded_by IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("kind_of_entity_ref", ["fact", "entity"])
def test_entity_fact_with_wrong_kind_refs_is_skipped(tmp_path, monkeypatch, kind_of_entity_ref):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent1 = _entry(hv, d, "entity", {"name": "box", "type": "host"}, "2026-01-01T00:00:01Z")
    ent2 = _entry(hv, d, "entity", {"name": "box2", "type": "host"}, "2026-01-01T00:00:02Z")
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:03Z")
    ref = lambda e: [e["node_id"], e["seq"]]      # noqa: E731
    # fact_ref -> an entity (the reviewer's reproducer); entity_ref -> a fact
    bad_fact_ref = _entry(hv, d, "entity_fact", {"entity_ref": ref(ent1), "fact_ref": ref(ent2)}, "2026-01-01T00:00:04Z")
    bad_entity_ref = _entry(hv, d, "entity_fact", {"entity_ref": ref(fact), "fact_ref": ref(fact)}, "2026-01-01T00:00:05Z")
    conn = _rebuild(hv, tmp_path, [ent1, ent2, fact, bad_fact_ref, bad_entity_ref])
    _no_dangling(conn)


def test_entity_fact_with_absent_refs_is_skipped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent = _entry(hv, d, "entity", {"name": "box", "type": "host"}, "2026-01-01T00:00:01Z")
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:02Z")
    ghost = ["k1:0000000000000000", 9]
    a = _entry(hv, d, "entity_fact", {"entity_ref": ghost, "fact_ref": [fact["node_id"], fact["seq"]]}, "2026-01-01T00:00:03Z")
    b = _entry(hv, d, "entity_fact", {"entity_ref": [ent["node_id"], ent["seq"]], "fact_ref": ghost}, "2026-01-01T00:00:04Z")
    _no_dangling(_rebuild(hv, tmp_path, [ent, fact, a, b]))


@pytest.mark.parametrize("payload", [
    {"entity_id": 999, "fact_id": 999},        # no such row
    {"entity_id": 999, "fact_id": 1},
    {"entity_id": 1, "fact_id": 999},
    {"entity_id": "1", "fact_id": 1},          # not an int
    {"entity_id": True, "fact_id": True},      # a bool is not a local id
    {"entity_id": 1.5, "fact_id": 1},
])
def test_entity_fact_with_a_bad_legacy_local_id_is_skipped(tmp_path, monkeypatch, payload):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent = _entry(hv, d, "entity", {"name": "box", "type": "host"}, "2026-01-01T00:00:01Z")
    bad = _entry(hv, d, "entity_fact", dict(payload), "2026-01-01T00:00:02Z")
    # a fact row 1 exists only when a fact is journalled first; with none, fact_id 1 is also absent
    _no_dangling(_rebuild(hv, tmp_path, [ent, bad]))


def test_entity_fact_with_valid_refs_and_legacy_ids_still_wires(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent = _entry(hv, d, "entity", {"name": "box", "type": "host"}, "2026-01-01T00:00:01Z")
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:02Z")
    byref = _entry(hv, d, "entity_fact", {"entity_ref": [ent["node_id"], ent["seq"]],
                                          "fact_ref": [fact["node_id"], fact["seq"]]}, "2026-01-01T00:00:03Z")
    conn = _rebuild(hv, tmp_path, [ent, fact, byref])
    assert [tuple(r) for r in conn.execute("SELECT entity_id, fact_id FROM entity_facts")] == [(1, 1)]
    legacy = _entry(hv, d, "entity_fact", {"entity_id": 1, "fact_id": 1, "confidence": 0.5}, "2026-01-01T00:00:04Z")
    conn = _rebuild(hv, tmp_path, [ent, fact, legacy])
    assert [tuple(r) for r in conn.execute("SELECT entity_id, fact_id, confidence FROM entity_facts")] == [(1, 1, 0.5)]


def test_supersedes_with_a_wrong_kind_or_absent_target_marks_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    dec = _decision(hv, d, "the decision", "2026-01-01T00:00:01Z")
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:02Z")
    wrong = _entry(hv, d, "decision", {"content": "replaces a fact?", "rationale": "r", "source": "manual", "tags": [],
                                       "supersedes_ref": [fact["node_id"], fact["seq"]]}, "2026-01-01T00:00:03Z")
    ghost = _entry(hv, d, "decision", {"content": "replaces nothing", "rationale": "r", "source": "manual", "tags": [],
                                       "supersedes_ref": ["k1:0000000000000000", 9]}, "2026-01-01T00:00:04Z")
    legacy = _entry(hv, d, "decision", {"content": "legacy ghost", "rationale": "r", "source": "manual", "tags": [],
                                        "supersedes": 999}, "2026-01-01T00:00:05Z")
    _no_dangling(_rebuild(hv, tmp_path, [dec, fact, wrong, ghost, legacy]))


def test_supersedes_by_ref_and_by_legacy_id_still_marks_the_old_decision(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    dec = _decision(hv, d, "the decision", "2026-01-01T00:00:01Z")
    new = _entry(hv, d, "decision", {"content": "the replacement", "rationale": "r", "source": "manual", "tags": [],
                                     "supersedes_ref": [dec["node_id"], dec["seq"]]}, "2026-01-01T00:00:02Z")
    conn = _rebuild(hv, tmp_path, [dec, new])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE id = 1").fetchone()[0] == 2
    legacy = _entry(hv, d, "decision", {"content": "legacy replacement", "rationale": "r", "source": "manual",
                                        "tags": [], "supersedes": 1}, "2026-01-01T00:00:03Z")
    conn = _rebuild(hv, tmp_path, [dec, legacy])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE id = 1").fetchone()[0] == 2


_BIG = 2**64
_BAD_REFS = [["x", _BIG], [{"a": 1}, 1], [[1], [2]], {"a": 1}, ["x"], "xy", [True, 1]]


@pytest.mark.parametrize("payload", [
    {"entity_id": _BIG, "fact_id": 1},
    *({"entity_ref": r, "fact_id": 1} for r in _BAD_REFS),
    *({"entity_id": 1, "fact_ref": r} for r in _BAD_REFS),
])
def test_entity_fact_with_a_malformed_value_is_skipped(tmp_path, monkeypatch, payload):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent = _entry(hv, d, "entity", {"name": "box", "type": "host"}, "2026-01-01T00:00:01Z")
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:02Z")
    bad = _entry(hv, d, "entity_fact", dict(payload), "2026-01-01T00:00:03Z")
    _no_dangling(_rebuild(hv, tmp_path, [ent, fact, bad]))


@pytest.mark.parametrize("payload", [{"supersedes": _BIG}, *({"supersedes_ref": r} for r in _BAD_REFS)])
def test_supersedes_with_a_malformed_value_marks_nothing(tmp_path, monkeypatch, payload):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    dec = _decision(hv, d, "the decision", "2026-01-01T00:00:01Z")
    bad = _entry(hv, d, "decision", {"content": "x", "rationale": "r", "source": "manual", "tags": [],
                                     **payload}, "2026-01-01T00:00:02Z")
    _no_dangling(_rebuild(hv, tmp_path, [dec, bad]))


@pytest.mark.parametrize("ref", _BAD_REFS)
def test_resolves_and_link_with_a_malformed_ref_are_skipped(tmp_path, monkeypatch, ref):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:01Z")
    res = _entry(hv, d, "fact", {"content": "r", "source": "manual", "tags": [], "resolves_ref": ref},
                 "2026-01-01T00:00:02Z")
    link = _entry(hv, d, "link", {"kind": "relates", "from_ref": [fact["node_id"], fact["seq"]], "to_ref": ref,
                                  "data": {}, "source": "manual"}, "2026-01-01T00:00:03Z")
    _no_dangling(_rebuild(hv, tmp_path, [fact, res, link]))


@pytest.mark.parametrize("bad", ["1", True, 1.5, [1], {"a": 1}, _BIG, -_BIG])
@pytest.mark.parametrize("which", ["entity_id", "fact_id"])
def test_a_legacy_local_id_of_the_wrong_type_is_skipped_though_both_rows_exist(tmp_path, monkeypatch, bad, which):
    """Entity 1 and fact 1 both exist, so only the type/range guard stands between this entry and a wrong wiring
    (`"1"` and `True` would bind as 1 through SQLite affinity; a list or an out-of-range int raises on bind)."""
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    ent = _entry(hv, d, "entity", {"name": "box", "type": "host"}, "2026-01-01T00:00:01Z")
    fact = _fact(hv, d, "a fact", "2026-01-01T00:00:02Z")
    bad_entry = _entry(hv, d, "entity_fact", {"entity_id": 1, "fact_id": 1, which: bad}, "2026-01-01T00:00:03Z")
    _no_dangling(_rebuild(hv, tmp_path, [ent, fact, bad_entry]))
