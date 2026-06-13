"""Membership lifecycle (`hv group`): revoke / deny / change / purge / list, and how each flows
through the governance projection (admission gate + confidence). Drives the real `hv` CLI against
an isolated temp HIVE_HOME, writing the same content from DISTINCT devices (HIVE_NODE_ID) where
independent corroboration is intended. Mirrors tests/test_governance.py helpers."""

import importlib.machinery
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _run(home, *args, node_id=None, check=True):
    env = dict(os.environ, HIVE_HOME=str(home))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _conf(home, content):
    conn = sqlite3.connect(home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT confidence FROM facts WHERE content=?", (content,)).fetchone()
        return row["confidence"] if row else None
    finally:
        conn.close()


def _gov(home):
    loader = importlib.machinery.SourceFileLoader("hvmod_grp", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_grp", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    import merkle
    return m, m._governance_state(merkle.read_all_entries(str(home / "journal")))


def _two_admitted_corroboration(home):
    """Owner + dev-a(alice) + dev-b(bob), both assert the same claim → independent corroboration."""
    _run(home, "owner", "init")
    _run(home, "group", "admit", "dev-a", "--principal", "alice")
    _run(home, "group", "admit", "dev-b", "--principal", "bob")
    _run(home, "remember", "claim", "--source", "agent", node_id="dev-a")
    _run(home, "remember", "claim", "--source", "agent", node_id="dev-b")


def test_revoke_returns_device_to_sterile(tmp_path):
    home = tmp_path
    _two_admitted_corroboration(home)
    two = _conf(home, "claim")
    assert two > 0.45                                   # two independent devices > one
    _run(home, "group", "revoke", "dev-b")
    _, gov = _gov(home)
    assert "dev-b" not in gov["admitted"]
    assert abs(_conf(home, "claim") - 0.45) < 1e-6      # only dev-a counts now


def test_readmit_after_revoke_restores_count(tmp_path):
    home = tmp_path
    _two_admitted_corroboration(home)
    two = _conf(home, "claim")
    _run(home, "group", "revoke", "dev-b")
    assert abs(_conf(home, "claim") - 0.45) < 1e-6
    _run(home, "group", "admit", "dev-b", "--principal", "bob")
    _, gov = _gov(home)
    assert "dev-b" in gov["admitted"]
    assert abs(_conf(home, "claim") - two) < 1e-6       # re-admit restores corroboration


def test_purge_tombstones_and_survives_readmit(tmp_path):
    home = tmp_path
    _two_admitted_corroboration(home)
    _run(home, "group", "purge", "dev-b")
    _, gov = _gov(home)
    assert "dev-b" in gov["purged"] and "dev-b" not in gov["admitted"]
    assert abs(_conf(home, "claim") - 0.45) < 1e-6      # purged device stops counting
    # A re-admit must be refused — purge is permanent.
    r = _run(home, "group", "admit", "dev-b", "--principal", "bob")
    assert "purged" in r.stdout.lower() and "cannot be re-admitted" in r.stdout.lower()
    _, gov = _gov(home)
    assert "dev-b" not in gov["admitted"]               # still not admitted
    assert abs(_conf(home, "claim") - 0.45) < 1e-6      # confidence stays excluded


def test_deny_excludes_from_pending_and_admit_overrides(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "join", "--principal", "carol", node_id="dev-c")     # dev-c asks to join
    hv, gov = _gov(home)
    import merkle
    pend = hv._pending_admissions(merkle.read_all_entries(str(home / "journal")), gov)
    assert any(r["device_id"] == "dev-c" for r in pend)            # pending before deny
    _run(home, "group", "deny", "dev-c")
    hv, gov = _gov(home)
    assert "dev-c" in gov["denied"]
    pend = hv._pending_admissions(merkle.read_all_entries(str(home / "journal")), gov)
    assert not any(r["device_id"] == "dev-c" for r in pend)        # gone from pending
    # admit overrides a prior deny
    _run(home, "group", "admit", "dev-c", "--principal", "carol")
    _, gov = _gov(home)
    assert "dev-c" in gov["admitted"] and "dev-c" not in gov["denied"]


def test_change_retags_principal_without_changing_admission(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "group", "admit", "dev-a", "--principal", "alice")
    _run(home, "group", "change", "dev-a", "--principal", "bob")
    _, gov = _gov(home)
    assert gov["principals"]["dev-a"] == "bob" and "dev-a" in gov["admitted"]
    # change on a non-admitted device is a no-op (CLI declines)
    r = _run(home, "group", "change", "dev-z", "--principal", "zed")
    assert "not an admitted device" in r.stdout.lower()
    _, gov = _gov(home)
    assert "dev-z" not in gov["principals"]


def test_group_list_shows_all_buckets(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "group", "admit", "dev-a", "--principal", "alice")
    _run(home, "join", "--principal", "pend", node_id="dev-p")
    _run(home, "group", "deny", "dev-d")
    _run(home, "group", "purge", "dev-x")
    out = _run(home, "group", "list").stdout
    assert "dev-a" in out and "alice" in out
    assert "dev-p" in out and "pending" in out.lower()
    assert "dev-d" in out and "denied" in out.lower()
    assert "dev-x" in out and "purged" in out.lower()


def test_aliases_still_work(tmp_path):
    """`hv admit`, `hv config set`, `hv key show` are kept as silent back-compat aliases."""
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "admit", "dev-a", "--principal", "alice")          # alias of `group admit`
    _run(home, "config", "set", "cap_self", "0.6")                # alias of `config confidence set`
    _, gov = _gov(home)
    assert "dev-a" in gov["admitted"] and gov["config"]["cap_self"] == 0.6
    assert _run(home, "key", "show").returncode == 0              # alias of `config identity show`
