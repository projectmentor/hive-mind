"""`hv search --id <sid|ref>` looks one entry up by its stable id from the local store (#158).

It needs no daemon (these tests start none), resolves a sid (`h:…`) and a ref (`node_id:seq`) to the same entry,
survives a rebuild, covers facts, decisions and ideas, and exits 1 on an id that names nothing, such as the
invented `h:e6f0b4c1a2` behind decision h:ee71989fe5."""
import json

import pytest


def _lookup(hive, ident, *extra, check=True):
    return hive.run("search", "--id", ident, *extra, check=check)


@pytest.fixture
def populated(hive):
    hive.run("remember", "the cache is warmed at boot", "--tags", "perf", "--source", "alice")
    hive.run("decide", "warm the cache at boot", "--rationale", "cold starts are slow", "--tags", "perf")
    hive.run("propose", "disk io is the bottleneck", "--tags", "perf", "--source", "bob")
    return hive


def _sids(hive):
    out = {}
    for kind in ("fact", "decision", "idea"):
        rows = json.loads(hive.run("search", "boot" if kind != "idea" else "bottleneck", "--kind", kind,
                                   "--format", "json").stdout)
        out[kind] = rows[0]
    return out


def test_sid_and_ref_resolve_to_the_same_entry_for_each_kind(populated):
    for kind, row in _sids(populated).items():
        by_sid = json.loads(_lookup(populated, row["sid"], "--format", "json").stdout)
        by_ref = json.loads(_lookup(populated, row["ref"], "--format", "json").stdout)
        assert by_sid == by_ref
        assert by_sid["kind"] == kind and by_sid["sid"] == row["sid"] and by_sid["ref"] == row["ref"]
        assert by_sid["item"]["content"] == row["content"]
        assert by_sid["item"]["tags"] == ["perf"]


def test_text_output_names_the_entry(populated):
    row = _sids(populated)["decision"]
    out = _lookup(populated, row["sid"]).stdout
    assert row["sid"] in out and row["ref"] in out and "warm the cache at boot" in out
    assert "cold starts are slow" in out and "current" in out


def test_same_after_a_rebuild(populated):
    row = _sids(populated)["fact"]
    before = _lookup(populated, row["sid"], "--format", "json").stdout
    populated.run("doctor", "rebuild")
    assert _lookup(populated, row["sid"], "--format", "json").stdout == before


@pytest.mark.parametrize("ident", ["h:e6f0b4c1a2", "k1:0000000000000000:99", "nonsense", "h:"])
def test_an_id_that_does_not_resolve_exits_nonzero(populated, ident):
    r = _lookup(populated, ident, check=False)
    assert r.returncode == 1 and r.stdout == ""
    assert "does not resolve" in r.stderr


def test_search_needs_a_query_or_an_id(populated):
    r = populated.run("search", check=False)
    assert r.returncode == 2 and "--id" in r.stderr
