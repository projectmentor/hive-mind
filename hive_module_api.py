"""Hive Mind module API, `/v1` (2.1, plan PR 4: the read-only skeleton).

A second, loopback-only HTTP listener for modules: separate processes that read the hive and (from PR 5)
write to it through the core. It is data plane only. It imports no owner-key code (the S2 boundary,
`tests/test_ownerkey_boundary.py`), serves no governance, owner, escrow, capsule, cell or comb state, and
has no write route yet.

  GET /v1/          -> {api, contract, hive_id, device_id, module, quota, config}
  GET /v1/feed      -> `hv feed`: ?after=<node_id:seq,...>&limit=
  GET /v1/search    -> `api_search`: ?q=&tag=&kind=&min_confidence=&limit=&offset=&sort=&status=
  GET /v1/item      -> `api_item`: ?id=<h:... or node_id:seq>

Every request is signed (the sync daemon's Hive-Auth envelope) by a device the owner admitted as a module's
(`admit --module`). Unlike the sync daemon this listener ignores `sync_auth_mode`: an unsigned request is
always 401. It binds 127.0.0.1 whatever `.peers.json` says and refuses a peer that is not loopback.
Per-module quotas and rate limits arrive with the write route (PR 5); until then only the daemon's
concurrency cap bounds it.
"""

import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import hive_sync_daemon as daemon
import merkle
import sync_common
import vocabulary

API_VERSIONS = ["v1"]
BIND = "127.0.0.1"
PORT_DEFAULT = 9886            # HIVE_MODULE_PORT overrides; the sync daemon's own port is 9876
FEED_LIMIT_DEFAULT, FEED_LIMIT_MAX = 200, 1000
SEARCH_LIMIT_MAX = 200
# What `api_search` may be asked. A5 splits two ways: search and item withhold the text of anything the owner
# forgot (`status=forgotten` is refused, and `include_forgotten=False` hides it in every branch), while
# `/v1/feed` is `hv feed` and carries the authoritative `forgotten` flag for the module to honour: hide the
# fact and never republish it.
_KINDS = ("all", "fact", "decision")
_SORTS = ("confidence", "recency", "importance", "utility")
_STATUSES = ("all", "contested", "volatile")


def is_loopback_peer(addr):
    """True iff `addr` (a client address) is on the loopback interface. The model is the sync daemon's
    `Handler._is_loopback`; kept here because that is a method on a handler this one does not extend."""
    return bool(addr) and (addr in ("127.0.0.1", "::1") or str(addr).startswith("127."))


class _Refused(Exception):
    def __init__(self, code, body):
        self.code, self.body = code, body


def _int(q, name, default, lo, hi):
    raw = q.get(name, [None])[0]
    if raw in (None, ""):
        return default
    try:
        n = int(raw)
    except ValueError:
        raise _Refused(400, {"error": f"{name} must be an integer"})
    return max(lo, min(hi, n))


def _choice(q, name, default, allowed):
    v = q.get(name, [default])[0]
    if v not in allowed:
        raise _Refused(400, {"error": f"{name} must be one of {', '.join(allowed)}", "got": v})
    return v


def _module_config(gov, module):
    prefix = vocabulary.MODULE_PREFIX + module + ":"
    return {k: v for k, v in sorted((gov.get("module_config") or {}).items()) if k.startswith(prefix)}


def route_root(ctx, q):
    hv, gov = daemon.hv, ctx["gov"]
    return {"api": API_VERSIONS, "contract": getattr(hv, "CONTRACT_VERSION", None), "hive_id": gov.get("hive_id", ""),
            "device_id": ctx["device_id"], "module": ctx["module"],
            "quota": None,                      # per-module use and limits arrive with the write route (PR 5)
            "config": _module_config(gov, ctx["module"])}


def route_feed(ctx, q):
    hv = daemon.hv
    try:
        after = hv._feed_cursor_parse(q.get("after", [""])[0])
    except ValueError as e:
        raise _Refused(400, {"error": str(e)})
    return hv.feed_page(after, _int(q, "limit", FEED_LIMIT_DEFAULT, 1, FEED_LIMIT_MAX))


def route_search(ctx, q):
    try:
        min_conf = float(q.get("min_confidence", ["0"])[0] or 0)
    except ValueError:
        raise _Refused(400, {"error": "min_confidence must be a number"})
    if not math.isfinite(min_conf) or min_conf < 0:
        raise _Refused(400, {"error": "min_confidence must be a finite number >= 0"})
    return daemon.hv.api_search(
        query=q.get("q", [""])[0], tag=q.get("tag", [None])[0] or None,
        kind=_choice(q, "kind", "all", _KINDS), min_confidence=min_conf,
        limit=_int(q, "limit", 50, 1, SEARCH_LIMIT_MAX), offset=_int(q, "offset", 0, 0, 10 ** 9),
        sort=_choice(q, "sort", "confidence", _SORTS), status=_choice(q, "status", "all", _STATUSES),
        include_forgotten=False)


def route_item(ctx, q):
    sid = q.get("id", [""])[0]
    if not sid:
        raise _Refused(400, {"error": "id is required"})
    out = daemon.hv.api_item(sid)
    if out.get("error"):
        raise _Refused(404, {"error": out["error"]})
    if (out.get("item") or {}).get("forgotten"):
        raise _Refused(404, {"error": f"{sid!r} is forgotten on this node"})
    return out


# The whole route table. A route is in this dict or it does not exist; `tests/test_module_api.py` pins it.
ROUTES = {"/v1/": route_root, "/v1/feed": route_feed, "/v1/search": route_search, "/v1/item": route_item}


class ModuleHandler(BaseHTTPRequestHandler):
    server_version = "hive-module/1"
    timeout = daemon.SOCKET_TIMEOUT

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self, u):
        """The caller as {gov, device_id, module}, or a _Refused. Signature, freshness, nonce and admission are
        the sync daemon's `_verify_sync_request`; on top of it the device must be a module's, which a hive with
        no owner yet cannot say, so it refuses everyone."""
        gov = daemon.hv._governance_state(merkle.read_all_entries(daemon.hv.JOURNAL_DIR))
        ok, reason, device = daemon._verify_sync_request(self.headers, "GET", u.path, u.query, b"", gov)
        if not ok:
            raise _Refused(403 if reason == "not-admitted" else 401, {"error": "authentication required", "detail": reason})
        if not gov.get("owner_id"):
            raise _Refused(403, {"error": "forbidden", "detail": "no-owner"})
        module = (gov.get("modules") or {}).get(device)
        if not module:
            raise _Refused(403, {"error": "forbidden", "detail": "not-a-module-device"})
        return {"gov": gov, "device_id": device, "module": module}

    def _handle(self):
        if not is_loopback_peer(self.client_address[0] if self.client_address else ""):
            return self._send(403, {"error": "the module API is local-only"})
        if not daemon._request_slots.acquire(blocking=False):
            return self._send(503, {"error": "server busy"})
        try:
            if self.command != "GET":
                return self._send(405, {"error": "read-only: no write route in this release"}, {"Allow": "GET"})
            u = urlparse(self.path)
            ctx = self._authenticate(u)
            route = ROUTES.get(u.path)
            if route is None:
                head = u.path.strip("/").split("/", 1)[0]
                if len(head) > 1 and head[0] == "v" and head[1:].isdigit() and head not in API_VERSIONS:
                    return self._send(404, {"error": f"unsupported API version {head!r}", "supported": API_VERSIONS})
                return self._send(404, {"error": "not found"})
            self._send(200, route(ctx, parse_qs(u.query, keep_blank_values=True)))
        except _Refused as r:
            self._send(r.code, r.body)
        except Exception as e:
            print(f"module api: {self.command} {self.path}: {e}", file=sys.stderr)
            self._send(500, {"error": "internal error"})
        finally:
            try:
                daemon._request_slots.release()
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


def make_module_server(port=None):
    """The listener, bound to loopback only. It does not use `sync_common.resolve_bind`, which can pick the
    tailnet address; 2.1 ships no remote opt-in. `port` 0 asks the OS for a free one (tests)."""
    if port is None:
        port = int(os.environ.get("HIVE_MODULE_PORT") or PORT_DEFAULT)
    srv = ThreadingHTTPServer((BIND, port), ModuleHandler)
    sync_common.clamp_mss(srv.socket)
    return srv


def start_module_api():
    """Start the listener on a daemon thread. Best effort: a taken port is reported and the sync daemon runs
    without it, because the module API must never be the reason sync does not come up. Returns the server or None."""
    try:
        srv = make_module_server()
    except (OSError, ValueError) as e:
        print(f"sync daemon: module API not started ({e})", file=sys.stderr)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"sync daemon: module API on {BIND}:{srv.server_address[1]}")
    return srv
