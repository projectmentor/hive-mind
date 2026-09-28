"""The control plane exists and routes (2.0 PR 2a, public #136), and after the flip (PR 2b) it is the only
place the owner steps run.

2a made `hive-mind` a front end over the library; 2b moved every owner step onto it, so `hv` cannot
owner-sign at all. The S2 assertions themselves live in `tests/test_s2_split.py`.

What is worth pinning here is the part that would otherwise drift silently: that the two planes agree
about which commands belong to which, in both directions. A pointer naming a command the control plane
does not implement leaves an operator mid-task with nothing correct to type, and a command that quietly
exists on both planes defeats the split.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import commandmap  # noqa: E402
import hivemind_ctl  # noqa: E402
import _keys  # noqa: E402  (where a hive's keys are, 2.0 PR 3a)


def _run_ctl(home, *argv):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"),
               HIVE_OWNER_PASSPHRASE="testpass")
    return subprocess.run([sys.executable, str(PROJECT / "hivemind_ctl.py"), *argv],
                          env=env, capture_output=True, text=True)


def _run_hv(home, *argv):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"),
               HIVE_OWNER_PASSPHRASE="testpass")
    return subprocess.run([sys.executable, str(PROJECT / "hv"), *argv],
                          env=env, capture_output=True, text=True)


# ── the two planes agree, in both directions ────────────────────────────────────────────────────

def _parser_paths():
    """Every command path the library's parser really accepts, walking argparse's subparser actions.

    Deliberately not "any action with choices": on `doctor` that picks up `--format`'s `text|json` and
    reports flag values as if they were subcommands."""
    import argparse

    def walk(parser, prefix=()):
        out = {prefix} if prefix else set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    out |= walk(sub, prefix + (name,))
        return out

    return walk(hivemind_ctl.hv().build_parser())


def test_every_moved_command_resolves_to_a_real_command():
    """The failure this prevents: `hv` points an operator at a `hive-mind` command that does not exist,
    leaving them mid-task with nothing correct to type.

    An earlier version of this test checked only ROUTING (`_is_control_plane`), never that the library
    parser accepts the command — so it passed while `MOVED` advertised `owner mint`, which does not
    exist. A test that names the right property and does not check it is worse than no test."""
    paths = _parser_paths()
    broken = []
    for (new, _why) in commandmap.MOVED.values():
        lib = tuple(hivemind_ctl._to_library_argv(new.split()))
        if lib not in paths:
            broken.append(f"{new!r} -> {' '.join(lib)!r}")
        if not hivemind_ctl._is_control_plane(new.split()):
            broken.append(f"{new!r} does not route to the control plane")
    assert not broken, "advertised but not real: " + "; ".join(sorted(set(broken)))


def test_every_command_that_stays_is_also_a_real_command():
    """The same check on the other table. `STAYS` listed `owner vote-election` — the JOURNAL ACTION name,
    not the CLI verb, which is `owner vote`. That made the dead-man guard assert something vacuous while
    the command that matters went unguarded."""
    paths = _parser_paths()
    unreal = [" ".join(k) for k in commandmap.STAYS if tuple(k) not in paths]
    assert not unreal, f"named in STAYS but not a real command: {unreal}"


def test_nothing_that_stays_is_also_on_the_control_plane():
    """The opposite failure: a command quietly available on both planes, which defeats the split. The
    reads and the DEVICE-signed election verbs must be refused here."""
    also_there = [" ".join(k) for k in commandmap.STAYS
                  if hivemind_ctl._is_control_plane(list(k))]
    assert not also_there, f"available on both planes: {also_there}"


def test_dead_man_recovery_is_not_on_the_control_plane():
    """Stated as its own test because the consequence is severe and non-obvious: if these move, an
    admitted member running only `hv` cannot propose or vote when the owner has gone dark, so recovering
    a lost owner would need the very binary the split keeps off agent nodes."""
    for verb in ("propose-election", "vote", "elections"):    # `vote`, not the action name `vote-election`
        assert not hivemind_ctl._is_control_plane(["owner", verb]), verb


@pytest.mark.parametrize("argv,expected", [
    (["retract", "--owner", "h:abc"], True),      # governance
    (["retract", "h:abc"], False),                # peer negative evidence
    (["retract", "h:abc", "--reason", "x"], False),
])
def test_retract_is_decided_by_the_flag_not_the_verb(argv, expected):
    assert hivemind_ctl._is_control_plane(argv) is expected


def test_the_s5_rename_translates_to_the_library_verb():
    """`owner revoke` on the control plane is the library's `owner revoke-escrow`. The hyphen goes with
    the move, per S5 — which renames the hyphenated verbs that MOVE."""
    assert hivemind_ctl._to_library_argv(["owner", "revoke", "k1:x:1"]) == ["owner", "revoke-escrow", "k1:x:1"]


def test_a_data_plane_command_is_refused_with_the_hv_form(tmp_path):
    # `remember` is no longer the example: since 2b it is also the owner-signed form of its links
    # (decision h:34cc1dbcd3). A pure read never is.
    r = _run_ctl(tmp_path, "search", "nope")
    assert r.returncode == 2
    assert "not a control-plane command" in r.stderr
    assert "hv search nope" in r.stderr            # names what to type instead


# ── it actually works, end to end ───────────────────────────────────────────────────────────────

def test_owner_init_through_the_control_plane_establishes_and_pins(tmp_path):
    r = _run_ctl(tmp_path, "owner", "init")
    assert r.returncode == 0, r.stderr
    assert "Owner established:" in r.stdout
    assert "pinned" in r.stdout                                  # 1.27's genesis pin still happens
    assert _keys.key_path(tmp_path, "owner-key").exists()
    assert (tmp_path / ".genesis-pin").exists()
    # and the data plane reads it back: `owner show` is a read and stays on hv
    s = _run_hv(tmp_path, "owner", "show")
    assert s.returncode == 0 and "this device holds the owner key" in s.stdout


def test_the_control_plane_runs_the_store_prologue(tmp_path):
    """Regression. `dispatch` was reached without `init_db` when the prologue lived in `main`, so the
    first control-plane `owner init` minted a key against an uninitialised store and then failed. The
    prologue belongs with the routing, where no caller can skip it."""
    r = _run_ctl(tmp_path, "owner", "init")
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert (tmp_path / "store.db").exists()


# ── 2b is the flip: hv cannot owner-sign ─────────────────────────────────────────────────────────

def test_hv_cannot_owner_sign_after_the_split(tmp_path):
    """The inversion of 2a's `test_hv_still_owner_signs_in_this_slice` — the security change, visible on
    its own. `hv owner init` points at the control plane and acts on nothing; the loaded library has no
    signer and never imported `ownerkey`."""
    r = _run_hv(tmp_path, "owner", "init")
    assert r.returncode == 2 and "Run: hive-mind owner init" in r.stderr
    assert not _keys.key_path(tmp_path, "owner-key").exists()
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("hv_2b", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hv_2b", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    assert not hasattr(m, "ownerkey"), "hv imports ownerkey (S2)"
    assert not hasattr(m, "_sign_governance_payload"), "hv has an owner signer (S2)"


def test_the_dispatcher_routes_the_control_plane_verbs():
    """`hive-mind` stays `dispatcher.sh` — already in the signed manifest by its suffix, already on PATH.
    An extensionless second entry point would be skipped by the manifest, which special-cases only the
    name `hv`, so the file holding the owner-key path would ship unsigned."""
    src = (PROJECT / "scripts" / "installer" / "dispatcher.sh").read_text()
    assert "hivemind_ctl.py" in src
    assert "owner|group|admit|config|unforget|retract|remember|decide|entity|capsule|wire|doctor)" in src
    assert not (PROJECT / "hive-mind").exists(), "no extensionless entry point; the manifest would skip it"


# ── the boundary the control plane must not undo ─────────────────────────────────────────────────

def test_the_control_plane_does_not_mutate_sys_path_at_import():
    """Same fault #152 fixed in `ownerkey.py`, and this file is more exposed to it: it is BOTH an entry
    point and an imported module, so an import-time insert would prepend the real source directory and
    hide a staged copy for everything loaded afterwards. Entry-point path setup would be legitimate, but
    it has to happen inside the function the dispatcher runs."""
    import ast
    src = (PROJECT / "hivemind_ctl.py").read_text()
    tree = ast.parse(src)
    for node in tree.body:                       # module level only: a function may legitimately do it
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr in ("insert", "append", "extend"):
                val = sub.value
                if isinstance(val, ast.Attribute) and val.attr == "path" and not isinstance(node, ast.FunctionDef):
                    raise AssertionError("hivemind_ctl mutates sys.path at import time")


def test_hv_does_not_import_the_control_plane():
    """The back door into S2. `hv` must not import `hivemind_ctl`, because the control plane imports
    `ownerkey` — so that edge would give `hv` a TRANSITIVE path to owner signing, and the import-graph
    test in 2b would be satisfied by neither module importing `ownerkey` directly."""
    # Check the AST, not the source text: `hv` mentions this module in a docstring explaining why
    # `dispatch` exists, and a substring scan would fail on documentation. (Third time today that
    # distinction has mattered — a text scan conflates what the code does with what it says.)
    import ast
    imported = set()
    for node in ast.walk(ast.parse((PROJECT / "hv").read_text())):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "hivemind_ctl" not in imported, "hv imports the control plane: a transitive path to ownerkey"
    # and 2b removed exactly this: hv no longer imports ownerkey (the transitive walk is test_s2_split)
    assert "ownerkey" not in imported, "hv imports ownerkey directly (S2)"


def test_the_control_plane_reaches_signing_and_the_library():
    """What it must be able to do, stated positively: reach the library for the projection and journal,
    and reach `ownerkey` for signing. That pairing is why the split needs two modules rather than one."""
    src = (PROJECT / "hivemind_ctl.py").read_text()
    assert "commandmap" in src
    m = hivemind_ctl.hv()
    assert callable(m.dispatch) and callable(m.build_parser)
    assert hasattr(m, "ownerkey"), "the control plane installs the owner steps, which reach ownerkey"
    assert callable(m._sign_governance_payload) and m._CONTROL_PLANE
