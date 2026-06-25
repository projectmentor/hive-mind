"""Phase 2 — capsules: encrypt-to-device sealing. Human-review-gated crypto.

Exercises _capsule_build / _capsule_open directly (so the multi-recipient, exclusion, tamper, and
downgrade properties are pinned independent of the sync layer) plus the `hv capsule` CLI round-trip.
"""

import base64
import copy
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import x25519        # noqa: E402
import ed25519       # noqa: E402


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_cap", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_cap", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _kp():
    seed = os.urandom(32)
    return seed, x25519.ed_seed_to_curve_scalar(seed), x25519.ed_pub_to_curve_pub(ed25519.pub_from_seed(seed))


def test_multi_recipient_open_and_nonrecipient_excluded(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (_a, a_sc, a_cp) = _kp(); (_b, b_sc, b_cp) = _kp(); (_c, c_sc, c_cp) = _kp()
    cap = hv._capsule_build("TOK", "credential", b"the-secret",
                            {"k1:a": a_cp, "k1:b": b_cp}, 1, "hive1", "k1:a")
    assert hv._capsule_open(cap, a_sc, "k1:a") == b"the-secret"     # recipient A
    assert hv._capsule_open(cap, b_sc, "k1:b") == b"the-secret"     # recipient B
    with pytest.raises(ValueError):                                 # C: not in the wraps at all
        hv._capsule_open(cap, c_sc, "k1:c")
    with pytest.raises(ValueError):                                 # C using B's slot: wrap tag fails
        hv._capsule_open(cap, c_sc, "k1:b")


def test_tampered_ciphertext_and_wrap_rejected(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (_a, a_sc, a_cp) = _kp()
    cap = hv._capsule_build("TOK", "credential", b"v", {"k1:a": a_cp}, 1, "h", "k1:a")
    bad_ct = copy.deepcopy(cap)
    ct = bytearray(base64.b64decode(bad_ct["ct"])); ct[0] ^= 1
    bad_ct["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(ValueError):
        hv._capsule_open(bad_ct, a_sc, "k1:a")                      # payload tag mismatch
    bad_wrap = copy.deepcopy(cap)
    wt = bytearray(base64.b64decode(bad_wrap["wraps"][0]["wrap_tag"])); wt[0] ^= 1
    bad_wrap["wraps"][0]["wrap_tag"] = base64.b64encode(bytes(wt)).decode()
    with pytest.raises(ValueError):
        hv._capsule_open(bad_wrap, a_sc, "k1:a")                    # wrap tag mismatch


def test_alg_downgrade_refused(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (_a, a_sc, a_cp) = _kp()
    cap = hv._capsule_build("TOK", "credential", b"v", {"k1:a": a_cp}, 1, "h", "k1:a")
    forged = copy.deepcopy(cap); forged["alg"] = "rot13-v1"
    with pytest.raises(ValueError):
        hv._capsule_open(forged, a_sc, "k1:a")


def test_nonce_and_ciphertext_are_fresh_per_seal(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (_a, _sc, a_cp) = _kp()
    c1 = hv._capsule_build("X", "credential", b"same", {"k1:a": a_cp}, 1, "h", "k1:a")
    c2 = hv._capsule_build("X", "credential", b"same", {"k1:a": a_cp}, 1, "h", "k1:a")
    assert c1["nonce"] != c2["nonce"] and c1["ct"] != c2["ct"]     # fresh CEK+nonce, no keystream reuse


def test_cli_put_get_rotate_roundtrip(tmp_path):
    """End-to-end through the real CLI on one device (solo hive seals to itself)."""
    home = tmp_path / "h"
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))

    def run(*args, stdin=None):
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                           input=stdin, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r
    run("key", "init")
    run("capsule", "put", "MYTOK", "--stdin", stdin="hunter2")
    assert run("capsule", "get", "MYTOK", "--raw").stdout == "hunter2"
    run("capsule", "rotate", "MYTOK")
    assert run("capsule", "get", "MYTOK", "--raw").stdout == "hunter2"   # still opens after rotate
