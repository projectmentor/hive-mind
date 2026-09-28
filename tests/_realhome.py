"""The real-HOME state the test suite must never touch (#109), and one way to compare it.

Shared by the session guard in conftest.py and the fail-closed meta-test in
test_real_home_guard.py, so the check that runs on every session and the proof that it works
cannot drift apart. Everything here is read-only.
"""
import hashlib
import json
import os
import pwd
import site
import subprocess
from pathlib import Path


EXPORTED_HOME_VAR = "HIVE_TEST_EXPORTED_HOME"


def session_paths():
    """The real paths as this session STARTED: the HOME it was launched with (a developer may run
    under a deliberate HOME=/tmp/x), else the passwd entry; an exported CLAUDE_CONFIG_DIR or
    HIVE_IDENTITY_STASH wins over the default under that home, exactly as `hv` resolves them.
    Call it before any fixture redirects the environment."""
    home = Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    stash = Path(os.environ.get("HIVE_IDENTITY_STASH") or home / ".config" / "hive-mind" / "identity")
    # The live hive's checkout, so the guard can see a .genesis-pin or a key written into it (#14, #160):
    # the pin is per-node governance authority, in the same class as the owner key. This is the EXPORTED
    # HIVE_HOME, which conftest records in EXPORTED_HOME_VAR before it sandboxes HIVE_HOME; an xdist worker
    # inherits the sandbox, so it must read the record, not HIVE_HOME.
    exported = os.environ.get(EXPORTED_HOME_VAR)
    if exported is None:
        exported = os.environ.get("HIVE_HOME")
    hive = Path(exported or Path(__file__).resolve().parent.parent)
    return {"home": home, "claude": claude, "stash": stash, "userbase": site.getuserbase(), "hive": hive,
            "key_dir": key_dir_of(hive, home)}


def key_dir_of(hive, home):
    """The exported hive's key directory (2.0 PR 3a), by `hv`'s rule but without importing `hv`:
    $HIVE_KEY_DIR, else the path recorded in `<hive>/.key-dir`, else `<home>/.hive/keys/<16 hex>`."""
    env = os.environ.get("HIVE_KEY_DIR")
    if env:
        return Path(env).expanduser()
    try:
        recorded = (Path(hive) / ".key-dir").read_text().strip()
        if recorded:
            return Path(recorded)
    except OSError:
        pass
    return Path(home) / ".hive" / "keys" / hashlib.sha256(str(Path(hive).resolve()).encode()).hexdigest()[:16]


def _file_state(p, ctime=False):
    """None if absent, else (mtime_ns, sha256), enough to tell 'untouched' from 'rewritten'. With
    ctime=True the inode change time is included too: `hv` writes settings.json.bak.doctor with
    shutil.copy2, which copies the SOURCE's mtime, so only ctime moves when --fix rewrites it."""
    try:
        st = p.stat()
        state = (st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        return state + (st.st_ctime_ns,) if ctime else state
    except FileNotFoundError:
        return None


def _hive_hooks(settings):
    """The Hive-owned hook commands in a Claude Code settings.json (the ones that run
    hive_dispatch.sh). Only these are compared: Claude Code itself rewrites the rest of the file
    during a session (a permission approval, /config), so a whole-file check would flake."""
    try:
        cfg = json.loads(settings.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return "unparseable"
    return sorted(h.get("command", "")
                  for groups in (cfg.get("hooks") or {}).values()
                  for grp in (groups or [])
                  for h in (grp.get("hooks") or [])
                  if "hive_dispatch.sh" in h.get("command", ""))


def snapshot(claude, stash, hive=None, key_dir=None):
    """The state `hv doctor --fix` and `hv owner init` would change: the skill symlink's target, the
    Hive-owned hooks, the .bak.doctor that every real --fix write refreshes (hv `_wire_agent`), and
    the owner-key stash a reinstall restores from (hv `_stash_owner_key`)."""
    skill = Path(claude) / "skills" / "hive-memory"
    if skill.is_symlink():
        skill_state = ("symlink", os.readlink(skill))
    else:
        skill_state = ("present", None) if skill.exists() else None
    return {
        "skill link": skill_state,
        "hive hooks in settings.json": _hive_hooks(Path(claude) / "settings.json"),
        "settings.json.bak.doctor": _file_state(Path(claude) / "settings.json.bak.doctor", ctime=True),
        "owner-key stash": _file_state(Path(stash) / ".owner-key"),
        "genesis pin": _file_state(Path(hive) / ".genesis-pin") if hive else None,
        # The real hive's key material and identity (#160): operator state, which nothing a developer or
        # an agent does during a test run changes, so any change is the suite's.
        **{f"real hive {name}": (_file_state(Path(hive) / name) if hive else None) for name in HIVE_KEY_FILES},
        # ...and its key directory outside the checkout (2.0 PR 3a), where the seeds live now.
        **{f"real key dir {name}": (_file_state(Path(key_dir) / name) if key_dir else None)
           for name in KEY_DIR_FILES},
    }


# Key material and identity in the checkout: the device and owner seeds, the public owner half, the cached
# device id, and the key-directory pointer.
HIVE_KEY_FILES = (".device-key", ".owner-key", ".owner-pub", ".device-id", ".key-dir")
KEY_DIR_FILES = ("device-key", "owner-key", "owner-key.sealed", "owner-pub")
# A hive marked with this file is a DECOY that only the test suite may see (#160): the guard then holds the
# whole tree to byte-identical, which it cannot do for a live hive whose daemon ingests from peers.
DECOY_MARKER = ".hive-test-decoy"


def activity(hive):
    """The live hive's journal files and store as (size, mtime_ns): reported, never failed on. Its daemon
    appends peers' entries and rebuilds the store during any run, and another session on this machine may
    write to it, so a change here is a note unless the hive is a decoy."""
    hive = Path(hive)
    out = {}
    for p in sorted((hive / "journal").glob("*.jsonl")) + [hive / "store.db"]:
        try:
            st = p.stat()
            out[str(p.relative_to(hive))] = (st.st_size, st.st_mtime_ns)
        except (FileNotFoundError, ValueError):
            pass
    return out


def decoy_state(hive):
    """If `hive` is a decoy (it holds DECOY_MARKER), every file under it as relative path -> sha256; else
    None. The guard requires a decoy byte-identical at the end of the session."""
    hive = Path(hive)
    if not (hive / DECOY_MARKER).is_file():
        return None
    out = {}
    for p in sorted(hive.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(hive))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def decoy_diff(before, after):
    """Changes to a decoy hive, one line each."""
    out = [f"decoy hive {k} {'removed' if k not in after else 'changed'}"
           for k in before if after.get(k) != before[k]]
    out += [f"decoy hive {k} created" for k in after if k not in before]
    return out


def diff(before, after):
    """Human-readable changes between two snapshots. A path absent before and present after is
    reported as created; the guard's contract is 'unchanged, or not created'."""
    out = []
    for key in before:
        if before[key] != after[key]:
            verb = "created" if before[key] is None else ("removed" if after[key] is None else "changed")
            out.append(f"{key} {verb}")
    return out


def live_daemon_pid():
    """The supervisor-managed hive-sync daemon's PID via the REAL systemctl, or None (no user
    systemd, e.g. CI or macOS). Informational only: the daemon may restart during a run for reasons
    that are not the suite's (hive-doctor.timer runs `hv doctor --fix`; a self-rebind exits 75)."""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "hive-sync", "-p", "ExecMainPID", "--value"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    pid = r.stdout.strip()
    return pid if r.returncode == 0 and pid not in ("", "0") else None
