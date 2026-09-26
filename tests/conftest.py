"""Shared test fixtures.

Every test drives the REAL `hv` CLI via subprocess against an isolated, temp
HIVE_HOME (the CLI honors $HIVE_HOME). No network, no shared state.

The suite is also proven not to touch the developer's real account (#109). Before #109 a run
from a git worktree relinked the live `~/.claude/skills/hive-memory` into that worktree, and any
test that ran `hv owner init` could overwrite the owner-key stash a reinstall restores from.
Every test now runs with:

  * HOME, CLAUDE_CONFIG_DIR and HIVE_IDENTITY_STASH pointed at a per-test temp dir (setenv, so a
    developer's own exported values are overridden too). HIVE_HOME is left alone: several test
    modules pin it at import, and each test already sets its own.
  * stub `pgrep`, `systemctl`, `launchctl` and `sv` first on PATH. `hv doctor --fix` finds
    "orphan" daemons with pgrep and restarts the managed hive-sync unit from two branches
    (orphans, and a sync-bind warning against the live daemon on 127.0.0.1:9876); the stubs log
    their argv to $HIVE_TEST_SERVICE_LOG, print nothing and exit 1, which is what a CI runner with
    no user service manager reports, so no test can restart the operator's daemon.

A session guard then checks the real paths, as the session started, are unchanged at the end:
the `skills/hive-memory` symlink, the Hive-owned hooks in `settings.json` (only those: Claude Code
rewrites the rest of the file itself), `settings.json.bak.doctor`, and the owner-key stash
(`.owner-key`). "Absent at the start" means "not created". A change in the live daemon's PID is
reported, not failed: hive-doctor.timer and a self-rebind restart it on their own. A test that
replaces PATH wholesale with a directory holding the real tools would bypass the stubs; don't.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import _realhome

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"

# The real paths as this session started, captured at import: before any fixture redirects HOME.
REAL = _realhome.session_paths()
SERVICE_STUBS = ("pgrep", "systemctl", "launchctl", "sv")
_DAEMON_NOTES = []


@pytest.fixture(scope="session")
def _service_stub_bin(tmp_path_factory):
    """One directory of service-manager stubs for the session; each logs to the per-test log."""
    d = tmp_path_factory.mktemp("service-stubs")
    for name in SERVICE_STUBS:
        stub = d / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "${{HIVE_TEST_SERVICE_LOG:-/dev/null}}"\nexit 1\n')
        stub.chmod(0o755)
    return d


@pytest.fixture(scope="session", autouse=True)
def _real_home_guard():
    """#109: the suite leaves the real account's Claude wiring and owner-key stash as it found them."""
    before = _realhome.snapshot(REAL["claude"], REAL["stash"], REAL["hive"])
    pid = _realhome.live_daemon_pid()
    yield
    after = _realhome.snapshot(REAL["claude"], REAL["stash"], REAL["hive"])
    new_pid = _realhome.live_daemon_pid()
    if pid and new_pid and pid != new_pid:
        _DAEMON_NOTES.append(f"the live hive-sync daemon restarted during the run (PID {pid} -> {new_pid}); "
                             "hive-doctor.timer or a self-rebind can do that on their own, so this is a note")
    changes = _realhome.diff(before, after)
    assert not changes, (f"the test suite touched the real account under {REAL['home']}: "
                         + "; ".join(changes))


@pytest.fixture(autouse=True)
def isolation(tmp_path_factory, monkeypatch, _service_stub_bin):
    """#109: every test runs against a throwaway HOME, Claude config dir and owner-key stash, with
    service-manager stubs first on PATH. Request it by name to get the paths."""
    root = tmp_path_factory.mktemp("iso")
    ns = SimpleNamespace(root=root, home=root / "home", claude=root / "home" / ".claude",
                         stash=root / "identity-stash", bin=_service_stub_bin,
                         service_log=root / "service-calls.log")
    ns.home.mkdir()
    monkeypatch.setenv("HOME", str(ns.home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ns.claude))
    monkeypatch.setenv("HIVE_IDENTITY_STASH", str(ns.stash))
    monkeypatch.setenv("HIVE_TEST_SERVICE_LOG", str(ns.service_log))
    monkeypatch.setenv("PATH", f"{_service_stub_bin}{os.pathsep}{os.environ['PATH']}")
    # A throwaway HOME hides the user's site-packages from child interpreters; keep them visible.
    if "PYTHONUSERBASE" not in os.environ:
        monkeypatch.setenv("PYTHONUSERBASE", REAL["userbase"])
    return ns


def pytest_terminal_summary(terminalreporter):
    for note in _DAEMON_NOTES:
        terminalreporter.write_line(f"note (#109): {note}")


class Hive:
    def __init__(self, home):
        self.home = Path(home)
        self.journal = self.home / "journal"
        self.db = self.home / "store.db"

    def run(self, *args, check=True):
        env = dict(os.environ, HIVE_HOME=str(self.home))
        r = subprocess.run(
            [sys.executable, str(HV), *args],
            env=env, capture_output=True, text=True,
        )
        if check and r.returncode != 0:
            raise AssertionError(f"`hv {' '.join(args)}` failed ({r.returncode}):\n{r.stderr}")
        return r

    def entries(self):
        out = []
        for f in sorted(self.journal.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


@pytest.fixture
def hive(tmp_path):
    return Hive(tmp_path)
