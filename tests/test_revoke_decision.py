"""#45 (contract 1.24): `hv decide --revoke <sid> --rationale "<why>"` withdraws a decision that was
wrong, with no replacement.

It writes one decision (content `Revoke <sid>: <the target's first line>` unless given, tag `revocation`,
payload `revokes: <ref>`) and ONE `supersedes` link to the target through the #114 builder. No new
projection: the target's `superseded_by` is set exactly as by `--supersedes`, and only when the link is
`hard` (§5). An evidence revoke is recorded, and the confirmation says it is not in effect (R4).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_link_write_path import _owner_hive, _as, _journal, _db, _first_sid  # noqa: E402


def _payloads(entries, etype):
    return [e["payload"] for e in entries if e["type"] == etype]


def test_revoke_writes_one_revocation_decision_and_one_supersedes_link(tmp_path):
    sid = _first_sid(_as(tmp_path, "nodeA", "decide", "ship on fridays\nsecond line", "--rationale", "r").stdout)
    before = len(_journal(tmp_path))
    r = _as(tmp_path, "nodeA", "decide", "--revoke", sid, "--rationale", "fridays broke prod",
            "--tags", "ops", "--source", "claude-code")
    new = _journal(tmp_path)[before:]
    assert sorted(e["type"] for e in new) == ["decision", "link"]          # nothing else, never dual-emitted
    dec = _payloads(new, "decision")[0]
    assert dec["content"] == f"Revoke {sid}: ship on fridays"
    assert dec["tags"] == ["ops", "revocation"]
    assert dec["rationale"] == "fridays broke prod" and dec["source"] == "claude-code"
    target = next(e for e in _journal(tmp_path) if e["type"] == "decision")
    assert dec["revokes"] == [target["node_id"], target["seq"]]
    link = _payloads(new, "link")[0]
    assert link["kind"] == "supersedes" and link["to_ref"] == dec["revokes"]
    # the device that wrote the target revokes it: hard, in effect
    assert r.stdout.startswith(f'Revoked decision {sid} ("ship on fridays") (device-signed: source claude-code)')
    rows = {x["content"]: x["superseded_by"] for x in _db(tmp_path, "SELECT content, superseded_by FROM decisions")}
    assert rows["ship on fridays\nsecond line"] is not None
    assert "SUPERSEDED" in _as(tmp_path, "nodeA", "search", "fridays").stdout


def test_given_content_is_kept(tmp_path):
    sid = _first_sid(_as(tmp_path, "nodeA", "decide", "use vendor X", "--rationale", "r").stdout)
    _as(tmp_path, "nodeA", "decide", "Vendor X is withdrawn: its licence changed", "--revoke", sid, "--rationale", "r")
    assert _payloads(_journal(tmp_path), "decision")[-1]["content"] == "Vendor X is withdrawn: its licence changed"


def test_revoke_is_superseded_after_rebuild_and_on_a_second_node(tmp_path):
    a = tmp_path / "a"
    sid = _first_sid(_as(a, "nodeA", "decide", "cache everything", "--rationale", "r").stdout)
    _as(a, "nodeA", "decide", "--revoke", sid, "--rationale", "stale reads")
    _as(a, "nodeA", "doctor", "rebuild")
    b = tmp_path / "b"
    shutil.copytree(a / "journal", b / "journal")                 # a second node holding the same journal
    _as(b, "nodeB", "doctor", "rebuild")
    for home in (a, b):
        row = _db(home, "SELECT superseded_by FROM decisions WHERE content = 'cache everything'")[0]
        assert row["superseded_by"] is not None, home


def test_refusals_write_nothing(tmp_path):
    fact = _first_sid(_as(tmp_path, "nodeA", "remember", "the api paginates at 100").stdout)
    dsid = _first_sid(_as(tmp_path, "nodeA", "decide", "page at 100", "--rationale", "r").stdout)
    n = len(_journal(tmp_path))
    cases = [
        (("decide", "--revoke", fact, "--rationale", "r"), 1, "is a fact, not a decision"),
        (("decide", "--revoke", dsid), 1, "--rationale is required"),
        (("decide", "--revoke", dsid, "--rationale", "   "), 1, "--rationale is required"),
        (("decide", "--revoke", dsid, "--supersedes", dsid, "--rationale", "r"), 2, "not allowed with"),
        (("decide", "--revoke", "h:0000000000", "--rationale", "r"), 1, "does not resolve"),
        (("decide", "--rationale", "r"), 2, "content is required"),
    ]
    for args, code, msg in cases:
        r = _as(tmp_path, "nodeA", *args, check=False)
        assert r.returncode == code, (args, r.stderr)
        assert msg in r.stderr, (args, r.stderr)
    assert len(_journal(tmp_path)) == n                             # every refusal aborts before any write


def test_an_evidence_revoke_is_recorded_but_not_in_effect(tmp_path):
    """R4: a revoke from a device that neither holds the owner key nor wrote the target weighs only. The
    confirmation says so, the target is not superseded, and `hv doctor` lists it under link-authz."""
    sid = _first_sid(_as(tmp_path, "nodeA", "decide", "deploy from laptops", "--rationale", "r").stdout)
    r = _as(tmp_path, "nodeB", "decide", "--revoke", sid, "--rationale", "no", "--source", "claude-code")
    assert r.stdout.startswith(f'Revoke of decision {sid} ("deploy from laptops") recorded '
                               f'(device-signed: source claude-code); NOT in effect')
    assert f"hv decide --revoke {sid}" in r.stdout
    row = _db(tmp_path, "SELECT superseded_by FROM decisions WHERE content = 'deploy from laptops'")[0]
    assert row["superseded_by"] is None
    assert "SUPERSEDED" not in _as(tmp_path, "nodeB", "search", "laptops").stdout.split("Revoke")[0]
    auth = {x["authority"] for x in _db(tmp_path, "SELECT authority FROM links WHERE kind = 'supersedes'")}
    assert auth == {"evidence"}
    doc = _as(tmp_path, "nodeB", "doctor", check=False).stdout
    assert "link-authz" in doc and "supersedes nodeB:" in doc


def test_on_the_owner_machine_a_person_revokes_with_owner_authority(tmp_path):
    run, entries = _owner_hive(tmp_path)
    sid = _first_sid(run("decide", "plan A", "--rationale", "r").stdout)
    r = run("decide", "--revoke", sid, "--rationale", "wrong")
    assert r.stdout.startswith(f'Revoked decision {sid} ("plan A") (owner-signed: source manual)')
    link = [e["payload"] for e in entries() if e["type"] == "link"][-1]
    assert link["kind"] == "supersedes" and "owner_sig" in link and link["source"] == "manual"


def test_on_the_owner_machine_an_agent_revoke_is_device_signed_and_says_what_the_projection_does(tmp_path):
    """#114: an agent never borrows the owner key. Whether its revoke is in effect is then the projection's
    call (§5), and the confirmation must say exactly that: it is derived from the link's authority, never
    from --source. (This fixture's device writes unsigned entries, so here the link is evidence.)"""
    run, entries = _owner_hive(tmp_path)
    sid = _first_sid(run("decide", "plan A", "--rationale", "r", "--source", "claude-code").stdout)
    r = run("decide", "--revoke", sid, "--rationale", "wrong", "--source", "claude-code")
    link = [e["payload"] for e in entries() if e["type"] == "link"][-1]
    assert "owner_sig" not in link and link["source"] == "claude-code"
    assert "(device-signed: source claude-code)" in r.stdout.splitlines()[0]
    authority = _db(tmp_path, "SELECT authority FROM links WHERE kind = 'supersedes'")[0]["authority"]
    superseded = _db(tmp_path, "SELECT superseded_by FROM decisions WHERE content = 'plan A'")[0]["superseded_by"]
    in_effect = r.stdout.startswith("Revoked decision")
    assert in_effect == (authority == "hard") == (superseded is not None)
    assert in_effect or "NOT in effect" in r.stdout


def test_sid_and_ref_inputs_journal_identical_payloads(tmp_path):
    out = []
    for home, form in ((tmp_path / "sid", "sid"), (tmp_path / "ref", "ref")):
        r = _as(home, "nodeA", "decide", "one region only", "--rationale", "r").stdout
        token = _first_sid(r) if form == "sid" else r.split("ref: ")[1].split()[0]
        _as(home, "nodeA", "decide", "--revoke", token, "--rationale", "latency", "--source", "claude-code")
        new = _journal(home)[1:]
        out.append([(e["type"], json.dumps(e["payload"], sort_keys=True)) for e in new])
    assert out[0] == out[1]


def test_hermes_decide_passes_revoke_and_refuses_two_relationships(monkeypatch):
    from test_hermes_plugin import hermes_plugin          # loaded there with the Hermes runtime stubbed
    calls = []
    monkeypatch.setattr(hermes_plugin, "_hv", lambda *a, **k: (calls.append(a), (True, "ok"))[1])
    monkeypatch.setattr(hermes_plugin, "_hv_telemetry", lambda *a, **k: None)
    p = hermes_plugin.HiveMindMemoryProvider()
    p.initialize("abcdef1234567890", agent_context="primary", agent_identity="coder")
    out = json.loads(p.handle_tool_call("hive_decide", {"revoke": "h:0123456789", "rationale": "wrong"}))
    assert out["ok"] is True
    assert calls[-1] == ("decide", "--rationale", "wrong", "--revoke", "h:0123456789")
    p.handle_tool_call("hive_decide", {"content": "B", "rationale": "r", "supersedes": "h:0123456789"})
    assert calls[-1] == ("decide", "B", "--rationale", "r", "--supersedes", "h:0123456789")
    n = len(calls)
    out = json.loads(p.handle_tool_call("hive_decide", {"content": "x", "supersedes": "h:0123456789",
                                                        "revoke": "h:9876543210"}))
    assert out["ok"] is False and "one relationship per write" in out["output"]
    out = json.loads(p.handle_tool_call("hive_decide", {"rationale": "r"}))
    assert out["ok"] is False and "optional only with revoke" in out["output"]
    assert len(calls) == n
