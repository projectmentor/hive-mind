"""1.19 PR2b — the write-path switch (design §2 Back-compat, §9 Version skew; hive #51, plan v2).

`hv decide --supersedes`, `hv remember --resolves` and `hv entity link` now emit ONE `link` entry
instead of their legacy field / entry (`supersedes_ref`, `resolves_ref` + `retract`, `entity_fact`).
Never dual-emitted. On a machine holding the owner key the link payload is owner-signed (hard
everywhere under §5); otherwise device-signed (hard only where this device authored the target).
The legacy resolvers stay forever, so a journal holding both shapes projects both, once each.
`hv doctor` gains the advisory `fleet-contract` check: the merge gate for this PR and the standing
warning for any peer that lands but cannot honour a `link`.

The pure projection cases reuse the in-memory builders from test_links.py; the owner-signed path
drives the real CLI with an initialised owner key.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _decision, _link, _project, _entry, _conf)  # noqa: E402


def _owner_hive(tmp_path):
    """A CLI hive whose device holds the owner key (like test_links.test_config_knob_bounds_via_cli)."""
    def run(*args, check=True):
        env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, r.stderr
        return r

    def entries():
        out = []
        for f in sorted((tmp_path / "journal").glob("*.jsonl")):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out
    run("owner", "init")
    return run, entries


# ── owner machine: links are owner-signed → hard everywhere ──────────────────────────────────────

def test_owner_machine_owner_signs_links_and_they_are_hard(tmp_path):
    run, entries = _owner_hive(tmp_path)
    run("remember", "issue Z is open", "--source", "alice")
    run("remember", "issue Z is fixed", "--resolves", "1", "--source", "alice")
    run("decide", "plan A", "--rationale", "r")
    run("decide", "plan B replaces A", "--rationale", "r", "--supersedes", "1")
    links = [e for e in entries() if e["type"] == "link"]
    assert sorted(l["payload"]["kind"] for l in links) == ["resolves", "supersedes"]
    for l in links:
        assert "owner_sig" in l["payload"] and "owner_pub" in l["payload"]      # owner-signed on the owner machine
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db"); conn.row_factory = sqlite3.Row
    assert {r["authority"] for r in conn.execute("SELECT authority FROM links")} == {"hard"}
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='plan A'").fetchone()[0] is not None
    assert conn.execute("SELECT resolves FROM facts WHERE content='issue Z is fixed'").fetchone()[0] == 1
    assert conn.execute("SELECT confidence FROM facts WHERE content='issue Z is open'").fetchone()[0] <= 0
    # nothing legacy, nothing doubled
    assert not any(e["type"] in ("retract", "entity_fact") for e in entries())
    assert not any("resolves_ref" in e["payload"] or "supersedes_ref" in e["payload"] for e in entries()
                   if e["type"] in ("fact", "decision"))


# ── non-owner, non-author device: evidence only ──────────────────────────────────────────────────

def test_link_from_non_owner_non_author_is_evidence_only_but_still_weighs(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    old = _decision(hv, a, "plan A", "2026-01-02T00:00:00Z")
    new = _decision(hv, b, "plan B replaces A", "2026-01-03T00:00:00Z")
    sup = _link(hv, b, "supersedes", new, old, "2026-01-03T00:00:01Z")            # what b's CLI now writes
    conn = _project(hv, tmp_path, base + [old, new, sup])
    assert conn.execute("SELECT authority FROM links").fetchone()[0] == "evidence"
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='plan A'").fetchone()[0] is None
    f_old = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:02Z")
    f_new = _fact(hv, b, "issue Z is fixed", "2026-01-03T00:00:02Z")
    res = _link(hv, b, "resolves", f_new, f_old, "2026-01-03T00:00:03Z")
    conn = _project(hv, tmp_path, base + [f_old, f_new, res])
    assert conn.execute("SELECT resolves FROM facts WHERE content='issue Z is fixed'").fetchone()[0] is None  # no command
    assert _conf(conn, "issue Z is open") == 0.0                                  # …but one identity's negative evidence


# ── --resolves post-switch equals the legacy retract, numerically ────────────────────────────────

def test_resolves_link_soft_retracts_to_the_same_confidence_as_a_legacy_retract(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    old = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:00Z")
    corro = _fact(hv, c, "issue Z is open", "2026-01-02T00:00:01Z")               # 2 identities → 0.675
    # legacy shape: fact with resolves_ref + a retract by the same device
    legacy_new = _entry(hv, b, "fact", {"content": "issue Z is fixed", "tags": [], "importance": 0.5,
                                        "source": "manual", "resolves_ref": [old["node_id"], old["seq"]]},
                        "2026-01-03T00:00:00Z")
    legacy_ret = _entry(hv, b, "retract", {"retracts_ref": [old["node_id"], old["seq"]], "reason": "resolved",
                                           "source": "manual"}, "2026-01-03T00:00:00Z")
    h1 = _loadhv(tmp_path / "legacy", monkeypatch)
    c1 = _project(h1, tmp_path / "legacy", base + [old, corro, legacy_new, legacy_ret])
    legacy_conf = _conf(c1, "issue Z is open")
    # new shape: fact + ONE resolves link from the same device
    new_fact = _fact(hv, b, "issue Z is fixed", "2026-01-03T00:00:00Z")
    res = _link(hv, b, "resolves", new_fact, old, "2026-01-03T00:00:00Z")
    h2 = _loadhv(tmp_path / "new", monkeypatch)
    c2 = _project(h2, tmp_path / "new", base + [old, corro, new_fact, res])
    assert _conf(c2, "issue Z is open") == legacy_conf == hv._confidence_for(2.0 - 1.0)
    assert c1.execute("SELECT contested FROM facts WHERE content='issue Z is open'").fetchone()[0] == 1
    assert c2.execute("SELECT contested FROM facts WHERE content='issue Z is open'").fetchone()[0] == 1


# ── both shapes in one journal project both, once each ───────────────────────────────────────────

def test_legacy_and_link_supersedes_coexist_and_project_once_each(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, _b, _c), base = _owned_hive(hv)
    d1 = _decision(hv, a, "first", "2026-01-02T00:00:00Z")
    d2 = _entry(hv, a, "decision", {"content": "second (legacy supersede)", "rationale": "", "tags": [],
                                    "source": "manual", "supersedes_ref": [d1["node_id"], d1["seq"]]},
                "2026-01-03T00:00:00Z")
    d3 = _decision(hv, a, "third", "2026-01-04T00:00:00Z")
    d4 = _decision(hv, a, "fourth (link supersede)", "2026-01-05T00:00:00Z")
    sup = _link(hv, a, "supersedes", d4, d3, "2026-01-05T00:00:01Z")
    conn = _project(hv, tmp_path, base + [d1, d2, d3, d4, sup])
    rows = {r[0]: r[1] for r in conn.execute("SELECT content, superseded_by FROM decisions")}
    ids = {r[0]: r[1] for r in conn.execute("SELECT content, id FROM decisions")}
    assert rows["first"] == ids["second (legacy supersede)"] and rows["third"] == ids["fourth (link supersede)"]
    assert rows["second (legacy supersede)"] is None and rows["fourth (link supersede)"] is None
    assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 1          # only the link shape makes an edge row


# ── fleet-contract: the merge gate ───────────────────────────────────────────────────────────────

def test_fleet_contract_lists_behind_and_unverified_peers_and_is_empty_when_all_current(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    gov = hv._governance_state(base)
    probed = {a["id"]: ("10.0.0.1:9876", "in sync", "a"), b["id"]: ("10.0.0.2:9876", "in sync", "b")}
    # a is on 1.18, b on a pre-PR2b build (no contract advertised), c is unreachable
    fc = hv._fleet_contract(gov, probed, {a["id"]: "1.18"})
    assert [d for d, _c in fc["behind"]] == sorted([a["id"], b["id"]]) and fc["unverified"] == [c["id"]] and fc["ok"] == []
    assert dict(fc["behind"])[b["id"]].startswith("unknown")
    # everyone current and reachable → nothing to report (1.20 ≥ 1.19; 1.19 exactly is enough)
    probed[c["id"]] = ("10.0.0.3:9876", "in sync", "c")
    fc = hv._fleet_contract(gov, probed, {a["id"]: "1.20", b["id"]: "1.19", c["id"]: "2.0"})
    assert fc["behind"] == [] and fc["unverified"] == [] and len(fc["ok"]) == 3
    # a purged device is not part of the fleet
    gov2 = dict(gov); gov2["purged"] = {c["id"]}
    assert hv._fleet_contract(gov2, {}, {})["unverified"] == sorted([a["id"], b["id"]])
    # the doctor surfaces it (advisory warn), without a network, and stays silent when clean
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    _project(hv, tmp_path, base)
    monkeypatch.setattr(hv, "_probe_peer_map_cached", lambda ttl=5: {a["id"]: ("x", "in sync", "a")})
    hv._PEER_CONTRACTS.clear(); hv._PEER_CONTRACTS[a["id"]] = "1.18"
    import io, contextlib, argparse
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            hv.doctor_cmd(argparse.Namespace(format="json", fix=False, dry_run=False, doctor_action=None))
        except SystemExit:
            pass                                       # a failing verdict exits non-zero; the JSON is printed first
    checks = {c_["name"]: c_ for c_ in json.loads(buf.getvalue())["checks"]}
    assert checks["fleet-contract"]["status"] == "warn"
    assert "1 peer(s) below contract 1.19" in checks["fleet-contract"]["detail"]
    assert "2 unverifiable" in checks["fleet-contract"]["detail"]
    daemon = (PROJECT / "hive_sync_daemon.py").read_text()
    assert daemon.count('"contract": hv.CONTRACT_VERSION') == 2                   # advertised on /hive/info and /sync/hello


# ── two-node differential ────────────────────────────────────────────────────────────────────────

def test_two_node_differential_for_the_link_write_path(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path / "n1", monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    d1 = _decision(hv, a, "first", "2026-01-02T00:00:00Z")
    d2 = _decision(hv, b, "second", "2026-01-03T00:00:00Z")
    sup = _link(hv, b, "supersedes", d2, d1, "2026-01-03T00:00:01Z", owner=(oseed, opub))   # owner machine
    f1 = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:02Z")
    f2 = _fact(hv, b, "issue Z is fixed", "2026-01-03T00:00:02Z")
    res = _link(hv, b, "resolves", f2, f1, "2026-01-03T00:00:03Z", owner=(oseed, opub))
    ent = _entry(hv, a, "entity", {"name": "Z", "type": "project", "attributes": {}}, "2026-01-02T00:00:03Z")
    el = _link(hv, a, "entity", ent, f2, "2026-01-03T00:00:04Z", data={"confidence": 0.9})
    entries = base + [d1, d2, sup, f1, f2, res, ent, el]

    def snap(home, order):
        h = _loadhv(home, monkeypatch)
        conn = _project(h, home, list(order))
        return ([tuple(r) for r in conn.execute("SELECT content, superseded_by IS NOT NULL FROM decisions ORDER BY content")],
                [tuple(r) for r in conn.execute("SELECT content, round(confidence,6), resolves IS NOT NULL FROM facts ORDER BY content")],
                [tuple(r) for r in conn.execute("SELECT kind, authority FROM links ORDER BY kind")],
                conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0])

    s1, s2 = snap(tmp_path / "n1", entries), snap(tmp_path / "n2", reversed(entries))
    assert s1 == s2
    assert s1[2] == [("entity", "evidence"), ("resolves", "hard"), ("supersedes", "hard")] and s1[3] == 1
