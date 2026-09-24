"""`hive-mind update` (scripts/installer/_update.sh) run for real, in a sandbox (#93).

No CI job ran the updater before; `ci.yml` only lints it. These tests run it end to end with HOME,
HIVE_DIR and HIVE_HOME all in a temp dir, a real git remote (a bare repo built from this working
tree, so the pull is real), stub `systemctl`/`loginctl` on PATH (so nothing touches the real user
services), and a stub `/sync/hello` server on a free port set in the sandbox's `.peers.json`.

  (a) the store is locked by another process when the updater rebuilds: the update still finishes
      (units written, daemon restarted and answering, hooks wired), in that order, and the next `hv`
      command catches the store up;
  (b) a clean tree that differs from the signed manifest, with no re-sign arriving: the updater says
      the re-sign is pending and carries on;
  (c) the re-sign commit lands while the updater waits: it is pulled.
Plus the doctor's `authenticity` window verdict, as a pure table.

Linux only: the updater's service path is systemd there, which the stubs cover.
"""
import http.server
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("git") is None
                                or shutil.which("curl") is None, reason="needs Linux, git and curl")

GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _load_hv():
    loader = SourceFileLoader("hv_update_test", str(REPO / "hv"))
    spec = importlib.util.spec_from_loader("hv_update_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **GIT_ID})
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fix_manifest_digest(repo_dir):
    """Make verify.json's digest match the committed tree, so the updater sees no pending re-sign."""
    hv = _load_hv()
    vj = repo_dir / "verify.json"
    m = json.loads(vj.read_text())
    m["digest"] = hv._source_manifest(repo_dir)["digest"]
    vj.write_text(json.dumps(m, indent=2) + "\n")


class Sandbox:
    def __init__(self, tmp, fresh_manifest):
        self.tmp = tmp
        self.src, self.bare, self.hive, self.home = tmp / "src", tmp / "remote.git", tmp / "hive", tmp / "home"
        self.stub, self.log = tmp / "stub", tmp / "systemctl.log"
        # The remote: this working tree as one commit (uncommitted edits included).
        files = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard"],
                               capture_output=True, text=True, check=True).stdout.split("\n")
        for rel in filter(None, files):
            p = REPO / rel
            if p.is_file():
                (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, self.src / rel)
        _git(tmp, "init", "-q", "-b", "main", str(self.src))
        _git(self.src, "add", "-A")
        if fresh_manifest:
            _fix_manifest_digest(self.src)
            _git(self.src, "add", "-A")
        _git(self.src, "commit", "-q", "-m", "sandbox base")
        _git(tmp, "clone", "-q", "--bare", str(self.src), str(self.bare))
        _git(self.src, "remote", "add", "origin", str(self.bare))
        _git(tmp, "clone", "-q", str(self.bare), str(self.hive))
        # HOME with a Claude Code dir, so the updater wires hooks into the sandbox, not the real ~/.claude.
        (self.home / ".claude").mkdir(parents=True)
        # Stub service managers: log every call, succeed. `show-environment` succeeding makes the
        # systemd unit writer run (it skips when there is no user manager).
        self.stub.mkdir()
        for name in ("systemctl", "loginctl"):
            (self.stub / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit 0\n')
            (self.stub / name).chmod(0o755)
        self.port = _free_port()
        (self.hive / ".peers.json").write_text(json.dumps({"port": self.port, "peers": []}))

    def env(self, **extra):
        e = {**os.environ, **GIT_ID, "HOME": str(self.home), "HIVE_DIR": str(self.hive),
             "HIVE_HOME": str(self.hive), "PATH": f"{self.stub}:{os.environ['PATH']}",
             "HIVE_DAEMON_WAIT_S": "3", "HIVE_RESIGN_WAIT_S": "1", "HIVE_RESIGN_POLL_S": "1",
             "XDG_CONFIG_HOME": str(self.home / ".config")}
        e.pop("HIVE_UPDATE_REEXEC", None)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def hv(self, *args):
        return subprocess.run([sys.executable, str(self.hive / "hv"), *args], capture_output=True,
                              text=True, env=self.env(), timeout=120)

    def update(self, **extra):
        return subprocess.run(["bash", str(self.hive / "scripts" / "installer" / "_update.sh")],
                              capture_output=True, text=True, env=self.env(**extra), timeout=300)

    def serve_hello(self):
        """A stand-in daemon: answers GET /sync/hello on the sandbox port."""
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/sync/hello" else 404)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


@pytest.fixture
def sandbox(tmp_path):
    made = []

    def make(fresh_manifest=True):
        sb = Sandbox(tmp_path, fresh_manifest)
        made.append(sb.serve_hello())
        return sb
    yield make
    for srv in made:
        srv.shutdown()


def _hold_store_lock(db):
    """Another process holding the store's write lock (as the old daemon did, mid-rebuild)."""
    code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1], isolation_level=None); "
            "c.execute('BEGIN EXCLUSIVE'); print('locked', flush=True); sys.stdin.read()")
    p = subprocess.Popen([sys.executable, "-c", code, str(db)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


def test_a_locked_store_no_longer_aborts_the_update(sandbox):
    sb = sandbox()
    assert sb.hv("remember", "the sandbox hive has one fact", "--source", "test").returncode == 0
    db = sb.hive / "store.db"
    with sqlite3.connect(db) as c:                       # make the store deliberately stale
        c.execute("UPDATE meta SET value = 'stale' WHERE key = 'projected_journal_sig'")
    holder = _hold_store_lock(db)
    try:
        r = sb.update(HIVE_BUSY_TIMEOUT_MS=1)
    finally:
        holder.communicate("", timeout=10)
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "Rebuild skipped (store busy)" in out and "next cycle" in out
    assert "Daemon responding" in out and "Update complete" in out
    # units, then restart, then rebuild (#93's order), and the restart really happened
    assert out.index("Refreshing supervisor units") < out.index("Restarting sync daemon") \
        < out.index("Rebuilding database") < out.index("Re-asserting Claude Code hooks")
    assert "restart hive-sync" in sb.log.read_text()
    assert (sb.home / ".config" / "systemd" / "user" / "hive-sync.service").exists()
    assert "hive_dispatch.sh" in (sb.home / ".claude" / "settings.json").read_text()
    # the next hv command, with the lock gone, catches the stale store up
    assert sb.hv("search", "sandbox").returncode == 0
    with sqlite3.connect(db) as c:
        sig = c.execute("SELECT value FROM meta WHERE key = 'projected_journal_sig'").fetchone()[0]
    assert sig != "stale"


def test_b_a_pending_resign_is_reported_and_the_update_carries_on(sandbox):
    sb = sandbox(fresh_manifest=False)                   # this tree differs from the signed manifest
    r = sb.update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Manifest re-sign pending on GitHub" in r.stdout
    assert "Update complete" in r.stdout


def test_c_a_resign_that_lands_during_the_wait_is_pulled(sandbox):
    sb = sandbox(fresh_manifest=False)

    def resign():                                        # sign.yml's commit, a moment later
        time.sleep(1.5)
        _fix_manifest_digest(sb.src)
        _git(sb.src, "commit", "-q", "-am", "chore: re-sign source manifest")
        _git(sb.src, "push", "-q", "origin", "main")
    t = threading.Thread(target=resign)
    t.start()
    r = sb.update(HIVE_RESIGN_WAIT_S=30)
    t.join()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Release re-sign landed; pulled it" in r.stdout
    assert "re-sign pending" not in r.stdout
    assert _git(sb.hive, "rev-parse", "HEAD") == _git(sb.src, "rev-parse", "HEAD")


@pytest.mark.parametrize("clean, at_upstream, age_s, expected", [
    (True, True, 60, True),            # just merged, re-sign not landed yet: warn, with the hint
    (True, True, 1799, True),
    (True, True, 1800, False),         # still unsigned after the window: an unsigned push, fail
    (False, True, 60, False),          # a local edit
    (True, False, 60, False),          # a local commit, or not yet pulled
    (True, True, None, False),         # git could not say
    (True, True, -5, False),           # a commit from the future is not evidence of anything
])
def test_resign_window_verdict(clean, at_upstream, age_s, expected):
    assert _load_hv()._resign_window_verdict(clean, at_upstream, age_s) is expected


def test_resign_pending_hint_reads_the_checkout(sandbox):
    """The git facts behind the hint, on a real clone: fresh, clean and on upstream gives the hint;
    a local edit takes it away (the check then fails, as before #93)."""
    sb = sandbox(fresh_manifest=False)
    hv = _load_hv()
    hint = hv._resign_pending_hint(sb.hive)
    assert hint and "re-sign is probably still pending" in hint
    (sb.hive / "README.md").write_text("a local edit\n")
    assert hv._resign_pending_hint(sb.hive) is None
