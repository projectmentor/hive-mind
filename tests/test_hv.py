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


import os  # noqa: E402
import subprocess  # noqa: E402


def _as(home, node_id, *args):
    """Run hv against `home` under a specific device identity (HIVE_NODE_ID) — for writing the
    same content from DISTINCT devices, which (post D0-v2) is what earns full corroboration."""
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_NODE_ID=node_id)
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


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


def _tags(hive, content):
    r = hive.query("SELECT tags FROM facts WHERE content=?", (content,))[0]["tags"]
    return json.loads(r) if r else []


def test_volatile_autotag_on_status_claim(hive):
    r = hive.run("remember", "the sync daemon is running", "--source", "alice")
    assert "tagged volatile" in r.stdout
    assert "volatile" in _tags(hive, "the sync daemon is running")


def test_volatile_not_tagged_on_durable_fact(hive):
    hive.run("remember", "the deploy succeeded at commit abc123", "--source", "alice")
    assert "volatile" not in _tags(hive, "the deploy succeeded at commit abc123")


def test_volatile_up_to_date_is_not_volatile(hive):
    # 'is up' must NOT match 'is up to date' (durable).
    hive.run("remember", "the docs are up to date", "--source", "alice")
    assert "volatile" not in _tags(hive, "the docs are up to date")


def test_volatile_opt_out(hive):
    hive.run("remember", "node b is reachable", "--no-volatile", "--source", "alice")
    assert "volatile" not in _tags(hive, "node b is reachable")


def test_volatile_explicit_ttl_not_double_tagged(hive):
    hive.run("remember", "node b is reachable", "--tags", "ttl:6h", "--source", "alice")
    t = _tags(hive, "node b is reachable")
    assert "ttl:6h" in t and "volatile" not in t   # explicit ttl already governs freshness


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


def test_distinct_devices_raise_confidence_with_cap(hive):
    # Distinct DEVICES corroborating (post D0-v2: independence is per device).
    for dev in ("dev-a", "dev-b", "dev-c"):
        _as(hive.home, dev, "remember", "corroborated claim", "--source", "agent")
    conf = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("corroborated claim",)
    )[0]["confidence"]
    assert abs(conf - 0.7875) < 1e-6              # 3 distinct devices, diminishing returns
    # Seven more distinct devices still stay strictly below the cap.
    for dev in ("d", "e", "f", "g", "h", "i", "j"):
        _as(hive.home, dev, "remember", "corroborated claim", "--source", "agent")
    capped = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("corroborated claim",)
    )[0]["confidence"]
    assert capped < 0.90


def test_same_device_discount(hive):
    # Several agents on ONE device are correlated, not independent: the device contributes its
    # strongest identity in full plus lambda (0.5) times the rest. Two primary agents -> net 1.5.
    hive.run("remember", "local claim", "--source", "alice")
    hive.run("remember", "local claim", "--source", "bob")
    conf = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("local claim",)
    )[0]["confidence"]
    assert abs(conf - 0.581802) < 1e-5           # _confidence_for(1 + 0.5*1)


def test_confidence_is_pure_projection_across_rebuild(hive):
    for dev in ("dev-a", "dev-b"):
        _as(hive.home, dev, "remember", "stable claim", "--source", "agent")
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
    # Two distinct agents on two distinct DEVICES → full independent corroboration.
    _as(hive.home, "dev-a", "remember", "joint claim", "--source", "hermes:primary/default/aaaa1111")
    _as(hive.home, "dev-b", "remember", "joint claim", "--source", "claude-code")
    conf = hive.query(
        "SELECT confidence FROM facts WHERE content=?", ("joint claim",)
    )[0]["confidence"]
    assert abs(conf - 0.675) < 1e-6               # two distinct devices


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
    # 1 corroborator, 2 distinct-DEVICE peer retractors → net = 1 - 2 = -1 → -0.45 (rejected).
    hive.run("remember", "claim to reject", "--source", "alice")
    fid = _fid(hive, "claim to reject")
    _as(hive.home, "dev-b", "retract", str(fid), "--source", "bob")
    _as(hive.home, "dev-c", "retract", str(fid), "--source", "carol")
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
    _as(hive.home, "dev-b", "retract", str(fid), "--source", "bob")
    _as(hive.home, "dev-c", "retract", str(fid), "--source", "carol")
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


def test_remember_resolves_links_and_soft_retracts(hive):
    hive.run("remember", "issue Z is open", "--source", "alice")
    old = _fid(hive, "issue Z is open")
    out = hive.run("remember", "issue Z is fixed", "--resolves", str(old), "--source", "alice").stdout
    new = _fid(hive, "issue Z is fixed")
    # link recorded on the NEW row, pointing at the resolved fact
    assert hive.query("SELECT resolves FROM facts WHERE id=?", (new,))[0]["resolves"] == old
    # resolved fact soft-retracted: negative evidence, confidence down to ~0, contested, reversible
    row = hive.query("SELECT confidence, contested FROM facts WHERE id=?", (old,))[0]
    assert row["confidence"] <= 0 and row["contested"] == 1
    # a retract entry was journaled against the resolved fact
    assert any(e["type"] == "retract" for e in hive.entries())
    assert "resolved fact" in out


def test_remember_resolves_survives_rebuild(hive):
    hive.run("remember", "host parked", "--source", "alice")
    hive.run("remember", "host live", "--resolves", str(_fid(hive, "host parked")), "--source", "alice")
    before = hive.query("SELECT confidence FROM facts WHERE content=?", ("host parked",))[0]["confidence"]
    hive.run("rebuild")
    new_id, old_id = _fid(hive, "host live"), _fid(hive, "host parked")
    assert hive.query("SELECT resolves FROM facts WHERE id=?", (new_id,))[0]["resolves"] == old_id
    after = hive.query("SELECT confidence FROM facts WHERE content=?", ("host parked",))[0]["confidence"]
    assert abs(before - after) < 1e-9


def test_remember_resolves_missing_target_is_a_warning(hive):
    out = hive.run("remember", "standalone fact", "--resolves", "999", "--source", "alice").stdout
    assert _fid(hive, "standalone fact")            # the fact is still written
    assert "no fact #999" in out.lower()
    assert hive.query("SELECT resolves FROM facts WHERE content=?", ("standalone fact",))[0]["resolves"] is None


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


def test_doctor_reports_all_checks(hive):
    # An empty .peers.json keeps doctor offline: no peers to probe. The journal +
    # database checks are deterministic in a fresh sandbox; authenticity / sync-daemon
    # depend on the signing state and whether a daemon is up, so we don't assert them.
    (hive.home / ".peers.json").write_text('{"peers": []}')
    hive.run("remember", "doctor can see this stored fact", "--source", "alice")

    r = hive.run("doctor", "--format", "json", check=False)
    data = json.loads(r.stdout)
    names = {c["name"]: c["status"] for c in data["checks"]}

    assert set(names) >= {"authenticity", "journal", "database", "hygiene", "sync-daemon", "peers"}
    assert names["journal"] == "ok"        # own hash chain intact from genesis
    assert names["database"] == "ok"       # store.db reflects the journal
    assert names["peers"] == "ok"          # none configured -> nothing to probe
    assert data["verdict"] in {"ok", "warn", "fail"}


def test_doctor_detects_a_broken_journal(hive):
    # Tamper with the journal so the hash chain no longer links; doctor must FAIL it.
    (hive.home / ".peers.json").write_text('{"peers": []}')
    hive.run("remember", "first fact, genesis of the chain")
    hive.run("remember", "second fact, links to the first")

    jf = sorted(hive.journal.glob("*.jsonl"))[0]
    lines = jf.read_text().splitlines()
    entry = json.loads(lines[-1])
    entry["prev_hash"] = "sha256:" + "0" * 64   # break the link
    lines[-1] = json.dumps(entry)
    jf.write_text("\n".join(lines) + "\n")

    r = hive.run("doctor", "--format", "json", check=False)
    data = json.loads(r.stdout)
    journal = next(c for c in data["checks"] if c["name"] == "journal")
    assert journal["status"] == "fail", data
    assert r.returncode == 1


def test_classify_orphans_logic():
    hv = _loadhv()
    f = hv._classify_orphans
    # healthy: the managed daemon is the only one
    assert f([100], 100, True) == []
    # a second daemon outside systemd alongside the managed one
    assert f([100, 200], 100, True) == [200]
    # the gotcha: unit exists but is dead (main_pid None) while a stale daemon runs
    assert f([1212], None, True) == [1212]
    assert sorted(f([1212, 1300], None, True)) == [1212, 1300]
    # no systemd unit at all (manual single daemon) -> not an orphan
    assert f([100], None, False) == []
    # no unit, two manual daemons -> all but the survivor
    assert f([100, 200], None, False) == [200]
    # nothing running
    assert f([], 100, True) == []


def test_claude_hook_spec_is_a_stable_shim():
    # The foreign-config footprint must be exactly the stable dispatch shim: one entry per lifecycle
    # event, all pointing at hive_dispatch.sh — never per-behavior hooks (that set is what drifts).
    hv = _loadhv()
    events = [ev for ev, _c, _t in hv._CLAUDE_HOOK_SPEC]
    assert events == ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact"]
    assert all("hive_dispatch.sh" in cmd for _ev, cmd, _t in hv._CLAUDE_HOOK_SPEC)
    # a config carrying exactly the shim reconciles clean (nothing missing, nothing stale)
    full = {ev: [{"hooks": [{"type": "command", "command": cmd}]}] for ev, cmd, _t in hv._CLAUDE_HOOK_SPEC}
    missing, stale = hv._claude_hook_reconcile({"hooks": full})
    assert missing == [] and stale == []


def test_reconcile_flags_legacy_direct_hooks_as_stale():
    # The gregorius/pre-shim case: a node wired with the OLD direct hooks. Reconcile must report the
    # shims as missing AND the direct hooks as stale (to be pruned so behaviors don't double-fire).
    hv = _loadhv()
    base = hv._CLAUDE_HOOKS_BASE
    cfg = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"{base}/session_hook.sh start"}]},
                         {"hooks": [{"type": "command", "command": f"{base}/nudge_hook.sh session-start"}]}],
        "SessionEnd":   [{"hooks": [{"type": "command", "command": f"{base}/session_hook.sh end"}]}],
    }}
    missing, stale = hv._claude_hook_reconcile(cfg)
    assert len(missing) == 4                                   # all four shims absent
    stale_cmds = {cmd for _ev, cmd in stale}
    assert stale_cmds == {
        f"{base}/session_hook.sh start",
        f"{base}/nudge_hook.sh session-start",
        f"{base}/session_hook.sh end",
    }


def test_wire_claude_hooks_migrates_legacy_and_preserves(tmp_path, monkeypatch):
    hv = _loadhv()
    base = hv._CLAUDE_HOOKS_BASE
    cdir = tmp_path / ".claude"; cdir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cdir))
    # Old-style wiring (telemetry + nudge, inline) PLUS one of the user's OWN unrelated hooks.
    own = {"type": "command", "command": "/usr/local/bin/my-own-hook.sh"}
    settings = cdir / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"{base}/session_hook.sh start"}]},
                         {"hooks": [{"type": "command", "command": f"{base}/nudge_hook.sh session-start"}]}],
        "SessionEnd":   [{"hooks": [{"type": "command", "command": f"{base}/session_hook.sh end"}]}],
        "UserPromptSubmit": [{"hooks": [own, {"type": "command", "command": f"{base}/nudge_hook.sh user-prompt"}]}],
    }}, indent=2))

    res = hv._wire_claude_hooks(write=True)
    assert res["applicable"] and res["wrote"]
    assert len(res["missing"]) == 4 and len(res["stale"]) == 4

    cfg = json.loads(settings.read_text())
    all_cmds = [h["command"] for ev in cfg["hooks"].values() for g in ev for h in g["hooks"]]
    # exactly the four shims are present...
    for _ev, cmd, _to in hv._CLAUDE_HOOK_SPEC:
        assert cmd in all_cmds, cmd
    # ...no legacy direct hooks survive (no double-firing)...
    assert not any("session_hook.sh" in c or "nudge_hook.sh" in c for c in all_cmds)
    # ...the user's own hook is untouched...
    assert own["command"] in all_cmds
    # ...a backup was taken, and the skill symlink repaired...
    assert (cdir / "settings.json.bak.doctor").exists()
    assert (cdir / "skills" / "hive-memory").is_symlink()

    # Idempotent: a second pass changes nothing.
    res2 = hv._wire_claude_hooks(write=True)
    assert not res2["wrote"] and res2["missing"] == [] and res2["stale"] == []


def test_wire_claude_hooks_not_applicable_without_claude(tmp_path, monkeypatch):
    hv = _loadhv()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-here"))
    res = hv._wire_claude_hooks(write=True)
    assert res["applicable"] is False and res["wrote"] is False


def test_doctor_agent_hooks_check(tmp_path, monkeypatch):
    # A pre-shim node must surface an `agent-hooks` warn (shims unwired + legacy to migrate).
    hv = _loadhv()
    base = hv._CLAUDE_HOOKS_BASE
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"{base}/session_hook.sh start"}]}],
    }}, indent=2))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cdir))
    checks = {c["name"]: c for c in hv._doctor_status()}
    assert "agent-hooks" in checks and checks["agent-hooks"]["status"] == "warn"
    assert "shim" in checks["agent-hooks"]["detail"]


def test_is_daemon_cmdline_excludes_shells():
    hv = _loadhv()
    g = hv._is_daemon_cmdline
    # real daemons
    assert g(["/usr/bin/python3", "/home/david/projects/hive-mind/hv", "sync", "daemon"]) is True
    assert g(["python3", "./hv", "sync", "daemon"]) is True
    # shells / scripts that merely MENTION the string must NOT match (so --fix can't kill them)
    assert g(["/bin/bash", "-c", "cd", "x", "&&", "nohup", "./hv", "sync", "daemon", "&&", "rm", "x"]) is False
    assert g(["/bin/bash", "-c", "nohup", "./hv", "sync", "daemon"]) is False   # shell ending in the string
    assert g(["python3", "./hv", "doctor"]) is False
    assert g(["hv", "sync", "now"]) is False
