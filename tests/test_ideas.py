"""Contract 1.20 — the `idea` journal type (design §6, §0.1; hive #54, plan v2).

An idea is a hypothesis whose confidence is EARNED, never asserted: it starts at 0.0 and moves only
via `supports`/`contradicts` links from other identities on the `sense` channel. Identity is the
journal entry, not the text — a restated idea is a second idea, never corroboration, and an idea
never corroborates a fact. Under `hv search` (kind=all) an idea surfaces only once it has earned
confidence; `--kind idea` lists them all. A peer's idea landing on ingest emits an `idea-arrived`
line to the local bus log; the session-start digest lists open ideas.

CLI-level cases use the shared `hive` fixture; projection numbers reuse the in-memory entry builders
from test_links.py against the real `rebuild_db`.
"""

import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _link, _project, _entry, _device, _gov)  # noqa: E402


def _json(hive, *args):
    return json.loads(hive.run("search", *args, "--format", "json").stdout)


def _idea(hv, dev, content, ts, channel="introspect", source="manual"):
    p = {"content": content, "tags": [], "source": source}
    if channel:
        p["channel"] = channel
    return _entry(hv, dev, "idea", p, ts)


# ── CLI surface ──────────────────────────────────────────────────────────────────────────────────

def test_propose_journals_an_introspect_idea_at_zero_confidence(hive):
    r = hive.run("propose", "the macos runner is slow because of disk io", "--tags", "ci", "--source", "alice")
    assert "Proposed idea i1" in r.stdout and "confidence 0.00" in r.stdout and "ref:" in r.stdout
    e = [x for x in hive.entries() if x["type"] == "idea"][0]
    assert e["payload"]["channel"] == "introspect" and e["payload"]["tags"] == ["ci"]
    row = hive.query("SELECT confidence, contested, last_evidence_at FROM ideas")[0]
    assert row["confidence"] == 0.0 and row["contested"] == 0 and row["last_evidence_at"] is None
    assert hive.query("SELECT kind FROM journal_index")[0]["kind"] == "idea"
    hive.run("doctor", "rebuild")
    assert hive.query("SELECT count(*) c FROM ideas")[0]["c"] == 1


def test_restated_idea_is_a_second_idea_and_never_corroborates_a_fact(hive):
    hive.run("propose", "the macos runner is slow because of disk io", "--source", "alice")
    hive.run("propose", "the macos runner is slow because of disk io", "--source", "bob")
    assert hive.query("SELECT count(*) c FROM ideas")[0]["c"] == 2          # two rows, no de-dup
    assert all(r["confidence"] == 0.0 for r in hive.query("SELECT confidence FROM ideas"))
    # a fact with identical text is NOT corroborated by the two ideas
    hive.run("remember", "the macos runner is slow because of disk io", "--source", "carol")
    f = [r for r in _json(hive, "macos runner") if r["kind"] == "fact"][0]
    assert round(f["confidence"], 4) == 0.45                                  # one identity, not three


def test_search_hides_raw_ideas_under_all_and_lists_them_under_kind_idea(hive):
    hive.run("propose", "the macos runner is slow because of disk io", "--source", "alice")
    hive.run("remember", "macos test job took 32 minutes", "--source", "alice")
    assert [r["kind"] for r in _json(hive, "macos")] == ["fact"]             # raw idea hidden under all
    ideas = _json(hive, "macos", "--kind", "idea")
    assert len(ideas) == 1 and ideas[0]["kind"] == "idea" and ideas[0]["ref"] and ideas[0]["confidence"] == 0.0
    assert "Ideas (hypotheses" in hive.run("search", "macos", "--kind", "idea").stdout
    assert [r["kind"] for r in _json(hive, "macos", "--kind", "fact")] == ["fact"]
    assert _json(hive, "macos", "--kind", "decision") == []
    # the i<N> prefix resolves through the kind-checked ref parser (PR3)
    r = hive.run("decide", "investigate disk io", "--informed", "i1")
    assert "informed by 1 ref(s)" in r.stdout
    assert hive.run("decide", "nope", "--informed", "i9", check=False).returncode != 0


def test_open_ideas_digest_lists_up_to_three_newest_and_stats_counts(hive):
    for i in range(4):
        hive.run("propose", f"hypothesis number {i} about the build", "--source", "alice")
    out = hive.run("nudge", "--event", "session-start", "--cwd", str(hive.home)).stdout
    assert "Open ideas" in out
    listed = [l for l in out.splitlines() if l.strip().startswith("• h:")]   # the stable sid, not `i<N>`
    assert len(listed) == 3 and "hypothesis number 3" in listed[0]         # cap 3, newest first
    assert "--supports h:" in out                                          # how to weigh in (#71)
    assert "Ideas:              4  (0 with earned confidence)" in hive.run("stats").stdout


# ── projection numbers (in-memory entries, real rebuild) ─────────────────────────────────────────

def test_sense_support_raises_introspect_support_does_not_until_knob(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, c), base = _owned_hive(hv)
    idea = _idea(hv, a, "the macos runner is slow because of disk io", "2026-01-02T00:00:00Z")
    obs = _fact(hv, b, "iostat showed 95% disk busy during the macos test job", "2026-01-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [idea, obs])
    assert conn.execute("SELECT confidence FROM ideas").fetchone()[0] == 0.0
    # a sense-channel supports link from another identity (absent channel = sense) → earns confidence
    sup = _link(hv, b, "supports", obs, idea, "2026-01-04T00:00:00Z")
    conn = _project(hv, tmp_path, base + [idea, obs, sup])
    c1 = conn.execute("SELECT confidence FROM ideas").fetchone()[0]
    assert round(c1, 6) == round(hv._confidence_for(1.0), 6)
    # an introspect-channel supports link (e.g. from another idea / a plan step) weighs 0 by default
    idea2 = _idea(hv, c, "I also think it is disk io", "2026-01-03T00:00:01Z")
    intro = _link(hv, c, "supports", idea2, idea, "2026-01-04T00:00:01Z", channel="introspect")
    conn = _project(hv, tmp_path, base + [idea, idea2, obs, intro])
    assert conn.execute("SELECT confidence FROM ideas WHERE content LIKE 'the macos%'").fetchone()[0] == 0.0
    # …until the owner raises the governed knob — identically on every node
    knob = _gov(hv, {"action": "set-config", "key": "introspect_support_weight", "value": 1.0},
                oseed, opub, "2026-01-01T00:00:09Z", 9)
    conn = _project(hv, tmp_path, base + [knob, idea, idea2, obs, intro])
    assert conn.execute("SELECT confidence FROM ideas WHERE content LIKE 'the macos%'").fetchone()[0] > 0


def test_contradicts_lowers_contested_flag_and_cap_self(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    idea = _idea(hv, a, "the macos runner is slow because of disk io", "2026-01-02T00:00:00Z")
    o1 = _fact(hv, b, "iostat showed 95% disk busy during the macos test job", "2026-01-03T00:00:00Z")
    o2 = _fact(hv, c, "the same job on a ramdisk runner was just as slow", "2026-01-03T00:00:01Z")
    sup = _link(hv, b, "supports", o1, idea, "2026-01-04T00:00:00Z")
    con = _link(hv, c, "contradicts", o2, idea, "2026-01-04T00:00:01Z")
    conn = _project(hv, tmp_path, base + [idea, o1, o2, sup, con])
    row = conn.execute("SELECT confidence, contested FROM ideas").fetchone()
    assert round(row[0], 6) == 0.0 and row[1] == 1                            # +1 −1 → net 0, contested
    # the author supporting its OWN idea, even from two agents on one device, counts for nothing (#71):
    # an idea earns confidence only from other principals
    own1 = _link(hv, a, "supports", o1, idea, "2026-01-04T00:00:02Z")
    own2 = _link(hv, a, "supports", o2, idea, "2026-01-04T00:00:03Z", source="hermes:primary/other/s1")
    conn = _project(hv, tmp_path, base + [idea, o1, o2, own1, own2])
    assert conn.execute("SELECT confidence FROM ideas").fetchone()[0] == 0.0


def test_author_self_support_weighs_zero_across_its_principal_but_contradicts_counts(tmp_path, monkeypatch):
    """#71: the same-principal rule covers another device admitted under the author's principal; it is
    asymmetric (the author's `contradicts` still counts); cap_self still binds when every supporter
    shares one OTHER principal."""
    hv = _loadhv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, c), base = _owned_hive(hv)
    a2, b2 = _device(hv), _device(hv)
    base = base + [_gov(hv, {"action": "admit", "device_id": a2["id"], "principal": "p0"}, oseed, opub,
                         "2026-01-01T00:00:07Z", 7),
                   _gov(hv, {"action": "admit", "device_id": b2["id"], "principal": "p1"}, oseed, opub,
                        "2026-01-01T00:00:08Z", 8)]
    idea = _idea(hv, a, "the flaky test is a timezone bug", "2026-01-02T00:00:00Z")
    obs_a2 = _fact(hv, a2, "the test fails only between 23:00 and 00:00 UTC", "2026-01-03T00:00:00Z")
    obs_b = _fact(hv, b, "setting TZ=UTC makes the test pass", "2026-01-03T00:00:01Z")
    obs_b2 = _fact(hv, b2, "the failing assertion compares local dates", "2026-01-03T00:00:02Z")
    obs_c = _fact(hv, c, "the test also failed once at noon", "2026-01-03T00:00:03Z")
    by_a2 = _link(hv, a2, "supports", obs_a2, idea, "2026-01-04T00:00:00Z")      # author's other device
    conn = _project(hv, tmp_path, base + [idea, obs_a2, by_a2])
    assert conn.execute("SELECT confidence FROM ideas").fetchone()[0] == 0.0
    by_b = _link(hv, b, "supports", obs_b, idea, "2026-01-04T00:00:01Z")
    by_b2 = _link(hv, b2, "supports", obs_b2, idea, "2026-01-04T00:00:02Z")
    conn = _project(hv, tmp_path, base + [idea, obs_b, obs_b2, by_b, by_b2])
    two_devices_one_principal = conn.execute("SELECT confidence FROM ideas").fetchone()[0]
    assert 0 < two_devices_one_principal <= hv.CONF_CAP_SELF + 1e-9               # cap_self still binds
    # the author withdraws it: their contradicts counts, and with others' support the idea is contested
    con_a = _link(hv, a, "contradicts", obs_c, idea, "2026-01-04T00:00:03Z")
    conn = _project(hv, tmp_path, base + [idea, obs_b, obs_b2, obs_c, by_b, by_b2, con_a])
    row = conn.execute("SELECT confidence, contested FROM ideas").fetchone()
    assert row[1] == 1 and row[0] < two_devices_one_principal


def test_peer_idea_arrival_emits_bus_line_local_write_does_not(hive, tmp_path, monkeypatch):
    # local propose → no bus line
    hive.run("propose", "a local hypothesis about caching", "--source", "alice")
    assert not (hive.home / ".bus" / "introspect.log").exists()
    # a PEER's idea landing through the ingest path → exactly one idea-arrived line
    hv = _loadhv(hive.home, monkeypatch)
    peer = _device(hv)
    e = _idea(hv, peer, "a peer hypothesis about the scheduler", "2026-01-02T00:00:00Z")
    accepted, dups = hv.append_foreign_entries([e])
    assert accepted == 1
    log = (hive.home / ".bus" / "introspect.log").read_text().splitlines()
    assert len(log) == 1 and "idea-arrived" in log[0] and f"{peer['id']}:1" in log[0]
    # replaying the same entry is a duplicate: no second line
    hv.append_foreign_entries([e])
    assert len((hive.home / ".bus" / "introspect.log").read_text().splitlines()) == 1
    hv.rebuild_db()
    assert hive.query("SELECT count(*) c FROM ideas")[0]["c"] == 2


def test_two_node_differential_idea_confidence(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path / "n1", monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    idea = _idea(hv, a, "the macos runner is slow because of disk io", "2026-01-02T00:00:00Z")
    o1 = _fact(hv, b, "iostat showed 95% disk busy", "2026-01-03T00:00:00Z")
    o2 = _fact(hv, c, "ramdisk runner just as slow", "2026-01-03T00:00:01Z")
    entries = base + [idea, o1, o2,
                      _link(hv, b, "supports", o1, idea, "2026-01-04T00:00:00Z"),
                      _link(hv, c, "contradicts", o2, idea, "2026-01-04T00:00:01Z", channel="introspect")]

    def snap(home, order):
        h = _loadhv(home, monkeypatch)
        conn = _project(h, home, list(order))
        return [tuple(r) for r in conn.execute("SELECT content, round(confidence, 6), contested, last_evidence_at FROM ideas")]

    s1, s2 = snap(tmp_path / "n1", entries), snap(tmp_path / "n2", reversed(entries))
    assert s1 == s2 and s1[0][2] == 0                    # introspect contradiction weighed nothing → not contested


# ── the live write path: `hv remember --supports/--contradicts` (#71) ─────────────────────────────────

import re                  # noqa: E402
import subprocess          # noqa: E402

PROJECT_HV = Path(__file__).resolve().parent.parent / "hv"


def _as(hive, node, *args, check=True):
    """Run `hv` as a given device (HIVE_NODE_ID), so one temp hive holds two principals (no owner yet:
    different devices are different principals)."""
    env = dict(os.environ, HIVE_HOME=str(hive.home), HIVE_NODE_ID=node)
    r = subprocess.run([sys.executable, str(PROJECT_HV), *args], env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"`hv {' '.join(args)}` failed ({r.returncode}):\n{r.stderr}")
    return r


def _sid(out):
    return re.search(r"h:[0-9a-f]{10}", out).group(0)


def test_supports_on_an_idea_raises_it_immediately_but_not_from_its_author(hive):
    idea = _sid(_as(hive, "nodeA", "propose", "the cache causes the retry storm").stdout)
    r = _as(hive, "nodeA", "remember", "the storm began right after a cache flush", "--supports", idea)
    assert f"↗ supports idea {idea}" in r.stdout and "your own idea" in r.stdout
    assert hive.query("SELECT confidence FROM ideas")[0]["confidence"] == 0.0      # the author's own: nothing
    r = _as(hive, "nodeB", "remember", "cache hit rate fell to 3% during the storm", "--supports", idea)
    assert "your own idea" not in r.stdout
    row = hive.query("SELECT confidence, last_evidence_at FROM ideas")[0]          # no rebuild in between
    assert row["confidence"] > 0 and row["last_evidence_at"]
    _as(hive, "nodeA", "remember", "the storm also happened with the cache disabled", "--contradicts", idea)
    assert hive.query("SELECT contested FROM ideas")[0]["contested"] == 1           # the author may withdraw


def test_supports_on_a_fact_moves_the_target_now_and_introspect_weighs_nothing(hive):
    target = _sid(_as(hive, "nodeA", "remember", "the vendor api paginates at 100 rows").stdout)
    before = hive.query("SELECT confidence FROM facts WHERE content LIKE 'the vendor api%'")[0]["confidence"]
    _as(hive, "nodeB", "remember", "I reasoned it must page at 100", "--supports", target,
        "--channel", "introspect")
    same = hive.query("SELECT confidence FROM facts WHERE content LIKE 'the vendor api%'")[0]["confidence"]
    assert round(same, 6) == round(before, 6)
    _as(hive, "nodeB", "remember", "fetching 250 rows took 3 pages", "--supports", target)
    after = hive.query("SELECT confidence FROM facts WHERE content LIKE 'the vendor api%'")[0]["confidence"]
    assert after > before


def test_bad_targets_and_two_relationships_write_nothing(hive):
    dec = _sid(_as(hive, "nodeA", "decide", "ship on friday", "--rationale", "r").stdout)
    fact = _sid(_as(hive, "nodeA", "remember", "ci is green").stdout)
    n = len(hive.entries())
    r = _as(hive, "nodeB", "remember", "it shipped", "--supports", dec, check=False)
    assert r.returncode == 1 and "--outcome-of" in r.stderr
    r = _as(hive, "nodeB", "remember", "nothing", "--contradicts", "h:0000000000", check=False)
    assert r.returncode == 1
    r = _as(hive, "nodeB", "remember", "both", "--supports", fact, "--resolves", fact, check=False)
    assert r.returncode == 2 and "not allowed with" in r.stderr                    # argparse, before any I/O
    assert len(hive.entries()) == n                                                 # nothing was written
