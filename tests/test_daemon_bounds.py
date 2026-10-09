"""The bounds on what an unauthenticated or unadmitted peer can make the sync daemon spend.

  * a request's headers and body each get a wall-clock deadline that a trickle of bytes does not renew;
  * one source address holds at most MAX_CONNECTIONS_PER_IP connections;
  * an unadmitted device's content entry is refused without an Ed25519 verification;
  * a Hive-Auth-Nonce is at most 64 characters;
  * an authority-less `join-request` / `announce` is size- and shape-capped, and its fields never reach .peers.json
    unbounded;
  * /sync/merkle-root re-hashes the journal only when a journal file changed.

In-process only: the daemon's Handler on a 127.0.0.1 server, every HIVE_HOME a temp dir.
"""
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-bounds-")   # always, even if exported: never the live hive

import ed25519  # noqa: E402
import merkle  # noqa: E402
import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402
import _planes  # noqa: E402  (which plane runs a command, 2.0)


def _load_hv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader(f"hvmod_bounds_{os.urandom(4).hex()}", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    _planes.install_control_plane(m)
    monkeypatch.setattr(d, "hv", m)
    monkeypatch.setattr(d, "_root_cache", {"sig": None, "root": None}, raising=False)
    monkeypatch.setattr(d, "_nonce_cache", {})
    monkeypatch.setattr(d.Handler, "_pinned", lambda self, u: True)
    return m


def _run(home, *args):
    r = subprocess.run([sys.executable, str(_planes.entry_for(args)), *args],
                       env=dict(os.environ, HIVE_HOME=str(home), HIVE_OWNER_PASSPHRASE="testpass",
                                HIVE_IDENTITY_STASH=str(Path(home) / "stash")),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


class _Daemon:
    def __init__(self, cls):
        self.srv = cls(("127.0.0.1", 0), d.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return s

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    hvm.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    made = []

    def make(cls=ThreadingHTTPServer):
        made.append(_Daemon(cls))
        return made[-1]
    yield make
    for x in made:
        x.close()


def _dripped_until_closed(sock, first, piece, interval, limit):
    """Send `first`, then `piece` every `interval` seconds. Returns the seconds until the server answered or
    closed (it has something for us to read), or None if it was still waiting after `limit`."""
    t0 = time.monotonic()
    sock.sendall(first)
    while time.monotonic() - t0 < limit:
        if select.select([sock], [], [], interval)[0]:
            return time.monotonic() - t0
        try:
            sock.sendall(piece)
        except OSError:
            return time.monotonic() - t0
    return None


def test_a_header_that_trickles_in_is_cut_at_the_deadline(daemon, monkeypatch):
    monkeypatch.setattr(d, "HEADER_DEADLINE", 1.0, raising=False)
    srv = daemon()
    s = srv.connect()
    took = _dripped_until_closed(s, b"GET /sync/merkle-root HTTP/1.0\r\n", b"X-A: b\r\n", 0.3, 4)
    s.close()
    assert took is not None and took < 3, "a byte every 0.3 s kept the header phase open past its deadline"


def test_a_body_that_trickles_in_is_cut_at_the_deadline(daemon, monkeypatch):
    monkeypatch.setattr(d, "BODY_DEADLINE", 1.0, raising=False)
    srv = daemon()
    s = srv.connect()
    head = (f"POST /sync/ingest HTTP/1.0\r\nHost: 127.0.0.1:{srv.port}\r\nContent-Length: 1000\r\n\r\n").encode()
    took = _dripped_until_closed(s, head, b"x", 0.3, 4)
    s.close()
    assert took is not None and took < 3, "a byte every 0.3 s held the handler past the body deadline"


def test_a_prompt_request_is_served_under_the_deadlines(daemon):
    srv = daemon()
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/sync/merkle-root", timeout=5) as r:
        assert r.status == 200 and "root_hash" in json.loads(r.read())


def test_connections_over_the_per_address_cap_are_refused(daemon, monkeypatch):
    monkeypatch.setattr(d, "MAX_CONNECTIONS_PER_IP", 3, raising=False)
    srv = daemon(getattr(d, "_BoundedServer", ThreadingHTTPServer))
    held = [srv.connect() for _ in range(3)]
    time.sleep(0.3)                                    # let the server accept and count them
    extra = srv.connect()
    extra.settimeout(2)
    try:
        got = extra.recv(100)                          # refused: an immediate 503 or a close; never a wait
    except socket.timeout:
        got = None
    assert got is not None, "a fourth connection from one address was held open instead of refused"
    assert all(not select.select([h], [], [], 0)[0] for h in held)       # the first three are untouched
    for h in held[:1]:
        h.close()
    deadline, line = time.monotonic() + 3, b""
    while not line.startswith(b"HTTP/1.0 200") and time.monotonic() < deadline:     # a freed slot is usable again
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", srv.port), timeout=3) as c:
            c.sendall(f"GET /sync/merkle-root HTTP/1.0\r\nHost: 127.0.0.1:{srv.port}\r\n\r\n".encode())
            line = c.recv(100)
    assert line.startswith(b"HTTP/1.0 200")
    for h in held[1:] + [extra]:
        h.close()


# ── a nonce is at most 64 characters ─────────────────────────────────────────────────────────────────

def _signed_headers(seed, nonce):
    pub = ed25519.pub_from_seed(seed)
    ts = int(time.time())
    sig = ed25519.sign(sync_common.sync_signing_bytes("GET", "/sync/hello", "", b"", ts, nonce), seed)
    return {"Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG,
            "Hive-Auth-Device": "k1:" + hashlib.sha256(pub).hexdigest()[:16],
            "Hive-Auth-Pub": base64.b64encode(pub).decode(), "Hive-Auth-Ts": str(ts),
            "Hive-Auth-Nonce": nonce, "Hive-Auth-Sig": base64.b64encode(sig).decode()}


def test_a_65_byte_nonce_is_refused_and_never_cached(tmp_path, monkeypatch):
    _load_hv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    ok, why, _ = d._verify_sync_request(_signed_headers(seed, "a" * 64), "GET", "/sync/hello", "", b"", {})
    assert ok, why
    cached = len(d._nonce_cache)
    ok, why, _ = d._verify_sync_request(_signed_headers(seed, "b" * 65), "GET", "/sync/hello", "", b"", {})
    assert not ok and why == "malformed-auth"
    ok, _, _ = d._verify_sync_request(_signed_headers(seed, "c" * 60_000), "GET", "/sync/hello", "", b"", {})
    assert not ok and len(d._nonce_cache) == cached


# ── the Merkle root is cached by the journal's mtime ─────────────────────────────────────────────────

def test_a_second_merkle_root_request_with_an_unchanged_journal_does_not_rehash(daemon, tmp_path, monkeypatch):
    hvm = d.hv
    hvm.init_db()
    _run(tmp_path, "remember", "a fact for the journal")
    srv = daemon()
    calls = []
    real = merkle.chunk_hashes
    monkeypatch.setattr(merkle, "chunk_hashes", lambda es, *a, **k: calls.append(1) or real(es, *a, **k))

    def root():
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/sync/merkle-root", timeout=5) as r:
            return json.loads(r.read())["root_hash"]
    first = root()
    n = len(calls)
    assert n >= 1 and root() == first and len(calls) == n          # unchanged: served from the cache
    _run(tmp_path, "remember", "another fact")                      # a journal file changed: re-hashed
    assert root() != first and len(calls) > n


# ── content from an unadmitted device costs no signature check ───────────────────────────────────────

def _foreign_device(hvm):
    seed = os.urandom(32)
    pub = hvm._ed25519.pub_from_seed(seed)
    return seed, pub, hvm._device_id_for_pub(pub)


def _signed(hvm, dev, seq, etype, payload):
    seed, pub, nid = dev
    e = {"node_id": nid, "seq": seq, "type": etype, "timestamp": f"2026-05-{seq:02d}T00:00:00Z",
         "payload": payload, "prev_hash": "sha256:genesis"}
    return hvm._sign_entry(e, seed, pub)


def test_an_unadmitted_devices_content_is_refused_without_a_signature_check(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hvm = _load_hv(tmp_path, monkeypatch)
    dev = _foreign_device(hvm)
    facts = [_signed(hvm, dev, i, "fact", {"content": f"claim {i}", "source": "agent"}) for i in range(1, 6)]
    seen = []
    real = hvm._verify_entry
    monkeypatch.setattr(hvm, "_verify_entry", lambda e: seen.append(e.get("seq")) or real(e))
    accepted, _ = hvm.append_foreign_entries(facts)
    assert accepted == 0
    assert not [q for q in seen if q in range(1, 6)], f"{len(seen)} signature checks for an unadmitted device"
    _run(tmp_path, "admit", dev[2], "--principal", "joiner")        # admitted: now verified, and it lands
    seen.clear()
    accepted, _ = hvm.append_foreign_entries(facts)
    assert accepted == 5 and sorted(seen) == [1, 2, 3, 4, 5]


# ── authority-less governance is bounded before it can reach .peers.json ─────────────────────────────

def _peers_json(home):
    p = Path(home) / ".peers.json"
    return p.read_text() if p.exists() else ""


@pytest.mark.parametrize("what,payload", [
    ("a label of 100 KB", {"label": "L" * 100_000}),
    ("a url of 100 KB", {"url": "http://" + "h" * 100_000}),
    ("a url that is not an address", {"url": "javascript:alert(1)"}),
    ("a label that is not text", {"label": ["a", "b"]}),
    ("a label with a control character", {"label": "x\x1b[2Jy"}),
    ("a device_id that is not the signer", {"device_id": "k1:0000000000000000"}),
    ("a device_id of 10 KB", {"device_id": "k" * 10_000}),
    ("a field this version does not know, 100 KB", {"extra": "z" * 100_000}),
    ("a nested field", {"extra": {"a": {"b": "c"}}}),
])
def test_an_oversized_or_misshapen_join_request_is_refused_and_never_written(tmp_path, monkeypatch, what, payload):
    _run(tmp_path, "owner", "init")
    hvm = _load_hv(tmp_path, monkeypatch)
    dev = _foreign_device(hvm)
    body = {"action": "join-request", "device_id": dev[2], "label": "joiner", "url": "http://100.64.0.9:9876"}
    body.update(payload)
    accepted, _ = hvm.append_foreign_entries([_signed(hvm, dev, 1, "governance", body)])
    assert accepted == 0, f"{what} was accepted"
    assert not [e for e in merkle.read_all_entries(hvm.JOURNAL_DIR) if e.get("node_id") == dev[2]]
    assert _peers_json(tmp_path) == ""


def test_a_well_formed_join_request_and_announce_still_land(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hvm = _load_hv(tmp_path, monkeypatch)
    dev = _foreign_device(hvm)
    jr = _signed(hvm, dev, 1, "governance", {"action": "join-request", "device_id": dev[2], "label": "joiner",
                                            "url": "http://100.64.0.9:9876", "requested_principal": "joiner"})
    an = _signed(hvm, dev, 2, "governance", {"action": "announce", "kind": "key",
                                            "data": {"device_id": dev[2], "label": "joiner"}})
    assert hvm.append_foreign_entries([jr, an])[0] == 2


def test_an_oversized_announce_is_refused(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hvm = _load_hv(tmp_path, monkeypatch)
    dev = _foreign_device(hvm)
    big = _signed(hvm, dev, 1, "governance", {"action": "announce", "kind": "key", "data": {"label": "L" * 100_000}})
    assert hvm.append_foreign_entries([big])[0] == 0


def test_a_poisoned_join_request_already_in_a_journal_never_reaches_peers_json(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hvm = _load_hv(tmp_path, monkeypatch)
    dev = _foreign_device(hvm)
    bad = _signed(hvm, dev, 1, "governance", {"action": "join-request", "device_id": dev[2],
                                             "label": "L" * 100_000, "url": "http://" + "h" * 100_000})
    hvm._append_line(hvm._journal_path_for(bad["timestamp"]), json.dumps(bad))      # as an older node would have stored it
    assert hvm._join_request_url(dev[2]) == "" and hvm._join_request_label(dev[2]) == ""
    assert hvm._add_peer("http://" + "h" * 100_000, "x") is False
    assert hvm._add_peer("http://100.64.0.9:9876", "L" * 100_000) is True         # a bad label falls back to the url
    assert json.loads(_peers_json(tmp_path))["peers"] == [{"url": "http://100.64.0.9:9876", "id": "http://100.64.0.9:9876"}]
