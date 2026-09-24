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


# ── owner machine: only a PERSON's links are owner-signed (1.23, #114) ─────────────────────────

def test_owner_machine_owner_signs_manual_links_and_they_are_hard(tmp_path):
    run, entries = _owner_hive(tmp_path)
    run("remember", "issue Z is open")
    run("remember", "issue Z is fixed", "--resolves", "1")
    run("decide", "plan A", "--rationale", "r")
    run("decide", "plan B replaces A", "--rationale", "r", "--supersedes", "1")
    links = [e for e in entries() if e["type"] == "link"]
    assert sorted(l["payload"]["kind"] for l in links) == ["resolves", "supersedes"]
    for l in links:
        assert l["payload"]["source"] == "manual"
        assert "owner_sig" in l["payload"] and "owner_pub" in l["payload"]      # a person on the owner machine
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db"); conn.row_factory = sqlite3.Row
    assert {r["authority"] for r in conn.execute("SELECT authority FROM links")} == {"hard"}
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='plan A'").fetchone()[0] is not None


def _all_eight(run, source_args):
    """Write one link of every kind the CLI writes, with the given --source (none = manual). Returns the
    combined confirmation text."""
    out = []
    def sid(r):
        out.append(r.stdout)
        return _first_sid(r.stdout)
    f1 = sid(run("remember", "service X answers on port 443", *source_args))
    sid(run("remember", "port 443 answered a probe at 10:00", *source_args, "--supports", f1))
    sid(run("remember", "port 443 refused a probe at 10:05", *source_args, "--contradicts", f1))
    sid(run("remember", "service X also answers on 8443", *source_args, "--extends", f1))
    sid(run("remember", "service X answers on port 8443 only", *source_args, "--resolves", f1))
    d1 = sid(run("decide", "route X through 443", "--rationale", "r", *source_args))
    sid(run("remember", "routing through 443 worked", *source_args, "--outcome-of", d1))
    sid(run("decide", "route X through 8443", "--rationale", "r", *source_args, "--supersedes", d1,
            "--informed", f1))
    run("entity", "add", "--name", "service-x", "--type", "concept")
    out.append(run("entity", "link", "--name", "service-x", "--fact-id", f1, *source_args).stdout)
    return "\n".join(out)


EIGHT = ["contradicts", "entity", "extends", "informed", "outcome-of", "resolves", "supersedes", "supports"]


def test_an_agent_on_the_owner_machine_writes_device_signed_links_of_all_eight_kinds(tmp_path):
    """#114: an agent source never borrows the owner key, whatever the link kind (outcome-of and informed
    now go through the same builder), and each confirmation says so."""
    run, entries = _owner_hive(tmp_path)
    out = _all_eight(run, ["--source", "claude-code"])
    links = [e for e in entries() if e["type"] == "link"]
    assert sorted({l["payload"]["kind"] for l in links}) == EIGHT
    for l in links:
        assert "owner_sig" not in l["payload"] and "owner_pub" not in l["payload"], l["payload"]["kind"]
        assert l["payload"]["source"] == "claude-code", l["payload"]["kind"]
    # decide and entity link take --source (D-6): the decision is the agent's, not a person's
    assert {e["payload"]["source"] for e in entries() if e["type"] == "decision"} == {"claude-code"}
    assert "owner-signed" not in out
    assert out.count("(device-signed: source claude-code)") == 8


def test_a_person_on_the_owner_machine_writes_owner_signed_links_of_all_eight_kinds(tmp_path):
    run, entries = _owner_hive(tmp_path)
    out = _all_eight(run, [])                                         # no --source: `manual`, a person
    links = [e for e in entries() if e["type"] == "link"]
    assert sorted({l["payload"]["kind"] for l in links}) == EIGHT
    for l in links:
        assert "owner_sig" in l["payload"] and l["payload"]["source"] == "manual", l["payload"]["kind"]
    assert out.count("(owner-signed: source manual)") == 8


def test_a_member_device_never_owner_signs_even_for_manual(tmp_path):
    out = _as(tmp_path, "nodeA", "remember", "the backup ran at 02:00").stdout
    f1 = _first_sid(out)
    out = _as(tmp_path, "nodeA", "remember", "the backup log shows 02:00", "--supports", f1).stdout
    link = next(e for e in _journal(tmp_path) if e["type"] == "link")
    assert "owner_sig" not in link["payload"]
    assert "(device-signed: source manual)" in out


def test_the_builder_signs_only_manual_and_the_projection_follows(tmp_path, monkeypatch):
    """The rule at its source and its §5 consequence: on the owner's machine (device b holds the owner
    seed), an agent's supersedes of device a's decision is evidence, a person's is hard."""
    hv = _loadhv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    monkeypatch.setattr(hv, "_owner_seed", lambda: oseed)            # b is the owner's machine
    old = _decision(hv, a, "plan A", "2026-01-02T00:00:00Z")
    new = _decision(hv, b, "plan B replaces A", "2026-01-03T00:00:00Z")
    ref_new, ref_old = [new["node_id"], new["seq"]], [old["node_id"], old["seq"]]
    agent = hv._link_payload("supersedes", ref_new, ref_old, "claude-code")
    person = hv._link_payload("supersedes", ref_new, ref_old, "manual")
    assert "owner_sig" not in agent and "owner_sig" in person
    for payload, authority in ((agent, "evidence"), (person, "hard")):
        link = _entry(hv, b, "link", dict(payload), "2026-01-03T00:00:01Z")
        conn = _project(hv, tmp_path, base + [old, new, link])
        assert conn.execute("SELECT authority FROM links").fetchone()[0] == authority, payload["source"]


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


# ── 1.22 (#115): `hv remember --extends` — "builds on" without evidence semantics ──────────────────

import re                  # noqa: E402

import pytest              # noqa: E402


def _as(tmp_path, node, *args, check=True):
    """Run `hv` as a given device (HIVE_NODE_ID) in one temp hive; no owner, so devices are principals."""
    env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_NODE_ID=node)
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env, capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _journal(tmp_path):
    out = []
    for f in sorted((tmp_path / "journal").glob("*.jsonl")):
        out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return out


def _db(tmp_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db"); conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _first_sid(out):
    return re.search(r"h:[0-9a-f]{10}", out).group(0)


@pytest.mark.parametrize("channel", ["sense", "introspect"])
@pytest.mark.parametrize("tkind", ["fact", "idea"])
def test_extends_writes_one_link_and_never_moves_the_target(tmp_path, tkind, channel):
    # A target with real evidence on it, so "unchanged" is not trivially 0 → 0.
    if tkind == "fact":
        sid = _first_sid(_as(tmp_path, "nodeA", "remember", "the vendor api paginates at 100 rows").stdout)
        _as(tmp_path, "nodeC", "remember", "the vendor api paginates at 100 rows")        # corroborated
        table, content = "facts", "the vendor api paginates at 100 rows"
    else:
        sid = _first_sid(_as(tmp_path, "nodeA", "propose", "the cache causes the retry storm").stdout)
        _as(tmp_path, "nodeC", "remember", "cache hit rate fell to 3% during the storm", "--supports", sid)
        table, content = "ideas", "the cache causes the retry storm"
    _as(tmp_path, "nodeA", "doctor", "rebuild")

    def state():
        r = _db(tmp_path, f"SELECT confidence, last_evidence_at FROM {table} WHERE content = ?", (content,))[0]
        return round(r["confidence"], 6), r["last_evidence_at"]
    before = state()
    assert before[0] > 0 and before[1]
    target = next(e for e in _journal(tmp_path) if e["type"] == tkind and e["payload"]["content"] == content)

    n = len(_journal(tmp_path))
    args = ["remember", "building on that: the retry budget should follow the page size", "--extends", sid]
    if channel != "sense":
        args += ["--channel", channel]
    r = _as(tmp_path, "nodeB", *args)
    assert f"↗ extends {tkind} {sid}" in r.stdout
    new = _journal(tmp_path)[n:]
    assert [e["type"] for e in new] == ["fact", "link"]                       # the fact + exactly ONE link
    fact, link = new
    assert link["payload"]["kind"] == "extends"
    assert link["payload"]["from_ref"] == [fact["node_id"], fact["seq"]]
    assert link["payload"]["to_ref"] == [target["node_id"], target["seq"]]
    assert link["payload"].get("channel", "sense") == channel               # recorded for readers, never weighed
    assert "polarity" not in link["payload"]["data"]

    assert state() == before                                                  # live path: nothing moved
    rows = _db(tmp_path, "SELECT kind, from_kind, to_kind FROM links WHERE kind = 'extends'")
    assert [tuple(x) for x in rows] == [("extends", "fact", tkind)]
    _as(tmp_path, "nodeA", "doctor", "rebuild")
    assert state() == before                                                  # ...and after a rebuild
    assert len(_db(tmp_path, "SELECT 1 FROM links WHERE kind = 'extends'")) == 1

    out = _as(tmp_path, "nodeA", "search", "retry budget").stdout
    assert f"extends {sid}" in out                                            # text: shown on the row
    js = json.loads(_as(tmp_path, "nodeA", "search", "retry budget", "--format", "json").stdout)
    assert js and not any("extends" in row for row in js)                     # JSON rows: nothing new


def test_extends_accepts_a_decision_target(tmp_path):
    dec = _first_sid(_as(tmp_path, "nodeA", "decide", "ship on friday", "--rationale", "r").stdout)
    before = [tuple(x) for x in _db(tmp_path, "SELECT outcome_score, last_outcome_at FROM decisions")]
    r = _as(tmp_path, "nodeB", "remember", "friday also suits the support rota", "--extends", dec)
    assert f"↗ extends decision {dec}" in r.stdout
    assert [tuple(x) for x in _db(tmp_path, "SELECT kind, to_kind FROM links")] == [("extends", "decision")]
    assert [tuple(x) for x in _db(tmp_path, "SELECT outcome_score, last_outcome_at FROM decisions")] == before


@pytest.mark.parametrize("other", ["--supports", "--contradicts", "--resolves", "--outcome-of"])
def test_extends_is_one_relationship_per_write(tmp_path, other):
    fact = _first_sid(_as(tmp_path, "nodeA", "remember", "ci is green").stdout)
    n = len(_journal(tmp_path))
    r = _as(tmp_path, "nodeB", "remember", "both", "--extends", fact, other, fact, check=False)
    assert r.returncode == 2 and "not allowed with" in r.stderr              # argparse, before any I/O
    assert len(_journal(tmp_path)) == n


def test_extends_unknown_target_writes_nothing(tmp_path):
    _as(tmp_path, "nodeA", "remember", "ci is green")
    n = len(_journal(tmp_path))
    r = _as(tmp_path, "nodeB", "remember", "builds on a ghost", "--extends", "h:0000000000", check=False)
    assert r.returncode == 1 and "--extends" in r.stderr
    assert len(_journal(tmp_path)) == n
