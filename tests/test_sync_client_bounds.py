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
        with pytest.raises(sc.SyncBoundError, match="deadline"):   # not a JSON error from a short body
            sc._get(s.url, "/sync/hello")
    finally:
        sc._round.deadline = None
    assert time.monotonic() - t0 < 5


def test_slow_body_chunks_hit_the_deadline_check_without_the_timer(serve, monkeypatch):
    """The timer is the cut; the per-chunk check is the backstop. Pin the backstop on its own."""
    class _NoTimer:
        def __init__(self, *a, **k): pass
        daemon = True
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(sc.threading, "Timer", _NoTimer)

    def slow_chunks(h):
        h.send_response(200)
        h.end_headers()
        try:
            for _ in range(20):
                h.wfile.write(b"x" * sc._STREAM_CHUNK)
                h.wfile.flush()
                time.sleep(0.3)
        except OSError:
            pass
    s = serve(slow_chunks)
    sc._round.deadline = time.monotonic() + 1.0
    try:
        with pytest.raises(sc.SyncBoundError, match="deadline"):
            sc._get(s.url, "/sync/hello")
    finally:
        sc._round.deadline = None


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


def _drip_headers(h):
    """Never finishes its headers: a header line every 100 ms, each well inside any idle timeout."""
    try:
        h.wfile.write(b"HTTP/1.1 200 OK\r\n")
        for _ in range(600):
            h.wfile.flush()
            time.sleep(0.1)
            h.wfile.write(b"X-Pad: 1\r\n")
    except OSError:
        pass


def _drip_status_line(h):
    try:
        for byte in b"HTTP/1.1 200 OK" + b" " * 600:
            h.wfile.write(bytes([byte]))
            h.wfile.flush()
            time.sleep(0.1)
    except OSError:
        pass


@pytest.mark.parametrize("drip", [_drip_headers, _drip_status_line])
@pytest.mark.parametrize("verb", ["get", "post"])
def test_drip_before_the_headers_is_cut_at_the_round_deadline(serve, monkeypatch, drip, verb):
    s = serve(drip)
    monkeypatch.setattr(sc, "ROUND_DEADLINE", 1.5)
    sc._round.deadline = time.monotonic() + 1.5
    t0 = time.monotonic()
    try:
        with pytest.raises(sc.SyncBoundError, match="deadline"):
            if verb == "get":
                sc._get(s.url, "/sync/hello")
            else:
                sc._post(s.url, "/sync/ingest", {"entries": []})
    finally:
        sc._round.deadline = None
    assert time.monotonic() - t0 < 4


def test_a_round_of_slow_requests_is_cut_at_the_total_deadline(serve, monkeypatch):
    """Each request is well inside its own idle timeout; together they pass the round deadline."""
    def slow(h):
        time.sleep(0.4)
        body = b"{}"
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    s = serve(slow)
    calls = []

    def many_requests(peer, mode=None):
        for _ in range(12):
            sc._get(s.url, "/sync/hello")
            calls.append(1)
    monkeypatch.setattr(sc, "_sync_round", many_requests)
    monkeypatch.setattr(sc, "ROUND_DEADLINE", 1.6)
    t0 = time.monotonic()
    with pytest.raises(sc.SyncBoundError, match="deadline"):
        sc._sync_with_peer({"url": s.url})
    assert time.monotonic() - t0 < 3.5
    assert 1 <= len(calls) < 12
    assert sc._round.deadline is None


def test_watchdog_state_is_clean_after_a_request(serve):
    s = serve(lambda h: (h.send_response(200), h.send_header("Content-Length", "2"), h.end_headers(), h.wfile.write(b"{}")))
    assert sc._get(s.url, "/sync/hello") == {}
    assert getattr(sc._round, "cell", None) is None
    assert sc._get(s.url, "/sync/hello") == {}      # a pooled connection is reused and tracked again


def test_a_body_cut_cleanly_at_the_deadline_is_refused_not_parsed_short(serve, monkeypatch):
    """A shutdown can end a read as a clean EOF; a short body must not be returned as complete."""
    cells = []
    real_cell = sc._Cell
    monkeypatch.setattr(sc, "_Cell", lambda: cells.append(real_cell()) or cells[-1])
    real_iter = requests.Response.iter_content

    def cut_after_last_chunk(self, *a, **k):
        yield from real_iter(self, *a, **k)
        cells[-1].fire()
    monkeypatch.setattr(requests.Response, "iter_content", cut_after_last_chunk)

    def ok(h):
        h.send_response(200)
        h.send_header("Content-Length", "2")
        h.end_headers()
        h.wfile.write(b"{}")
    s = serve(ok)
    with pytest.raises(sc.SyncBoundError, match="deadline"):
        sc._get(s.url, "/sync/hello")


def _raw_server(serve_conn):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=serve_conn, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.getsockname()[1]}"


def _read_request(c):
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = c.recv(4096)
        if not d:
            return False
        buf += d
    return True


def _drip_bytes(c):
    try:
        for _ in range(600):
            c.sendall(b"H")
            time.sleep(0.1)
    except OSError:
        pass


def test_a_drip_on_a_reused_pooled_connection_is_cut(monkeypatch):
    """One good keep-alive reply, then the second request on the same connection drips its status line."""
    def conn(c):
        try:
            if _read_request(c):
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            if _read_request(c):
                _drip_bytes(c)
        except OSError:
            pass
    srv, url = _raw_server(conn)
    try:
        sc._session = None
        monkeypatch.setattr(sc, "ROUND_DEADLINE", 1.5)
        sc._round.deadline = time.monotonic() + 1.5
        try:
            assert sc._get(url, "/sync/hello") == {}
            t0 = time.monotonic()
            with pytest.raises(sc.SyncBoundError, match="deadline"):
                sc._get(url, "/sync/hello")
        finally:
            sc._round.deadline = None
        assert time.monotonic() - t0 < 4
    finally:
        srv.close()


def test_a_proxy_in_the_environment_does_not_escape_the_round_deadline(monkeypatch):
    """HTTP_PROXY must not route sync traffic through untracked pools: a dripping proxy is still cut."""
    def conn(c):
        if _read_request(c):
            _drip_bytes(c)
    proxy, purl = _raw_server(conn)
    try:
        monkeypatch.setenv("HTTP_PROXY", purl)
        monkeypatch.setenv("http_proxy", purl)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        sc._session = None
        assert sc._sess().trust_env is False
        ok = _Server(lambda h: (h.send_response(200), h.send_header("Content-Length", "2"), h.end_headers(), h.wfile.write(b"{}")))
        try:
            assert sc._get(ok.url, "/sync/hello") == {}    # went direct, not via the proxy
        finally:
            ok.close()
        sc._session = None
    finally:
        proxy.close()
