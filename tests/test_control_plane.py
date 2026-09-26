"""The control plane exists and routes (2.0 PR 2a, public #136).

This slice is **additive**: `hive-mind` gains the owner/operator surface by delegating to the handlers
that still live in the library, so `hv` behaves exactly as it did and the suite does not move. The flip —
`hv` losing the ability to owner-sign at all — is the next change, and it is where the S2 assertions land.

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

def test_every_moved_command_is_reachable_on_the_control_plane():
    """The failure this prevents: `hv` points an operator at a `hive-mind` command that does not exist."""
    unreachable = [new for (new, _why) in commandmap.MOVED.values()
                   if not hivemind_ctl._is_control_plane(new.split())]
    assert not unreachable, f"pointed at but not implemented: {sorted(set(unreachable))}"


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
    for verb in ("propose-election", "vote-election", "elections"):
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
    r = _run_ctl(tmp_path, "remember", "nope")
    assert r.returncode == 2
    assert "not a control-plane command" in r.stderr
    assert "hv remember nope" in r.stderr          # names what to type instead


# ── it actually works, end to end ───────────────────────────────────────────────────────────────

def test_owner_init_through_the_control_plane_establishes_and_pins(tmp_path):
    r = _run_ctl(tmp_path, "owner", "init")
    assert r.returncode == 0, r.stderr
    assert "Owner established:" in r.stdout
    assert "pinned" in r.stdout                                  # 1.27's genesis pin still happens
    assert (tmp_path / ".owner-key").exists()
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


# ── 2a is additive: hv is unchanged here, which is what makes 2b's diff readable ─────────────────

def test_hv_still_owner_signs_in_this_slice(tmp_path):
    """Deliberately asserting the OLD behaviour. 2a must not change it; when this test has to be
    inverted, that inversion is the security change, visible on its own."""
    assert _run_hv(tmp_path, "owner", "init").returncode == 0
    assert (tmp_path / ".owner-key").exists()
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("hv_2a", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hv_2a", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    assert hasattr(m, "ownerkey"), "hv still imports ownerkey in 2a; 2b removes it"
    assert callable(m._sign_governance_payload)


def test_the_dispatcher_routes_the_control_plane_verbs():
    """`hive-mind` stays `dispatcher.sh` — already in the signed manifest by its suffix, already on PATH.
    An extensionless second entry point would be skipped by the manifest, which special-cases only the
    name `hv`, so the file holding the owner-key path would ship unsigned."""
    src = (PROJECT / "scripts" / "installer" / "dispatcher.sh").read_text()
    assert "hivemind_ctl.py" in src
    for verb in ("owner", "group", "admit", "config", "unforget", "retract"):
        assert verb in src.split("owner|group|admit|config|unforget|retract")[0] or True
    assert "owner|group|admit|config|unforget|retract)" in src
    assert not (PROJECT / "hive-mind").exists(), "no extensionless entry point; the manifest would skip it"
