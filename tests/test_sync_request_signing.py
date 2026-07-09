"""Unit tests for sync read-authentication (GHSA-242f-7fxg-f7wm).

Covers the pure signing/canonicalization helpers in sync_common and the daemon's request verifier
(_verify_sync_request) in isolation — no wire. The verifier depends only on hv's Ed25519 module and
pure helpers, not on HIVE_HOME, so a synthetic `gov` dict drives the admission cases. The HTTP
integration (loopback trust, sync_auth modes, discovery-open, /api 403) is covered in
tests/test_sync_auth.py.
"""

import base64
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
# A throwaway HIVE_HOME so importing the daemon/hv never touches the real corpus (the verifier reads
# no journal — it takes `gov` as an argument).
os.environ.setdefault("HIVE_HOME", tempfile.mkdtemp(prefix="hive-authtest-"))

import ed25519  # noqa: E402
import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402


def _dev():
    seed = os.urandom(32)
    pub = ed25519.pub_from_seed(seed)
    return seed, pub, "k1:" + hashlib.sha256(pub).hexdigest()[:16]


def _headers(seed, method, path, query, body=b"", ts=None, nonce=None, device_override=None):
    pub = ed25519.pub_from_seed(seed)
    ts = int(time.time()) if ts is None else int(ts)
    nonce = os.urandom(16).hex() if nonce is None else nonce
    msg = sync_common.sync_signing_bytes(method, path, query, body, ts, nonce)
    sig = ed25519.sign(msg, seed)
    return {
        "Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG,
        "Hive-Auth-Device": device_override or ("k1:" + hashlib.sha256(pub).hexdigest()[:16]),
        "Hive-Auth-Pub": base64.b64encode(pub).decode(),
        "Hive-Auth-Ts": str(ts),
        "Hive-Auth-Nonce": nonce,
        "Hive-Auth-Sig": base64.b64encode(sig).decode(),
    }


def _gov(admitted=(), purged=(), owner="o1:owner"):
    return {"owner_id": owner, "admitted": set(admitted), "purged": set(purged)}


# ── canonicalization / signing bytes ─────────────────────────────────────────────────────────────

def test_signing_bytes_stable():
    a = sync_common.sync_signing_bytes("GET", "/sync/chunk", "a=1&b=2", b"", 100, "n")
    b = sync_common.sync_signing_bytes("GET", "/sync/chunk", "a=1&b=2", b"", 100, "n")
    assert a == b
    # any field change flips the bytes
    assert a != sync_common.sync_signing_bytes("POST", "/sync/chunk", "a=1&b=2", b"", 100, "n")
    assert a != sync_common.sync_signing_bytes("GET", "/sync/hello", "a=1&b=2", b"", 100, "n")
    assert a != sync_common.sync_signing_bytes("GET", "/sync/chunk", "a=1&b=3", b"", 100, "n")
    assert a != sync_common.sync_signing_bytes("GET", "/sync/chunk", "a=1&b=2", b"x", 100, "n")
    assert a != sync_common.sync_signing_bytes("GET", "/sync/chunk", "a=1&b=2", b"", 101, "n")
    assert a != sync_common.sync_signing_bytes("GET", "/sync/chunk", "a=1&b=2", b"", 100, "m")


def test_canonical_query_order_independent():
    assert sync_common.canonical_query("b=2&a=1") == sync_common.canonical_query("a=1&b=2")
    assert sync_common.sync_signing_bytes("GET", "/p", "b=2&a=1", b"", 1, "n") == \
        sync_common.sync_signing_bytes("GET", "/p", "a=1&b=2", b"", 1, "n")


def test_query_order_independent_end_to_end():
    seed, _, _ = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "node=k1:x&start=1&end=9")
    ok, reason, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "end=9&node=k1:x&start=1", b"",
                                           _gov(owner=None))
    assert ok, reason


def test_body_hash_bound():
    seed, _, dev = _dev()
    h = _headers(seed, "POST", "/sync/ingest", "", body=b'{"entries":[]}')
    ok, _, _ = d._verify_sync_request(h, "POST", "/sync/ingest", "", b'{"entries":[]}', _gov(owner=None))
    assert ok
    bad, reason, _ = d._verify_sync_request(h, "POST", "/sync/ingest", "", b'{"entries":[1]}', _gov(owner=None))
    assert not bad and reason == "bad-signature"


# ── verifier: signature / envelope integrity ─────────────────────────────────────────────────────

def test_sign_then_verify_roundtrip_admitted():
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "node=k1:x")
    ok, reason, who = d._verify_sync_request(h, "GET", "/sync/chunk", "node=k1:x", b"", _gov(admitted=[dev]))
    assert ok and who == dev, reason


def test_missing_headers_blocked():
    ok, reason, _ = d._verify_sync_request({}, "GET", "/sync/chunk", "", b"", _gov(owner=None))
    assert not ok and reason == "missing-auth-headers"


def test_bad_signature_blocked():
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "node=k1:x")
    h["Hive-Auth-Sig"] = base64.b64encode(b"\x00" * 64).decode()
    ok, reason, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "node=k1:x", b"", _gov(admitted=[dev]))
    assert not ok and reason == "bad-signature"


def test_pub_fingerprint_mismatch_blocked():
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "", device_override="k1:deadbeefdeadbeef")
    ok, reason, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "", b"", _gov(admitted=[dev]))
    assert not ok and reason == "pub-fingerprint-mismatch"


def test_stale_timestamp_blocked():
    seed, _, dev = _dev()
    old = int(time.time()) - (sync_common.SYNC_AUTH_WINDOW + 60)
    h = _headers(seed, "GET", "/sync/chunk", "", ts=old)
    ok, reason, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "", b"", _gov(admitted=[dev]))
    assert not ok and reason == "stale-timestamp"


def test_replayed_nonce_blocked():
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "x=1", nonce="fixed-nonce-" + os.urandom(4).hex())
    ok1, _, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "x=1", b"", _gov(admitted=[dev]))
    ok2, reason2, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "x=1", b"", _gov(admitted=[dev]))
    assert ok1 and not ok2 and reason2 == "replayed-nonce"


# ── verifier: admission ──────────────────────────────────────────────────────────────────────────

def test_unadmitted_device_blocked():
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "")
    ok, reason, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "", b"", _gov(admitted=[]))
    assert not ok and reason == "not-admitted"


def test_purged_device_blocked():
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "")
    ok, reason, _ = d._verify_sync_request(h, "GET", "/sync/chunk", "", b"", _gov(admitted=[dev], purged=[dev]))
    assert not ok and reason == "not-admitted"


def test_pre_owner_signed_accepted():
    # Pre-owner (no owner_id yet): a valid signature is accepted without an admitted-set check, so
    # genesis/owner-declaration can still propagate during bootstrap.
    seed, _, dev = _dev()
    h = _headers(seed, "GET", "/sync/chunk", "")
    ok, reason, who = d._verify_sync_request(h, "GET", "/sync/chunk", "", b"", _gov(admitted=[], owner=None))
    assert ok and who == dev, reason


# ── bind resolution / auth mode ──────────────────────────────────────────────────────────────────

def test_resolve_bind_env_override(monkeypatch):
    monkeypatch.setenv("HIVE_BIND", "10.9.8.7")
    assert sync_common.resolve_bind({"bind": None}) == "10.9.8.7"


def test_resolve_bind_explicit_peers_bind(monkeypatch):
    monkeypatch.delenv("HIVE_BIND", raising=False)
    assert sync_common.resolve_bind({"bind": "192.168.1.5"}) == "192.168.1.5"


def test_resolve_bind_prefers_tailscale(monkeypatch):
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: "100.1.2.3")
    assert sync_common.resolve_bind({"bind": None}) == "100.1.2.3"


def test_resolve_bind_falls_back_loopback(monkeypatch):
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: None)
    assert sync_common.resolve_bind({"bind": None}) == "127.0.0.1"


def test_resolve_bind_ignores_legacy_all_interfaces(monkeypatch):
    # An existing node's .peers.json still carries the old installer's "0.0.0.0"; treat it as auto so
    # the node hardens to its tailnet IP on restart rather than staying on all-interfaces.
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: "100.100.1.2")
    assert sync_common.resolve_bind({"bind": "0.0.0.0"}) == "100.100.1.2"


def test_resolve_bind_env_0000_is_escape_hatch(monkeypatch):
    monkeypatch.setenv("HIVE_BIND", "0.0.0.0")     # deliberate all-interfaces is only via HIVE_BIND
    assert sync_common.resolve_bind({"bind": None}) == "0.0.0.0"


def test_is_tailnet_ip_cgnat_range():
    assert sync_common._is_tailnet_ip("100.64.0.0")
    assert sync_common._is_tailnet_ip("100.84.84.100")
    assert sync_common._is_tailnet_ip("100.127.255.255")
    assert not sync_common._is_tailnet_ip("100.63.255.255")   # just below the 100.64/10 range
    assert not sync_common._is_tailnet_ip("100.128.0.0")      # just above
    assert not sync_common._is_tailnet_ip("10.0.0.1")
    assert not sync_common._is_tailnet_ip("not-an-ip")


def test_locally_bindable_rejects_nonlocal():
    # The WSL2 guard: a tailnet IP that belongs to the WINDOWS host (or any non-local address) is not
    # bindable here, so tailscale_ip() rejects it instead of returning an address the daemon can't bind.
    assert sync_common._locally_bindable("127.0.0.1")
    assert not sync_common._locally_bindable("203.0.113.1")   # TEST-NET-3, never local


def test_tailscale_ip_rejects_unbindable(monkeypatch):
    # Simulate `tailscale ip` reporting only a non-local (Windows-host) tailnet IP → None, not a crash.
    class _R:
        returncode = 0
        stdout = "100.114.200.119\n"
    monkeypatch.setattr(sync_common.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(sync_common, "_locally_bindable", lambda ip: False)
    assert sync_common.tailscale_ip() is None


def test_sync_auth_mode_env_override(monkeypatch):
    monkeypatch.setenv("HIVE_SYNC_AUTH", "enforce")
    assert sync_common.sync_auth_mode({"sync_auth": "off"}) == "enforce"


def test_sync_auth_mode_from_cfg(monkeypatch):
    monkeypatch.delenv("HIVE_SYNC_AUTH", raising=False)
    assert sync_common.sync_auth_mode({"sync_auth": "enforce"}) == "enforce"
    assert sync_common.sync_auth_mode({}) == "permissive"      # default
    assert sync_common.sync_auth_mode({"sync_auth": "bogus"}) == "permissive"
