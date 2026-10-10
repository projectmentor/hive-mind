"""Release 3.0 PR 2 (plan 3.2, 3.4, 3.6): `hive-mind migrate forget` and `--check`, the migration set, the
node-local notices. `forget-authz` stays the 2.4 fail-on-open-policy (the pass-on-empty rule ships with the arm's
removal in PR 4).

- The migration set is a differential of the projection (live key vs. the grandfather arm off), checked on the
  whole forget corpus against the frozen 2.4 oracle.
- `--check` never writes; it prints an explicit empty list.
- Idempotence, partial failure and #130 (nothing re-keyed: the old journal is a prefix of the new one and every
  new entry's ref resolves to a fact it holds).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
sys.path.insert(0, str(PROJECT / "tests" / "oracles"))
from test_links import _loadhv  # noqa: E402
from test_forget_close import _ctl, _ctl_at_a_terminal, _backdate, _forget_authz, _retracts  # noqa: E402
from test_forget_writers import _cli, _legacy_hive, _policy_acts, _journal  # noqa: E402
import forget_corpus as fc  # noqa: E402
import content_evidence_2_4 as oracle_mod  # noqa: E402
import _planes  # noqa: E402

CASES = [(name, policy) for name in fc.CORPUS for policy in fc.POLICIES]


# ── the migration set, on the corpus, against the oracle ───────────────────────────────────────────

@pytest.mark.parametrize("name,policy", CASES)
def test_the_migration_set_is_the_oracle_differential(tmp_path, monkeypatch, name, policy):
    hv = _loadhv(tmp_path, monkeypatch)
    scn = fc.CORPUS[name](hv)
    entries = fc.journal(hv, scn, policy)
    gov = hv._governance_state(entries)
    m = hv._forget_migration(entries, gov)
    oracle = oracle_mod.bind(hv)
    live = {c for c, ev in oracle(entries, gov, False).items() if ev["forget"]}
    arm_off = {c for c, ev in oracle(entries, dict(gov, config=dict(gov["config"], forget_writers="owner")),
                                     False).items() if ev["forget"]} if scn.owner is not None else live
    assert {f["content"] for f in m["facts"]} == live - arm_off
    if fc.effective(scn, policy) == "owner":
        assert m["facts"] == [] and m["hides"] == []             # nothing to migrate once the key is `owner`
    expected = {c for c, (before, after) in scn.expect.items() if before and not after}
    if fc.effective(scn, policy) != "owner":
        assert {f["content"] for f in m["facts"]} == expected    # and by hand, not read back from the oracle


def test_no_owner_yet_has_an_empty_set(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    m = hv._forget_migration([], hv._governance_state([]))
    assert m["facts"] == [] and m["owner"] is False


# ── the verb, on real hives ─────────────────────────────────────────────────────────────────────────

def _bytes(home):
    return {str(p.relative_to(home)): p.read_bytes() for p in sorted(Path(home, "journal").glob("*.jsonl"))}


def _open_hive(tmp_path, texts=("the backup runs at 02:00", "the vpn uses wireguard")):
    run = _cli(tmp_path)
    base = _legacy_hive(tmp_path, run)
    run("remember", "a fact nobody forgot")
    sids = [_backdate(tmp_path, run, t, i) for i, t in enumerate(texts, 1)]
    return run, base, sids


def _entries(home):
    return _journal(home)


def test_check_lists_the_dependent_facts_exits_non_zero_and_writes_nothing(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    before = _bytes(tmp_path)
    r = _ctl(tmp_path, "migrate", "forget", "--check")
    assert r.returncode == 1 and all(s in r.stdout for s in sids) and "hive-mind migrate forget" in r.stdout
    assert _bytes(tmp_path) == before


def test_check_prints_an_explicit_empty_list_on_a_migrated_hive_and_still_writes_nothing(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    rc, out = _ctl_at_a_terminal(tmp_path, "y", "migrate", "forget")
    assert rc == 0, out
    before = _bytes(tmp_path)
    r = _ctl(tmp_path, "migrate", "forget", "--check")
    assert r.returncode == 0 and "closed: 0 facts depend on a pre-genesis unsigned forget" in r.stdout
    assert _bytes(tmp_path) == before


def test_check_passes_on_legacy_with_nothing_dependent_and_the_doctor_check_still_fails(tmp_path):
    run = _cli(tmp_path)
    _legacy_hive(tmp_path, run)
    before = _bytes(tmp_path)
    r = _ctl(tmp_path, "migrate", "forget", "--check")
    assert r.returncode == 0 and "closed: 0 facts depend" in r.stdout
    assert _bytes(tmp_path) == before
    fa = _forget_authz(tmp_path)                                  # PR 2 leaves the 2.4 check alone
    assert fa["status"] == "fail" and "forget_writers=legacy" in fa["detail"]


def test_a_key_owner_hive_ignores_a_pre_genesis_plant_and_the_run_appends_nothing(tmp_path):
    run = _cli(tmp_path)
    run("owner", "init")                                          # born closed
    sid = _backdate(tmp_path, run, "the backup runs at 02:00", 1)
    before = _bytes(tmp_path)
    r = _ctl(tmp_path, "migrate", "forget", "--check")
    assert r.returncode == 0 and "closed: 0 facts depend" in r.stdout and sid not in r.stdout
    rc, out = _ctl_at_a_terminal(tmp_path, "y", "migrate", "forget")
    assert rc == 0 and _bytes(tmp_path) == before                 # never asks, never re-signs a plant 2.4 ignores
    assert _forget_authz(tmp_path)["status"] == "ok"


@pytest.mark.parametrize("how", ["no-terminal", "N"])
def test_without_a_yes_nothing_is_written(tmp_path, how):
    run, base, sids = _open_hive(tmp_path)
    before = _bytes(tmp_path)
    if how == "N":
        rc, out = _ctl_at_a_terminal(tmp_path, "n", "migrate", "forget")
    else:
        r = _ctl(tmp_path, "migrate", "forget")
        rc, out = r.returncode, r.stdout
    assert rc == 1 and all(s in out for s in sids)
    assert _bytes(tmp_path) == before


def test_yes_re_issues_every_forget_then_sets_the_key_and_is_idempotent(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    old = _entries(tmp_path)
    retracts_before = len(_retracts(tmp_path))
    rc, out = _ctl_at_a_terminal(tmp_path, "y", "migrate", "forget")
    assert rc == 0, out
    new = _entries(tmp_path)
    # #130: nothing re-keyed. The old journal is a prefix; every new retract names a fact the journal holds.
    assert new[:len(old)] == old
    facts = {(e["node_id"], e["seq"]) for e in new if e["type"] == "fact"}
    added = new[len(old):]
    retracts = [e for e in added if e["type"] == "retract"]
    assert len(retracts) == 2 and len(_retracts(tmp_path)) == retracts_before + 2
    assert all(tuple(e["payload"]["retracts_ref"]) in facts and "owner_sig" in e["payload"]
               and "migrate forget" in e["payload"]["reason"] for e in retracts)
    acts = _policy_acts(tmp_path)
    assert acts[-1]["payload"]["value"] == "owner" and new.index(retracts[-1]) < new.index(acts[-1])
    assert _forget_authz(tmp_path)["status"] == "ok"
    # idempotent: a second run, with or without a terminal, appends nothing
    snap = _bytes(tmp_path)
    r = _ctl(tmp_path, "migrate", "forget")
    assert r.returncode == 0 and "closed: 0 facts" in r.stdout and _bytes(tmp_path) == snap
    rc2, _ = _ctl_at_a_terminal(tmp_path, "y", "migrate", "forget")
    assert rc2 == 0 and _bytes(tmp_path) == snap


def test_the_projection_is_unchanged_by_the_migration(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    import sqlite3
    def conf():
        with sqlite3.connect(Path(tmp_path) / "store.db") as c:
            return dict(c.execute("SELECT content, confidence FROM facts").fetchall())
    before = conf()
    rc, out = _ctl_at_a_terminal(tmp_path, "y", "migrate", "forget")
    assert rc == 0 and conf() == before


def test_a_device_without_the_owner_key_writes_nothing(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    for p in list((tmp_path / "keys").glob("owner*")) if (tmp_path / "keys").exists() else []:
        p.unlink()
    env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"),
               HIVE_KEY_DIR=str(tmp_path / "nokeys"))
    before = _bytes(tmp_path)
    r = _ctl(tmp_path, "migrate", "forget", env=env)
    assert r.returncode == 1 and "does not hold the owner key" in r.stdout and _bytes(tmp_path) == before


def test_partial_failure_sets_no_key_and_a_rerun_completes(tmp_path, monkeypatch, capsys):
    run, base, sids = _open_hive(tmp_path)
    hv = _planes.load_hv(tmp_path, "hv_migrate_partial")
    monkeypatch.setattr(hv, "_confirmed", lambda q: True)
    real = hv.append_journal
    seen = []

    def flaky(entry_type, payload, timestamp=None):
        if entry_type == "retract":
            seen.append(payload)
            if len(seen) == 2:
                raise OSError("no space left on device")
        return real(entry_type, payload, timestamp=timestamp)

    args = type("A", (), {"migrate_action": "forget", "check": False})()
    monkeypatch.setattr(hv, "append_journal", flaky)
    assert hv.migrate_cmd(args) == 1
    out = capsys.readouterr().out
    assert "PARTLY re-issued" in out
    entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    assert (hv._governance_state(entries).get("config") or {}).get("forget_writers", "legacy") == "legacy"
    assert len([e for e in entries if e["type"] == "retract" and "migrate forget" in (e["payload"].get("reason") or "")]) == 1
    # the check sees the remainder; the rerun finds it and completes
    assert hv.migrate_cmd(type("A", (), {"migrate_action": "forget", "check": True})()) == 1
    monkeypatch.setattr(hv, "append_journal", real)
    assert hv.migrate_cmd(args) == 0
    entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    assert (hv._governance_state(entries).get("config") or {}).get("forget_writers") == "owner"
    assert hv._forget_migration(entries, hv._governance_state(entries))["facts"] == []


# ── the pointer, the notices ───────────────────────────────────────────────────────────────────────

def test_hv_points_at_the_control_plane_and_acts_on_nothing(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    before = _bytes(tmp_path)
    env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "migrate", "forget"], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "hive-mind migrate forget" in r.stderr and _bytes(tmp_path) == before


def test_read_verbs_and_the_nudge_carry_a_notice_while_a_migration_is_owed(tmp_path):
    run, base, sids = _open_hive(tmp_path)
    env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))
    for argv in (["search", "wireguard"], ["feed"]):
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *argv], env=env, capture_output=True, text=True)
        assert "hive-mind migrate forget" in r.stderr and "NOTICE" in r.stderr, argv
        assert "NOTICE" not in r.stdout
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "nudge", "--event=session-start"], env=env,
                       capture_output=True, text=True)
    assert "hive-mind migrate forget" in r.stdout
    rc, _ = _ctl_at_a_terminal(tmp_path, "y", "migrate", "forget")
    assert rc == 0
    for argv in (["search", "wireguard"], ["feed"]):
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *argv], env=env, capture_output=True, text=True)
        assert "NOTICE" not in r.stderr, argv
