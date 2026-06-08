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


def test_stats_excludes_forgotten_from_average(hive):
    import re
    hive.run("remember", "fact alpha is true", "--source", "alice")
    out = hive.run("remember", "fact beta is true", "--source", "bob").stdout
    fid = re.search(r"#(\d+)", out).group(1)
    hive.run("retract", fid, "--owner")          # -> FORGET_FLOOR (-1.0)
    s = hive.run("stats").stdout
    assert "1 live" in s and "1 forgotten" in s, s
    # average is over the live fact (~0.45), NOT dragged toward -1.0 by the forgotten one
    m = re.search(r"avg confidence: ([\-0-9.]+)", s)
    assert m and float(m.group(1)) > 0, s


def test_search_punctuation_does_not_crash(hive):
    hive.run("remember", "uses C++ and (parens)")
    r = hive.run("search", 'C++ (parens) & punctuation!')
    assert r.returncode == 0  # sanitized into a safe FTS expression


def test_repetition_does_not_inflate_confidence(hive):
    # Same content from the SAME source, twice → content-dedups to one row and
    # confidence must NOT rise (repetition is not evidence).
    hive.run("remember", "exact same content", "--source", "alice")
    r = hive.run("remember", "exact same content", "--source", "alice")
    assert "Corroborated" in r.stdout and "boosted" not in r.stdout
    rows = hive.query(
        "SELECT count(*) c, max(confidence) conf FROM facts WHERE content=?",
        ("exact same content",),
    )
    assert rows[0]["c"] == 1                      # still one row
    assert abs(rows[0]["conf"] - 0.45) < 1e-6     # 1 distinct source → 0.45, unchanged


def test_distinct_sources_raise_confidence_with_cap(hive):
    for src in ("alice", "bob", "carol"):
        hive.run("remember", "corroborated claim", "--source", src)
    conf = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("corroborated claim",)
    )[0]["confidence"]
    assert abs(conf - 0.7875) < 1e-6              # 3 distinct sources, diminishing returns
    # Seven more distinct sources still stay strictly below the cap.
    for src in ("d", "e", "f", "g", "h", "i", "j"):
        hive.run("remember", "corroborated claim", "--source", src)
    capped = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("corroborated claim",)
    )[0]["confidence"]
    assert capped < 0.90


def test_confidence_is_pure_projection_across_rebuild(hive):
    for src in ("alice", "bob"):
        hive.run("remember", "stable claim", "--source", src)
    before = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("stable claim",)
    )[0]["confidence"]
    hive.run("rebuild")
    after = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("stable claim",)
    )[0]["confidence"]
    assert abs(before - after) < 1e-9 and abs(before - 0.675) < 1e-6


def test_same_agent_across_sessions_is_idempotent(hive):
    # D0: a structured source varies per session, but the IDENTITY (node, app,
    # instance) does not — so one agent across two sessions must NOT inflate.
    hive.run("remember", "structured claim", "--source", "hermes:primary/default/aaaa1111")
    hive.run("remember", "structured claim", "--source", "hermes:primary/default/bbbb2222")
    rows = hive.query(
        "SELECT count(*) c, max(confidence) conf FROM facts WHERE content=?",
        ("structured claim",),
    )
    assert rows[0]["c"] == 1
    assert abs(rows[0]["conf"] - 0.45) < 1e-6     # one identity, two sessions → 0.45


def test_distinct_structured_agents_corroborate(hive):
    hive.run("remember", "joint claim", "--source", "hermes:primary/default/aaaa1111")
    hive.run("remember", "joint claim", "--source", "claude-code")
    conf = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("joint claim",)
    )[0]["confidence"]
    assert abs(conf - 0.675) < 1e-6               # two distinct identities


def test_context_class_weighting(hive):
    # A subagent assertion is worth less than a primary one (weight 0.5).
    hive.run("remember", "sub claim", "--source", "hermes:subagent/default/aaaa1111")
    conf = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("sub claim",)
    )[0]["confidence"]
    assert abs(conf - 0.263604) < 1e-5            # _confidence_for(0.5) = 0.9*(1-0.5**0.5)


def _fid(hive, content):
    return hive.query("SELECT id FROM facts WHERE content=?", (content,))[0]["id"]


def test_peer_retract_drives_negative(hive):
    # 1 corroborator, 2 distinct peer retractors → net = 1 - 2 = -1 → -0.45 (rejected).
    hive.run("remember", "claim to reject", "--source", "alice")
    fid = _fid(hive, "claim to reject")
    hive.run("retract", str(fid), "--source", "bob")
    hive.run("retract", str(fid), "--source", "carol")
    conf = hive.query("SELECT confidence FROM facts WHERE content=?", ("claim to reject",))[0]["confidence"]
    assert abs(conf - (-0.45)) < 1e-6


def test_owner_retract_forgets(hive):
    # Owner retract is decisive — drives to the forget floor regardless of corroboration.
    hive.run("remember", "owner forget me", "--source", "alice")
    hive.run("remember", "owner forget me", "--source", "bob")     # corroborated → 0.675
    fid = _fid(hive, "owner forget me")
    hive.run("retract", str(fid), "--owner")
    conf = hive.query("SELECT confidence FROM facts WHERE content=?", ("owner forget me",))[0]["confidence"]
    assert abs(conf - (-1.0)) < 1e-9


def test_rejected_hidden_from_default_search(hive):
    hive.run("remember", "rejected thing zzz", "--source", "alice")
    fid = _fid(hive, "rejected thing zzz")
    hive.run("retract", str(fid), "--source", "bob")
    hive.run("retract", str(fid), "--source", "carol")            # net -1 → -0.45
    assert "rejected thing zzz" not in hive.run("search", "rejected thing zzz").stdout
    assert "rejected thing zzz" in hive.run("search", "rejected thing zzz", "--min-confidence=-1").stdout


def test_retract_is_pure_projection_across_rebuild(hive):
    hive.run("remember", "rebuild retract", "--source", "alice")
    fid = _fid(hive, "rebuild retract")
    hive.run("retract", str(fid), "--source", "bob")
    hive.run("retract", str(fid), "--source", "carol")
    before = hive.query("SELECT confidence FROM facts WHERE content=?", ("rebuild retract",))[0]["confidence"]
    hive.run("rebuild")
    after = hive.query("SELECT confidence FROM facts WHERE content=?", ("rebuild retract",))[0]["confidence"]
    assert abs(before - after) < 1e-9 and abs(before - (-0.45)) < 1e-6


def test_salience_gate_passes_substantive(hive):
    r = hive.run("remember", "The node-b deploy succeeded at commit abc123.", "--gate", "--source", "alice")
    assert "Remembered" in r.stdout
    assert hive.query("SELECT count(*) c FROM facts WHERE content LIKE 'The node-b%'")[0]["c"] == 1


def test_salience_gate_rejects_noise(hive):
    for junk in ("hi there", "ok", "What now?"):
        assert "Skipped" in hive.run("remember", junk, "--gate", "--source", "alice").stdout
    assert hive.query(
        "SELECT count(*) c FROM facts WHERE content IN ('hi there','ok','What now?')"
    )[0]["c"] == 0


def test_no_gate_writes_anything(hive):
    hive.run("remember", "ok", "--source", "alice")     # no --gate → written verbatim
    assert hive.query("SELECT count(*) c FROM facts WHERE content='ok'")[0]["c"] == 1


def _loadhv():
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader("hvmod", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod", loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m


def test_contested_flag_and_marker(hive):
    # Same claim with BOTH a corroborator and a (distinct) retractor → contested + net 0.
    hive.run("remember", "contested claim", "--source", "alice")
    fid = _fid(hive, "contested claim")
    hive.run("retract", str(fid), "--source", "bob")
    row = hive.query("SELECT confidence, contested FROM facts WHERE content=?", ("contested claim",))[0]
    assert row["contested"] == 1
    assert abs(row["confidence"] - 0.0) < 1e-9                 # base net 1-1 = 0
    assert "CONTESTED" in hive.run("search", "contested claim").stdout


def test_decay_function():
    m = _loadhv()
    t0 = "2026-01-01T00:00:00+00:00"
    assert abs(m._effective_confidence(0.45, t0, t0) - 0.45) < 1e-6            # no age → base
    later = m._effective_confidence(0.45, t0, "2027-01-01T00:00:00+00:00")     # ~1yr → decayed
    assert 0.0 < later < 0.45
    assert m._effective_confidence(-1.0, t0, "2030-01-01T00:00:00+00:00") == -1.0  # forget never decays


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
