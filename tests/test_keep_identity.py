"""The reusable device-identity primitive (scripts/installer/_identity.sh): stash this device's
keys to a stable location, then restore them into a fresh install — so a reinstall resumes the
same device_id and the owner's prior admission still applies (no re-admit). Drives the bash
functions directly against temp dirs."""

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LIB = PROJECT / "scripts" / "installer" / "_identity.sh"


def _bash(script, **env):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=dict(os.environ, **env))


def test_save_then_restore_round_trip(tmp_path):
    stash, hive = tmp_path / "stash", tmp_path / "hive"
    hive.mkdir()
    (hive / ".device-key").write_text("SEED-KEY-XYZ\n")
    (hive / ".device-id").write_text("k1:abc123\n")
    (hive / ".owner-key").write_text("OWNER-SEED\n")

    script = f"""
      set -e
      . "{LIB}"
      keep_identity_save "{hive}"                # stash the identity
      rm -rf "{hive}" && mkdir -p "{hive}"       # simulate uninstall + a fresh reinstall (no key)
      keep_identity_can_restore "{hive}"         # must be true now
      [ "$(keep_identity_stashed_id)" = "k1:abc123" ]
      keep_identity_restore "{hive}"
    """
    r = _bash(script, HIVE_IDENTITY_STASH=str(stash))
    assert r.returncode == 0, r.stderr
    # same identity comes back → same device_id → admission still applies
    assert (hive / ".device-key").read_text() == "SEED-KEY-XYZ\n"
    assert (hive / ".device-id").read_text() == "k1:abc123\n"
    assert (hive / ".owner-key").read_text() == "OWNER-SEED\n"


def test_can_restore_false_when_install_already_has_a_key(tmp_path):
    stash, hive, src = tmp_path / "stash", tmp_path / "hive", tmp_path / "src"
    hive.mkdir(); src.mkdir()
    (hive / ".device-key").write_text("EXISTING\n")
    (src / ".device-key").write_text("STASHED\n")
    assert _bash(f'. "{LIB}"; keep_identity_save "{src}"', HIVE_IDENTITY_STASH=str(stash)).returncode == 0
    # refuse to restore over a live key (no clobber)
    r = _bash(f'. "{LIB}"; keep_identity_can_restore "{hive}"', HIVE_IDENTITY_STASH=str(stash))
    assert r.returncode != 0
    assert (hive / ".device-key").read_text() == "EXISTING\n"


def test_save_is_noop_without_a_key(tmp_path):
    stash, empty = tmp_path / "stash", tmp_path / "empty"
    empty.mkdir()
    r = _bash(f'. "{LIB}"; keep_identity_save "{empty}"', HIVE_IDENTITY_STASH=str(stash))
    assert r.returncode != 0                       # nothing to save → returns 1
    assert not (stash / ".device-key").exists()
