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


def session_paths():
    """The real paths as this session STARTED: the HOME it was launched with (a developer may run
    under a deliberate HOME=/tmp/x), else the passwd entry; an exported CLAUDE_CONFIG_DIR or
    HIVE_IDENTITY_STASH wins over the default under that home, exactly as `hv` resolves them.
    Call it before any fixture redirects the environment."""
    home = Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    stash = Path(os.environ.get("HIVE_IDENTITY_STASH") or home / ".config" / "hive-mind" / "identity")
    return {"home": home, "claude": claude, "stash": stash, "userbase": site.getuserbase()}


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


def snapshot(claude, stash):
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
    }


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
