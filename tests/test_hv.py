"""Phase 1 foundation tests for the hv CLI.

Offline (no network): each test drives the real CLI against a temp HIVE_HOME.
Run with:
    python3 -m pytest -q
"""

import hashlib
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import merkle  # noqa: E402


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _hash(entry):
    return "sha256:" + hashlib.sha256(_canonical(entry)).hexdigest()


def test_wal_enabled(hive):
    hive.run("stats")  # triggers init_db
    mode = hive.query("PRAGMA journal_mode")[0][0]
    assert mode.lower() == "wal"


def test_remember_and_fts_search(hive):
    hive.run("remember", "David prefers parallel delegation", "--tags", "workflow")
    out = hive.run("search", "parallel").stdout
    assert "parallel delegation" in out
    # multi-term is an implicit AND
    hive.run("remember", "TBHH is a real estate team")
    assert "real estate" in hive.run("search", "real estate").stdout


def test_search_punctuation_does_not_crash(hive):
    hive.run("remember", "uses C++ and (parens)")
    r = hive.run("search", 'C++ (parens) & punctuation!')
    assert r.returncode == 0  # sanitized into a safe FTS expression


def test_dedup_boosts_trust(hive):
    hive.run("remember", "exact same content")
    r = hive.run("remember", "exact same content")
    assert "boosted" in r.stdout
    rows = hive.query(
        "SELECT count(*) c, max(trust_score) t FROM facts WHERE content=?",
        ("exact same content",),
    )
    assert rows[0]["c"] == 1
    assert rows[0]["t"] > 1.0


def test_decide_supersede(hive):
    hive.run("decide", "old decision")
    hive.run("decide", "new decision", "--supersedes", "1")
    assert hive.query("SELECT superseded_by FROM decisions WHERE id=1")[0]["superseded_by"] is not None
    assert hive.query("SELECT count(*) c FROM decisions WHERE superseded_by IS NULL")[0]["c"] == 1


def test_entity_add_and_link_are_journaled(hive):
    hive.run("remember", "a linkable fact")
    hive.run("entity", "add", "--name", "Acme", "--type", "project")
    hive.run("entity", "link", "--name", "Acme", "--fact-id", "1", "--confidence", "0.9")

    types = [e["type"] for e in hive.entries()]
    assert "entity" in types, "entity add must append a journal entry"
    assert "entity_fact" in types, "entity link must append a journal entry"
    assert hive.query("SELECT count(*) c FROM entity_facts")[0]["c"] == 1


def test_journal_format_and_hash_chain(hive):
    hive.run("remember", "fact one")
    hive.run("decide", "decision one")
    hive.run("entity", "add", "--name", "Zeta")

    entries = sorted(hive.entries(), key=lambda e: e["seq"])
    required = {"node_id", "seq", "type", "timestamp", "payload", "prev_hash"}
    for e in entries:
        assert required <= set(e), f"missing fields in {e}"

    # seq is monotonic from 1
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))

    # prev_hash chains correctly
    assert entries[0]["prev_hash"] == "sha256:genesis"
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev_hash"] == _hash(prev)


def test_merkle_stable_and_changes(hive):
    hive.run("remember", "merkle fact a")
    hive.run("remember", "merkle fact b")
    e1 = merkle.read_all_entries(hive.journal)
    root1 = merkle.merkle_root(merkle.chunk_hashes(e1))
    # Recomputing over the same set is stable
    assert root1 == merkle.merkle_root(merkle.chunk_hashes(merkle.read_all_entries(hive.journal)))
    # A new entry changes the root
    hive.run("remember", "merkle fact c")
    root2 = merkle.merkle_root(merkle.chunk_hashes(merkle.read_all_entries(hive.journal)))
    assert root1 != root2


def test_rebuild_roundtrip(hive):
    hive.run("remember", "rt fact one", "--tags", "a,b")
    hive.run("remember", "rt fact two")
    hive.run("decide", "rt decision")
    hive.run("entity", "add", "--name", "RtEntity", "--type", "concept")
    hive.run("entity", "link", "--name", "RtEntity", "--fact-id", "1")

    before = {
        "facts": hive.query("SELECT count(*) c FROM facts")[0]["c"],
        "decisions": hive.query("SELECT count(*) c FROM decisions")[0]["c"],
        "entities": hive.query("SELECT count(*) c FROM entities")[0]["c"],
        "links": hive.query("SELECT count(*) c FROM entity_facts")[0]["c"],
    }

    hive.run("rebuild")

    after = {
        "facts": hive.query("SELECT count(*) c FROM facts")[0]["c"],
        "decisions": hive.query("SELECT count(*) c FROM decisions")[0]["c"],
        "entities": hive.query("SELECT count(*) c FROM entities")[0]["c"],
        "links": hive.query("SELECT count(*) c FROM entity_facts")[0]["c"],
    }
    assert before == after
    # FTS index survives the rebuild
    assert "rt fact one" in hive.run("search", "fact one").stdout
