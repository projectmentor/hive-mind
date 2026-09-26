"""The owner-key boundary (2.0 S2, public #136), and the library contract the daemon depends on.

`ownerkey.py` is the one place the owner seed is read and a governance signature is produced. The split
will assert it is absent from `hv`'s transitive imports; that cannot be true yet, because the five
signing call sites have not moved, so `hv` still imports it. What IS assertable today is everything that
must hold for that later assertion to be reachable — and those are the properties a future refactor
would quietly break:

  * the dependency points one way (`hv` -> `ownerkey`, never back);
  * `ownerkey` owns no path and no hive location, so it cannot disagree with `hv` about which file the
    owner key is;
  * there is no second implementation of the canonical bytes a signature covers;
  * signing is byte-for-byte what it was before the move.

Plus the compatibility contract this extraction must not break: the symbols the sync daemon and sync
client consume from `hv` as a library.
"""

import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

OWNERKEY_SRC = (PROJECT / "ownerkey.py").read_text()
HV_SRC = (PROJECT / "hv").read_text()


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_boundary", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_boundary", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


# ── the boundary ────────────────────────────────────────────────────────────────────────────────

def test_ownerkey_never_imports_hv():
    """The direction that must never exist. A cycle here would make the split's import-graph test
    unsatisfiable without unpicking it again, and `hv` is what has to stop importing `ownerkey`."""
    for line in OWNERKEY_SRC.splitlines():
        s = line.strip()
        assert not re.match(r"^(import|from)\s+hv\b", s), f"ownerkey.py imports hv: {s!r}"
    assert "load_hv" not in OWNERKEY_SRC        # nor the daemon's file-path loader


def test_ownerkey_owns_no_hive_location():
    """It takes paths as arguments. If it grew its own HIVE_HOME default, two modules would each have an
    opinion about which file the owner key is, and the quieter one would win on some node."""
    # Assert about CODE, not prose: the docstring names HIVE_HOME precisely to say it is not used here.
    import ast
    tree = ast.parse(OWNERKEY_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in ("HIVE_HOME", "HIVE_HOME_DEFAULT"):
            raise AssertionError("ownerkey.py reads HIVE_HOME; paths must be arguments")
    # and no module-level path constant of its own
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.endswith(("_PATH", "_DIR")):
                    raise AssertionError(f"ownerkey.py defines its own {t.id}")
    for fn in ("def load_seed(path)", "def stash(seed, stash_dir)"):
        assert fn in OWNERKEY_SRC, f"{fn} must take its path as an argument"


def test_ownerkey_does_not_touch_sys_path():
    """A library module must not decide import resolution for the process.

    An earlier version inserted this file's resolved parent at sys.path[0]. In a staged or shadowed
    install the module is a symlink, so that resolves to the REAL source directory and prepends it,
    silently shadowing the staged copy for every later import — `hv doctor`'s crypto self-test then
    loaded the genuine KAT vectors instead of the staged ones and reported ok on a tampered install.
    The entry point owns sys.path: `hv` inserts its own directory before importing this."""
    import ast
    for node in ast.walk(ast.parse(OWNERKEY_SRC)):
        if isinstance(node, ast.Attribute) and node.attr in ("insert", "append", "extend"):
            val = node.value
            if isinstance(val, ast.Attribute) and val.attr == "path":
                raise AssertionError("ownerkey.py manipulates sys.path; the entry point owns it")
    assert "import sys" not in OWNERKEY_SRC


def test_there_is_no_third_canonicaliser():
    """`hv` and `merkle` already carry byte-identical `_canonical` copies, each documented as having to
    match the other. A third copy — in the module that produces the bytes a signature covers — would turn
    a divergence from a local bug into a signature that fails to verify fleet-wide."""
    assert "def _canonical" not in OWNERKEY_SRC
    assert "merkle._canonical" in OWNERKEY_SRC


def test_hv_reads_the_seed_only_through_ownerkey():
    """One reader of the seed file. `hv`'s wrapper supplies the path; the read lives in `ownerkey`."""
    import ownerkey
    assert "ownerkey.load_seed(OWNER_KEY_PATH)" in HV_SRC
    body = HV_SRC[HV_SRC.index("def _owner_seed():"):HV_SRC.index("def _owner_id_for_pub")]
    assert "read_text" not in body and "b64decode" not in body, "hv still decodes the seed itself"
    assert "read_text" in (PROJECT / "ownerkey.py").read_text()
    assert callable(ownerkey.load_seed)


def test_verify_governance_deliberately_did_not_move():
    """Signing moves; verifying must not. Every node projects governance, including agent nodes that can
    never produce an owner signature, so the verifier has to stay in the data plane."""
    assert "def _verify_governance(payload):" in HV_SRC
    assert "def _verify_governance" not in OWNERKEY_SRC


# ── behaviour is unchanged by the move ──────────────────────────────────────────────────────────

def test_signing_is_byte_identical_and_still_verifies(tmp_path, monkeypatch):
    import os
    import ownerkey
    hv = _loadhv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    payload = {"action": "set-config", "key": "forget_writers", "value": "owner"}

    through_hv = hv._sign_governance_payload(dict(payload), seed, pub)
    direct = ownerkey.sign_governance(dict(payload), seed, pub)
    assert through_hv == direct                                   # the wrapper adds nothing
    assert hv._verify_governance(through_hv) == hv._owner_id_for_pub(pub)   # and it still verifies

    # the signed body is merkle's canonical bytes over the payload without owner_sig
    body = hv.merkle._canonical({k: v for k, v in through_hv.items() if k != "owner_sig"})
    assert hv._ed25519.verify(body, hv.base64.b64decode(through_hv["owner_sig"]), pub)


@pytest.mark.parametrize("content,why", [
    (None, "absent file"),
    ("", "empty file"),
    ("not base64 at all !!", "undecodable"),
    ("c2hvcnQ=", "decodes to the wrong length"),
])
def test_load_seed_answers_no_for_every_failure(tmp_path, content, why):
    """Every caller's question is "can this device owner-sign?", so every failure answers None rather
    than raising — an exception here would crash a read-only command on a member node."""
    import ownerkey
    p = tmp_path / ".owner-key"
    if content is not None:
        p.write_text(content)
    assert ownerkey.load_seed(p) is None, why


def test_load_seed_round_trips_a_real_seed(tmp_path):
    import base64
    import os
    import ownerkey
    seed = os.urandom(32)
    p = tmp_path / ".owner-key"
    p.write_text(base64.b64encode(seed).decode() + "\n")
    assert ownerkey.load_seed(p) == seed


def test_stash_writes_0600_in_a_0700_dir(tmp_path):
    import os
    import ownerkey
    seed = os.urandom(32)
    out = ownerkey.stash(seed, tmp_path / "identity")
    assert out is not None
    f = Path(out)
    assert f.stat().st_mode & 0o777 == 0o600
    assert f.parent.stat().st_mode & 0o777 == 0o700
    assert ownerkey.load_seed(f) == seed        # and it is a usable backup, not just bytes


# ── the library contract the daemon depends on ──────────────────────────────────────────────────

def test_every_hv_symbol_the_daemon_uses_still_resolves(tmp_path, monkeypatch):
    """`sync_common.load_hv()` loads `hv` by file path and the daemon and client consume it as a library.
    The extraction must not move one of those names out from under them — this is the contract that makes
    the later split safe to attempt, and nothing else in the suite states it."""
    used = set()
    for name in ("hive_sync_daemon.py", "sync_common.py", "sync_client.py"):
        for m in re.finditer(r"\bhv\.([A-Za-z_][A-Za-z0-9_]*)", (PROJECT / name).read_text()):
            used.add(m.group(1))
    assert len(used) >= 25, f"expected the known library surface, found {len(used)}"
    hv = _loadhv(tmp_path, monkeypatch)
    missing = sorted(n for n in used if not hasattr(hv, n))
    assert not missing, f"the daemon consumes hv symbols that no longer exist: {missing}"
