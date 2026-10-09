"""The daemon's per-node /api/* proxy signs a read only to an address that proved itself (outbound enforce).

In-process: the daemon's Handler on a 127.0.0.1 server and a stand-in peer; hv's address and verification
lookups are replaced, so this pins the proxy's own gate and its refusal to follow a redirect.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-proxy-")

import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402

DEV = "k1:" + "a" * 16


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def rig(monkeypatch):
    seen = {"peer": [], "elsewhere": []}

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["elsewhere"].append(dict(self.headers))
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
        log_message = lambda *a: None  # noqa: E731

    elsewhere = _serve(Elsewhere)

    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["peer"].append(dict(self.headers))
            if self.path.startswith("/api/status"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_address[1]}/x")
                self.end_headers()
                return
            body = b'{"available": true}'
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        log_message = lambda *a: None  # noqa: E731

    peer = _serve(Peer)
    daemon = _serve(d.Handler)
    addr = f"127.0.0.1:{peer.server_address[1]}"
    monkeypatch.setattr(d.hv, "api_node_addr", lambda node: addr)
    monkeypatch.setattr(d.hv, "_governance_state", lambda es: {})
    monkeypatch.setattr(d.hv.merkle, "read_all_entries", lambda p: [])
    monkeypatch.setattr(sync_common, "sign_sync_request", lambda *a, **k: {"Hive-Auth-Sig": "sig"})
    monkeypatch.setattr(d, "_nonce_cache", {})
    verdict = {"ok": False, "asked": []}

    def verified(a, device, gov):
        verdict["asked"].append((a, device))
        return verdict["ok"]
    monkeypatch.setattr(d.hv, "_telemetry_target_verified", verified)

    def call(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{daemon.server_address[1]}{path}?node={DEV}", timeout=10) as r:
            return json.loads(r.read())
    yield call, seen, verdict, addr, monkeypatch
    for s in (daemon, peer, elsewhere):
        s.shutdown()


@pytest.mark.parametrize("path", ["/api/overview", "/api/search", "/api/status", "/api/telemetry"])
def test_enforce_sends_no_signed_proxy_read_to_an_unverified_address(rig, path):
    call, seen, verdict, addr, mp = rig
    mp.setenv("HIVE_SYNC_AUTH_OUTBOUND", "enforce")
    out = call(path)
    assert seen["peer"] == [] and out["reachable"] is False
    assert verdict["asked"] == [(addr, DEV)]


def test_enforce_signs_the_proxy_read_to_a_verified_address_and_follows_no_redirect(rig):
    call, seen, verdict, addr, mp = rig
    mp.setenv("HIVE_SYNC_AUTH_OUTBOUND", "enforce")
    verdict["ok"] = True
    assert call("/api/overview")["available"] is True
    assert len(seen["peer"]) == 1 and seen["peer"][0].get("Hive-Auth-Sig") == "sig"
    out = call("/api/status")                       # the verified peer answers with a redirect
    assert seen["elsewhere"] == [] and out["reachable"] is False


def test_non_enforce_modes_still_sign_without_the_gate(rig):
    call, seen, verdict, addr, mp = rig
    mp.setenv("HIVE_SYNC_AUTH_OUTBOUND", "permissive")
    assert call("/api/overview")["available"] is True
    assert seen["peer"][0].get("Hive-Auth-Sig") == "sig" and verdict["asked"] == []


def test_the_gate_fails_closed_and_names_the_device(monkeypatch):
    hv = d.hv
    monkeypatch.setattr(hv, "_probe_headers", lambda p: {"Hive-Auth-Nonce": "n"})
    monkeypatch.setattr(hv, "_hive_info_fetch", lambda *a: {})
    ans = {"outcome": "verified", "device": DEV}
    monkeypatch.setattr(sync_common, "verify_hello", lambda *a, **k: dict(ans))
    assert hv._telemetry_target_verified("127.0.0.1:9", DEV, {}) is True
    assert hv._telemetry_target_verified("127.0.0.1:9", "k1:" + "b" * 16, {}) is False   # a different admitted device

    def boom(*a):
        raise OSError("down")
    monkeypatch.setattr(hv, "_hive_info_fetch", boom)
    assert hv._telemetry_target_verified("127.0.0.1:9", DEV, {}) is False                  # a failed probe is not verified
