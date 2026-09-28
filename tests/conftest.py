"""Shared test fixtures.

Every test drives the REAL `hv` CLI via subprocess against an isolated, temp
HIVE_HOME (the CLI honors $HIVE_HOME). No network, no shared state.

The suite is also proven not to touch the developer's real account (#109). Before #109 a run
from a git worktree relinked the live `~/.claude/skills/hive-memory` into that worktree, and any
test that ran `hv owner init` could overwrite the owner-key stash a reinstall restores from.
Every test now runs with:

  * HOME, CLAUDE_CONFIG_DIR, HIVE_IDENTITY_STASH and HIVE_HOME pointed at a per-test temp dir (setenv,
    so a developer's own exported values are overridden too). HOME and HIVE_HOME are ALSO replaced at
    import with session sandboxes (#160; HOME since 2.0 PR 3a, whose default key directory is under it),
    before any test module loads `hv` or any module-scoped fixture writes a key: `hv` resolves every path from it when
    imported, and an in-process loader that did not set its own used to read the developer's exported
    hive (its genesis pin, keys and store). A loader that forgets now gets an empty sandbox, as in CI;
    one that means a particular hive says so (`_planes.load_hv(home)`).
  * stub `pgrep`, `systemctl`, `launchctl` and `sv` first on PATH. `hv doctor --fix` finds
    "orphan" daemons with pgrep and restarts the managed hive-sync unit from two branches
    (orphans, and a sync-bind warning against the live daemon on 127.0.0.1:9876); the stubs log
    their argv to $HIVE_TEST_SERVICE_LOG, print nothing and exit 1, which is what a CI runner with
    no user service manager reports, so no test can restart the operator's daemon.

A session guard then checks the real paths, as the session started, are unchanged at the end:
the `skills/hive-memory` symlink, the Hive-owned hooks in `settings.json` (only those: Claude Code
rewrites the rest of the file itself), `settings.json.bak.doctor`, the owner-key stash (`.owner-key`),
and the exported hive's genesis pin and key files (#160). "Absent at the start" means "not created". A
change in the live daemon's PID, or in the live hive's journal or store, is reported, not failed: the
timer, a self-rebind, peer sync and other sessions change those on their own. A hive marked as a decoy
(`_realhome.DECOY_MARKER`) is held to byte-identical instead, journal and store included. A test that
replaces PATH wholesale with a directory holding the real tools would bypass the stubs; don't.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import _realhome

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"

# Under xdist the controller imports this file before it starts the workers, and each worker inherits its
# environment, sandboxed HIVE_HOME included. So the controller (or a serial run) records the hive that was
# exported, and a worker reads that instead: without it every worker's guard watched the controller's
# sandbox and could never fail (Fable on #165).
if not os.environ.get("PYTEST_XDIST_WORKER"):
    os.environ[_realhome.EXPORTED_HOME_VAR] = os.environ.get("HIVE_HOME", "")
    os.environ[_realhome.REAL_HOME_VAR] = _realhome.real_home()
# The real paths as this session started, captured at import: before any fixture redirects HOME.
REAL = _realhome.session_paths()
# #160: then a sandbox HIVE_HOME for the session, still at import, so a test module that loads `hv` at its
# own import (and a helper that never sets HIVE_HOME) resolves into this, never the exported hive.
SANDBOX_HIVE = Path(tempfile.mkdtemp(prefix="hive-test-sandbox-"))
os.environ["HIVE_HOME"] = str(SANDBOX_HIVE)
# HOME too (2.0 PR 3a; Fable on #166). `hv`'s default key directory is ~/.hive/keys/<id>, and a
# module-scoped fixture or a module-level `hv` loader runs before `isolation` patches HOME, so a key written
# then went into the developer's real ~/.hive/keys. Child interpreters keep the user's site-packages.
SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="hive-test-home-"))
os.environ.setdefault("PYTHONUSERBASE", REAL["userbase"])
os.environ["HOME"] = str(SANDBOX_HOME)
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
    before = _realhome.snapshot(REAL["claude"], REAL["stash"], REAL["hive"], REAL["key_dir"], REAL["keys_root"])
    activity = _realhome.activity(REAL["hive"])
    decoy = _realhome.decoy_state(REAL["hive"])
    pid = _realhome.live_daemon_pid()
    yield
    after = _realhome.snapshot(REAL["claude"], REAL["stash"], REAL["hive"], REAL["key_dir"], REAL["keys_root"])
    new_pid = _realhome.live_daemon_pid()
    if pid and new_pid and pid != new_pid:
        _DAEMON_NOTES.append(f"the live hive-sync daemon restarted during the run (PID {pid} -> {new_pid}); "
                             "hive-doctor.timer or a self-rebind can do that on their own, so this is a note")
    moved = sorted(k for k, v in _realhome.activity(REAL["hive"]).items() if activity.get(k) != v)
    if moved and decoy is None:
        _DAEMON_NOTES.append(f"the live hive's {', '.join(moved)} changed during the run; its daemon and other "
                             "sessions write those, so this is a note (#160)")
    changes = _realhome.diff(before, after)
    if decoy is not None:
        changes += _realhome.decoy_diff(decoy, _realhome.decoy_state(REAL["hive"]) or {})
    assert not changes, (f"the test suite touched the real account under {REAL['home']}: "
                         + "; ".join(changes))


OWNER_KEY_PASSPHRASE = "test-owner-key-passphrase"


@pytest.fixture(scope="session", autouse=True)
def _sealed_owner_key_passphrase():
    """The owner key is sealed at rest (2.0 PR 3b), so every `hive-mind` command that owner-signs unlocks
    it. One passphrase for the whole session, from the environment, set before any module-scoped fixture
    runs `owner init`. `isolation` re-sets it per test, so a test that removes it cannot leak."""
    mp = pytest.MonkeyPatch()
    mp.setenv("HIVE_OWNER_KEY_PASSPHRASE", OWNER_KEY_PASSPHRASE)
    yield OWNER_KEY_PASSPHRASE
    mp.undo()


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
    monkeypatch.setenv("HIVE_HOME", str(root / "hive"))       # #160: never the exported hive
    monkeypatch.setenv("HIVE_OWNER_KEY_PASSPHRASE", OWNER_KEY_PASSPHRASE)     # the sealed owner key (PR 3b)
    monkeypatch.setenv("PATH", f"{_service_stub_bin}{os.pathsep}{os.environ['PATH']}")
    # A throwaway HOME hides the user's site-packages from child interpreters; keep them visible.
    if "PYTHONUSERBASE" not in os.environ:
        monkeypatch.setenv("PYTHONUSERBASE", REAL["userbase"])
    return ns


def pytest_unconfigure(config):
    shutil.rmtree(SANDBOX_HIVE, ignore_errors=True)
    shutil.rmtree(SANDBOX_HOME, ignore_errors=True)


def pytest_terminal_summary(terminalreporter):
    for note in _DAEMON_NOTES:
        terminalreporter.write_line(f"note (#109): {note}")


class Hive:
    def __init__(self, home):
        self.home = Path(home)
        self.journal = self.home / "journal"
        self.db = self.home / "store.db"

    def run(self, *args, check=True):
        # A command that moved off `hv` in 2.0 runs where it now lives, `hive-mind` (tests/_planes.py).
        import _planes
        env = dict(os.environ, HIVE_HOME=str(self.home))
        r = subprocess.run(
            [sys.executable, str(_planes.entry_for(args)), *args],
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
