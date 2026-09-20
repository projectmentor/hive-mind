"""1.19 PR3b — stable short ids (hive #59; closes #58).

`sid = "h:" + sha256("node_id:seq")[:10]` is derived purely from journal identity, so it is the same on
every node and never changes on a rebuild — unlike the SQLite rowid it is shown beside. It is SHOWN
everywhere a rowid is shown (search text/JSON, write confirmations, audit, the open-ideas digest, the
dashboard API) and ACCEPTED everywhere an id is accepted (`--informed`, `--outcome-of`, `--supersedes`,
`--resolves`, `hv retract`, `hv entity link`). Resolution is EXACT (an equality lookup on the
projection-written `journal_index.sid`), never a prefix match. Bare integers still work but print one
deprecation warning per process; they are removed at the next MAJOR. Rowids are NOT removed from
store.db — they remain its internal keys.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _decision, _project, _device)  # noqa: E402

WARN = "bare local ids drift across rebuilds"


def _json(hive, *args):
    return json.loads(hive.run("search", *args, "--format", "json").stdout)


def _sid(node_id, seq):
    return "h:" + hashlib.sha256(f"{node_id}:{seq}".encode()).hexdigest()[:10]


def _seed(hive):
    hive.run("remember", "the deploy succeeded at commit abc123", "--source", "alice")
    hive.run("remember", "the deploy took eleven minutes", "--source", "alice")
    hive.run("decide", "base decision about deploys", "--rationale", "r")
    facts = {r["content"]: r for r in _json(hive, "deploy") if r["kind"] == "fact"}
    dec = [r for r in _json(hive, "base decision") if r["kind"] == "decision"][0]
    return facts, dec


# ── derivation + display ──────────────────────────────────────────────────────────────────────────

def test_sid_is_derived_from_journal_identity_and_shown_everywhere(hive):
    r = hive.run("remember", "the widget is broken", "--source", "alice")
    e = [x for x in hive.entries() if x["type"] == "fact"][0]
    sid = _sid(e["node_id"], e["seq"])
    assert r.stdout.startswith(f"Remembered as {sid} (fact #1")
    # projected column, search text + JSON, propose/decide confirmations, open-ideas digest
    assert hive.query("SELECT sid FROM journal_index WHERE seq = ?", (e["seq"],))[0]["sid"] == sid
    row = [x for x in _json(hive, "widget") if x["kind"] == "fact"][0]
    assert row["sid"] == sid and row["ref"] == f"{e['node_id']}:{e['seq']}" and row["id"] == 1
    assert f"[{sid} · 1] the widget is broken" in hive.run("search", "widget").stdout
    d = hive.run("decide", "replace the widget", "--rationale", "r")
    de = [x for x in hive.entries() if x["type"] == "decision"][0]
    assert d.stdout.startswith(f"Decided as {_sid(de['node_id'], de['seq'])} (decision #1)")
    p = hive.run("propose", "the widget fails under load")
    ie = [x for x in hive.entries() if x["type"] == "idea"][0]
    assert f"Proposed idea i1 {_sid(ie['node_id'], ie['seq'])}" in p.stdout
    out = hive.run("nudge", "--event", "session-start", "--cwd", str(hive.home)).stdout
    assert _sid(ie["node_id"], ie["seq"]) in out
    js = _json(hive, "widget", "--kind", "decision")
    assert js[0]["sid"] == _sid(de["node_id"], de["seq"])
    assert _json(hive, "widget", "--kind", "idea")[0]["sid"] == _sid(ie["node_id"], ie["seq"])


def test_corroborated_fact_has_one_canonical_sid_but_every_entry_resolves(hive):
    hive.run("remember", "the deploy succeeded", "--source", "alice")
    r2 = hive.run("remember", "the deploy succeeded", "--source", "bob")
    es = [x for x in hive.entries() if x["type"] == "fact"]
    first, second = (_sid(e["node_id"], e["seq"]) for e in sorted(es, key=lambda e: (e["node_id"], e["seq"])))
    assert r2.stdout.startswith(f"Corroborated {first} (fact #1")            # the row's ONE name
    assert [x["sid"] for x in _json(hive, "deploy")] == [first]
    # …but the corroborating entry's own sid names the same row
    assert hive.run("decide", "x", "--informed", second).returncode == 0
    d = [e for e in hive.entries() if e["type"] == "decision"][0]
    assert d["payload"]["informed_by"] == [[es[1]["node_id"], es[1]["seq"]]] or \
        d["payload"]["informed_by"][0][1] in {e["seq"] for e in es}


# ── differential: identical on two nodes whose rowids differ ──────────────────────────────────────

def test_sid_agrees_across_nodes_with_divergent_rowids(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path / "n1", monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    fa = _fact(hv, a, "a's observation", "2026-01-02T00:00:00Z")
    fb = _fact(hv, b, "b's observation", "2026-01-02T00:00:01Z")
    da = _decision(hv, a, "a's call", "2026-01-03T00:00:00Z")
    entries = base + [fa, fb, da]

    def snap(home, order):
        h = _loadhv(home, monkeypatch)
        conn = _project(h, home, list(order))
        return {r["content"]: (r["id"], r["sid"]) for r in conn.execute(
            "SELECT f.content, f.id, j.sid FROM facts f JOIN journal_index j ON j.kind='fact' AND j.local_id=f.id")}, \
               {r["sid"]: (r["kind"], r["node_id"], r["seq"]) for r in conn.execute("SELECT * FROM journal_index")}, h, conn

    s1, j1, h1, c1 = snap(tmp_path / "n1", entries)
    # node 2 saw only b's fact first, then the rest: b's fact takes rowid 1 there
    s2, j2, h2, c2 = snap(tmp_path / "n2", [fb] + base + [fa, da])
    # (rebuild orders by node_id, seq, so rowids may or may not differ — force divergence by a live write)
    assert {k: v[1] for k, v in s1.items()} == {k: v[1] for k, v in s2.items()}     # sids agree
    assert set(j1) == set(j2) and all(j1[k] == j2[k] for k in j1)                  # same identity per sid
    # the same sid resolves to the same CONTENT on both nodes through the kind-checked parser
    sid = _sid(fb["node_id"], fb["seq"])
    for h, c in ((h1, c1), (h2, c2)):
        kind, ref = h._parse_ref_arg(c, sid)
        assert kind == "fact" and ref == [fb["node_id"], fb["seq"]]
    assert hashlib.sha256(f"{fb['node_id']}:{fb['seq']}".encode()).hexdigest()[:10] == sid[2:]


# ── acceptance: every verb, three forms, byte-identical entries; one deprecation warning ──────────

def test_every_accepting_verb_takes_sid_ref_and_bare_id_identically(hive):
    facts, dec = _seed(hive)
    f1 = facts["the deploy succeeded at commit abc123"]
    f2 = facts["the deploy took eleven minutes"]

    def payloads_after(fn):
        before = len(hive.entries())
        r = fn()
        new = hive.entries()[before:]
        # drop what legitimately differs per run: source, and the writer's OWN identity (from_ref)
        return r, [(e["type"], {k: v for k, v in e["payload"].items() if k not in ("source", "from_ref")}) for e in new]

    # --informed: sid / ref / bare — same informed_by + same link targets
    outs = [payloads_after(lambda t=t: hive.run("decide", "ship", "--rationale", "r", "--informed", t))
            for t in (f1["sid"], f1["ref"], str(f1["id"]))]
    assert outs[0][1] == outs[1][1] == outs[2][1]
    assert WARN not in outs[0][0].stderr and WARN not in outs[1][0].stderr and outs[2][0].stderr.count(WARN) == 1
    # --outcome-of
    outs = [payloads_after(lambda t=t: hive.run("remember", f"it worked {i}", "--outcome-of", t, "--source", "alice"))
            for i, t in enumerate((dec["sid"], dec["ref"], f"d{dec['id']}"))]
    strip = lambda pl: [(t, {k: v for k, v in p.items() if k != "content"}) for t, p in pl]
    assert strip(outs[0][1]) == strip(outs[1][1]) == strip(outs[2][1])
    assert outs[2][0].stderr.count(WARN) == 1 and WARN not in outs[0][0].stderr
    # --supersedes
    outs = [payloads_after(lambda t=t: hive.run("decide", "v2", "--rationale", "r", "--supersedes", t))
            for t in (dec["sid"], dec["ref"], str(dec["id"]))]
    assert outs[0][1] == outs[1][1] == outs[2][1]
    sup = [pl for t, pl in outs[0][1] if t == "link" and pl["kind"] == "supersedes"]      # 1.19 PR2b: a link, not a field
    assert len(sup) == 1 and sup[0]["to_ref"] == [dec["ref"].rpartition(":")[0], int(dec["ref"].rpartition(":")[2])]
    # entity link --fact-id
    hive.run("entity", "add", "--name", "Deploys", "--type", "project")
    outs = [payloads_after(lambda t=t: hive.run("entity", "link", "--name", "Deploys", "--fact-id", t))
            for t in (f2["sid"], f2["ref"], str(f2["id"]))]
    assert outs[0][1] == outs[1][1] == outs[2][1]
    assert outs[0][1][0][0] == "link" and outs[0][1][0][1]["kind"] == "entity"          # 1.19 PR2b: an `entity` link
    assert f"Linked fact {f2['sid']}" in outs[0][0].stdout
    # two bare ids in ONE command → the warning prints exactly once
    r = hive.run("decide", "twice", "--rationale", "r", "--informed", str(f1["id"]), str(f2["id"]))
    assert r.stderr.count(WARN) == 1


def test_resolves_accepts_sid_and_kind_mismatch_aborts(hive):
    facts, dec = _seed(hive)
    f1 = facts["the deploy succeeded at commit abc123"]
    r = hive.run("remember", "the deploy actually failed", "--resolves", f1["sid"], "--source", "bob")
    assert f"resolved fact {f1['sid']}" in r.stdout and f"`hv retract {f1['sid']} --owner`" in r.stdout
    assert hive.query("SELECT confidence FROM facts WHERE id = ?", (f1["id"],))[0]["confidence"] <= 0
    # a decision named as the resolve target is an error, not a silent no-link write
    before = len(hive.entries())
    r = hive.run("remember", "nope", "--resolves", dec["sid"], "--source", "bob", check=False)
    assert r.returncode != 0 and "is a decision, not a fact" in r.stderr and len(hive.entries()) == before
    # an unresolvable target keeps the documented behaviour: warning, fact still written, no link
    r = hive.run("remember", "still written", "--resolves", "h:0000000000", "--source", "bob")
    assert "writing the fact without a resolve link" in r.stdout
    assert hive.query("SELECT resolves FROM facts WHERE content='still written'")[0]["resolves"] is None


# ── retract (closes #58): the right fact after renumbering; kind-checked ──────────────────────────

def test_retract_by_sid_survives_renumbering_and_bare_id_is_kind_checked(hive):
    hive.run("remember", "zeta claim", "--source", "alice")            # rowid 1 on this node, for now
    zeta = [r for r in _json(hive, "zeta") if r["kind"] == "fact"][0]
    hive.run("decide", "a decision", "--rationale", "r")
    # Simulate a sync having landed entries from a node whose id sorts EARLIER (a synced journal file —
    # ingest verification is tested elsewhere; the rebuild orders by node_id, seq and renumbers zeta).
    peer_lines = [json.dumps({"type": "fact", "node_id": "0000-earlier-node", "seq": i + 1,
                              "timestamp": f"2026-01-0{i + 1}T00:00:00Z",
                              "payload": {"content": f"peer claim {i}", "tags": [], "source": "manual"}})
                  for i in range(3)]
    (hive.journal / "0000-earlier-node.jsonl").write_text("\n".join(peer_lines) + "\n")
    hive.run("doctor", "rebuild")
    new_id = hive.query("SELECT id FROM facts WHERE content='zeta claim'")[0]["id"]
    assert new_id != zeta["id"]                                          # renumbered: the #58 hazard
    assert hive.query("SELECT content FROM facts WHERE id = ?", (zeta["id"],))[0]["content"] != "zeta claim"
    # by sid → the RIGHT fact; by the stale bare id → a DIFFERENT fact (that is the hazard, now warned)
    r = hive.run("retract", zeta["sid"], "--source", "bob")
    assert f"Fact {zeta['sid']} (#{new_id}) retracted" in r.stdout
    assert hive.query("SELECT confidence FROM facts WHERE content='zeta claim'")[0]["confidence"] <= 0
    # kind check: a decision id given to retract aborts, nothing journaled
    before = len(hive.entries())
    r = hive.run("retract", "d1", check=False)
    assert r.returncode != 0 and "is a decision, not a fact" in r.stderr and len(hive.entries()) == before
    r = hive.run("retract", "h:0000000000", check=False)
    assert r.returncode != 0 and "does not resolve" in r.stderr
    # MCP forwards the sid string unchanged (source-level; the live tool needs the `mcp` package)
    src = (PROJECT / "integrations" / "mcp" / "hive_mcp.py").read_text()
    assert "def hive_retract(fact_id: str" in src and '["retract", str(fact_id).strip()' in src
    assert "fact_id: str | int | None = None" in src


# ── audit: CONTRAVENED parses h: exactly; sids on every audit line ────────────────────────────────

def test_audit_contravened_parses_sid_exactly_and_lines_carry_sid(hive):
    hive.run("remember", "the widget is broken", "--source", "alice")
    w = [r for r in _json(hive, "widget") if r["kind"] == "fact"][0]
    hive.run("remember", f"the widget is healed, resolves {w['sid']}", "--source", "alice")
    data = json.loads(hive.run("audit", "--format", "json").stdout)
    hit = [x for x in data["contravened"] if x["references"] == w["id"]]
    assert hit and hit[0]["exact"] is True and hit[0]["references_sid"] == w["sid"] and hit[0]["sid"]
    txt = hive.run("audit").stdout
    assert f"names fact {w['sid']} (#{w['id']})" in txt and "best-effort" not in txt
    assert f"--resolves {w['sid']}" in txt and f"hv retract {w['sid']}" in txt
    # a prose #N is still parsed, flagged best-effort
    hive.run("remember", "another correction, supersedes #1", "--source", "alice")
    data = json.loads(hive.run("audit", "--format", "json").stdout)
    assert any(x["exact"] is False and x["references"] == 1 for x in data["contravened"])
    assert "best-effort" in hive.run("audit").stdout
    # reconciling by sid clears it
    hive.run("remember", "the widget is fine now", "--resolves", w["sid"], "--source", "alice")
    data = json.loads(hive.run("audit", "--format", "json").stdout)
    assert not any(x["references"] == w["id"] for x in data["contravened"])
    assert all("sid" in x for x in data["obsolete"]["contested"] + data["obsolete"]["forgotten"] + data["recheck"])


# ── dashboard API: sid on rows, /api/item resolves a deep link ────────────────────────────────────

def test_api_search_rows_carry_sid_and_api_item_resolves_it(hive, monkeypatch):
    facts, dec = _seed(hive)
    hv = _loadhv(hive.home, monkeypatch)
    monkeypatch.setenv("HIVE_HOME", str(hive.home))
    s = hv.api_search("deploy")
    assert all(f["sid"] and f["ref"] for f in s["facts"]) and all(d["sid"] for d in s["decisions"])
    f1 = facts["the deploy succeeded at commit abc123"]
    it = hv.api_item(f1["sid"])
    assert it["kind"] == "fact" and it["id"] == f1["id"] and it["item"]["content"] == f1["content"] and it["item"]["sid"] == f1["sid"]
    it = hv.api_item(dec["ref"])                                         # a raw ref resolves too
    assert it["kind"] == "decision" and it["item"]["content"] == "base decision about deploys"
    assert "error" in hv.api_item("h:0000000000") and "error" in hv.api_item("garbage")
    html = (PROJECT / "dashboard" / "index.html").read_text()
    assert "/api/item?sid=" in html and "hashchange" in html and "history.replaceState" in html
    daemon = (PROJECT / "hive_sync_daemon.py").read_text()
    assert '"/api/item"' in daemon and "hv.api_item(" in daemon


def test_sid_column_is_migrated_into_an_existing_store(hive):
    hive.run("remember", "pre-existing row", "--source", "alice")
    import sqlite3
    conn = sqlite3.connect(hive.db)
    conn.executescript("DROP INDEX IF EXISTS idx_jidx_sid; "
                       "CREATE TABLE ji2 AS SELECT node_id, seq, kind, local_id FROM journal_index; "
                       "DROP TABLE journal_index; ALTER TABLE ji2 RENAME TO journal_index;")
    conn.commit(); conn.close()
    assert "sid" not in {r[1] for r in hive.query("PRAGMA table_info(journal_index)")}
    hive.run("stats")                                                    # any command → init_db migrates
    assert "sid" in {r[1] for r in hive.query("PRAGMA table_info(journal_index)")}
    hive.run("doctor", "rebuild")
    e = [x for x in hive.entries() if x["type"] == "fact"][0]
    assert hive.query("SELECT sid FROM journal_index")[0]["sid"] == _sid(e["node_id"], e["seq"])
