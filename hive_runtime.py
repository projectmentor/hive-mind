"""The per-user runtime directory when a session did not set `XDG_RUNTIME_DIR` (#522).

`pam_systemd` sets `XDG_RUNTIME_DIR=/run/user/<uid>` for a login, but some sessions never pass through PAM: Tailscale
SSH runs `bash -c` with no PAM session, and so do cron and `claude -p` started from such a shell. Without it the core
thinks there is no user systemd manager (a remote `module install` or `update` writes units and restarts nothing), the
owner agent's clients look for its socket under `$TMPDIR`, and a module's CLI cannot find its own runtime directory.

The fallback is the directory `pam_systemd` would have named, and only for the account's own login: `$HOME` must be
the account's home (its password-database entry), so a test or sandbox that points `HOME` at a decoy gets no runtime
directory and keeps reaching no user manager, never the real one. The directory must be a real directory owned by
this uid with no group or other access. Stdlib only: `owner_agent` imports it too.
"""

import os
import pwd
import stat

RUN_USER = "/run/user"            # where pam_systemd makes each uid's runtime directory


def _account_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return None


def fallback(env=None):
    """`/run/user/<uid>` when this is the account's own login and that directory is private to it; else None."""
    env = os.environ if env is None else env
    home, account = env.get("HOME"), _account_home()
    if not home or not account or os.path.realpath(home) != os.path.realpath(account):
        return None
    d = f"{RUN_USER}/{os.getuid()}"
    try:
        st = os.lstat(d)
    except OSError:
        return None
    return d if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077 else None


def runtime_dir(env=None):
    """`$XDG_RUNTIME_DIR` when it is absolute, else the fallback, else None."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_RUNTIME_DIR")
    return xdg if xdg and os.path.isabs(xdg) else fallback(env)


def ensure(env=None):
    """Put the fallback into `env` (default `os.environ`) when `XDG_RUNTIME_DIR` is unset, so child processes
    (`systemctl --user`, a module) get it too. Returns the runtime directory or None."""
    env = os.environ if env is None else env
    d = runtime_dir(env)
    if d and not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = d
    return d


def launcher_lines():
    """Shell lines for a module launcher: the same fallback, decided when the launcher runs. The account's home and
    uid are fixed when it is rendered; a launcher run with another HOME, or with XDG_RUNTIME_DIR set, is unchanged."""
    home, uid = _account_home(), os.getuid()
    if not home or "'" in home:
        return []
    d = f"{RUN_USER}/{uid}"
    return [f"if [ -z \"${{XDG_RUNTIME_DIR:-}}\" ] && [ \"${{HOME:-}}\" = '{home}' ] && [ -d '{d}' ] && [ ! -L '{d}' ] "
            f"&& [ -O '{d}' ]; then XDG_RUNTIME_DIR='{d}'; export XDG_RUNTIME_DIR; fi"]
