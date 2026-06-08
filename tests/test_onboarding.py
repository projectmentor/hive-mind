"""Onboarding: hive_id minting + projection, and the signed genesis used for discovery.
Network scoping (cross-hive no-merge, /hive/info, /sync/ingest 409) is covered by sync_smoke.sh."""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _run(home, *args):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args],
                       env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _loadhv():
    m = importlib.machinery.SourceFileLoader("hvmod_ob", str(PROJECT / "hv"))
    s = importlib.util.spec_from_loader("hvmod_ob", m)
    mod = importlib.util.module_from_spec(s)
    m.exec_module(mod)
    return mod


def _entries(home):
    import merkle
    return merkle.read_all_entries(str(Path(home) / "journal"))


def test_owner_init_mints_a_hive_id(tmp_path):
    out = _run(tmp_path, "owner", "init").stdout
    assert "hive_id: h1:" in out
    hv = _loadhv()
    gov = hv._governance_state(_entries(tmp_path))
    assert gov["hive_id"].startswith("h1:") and gov["owner_id"].startswith("o1:")


def test_two_hives_get_different_ids(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _run(a, "owner", "init")
    _run(b, "owner", "init")
    hv = _loadhv()
    ha = hv._governance_state(_entries(a))["hive_id"]
    hb = hv._governance_state(_entries(b))["hive_id"]
    assert ha and hb and ha != hb


def test_owner_declaration_is_verifiable(tmp_path):
    _run(tmp_path, "owner", "init")
    hv = _loadhv()
    ents = _entries(tmp_path)
    gen = hv._owner_declaration(ents)
    assert gen is not None
    # the genesis verifies to the established owner (this is what /hive/info hands a joiner)
    assert hv._verify_governance(gen) == hv._governance_state(ents)["owner_id"]
    # no hive yet -> no declaration
    assert hv._owner_declaration([]) is None
