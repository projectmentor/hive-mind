"""`hv whoami` / `_self_membership` — a device's own membership standing.

Answers the question an agent on a member node asked live ("am I fertile or sterile?"):
  • no owner yet            → unaffiliated / no-hive
  • owner exists, not admitted → STERILE (read-only)
  • admitted                → FERTILE
  • holds the owner key     → OWNER
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _run(home, *args):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args],
                       env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _loadhv():
    loader = importlib.machinery.SourceFileLoader("hvmod_who", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_who", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_self_membership_states(tmp_path, monkeypatch):
    # Fresh home → NO owner key, so the owner-held branch is skipped and status is purely the
    # admitted-set projection. NODE_ID is fixed via the env override.
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    monkeypatch.setenv("HIVE_NODE_ID", "k1:member0000000a")
    hv = _loadhv()
    me = "k1:member0000000a"

    assert hv._self_membership({"owner_id": None, "admitted": set(), "principals": {}}) == ("no-hive", None)

    sterile = {"owner_id": "o1:x", "hive_id": "h1:x", "admitted": set(), "principals": {}}
    assert hv._self_membership(sterile) == ("sterile", None)

    fertile = {"owner_id": "o1:x", "hive_id": "h1:x", "admitted": {me}, "principals": {me: "bob"}}
    assert hv._self_membership(fertile) == ("fertile", "bob")


def test_whoami_owner(tmp_path):
    _run(tmp_path, "owner", "init")
    out = _run(tmp_path, "whoami").stdout
    assert "OWNER" in out
    assert "hive:" in out and "h1:" in out


def test_whoami_unaffiliated(tmp_path):
    out = _run(tmp_path, "whoami").stdout      # no owner established
    assert "UNAFFILIATED" in out
