"""#107: responder-signed /sync/hello and /hive/info (contract 1.26, sync protocol 3).

A caller that signs its request sends a Hive-Auth-Nonce; the responder signs its answer over that nonce,
the path and a digest of the whole body (`hello_sig`, domain `hive-hello-v1`). The caller checks it with
sync_common.verify_hello (verified / addr-unproven / unadmitted / purged / unsigned / invalid) and, under
`hv sync auth --outbound enforce`, pushes only to a verified peer. `hv doctor` uses the same proof to find
a peer that never contacts this node (peer-address) and reports the fleet's state (peer-identity).

In-process, modelled on tests/test_peer_address.py and tests/test_sync_request_signing.py: the daemon's real
Handler runs on 127.0.0.1 ThreadingHTTPServers, stand-in daemons serve crafted hellos and count what the
client pulls and pushes, every HIVE_HOME is a temp dir, and doctor's probe goes through a stubbed fetcher
that routes a fake tailnet host to a local server. One wire test runs two real daemons bound to this host's
LAN address (skipped without one, and on macOS, like tests/test_sync_auth.py).
"""
import base64
import collections
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from test_peer_address import _daemon, _doctor_fix, _free_port, _headers, _run, _write_peers  # noqa: E402

os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-hellosig-")   # always, even if exported: never the live hive

import ed25519  # noqa: E402
import merkle  # noqa: E402
import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402
import sync_client as sc  # noqa: E402

URL = "http://100.64.0.2:9876"
ADDR = "100.64.0.2:9876"
HINT, REAL, OTHER = "100.64.0.9", "100.64.0.20", "100.64.0.7"
REMOTE = "k1:" + "e" * 16          # a device a stand-in holds entries for and the client does not: a pull
OUTCOMES = ("verified", "addr-unproven", "unadmitted", "purged", "unsigned", "invalid")
POLICY = {                         # the adopted policy table: mode -> outcome -> what the round does
    "off": {o: "push" for o in OUTCOMES},
    "permissive": {o: "push" for o in OUTCOMES},
    "enforce": {"verified": "push", "addr-unproven": "pull", "unsigned": "pull", "unadmitted": "pull",
                "purged": "skip", "invalid": "skip"},
}


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────

def _did(seed):
    return "k1:" + hashlib.sha256(ed25519.pub_from_seed(seed)).hexdigest()[:16]


def _nonce():
    return os.urandom(16).hex()


def _body(dev, addr=ADDR, hive="h1:aaaa", **extra):
    return {"node_id": dev, "hive_id": hive, "protocol_version": 3, "contract": "1.26", "advertised_addr": addr,
            "journal_summary": {"total": 3, "by_node": {dev: 3}}, "chunks": {dev: ["sha256:" + "a" * 64]}, **extra}


def _signed(seed, body, nonce, path="/sync/hello"):
    body = dict(body)
    body["hello_sig"] = sync_common.sign_hello(body, nonce, path, seed, body["node_id"])
    assert body["hello_sig"]
    return json.loads(json.dumps(body))            # what a caller parses off the wire


def _gov(admitted=(), purged=(), owner="o1:owner", hive="h1:aaaa"):
    return {"owner_id": owner, "hive_id": hive, "admitted": set(admitted), "purged": set(purged)}


def _keyed(home, seed=None):
    """A HIVE_HOME holding a device key (what `hv key init` leaves, minus the CLI round trip)."""
    seed = seed or os.urandom(32)
    home.mkdir(parents=True, exist_ok=True)
    (home / ".device-key").write_text(base64.b64encode(seed).decode() + "\n")
    os.chmod(home / ".device-key", 0o600)
    return seed, _did(seed)


def _hvmod(home, monkeypatch):
    """A fresh hv module bound to `home` (hv reads HIVE_HOME at import). HIVE_HOME stays set to it, since
    sync_common.load_peers reads the environment at call time: load the hive doctor runs for LAST."""
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader(f"hvmod_hello_{os.urandom(4).hex()}", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _serve_as(hvm, monkeypatch, addr=None):
    """The daemon's Handler answers as `hvm`'s hive, advertising `addr`."""
    monkeypatch.setattr(d, "hv", hvm)
    monkeypatch.setattr(d, "_peer_seen", {})
    monkeypatch.setitem(d._ADVERTISED, "addr", addr)


def _client_as(hvm, monkeypatch):
    """sync_client runs as `hvm`'s hive, and signs its requests with that hive's key."""
    monkeypatch.setattr(sc, "hv", hvm)
    monkeypatch.setattr(sync_common, "_hv", hvm)
    monkeypatch.setattr(sc, "_sightings", {})


def _req(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def _root(hvm):
    return merkle.merkle_root(merkle.chunk_hashes(merkle.read_all_entries(hvm.JOURNAL_DIR)))


def _facts(hvm):
    return sorted(e["payload"]["content"] for e in merkle.read_all_entries(hvm.JOURNAL_DIR) if e["type"] == "fact")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("HIVE_SYNC_AUTH", "HIVE_SYNC_AUTH_OUTBOUND", "HIVE_NODE_ID"):
        monkeypatch.delenv(var, raising=False)


# ── the signed statement (pure) ───────────────────────────────────────────────────────────────────────

def test_signing_bytes_are_stable_and_every_line_counts():
    args = ("/sync/hello", "n" * 32, "k1:x", "h1:y", ADDR, 3, "sha256:z")
    a = sync_common.hello_signing_bytes(*args)
    assert a == sync_common.hello_signing_bytes(*args)
    assert a.split(b"\n")[0] == b"hive-hello-v1"
    for i in range(len(args)):
        changed = list(args)
        changed[i] = "/hive/info" if i == 0 else (4 if i == 5 else str(changed[i]) + "!")
        assert sync_common.hello_signing_bytes(*changed) != a, f"line {i} is not bound"


def test_a_request_signature_never_verifies_as_a_hello_and_back():
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    body = _body(dev)
    # a hive-sig-v1 request signature, over the same nonce, presented as a hello
    req_sig = ed25519.sign(sync_common.sync_signing_bytes("GET", "/sync/hello", "", b"", 1, n), seed)
    forged = dict(body, hello_sig={"alg": "hive-sig-v1", "device": dev, "pub": base64.b64encode(ed25519.pub_from_seed(seed)).decode(),
                                   "sig": base64.b64encode(req_sig).decode()})
    assert sync_common.verify_hello(forged, n, "/sync/hello", URL, _gov(owner=None))["reason"] == "bad-alg"
    forged["hello_sig"]["alg"] = "hive-hello-v1"
    assert sync_common.verify_hello(forged, n, "/sync/hello", URL, _gov(owner=None))["reason"] == "bad-signature"
    # and a hello signature never passes as a request envelope
    good = _signed(seed, body, n)
    hdrs = _headers(seed, "GET", "/sync/chunk")
    hdrs["Hive-Auth-Sig"] = good["hello_sig"]["sig"]
    ok, reason, _ = d._verify_sync_request(hdrs, "GET", "/sync/chunk", "", b"", _gov(owner=None))
    assert not ok and reason == "bad-signature"


@pytest.mark.parametrize("path", ["/sync/hello", "/hive/info"])
def test_a_round_trip_verifies(path):
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    v = sync_common.verify_hello(_signed(seed, _body(dev), n, path), n, path, URL, _gov([dev]), expected=dev)
    assert v == {"outcome": "verified", "reason": "ok", "device": dev, "other_device": False}


@pytest.mark.parametrize("tamper", [
    lambda b: b.update(hive_id="h1:other"),
    lambda b: b.update(advertised_addr="100.64.0.2:9877"),
    lambda b: b.update(protocol_version=2),
    lambda b: b.update(contract="1.25"),
    lambda b: b["chunks"][b["node_id"]].append("sha256:" + "b" * 64),
    lambda b: b["journal_summary"].update(total=4),
    lambda b: b.update(label="impostor"),
    lambda b: b.pop("contract"),
    lambda b: b.update(node_id="k1:" + "0" * 16),
])
def test_tampering_with_any_field_fails(tamper):
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    resp = _signed(seed, _body(dev), n)
    tamper(resp)
    v = sync_common.verify_hello(resp, n, "/sync/hello", URL, _gov(owner=None, hive=""))
    assert v["outcome"] == "invalid" and v["reason"] in ("bad-signature", "node-id-mismatch")


def test_a_signature_for_one_path_does_not_stand_in_for_the_other():
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    resp = _signed(seed, _body(dev), n, "/hive/info")
    assert sync_common.verify_hello(resp, n, "/sync/hello", URL, _gov([dev]))["outcome"] == "invalid"


def test_a_replayed_hello_is_invalid():
    seed = os.urandom(32)
    dev, n1, n2 = _did(seed), _nonce(), _nonce()
    captured = _signed(seed, _body(dev), n1)
    v = sync_common.verify_hello(captured, n2, "/sync/hello", URL, _gov([dev]))
    assert v["outcome"] == "invalid" and v["reason"] == "bad-signature"


def test_a_foreign_key_claiming_the_node_id_is_invalid():
    seed, foreign = os.urandom(32), os.urandom(32)
    dev, n = _did(seed), _nonce()
    body = _body(dev)
    msg = sync_common.hello_signing_bytes("/sync/hello", n, dev, body["hive_id"], ADDR, 3, sync_common.hello_body_digest(body))
    fpub = base64.b64encode(ed25519.pub_from_seed(foreign)).decode()
    fsig = base64.b64encode(ed25519.sign(msg, foreign)).decode()
    claims_victim = dict(body, hello_sig={"alg": "hive-hello-v1", "device": dev, "pub": fpub, "sig": fsig})
    v = sync_common.verify_hello(claims_victim, n, "/sync/hello", URL, _gov([dev]), expected=dev)
    assert (v["outcome"], v["reason"]) == ("invalid", "pub-fingerprint-mismatch")
    own_id = dict(body, hello_sig={"alg": "hive-hello-v1", "device": _did(foreign), "pub": fpub, "sig": fsig})
    v = sync_common.verify_hello(own_id, n, "/sync/hello", URL, _gov([dev, _did(foreign)]), expected=dev)
    assert (v["outcome"], v["reason"]) == ("invalid", "node-id-mismatch")


def test_membership_outcomes_and_the_pre_owner_case():
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    resp = _signed(seed, _body(dev), n)
    check = lambda gov: sync_common.verify_hello(resp, n, "/sync/hello", URL, gov)["outcome"]  # noqa: E731
    assert check(_gov([dev])) == "verified"
    assert check(_gov([])) == "unadmitted"
    assert check(_gov([dev], purged=[dev])) == "purged"
    assert check(_gov([], purged=[dev])) == "purged"
    assert check(_gov([], owner=None, hive="")) == "verified"            # pre-owner: identity alone is enough
    assert check(_gov([dev], hive="")) == "verified"                     # no hive on one side
    assert check(_gov([dev], hive="h1:bbbb")) == "invalid"               # another hive
    assert check(_gov([dev], owner=None, hive="h1:bbbb")) == "invalid"


def test_the_signed_address_must_be_the_one_contacted():
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    gov = _gov([dev])
    for addr, contacted, outcome in [
        (ADDR, URL, "verified"),
        (ADDR, URL + "/", "verified"),
        (None, URL, "addr-unproven"),                                   # a loopback-bound peer advertises none
        ("100.64.0.3:9876", URL, "addr-unproven"),                      # a relay: the real peer's address
        ("100.64.0.2:9877", URL, "addr-unproven"),                      # another port
        (ADDR, "http://node-b.tailnet.ts.net:9876", "addr-unproven"),   # a name is not the signed address
        ("fd7a:115c::5:9876", "http://[fd7a:115c::5]:9876", "verified"),
    ]:
        resp = _signed(seed, _body(dev, addr=addr), n)
        assert sync_common.verify_hello(resp, n, "/sync/hello", contacted, gov)["outcome"] == outcome, (addr, contacted)


def test_another_device_than_the_entry_names_is_flagged():
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    v = sync_common.verify_hello(_signed(seed, _body(dev), n), n, "/sync/hello", URL, _gov([dev]), expected="k1:" + "1" * 16)
    assert v["outcome"] == "verified" and v["other_device"] is True


def test_unsigned_and_malformed_answers():
    seed = os.urandom(32)
    dev, n = _did(seed), _nonce()
    gov = _gov([dev])
    assert sync_common.verify_hello(_body(dev), n, "/sync/hello", URL, gov)["outcome"] == "unsigned"
    good = _signed(seed, _body(dev), n)
    assert sync_common.verify_hello(good, None, "/sync/hello", URL, gov)["reason"] == "no-nonce-sent"   # we had no key
    for bad in ({}, {"alg": "hive-hello-v1"}, "nope", {"alg": "hive-hello-v1", "device": dev, "pub": "!!", "sig": "!!"}):
        assert sync_common.verify_hello(dict(good, hello_sig=bad), n, "/sync/hello", URL, gov)["outcome"] == "invalid"


def test_the_responder_signs_only_for_a_well_formed_nonce_and_a_device_identity():
    seed = os.urandom(32)
    dev = _did(seed)
    sign = lambda nonce, s=seed, node=dev: sync_common.sign_hello(_body(dev), nonce, "/sync/hello", s, node)  # noqa: E731
    assert sign(_nonce()) and sign("A" * 32) and sign("a" * 64)
    for nonce in (None, "", "a" * 31, "a" * 65, "g" * 32, "fixed-nonce-" + "a" * 20, 123):
        assert sign(nonce) is None, nonce
    assert sign(_nonce(), s=None) is None                               # no device key
    assert sign(_nonce(), node="legacy-hostname") is None               # a hostname identity
    assert sign(_nonce(), node="k1:" + "0" * 16) is None                # HIVE_NODE_ID naming another device


@pytest.mark.parametrize("mode", sorted(POLICY))
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_the_outbound_policy_table(mode, outcome):
    assert sync_common.outbound_action(mode, outcome) == POLICY[mode][outcome]


def test_the_outbound_mode_setting_precedence(monkeypatch):
    assert sync_common.sync_auth_outbound_mode({}) == "permissive"
    assert sync_common.sync_auth_outbound_mode({"sync_auth_outbound": "enforce"}) == "enforce"
    assert sync_common.sync_auth_outbound_mode({"sync_auth_outbound": "bogus"}) == "permissive"
    assert sync_common.sync_auth_outbound_mode({"sync_auth": "enforce"}) == "permissive"      # separate from inbound
    monkeypatch.setenv("HIVE_SYNC_AUTH_OUTBOUND", "off")
    assert sync_common.sync_auth_outbound_mode({"sync_auth_outbound": "enforce"}) == "off"
    monkeypatch.setenv("HIVE_SYNC_AUTH_OUTBOUND", "bogus")
    assert sync_common.sync_auth_outbound_mode({"sync_auth_outbound": "enforce"}) == "enforce"
    monkeypatch.setenv("HIVE_SYNC_AUTH", "enforce")
    monkeypatch.delenv("HIVE_SYNC_AUTH_OUTBOUND")
    assert sync_common.sync_auth_outbound_mode({}) == "permissive"


# ── the real handler ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/sync/hello", "/hive/info"])
def test_the_handler_signs_for_a_nonce_even_when_the_callers_envelope_does_not_verify(tmp_path, monkeypatch, path):
    _seed, dev = _keyed(tmp_path / "b")
    stranger = os.urandom(32)
    hvb = _hvmod(tmp_path / "b", monkeypatch)
    with _daemon(OTHER) as port:
        _serve_as(hvb, monkeypatch, f"127.0.0.1:{port}")
        bad = _headers(stranger, "GET", path)
        bad["Hive-Auth-Sig"] = base64.b64encode(b"\x00" * 64).decode()      # permissive serves it, unverified
        for hdrs in (bad, {"Hive-Auth-Nonce": _nonce()}):
            status, resp = _req(port, path, hdrs)
            assert status == 200 and resp["protocol_version"] == 3 and resp["hello_sig"]["device"] == dev
            v = sync_common.verify_hello(resp, hdrs["Hive-Auth-Nonce"], path, f"http://127.0.0.1:{port}", _gov(owner=None, hive=""))
            assert v["outcome"] == "verified", v
        status, plain = _req(port, path)                                    # no nonce: exactly the old answer
        assert status == 200 and "hello_sig" not in plain and plain["node_id"] == dev


def test_a_legacy_identity_sends_no_signature_and_its_caller_sees_unsigned(tmp_path, monkeypatch):
    _keyed(tmp_path / "legacy")
    monkeypatch.setenv("HIVE_NODE_ID", "legacy-host")                       # a key, but not its identity
    legacy = _hvmod(tmp_path / "legacy", monkeypatch)
    monkeypatch.delenv("HIVE_NODE_ID")
    keyless = _hvmod(tmp_path / "keyless", monkeypatch)                     # a hostname node, no key at all
    for hvm in (legacy, keyless):
        with _daemon(OTHER) as port:
            _serve_as(hvm, monkeypatch, f"127.0.0.1:{port}")
            n = _nonce()
            status, resp = _req(port, "/hive/info", {"Hive-Auth-Nonce": n})
            assert status == 200 and "hello_sig" not in resp
            assert sync_common.verify_hello(resp, n, "/hive/info", f"http://127.0.0.1:{port}", _gov(owner=None))["outcome"] == "unsigned"


def test_a_flood_of_nonced_info_requests_stays_within_the_slot_cap(tmp_path, monkeypatch):
    """The responder signs inside the request slot _enter takes, so N concurrent nonced /hive/info requests
    sign at most MAX_CONCURRENT_REQUESTS at a time (the rest get 503), and the daemon still answers after."""
    _keyed(tmp_path / "b")
    hvb = _hvmod(tmp_path / "b", monkeypatch)
    cap, n = 4, 32
    monkeypatch.setattr(d, "_request_slots", threading.BoundedSemaphore(cap))
    monkeypatch.setattr(d, "_rate_limiter", d._RateLimiter(10_000, 10_000))   # isolate the slot cap
    real, lock, live = sync_common.sign_hello, threading.Lock(), {"now": 0, "peak": 0, "calls": 0}

    def slow_sign(*a, **k):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            live["calls"] += 1
        try:
            time.sleep(0.3)
            return real(*a, **k)
        finally:
            with lock:
                live["now"] -= 1
    monkeypatch.setattr(sync_common, "sign_hello", slow_sign)

    with _daemon(OTHER) as port:
        _serve_as(hvb, monkeypatch, f"127.0.0.1:{port}")
        barrier, codes = threading.Barrier(n), []

        def hit():
            barrier.wait()
            codes.append(_req(port, "/hive/info", {"Hive-Auth-Nonce": _nonce()})[0])
        threads = [threading.Thread(target=hit) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not any(t.is_alive() for t in threads), "the flood wedged the daemon"
        assert len(codes) == n and set(codes) <= {200, 503}
        assert 1 <= live["peak"] <= cap and live["calls"] == codes.count(200)
        assert codes.count(503) > 0

        for _ in range(cap):                                                # every slot came back
            assert d._request_slots.acquire(blocking=False)
        for _ in range(cap):
            d._request_slots.release()
        nonce = _nonce()
        status, resp = _req(port, "/hive/info", {"Hive-Auth-Nonce": nonce})
        assert status == 200
        v = sync_common.verify_hello(resp, nonce, "/hive/info", f"http://127.0.0.1:{port}", _gov(owner=None, hive=""))
        assert v["outcome"] == "verified"


# ── the client against stand-in daemons: the policy table on the wire ────────────────────────────────

@contextlib.contextmanager
def _stand_in(make):
    """A stand-in peer on 127.0.0.1: a root that differs from ours, make(nonce, port, path) as its /sync/hello
    and /hive/info, one differing window of entries it does not actually serve, and counters: `pull` per
    /sync/chunk GET, `push` per /sync/ingest POST (it accepts nothing)."""
    counts = collections.Counter()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/sync/merkle-root":
                self._json(200, {"root_hash": "sha256:" + "0" * 64})
            elif path in ("/sync/hello", "/hive/info"):
                self._json(200, make(self.headers.get("Hive-Auth-Nonce"), self.server.server_address[1], path))
            elif path == "/sync/chunk":
                counts["pull"] += 1
                self._json(200, {"entries": [], "hash": ""})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            counts["push"] += 1
            self._json(200, {"accepted": 0, "duplicates": 0})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], counts
    finally:
        srv.shutdown()
        srv.server_close()


PEER, STRANGER, PURGED, FOREIGN = (os.urandom(32) for _ in range(4))


def _stand_in_body(seed, port, addr=None, protocol=3):
    return {"node_id": _did(seed), "hive_id": "", "protocol_version": protocol, "contract": "1.26",
            "advertised_addr": addr or f"127.0.0.1:{port}", "journal_summary": {"total": 1, "by_node": {REMOTE: 1}},
            "chunks": {REMOTE: ["sha256:" + "f" * 64]}}


def _sign_into(body, seed, nonce, path):
    sig = sync_common.sign_hello(body, nonce, path, seed, _did(seed))
    if sig:
        body["hello_sig"] = sig
    return body


def _forged(nonce, port, path):
    body = _sign_into(_stand_in_body(PEER, port), PEER, nonce, path)
    body["hello_sig"]["sig"] = base64.b64encode(b"\x01" * 64).decode()
    return body


def _foreign(nonce, port, path):
    body = _stand_in_body(PEER, port)
    msg = sync_common.hello_signing_bytes(path, nonce, body["node_id"], "", body["advertised_addr"], 3,
                                          sync_common.hello_body_digest(body))
    body["hello_sig"] = {"alg": "hive-hello-v1", "device": _did(PEER),
                         "pub": base64.b64encode(ed25519.pub_from_seed(FOREIGN)).decode(),
                         "sig": base64.b64encode(ed25519.sign(msg, FOREIGN)).decode()}
    return body


HELLOS = {   # stand-in -> (what it answers, the outcome the caller must reach)
    "verified": (lambda n, p, path: _sign_into(_stand_in_body(PEER, p), PEER, n, path), "verified"),
    "unsigned": (lambda n, p, path: _stand_in_body(PEER, p, protocol=2), "unsigned"),
    "forged": (_forged, "invalid"),
    "foreign": (_foreign, "invalid"),
    "replayed": (lambda n, p, path: _sign_into(_stand_in_body(PEER, p), PEER, "ab" * 16, path), "invalid"),
    "relayed": (lambda n, p, path: _sign_into(_stand_in_body(PEER, p, addr=f"{REAL}:{p}"), PEER, n, path), "addr-unproven"),
    "unadmitted": (lambda n, p, path: _sign_into(_stand_in_body(STRANGER, p), STRANGER, n, path), "unadmitted"),
    "purged": (lambda n, p, path: _sign_into(_stand_in_body(PURGED, p), PURGED, n, path), "purged"),
}


def _client(tmp_path, monkeypatch, gov, key=True):
    """A client hive holding one fact of its own (something to push), with `gov` as its governance."""
    home = tmp_path / "client"
    if key:
        _keyed(home)
    hvc = _hvmod(home, monkeypatch)
    hvc.init_db()
    hvc.append_journal("fact", {"content": "client fact", "tags": [], "importance": 0.5, "source": "manual"})
    monkeypatch.setattr(hvc, "_governance_state", lambda entries: dict(gov))
    _client_as(hvc, monkeypatch)
    seen = []
    monkeypatch.setattr(hvc, "_record_peer_candidate", lambda *a, **k: seen.append((a, k)))
    return hvc, seen


@pytest.mark.parametrize("mode", sorted(POLICY))
@pytest.mark.parametrize("kind", sorted(HELLOS))
def test_the_policy_table_on_the_wire(tmp_path, monkeypatch, capsys, mode, kind):
    make, outcome = HELLOS[kind]
    _hvc, seen = _client(tmp_path, monkeypatch, _gov([_did(PEER)], purged=[_did(PURGED)], hive=""))
    with _stand_in(make) as (port, counts):
        sc._sync_with_peer({"url": f"http://127.0.0.1:{port}", "id": _did(PEER)}, mode)
    out = capsys.readouterr().out
    action = POLICY[mode][outcome]
    assert (counts["pull"] > 0, counts["push"]) == {"push": (True, 1), "pull": (True, 0), "skip": (False, 0)}[action], out
    if mode == "off":
        assert "hello" not in out                                           # off does not look
    elif outcome != "verified":
        assert f"hello {outcome}" in out
        assert ("push withheld" in out) == (action == "pull") and ("skipped" in out) == (action == "skip")
    else:
        assert "hello" not in out
    recorded = mode != "off" and outcome == "verified"
    assert seen == ([((_did(PEER), "127.0.0.1"), {"via": "outbound"})] if recorded else [])


def test_pre_owner_identity_alone_is_enough_to_push_under_enforce(tmp_path, monkeypatch, capsys):
    _client(tmp_path, monkeypatch, _gov(owner=None, hive=""))
    make = lambda n, p, path: _sign_into(_stand_in_body(STRANGER, p), STRANGER, n, path)  # noqa: E731
    with _stand_in(make) as (port, counts):
        sc._sync_with_peer({"url": f"http://127.0.0.1:{port}"}, "enforce")
    assert counts["push"] == 1 and "hello" not in capsys.readouterr().out


def test_a_keyless_caller_sends_no_nonce_so_every_hello_is_unsigned(tmp_path, monkeypatch, capsys):
    _client(tmp_path, monkeypatch, _gov([_did(PEER)], hive=""), key=False)
    with _stand_in(HELLOS["verified"][0]) as (port, counts):
        sc._sync_with_peer({"url": f"http://127.0.0.1:{port}"}, "enforce")
    out = capsys.readouterr().out
    assert counts["push"] == 0 and counts["pull"] > 0 and "push withheld: hello unsigned (no-nonce-sent)" in out


def test_another_device_than_the_entrys_id_is_flagged_but_pushed_to(tmp_path, monkeypatch, capsys):
    _client(tmp_path, monkeypatch, _gov([_did(PEER), _did(STRANGER)], hive=""))
    make = lambda n, p, path: _sign_into(_stand_in_body(STRANGER, p), STRANGER, n, path)  # noqa: E731
    with _stand_in(make) as (port, counts):
        sc._sync_with_peer({"url": f"http://127.0.0.1:{port}", "id": _did(PEER)}, "enforce")
    out = capsys.readouterr().out
    assert counts["push"] == 1 and f"signed by {_did(STRANGER)}, not the device its .peers.json id names" in out


# ── real handlers end to end ──────────────────────────────────────────────────────────────────────────

def _pair(tmp_path, monkeypatch):
    """Two real hives: A the owner admitting A and B, one fact on each. Returns (hva, hvb, a_dev, b_dev)."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _run(a, "key", "init")
    _run(b, "key", "init")
    a_dev, b_dev = ((h / ".device-id").read_text().strip() for h in (a, b))
    _run(a, "owner", "init")
    _run(a, "group", "admit", a_dev, "--principal", "a")
    _run(a, "group", "admit", b_dev, "--principal", "b")
    _run(a, "remember", "alpha from A")
    _run(b, "remember", "beta from B")
    return _hvmod(a, monkeypatch), _hvmod(b, monkeypatch), a_dev, b_dev


def _round(client, server, server_dev, monkeypatch, capsys, mode, contact=None):
    """One sync round from `client` to `server`'s real Handler on 127.0.0.1, advertising its own address, or
    reached through `contact`, a context manager that yields another port in front of it. Returns the output."""
    capsys.readouterr()
    with _daemon(None) as port:
        _serve_as(server, monkeypatch, f"127.0.0.1:{port}")
        _client_as(client, monkeypatch)
        with (contact(port) if contact else contextlib.nullcontext(port)) as asked:
            sc._sync_with_peer({"url": f"http://127.0.0.1:{asked}", "id": server_dev}, mode)
    return capsys.readouterr().out


def test_two_real_handlers_converge_under_outbound_enforce(tmp_path, monkeypatch, capsys):
    hva, hvb, a_dev, b_dev = _pair(tmp_path, monkeypatch)
    line = _round(hva, hvb, b_dev, monkeypatch, capsys, "enforce")      # B is pre-owner: A proves B by its own governance
    assert "pulled 1 (dup 0), pushed" in line and "hello" not in line, line
    _run(tmp_path / "b", "remember", "gamma from B")
    line = _round(hvb, hva, a_dev, monkeypatch, capsys, "enforce")      # B now holds A's governance, and proves A by it
    assert "pushed 1" in line and "hello" not in line, line
    assert _root(hva) == _root(hvb)
    assert _facts(hva) == _facts(hvb) == ["alpha from A", "beta from B", "gamma from B"]


def test_a_v2_peer_is_only_pulled_from_under_enforce_and_converges_under_permissive(tmp_path, monkeypatch, capsys):
    hva, hvb, _a_dev, b_dev = _pair(tmp_path, monkeypatch)
    monkeypatch.setattr(sync_common, "sign_hello", lambda *a, **k: None)     # B answers like a protocol-2 peer
    line = _round(hva, hvb, b_dev, monkeypatch, capsys, "enforce")
    assert "push withheld: hello unsigned (no-hello-sig)" in line, line
    assert _facts(hva) == ["alpha from A", "beta from B"] and _facts(hvb) == ["beta from B"]
    line = _round(hva, hvb, b_dev, monkeypatch, capsys, "permissive")
    assert "pushed" in line and "hello unsigned (no-hello-sig) (outbound enforce would pull only)" in line, line
    assert _root(hva) == _root(hvb)


@contextlib.contextmanager
def _relay(to_port):
    """A squatter on 127.0.0.1 forwarding every request, its Hive-Auth-* headers and body, to 127.0.0.1:to_port
    and returning the answer: it can obtain the real peer's signature, but one over the peer's own address."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _fwd(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            hdrs = {k: v for k, v in self.headers.items() if k.lower().startswith("hive-auth-") or k.lower() == "content-type"}
            req = urllib.request.Request(f"http://127.0.0.1:{to_port}{self.path}", data=self.rfile.read(n) if n else None,
                                         headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    code, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                code, body = e.code, e.read()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._fwd("GET")

        def do_POST(self):
            self._fwd("POST")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_relayed_hello_is_addr_unproven_and_not_pushed_to_under_enforce(tmp_path, monkeypatch, capsys):
    hva, hvb, _a_dev, b_dev = _pair(tmp_path, monkeypatch)
    line = _round(hva, hvb, b_dev, monkeypatch, capsys, "enforce", contact=_relay)
    assert "push withheld: hello addr-unproven" in line, line
    assert _facts(hvb) == ["beta from B"]                                    # nothing reached B through the relay


# ── #7 end to end: doctor finds a peer that never contacts this node ─────────────────────────────────

def _router(routes, calls):
    """doctor's /hive/info fetcher, with each fake tailnet host in `routes` sent to a local port."""
    def fetch(url, headers, timeout):
        u = urlsplit(url)
        calls.append(u.hostname)
        if u.hostname not in routes:
            raise ConnectionError(f"no route to {u.hostname}")
        req = urllib.request.Request(f"http://127.0.0.1:{routes[u.hostname]}{u.path}", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return fetch


def _stale_b(tmp_path):
    """A (owner) admits B; A's only entry for B is a stale address nothing answers at. B never contacts A."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _run(b, "key", "init")
    b_dev = (b / ".device-id").read_text().strip()
    _run(a, "key", "init")
    _run(a, "owner", "init")
    _run(a, "group", "admit", b_dev, "--principal", "b")
    stale = f"http://127.0.0.1:{_free_port()}"
    cfg = {"self": "node-a", "port": _free_port(), "bind": "127.0.0.1",
           "peers": [{"id": b_dev, "label": "node-b", "url": stale}]}
    return a, b, b_dev, stale, cfg, _write_peers(a, cfg)


def test_doctor_repoints_a_peer_that_never_contacts_this_node(tmp_path, monkeypatch, capsys):
    a, b, b_dev, stale, cfg, peers = _stale_b(tmp_path)
    port = stale.rsplit(":", 1)[1]
    hvb = _hvmod(b, monkeypatch)
    hva = _hvmod(a, monkeypatch)
    calls = []
    with _daemon(None) as pb:
        _serve_as(hvb, monkeypatch, f"{HINT}:{port}")                        # B now lives at HINT, same port
        monkeypatch.setattr(hva, "_tailscale_hosts", lambda: [("node-b", [HINT])])
        monkeypatch.setattr(hva, "_hive_info_fetch", _router({HINT: pb}, calls))
        _doctor_fix(hva, monkeypatch, dry=True, stub_tailscale=False)
        out = capsys.readouterr().out
        assert f"proved itself at {HINT}" in out and "would repoint" in out
        assert json.loads(peers.read_text()) == cfg                          # the preview changes nothing
        _doctor_fix(hva, monkeypatch, stub_tailscale=False)
    out = capsys.readouterr().out
    assert f"repointed peer {b_dev[:11]}…" in out, out
    want = dict(cfg, peers=[dict(cfg["peers"][0], url=f"http://{HINT}:{port}")])
    assert json.loads(peers.read_text()) == want
    assert HINT in calls
    assert not hva.PEER_CANDIDATES_PATH.exists()                             # B never contacted A; doctor records nothing


@pytest.mark.parametrize("case", ["other-device", "unsigned", "relay", "purged"])
def test_doctor_does_not_move_a_peer_the_answer_does_not_prove(tmp_path, monkeypatch, capsys, case):
    a, b, b_dev, stale, cfg, peers = _stale_b(tmp_path)
    port = stale.rsplit(":", 1)[1]
    who = b
    if case == "other-device":                                               # C, also admitted, answers at the hint
        who = tmp_path / "c"
        who.mkdir()
        _run(who, "key", "init")
        _run(a, "group", "admit", (who / ".device-id").read_text().strip(), "--principal", "c")
    if case == "purged":
        _run(a, "group", "purge", b_dev)
    if case == "unsigned":
        monkeypatch.setattr(sync_common, "sign_hello", lambda *x, **k: None)
    peers = _write_peers(a, cfg)
    hvs = _hvmod(who, monkeypatch)
    hva = _hvmod(a, monkeypatch)
    calls = []
    with _daemon(None) as pb:
        # a relay squats the hint and forwards to B, which signs for the address it really has
        _serve_as(hvs, monkeypatch, f"{REAL if case == 'relay' else HINT}:{port}")
        with (_relay(pb) if case == "relay" else contextlib.nullcontext(pb)) as front:
            monkeypatch.setattr(hva, "_tailscale_hosts", lambda: [("node-b", [HINT])])
            monkeypatch.setattr(hva, "_hive_info_fetch", _router({HINT: front}, calls))
            _doctor_fix(hva, monkeypatch, stub_tailscale=False)
    out = capsys.readouterr().out
    assert HINT in calls, "the probe never ran"
    assert "repointed" not in out and "proved itself" not in out, out
    assert json.loads(peers.read_text()) == cfg


def test_an_answering_address_is_never_probed_or_rewritten(tmp_path, monkeypatch, capsys):
    """Q3: a stored address that answers is left alone, even when its hello is addr-unproven; that is only
    peer-identity's flag."""
    a, b, b_dev, _stale, cfg, _peers = _stale_b(tmp_path)
    hvb = _hvmod(b, monkeypatch)
    hva = _hvmod(a, monkeypatch)
    calls = []
    with _daemon(None) as pb:
        cfg = dict(cfg, peers=[dict(cfg["peers"][0], url=f"http://127.0.0.1:{pb}")])
        peers = _write_peers(a, cfg)
        _serve_as(hvb, monkeypatch, f"{HINT}:{pb}")                           # it answers, but signs another address
        monkeypatch.setattr(hva, "_tailscale_hosts", lambda: [("node-b", [HINT])])
        monkeypatch.setattr(hva, "_hive_info_fetch", _router({HINT: pb}, calls))
        _doctor_fix(hva, monkeypatch, stub_tailscale=False)
    out = capsys.readouterr().out
    assert calls == [] and "repointed" not in out
    assert json.loads(peers.read_text()) == cfg
    assert "peer-identity" in out and "addr-unproven" in out


def test_the_prover_is_asked_only_for_a_report_entry_with_hints_then_sightings():
    hv = d.hv
    dev = "k1:0123456789abcdef"
    peers = [
        {"id": dev, "label": "node-b", "url": "http://100.64.0.2:9876"},     # fails, device known: report
        {"id": "david", "url": "http://100.64.0.3:9876"},                    # fails, no device: unknown
        {"id": "k1:" + "2" * 16, "url": "http://100.64.0.4:9876"},           # answers
    ]
    answered = {"http://100.64.0.2:9876": False, "http://100.64.0.3:9876": False, "http://100.64.0.4:9876": True}
    cands = {dev: {"100.64.0.2": {"first_seen": 1, "last_seen": 900}, "100.64.0.30": {"first_seen": 1, "last_seen": 50},
                   "100.64.0.31": {"first_seen": 1, "last_seen": 80}}}
    asked = []
    prove = lambda url, device, hosts: asked.append((url, device, hosts)) or None  # noqa: E731
    c = hv._peer_address_check(peers, answered, cands, 100, lambda: [("node-b", [HINT])], prove)
    assert asked == [("http://100.64.0.2:9876", dev, [HINT, "100.64.0.2", "100.64.0.31", "100.64.0.30"])]
    assert c["moves"] == []
    asked.clear()
    moved = hv._peer_address_check(peers, answered, cands, 100, lambda: [], lambda url, device, hosts: "100.64.0.31")
    assert moved["moves"] == [{"index": 0, "device": dev, "url": "http://100.64.0.2:9876", "to": "http://100.64.0.31:9876"}]
    assert "proved itself at 100.64.0.31" in moved["detail"]


# ── peer-identity ─────────────────────────────────────────────────────────────────────────────────────

def _ident(outcome, pv=3, other=False, reason="ok", dev="k1:" + "3" * 16):
    return {"outcome": outcome, "reason": reason, "device": dev, "other_device": other, "protocol_version": pv}


def test_peer_identity_names_what_is_not_verified_and_gives_the_green_light():
    hv = d.hv
    u = [f"http://100.64.0.{i}:9876" for i in range(2, 7)]
    peers = [{"url": x} for x in u]
    ok = hv._peer_identity_check(peers[:2], {u[0]: _ident("verified"), u[1]: _ident("verified")}, "permissive")
    assert ok["status"] == "ok" and "2 peer(s) verified" in ok["detail"]
    assert "`hv sync auth enforce`: yes, for `hv sync auth --outbound enforce`: yes" in ok["detail"]

    v2 = {u[0]: _ident("verified"), u[1]: _ident("unsigned", pv=2, reason="no-hello-sig")}
    c = hv._peer_identity_check(peers[:2], v2, "permissive")
    assert c["status"] == "ok" and "1 before protocol 3, unsigned: 100.64.0.3:9876 (protocol 2)" in c["detail"]
    assert "`hv sync auth enforce`: yes, for `hv sync auth --outbound enforce`: no" in c["detail"]
    assert hv._peer_identity_check(peers[:2], v2, "enforce")["status"] == "warn"   # enforce is holding its push

    odd = {u[0]: _ident("invalid", reason="bad-signature"), u[1]: _ident("verified", other=True),
           u[2]: _ident("addr-unproven", reason="advertises 100.64.0.9:9876"), u[3]: _ident("unsigned", pv=1)}
    c = hv._peer_identity_check(peers, odd, "permissive")
    assert c["status"] == "warn"
    for bit in ("100.64.0.2:9876 invalid (bad-signature)", "100.64.0.3:9876 answered as k1:33333333…",
                "100.64.0.4:9876 addr-unproven (advertises 100.64.0.9:9876)", "(protocol 1)", "1 not answering",
                "`hv sync auth enforce`: no, for `hv sync auth --outbound enforce`: no"):
        assert bit in c["detail"], bit


def test_doctor_peer_identity_signs_its_probe_and_reads_each_peer(tmp_path, monkeypatch):
    a, b, b_dev, _stale, cfg, _peers = _stale_b(tmp_path)
    hvb = _hvmod(b, monkeypatch)
    hva = _hvmod(a, monkeypatch)
    monkeypatch.setattr(hva, "_orphan_daemons", lambda: ([], False))
    monkeypatch.setattr(hva, "_tailscale_hosts", lambda: [])
    with _daemon(None) as pb, _stand_in(HELLOS["unsigned"][0]) as (pv2, _counts):
        _serve_as(hvb, monkeypatch, f"127.0.0.1:{pb}")
        _write_peers(a, dict(cfg, peers=[{"id": b_dev, "url": f"http://127.0.0.1:{pb}"},
                                         {"id": "old-node", "url": f"http://127.0.0.1:{pv2}"}]))
        checks = {c["name"]: c for c in hva._doctor_status()}
    pi = checks["peer-identity"]
    assert pi["status"] == "ok", pi
    assert "outbound permissive; 1 peer(s) verified" in pi["detail"]
    assert f"1 before protocol 3, unsigned: 127.0.0.1:{pv2} (protocol 2)" in pi["detail"]
    assert "for `hv sync auth --outbound enforce`: no" in pi["detail"]


# ── the CLI and the wire: two real daemons, inbound and outbound enforce ─────────────────────────────

def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = None
    finally:
        s.close()
    return None if not ip or ip.startswith("127.") else ip


def test_the_outbound_flag_sets_its_own_key(tmp_path):
    _run(tmp_path, "sync", "auth", "--outbound", "enforce")
    cfg = json.loads((tmp_path / ".peers.json").read_text())
    assert cfg == {"sync_auth_outbound": "enforce"}                          # inbound untouched
    out = _run(tmp_path, "sync", "auth").stdout
    assert "sync read-auth mode: permissive" in out and "sync outbound-auth mode: enforce" in out
    _run(tmp_path, "sync", "auth", "off", "--outbound", "permissive")
    assert json.loads((tmp_path / ".peers.json").read_text()) == {"sync_auth_outbound": "permissive", "sync_auth": "off"}


@pytest.mark.skipif(sys.platform == "darwin", reason="a non-loopback `hv sync daemon` does not serve reliably on "
                    "GitHub macOS runners (see tests/test_sync_auth.py)")
def test_two_daemons_on_the_lan_converge_under_inbound_and_outbound_enforce(tmp_path):
    lan = _lan_ip()
    if not lan:
        pytest.skip("no non-loopback address on this host")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for h in (a, b):
        _run(h, "key", "init")
    a_dev, b_dev = ((h / ".device-id").read_text().strip() for h in (a, b))
    _run(a, "owner", "init")
    _run(a, "group", "admit", a_dev, "--principal", "a")
    _run(a, "group", "admit", b_dev, "--principal", "b")
    _run(a, "remember", "alpha from A")
    _run(b, "remember", "beta from B")
    pa, pb = _free_port(), _free_port()
    for home, port, peer_dev, peer_port in ((a, pa, b_dev, pb), (b, pb, a_dev, pa)):
        (home / ".peers.json").write_text(json.dumps({"self": home.name, "bind": lan, "port": port, "sync_auth": "enforce",
                                                      "peers": [{"id": peer_dev, "url": f"http://{lan}:{peer_port}"}]}))
        _run(home, "sync", "auth", "--outbound", "enforce")
    procs = []
    try:
        for home, port in ((a, pa), (b, pb)):
            procs.append(subprocess.Popen([sys.executable, "-c", "import hive_sync_daemon as d; d.serve_forever()"],
                                          cwd=str(PROJECT), env=dict(os.environ, HIVE_HOME=str(home)),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            deadline = time.time() + 45
            while True:
                try:
                    urllib.request.urlopen(f"http://{lan}:{port}/sync/merkle-root", timeout=2)
                    break
                except Exception:
                    assert time.time() < deadline, "daemon did not start"
                    time.sleep(0.3)
        out = _run(a, "sync", "now").stdout
        assert "pushed" in out and "hello" not in out, out
        rec = json.loads((a / ".peer_candidates.json").read_text())["devices"][b_dev][lan]
        assert rec["via"] == "outbound"                                      # A's verified outbound sighting of B
        out = _run(b, "sync", "now").stdout
        assert "hello" not in out, out
        _run(a, "sync", "now")
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    roots = [_run(h, "merkle").stdout.split("Root:")[1].split()[0] for h in (a, b)]
    assert roots[0] == roots[1]
