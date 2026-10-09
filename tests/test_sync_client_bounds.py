"""hive-mind-private #55: what one peer can make the sync client spend. A round has a total deadline,
a reply has a byte cap, and a redirect is refused without a request to its target or any auth header.
Real local HTTP servers, no mocks of the client."""
import http.server
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import requests

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-clientbounds-")

import sync_client as sc  # noqa: E402


class _Server:
    def __init__(self, handler):
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                handler(self)

            do_POST = do_GET

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def serve():
    made = []

    def make(handler):
        s = _Server(handler)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def test_drip_is_cut_at_the_round_deadline(serve, monkeypatch):
    def drip(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.end_headers()
        try:
            h.wfile.write(b"[")
            for _ in range(600):
                h.wfile.flush()
                time.sleep(0.1)
                h.wfile.write(b" ")
        except OSError:
            pass
    s = serve(drip)
    monkeypatch.setattr(sc, "ROUND_DEADLINE", 1.5)
    sc._round.deadline = time.monotonic() + 1.5
    t0 = time.monotonic()
    try:
        with pytest.raises(requests.RequestException):
            sc._get(s.url, "/sync/hello")
    finally:
        sc._round.deadline = None
    assert time.monotonic() - t0 < 5


def test_response_over_the_cap_is_refused(serve, monkeypatch):
    monkeypatch.setattr(sc, "MAX_RESPONSE_BYTES", 4096)

    def big(h):                                   # chunked: no Content-Length to trust
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for _ in range(20):
                h.wfile.write(b"400\r\n" + b"x" * 0x400 + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
    s = serve(big)
    with pytest.raises(sc.SyncBoundError, match="size cap"):
        sc._get(s.url, "/sync/chunk")


def test_declared_oversize_is_refused(serve, monkeypatch):
    monkeypatch.setattr(sc, "MAX_RESPONSE_BYTES", 100)

    def declared(h):
        h.send_response(200)
        h.send_header("Content-Length", "100000")
        h.end_headers()
    s = serve(declared)
    with pytest.raises(sc.SyncBoundError, match="size cap"):
        sc._get(s.url, "/sync/chunk")


def test_normal_reply_still_parses(serve):
    def ok(h):
        body = b'{"root_hash": "abc"}'
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    s = serve(ok)
    assert sc._get(s.url, "/sync/merkle-root") == {"root_hash": "abc"}
    assert sc._post(s.url, "/sync/ingest", {"entries": []}) == {"root_hash": "abc"}


@pytest.mark.parametrize("code", [301, 302, 307, 308])
@pytest.mark.parametrize("verb", ["get", "post"])
def test_redirect_is_refused_and_target_gets_nothing(serve, monkeypatch, code, verb):
    target = serve(lambda h: (h.send_response(200), h.end_headers()))
    # a signed request, so there is an auth header to leak
    monkeypatch.setattr(sc.sync_common, "sign_sync_request",
                        lambda *a, **k: {"Hive-Auth": "secret-sig", "Hive-Auth-Nonce": "n"})

    def redirect(h):
        h.send_response(code)
        h.send_header("Location", f"{target.url}/stolen")
        h.end_headers()
    s = serve(redirect)
    with pytest.raises(sc.SyncBoundError, match="redirect refused"):
        if verb == "get":
            sc._get(s.url, "/sync/hello")
        else:
            sc._post(s.url, "/sync/ingest", {"entries": []})
    assert target.seen == []
    assert len(s.seen) == 1                       # asked once, never retried at the target


def test_a_refused_peer_does_not_stop_the_round_for_others(serve, monkeypatch, capsys):
    s = serve(lambda h: (h.send_response(302), h.send_header("Location", "http://127.0.0.1:1/"), h.end_headers()))
    monkeypatch.setattr(sc.sync_common, "load_peers", lambda: {"peers": [{"url": s.url}, {"url": s.url}]})
    monkeypatch.setattr(sc.hv, "init_db", lambda: None)
    monkeypatch.setattr(sc, "_local_entries", lambda: [])
    sc.sync_now()
    out = capsys.readouterr().out
    assert out.count("redirect refused") == 2
    assert sc._round.deadline is None
