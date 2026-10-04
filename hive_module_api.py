"""Hive Mind module API, `/v1` (2.1: the read routes of plan PR 4, the write route of PR 5).

A second, loopback-only HTTP listener for modules: separate processes that read the hive and write to it
through the core. It is data plane only. It imports no owner-key code (the S2 boundary,
`tests/test_ownerkey_boundary.py`) and serves no governance, owner, escrow, capsule, cell or comb state.

  GET  /v1/         -> {api, contract, hive_id, device_id, module, quota, config}
  GET  /v1/feed     -> `hv feed`: ?after=<node_id:seq,...>&limit=
  GET  /v1/search   -> `api_search`: ?q=&tag=&kind=&min_confidence=&limit=&offset=&sort=&status=
  GET  /v1/item     -> `api_item`: ?id=<h:... or node_id:seq>
  GET  /v1/tip      -> {node_id, seq, hash}: the caller's own chain tip (seq 0 and `sha256:genesis` before its first write)
  POST /v1/entries  -> append one entry the module signed itself (see `route_entries`)

Authentication runs before the route lookup, so an unsigned GET, or a POST with a Content-Length, to any path (even an unknown one) is
401; the 404s below exist only for a signed module. Every other verb answers a JSON 405 before authentication,
and a signed GET or POST on the wrong route answers 405 with `Allow`. `item` answers 404 for anything that does
not resolve (`h:nope`, a local id, a forgotten fact).

`config` in `/v1/` is the caller's own `x-<module>:` keys, a convenience and not a confidentiality boundary:
the journal is shared by design and `/v1/feed` carries all of it, other modules' `set-config` entries included.

Every request is signed (the sync daemon's Hive-Auth envelope) by a device the owner admitted as a module's
(`admit --module`). Unlike the sync daemon this listener ignores `sync_auth_mode`: an unsigned request is
always 401. It binds 127.0.0.1 whatever `.peers.json` says and refuses a peer that is not loopback.

The listener has its own concurrency cap (`MODULE_MAX_CONCURRENT`), apart from the sync daemon's
`_request_slots`: a quota bounds accepted writes, not reads or refused requests, so without a cap of its own a
module could fill the daemon's 32 slots and push sync peers into 503. Per-module quotas and rate limits bound
what a module may journal (entries are permanent): see `QUOTA_DEFAULTS`, and `route_entries`.
"""

import json
import math
import os
import sys
import tempfile
import threading
import time
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
MODULE_MAX_CONCURRENT = 8      # in-flight handlers on this listener; excess is 503. The sync daemon's own cap is untouched
_module_slots = threading.BoundedSemaphore(MODULE_MAX_CONCURRENT)
# What a module device may journal (plan A4, §8 item 8), from this hive's own journal. A module's manifest (PR 6)
# may ask for less and only the owner raises a limit, both through `limits_for`.
QUOTA_DEFAULTS = {"per_hour": 60, "per_day": 500, "entry_bytes": 16 * 1024, "lifetime": 50000}
HOUR, DAY = 3600, 86400
QUOTA_FILE = ".module-quota.json"
# The entry types a module may write (plan A3). Rule 4 only forbids adding types; this allow-list is the plan's choice.
# `governance`, `capsule`, `cell`, `comb` and `retract` are refused: a retract from a module would be peer evidence
# in the module's name. `tests/test_module_api.py` holds the two lists to `vocabulary.ENTRY_TYPES`.
MODULE_ENTRY_TYPES = ("fact", "decision", "idea", "entity", "link")
ENTRY_FIELDS = ("node_id", "seq", "type", "timestamp", "payload", "prev_hash", "pub", "sig")
# What `api_search` may be asked. A5 splits two ways: search and item withhold the text of anything the owner
# forgot (`status=forgotten` is refused, and `include_forgotten=False` hides it in every branch), while
# `/v1/feed` is `hv feed` and carries the authoritative `forgotten` flag for the module to honour: hide the
# fact and never republish it.
_KINDS = ("all", "fact", "decision", "idea")   # idea: the feed already hands a module every idea
_SORTS = ("confidence", "recency", "importance", "utility")
_STATUSES = ("all", "contested", "volatile")


def is_loopback_peer(addr):
    """True iff `addr` (a client address) is on the loopback interface. The model is the sync daemon's
    `Handler._is_loopback`; kept here because that is a method on a handler this one does not extend."""
    return bool(addr) and (addr in ("127.0.0.1", "::1") or str(addr).startswith("127."))


class _Refused(Exception):
    def __init__(self, code, body, headers=None):
        self.code, self.body, self.headers = code, body, headers


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


def limits_for(module):
    """The limits that bind `module`: the defaults today. A manifest may lower them and the owner may raise them
    (PR 6, `hive-mind module quota`); both land here so no caller reads a second table."""
    return dict(QUOTA_DEFAULTS)


def _chain(device_id):
    """(entries by seq, tip seq, tip hash) of one device's chain, read from the journal."""
    hv = daemon.hv
    mine = {e["seq"]: e for e in merkle.read_all_entries(hv.JOURNAL_DIR)
            if e.get("node_id") == device_id and isinstance(e.get("seq"), int)}
    if not mine:
        return mine, 0, "sha256:genesis"
    top = max(mine)
    return mine, top, hv.compute_hash(mine[top])


def _quota_path():
    hv = daemon.hv
    return hv.HIVE_HOME / ".module-quota.json"


def _quota_times(device_id, now):
    """This device's accepted-write times in the last day, from the local window file. Only the hourly and daily
    windows live there; a missing, unreadable or hand-edited file reads as empty, which can only loosen a
    window of at most a day, never the lifetime ceiling."""
    try:
        raw = json.loads(_quota_path().read_text()).get(device_id, [])
        return sorted(t for t in raw if isinstance(t, (int, float)) and not isinstance(t, bool) and now - DAY < t <= now)
    except (OSError, ValueError, AttributeError):
        return []


def _quota_record(device_id, now):
    """Add one accepted write at `now` to the window file (0600, replaced atomically, other devices' times kept)."""
    path = _quota_path()
    try:
        data = json.loads(path.read_text())
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data = {d: [t for t in ts if isinstance(t, (int, float)) and not isinstance(t, bool) and now - DAY < t <= now]
            for d, ts in data.items() if isinstance(ts, list)}
    data[device_id] = _quota_times(device_id, now) + [now]
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=QUOTA_FILE + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def quota_state(device_id, module, now=None, chain=None):
    """{limits, used, remaining}: what `module` may still journal. `lifetime` is counted from the journal itself
    (`journal_keys`, filtered to this device), which is permanent, so a deleted window file, a rebuild or a
    restart cannot reset it; `per_hour` and `per_day` come from the local window file."""
    now = time.time() if now is None else now
    limits = limits_for(module)
    times = _quota_times(device_id, now)
    used = {"per_hour": sum(1 for t in times if t > now - HOUR), "per_day": len(times),
            "lifetime": sum(1 for n, _ in daemon.hv.journal_keys() if n == device_id)}
    return {"limits": limits, "used": used, "remaining": {k: max(0, limits[k] - used[k]) for k in used}}, times


def _over_quota(state, times, now):
    """None if one more entry fits, else (which limit, Retry-After seconds or None for a ceiling no wait lifts)."""
    lim, used = state["limits"], state["used"]
    if used["lifetime"] >= lim["lifetime"]:
        return "lifetime", None
    for window in ("per_hour", "per_day"):
        if lim[window] <= 0:                        # a manifest may ask for less, and 0 is less: nothing fits, no wait helps
            return window, None
    waits = []
    if used["per_hour"] >= lim["per_hour"]:
        waits.append(("per_hour", [t for t in times if t > now - HOUR][-lim["per_hour"]] + HOUR - now))
    if used["per_day"] >= lim["per_day"]:
        waits.append(("per_day", times[-lim["per_day"]] + DAY - now))
    return max(waits, key=lambda w: w[1]) if waits else None


def route_root(ctx, q):
    hv, gov = daemon.hv, ctx["gov"]
    return {"api": API_VERSIONS, "contract": getattr(hv, "CONTRACT_VERSION", None), "hive_id": gov.get("hive_id", ""),
            "device_id": ctx["device_id"], "module": ctx["module"],
            "quota": quota_state(ctx["device_id"], ctx["module"])[0],
            "config": _module_config(gov, ctx["module"])}


def route_tip(ctx, q):
    _, seq, tip_hash = _chain(ctx["device_id"])
    return {"node_id": ctx["device_id"], "seq": seq, "hash": tip_hash}


def _is_sid(ref):
    return isinstance(ref, str) and bool(daemon.hv._SID_RE.match(ref))


def _refused(code, why, **extra):
    return _Refused(code, dict({"error": why}, **extra))


def _check_names(module, payload):
    """Every name the payload introduces is a core name a module may use or its own `x-<module>:` name (plan A4,
    reserved names): the source app and class, each tag, a link's kind."""
    hv = daemon.hv
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        raise _refused(400, "payload.source is required: a module's source is `x-<module>`")
    app, ctx, _inst, _sess = hv._parse_source(source)
    why = vocabulary.check_module_name(module, "source_apps", app)
    if why is None and ":" in source:
        why = vocabulary.check_module_name(module, "source_contexts", ctx)
    if why:
        raise _refused(403, why, field="source")
    if "channel" in payload and payload["channel"] not in vocabulary.CHANNEL_NAMES:
        raise _refused(400, "payload.channel must be one of " + ", ".join(vocabulary.CHANNEL_NAMES), got=payload["channel"])
    tags = payload.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise _refused(400, "payload.tags must be a list of strings")
    for t in tags:
        why = vocabulary.check_module_name(module, "behaviour_tags", t)
        if why:
            raise _refused(403, why, field="tags")


def _check_link(module, payload):
    """A link's kind is a core kind or `x-<module>:…`. A core kind carries `from_ref`/`to_ref` pairs, the fields
    `resolve_link` reads. A module's own kind carries the envelope's `from`/`to`, each a pair or an `h:` short id
    (`_resolve_ref(..., short_ids=True)`). An end that resolves to nothing yet is allowed: the target may sync
    later, and a projection skips a dangling edge deterministically."""
    hv = daemon.hv
    kind = payload.get("kind")
    why = vocabulary.check_module_name(module, "link_kinds", kind) if isinstance(kind, str) else "payload.kind is required"
    if why:
        raise _refused(403 if isinstance(kind, str) else 400, why, field="kind")
    pair_only = kind in vocabulary.LINK_KINDS
    for field in (("from_ref", "to_ref") if pair_only else ("from", "to")):
        ref = payload.get(field)
        if not (hv._valid_ref(ref) or (not pair_only and _is_sid(ref))):
            raise _refused(400, f"payload.{field} must be a [node_id, seq] pair" + ("" if pair_only else " or an `h:` short id"))


# Payload fields that name a local row id or a pre-1.19 reference. `rebuild_db` still projects them from an old
# journal, with no authority check, so a module may not carry them. The table is read from `REF_FIELDS`; the
# names listed are the ones the plan named, kept so a status change in the table cannot drop them.
_LEGACY_REF_NAMES = ("supersedes_ref", "supersedes", "resolves_ref", "entity_ref", "fact_ref", "entity_id", "fact_id")


def _legacy_ref_fields():
    return sorted(set(_LEGACY_REF_NAMES) | {k for k, r in vocabulary.REF_FIELDS.items() if r["status"] == vocabulary.LEGACY})


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and (not isinstance(v, int) or -2 ** 63 <= v < 2 ** 63)


# field -> (what it must be, test); each is "absent or ..." and the projection binds it straight into SQLite
_BOUND_FIELDS = {
    "importance": ("a number", _is_number), "confidence": ("a number", _is_number),
    "source_session": ("a string", lambda v: isinstance(v, str)), "created_at": ("a string", lambda v: isinstance(v, str)),
    "rationale": ("a string", lambda v: isinstance(v, str)),
    "access_count": ("an integer", lambda v: isinstance(v, int) and not isinstance(v, bool) and -2 ** 63 <= v < 2 ** 63),
    "attributes": ("an object", lambda v: isinstance(v, dict)),
}


def _check_fields(etype, payload):
    """Refuse a legacy ref field, and a field the projection binds whose type SQLite cannot take (plan A4: a signed
    payload must not make `rebuild_db` raise). `type` is checked only on an entity, where it is the entity's type."""
    legacy = [k for k in _legacy_ref_fields() if k in payload]
    if legacy:
        raise _refused(400, "a module may not carry a legacy reference field; use a link entry", fields=legacy)
    for k, (want, ok) in _BOUND_FIELDS.items():
        if k in payload and not ok(payload[k]):
            raise _refused(400, f"payload.{k} must be {want} or absent")
    if etype == "entity" and "type" in payload and not isinstance(payload["type"], str):
        raise _refused(400, "payload.type must be a string or absent")


def _dry_run(entry):
    """Project `entry` into the live store inside a SAVEPOINT and roll it back; the backstop for any field the
    checks above do not know. Raises _Refused(400) when the core's own projection raises on it."""
    conn = daemon.hv.get_conn()
    try:
        conn.execute("SAVEPOINT module_dry_run")
        try:
            daemon.hv.persist_entry(conn, entry)
        finally:
            conn.execute("ROLLBACK TO module_dry_run")
            conn.rollback()
    except Exception as e:
        raise _refused(400, "the core cannot project this payload", detail=f"{type(e).__name__}: {e}")
    finally:
        conn.close()


def _check_entry(ctx, entry, limits):
    """The gate on a module-signed entry, before anything is written. Every refusal is a 4xx and writes nothing."""
    hv = daemon.hv
    if not isinstance(entry, dict):
        raise _refused(400, "the body must be one signed entry (a JSON object)")
    extra = sorted(set(entry) - set(ENTRY_FIELDS))
    missing = [k for k in ENTRY_FIELDS if k not in entry]
    if extra or missing:
        raise _refused(400, "an entry is exactly " + ", ".join(ENTRY_FIELDS), unknown=extra, missing=missing)
    seq, payload = entry["seq"], entry["payload"]
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 1 <= seq < 2 ** 63) or not isinstance(payload, dict) \
            or not all(isinstance(entry[k], str) for k in ("node_id", "type", "timestamp", "prev_hash", "pub", "sig")):
        raise _refused(400, "malformed entry: seq is an integer from 1, payload an object, the rest strings")
    if entry["node_id"] != ctx["device_id"]:
        raise _refused(403, "an entry is signed by the device that sends it", node_id=entry["node_id"], device_id=ctx["device_id"])
    if entry["type"] not in MODULE_ENTRY_TYPES:
        raise _refused(403, f"a module may write {', '.join(MODULE_ENTRY_TYPES)}, not {entry['type']!r}", field="type")
    if not hv._valid_timestamp(entry["timestamp"]):
        raise _refused(400, "timestamp must be an ISO 8601 time")
    if not hv._verify_entry(entry) or hv._ed25519 is None:
        raise _refused(400, "the signature does not verify for this entry and key")
    _check_names(ctx["module"], payload)
    t = entry["type"]
    _check_fields(t, payload)
    if t in ("fact", "decision", "idea") and not (isinstance(payload.get("content"), str) and payload["content"]):
        raise _refused(400, f"a {t} needs payload.content, a non-empty string")
    if t == "entity" and not (isinstance(payload.get("name"), str) and payload["name"]):
        raise _refused(400, "an entity needs payload.name, a non-empty string")
    if t == "link":
        _check_link(ctx["module"], payload)


def route_entries(ctx, q, body):
    """Append one entry the module signed itself (plan A2, A3, A4, A6). The core verifies, gates and appends it
    through `append_foreign_entries`; it never signs for a module, so the module's key stays its own.

    In order: the entry's shape, signer, type, names, size; then, inside one `_ingest_lock` section, the chain
    (the same bytes at a held seq is 200 and idempotent, a different body there or a stale `prev_hash` is 409
    carrying the tip), the quota (429, nothing written), the append, and the projection. 200 is
    `{accepted, duplicate, ref, tip}`."""
    hv = daemon.hv
    device, now = ctx["device_id"], time.time()
    limits = limits_for(ctx["module"])
    if len(body) > limits["entry_bytes"]:
        raise _refused(413, "entry too large", max_bytes=limits["entry_bytes"])
    try:
        entry = json.loads(body)
    except ValueError:
        raise _refused(400, "the body is not JSON")
    _check_entry(ctx, entry, limits)
    seq = entry["seq"]
    with daemon._ingest_lock:
        mine, top, tip_hash = _chain(device)
        tip = {"node_id": device, "seq": top, "hash": tip_hash}
        if seq in mine:
            if hv.compute_hash(mine[seq]) == hv.compute_hash(entry):
                return {"accepted": 0, "duplicate": True, "ref": f"{device}:{seq}", "tip": tip}
            raise _refused(409, "a different entry is already held at this seq", tip=tip)
        if seq != top + 1 or entry["prev_hash"] != tip_hash:
            raise _refused(409, "stale tip: the entry does not extend the chain", tip=tip)
        state, times = quota_state(device, ctx["module"], now)
        over = _over_quota(state, times, now)
        if over:
            wait = None if over[1] is None else max(1, math.ceil(over[1]))
            raise _Refused(429, {"error": f"over quota: {over[0]}", "limit": over[0], "quota": state,
                                 "retry_after": wait}, {"Retry-After": str(wait)} if wait else None)
        _dry_run(entry)
        accepted, _dup = hv.append_foreign_entries([entry])
        if accepted != 1:
            raise _refused(403, "the core did not accept the entry (admission, signature or timestamp)")
        try:
            _quota_record(device, now)
        except OSError as e:
            print(f"module api: quota window not saved ({e})", file=sys.stderr)
        hv.rebuild_db()
    return {"accepted": 1, "duplicate": False, "ref": f"{device}:{seq}", "sid": hv._short_id(device, seq),
            "tip": {"node_id": device, "seq": seq, "hash": hv.compute_hash(entry)}}


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
ROUTES = {"/v1/": route_root, "/v1/feed": route_feed, "/v1/search": route_search, "/v1/item": route_item,
          "/v1/tip": route_tip, "/v1/entries": route_entries}
WRITE_ROUTES = ("/v1/entries",)             # the POST routes; every other route is GET


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

    def _authenticate(self, u, body=b""):
        """The caller as {gov, device_id, module}, or a _Refused. Signature, freshness, nonce and admission are
        the sync daemon's `_verify_sync_request`, over this request's method and exact body; on top of it the
        device must be a module's, which a hive with no owner yet cannot say, so it refuses everyone."""
        gov = daemon.hv._governance_state(merkle.read_all_entries(daemon.hv.JOURNAL_DIR))
        ok, reason, device = daemon._verify_sync_request(self.headers, self.command, u.path, u.query, body, gov)
        if not ok:
            raise _Refused(403 if reason == "not-admitted" else 401, {"error": "authentication required", "detail": reason})
        if not gov.get("owner_id"):       # defence in depth: an owner signs every module admit, so `modules` is empty without one
            raise _Refused(403, {"error": "forbidden", "detail": "no-owner"})
        module = (gov.get("modules") or {}).get(device)
        if not module:
            raise _Refused(403, {"error": "forbidden", "detail": "not-a-module-device"})
        return {"gov": gov, "device_id": device, "module": module}

    def send_error(self, code, message=None, explain=None):
        """The stdlib answers a verb with no `do_<VERB>` (and a malformed request) with an HTML page; the
        contract is JSON, so any such answer is JSON, and an unknown verb is the same 405 as any other."""
        if code == 501:
            return self._send(405, {"error": "the module API takes GET and POST only"}, {"Allow": "GET, POST"})
        self._send(code, {"error": message or "bad request"})

    def _read_body(self):
        """A POST's body, bounded before it is read: a missing or garbage Content-Length is 400, one past the
        largest entry any module may send (`QUOTA_DEFAULTS`: a manifest may only lower it) is 413."""
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _Refused(400, {"error": "Content-Length is required"})
        cap = QUOTA_DEFAULTS["entry_bytes"]
        if n < 0 or n > cap:
            raise _Refused(413, {"error": "entry too large", "max_bytes": cap})
        return self.rfile.read(n) if n else b""

    def _handle(self):
        if not is_loopback_peer(self.client_address[0] if self.client_address else ""):
            return self._send(403, {"error": "the module API is local-only"})
        if not _module_slots.acquire(blocking=False):
            return self._send(503, {"error": "server busy"})
        try:
            if self.command not in ("GET", "POST"):
                return self._send(405, {"error": "the module API takes GET and POST only"}, {"Allow": "GET, POST"})
            u = urlparse(self.path)
            body = self._read_body() if self.command == "POST" else b""
            ctx = self._authenticate(u, body)
            route = ROUTES.get(u.path)
            if route is None:
                head = u.path.strip("/").split("/", 1)[0]
                if len(head) > 1 and head[0] == "v" and head[1:].isdigit() and head not in API_VERSIONS:
                    return self._send(404, {"error": f"unsupported API version {head!r}", "supported": API_VERSIONS})
                return self._send(404, {"error": "not found"})
            allowed = "POST" if u.path in WRITE_ROUTES else "GET"
            if self.command != allowed:
                return self._send(405, {"error": f"{u.path} takes {allowed}"}, {"Allow": allowed})
            q = parse_qs(u.query, keep_blank_values=True)
            self._send(200, route(ctx, q, body) if allowed == "POST" else route(ctx, q))
        except _Refused as r:
            self._send(r.code, r.body, r.headers)
        except Exception as e:
            print(f"module api: {self.command} {self.path}: {e}", file=sys.stderr)
            self._send(500, {"error": "internal error"})
        finally:
            try:
                _module_slots.release()
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _handle


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
