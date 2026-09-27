"""The `keyperm` doctor check: raw device/owner seeds at rest must be 0600 — the threat model rests
on it. A group/other-readable key is a HARD doctor failure. `hv doctor --fix` re-tightens the DEVICE key,
and `hive-mind doctor --fix` the owner key too: the owner key is operator state (2.0, public #136)."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent

if os.name == "nt":
    pytest.skip("POSIX file modes only", allow_module_level=True)


def _doctor(home):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "doctor", "--format", "json"],
                       env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)
    checks = json.loads(r.stdout)["checks"]
    return r.returncode, next(c for c in checks if c["name"] == "keyperm")


def _plant_key(home, name, mode):
    p = home / name
    p.write_text(base64.b64encode(bytes(range(32))).decode() + "\n")
    os.chmod(p, mode)
    return p


def test_tight_key_passes(tmp_path):
    _plant_key(tmp_path, ".device-key", 0o600)
    _rc, c = _doctor(tmp_path)
    assert c["status"] == "ok"


def test_leaky_key_is_hard_fail(tmp_path):
    _plant_key(tmp_path, ".device-key", 0o644)
    rc, c = _doctor(tmp_path)
    assert c["status"] == "fail"
    assert rc == 1                                  # a leaky key sets doctor's non-zero exit


def _fix(home, script):
    return subprocess.run([sys.executable, str(PROJECT / script), "doctor", "--fix"],
                          env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)


def test_fix_retightens_the_device_key_on_hv(tmp_path):
    """The 15-minute timer runs `hv doctor --fix`, so an agent-only node still repairs its own key."""
    dk = _plant_key(tmp_path, ".device-key", 0o660)
    _fix(tmp_path, "hv")
    assert (dk.stat().st_mode & 0o777) == 0o600
    _rc, c = _doctor(tmp_path)
    assert c["status"] == "ok"


def test_fix_retightens_the_owner_key_only_on_the_control_plane(tmp_path):
    ok = _plant_key(tmp_path, ".owner-key", 0o660)
    r = _fix(tmp_path, "hv")
    assert (ok.stat().st_mode & 0o777) == 0o660, "hv must not touch the owner key (2.0 S2)"
    assert "hive-mind doctor --fix" in r.stdout, "hv says where the repair lives"
    _fix(tmp_path, "hivemind_ctl.py")
    assert (ok.stat().st_mode & 0o777) == 0o600
    _rc, c = _doctor(tmp_path)
    assert c["status"] == "ok"
