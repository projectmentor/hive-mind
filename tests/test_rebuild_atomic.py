"""#206: `rebuild_db` is one transaction. A failure in pass 1 or pass 2 rolls back, so the previous projection
stays queryable, and the connection is closed, so a second rebuild does not wait on a lock."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owned_hive, _fact, _decision, _link, _project  # noqa: E402


def _journal(hv, tmp_path):
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, a, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    d1 = _decision(hv, a, "ship on friday", "2026-01-02T00:00:02Z")
    sup = _link(hv, b, "supports", f2, f1, "2026-01-03T00:00:00Z")
    return base + [f1, f2, d1, sup]


def _counts(home):
    conn = sqlite3.connect(Path(home) / "store.db")
    try:
        return (conn.execute("SELECT count(*) FROM facts").fetchone()[0],
                conn.execute("SELECT count(*) FROM decisions").fetchone()[0],
                conn.execute("SELECT count(*) FROM links").fetchone()[0],
                conn.execute("SELECT count(*) FROM facts_fts WHERE facts_fts MATCH 'deploy'").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def tracked(monkeypatch):
    """Every connection `get_conn` hands out, so a test can see which were left open."""
    seen = []

    def wrap(hv):
        real = hv.get_conn

        def get_conn():
            c = real()
            seen.append(c)
            return c
        monkeypatch.setattr(hv, "get_conn", get_conn)
        return seen
    return wrap


def _open(conn):
    try:
        conn.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False


@pytest.mark.parametrize("where", ["pass1", "pass2", "recompute"])
def test_a_failed_rebuild_keeps_the_previous_projection_and_closes_the_connection(tmp_path, monkeypatch, tracked,
                                                                                  where):
    hv = _loadhv(tmp_path, monkeypatch)
    _project(hv, tmp_path, _journal(hv, tmp_path)).close()
    before = _counts(tmp_path)
    assert before[0] == 2 and before[1] == 1 and before[2] == 1 and before[3] == 2

    seen = tracked(hv)
    boom = RuntimeError("injected")
    if where == "pass1":
        real = hv.persist_entry
        n = {"i": 0}

        def persist_entry(conn, entry):
            n["i"] += 1
            if n["i"] == 2:
                raise boom
            return real(conn, entry)
        monkeypatch.setattr(hv, "persist_entry", persist_entry)
    elif where == "pass2":
        monkeypatch.setattr(hv, "resolve_link", lambda *a, **k: (_ for _ in ()).throw(boom))
    else:
        monkeypatch.setattr(hv, "_recompute_confidence", lambda *a, **k: (_ for _ in ()).throw(boom))
    with pytest.raises(RuntimeError, match="injected"):
        hv.rebuild_db()

    assert _counts(tmp_path) == before                      # the old rows, edges and FTS index are all still there
    assert seen and not any(_open(c) for c in seen)         # no connection left open
    monkeypatch.undo()                                      # drop the injected failure
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    hv2 = _loadhv(tmp_path, monkeypatch)
    hv2._BUSY_TIMEOUT_S = 0.2                                # a lock held by a leaked connection would fail fast
    assert hv2.rebuild_db()["facts"] == 2                   # a second rebuild runs without waiting on a lock
    assert _counts(tmp_path) == before


def test_a_successful_rebuild_is_unchanged_and_closes_its_connection(tmp_path, monkeypatch, tracked):
    hv = _loadhv(tmp_path, monkeypatch)
    entries = _journal(hv, tmp_path)
    seen = tracked(hv)
    _project(hv, tmp_path, entries).close()
    assert _counts(tmp_path) == (2, 1, 1, 2)
    assert seen and not any(_open(c) for c in seen)
