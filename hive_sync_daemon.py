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
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import merkle
import sync_common

hv = sync_common.load_hv()

# Read-only dashboard SPA, served from the committed dashboard/ dir next to this file. An explicit
# allowlist (name -> content-type) — NOT arbitrary file serving — so there is no path-traversal
# surface. The /api/* JSON below is the data layer; both are read-only and reachable wherever the
# sync port is (the daemon already serves the corpus via /sync/chunk, so this adds no new exposure).
_DASH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
_STATIC = {
    "index.html": "text/html; charset=utf-8",
    "logo.svg": "image/svg+xml",
    "favicon.svg": "image/svg+xml",
}

# Sync wire-protocol version. Advertised in /sync/hello and /hive/info so additive handshake
# changes (e.g. later read-gating) can be negotiated without a journal-schema break.
PROTOCOL_VERSION = 1

# Serialize journal-mutating ingests so an inbound POST and the periodic
# outbound sync (M5) don't rebuild SQLite concurrently.
_ingest_lock = threading.Lock()

# ── DoS / abuse hardening ────────────────────────────────────────────────────────────────────
# The tailnet (Tailscale) is the trust perimeter, but a SINGLE misbehaving/compromised peer must
# not be able to OOM the node (huge body), thread-exhaust it (connection flood), wedge it (slow
# read), or flood it (request storm). These guards are best-effort and NOT consensus-bearing.
MAX_BODY_BYTES = 32 * 1024 * 1024          # reject /sync/ingest bodies larger than this (413)
SOCKET_TIMEOUT = 30                        # per-connection read timeout, seconds (slow-loris guard)
MAX_CONCURRENT_REQUESTS = 32               # in-flight handlers; excess → 503 (thread-exhaustion guard)
RATE_BUCKET_CAPACITY = 256                 # per-peer token bucket burst (generous: a full chunked
RATE_REFILL_PER_SEC = 64                   #   sync is bursty — these throttle abuse, not real sync)

_request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)


class _RateLimiter:
    """Best-effort in-memory token bucket per peer-IP. Blunts a chatty/abusive peer without
    throttling a legitimate (bursty) sync. Buckets are pruned lazily so the map stays bounded."""

    def __init__(self, capacity, refill_per_sec):
        self.capacity = float(capacity)
        self.refill = float(refill_per_sec)
        self._buckets = {}                 # ip -> (tokens, last_monotonic)
        self._lock = threading.Lock()

    def allow(self, ip):
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(ip, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1.0:
                self._buckets[ip] = (tokens, now)
                return False
            self._buckets[ip] = (tokens - 1.0, now)
            if len(self._buckets) > 4096:  # lazy prune: drop buckets idle > 1h
                self._buckets = {k: (t, ts) for k, (t, ts) in self._buckets.items()
                                 if ts >= now - 3600}
            return True


_rate_limiter = _RateLimiter(RATE_BUCKET_CAPACITY, RATE_REFILL_PER_SEC)


def _entries():
    return merkle.read_all_entries(hv.JOURNAL_DIR)


class Handler(BaseHTTPRequestHandler):
    server_version = "hive-sync/2.0"
    timeout = SOCKET_TIMEOUT             # honored by socketserver setup() → socket read timeout

    def setup(self):
        super().setup()
        try:                             # belt-and-suspenders: bound slow reads even if `timeout` is ignored
            self.connection.settimeout(SOCKET_TIMEOUT)
        except Exception:
            pass
        sync_common.clamp_mss(self.connection)   # keep responses under a sub-1280-MTU tailnet ceiling

    def log_message(self, fmt, *args):  # keep the daemon quiet; errors go to do_*
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name):
        """Serve one allowlisted dashboard asset (no traversal: name must be a literal key)."""
        ctype = _STATIC.get(name)
        if not ctype:
            return self._send(404, {"error": "not found"})
        try:
            with open(os.path.join(_DASH_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, {"error": "dashboard asset missing"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _enter(self):
        """Rate-limit + concurrency gate. Returns True if the request may proceed (caller MUST then
        call _leave() in a finally); otherwise sends 429/503 and returns False."""
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_limiter.allow(ip):
            self._send(429, {"error": "rate limited"})
            return False
        if not _request_slots.acquire(blocking=False):
            self._send(503, {"error": "server busy"})
            return False
        return True

    def _leave(self):
        try:
            _request_slots.release()
        except Exception:
            pass

    def do_GET(self):
        if not self._enter():
            return
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
            elif u.path == "/hive/info":
                # Discovery: minimal hive metadata + the signed genesis (for verification). NEVER
                # the journal — so listing stays open even if reads are gated later.
                es = _entries()
                gov = hv._governance_state(es)
                self._send(200, {
                    "hive_id": gov["hive_id"],
                    "owner_id": gov["owner_id"],
                    "label": hv.NODE_LABEL,
                    "node_count": len(gov["admitted"]) or len({e.get("node_id") for e in es}),
                    "protocol_version": PROTOCOL_VERSION,
                    "genesis": hv._owner_declaration(es),
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
            elif u.path == "/api/overview":
                self._send(200, hv.api_overview())
            elif u.path == "/api/search":
                self._send(200, hv.api_search(
                    query=q.get("q", [""])[0], tag=(q.get("tag", [None])[0] or None),
                    kind=q.get("kind", ["all"])[0],
                    min_confidence=float(q.get("min_confidence", ["0"])[0] or 0)))
            elif u.path == "/api/peers":
                self._send(200, {"peers": hv.api_peers()})
            elif u.path in ("/", "/index.html", "/dashboard", "/dashboard/"):
                self._serve_static("index.html")
            elif u.path in ("/logo.svg", "/favicon.svg"):
                self._serve_static(u.path.lstrip("/"))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never crash the handler thread
            self._send(500, {"error": str(e)})
        finally:
            self._leave()

    def do_POST(self):
        if not self._enter():
            return
        try:
            u = urlparse(self.path)
            if u.path != "/sync/ingest":
                self._send(404, {"error": "not found"})
                return
            # Bound the request body BEFORE reading it: a missing/garbage Content-Length is a 400,
            # an over-cap one is a 413 — so no peer can stream an unbounded body into memory.
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send(400, {"error": "bad Content-Length"})
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, {"error": "request too large", "max_bytes": MAX_BODY_BYTES})
                return
            body = json.loads((self.rfile.read(length) if length else b"{}") or b"{}")
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
        finally:
            self._leave()


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


def _persist_port(port):
    """Record the port the daemon actually bound, so the CLI + peers use it."""
    try:
        path = sync_common.hive_home() / ".peers.json"
        cfg = json.loads(path.read_text()) if path.exists() else {}
        cfg["port"] = port
        path.write_text(json.dumps(cfg, indent=2) + "\n")
    except Exception:
        pass


def make_server(bind=None, port=None):
    cfg = sync_common.load_peers()
    bind = bind if bind is not None else cfg.get("bind", "0.0.0.0")
    base = port if port is not None else cfg.get("port", sync_common.PORT_DEFAULT)
    # Bind the base port, or fall back to the next few if a NON-hive service squats it — so a port
    # conflict degrades gracefully instead of failing the install. A healthy hive on the base port
    # still means "already running" (redundant launch → clean no-op).
    for p in range(base, base + 5):
        try:
            srv = ThreadingHTTPServer((bind, p), Handler)
            sync_common.clamp_mss(srv.socket)   # accepted connections inherit the clamped MSS (Linux)
            if p != base:
                print(f"sync daemon: :{base} is held by a non-hive service; using :{p}")
                _persist_port(p)
            return srv, bind, p
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                if _incumbent_is_hive(bind, p):
                    raise AlreadyRunning(f"a healthy hive sync daemon already owns {bind}:{p}") from e
                continue   # a squatter on this port — try the next one
            raise
    raise OSError(f"no free port in {base}..{base + 4} for the hive sync daemon")


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
