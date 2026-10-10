"""The retired 1.x aliases and June-era migrations are gone (3.0, public #151; decision h:af137f9421).

`hv rebuild`, `hv merkle`, `hv key`, `hv doctor wire-agent`, `hv doctor migrate-identity` and
`hv migrate-device-identity` were exit-2 pointers through 2.x. In 3.0 each is an unknown command: argparse
exits 2, nothing is printed on stdout, no replacement is named, and nothing is written.

The argv list is frozen here rather than derived from `commandmap`, so deleting a table row cannot shrink it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import commandmap  # noqa: E402
import _keys  # noqa: E402

HV = PROJECT / "hv"
CTL = PROJECT / "hivemind_ctl.py"

# With and without arguments, as a 2.x script would have typed them.
CASES = [
    ["rebuild"],
    ["merkle"],
    ["key"],
    ["key", "init"],
    ["key", "init", "--force"],
    ["key", "show"],
    ["key", "announce"],
    ["doctor", "wire-agent"],
    ["doctor", "--fix", "wire-agent"],
    ["doctor", "migrate-identity", "--map", "m.json"],
    ["migrate-device-identity", "--map", "m.json", "--dry-run"],
]


def _env(home, claude):
    return dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"),
                CLAUDE_CONFIG_DIR=str(claude))


def _snapshot(*roots):
    """Every file under each root, by path and bytes. A missing root is recorded as missing."""
    out = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            out[str(root)] = None
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p)] = p.read_bytes()
    return out


@pytest.fixture
def seeded(tmp_path):
    """A hive with a journal and a store, and an empty Claude config dir."""
    home, claude = tmp_path / "h", tmp_path / "claude"
    claude.mkdir()
    r = subprocess.run([sys.executable, str(HV), "remember", "a fact before the alias runs",
                        "--source", "test"], env=_env(home, claude), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (home / "store.db").exists() and (home / "journal").is_dir()
    return home, claude


@pytest.mark.parametrize("argv", CASES, ids=" ".join)
def test_a_removed_alias_is_an_unknown_command_and_acts_on_nothing(seeded, argv):
    home, claude = seeded
    before = _snapshot(home, _keys.key_dir(home), claude)
    r = subprocess.run([sys.executable, str(HV), *argv], env=_env(home, claude), capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stdout == "", f"an unknown command prints nothing on stdout: {r.stdout!r}"
    assert "hive-mind" not in r.stderr and "removed in 2.0" not in r.stderr and "Run:" not in r.stderr, r.stderr
    assert _snapshot(home, _keys.key_dir(home), claude) == before, "a removed alias must act on nothing"


def test_key_init_on_a_fresh_home_mints_nothing(tmp_path):
    """The case where acting would be most visible: `key init` on a home with no device key."""
    home, claude = tmp_path / "fresh", tmp_path / "claude"
    claude.mkdir()
    r = subprocess.run([sys.executable, str(HV), "key", "init"], env=_env(home, claude),
                       capture_output=True, text=True)
    assert r.returncode == 2 and r.stdout == "", r.stderr
    assert not (home / ".key-dir").exists() and not (home / ".device-key").exists()
    assert not (_keys.key_dir(home) / "device-key").exists()


def test_rebuild_does_not_fall_through_to_the_rebuild(seeded):
    """With store.db gone, a rebuild would recreate it. The pointer must not."""
    home, claude = seeded
    for p in home.glob("store.db*"):
        p.unlink()
    r = subprocess.run([sys.executable, str(HV), "rebuild"], env=_env(home, claude), capture_output=True, text=True)
    assert r.returncode == 2 and r.stdout == "", r.stderr
    assert not (home / "store.db").exists()


def test_the_alias_table_and_its_helpers_are_gone():
    for name in ("RENAMED", "renamed", "renamed_text"):
        assert not hasattr(commandmap, name), name


@pytest.mark.parametrize("argv", [["rebuild"], ["merkle"], ["key"], ["doctor", "wire-agent"]])
def test_the_control_plane_does_not_suggest_a_removed_alias(tmp_path, argv):
    r = subprocess.run([sys.executable, str(CTL), *argv], env=_env(tmp_path / "h", tmp_path),
                       capture_output=True, text=True)
    assert r.returncode == 2 and "was removed" not in r.stderr and "Run:" not in r.stderr, r.stderr
    assert not (tmp_path / "h" / "store.db").exists()


# Shipped shell and CI that still called an old name would now exit 2. Under `|| true` or `>/dev/null
# 2>&1` that fails silently: the 2b gotcha, where `_update.sh` piped a moved command into /dev/null.
_OLD_CALL = re.compile(r"""(?:\bhv|\$HV|\$\{HV\})["']?\s+(?:rebuild|merkle|key)\b|\bdoctor\s+wire-agent\b""")


def test_no_shipped_script_calls_a_removed_alias():
    try:
        out = subprocess.run(["git", "-C", str(PROJECT), "ls-files", "-z"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("not a git checkout")
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    shipped = [f for f in out.stdout.decode().split("\0")
               if f.endswith((".sh", ".yml", ".yaml")) or f in ("scripts/installer/dispatcher.sh",)]
    assert shipped, "found no shell or workflow files to check"
    hits = []
    for rel in shipped:
        for n, line in enumerate((PROJECT / rel).read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _OLD_CALL.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "shipped code still calls a removed alias:\n" + "\n".join(hits)
