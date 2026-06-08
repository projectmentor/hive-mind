"""
Hive Mind sync daemon (Phase 2, P2P_DESIGN.md §5).

A tiny stdlib HTTP service (no FastAPI dependency) exposing the 4 sync
endpoints over the Tailnet. Separate process from the Laravel dashboard
(:8000) — this binds :9876 by default. Reads .peers.json for bind/port.

Endpoints:
  GET  /sync/hello        -> node_id + journal summary (by_node maxima) + per-node chunk hashes
  GET  /sync/merkle-root  -> global root hash (O(1) "are we identical?")
  GET  /sync/chunk?node=X&start=1&end=100 -> {entries, hash}
  POST /sync/ingest       -> append foreign entries (G-Set dedup), rebuild, {accepted, duplicates}
"""

import errno
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import merkle
import sync_common

hv = sync_common.load_hv()

# Sync wire-protocol version. Advertised in /sync/hello and /hive/info so additive handshake
# changes (e.g. later read-gating) can be negotiated without a journal-schema break.
PROTOCOL_VERSION = 1

# Serialize journal-mutating ingests so an inbound POST and the periodic
# outbound sync (M5) don't rebuild SQLite concurrently.
_ingest_lock = threading.Lock()


def _entries():
    return merkle.read_all_entries(hv.JOURNAL_DIR)


class Handler(BaseHTTPRequestHandler):
    server_version = "hive-sync/2.0"

    def log_message(self, fmt, *args):  # keep the daemon quiet; errors go to do_*
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/sync/hello":
                es = _entries()
                self._send(200, {
                    "node_id": hv.NODE_ID,
                    "hive_id": hv._local_hive_id(),
                    "protocol_version": PROTOCOL_VERSION,
                    "journal_summary": {"total": len(es), "by_node": merkle.node_max_seq(es)},
                    "chunks": merkle.node_chunk_hashes(es),
                })
            elif u.path == "/sync/merkle-root":
                es = _entries()
                self._send(200, {"root_hash": merkle.merkle_root(merkle.chunk_hashes(es))})
            elif u.path == "/sync/chunk":
                node = q.get("node", [None])[0]
                start = int(q.get("start", ["1"])[0])
                end = int(q.get("end", ["0"])[0])
                sel = merkle.entries_in_range(_entries(), node, start, end)
                self._send(200, {"entries": sel, "hash": merkle.hash_entries(sel)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never crash the handler thread
            self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/sync/ingest":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            # Refuse a cross-hive push: if both sides have a hive_id and they differ, this is a
            # different hive's journal and must not merge into ours.
            local_hive, sender_hive = hv._local_hive_id(), body.get("hive_id", "")
            if local_hive and sender_hive and local_hive != sender_hive:
                self._send(409, {"error": "different hive", "hive_id": local_hive, "accepted": 0})
                return
            entries = body.get("entries", [])
            with _ingest_lock:
                accepted, duplicates = hv.append_foreign_entries(entries)
                if accepted:
                    hv.rebuild_db()
            self._send(200, {"accepted": accepted, "duplicates": duplicates})
        except Exception as e:
            self._send(500, {"error": str(e)})


class AlreadyRunning(Exception):
    """Another healthy hive sync daemon already owns the bind address.

    Raised so a duplicate launcher (e.g. a second systemd unit, or a manual
    `hv sync daemon` while one is already up) can no-op cleanly instead of
    crash-looping on EADDRINUSE."""


def _incumbent_is_hive(bind, port, attempts=3, delay=0.5):
    """Probe whatever already holds bind:port — is it a healthy hive daemon?

    Returns True iff GET /sync/merkle-root answers with a root_hash. Retries a
    few times to cover the race where the incumbent has bound the port but is
    not serving yet (e.g. two units starting together at boot)."""
    host = "127.0.0.1" if bind in ("0.0.0.0", "", None) else bind
    url = f"http://{host}:{port}/sync/merkle-root"
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if "root_hash" in json.loads(r.read() or b"{}"):
                    return True
        except Exception:
            pass
        if i < attempts - 1:
            time.sleep(delay)
    return False


def make_server(bind=None, port=None):
    cfg = sync_common.load_peers()
    bind = bind if bind is not None else cfg.get("bind", "0.0.0.0")
    port = port if port is not None else cfg.get("port", sync_common.PORT_DEFAULT)
    try:
        return ThreadingHTTPServer((bind, port), Handler), bind, port
    except OSError as e:
        # If the port is taken by a healthy hive daemon, this launch is
        # redundant — signal the caller to exit cleanly. Any other holder
        # (or a non-responsive one) is a real error; re-raise it.
        if e.errno == errno.EADDRINUSE and _incumbent_is_hive(bind, port):
            raise AlreadyRunning(
                f"a healthy hive sync daemon already owns {bind}:{port}"
            ) from e
        raise


def serve_forever(bind=None, port=None):
    """Run the HTTP server in the foreground (blocks)."""
    hv.init_db()
    try:
        server, bind, port = make_server(bind, port)
    except AlreadyRunning as e:
        print(f"sync daemon: {e}; nothing to do")
        return
    print(f"sync daemon: serving on {bind}:{port} as {hv.NODE_ID}")
    server.serve_forever()


def run_daemon(interval=300):
    """M5: serve inbound endpoints AND run an outbound sync round every
    `interval` seconds. One process; graceful when peers are offline."""
    import sync_client

    hv.init_db()
    try:
        server, bind, port = make_server()
    except AlreadyRunning as e:
        print(f"sync daemon: {e}; nothing to do")
        return
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"sync daemon: serving on {bind}:{port} as {hv.NODE_ID}; outbound every {interval}s")
    try:
        while True:
            try:
                sync_client.sync_now()
            except Exception as e:
                print(f"sync round error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nsync daemon: shutting down")
        server.shutdown()


if __name__ == "__main__":
    run_daemon()
