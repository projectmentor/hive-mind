"""The 1.x aliases 2.0 removes (S8, public #136; decision h:af137f9421).

`hv rebuild`, `hv merkle`, `hv key` and `hv doctor wire-agent` were hidden aliases that still worked through
1.x. In 2.0 each is an exit-2 pointer, the same shape as a moved command: it acts on nothing, prints nothing
on stdout, and names the `hv` command it became, with this invocation's arguments carried over.

The cases below are written out rather than derived from `commandmap.RENAMED`, so dropping a name from the
table fails its case here instead of quietly shrinking the test.
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
import hivemind_ctl  # noqa: E402
import _keys  # noqa: E402

HV = PROJECT / "hv"
CTL = PROJECT / "hivemind_ctl.py"

# (old argv, the replacement it must name). With and without arguments: the rest is carried as typed.
CASES = [
    (["rebuild"], "hv doctor rebuild"),
    (["merkle"], "hv doctor merkle"),
    (["key"], "hv config identity"),
    (["key", "init"], "hv config identity init"),
    (["key", "init", "--force"], "hv config identity init --force"),
    (["key", "show"], "hv config identity show"),
    (["key", "announce"], "hv config identity announce"),
    (["doctor", "wire-agent"], "hv wire claude"),
    (["doctor", "--fix", "wire-agent"], "hv wire claude"),
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


@pytest.mark.parametrize("argv, target", CASES, ids=lambda v: " ".join(v) if isinstance(v, list) else None)
def test_a_removed_alias_points_and_acts_on_nothing(seeded, argv, target):
    home, claude = seeded
    before = _snapshot(home, _keys.key_dir(home), claude)
    r = subprocess.run([sys.executable, str(HV), *argv], env=_env(home, claude), capture_output=True, text=True)
    assert r.returncode == commandmap.POINTER_EXIT == 2, r.stdout + r.stderr
    assert r.stdout == "", f"a pointer prints nothing on stdout: {r.stdout!r}"
    assert f"`hv {' '.join(argv)}` was removed in 2.0." in r.stderr, r.stderr
    assert f"Run: {target}\n" in r.stderr + "\n", r.stderr
    assert _snapshot(home, _keys.key_dir(home), claude) == before, "a pointer must act on nothing"


def test_key_init_on_a_fresh_home_mints_nothing(tmp_path):
    """The case where acting would be most visible: `key init` on a home with no device key."""
    home, claude = tmp_path / "fresh", tmp_path / "claude"
    claude.mkdir()
    r = subprocess.run([sys.executable, str(HV), "key", "init"], env=_env(home, claude),
                       capture_output=True, text=True)
    assert r.returncode == 2 and "Run: hv config identity init" in r.stderr, r.stderr
    assert not (home / ".key-dir").exists() and not (home / ".device-key").exists()
    assert not (_keys.key_dir(home) / "device-key").exists()


def test_rebuild_does_not_fall_through_to_the_rebuild(seeded):
    """With store.db gone, a rebuild would recreate it. The pointer must not."""
    home, claude = seeded
    for p in home.glob("store.db*"):
        p.unlink()
    r = subprocess.run([sys.executable, str(HV), "rebuild"], env=_env(home, claude), capture_output=True, text=True)
    assert r.returncode == 2 and "Run: hv doctor rebuild" in r.stderr, r.stderr
    assert not (home / "store.db").exists()


def _parser_paths():
    import argparse

    def walk(parser, prefix=()):
        out = {prefix} if prefix else set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    out |= walk(sub, prefix + (name,))
        return out

    return walk(hivemind_ctl.hv().build_parser())


def test_every_replacement_is_a_real_hv_command():
    """A pointer naming a command that does not exist leaves the operator with nothing correct to type.
    `wire claude` is `wire` with a cell name, so its path is `wire`."""
    paths = _parser_paths()
    for old, (new, _why) in commandmap.RENAMED.items():
        words = tuple(new.split())
        assert words in paths or words[:1] == ("wire",) and ("wire",) in paths, f"{old} -> {new}"


def test_no_removed_name_is_still_a_parser_command():
    """The old names are gone from the parser, so the pointer is not one branch in front of a working
    handler that some other entry (the control plane parses with the same parser) could still reach."""
    paths = _parser_paths()
    assert not [k for k in commandmap.RENAMED if k in paths], "a removed alias is still parseable"


def test_every_case_is_in_the_table_and_every_table_entry_has_a_case():
    covered = {tuple(a[:1]) if a[0] != "doctor" else ("doctor", "wire-agent") for a, _ in CASES}
    assert covered == set(commandmap.RENAMED)


@pytest.mark.parametrize("argv, target", [
    (["rebuild"], "hv doctor rebuild"),
    (["key", "init"], "hv config identity init"),
    (["doctor", "--fix", "wire-agent"], "hv wire claude"),
])
def test_the_control_plane_suggests_the_new_name(tmp_path, argv, target):
    """`hive-mind rebuild` must not suggest `hv rebuild`, which is itself only a pointer now."""
    r = subprocess.run([sys.executable, str(CTL), *argv], env=_env(tmp_path / "h", tmp_path),
                       capture_output=True, text=True)
    assert r.returncode == 2 and f"try `{target}`" in r.stderr, r.stderr
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
