"""The suite runs sealed off from the developer's real account (#109).

conftest.py's autouse `isolation` fixture redirects HOME, CLAUDE_CONFIG_DIR and
HIVE_IDENTITY_STASH and puts service-manager stubs first on PATH; its session guard checks the
real paths at the end. These tests prove the two writes that used to escape now land in the
sandbox, and that the guard's comparison really detects such writes (on a stand-in home, never the
real one). Assertions read the stub log, never doctor's text: `_restart_managed_daemon` reports
success without checking an exit code, so its message says nothing about what actually ran.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _realhome
from conftest import HV, PROJECT, REAL, SERVICE_STUBS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells and symlinks")


def _hv(env, *args):
    return subprocess.run([sys.executable, str(HV), *args], env=env, capture_output=True, text=True,
                          timeout=120)


def test_children_see_the_sandbox_and_the_stubs(isolation):
    code = ("import os, shutil, sys; print(repr({k: os.environ.get(k) for k in "
            "('HOME', 'CLAUDE_CONFIG_DIR', 'HIVE_IDENTITY_STASH')})); "
            f"print(repr({{t: shutil.which(t) for t in {SERVICE_STUBS!r}}}))")
    out = subprocess.run([sys.executable, "-c", code], env=dict(os.environ), capture_output=True,
                         text=True, check=True).stdout.splitlines()
    env_seen, tools = eval(out[0]), eval(out[1])
    for key, value in env_seen.items():
        assert value and Path(value).is_relative_to(isolation.root), (key, value)
    for tool, path in tools.items():
        assert path and Path(path).parent == isolation.bin, (tool, path)


def test_owner_init_writes_the_sandbox_stash_not_the_real_one(isolation, tmp_path):
    before = _realhome.snapshot(REAL["claude"], REAL["stash"])
    r = _hv(dict(os.environ, HIVE_HOME=str(tmp_path / "hive")), "owner", "init")
    assert r.returncode == 0, r.stderr
    assert (isolation.stash / ".owner-key").is_file()            # the write happened, in the sandbox
    assert _realhome.diff(before, _realhome.snapshot(REAL["claude"], REAL["stash"])) == []


def test_doctor_fix_wires_the_sandbox_and_only_reaches_the_stubs(isolation, tmp_path):
    isolation.claude.mkdir()                                     # a Claude Code install to wire
    before = _realhome.snapshot(REAL["claude"], REAL["stash"])
    _hv(dict(os.environ, HIVE_HOME=str(tmp_path / "hive")), "doctor", "--fix")
    # Load-bearing: the write LANDED in the sandbox. (From a checkout the real link already points
    # at, --fix writes nothing real, so "real side unchanged" alone would pass vacuously.)
    link = isolation.claude / "skills" / "hive-memory"
    assert link.is_symlink() and link.resolve() == (PROJECT / "integrations/claude-code/hive-memory").resolve()
    hooks = _realhome.snapshot(isolation.claude, isolation.stash)["hive hooks in settings.json"]
    assert hooks, "no Hive hooks were wired into the sandbox settings.json"
    assert _realhome.diff(before, _realhome.snapshot(REAL["claude"], REAL["stash"])) == []
    # The orphan scan ran, and it ran against the stub.
    calls = isolation.service_log.read_text().splitlines()
    assert any(c.startswith("pgrep ") for c in calls), calls


def test_the_guard_detects_writes_to_a_home_it_is_watching(isolation, tmp_path):
    """Fail-closed proof, on a stand-in home: with the redirect undone (HOME pointed at a second
    temp dir playing the real one), `owner init` and `doctor --fix` write there, and the same
    comparison the session guard uses reports both. The stubs stay on PATH throughout."""
    standin = tmp_path / "standin-home"
    (standin / ".claude").mkdir(parents=True)
    claude, stash = standin / ".claude", standin / ".config" / "hive-mind" / "identity"
    before = _realhome.snapshot(claude, stash)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HIVE_IDENTITY_STASH")}
    env.update(HOME=str(standin), HIVE_HOME=str(tmp_path / "hive"))
    assert _hv(env, "owner", "init").returncode == 0
    _hv(env, "doctor", "--fix")
    changes = _realhome.diff(before, _realhome.snapshot(claude, stash))
    assert "owner-key stash created" in changes, changes
    assert "skill link created" in changes, changes
    assert "hive hooks in settings.json created" in changes, changes


def test_diff_reports_unchanged_as_nothing_and_names_each_change(tmp_path):
    claude, stash = tmp_path / "c", tmp_path / "s"
    claude.mkdir()
    stash.mkdir()
    a = _realhome.snapshot(claude, stash)
    assert _realhome.diff(a, _realhome.snapshot(claude, stash)) == []
    (stash / ".owner-key").write_text("x\n")
    (claude / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "/x/hive_dispatch.sh session-start"}]}]}}))
    b = _realhome.snapshot(claude, stash)
    assert _realhome.diff(a, b) == ["hive hooks in settings.json created", "owner-key stash created"]
    # Claude Code rewriting the non-Hive part of settings.json is not a change the guard reports.
    cfg = json.loads((claude / "settings.json").read_text())
    cfg["permissions"] = {"allow": ["Bash(ls)"]}
    (claude / "settings.json").write_text(json.dumps(cfg))
    assert _realhome.diff(b, _realhome.snapshot(claude, stash)) == []
    (stash / ".owner-key").write_text("y\n")
    assert _realhome.diff(b, _realhome.snapshot(claude, stash)) == ["owner-key stash changed"]


def test_a_bak_doctor_rewrite_with_the_same_content_and_mtime_is_still_seen(tmp_path):
    """shutil.copy2 (how hv writes settings.json.bak.doctor) copies the source's mtime, and a --fix
    on unchanged settings writes identical bytes, so only the inode change time shows the write."""
    claude, stash = tmp_path / "c", tmp_path / "s"
    claude.mkdir()
    stash.mkdir()
    (claude / "settings.json").write_text("{}\n")
    shutil.copy2(claude / "settings.json", claude / "settings.json.bak.doctor")
    a = _realhome.snapshot(claude, stash)
    time.sleep(0.2)                              # past the filesystem's timestamp granularity
    shutil.copy2(claude / "settings.json", claude / "settings.json.bak.doctor")   # same bytes, same mtime
    assert _realhome.diff(a, _realhome.snapshot(claude, stash)) == ["settings.json.bak.doctor changed"]
