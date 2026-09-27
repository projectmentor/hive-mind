"""The suite never reads or writes the developer's exported hive (#160).

`hv` resolves every path it uses (journal, store, genesis pin, keys) from HIVE_HOME when it is imported.
conftest used to leave HIVE_HOME alone, so an in-process loader that did not set its own read whatever the
developer exported: on gregorius and EGMBL5A, the live hive. conftest now replaces it with a sandbox at
import, `isolation` with a per-test one, and a helper that means a particular hive says so
(`_planes.load_hv(home)`). The session guard holds the exported hive's key material to unchanged, and a
hive marked as a decoy to byte-identical. These tests prove each part, including by running a test that
used to fail this way against a decoy.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import _planes
import _realhome
import conftest

PROJECT = Path(__file__).resolve().parent.parent


def _decoy(tmp_path):
    """A live-looking hive (an owner, a genesis pin, keys, a journal) marked as a decoy."""
    decoy = tmp_path / "decoy-real-hive"
    env = dict(os.environ, HIVE_HOME=str(decoy), HIVE_IDENTITY_STASH=str(tmp_path / "decoy-stash"))
    for argv in (["hv", "key", "init"], ["hivemind_ctl.py", "owner", "init"]):
        r = subprocess.run([sys.executable, str(PROJECT / argv[0]), *argv[1:]], env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    (decoy / _realhome.DECOY_MARKER).write_text("the test suite must leave this hive byte-identical\n")
    return decoy


def test_every_test_runs_with_a_sandbox_hive_home(isolation):
    here = Path(os.environ["HIVE_HOME"])
    assert here.is_relative_to(isolation.root), "isolation gives each test its own HIVE_HOME"
    assert conftest.SANDBOX_HIVE.is_dir() and conftest.SANDBOX_HIVE != conftest.REAL["hive"]
    m = _planes.load_hv(here, "hv_sandbox_probe", control_plane=False)
    assert Path(m.JOURNAL_DIR).is_relative_to(isolation.root)


def test_a_loader_for_a_hive_resolves_that_hive_whatever_hive_home_says(tmp_path, monkeypatch):
    mine, other = tmp_path / "mine", tmp_path / "other"
    monkeypatch.setenv("HIVE_HOME", str(other))
    m = _planes.load_hv(mine, "hv_for_mine")
    assert Path(m.JOURNAL_DIR) == mine / "journal" and Path(m.GENESIS_PIN_PATH) == mine / ".genesis-pin"
    assert os.environ["HIVE_HOME"] == str(other), "load_hv puts HIVE_HOME back"


def test_a_test_that_used_to_read_the_exported_hive_passes_against_a_decoy(tmp_path):
    """The issue's first observation, as a regression: with HIVE_HOME exported to a live-looking hive,
    `test_standby_declaration_visible` failed (its `_gov` read that hive's genesis pin). Run it, and the
    other `_gov` user, in a fresh session with HIVE_HOME exported to a decoy: both pass, and the inner
    session's guard (strict in decoy mode) and this test both find the decoy byte-identical."""
    decoy = _decoy(tmp_path)
    before = _realhome.decoy_state(decoy)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["HIVE_HOME"] = str(decoy)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                        "tests/test_succession.py::test_standby_declaration_visible", "tests/test_group.py"],
                       cwd=PROJECT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert _realhome.decoy_state(decoy) == before


def test_the_guard_fails_on_a_write_into_the_exported_hive(tmp_path):
    """Fail-closed (h:157bd5e469): a key written into the exported hive, and any write into a decoy, are
    reported by the same comparison the session guard makes."""
    claude, stash, hive = tmp_path / "c", tmp_path / "s", tmp_path / "hive"
    for d in (claude, stash, hive / "journal"):
        d.mkdir(parents=True)
    before = _realhome.snapshot(claude, stash, hive)
    for name in _realhome.HIVE_KEY_FILES:
        (hive / name).write_text("x\n")
    changes = _realhome.diff(before, _realhome.snapshot(claude, stash, hive))
    assert sorted(changes) == sorted(f"real hive {n} created" for n in _realhome.HIVE_KEY_FILES)

    # A live hive's journal moving is a note, not a failure: its daemon and other sessions write it.
    act = _realhome.activity(hive)
    (hive / "journal" / "k1_peer.jsonl").write_text("{}\n")
    assert _realhome.activity(hive) != act and _realhome.decoy_state(hive) is None

    # A decoy is held to every byte, journal included.
    (hive / _realhome.DECOY_MARKER).write_text("decoy\n")
    d0 = _realhome.decoy_state(hive)
    with open(hive / "journal" / "k1_peer.jsonl", "a") as f:
        f.write("{}\n")
    assert _realhome.decoy_diff(d0, _realhome.decoy_state(hive)) == ["decoy hive journal/k1_peer.jsonl changed"]


@pytest.mark.parametrize("mod", ["test_group", "test_succession"])
def test_the_helpers_that_mean_a_hive_use_the_explicit_loader(mod):
    """`_gov(home)` computes governance from `home`'s journal, so it must also read `home`'s pin: it loads
    `hv` through `_planes.load_hv(home, …)`, never a bare SourceFileLoader."""
    import ast
    tree = ast.parse((PROJECT / "tests" / f"{mod}.py").read_text())
    gov = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_gov")
    calls = [n for n in ast.walk(gov) if isinstance(n, ast.Call)]
    names = {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in calls}
    assert "load_hv" in names and "SourceFileLoader" not in names, names
