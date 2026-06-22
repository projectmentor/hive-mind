"""Offline tests for the nudge/audit loop (hv nudge / hv audit).

Drives the real `hv` CLI via subprocess against an isolated temp HIVE_HOME, with
config supplied as env vars (load_nudge_config honors env overrides) and decay
made deterministic via $HIVE_NOW. No network.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"


def run_hv(home, *args, stdin="", **env):
    e = dict(os.environ, HIVE_HOME=str(home))
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run([sys.executable, str(HV), *args], input=stdin,
                          env=e, capture_output=True, text=True)


def _nudged(r):
    return "[hive]" in r.stdout


# ---- session-start freshness check (warn-on-behind) ----

def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, check=True)


def _checkout_with_origin(tmp_path, advance_origin):
    """A git checkout (home) whose origin/main is optionally one commit ahead."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    home = tmp_path / "home"
    subprocess.run(["git", "init", "-b", "main", str(home)], capture_output=True, check=True)
    _git(home, "config", "user.email", "t@t"); _git(home, "config", "user.name", "t")
    (home / "f.txt").write_text("1")
    _git(home, "add", "-A"); _git(home, "commit", "-m", "c1")
    _git(home, "remote", "add", "origin", str(origin))
    _git(home, "push", "-u", "origin", "main")
    if advance_origin:
        work2 = tmp_path / "work2"
        subprocess.run(["git", "clone", str(origin), str(work2)], capture_output=True, check=True)
        _git(work2, "config", "user.email", "t@t"); _git(work2, "config", "user.name", "t")
        (work2 / "g.txt").write_text("2")
        _git(work2, "add", "-A"); _git(work2, "commit", "-m", "c2"); _git(work2, "push", "origin", "main")
    return home


def test_freshness_warns_when_behind(tmp_path):
    home = _checkout_with_origin(tmp_path, advance_origin=True)
    r = run_hv(home, "nudge", "--event", "session-start", "--cwd", str(home))
    assert "behind origin/main" in r.stdout


def test_freshness_silent_when_current(tmp_path):
    home = _checkout_with_origin(tmp_path, advance_origin=False)
    r = run_hv(home, "nudge", "--event", "session-start", "--cwd", str(home))
    assert "behind origin/main" not in r.stdout


def test_freshness_silent_when_not_a_git_checkout(tmp_path):
    # The default test HIVE_HOME is not a git repo -> must never warn or error.
    r = run_hv(tmp_path, "nudge", "--event", "session-start", "--cwd", str(tmp_path))
    assert r.returncode == 0 and "behind origin/main" not in r.stdout


# ---- save-nudge cadence / debounce / phrase ----

def test_save_nudge_cadence(tmp_path):
    out = [run_hv(tmp_path, "nudge", "--event=user-prompt", "--session=s",
                  stdin="plain text", SAVE_EVERY=3, MIN_GAP=1, SAVE_ON_PHRASE=0)
           for _ in range(3)]
    assert not _nudged(out[0]) and not _nudged(out[1]), "should stay quiet before the cadence"
    assert _nudged(out[2]), "should nudge on the 3rd turn"


def test_save_nudge_min_gap(tmp_path):
    fired = 0
    for _ in range(4):  # SAVE_EVERY=1 (every turn) but MIN_GAP=3 throttles
        r = run_hv(tmp_path, "nudge", "--event=user-prompt", "--session=g",
                   stdin="plain", SAVE_EVERY=1, MIN_GAP=3, SAVE_ON_PHRASE=0)
        fired += _nudged(r)
    assert fired == 2, f"MIN_GAP should cap to turns 1 and 4, got {fired}"


def test_save_nudge_phrase_trigger(tmp_path):
    r = run_hv(tmp_path, "nudge", "--event=user-prompt", "--session=p",
               stdin="i have an idea about this", SAVE_EVERY=0, MIN_GAP=1,
               SAVE_ON_PHRASE=1, HIVE_NUDGE_PHRASES="idea,decision")
    assert _nudged(r), "a watched phrase should trigger a save-nudge even with cadence off"


def test_nudge_never_errors(tmp_path):
    for stdin in ("", "not json", "x" * 5000):
        r = run_hv(tmp_path, "nudge", "--event=user-prompt", stdin=stdin)  # no --session
        assert r.returncode == 0 and "Traceback" not in r.stderr


# ---- audit: redundancy (the critical correctness guard) ----

def test_audit_flags_same_source_duplication(tmp_path):
    run_hv(tmp_path, "remember", "the cache halves cold-start latency", "--source", "alice")
    run_hv(tmp_path, "remember", "the cache halves cold-start latency", "--source", "alice")
    r = run_hv(tmp_path, "audit", "--format=json")
    data = json.loads(r.stdout)
    assert any("cache halves" in x["content"] for x in data["redundant"]), \
        "same identity asserting identical content twice = redundant"


def test_audit_flags_contravened(tmp_path):
    run_hv(tmp_path, "remember", "the widget is broken", "--source", "alice")
    # a correction that NAMES the prior fact in prose but never reconciled it
    run_hv(tmp_path, "remember", "the widget is healed, resolves #1", "--source", "alice")
    data = json.loads(run_hv(tmp_path, "audit", "--format=json").stdout)
    assert any(x["references"] == 1 for x in data["contravened"]), \
        "a correction naming a still-live fact must be flagged CONTRAVENED"


def test_audit_resolves_clears_contravened(tmp_path):
    run_hv(tmp_path, "remember", "the widget is broken", "--source", "alice")
    # --resolves soft-retracts #1, so nothing is left un-reconciled even though prose names it
    run_hv(tmp_path, "remember", "the widget is healed, resolves #1", "--resolves", "1", "--source", "alice")
    data = json.loads(run_hv(tmp_path, "audit", "--format=json").stdout)
    assert not data["contravened"], "a --resolves'd correction must not be flagged"


def test_audit_excludes_owner_forgotten_from_redundant(tmp_path):
    import re as _re
    run_hv(tmp_path, "remember", "the cache halves cold-start latency", "--source", "alice")
    out = run_hv(tmp_path, "remember", "the cache halves cold-start latency", "--source", "alice").stdout
    fid = _re.search(r"#(\d+)", out).group(1)
    # Before forgetting, it is flagged.
    data = json.loads(run_hv(tmp_path, "audit", "--format=json").stdout)
    assert any("cache halves" in x["content"] for x in data["redundant"])
    # Owner-forget the canonical fact -> it must drop out of EVERY audit bucket.
    run_hv(tmp_path, "retract", fid, "--owner")
    data = json.loads(run_hv(tmp_path, "audit", "--format=json").stdout)
    assert not any("cache halves" in x["content"] for x in data["redundant"]), \
        "owner-forgotten content must not keep appearing in the redundant bucket"


def test_audit_does_not_flag_independent_corroboration(tmp_path):
    c = "tailscale gives ~5ms RTT between the nodes"
    for src in ("alice", "bob", "carol"):
        run_hv(tmp_path, "remember", c, "--source", src)
    r = run_hv(tmp_path, "audit", "--format=json")
    data = json.loads(r.stdout)
    assert not any("tailscale gives" in x["content"] for x in data["redundant"]), \
        "3 distinct sources is corroboration, NOT redundancy"
    assert data["well_corroborated"] >= 1


# ---- audit: obsolete (decayed / contested) ----

def test_audit_flags_obsolete_decayed(tmp_path):
    run_hv(tmp_path, "remember", "the staging endpoint is at example.test", "--source", "alice")
    r = run_hv(tmp_path, "audit", "--format=json", HIVE_NOW="2027-12-01T00:00:00+00:00")
    data = json.loads(r.stdout)
    assert any("staging endpoint" in x["content"] for x in data["obsolete"]["decayed"]), \
        "a single-source fact ~540d old should decay into the obsolete/decayed bucket"


def test_audit_flags_contested(tmp_path):
    out = run_hv(tmp_path, "remember", "redis is better than sqlite here", "--source", "alice")
    m = re.search(r"#(\d+)", out.stdout)
    assert m, out.stdout
    run_hv(tmp_path, "retract", m.group(1), "--source", "bob", "--reason", "disagree")
    r = run_hv(tmp_path, "audit", "--format=json")
    data = json.loads(r.stdout)
    assert any("redis is better" in x["content"] for x in data["obsolete"]["contested"])


def test_audit_depth_caps(tmp_path):
    for i in range(5):
        c = f"distinct redundant fact number {i}"
        run_hv(tmp_path, "remember", c, "--source", "alice")
        run_hv(tmp_path, "remember", c, "--source", "alice")
    light = json.loads(run_hv(tmp_path, "audit", "--format=json", "--depth=light").stdout)
    deep = json.loads(run_hv(tmp_path, "audit", "--format=json", "--depth=deep").stdout)
    assert len(light["redundant"]) <= 3 < len(deep["redundant"])
    assert light["depth"] == "light" and deep["depth"] == "deep"


def test_audit_recheck_volatile(tmp_path):
    run_hv(tmp_path, "remember", "gregorius sshd is reachable", "--tags", "volatile", "--source", "alice")
    # default TTL 24h; jump 2 days ahead so it's past freshness
    data = json.loads(run_hv(tmp_path, "audit", "--format=json",
                             HIVE_NOW="2027-12-01T00:00:00+00:00").stdout)
    assert any("sshd is reachable" in x["content"] for x in data["recheck"]), \
        "a volatile fact past its freshness window should land in RECHECK"
    # and it must NOT be miscategorized as obsolete/decayed
    assert not any("sshd is reachable" in x["content"] for x in data["obsolete"]["decayed"])


def _insert_raw_fact(home, content, tags_json="[]", last_ev="2026-01-01T00:00:00+00:00"):
    """Insert a fact straight into the table to simulate a pre-existing (pre-auto-tag) row."""
    import sqlite3
    run_hv(home, "stats")  # ensure init_db ran
    c = sqlite3.connect(home / "store.db")
    c.execute("INSERT INTO facts (content, tags, source_agent, confidence, last_evidence_at) "
              "VALUES (?,?,?,?,?)", (content, tags_json, "alice", 0.45, last_ev))
    c.commit(); c.close()


def test_audit_rechecks_untagged_operational_fact_by_content(tmp_path):
    # An OLD fact (no volatile tag) whose content reads as a transient status must still
    # land in RECHECK — volatility is detected from content at audit time (retro-coverage).
    _insert_raw_fact(tmp_path, "the build server is currently down")
    data = json.loads(run_hv(tmp_path, "audit", "--format=json",
                             HIVE_NOW="2027-12-01T00:00:00+00:00").stdout)
    assert any("build server" in x["content"] for x in data["recheck"])
    assert not any("build server" in x["content"] for x in data["obsolete"]["decayed"])


def test_audit_respects_durable_optout(tmp_path):
    # A fact the owner marked durable (--no-volatile) must NOT be re-detected as volatile.
    _insert_raw_fact(tmp_path, "the build server is currently down", tags_json='["durable"]')
    data = json.loads(run_hv(tmp_path, "audit", "--format=json",
                             HIVE_NOW="2027-12-01T00:00:00+00:00").stdout)
    assert not any("build server" in x["content"] for x in data["recheck"])


def test_remember_no_volatile_marks_durable(tmp_path):
    run_hv(tmp_path, "remember", "node b is reachable", "--no-volatile", "--source", "alice")
    import sqlite3
    c = sqlite3.connect(tmp_path / "store.db"); c.row_factory = sqlite3.Row
    tags = json.loads(c.execute("SELECT tags FROM facts WHERE content=?",
                                ("node b is reachable",)).fetchone()["tags"])
    c.close()
    assert "durable" in tags and "volatile" not in tags


def test_audit_volatile_not_flagged_redundant(tmp_path):
    c = "the sync daemon is running"
    run_hv(tmp_path, "remember", c, "--tags", "volatile", "--source", "alice")
    run_hv(tmp_path, "remember", c, "--tags", "volatile", "--source", "alice")  # re-verify, same source
    data = json.loads(run_hv(tmp_path, "audit", "--format=json").stdout)
    assert not any("sync daemon is running" in x["content"] for x in data["redundant"]), \
        "re-verifying a volatile fact is freshness evidence, not redundancy"


def test_audit_json_shape(tmp_path):
    run_hv(tmp_path, "remember", "a plain fact for shape check", "--source", "alice")
    data = json.loads(run_hv(tmp_path, "audit", "--format=json").stdout)
    assert {"redundant", "obsolete", "well_corroborated", "depth"} <= set(data)
    assert {"forgotten", "contested", "decayed"} <= set(data["obsolete"])


# ---- session-start digest ----

def test_session_digest_project_scoped(tmp_path):
    run_hv(tmp_path, "remember", "alphaproj milestone reached on schedule", "--source", "alice")
    r = run_hv(tmp_path, "nudge", "--event=session-start", "--cwd=/tmp/alphaproj")
    assert "alphaproj" in r.stdout and "Project context" in r.stdout
