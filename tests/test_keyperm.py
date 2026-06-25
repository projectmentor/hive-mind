"""The `keyperm` doctor check: raw device/owner seeds at rest must be 0600 — the threat model rests
on it. A group/other-readable key is a HARD doctor failure; `hv doctor --fix` re-tightens it."""

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


def test_fix_retightens(tmp_path):
    dk = _plant_key(tmp_path, ".owner-key", 0o660)
    subprocess.run([sys.executable, str(PROJECT / "hv"), "doctor", "--fix"],
                   env=dict(os.environ, HIVE_HOME=str(tmp_path)), capture_output=True, text=True)
    assert (dk.stat().st_mode & 0o777) == 0o600
    _rc, c = _doctor(tmp_path)
    assert c["status"] == "ok"
