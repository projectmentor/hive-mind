"""The owner key is sealed at rest (2.0 PR 3b, private #27).

PR 3a moved the seeds out of the working tree, but a 0600 file is still readable by every process running
as the owner's user, the agents included, and whoever reads the owner seed can sign governance. So every
new owner key is written only as `owner-key.sealed`: the passphrase envelope `owner escrow` already uses
(scrypt, then ChaCha20-Poly1305). `hive-mind` unlocks it once per command (h:af5ecf48c5), from the tty or
from $HIVE_OWNER_KEY_PASSPHRASE. `hive-mind owner seal` converts a plaintext key, and doctor fails while
one exists. `hv` never opens either form: presence stays a `stat` of the file names.
"""

import ast
import base64
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import hivemind_ctl  # noqa: E402

POSIX = os.name != "nt"
PASS = os.environ.get("HIVE_OWNER_KEY_PASSPHRASE") or "test-owner-key-passphrase"


def _env(home, **extra):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home).parent / "stash"))
    env.pop("HIVE_KEY_DIR", None)
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


# Each child gets its own session, so it has no controlling terminal: with the passphrase variable
# removed, getpass then cancels instead of waiting at the developer's /dev/tty.
def _hv(home, *argv, **extra):
    return subprocess.run([sys.executable, str(PROJECT / "hv"), *argv], env=_env(home, **extra),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=POSIX)


def _ctl(home, *argv, **extra):
    return subprocess.run([sys.executable, str(PROJECT / "hivemind_ctl.py"), *argv], env=_env(home, **extra),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=POSIX)


def _key_dir(home):
    return Path((Path(home) / ".key-dir").read_text().strip())


def _journal(home):
    return b"".join(p.read_bytes() for p in sorted((Path(home) / "journal").glob("*.jsonl")))


def _seed_b64(home):
    out = Path(home).parent / "export.json"
    assert _ctl(home, "owner", "export", "--out", str(out)).returncode == 0
    return json.loads(out.read_text())["seed"]


def _check(home, name):
    r = _hv(home, "doctor", "--format", "json")
    return next(c for c in json.loads(r.stdout)["checks"] if c["name"] == name)


def _plaintext_node(tmp_path):
    """An owner node as 3a left it: a plaintext owner key in the key directory, nothing sealed."""
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    seed = _seed_b64(home)
    kd = _key_dir(home)
    (kd / "owner-key.sealed").unlink()
    (kd / "owner-key").write_text(seed + "\n")
    os.chmod(kd / "owner-key", 0o600)
    stash = home.parent / "stash"
    (stash / ".owner-key.sealed").unlink()
    (stash / ".owner-key").write_text(seed + "\n")
    return home, seed


def _lib(home, monkeypatch, name="hv_sealed"):
    monkeypatch.setenv("HIVE_HOME", str(home))
    monkeypatch.delenv("HIVE_KEY_DIR", raising=False)
    loader = importlib.machinery.SourceFileLoader(name, str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return hivemind_ctl.install(m)


# ── what is on disk ──────────────────────────────────────────────────────────────────────────────────

def test_owner_init_writes_only_the_sealed_form(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    kd, stash = _key_dir(home), tmp_path / "stash"
    sealed = kd / "owner-key.sealed"
    assert sealed.is_file()
    assert not (kd / "owner-key").exists() and not (home / ".owner-key").exists()
    assert (stash / ".owner-key.sealed").is_file() and not (stash / ".owner-key").exists(), \
        "the stash outlives an uninstall; it must not hold a readable seed"
    if POSIX:
        assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
        assert stat.S_IMODE((stash / ".owner-key.sealed").stat().st_mode) == 0o600
    seed_b64 = _seed_b64(home)
    raw = base64.b64decode(seed_b64)
    for blob in (sealed.read_bytes(), (stash / ".owner-key.sealed").read_bytes()):
        assert seed_b64.encode() not in blob and raw.hex().encode() not in blob and raw not in blob
    assert json.loads(sealed.read_text())["enc"] == "scrypt-chacha20poly1305-v2"


def test_hv_answers_presence_without_the_passphrase_and_never_prompts(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    for env in ({"HIVE_OWNER_KEY_PASSPHRASE": None}, {"HIVE_OWNER_KEY_PASSPHRASE": "wrong"}):
        show = _hv(home, "owner", "show", **env)
        assert show.returncode == 0 and "this device holds the owner key" in show.stdout
        for r in (show, _hv(home, "whoami", **env), _hv(home, "doctor", **env)):
            assert "passphrase" not in (r.stdout + r.stderr).lower(), "hv must never try to unlock the key"


def test_the_data_plane_never_opens_a_sealed_key():
    """Static, over the code that executes (h:157bd5e469): nothing in `hv` calls the AEAD opener or the
    sealed-key reader. `_owner_unseal` is defined there (it is the escrow cipher) but only the control
    plane calls it."""
    tree = ast.parse((PROJECT / "hv").read_text())
    calls = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"_owner_unseal", "load_sealed"}, calls & {"_owner_unseal", "load_sealed"}


# ── unlocking ────────────────────────────────────────────────────────────────────────────────────────

def test_a_wrong_or_cancelled_passphrase_signs_nothing(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    before = _journal(home)
    for env, says in (({"HIVE_OWNER_KEY_PASSPHRASE": "wrong"}, "Could not unlock the owner key"),
                      ({"HIVE_OWNER_KEY_PASSPHRASE": ""}, "Cancelled"),
                      ({"HIVE_OWNER_KEY_PASSPHRASE": None}, "Cancelled")):     # no tty, no variable
        r = _ctl(home, "config", "set", "cap_self", "0.6", **env)
        assert r.returncode == 1 and says in r.stderr, (env, r.stdout, r.stderr)
        assert _journal(home) == before, "a locked key must leave the journal byte-identical"
    assert _ctl(home, "config", "set", "cap_self", "0.6").returncode == 0
    assert _journal(home) != before


def test_a_cancelled_owner_init_writes_no_key_and_no_journal(tmp_path):
    home = tmp_path / "h"
    r = _ctl(home, "owner", "init", HIVE_OWNER_KEY_PASSPHRASE="")
    assert r.returncode == 1 and "no owner key was written" in r.stderr
    assert not (home / "journal").exists() or _journal(home) == b""
    kd = (home / ".key-dir")
    assert not kd.exists() or not (_key_dir(home) / "owner-key.sealed").exists()


def test_one_command_unlocks_once(tmp_path, monkeypatch):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    m = _lib(home, monkeypatch)
    opened = []
    real = m._owner_unseal
    monkeypatch.setattr(m, "_owner_unseal", lambda env, pw: opened.append(1) or real(env, pw))
    first, second = m._owner_seed(), m._owner_seed()
    assert first == second and len(first) == 32
    assert len(opened) == 1, "unlock is per command: one passphrase, however many signatures"


@pytest.mark.skipif(not POSIX, reason="a pseudo-terminal")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")   # child execs at once
def test_at_a_terminal_it_asks_and_allows_three_tries(tmp_path):
    """The interactive path, driven through a real pty so getpass reads /dev/tty: two wrong answers, then
    the right one, and the act is signed."""
    import pty
    import select
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    before = _journal(home)
    env = _env(home, HIVE_OWNER_KEY_PASSPHRASE=None)
    pid, fd = pty.fork()
    if pid == 0:                                                  # the child: hive-mind on the pty
        os.execve(sys.executable, [sys.executable, str(PROJECT / "hivemind_ctl.py"),
                                   "config", "set", "cap_self", "0.6"], env)
    out, answers = b"", [b"wrong\n", b"also wrong\n", PASS.encode() + b"\n"]
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 30)
            if not ready:
                break
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if answers and out.count(b"Owner key passphrase") > 3 - len(answers):
                os.write(fd, answers.pop(0))
    finally:
        _, status = os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert os.waitstatus_to_exitcode(status) == 0, text
    assert text.count("Wrong passphrase, try again.") == 2, text
    assert _journal(home) != before


# ── migrating a plaintext key ────────────────────────────────────────────────────────────────────────

def test_doctor_fails_while_the_owner_key_is_plaintext(tmp_path):
    home, _seed = _plaintext_node(tmp_path)
    c = _check(home, "owner-seal")
    assert c["status"] == "fail" and "hive-mind owner seal" in c["detail"]
    r = _hv(home, "doctor")
    assert r.returncode == 1


def test_owner_seal_converts_a_plaintext_key_in_place(tmp_path):
    home, seed = _plaintext_node(tmp_path)
    oid = _hv(home, "owner", "show").stdout
    r = _ctl(home, "owner", "seal", HIVE_OWNER_KEY_PASSPHRASE="new-pass")
    assert r.returncode == 0 and "Sealed owner key" in r.stdout, r.stdout + r.stderr
    kd, stash = _key_dir(home), tmp_path / "stash"
    assert (kd / "owner-key.sealed").is_file() and not (kd / "owner-key").exists()
    assert (stash / ".owner-key.sealed").is_file() and not (stash / ".owner-key").exists()
    assert _hv(home, "owner", "show").stdout == oid, "sealing changes storage, never the owner"
    assert _check(home, "owner-seal")["status"] == "ok"
    assert _ctl(home, "config", "set", "cap_self", "0.6", HIVE_OWNER_KEY_PASSPHRASE="new-pass").returncode == 0
    assert _seed_b64_with(home, "new-pass") == seed
    again = _ctl(home, "owner", "seal", HIVE_OWNER_KEY_PASSPHRASE="new-pass")
    assert "already sealed" in again.stdout


def _seed_b64_with(home, pw):
    out = Path(home).parent / "export2.json"
    assert _ctl(home, "owner", "export", "--out", str(out), HIVE_OWNER_KEY_PASSPHRASE=pw).returncode == 0
    return json.loads(out.read_text())["seed"]


def test_owner_seal_moves_a_legacy_root_key_too(tmp_path):
    home, seed = _plaintext_node(tmp_path)
    kd = _key_dir(home)
    (kd / "owner-key").rename(home / ".owner-key")                 # pre-3a: at the root of the checkout
    assert _ctl(home, "owner", "seal").returncode == 0
    assert not (home / ".owner-key").exists() and (kd / "owner-key.sealed").is_file()


def test_owner_seal_never_drops_a_plaintext_key_that_differs_from_the_sealed_one(tmp_path):
    home, _seed = _plaintext_node(tmp_path)
    kd = _key_dir(home)
    other = base64.b64encode(os.urandom(32)).decode()
    assert _ctl(home, "owner", "seal").returncode == 0                   # seal the real one
    (kd / "owner-key").write_text(other + "\n")                          # a different plaintext beside it
    r = _ctl(home, "owner", "seal")
    assert "DIFFERENT key" in r.stdout and (kd / "owner-key").read_text().strip() == other


def test_a_seal_that_does_not_round_trip_keeps_the_plaintext(tmp_path, monkeypatch):
    """The sealed copy is opened before any plaintext copy is removed."""
    home, seed = _plaintext_node(tmp_path)
    m = _lib(home, monkeypatch)
    real = m._owner_seal

    def corrupt(s, pw):
        env = real(s, pw)
        env["ct"] = base64.b64encode(bytes(32)).decode()
        return env

    monkeypatch.setattr(m, "_owner_seal", corrupt)
    with pytest.raises(Exception):
        m._write_owner_key(base64.b64decode(seed), "pw")
    assert (_key_dir(home) / "owner-key").read_text().strip() == seed, "the only readable copy survived"


# ── the installer's identity stash ───────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not POSIX, reason="bash installer primitive")
def test_identity_stash_carries_the_sealed_key(tmp_path):
    src, dst, stash = tmp_path / "src", tmp_path / "dst", tmp_path / "keep"
    for d in (src, dst):
        d.mkdir()
        (d / "hv").symlink_to(PROJECT / "hv")
    assert _hv(src, "key", "init").returncode == 0 and _ctl(src, "owner", "init").returncode == 0
    script = f'''
        export HIVE_IDENTITY_STASH="{stash}"
        . "{PROJECT}/scripts/installer/_identity.sh"
        keep_identity_save "{src}" || exit 11
        keep_identity_restore "{dst}" || exit 13
    '''
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k not in ("HIVE_HOME", "HIVE_KEY_DIR")})
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (stash / ".owner-key.sealed").is_file() and not (stash / ".owner-key").exists()
    assert (_key_dir(dst) / "owner-key.sealed").read_bytes() == (_key_dir(src) / "owner-key.sealed").read_bytes()
    assert not (_key_dir(dst) / "owner-key").exists()


# ── the link verbs: a locked key aborts before the fact or decision is written (Fable on #167) ─────────

def _link_setup(tmp_path):
    """An owner node with a fact and a decision to link to, and their sids."""
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    assert _hv(home, "remember", "the build is red", "--source", "alice").returncode == 0
    assert _hv(home, "decide", "ship on friday", "--rationale", "r").returncode == 0
    import sqlite3
    conn = sqlite3.connect(home / "store.db")
    sids = dict(conn.execute("SELECT kind, sid FROM journal_index WHERE kind IN ('fact', 'decision')").fetchall())
    conn.close()
    return home, sids["fact"], sids["decision"]


_LINK_CASES = [      # every link flag `_LINK_FLAGS` guards (Fable on #167), plus `entity link`
    ("remember --resolves", lambda f, d: ["remember", "the build is green", "--resolves", f], "resolves"),
    ("remember --outcome-of", lambda f, d: ["remember", "friday went fine", "--outcome-of", d], "outcome-of"),
    ("remember --supports", lambda f, d: ["remember", "red again at noon", "--supports", f], "supports"),
    ("remember --contradicts", lambda f, d: ["remember", "it was green all day", "--contradicts", f], "contradicts"),
    ("remember --extends", lambda f, d: ["remember", "red since the merge", "--extends", f], "extends"),
    ("decide --supersedes", lambda f, d: ["decide", "ship on monday", "--rationale", "r", "--supersedes", d],
     "supersedes"),
    ("decide --revoke", lambda f, d: ["decide", "--revoke", d, "--rationale", "friday was wrong"], "supersedes"),
    ("decide --informed", lambda f, d: ["decide", "fix the build first", "--rationale", "r", "--informed", f],
     "informed"),
    ("entity link", lambda f, d: ["entity", "link", "--name", "Build", "--fact-id", f], "entity"),
]


def _manual(argv):
    return argv + ["--source", "manual"]


@pytest.mark.parametrize("name, argv, kind", _LINK_CASES, ids=[c[0] for c in _LINK_CASES])
@pytest.mark.parametrize("passphrase, says", [("wrong", "Could not unlock"), ("", "Cancelled")],
                         ids=["wrong", "cancelled"])
def test_a_locked_key_aborts_a_link_verb_before_anything_is_written(tmp_path, name, argv, kind,
                                                                     passphrase, says):
    home, fact, dec = _link_setup(tmp_path)
    if name == "entity link":
        assert _hv(home, "entity", "add", "--name", "Build", "--type", "concept").returncode == 0
    before = _journal(home)
    r = _ctl(home, *_manual(argv(fact, dec)), HIVE_OWNER_KEY_PASSPHRASE=passphrase)
    assert r.returncode == 1 and says in r.stderr, (r.stdout, r.stderr)
    assert _journal(home) == before, "the fact or decision must not be written without its link"


@pytest.mark.parametrize("name, argv, kind", _LINK_CASES, ids=[c[0] for c in _LINK_CASES])
def test_with_the_passphrase_the_link_verb_owner_signs_its_link(tmp_path, name, argv, kind):
    home, fact, dec = _link_setup(tmp_path)
    if name == "entity link":
        assert _hv(home, "entity", "add", "--name", "Build", "--type", "concept").returncode == 0
    r = _ctl(home, *_manual(argv(fact, dec)))
    assert r.returncode == 0, r.stdout + r.stderr
    links = [json.loads(line) for line in _journal(home).decode().splitlines() if line.strip()]
    links = [e for e in links if e.get("type") == "link" and e["payload"].get("kind") == kind]
    assert links and all("owner_sig" in e["payload"] for e in links), links


def test_the_link_flag_list_matches_the_parser(tmp_path, monkeypatch):
    """`_LINK_FLAGS` is the control plane's list of the options that write a link; it must name every
    ref-taking option `hv`'s `remember` and `decide` define, so a link flag added to `hv` later cannot
    miss the unlock-before-write (Fable on #167)."""
    import argparse
    m = _lib(tmp_path / "h", monkeypatch, "hv_flag_list")
    sub = next(a for a in m.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    for cmd in ("remember", "decide"):
        ref_opts = {a.dest for a in sub.choices[cmd]._actions
                    if a.option_strings and a.metavar in ("FACT", "DECISION", "REF")}
        assert set(m._LINK_FLAGS[cmd]) == ref_opts, (cmd, sorted(ref_opts))
