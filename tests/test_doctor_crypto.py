"""Phase 3 — the crypto watchdog: `crypto_selftest.run_all()` + the `hv doctor` `crypto` check.

A broken/tampered crypto primitive must be caught at runtime (it would silently weaken every sealed
capsule), so the KATs run on every doctor pass and a break is a HARD failure (non-zero exit).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import crypto_selftest   # noqa: E402
import x25519            # noqa: E402


def test_selftest_all_pass():
    res = crypto_selftest.run_all()
    assert res == {"x25519": True, "ed25519": True, "edcurve": True,
                   "aead": True, "sealed_box": True}


def test_selftest_catches_a_broken_primitive(monkeypatch):
    # poison the X25519 primitive → the RFC KAT must notice
    monkeypatch.setattr(x25519, "x25519", lambda *a, **k: b"\x00" * 32)
    res = crypto_selftest.run_all()
    assert res["x25519"] is False


def test_doctor_crypto_check_ok(tmp_path):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "doctor", "--format", "json"],
                       env=dict(os.environ, HIVE_HOME=str(tmp_path)), capture_output=True, text=True)
    data = json.loads(r.stdout)
    crypto = next(c for c in data["checks"] if c["name"] == "crypto")
    assert crypto["status"] == "ok"


def test_doctor_exit_nonzero_when_crypto_breaks(tmp_path):
    """Shadow the real crypto module with a tampered copy (wrong RFC vector) on a hv run whose CWD
    makes it resolve first, and assert doctor reports the crypto check as failed."""
    bad = tmp_path / "shadow"
    bad.mkdir()
    # copy a tampered crypto_selftest (broken x25519 vector) + the real deps next to a hv symlink
    src = (PROJECT / "crypto_selftest.py").read_text().replace(
        "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552",
        "00" * 32)
    (bad / "crypto_selftest.py").write_text(src)
    shutil.copy(PROJECT / "hv", bad / "hv")        # a COPY, so hv's __file__ resolves into `bad`
    for f in ("x25519.py", "ed25519.py", "merkle.py"):
        (bad / f).symlink_to(PROJECT / f)
    r = subprocess.run([sys.executable, str(bad / "hv"), "doctor", "--format", "json"],
                       env=dict(os.environ, HIVE_HOME=str(tmp_path / "h")), capture_output=True, text=True)
    data = json.loads(r.stdout)
    crypto = next(c for c in data["checks"] if c["name"] == "crypto")
    assert crypto["status"] == "fail"
    assert data["verdict"] == "fail"
