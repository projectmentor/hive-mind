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

import base64
import collections
import errno
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

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
# changes can be negotiated without a journal-schema break. Bumped to 2 with read-authentication
# (GHSA-242f): a peer reporting >= 2 understands the Hive-Auth-* signed-request envelope, which
# gates the enforce-flip during a phased rollout.
PROTOCOL_VERSION = 2

# The address:port the daemon actually bound, filled in by make_server() so the handler can
# advertise a reachable endpoint to peers in /sync/hello and /hive/info.
_ADVERTISED = {"addr": None}

# ── read-auth endpoint classification (GHSA-242f) ────────────────────────────────────────────────
# open-discovery : reachable unauthenticated — no journal/corpus content; a joiner/health check needs
#                  them BEFORE admission (hive id, owner id, genesis, node id, root hash, verdict).
# remote-auth    : a remote reader must present a valid signed envelope (honors permissive→enforce).
# loopback-only  : local operator (dashboard/hv) OR a valid signed peer proxy; remote-unsigned = 403
#                  even in permissive (this is corpus/telemetry/dashboard data — the disclosure).
_OPEN_DISCOVERY = frozenset({"/hive/info", "/sync/merkle-root", "/api/verify"})
_REMOTE_AUTH = frozenset({"/sync/hello", "/sync/chunk"})
_LOOPBACK_ONLY = frozenset({
    "/api/overview", "/api/search", "/api/tags", "/api/related", "/api/audit",
    "/api/status", "/api/telemetry", "/api/peers",
    "/", "/index.html", "/dashboard", "/dashboard/", "/logo.svg", "/favicon.svg",
})

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


_HV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hv")


def _run_hv_text(args, timeout):
    """Capture the text output of a read-only `hv` subcommand for the dashboard Status view. The args
    are HARD-CODED at the call site (never client input), so there's no command-injection surface."""
    try:
        r = subprocess.run([sys.executable, _HV_PATH, *args], capture_output=True, text=True,
                           timeout=timeout, env=os.environ)
        out = r.stdout.strip()
        if r.stderr.strip():
            out = (out + "\n" + r.stderr.strip()).strip()
        return out or "(no output)"
    except Exception as e:
        return f"(error running hv {' '.join(args)}: {e})"


def _api_status():
    """`hv whoami` + `hv stats` + `hv doctor` text — the header owner-chip Status view."""
    return {"whoami": _run_hv_text(["whoami"], 10),
            "stats": _run_hv_text(["stats"], 10),
            "doctor": _run_hv_text(["doctor"], 30)}


# ── read-authentication (GHSA-242f) ──────────────────────────────────────────────────────────────
# A bounded, per-process nonce cache gives replay protection within the freshness window. Growth is
# capped by the rate limiter × window; on overflow we drop expired entries, then clear (which only
# re-opens a <=window replay chance for already-captured requests — self-healing). A daemon restart
# also clears it; the timestamp window bounds the risk either way.
_nonce_lock = threading.Lock()
_nonce_cache = {}                 # nonce_hex -> expiry_monotonic
_NONCE_CACHE_MAX = 100_000

# Permissive-mode observability: count would-be-blocked remote reads so an operator can see there is
# pre-enforcement traffic before flipping to enforce.
_auth_flag_lock = threading.Lock()
_auth_flags = collections.Counter()


def _nonce_seen(nonce, ttl):
    """Record `nonce`; return True iff it was already recorded within its TTL (a replay)."""
    now = time.monotonic()
    with _nonce_lock:
        exp = _nonce_cache.get(nonce)
        if exp is not None and exp > now:
            return True
        if len(_nonce_cache) > _NONCE_CACHE_MAX:
            for k in [k for k, e in _nonce_cache.items() if e <= now]:
                _nonce_cache.pop(k, None)
            if len(_nonce_cache) > _NONCE_CACHE_MAX:
                _nonce_cache.clear()
        _nonce_cache[nonce] = now + ttl
        return False


def _auth_flag(reason):
    with _auth_flag_lock:
        _auth_flags[reason] += 1
        n = _auth_flags[reason]
    if n <= 3 or n % 100 == 0:   # don't spam the journal
        print(f"sync-auth[permissive]: served a remote read that would be blocked under enforce "
              f"({reason}); count={n}. Flip to enforce once all peers report protocol "
              f"{PROTOCOL_VERSION}.", file=sys.stderr)


def _verify_sync_request(headers, method, path, query, body_bytes, gov):
    """Return (ok, reason, device_id). ok iff the Hive-Auth-* envelope is a valid Ed25519 signature,
    by an ADMITTED device, over THIS exact request, with a fresh (non-replayed) timestamp+nonce.
    Reuses hv's device-key identity. Pre-owner hives (no owner yet) accept any valid signature so the
    genesis/owner declaration can still propagate during bootstrap (deeper bootstrap hardening is a
    deferred follow-up)."""
    if hv._ed25519 is None:
        return (False, "no-verifier", None)
    alg = headers.get("Hive-Auth-Alg")
    device_id = headers.get("Hive-Auth-Device")
    pub_b64 = headers.get("Hive-Auth-Pub")
    ts_raw = headers.get("Hive-Auth-Ts")
    nonce = headers.get("Hive-Auth-Nonce")
    sig_b64 = headers.get("Hive-Auth-Sig")
    if not all((alg, device_id, pub_b64, ts_raw, nonce, sig_b64)):
        return (False, "missing-auth-headers", None)
    if alg != sync_common.HIVE_AUTH_ALG:
        return (False, "bad-alg", None)
    try:
        pub = base64.b64decode(pub_b64)
        sig = base64.b64decode(sig_b64)
        ts = int(ts_raw)
    except Exception:
        return (False, "malformed-auth", None)
    if len(pub) != 32 or hv._device_id_for_pub(pub) != device_id:
        return (False, "pub-fingerprint-mismatch", device_id)
    if abs(int(time.time()) - ts) > sync_common.SYNC_AUTH_WINDOW:
        return (False, "stale-timestamp", device_id)
    msg = sync_common.sync_signing_bytes(method, path, query, body_bytes or b"", ts, nonce)
    if not hv._ed25519.verify(msg, sig, pub):
        return (False, "bad-signature", device_id)
    if _nonce_seen(nonce, sync_common.SYNC_AUTH_WINDOW * 2):
        return (False, "replayed-nonce", device_id)
    if gov.get("owner_id"):
        if device_id not in gov.get("admitted", set()) or device_id in gov.get("purged", set()):
            return (False, "not-admitted", device_id)
    return (True, "ok", device_id)


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

    def _is_loopback(self):
        """True iff the request arrived on the loopback interface (local operator / dashboard / hv).
        Sound as a trust signal ONLY because the daemon no longer binds 0.0.0.0 — a remote peer's
        source address is its tailnet IP, never 127.x/::1."""
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1") or ip.startswith("127.")

    def _gov(self):
        return hv._governance_state(_entries())

    def _authorized(self, u, body=b""):
        """Gate a REMOTE-AUTH read (/sync/hello, /sync/chunk, /sync/ingest). Loopback is always
        allowed. Otherwise honor the node's sync_auth mode: off → allow; permissive → allow but flag;
        enforce → require a valid signed envelope from an admitted device (else 401). Returns False
        (and sends the response) when it blocks."""
        if self._is_loopback():
            return True
        mode = sync_common.sync_auth_mode()
        if mode == "off":
            return True
        ok, reason, _dev = _verify_sync_request(self.headers, self.command, u.path, u.query, body, self._gov())
        if ok:
            return True
        if mode == "permissive":
            _auth_flag(reason)
            return True
        self._send(401, {"error": "authentication required", "detail": reason})
        return False

    def _local_or_signed(self, u, body=b""):
        """Gate a LOOPBACK-ONLY endpoint (dashboard SPA + /api/* corpus/telemetry data). Allow the
        local operator, or a valid SIGNED request from an admitted peer (the per-node /api proxy).
        A remote-unsigned request is 403 even in permissive mode — this data is never for anonymous
        remotes, which is the disclosure the advisory flags."""
        if self._is_loopback():
            return True
        if sync_common.sync_auth_mode() == "off":
            return True
        ok, reason, _dev = _verify_sync_request(self.headers, self.command, u.path, u.query, body, self._gov())
        if ok:
            return True
        self._send(403, {"error": "forbidden", "detail": reason})
        return False

    def do_GET(self):
        if not self._enter():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            # Read-auth gate (GHSA-242f): classify the path once. open-discovery falls through;
            # remote-auth and loopback-only are enforced here (the /api/* proxy paths are in
            # loopback-only, so they're gated here too, then the outbound proxy re-signs below).
            if u.path in _REMOTE_AUTH:
                if not self._authorized(u):
                    return
            elif u.path in _LOOPBACK_ONLY:
                if not self._local_or_signed(u):
                    return
            # Per-node proxy: these read endpoints can be served for a SPECIFIC node — self = local,
            # a peer = proxied over the tailnet. hv resolves the address from the admitted-peer probe
            # (never from the client), so the proxy can't be aimed at an arbitrary host (no SSRF).
            # This serves /api/* data, so gate it loopback-only-or-signed like the local /api routes.
            node = q.get("node", [None])[0]
            if node and node != hv.NODE_ID and u.path in (
                    "/api/overview", "/api/search", "/api/status", "/api/telemetry"):
                addr = hv.api_node_addr(node)
                if not addr:
                    self._send(200, {"available": False, "reachable": False, "node_id": node,
                                     "error": "node unreachable or no advertised address"})
                    return
                qs = "&".join(f"{k}={quote(v[0])}" for k, v in q.items() if k != "node")
                try:
                    # generous timeout: a slow mobile node computes /api/overview (merkle + governance
                    # over the whole journal in pure Python) in ~10-15s; the SPA shows a spinner meanwhile.
                    # Sign the proxied read with THIS node's device key so the admitted peer accepts it.
                    fwd_qs = qs if qs else ""
                    hdrs = sync_common.sign_sync_request("GET", u.path, fwd_qs, b"")
                    req = urllib.request.Request(
                        f"http://{addr}{u.path}" + (f"?{fwd_qs}" if fwd_qs else ""), headers=hdrs)
                    with urllib.request.urlopen(req, timeout=20) as rr:
                        body = rr.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self._send(200, {"available": False, "reachable": False, "node_id": node,
                                     "error": str(e)[:90]})
                return
            if u.path == "/sync/hello":            # remote-auth (gated above): per-node maxima + hashes
                es = _entries()
                self._send(200, {
                    "node_id": hv.NODE_ID,
                    "hive_id": hv._local_hive_id(),
                    "protocol_version": PROTOCOL_VERSION,
                    "advertised_addr": _ADVERTISED["addr"],
                    "journal_summary": {"total": len(es), "by_node": merkle.node_max_seq(es)},
                    "chunks": merkle.node_chunk_hashes(es),
                })
            elif u.path == "/hive/info":
                # Open discovery: minimal hive metadata + the signed genesis (for verification) + the
                # node_id/advertised address a joiner or probe needs BEFORE it is admitted. NEVER the
                # journal — so listing stays open while reads are gated.
                es = _entries()
                gov = hv._governance_state(es)
                self._send(200, {
                    "node_id": hv.NODE_ID,
                    "hive_id": gov["hive_id"],
                    "owner_id": gov["owner_id"],
                    "label": hv.NODE_LABEL,
                    "node_count": len(gov["admitted"]) or len({e.get("node_id") for e in es}),
                    "protocol_version": PROTOCOL_VERSION,
                    "advertised_addr": _ADVERTISED["addr"],
                    "genesis": hv._owner_declaration(es),
                })
            elif u.path == "/sync/merkle-root":
                es = _entries()                     # open discovery: a single root hash, no content
                self._send(200, {"root_hash": merkle.merkle_root(merkle.chunk_hashes(es))})
            elif u.path == "/sync/chunk":          # remote-auth (gated above): the journal itself
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
                    min_confidence=float(q.get("min_confidence", ["0"])[0] or 0),
                    limit=max(1, min(200, int(q.get("limit", ["50"])[0] or 50))),
                    offset=max(0, int(q.get("offset", ["0"])[0] or 0)),
                    sort=q.get("sort", ["salience"])[0], status=q.get("status", ["all"])[0]))
            elif u.path == "/api/tags":
                self._send(200, hv.api_tags())
            elif u.path == "/api/related":
                self._send(200, hv.api_related(
                    kind=q.get("kind", ["fact"])[0],
                    item_id=int(q.get("id", ["0"])[0] or 0)))
            elif u.path == "/api/audit":
                self._send(200, hv.api_audit())
            elif u.path == "/api/status":
                cmd = q.get("cmd", [None])[0]   # per-command so the UI can load progressively
                if cmd in ("whoami", "stats", "doctor"):
                    self._send(200, {cmd: _run_hv_text([cmd], 40 if cmd == "doctor" else 12)})
                else:
                    self._send(200, _api_status())
            elif u.path == "/api/verify":
                self._send(200, hv.api_verify())
            elif u.path == "/api/telemetry":          # node!=self already handled by the proxy above
                lim = max(1, min(2000, int(q.get("limit", ["25"])[0] or 25)))
                off = max(0, int(q.get("offset", ["0"])[0] or 0))
                sort = q.get("sort", ["recent"])[0]
                fproj = q.get("fproject", [None])[0]
                fagent = q.get("fagent", [None])[0]
                fnode = q.get("fnode", [None])[0]
                fday = q.get("fday", [None])[0]
                fmodel = q.get("fmodel", [None])[0]
                if q.get("scope", ["self"])[0] == "hive":   # combined across reachable nodes
                    self._send(200, hv.api_telemetry_hive(limit=lim, offset=off, sort=sort, project=fproj,
                                                          agent=fagent, node=fnode, day=fday, model=fmodel))
                else:                                       # local (also what peers answer during aggregation)
                    self._send(200, hv.api_telemetry(limit=lim, offset=off, sort=sort, project=fproj,
                                                     agent=fagent, day=fday, model=fmodel))
            elif u.path == "/api/peers":
                probe = q.get("probe", ["1"])[0] not in ("0", "false", "no")
                self._send(200, {"peers": hv.api_peers(probe=probe)})
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
            raw = self.rfile.read(length) if length else b""
            # remote-auth: read-auth gate over the exact body bytes, so an unadmitted device can't
            # even attempt an ingest under enforce (entries are still per-entry verified below).
            if not self._authorized(u, body=raw):
                return
            body = json.loads(raw or b"{}")
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


def _advertised_addr(bind, port):
    """The host:port to advertise to peers (a reachable endpoint), or None when the bind isn't
    peer-reachable. Loopback → None (single-node/local only). All-interfaces → the tailnet IP if
    discoverable. A specific bind (tailnet IP) → itself."""
    if bind in ("127.0.0.1", "::1", "localhost") or str(bind).startswith("127."):
        return None
    host = bind
    if bind in ("0.0.0.0", "::", ""):
        host = sync_common.tailscale_ip()
    return f"{host}:{port}" if host else None


def make_server(bind=None, port=None):
    cfg = sync_common.load_peers()
    # Default bind is no longer 0.0.0.0 (GHSA-242f): resolve_bind picks HIVE_BIND / explicit
    # .peers.json bind / the tailnet IP / loopback — so the daemon never listens on all interfaces
    # by default. Loopback-trust in the handler is sound precisely because of this.
    bind = bind if bind is not None else sync_common.resolve_bind(cfg)
    base = port if port is not None else cfg.get("port", sync_common.PORT_DEFAULT)
    if os.environ.get("HIVE_BIND") == "0.0.0.0":
        print("sync daemon: WARNING bound to 0.0.0.0 (all interfaces) via HIVE_BIND — loopback-trust "
              "is weaker here; prefer a tailnet-IP bind and 'hv sync auth enforce'.", file=sys.stderr)
    # Bind the base port, or fall back to the next few if a NON-hive service squats it — so a port
    # conflict degrades gracefully instead of failing the install. A healthy hive on the base port
    # still means "already running" (redundant launch → clean no-op).
    for p in range(base, base + 5):
        try:
            srv = ThreadingHTTPServer((bind, p), Handler)
            sync_common.clamp_mss(srv.socket)   # accepted connections inherit the clamped MSS (Linux)
            _ADVERTISED["addr"] = _advertised_addr(bind, p)
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


def _needs_loopback_alias(bind):
    """True iff the primary bind is a SPECIFIC non-loopback address (e.g. the tailnet IP). In that
    case loopback is otherwise unserved — which would break the local dashboard/hv and force the
    operator through remote auth on their own node — so we ALSO bind 127.0.0.1. Not needed when the
    bind already covers loopback (0.0.0.0) or IS loopback."""
    return (bind not in ("0.0.0.0", "::", "", None, "127.0.0.1", "::1", "localhost")
            and not str(bind).startswith("127."))


def _extra_servers(primary_bind, port):
    """Best-effort loopback listener (same port) so LOCAL access is always trusted-unauthenticated
    even when the daemon's primary bind is the tailnet IP. Returns a list of extra servers to run."""
    extra = []
    if _needs_loopback_alias(primary_bind):
        try:
            s = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            sync_common.clamp_mss(s.socket)
            extra.append(s)
        except OSError as e:
            print(f"sync daemon: could not also bind 127.0.0.1:{port} ({e}); "
                  f"local access must use {primary_bind}:{port}", file=sys.stderr)
    return extra


def serve_forever(bind=None, port=None):
    """Run the HTTP server(s) in the foreground (blocks). Serves the primary bind plus a loopback
    alias so local access works when the primary is a specific (tailnet) address."""
    hv.init_db()
    try:
        server, bind, port = make_server(bind, port)
    except AlreadyRunning as e:
        print(f"sync daemon: {e}; nothing to do")
        return
    extra = _extra_servers(bind, port)
    for s in extra:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    also = " (+127.0.0.1)" if extra else ""
    print(f"sync daemon: serving on {bind}:{port}{also} as {hv.NODE_ID}")
    try:
        server.serve_forever()
    finally:
        for s in extra:
            s.shutdown()


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
    extra = _extra_servers(bind, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for s in extra:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    also = " (+127.0.0.1)" if extra else ""
    print(f"sync daemon: serving on {bind}:{port}{also} as {hv.NODE_ID}; outbound every {interval}s")
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
        for s in extra:
            s.shutdown()


if __name__ == "__main__":
    run_daemon()
