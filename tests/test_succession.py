"""Owner resilience (Effort 1). Phase 1: owner-key backup/restore + standby declaration.
Drives the real `hv` CLI against an isolated temp HIVE_HOME (mirrors tests/test_governance.py),
and loads `hv` as a module to exercise the stdlib seal/unseal crypto directly."""

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import subprocess  # noqa: E402


def _run(home, *args, node_id=None, passphrase=None, check=True):
    env = dict(os.environ, HIVE_HOME=str(home))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    if passphrase is not None:
        env["HIVE_OWNER_PASSPHRASE"] = passphrase     # non-interactive passphrase for tests
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _gov(home):
    loader = importlib.machinery.SourceFileLoader("hvmod_succ", str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("hvmod_succ", loader))
    loader.exec_module(m)
    import merkle
    return m, m._governance_state(merkle.read_all_entries(str(home / "journal")))


def _owner_id(home):
    return _run(home, "owner", "show").stdout.splitlines()[0].split()[1]


def test_export_import_round_trip_resumes_same_owner(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    oid = _owner_id(home)
    keyfile = tmp_path / "owner.key"
    _run(home, "owner", "export", "--out", str(keyfile))
    assert keyfile.exists()
    (home / ".owner-key").unlink()                          # lose the owner device's key
    assert "does NOT hold the owner key" in _run(home, "owner", "show").stdout
    _run(home, "owner", "import", str(keyfile))             # restore on (this stand-in for) a new device
    show = _run(home, "owner", "show").stdout
    assert "holds the owner key" in show and oid in show    # same owner_id resumes


def test_import_mismatch_is_refused(tmp_path):
    import base64
    import json
    home = tmp_path
    _run(home, "owner", "init")
    bad = tmp_path / "bad.key"
    bad.write_text(json.dumps({"enc": "none", "owner_id": "o1:deadbeef",
                               "seed": base64.b64encode(os.urandom(32)).decode()}))
    out = _run(home, "owner", "import", str(bad), check=False).stdout
    assert "Refusing" in out
    out2 = _run(home, "owner", "import", str(bad), "--force", check=False).stdout
    assert "installed" in out2.lower()                      # --force overrides


def test_passphrase_seal_unseal_roundtrip_and_wrong_pass():
    loader = importlib.machinery.SourceFileLoader("hvmod_crypto", str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("hvmod_crypto", loader))
    loader.exec_module(m)
    seed = os.urandom(32)
    env = m._owner_seal(seed, "correct horse")
    assert env["enc"] == "scrypt-ctr-hmac-v1"
    assert m._owner_unseal(env, "correct horse") == seed
    try:
        m._owner_unseal(env, "wrong")
        assert False, "wrong passphrase should raise"
    except ValueError:
        pass


def test_standby_declaration_visible(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "owner", "standby", "k1:standbydev")
    _, gov = _gov(home)
    assert "k1:standbydev" in gov["standbys"]
    assert "k1:standbydev" in _run(home, "owner", "show").stdout
    _run(home, "owner", "standby", "k1:standbydev", "--off")
    _, gov = _gov(home)
    assert "k1:standbydev" not in gov["standbys"]


def test_hive_escrow_restore_round_trip(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    oid = _owner_id(home)
    _run(home, "owner", "escrow", passphrase="correct-horse-battery")   # encrypted blob into the journal
    _, gov = _gov(home)                                                  # it's an owner-signed governance entry
    import merkle
    entries = merkle.read_all_entries(str(home / "journal"))
    assert any(e.get("payload", {}).get("action") == "owner-escrow" for e in entries)
    (home / ".owner-key").unlink()                                      # lose the device's key
    assert "does NOT hold the owner key" in _run(home, "owner", "show").stdout
    out = _run(home, "owner", "restore", passphrase="correct-horse-battery").stdout
    assert "recovered from the hive" in out
    show = _run(home, "owner", "show").stdout
    assert "holds the owner key" in show and oid in show                # same owner resumes


def test_hive_escrow_refuses_short_passphrase(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    out = _run(home, "owner", "escrow", passphrase="short").stdout
    assert "too short" in out
    import merkle
    entries = merkle.read_all_entries(str(home / "journal"))
    assert not any(e.get("payload", {}).get("action") == "owner-escrow" for e in entries)


def test_hive_restore_wrong_passphrase_fails(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "owner", "escrow", passphrase="correct-horse-battery")
    out = _run(home, "owner", "restore", passphrase="wrong-passphrase-xx").stdout
    assert "Could not decrypt" in out


def test_backcompat_no_resilience_entries_equals_today(tmp_path):
    # A plain owner+admit hive must project identically to before (no standbys, owner unchanged).
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "group", "admit", "k1:aaa", "--principal", "alice")
    _, gov = _gov(home)
    assert gov["owner_id"] and "k1:aaa" in gov["admitted"] and gov["standbys"] == set()
