"""`hive-mind <install|update|status|invite> --help` prints usage and changes nothing (#224).

`update --help` used to run the update: the dispatcher passed "$@" to `_update.sh`, which never read its
arguments. Each test runs the real dispatcher in a scratch HOME with stub `git`/`curl`/`systemctl`/`tailscale`
on PATH that record any call; help must exit 0, print usage, leave HOME untouched and call none of them. An
unknown argument exits 2 the same way. Never run against the live checkout: HOME and HIVE_HOME are scratch.
"""
import os
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
DISPATCHER = PROJECT / "scripts" / "installer" / "dispatcher.sh"
SUBCOMMANDS = ("install", "update", "status", "invite", "reset", "uninstall")
STUBBED = ("git", "curl", "systemctl", "tailscale", "launchctl", "sv", "pip", "uv")


def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))


def _run(tmp_path, *argv):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "stubs"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    for name in STUBBED:
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 1\n')
        stub.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "HIVE_HOME": str(home / "hive"), "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("HIVE_DIR", None)
    before = _snapshot(home)
    r = subprocess.run(["bash", str(DISPATCHER), *argv], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    assert _snapshot(home) == before, "HOME changed"
    assert not calls.exists(), f"help ran a command: {calls.read_text()}"
    return r


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("cmd", SUBCOMMANDS)
def test_help_prints_usage_and_changes_nothing(tmp_path, cmd, flag):
    r = _run(tmp_path, cmd, flag)
    assert r.returncode == 0, r.stderr
    assert f"hive-mind {cmd}" in r.stdout and "sage" in r.stdout


@pytest.mark.parametrize("cmd", ("install", "update", "status", "invite"))
def test_unknown_argument_exits_2_and_changes_nothing(tmp_path, cmd):
    r = _run(tmp_path, cmd, "--bogus")
    assert r.returncode == 2
    assert "--bogus" in r.stderr
