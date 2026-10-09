"""`hive-mind update` (scripts/installer/_update.sh) run for real, in a sandbox (#93).

No CI job ran the updater before; `ci.yml` only lints it. These tests run it end to end with HOME,
HIVE_DIR and HIVE_HOME all in a temp dir, a real git remote (a bare repo built from this working
tree, so the pull is real), stub `systemctl`/`loginctl` on PATH (so nothing touches the real user
services), and a stub `/sync/hello` server on a free port set in the sandbox's `.peers.json`.

  (a) the store is locked by another process when the updater rebuilds: the update still finishes
      (units written, daemon restarted and answering, hooks wired), in that order, and the next `hv`
      command catches the store up;
  (b) upstream's newest commit differs from its signed manifest, with no re-sign arriving: the updater
      refuses it, says the re-sign is pending, and changes nothing;
  (c) the re-sign commit lands while the updater waits: that one is verified and pulled.
Verify, then switch: a commit that is unsigned, signed by another key, ships another key, or is an unsigned
`[skip ci]` main is REFUSED and leaves the branch and the tree exactly as they were (nothing of the fetched
tree is run); `--allow-unsigned` overrides. The sandbox signs with a test key that the installed tree pins.
Plus the doctor's `authenticity` window verdict, as a pure table.

Linux only: the updater's service path is systemd there, which the stubs cover.
"""
import base64
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


SEED = bytes(range(32))             # the sandbox's release key: the installed tree pins its public half
OTHER_SEED = bytes(range(1, 33))    # any other key


def _ed():
    spec = importlib.util.spec_from_file_location("ed25519_update_test", REPO / "ed25519.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sign(repo_dir, seed=SEED, pub_seed=SEED, sig=True):
    """Sign verify.json's exact bytes with `seed`; ship `pub_seed`'s public key as hivemind.pub."""
    ed = _ed()
    (repo_dir / "hivemind.pub").write_text(base64.b64encode(ed.pub_from_seed(pub_seed)).decode() + "\n")
    sigf = repo_dir / "verify.json.sig"
    if sig:
        sigf.write_text(base64.b64encode(ed.sign((repo_dir / "verify.json").read_bytes(), seed)).decode() + "\n")
    elif sigf.exists():
        sigf.unlink()


def _fix_manifest_digest(repo_dir, **sign):
    """Make verify.json's digest match the committed tree and sign it, so the updater sees a good release."""
    hv = _load_hv()
    vj = repo_dir / "verify.json"
    m = json.loads(vj.read_text())
    m["digest"] = hv._source_manifest(repo_dir)["digest"]
    vj.write_text(json.dumps(m, indent=2) + "\n")
    _sign(repo_dir, **sign)


def _stale_manifest_digest(repo_dir):
    """Make verify.json's digest deliberately wrong. The working tree's own verify.json can't be relied
    on to be stale: sign.yml regenerates it before it runs this suite (which broke the re-sign of
    99db973 and ae6d8db)."""
    vj = repo_dir / "verify.json"
    m = json.loads(vj.read_text())
    m["digest"] = "0" * 64
    vj.write_text(json.dumps(m, indent=2) + "\n")
    _sign(repo_dir)


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
        (_fix_manifest_digest if fresh_manifest else _stale_manifest_digest)(self.src)
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

    def upstream_commit(self, stale=False, force=False, **sign):
        """A new commit on the remote's main: a code change (README line) plus its manifest, `sign` as for
        _sign (seed, pub_seed, sig), or a deliberately stale digest (an unsigned `[skip ci]` squash)."""
        n = len(_git(self.src, "log", "--format=%H").split())
        with open(self.src / "README.md", "a") as fh:
            fh.write(f"\nrelease note {n}\n")
        (_stale_manifest_digest if stale else _fix_manifest_digest)(self.src, **({} if stale else sign))
        _git(self.src, "add", "-A")
        _git(self.src, "commit", "-q", "-m", f"change {n}")
        _git(self.src, "push", "-q", *(["-f"] if force else []), "origin", "main")
        return _git(self.src, "rev-parse", "HEAD")

    def state(self):
        """Everything a refused update must leave alone: branch, HEAD, the status and the files."""
        files = {str(p.relative_to(self.hive)): p.read_bytes() for p in sorted(self.hive.rglob("*"))
                 if p.is_file() and ".git" not in p.relative_to(self.hive).parts}
        return (_git(self.hive, "rev-parse", "--abbrev-ref", "HEAD"), _git(self.hive, "rev-parse", "HEAD"),
                _git(self.hive, "status", "--porcelain"), _git(self.hive, "stash", "list"), files)

    def env(self, **extra):
        e = {**os.environ, **GIT_ID, "HOME": str(self.home), "HIVE_DIR": str(self.hive),
             "HIVE_HOME": str(self.hive), "PATH": f"{self.stub}:{os.environ['PATH']}",
             "HIVE_DAEMON_WAIT_S": "3", "HIVE_RESIGN_WAIT_S": "1", "HIVE_RESIGN_POLL_S": "1",
             "XDG_CONFIG_HOME": str(self.home / ".config"),
             "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),       # pinned: an inherited one would win
             "HIVE_IDENTITY_STASH": str(self.tmp / "identity-stash")}
        e.pop("HIVE_UPDATE_REEXEC", None)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def hv(self, *args):
        return subprocess.run([sys.executable, str(self.hive / "hv"), *args], capture_output=True,
                              text=True, env=self.env(), timeout=120)

    def update(self, args=(), **extra):
        return subprocess.run(["bash", str(self.hive / "scripts" / "installer" / "_update.sh"), *args],
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


def test_a_signed_release_is_verified_and_pulled(sandbox):
    sb = sandbox()
    new = sb.upstream_commit()
    r = sb.update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Signature valid under the pinned key" in r.stdout and "Update complete" in r.stdout
    assert _git(sb.hive, "rev-parse", "HEAD") == new
    assert r.stdout.count("Signature valid") == 1                 # the re-exec'd copy has nothing left to switch


def test_b_a_pending_resign_is_refused_and_nothing_changes(sandbox):
    sb = sandbox()
    sb.upstream_commit(stale=True)                               # main as `[skip ci]` left it: code, old manifest
    before = sb.state()
    r = sb.update()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "waiting up to" in r.stdout and "REFUSED" in r.stderr and "(modified)" in r.stderr
    assert "run 'hive-mind update' again in a few minutes" in r.stderr
    assert "Update complete" not in r.stdout and "Restarting sync daemon" not in r.stdout
    assert sb.state() == before


def test_c_a_resign_that_lands_during_the_wait_is_pulled(sandbox):
    sb = sandbox()
    sb.upstream_commit(stale=True)

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
    assert "Signature valid under the pinned key" in r.stdout
    assert _git(sb.hive, "rev-parse", "HEAD") == _git(sb.src, "rev-parse", "HEAD")


# Verify, then switch: every way a fetched tree can fail the check refuses it and changes nothing.
REFUSED = [
    pytest.param(dict(sig=False), "unsigned", id="unsigned-tree"),
    pytest.param(dict(seed=OTHER_SEED), "sig_invalid", id="signed-by-another-key-under-the-pinned-pub"),
    pytest.param(dict(seed=OTHER_SEED, pub_seed=OTHER_SEED), "key_changed", id="ships-and-uses-its-own-key"),
    pytest.param(dict(pub_seed=OTHER_SEED), "key_changed", id="valid-signature-but-a-swapped-hivemind.pub"),
    pytest.param(dict(stale=True), "modified", id="unsigned-skip-ci-main"),
]


@pytest.mark.parametrize("kw, level", REFUSED)
def test_a_tree_that_fails_the_check_is_refused_and_left_untouched(sandbox, kw, level):
    sb = sandbox()
    sb.upstream_commit(**kw)
    marker = sb.tmp / "fetched-code-ran"
    before = sb.state()
    r = sb.update(HIVE_RESIGN_WAIT_S=1)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr and f"({level})" in r.stderr and "--allow-unsigned" in r.stderr
    assert "Update complete" not in r.stdout and "Pinned this hive" not in r.stdout
    assert "Restarting sync daemon" not in r.stdout and not sb.log.exists()      # no unit, no restart
    assert sb.state() == before and not marker.exists()


@pytest.mark.parametrize("stray", ["__pycache__/merkle.cpython-312.pyc", "journal/x.log", "tools/run"],
                         ids=["tracked-pyc", "tracked-journal-file", "extensionless-file"])
def test_a_tracked_file_outside_the_manifest_is_refused_even_under_a_good_signature(sandbox, stray):
    sb = sandbox()
    marker = sb.tmp / "fetched-code-ran"
    f = sb.src / stray
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    _git(sb.src, "add", "-f", stray)
    _fix_manifest_digest(sb.src)                  # the digest ignores the stray file, so this stays signed
    _git(sb.src, "add", "-A")
    _git(sb.src, "commit", "-q", "-m", "stray tracked file")
    _git(sb.src, "push", "-q", "origin", "main")
    before = sb.state()
    r = sb.update(HIVE_RESIGN_WAIT_S=1)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr and "(unsigned_files)" in r.stderr
    assert "Update complete" not in r.stdout and sb.state() == before and not marker.exists()


def test_the_fetched_hv_is_never_run_to_judge_itself(sandbox):
    """The fetched tree's own `hv` would say whatever it likes; the installed one judges."""
    sb = sandbox()
    marker = sb.tmp / "fetched-code-ran"
    hv_src = sb.src / "hv"
    hv_src.write_text(hv_src.read_text() + f"\nopen({str(marker)!r}, 'w').write('ran')\n")
    sb.upstream_commit(sig=False)
    r = sb.update()
    assert r.returncode == 1 and "REFUSED" in r.stderr, r.stdout + r.stderr
    assert not marker.exists()


@pytest.mark.parametrize("kw", [dict(sig=False), dict(stale=True), dict(seed=OTHER_SEED, pub_seed=OTHER_SEED)])
def test_allow_unsigned_installs_it_anyway_and_says_so(sandbox, kw):
    sb = sandbox()
    new = sb.upstream_commit(**kw)
    r = sb.update(args=["--allow-unsigned"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "because of --allow-unsigned" in r.stdout and "Update complete" in r.stdout
    assert _git(sb.hive, "rev-parse", "HEAD") == new


def _installed_one_ahead(sb):
    """The node has updated to a signed commit B1 (base B0 -> B1). Returns (B0, B1)."""
    b0 = _git(sb.hive, "rev-parse", "HEAD")
    b1 = sb.upstream_commit()
    _git(sb.hive, "pull", "-q", "--ff-only")
    assert _git(sb.hive, "rev-parse", "HEAD") == b1
    return b0, b1


def _refused_as_rewind(sb, r, before, b0, b1, what):
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr and what in r.stderr and "--allow-rewind" in r.stderr
    assert b0[:12] in r.stderr and b1[:12] in r.stderr          # both shas are named
    assert "older or rewritten tree is refused" in r.stderr
    assert "Update complete" not in r.stdout and "Restarting sync daemon" not in r.stdout and not sb.log.exists()
    assert sb.state() == before


def test_a_rollback_to_an_older_signed_commit_is_refused_and_left_untouched(sandbox):
    sb = sandbox()
    b0, b1 = _installed_one_ahead(sb)
    _git(sb.src, "reset", "-q", "--hard", b0)                    # origin/main goes back to B0, still validly signed
    _git(sb.src, "push", "-q", "-f", "origin", "main")
    before = sb.state()
    r = sb.update()
    _refused_as_rewind(sb, r, before, b0, b1, "an OLDER commit")
    assert _git(sb.hive, "rev-parse", "HEAD") == b1


def test_a_diverged_rewrite_is_refused_without_the_flag_and_reset_with_it(sandbox):
    sb = sandbox()
    b0, b1 = _installed_one_ahead(sb)
    _git(sb.src, "reset", "-q", "--hard", b0)
    new = sb.upstream_commit(force=True)                         # B0 -> B1': same parent, other history
    assert new != b1
    before = sb.state()
    r = sb.update()
    _refused_as_rewind(sb, r, before, new, b1, "a rewritten history")
    r = sb.update(args=["--allow-rewind"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hard-resetting" in r.stdout and "Update complete" in r.stdout
    assert _git(sb.hive, "rev-parse", "HEAD") == new


def test_allow_rewind_does_not_waive_the_signature_check(sandbox):
    sb = sandbox()
    b0, b1 = _installed_one_ahead(sb)
    _git(sb.src, "reset", "-q", "--hard", b0)
    sb.upstream_commit(force=True, sig=False)
    before = sb.state()
    r = sb.update(args=["--allow-rewind"])
    assert r.returncode == 1 and "(unsigned)" in r.stderr, r.stdout + r.stderr
    assert sb.state() == before


def test_a_normal_fast_forward_needs_no_rewind_flag(sandbox):
    sb = sandbox()
    b0, b1 = _installed_one_ahead(sb)
    new = sb.upstream_commit()
    r = sb.update()
    assert r.returncode == 0 and "REFUSED" not in r.stderr, r.stdout + r.stderr
    assert _git(sb.hive, "rev-parse", "HEAD") == new


def test_no_pinned_key_in_the_installed_tree_refuses(sandbox):
    sb = sandbox()
    sb.upstream_commit()
    (sb.hive / "hivemind.pub").unlink()                          # untracked-from-the-manifest, so the tree stays clean
    _git(sb.hive, "update-index", "--assume-unchanged", "hivemind.pub")
    before = sb.state()
    r = sb.update()
    assert r.returncode == 1 and "(no_pin)" in r.stderr, r.stdout + r.stderr
    assert sb.state() == before


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


# ── 4c: the update's last word on the pre-genesis forget grandfather (decision h:696638b9b7) ─────────

def _ctl(sb, *args):
    return subprocess.run([sys.executable, str(sb.hive / "hivemind_ctl.py"), *args], capture_output=True,
                          text=True, env=sb.env(), timeout=120)


def _backdated_forget(sb, text):
    """A fact, then an unsigned owner-source forget of it dated before genesis (#122's shape)."""
    assert sb.hv("remember", text, "--source", "test").returncode == 0
    ref = [x for x in json.loads(sb.hv("search", text.split()[1], "--format", "json").stdout)
           if x.get("kind") == "fact"][0]["ref"]
    node, seq = ref.rsplit(":", 1)
    jf = sorted((sb.hive / "journal").glob("*.jsonl"))[0]
    with open(jf, "a") as fh:
        fh.write(json.dumps({"node_id": "legacy-dev", "seq": 1, "type": "retract", "timestamp": "2020-01-01T00:00:00Z",
                             "payload": {"retracts_ref": [node, int(seq)], "reason": "",
                                         "source": "owner:owner/owner"}}) + "\n")
    assert sb.hv("doctor", "rebuild").returncode == 0
    return _load_hv()._short_id(node, int(seq))


@pytest.mark.parametrize("state", ["open, a fact depends", "open, nothing depends", "closed", "no owner"])
def test_d_an_open_forget_grandfather_ends_the_update_with_action_required(sandbox, state):
    sb = sandbox()
    sid = None
    if state != "no owner":
        assert _ctl(sb, "owner", "init").returncode == 0                  # 1.28: born closed
    if state.startswith("open"):
        assert _ctl(sb, "config", "set", "forget_writers", "legacy").returncode == 0   # a pre-1.28 hive
    if state == "open, a fact depends":
        sid = _backdated_forget(sb, "the backup runs at 02:00")
    r = sb.update()
    out = r.stdout
    if state.startswith("open"):
        assert r.returncode != 0, out + r.stderr
        assert "ACTION REQUIRED" in out and "hive-mind doctor --fix" in out and "Update complete" not in out
        assert "here (this device keeps an owner key)" in out
        # after the restart, the rebuild and the re-wire, and the last thing printed
        assert out.index("Restarting sync daemon") < out.index("Rebuilding database") \
            < out.index("Re-asserting Claude Code hooks") < out.index("ACTION REQUIRED")
        assert out.rstrip().endswith("every `hive-mind update` ends here.")
        if sid:
            assert sid in out and "the backup runs at 02:00" in out and "asks y/N" in out
        else:
            assert "closes without asking" in out
        assert out.count("ACTION REQUIRED") == 1                          # the re-exec'd copy runs it once
    else:
        assert r.returncode == 0, out + r.stderr
        assert "ACTION REQUIRED" not in out and "Update complete" in out


def test_the_update_re_applies_module_units_and_a_broken_module_only_warns(sandbox):
    sb = sandbox()
    mods = sb.tmp / "modules"
    (mods / "ghost").mkdir(parents=True)                 # recorded as installed, but its tree is not a module
    (sb.hive / ".modules.json").write_text(json.dumps({"ghost": {"version": "1", "publisher": "x"}}))
    r = sb.update(HIVE_MODULES_DIR=mods)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ghost: units not re-applied" in r.stdout and "Update complete" in r.stdout
