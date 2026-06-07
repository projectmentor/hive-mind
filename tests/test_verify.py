"""`hv verify` — integrity + signature + key-anchor layers.

Builds an isolated install dir (a copy of hv + its sibling modules), mints a throwaway signing
key, computes a manifest over that dir, signs it, and drives `hv verify` as a subprocess with the
key-anchor URL pointed at a local file:// (so no network). Proves each defense and each failure.
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import ed25519  # noqa: E402


def _load_manifest_fn(install):
    loader = importlib.machinery.SourceFileLoader("hv_under_test", str(install / "hv"))
    spec = importlib.util.spec_from_loader("hv_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod._source_manifest


@pytest.fixture
def install(tmp_path):
    """An isolated, signed HiveMind install with a throwaway key."""
    d = tmp_path / "hm"
    d.mkdir()
    for f in ("hv", "ed25519.py", "merkle.py"):
        shutil.copy(PROJECT / f, d / f)
    manifest_fn = _load_manifest_fn(d)
    seed = os.urandom(32)
    (d / "hivemind.pub").write_text(base64.b64encode(ed25519.pub_from_seed(seed)).decode() + "\n")
    (d / "other.pub").write_text(base64.b64encode(ed25519.pub_from_seed(os.urandom(32))).decode() + "\n")

    def regen_and_sign():
        m = manifest_fn(str(d))
        (d / "verify.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
        sig = ed25519.sign((d / "verify.json").read_bytes(), seed)
        (d / "verify.json.sig").write_text(base64.b64encode(sig).decode() + "\n")

    regen_and_sign()
    return d, regen_and_sign


def _verify(install_dir, pubkey_url):
    env = dict(os.environ, HIVE_PUBKEY_URL=pubkey_url)
    r = subprocess.run([sys.executable, str(install_dir / "hv"), "verify"],
                       env=env, capture_output=True, text=True)
    return r.returncode, r.stdout


def test_genuine_full_pass(install):
    d, _ = install
    rc, out = _verify(d, f"file://{d}/hivemind.pub")
    assert rc == 0
    assert "✓ Official" in out and "verified" in out


def test_fork_with_its_own_key_is_caught(install):
    d, _ = install
    rc, out = _verify(d, f"file://{d}/other.pub")
    assert "does NOT match" in out and "fork or an impostor" in out


def test_offline_anchor_degrades_gracefully(install):
    d, _ = install
    rc, out = _verify(d, "http://127.0.0.1:1/unreachable")
    assert "✓ Official" in out and "integrity OK and the release signature is valid" in out


def test_tampered_signature_is_caught(install):
    d, _ = install
    p = d / "verify.json.sig"
    raw = bytearray(base64.b64decode(p.read_text()))
    raw[0] ^= 1
    p.write_text(base64.b64encode(bytes(raw)).decode() + "\n")
    rc, out = _verify(d, f"file://{d}/hivemind.pub")
    assert "SIGNATURE INVALID" in out


def test_modified_source_is_caught(install):
    d, _ = install
    (d / "merkle.py").write_text((d / "merkle.py").read_text() + "\n# tampered\n")
    rc, out = _verify(d, f"file://{d}/hivemind.pub")
    assert "MODIFIED locally" in out


def test_missing_signature_falls_back_to_integrity(install):
    d, _ = install
    (d / "verify.json.sig").unlink()
    rc, out = _verify(d, f"file://{d}/hivemind.pub")
    assert "Integrity OK" in out and "predates signed releases" in out


def test_never_raises_on_missing_manifest(install):
    d, _ = install
    (d / "verify.json").unlink()
    rc, out = _verify(d, f"file://{d}/hivemind.pub")
    assert rc == 0 and "No manifest" in out
