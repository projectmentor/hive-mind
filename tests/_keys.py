"""Where a test hive's private keys are (2.0 PR 3a, private #27): in the key directory outside the
working tree, named by `$HIVE_HOME/.key-dir` once a key has been written, with the legacy root file
still honoured. Tests read and plant keys through here rather than hard-coding `HIVE_HOME/.device-key`."""

import base64
import hashlib
import os
from pathlib import Path


def key_dir(home):
    """The key directory `hv` would use for `home`, with the conftest's HOME and no `$HIVE_KEY_DIR`."""
    home = Path(home)
    env = os.environ.get("HIVE_KEY_DIR")
    if env:
        return Path(env).expanduser()
    ptr = home / ".key-dir"
    if ptr.exists() and ptr.read_text().strip():
        return Path(ptr.read_text().strip())
    return Path.home() / ".hive" / "keys" / hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]


def key_path(home, name):
    """`name` is "device-key", "owner-key" or "owner-pub". The key directory's file, unless only the
    legacy one in the working tree exists (exactly hv's rule)."""
    new, legacy = key_dir(home) / name, Path(home) / f".{name}"
    return legacy if not new.exists() and legacy.exists() else new


def plant(home, name, mode=0o600, seed=bytes(range(32))):
    """Write a key into the key directory (0700) and record it in `.key-dir`, as hv does."""
    d = key_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    Path(home).mkdir(parents=True, exist_ok=True)
    (Path(home) / ".key-dir").write_text(f"{d}\n")
    p = d / name
    p.write_text(base64.b64encode(seed).decode() + "\n")
    os.chmod(p, mode)
    return p
