"""Phase 0 — cell/comb journal primitives: the deterministic projection over the journal.

`_cell_state` / `_comb_state` mirror `_governance_state`: a pure fold over the G-Set journal that
every node derives identically. "Latest" per name = highest `version`, then (timestamp, node_id,
seq) as a total tie-break. Entries that fail their device signature are skipped; unsigned local
entries are included. These tests drive the real projection functions against synthetic entries.
"""

import importlib.machinery
import importlib.util
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _loadhv(home, monkeypatch):
    """Load hv as a module bound to a temp HIVE_HOME (it reads HIVE_HOME at import)."""
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_cell", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_cell", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _cell(name, version, node="k1:a", seq=1, ts="2026-01-01T00:00:00Z", **spec):
    return {"type": "cell", "node_id": node, "seq": seq, "timestamp": ts,
            "payload": {"name": name, "kind": spec.pop("kind", "tool"),
                        "version": version, **spec}}


def test_latest_version_wins_regardless_of_order(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    v1 = _cell("cloudflare", 1, seq=1, foo="old")
    v2 = _cell("cloudflare", 2, seq=2, foo="new")
    for order in ([v1, v2], [v2, v1]):                 # version dominates, not list order
        state = hv._cell_state(order)
        assert set(state) == {"cloudflare"}
        assert state["cloudflare"]["version"] == 2
        assert state["cloudflare"]["foo"] == "new"


def test_tiebreak_is_total_and_deterministic(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    # same name+version from two devices → deterministic winner by (ts, node_id, seq)
    a = _cell("vultr", 1, node="k1:a", seq=9, ts="2026-01-01T00:00:00Z", who="a")
    b = _cell("vultr", 1, node="k1:b", seq=1, ts="2026-01-01T00:00:00Z", who="b")
    assert hv._cell_state([a, b])["vultr"]["who"] == "b"   # k1:b sorts after k1:a
    assert hv._cell_state([b, a])["vultr"]["who"] == "b"   # order-independent


def test_type_isolation_and_name_required(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    comb = {"type": "comb", "node_id": "k1:a", "seq": 5, "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"name": "stack", "version": 1, "cells": ["cloudflare", "vultr"]}}
    noname = {"type": "cell", "node_id": "k1:a", "seq": 6, "timestamp": "2026-01-01T00:00:00Z",
              "payload": {"version": 1}}
    fact = {"type": "fact", "node_id": "k1:a", "seq": 7, "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"name": "x", "version": 1}}
    entries = [_cell("cloudflare", 1), comb, noname, fact]
    cells = hv._cell_state(entries)
    combs = hv._comb_state(entries)
    assert set(cells) == {"cloudflare"}                # comb/fact/no-name excluded from cells
    assert set(combs) == {"stack"}                     # comb projected separately
    assert combs["stack"]["cells"] == ["cloudflare", "vultr"]


def test_signed_but_invalid_entry_is_skipped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    good = _cell("chrome-devtools-mcp", 1)
    bad = _cell("chrome-devtools-mcp", 2)
    bad["sig"] = "bm90LWEtcmVhbC1zaWc="            # has a sig but it won't verify → skipped
    bad["pub"] = "bm90LWEtcmVhbC1wdWI="
    state = hv._cell_state([good, bad])
    assert state["chrome-devtools-mcp"]["version"] == 1   # the forged v2 is dropped
