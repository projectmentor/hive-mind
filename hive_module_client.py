"""Hive Mind module client, the reference signer (2.1, plan PR 5; A2).

A module signs its own entries and its own requests; the core verifies, gates and appends them (plan finding 4).
So the entry format is a contract a module must be able to reproduce in any language, and this file is the
reference for it, readable top to bottom. It imports no `hv`, no `hive_sync_daemon` and nothing owner-side:
only the hash/signature primitives the core itself uses (`merkle._canonical`, `ed25519`) and the request
envelope's signing bytes (`sync_common.sync_signing_bytes`).

An entry is the object

    {"node_id": <device id>, "seq": <int>, "type": <type>, "timestamp": <ISO 8601>, "payload": {...},
     "prev_hash": <hash of this device's previous entry, or "sha256:genesis">, "pub": <b64 key>, "sig": <b64>}

  canonical(obj)  = JSON with sorted keys and separators (",", ":"), as UTF-8 bytes (`merkle._canonical`)
  device id       = "k1:" + the first 16 hex of sha256(pub)
  signature       = Ed25519 over canonical(entry without "sig"), so `pub` is covered
  entry hash      = "sha256:" + hex sha256(canonical(entry)), `sig` included; the next entry's `prev_hash`

A request is signed with the Hive-Auth envelope: Ed25519 over `sync_common.sync_signing_bytes` (alg, method,
path, canonical query, body hash, timestamp, nonce), sent as the `Hive-Auth-*` headers. `ModuleClient` is a
small `urllib` client over `/v1`; a module that is not Python reimplements these few lines.
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import ed25519
import merkle
import sync_common

GENESIS_PREV = "sha256:genesis"


def canonical(obj):
    return merkle._canonical(obj)


def device_id_for_pub(pub):
    return "k1:" + hashlib.sha256(pub).hexdigest()[:16]


def entry_hash(entry):
    return "sha256:" + hashlib.sha256(canonical(entry)).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sign_entry(entry, seed):
    """Attach `pub`, then `sig` over the entry without it. Returns the entry."""
    pub = ed25519.pub_from_seed(seed)
    entry["pub"] = base64.b64encode(pub).decode()
    entry["sig"] = base64.b64encode(ed25519.sign(canonical({k: v for k, v in entry.items() if k != "sig"}), seed)).decode()
    return entry


def build_entry(seed, entry_type, payload, tip=None, timestamp=None):
    """The next signed entry of the device that holds `seed`. `tip` is `GET /v1/tip`'s answer
    (`{"seq": n, "hash": h}`), or None for a device that has written nothing."""
    pub = ed25519.pub_from_seed(seed)
    seq, prev = (tip["seq"] + 1, tip["hash"]) if tip and tip.get("seq") else (1, GENESIS_PREV)
    return sign_entry({"node_id": device_id_for_pub(pub), "seq": seq, "type": entry_type,
                       "timestamp": timestamp or now_iso(), "payload": payload, "prev_hash": prev}, seed)


def signed_headers(seed, method, path, query="", body=b""):
    """The Hive-Auth-* headers for one request."""
    pub = ed25519.pub_from_seed(seed)
    ts, nonce = int(time.time()), os.urandom(16).hex()
    msg = sync_common.sync_signing_bytes(method, path, query, body or b"", ts, nonce)
    return {"Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG, "Hive-Auth-Device": device_id_for_pub(pub),
            "Hive-Auth-Pub": base64.b64encode(pub).decode(), "Hive-Auth-Ts": str(ts), "Hive-Auth-Nonce": nonce,
            "Hive-Auth-Sig": base64.b64encode(ed25519.sign(msg, seed)).decode()}


class ModuleClient:
    """`/v1` over loopback for the module that holds `seed` (its admitted device key)."""

    def __init__(self, seed, port=9886, host="127.0.0.1", timeout=10):
        self.seed, self.base, self.timeout = seed, f"http://{host}:{port}", timeout

    def request(self, method, path, query="", body=None):
        data = json.dumps(body).encode() if body is not None else b""
        headers = signed_headers(self.seed, method, path, query, data)
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path + (f"?{query}" if query else ""), data=data or None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def tip(self):
        return self.request("GET", "/v1/tip")[1]

    def write(self, entry_type, payload, timestamp=None):
        """Sign the next entry on the current tip and post it. On a 409 (another write took the seq) the answer
        carries the tip, so a caller may rebuild once and retry. Returns (status, body)."""
        entry = build_entry(self.seed, entry_type, payload, self.tip(), timestamp)
        return self.request("POST", "/v1/entries", body=entry)
