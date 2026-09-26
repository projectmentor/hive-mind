"""
Shared helpers for the Phase 2 sync daemon and client.

Loads the `hv` CLI as an importable module (it's a valid Python file with no
.py extension) so the daemon/client can reuse its journal + persistence code
without shelling out, and loads the per-node peer registry.
"""

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent
PORT_DEFAULT = 9876

# ── sync read-authentication (GHSA-242f-7fxg-f7wm) ───────────────────────────────────────────────
# The sync READ surface (/sync/chunk, /sync/hello, /api/*) historically trusted the tailnet perimeter
# alone, so any reachable client could dump the journal. We now authenticate a remote read with a
# signed-request envelope: the client signs (method, path, canonical-query, body-hash, timestamp,
# nonce) with its Ed25519 DEVICE key; the daemon verifies the signature, that the signer is an
# ADMITTED device, and that the timestamp is fresh (replay guard). Reuses the existing device-key
# identity (hv._device_seed / hv._ed25519 / hv._device_id_for_pub) — no new crypto. Domain-separated
# by HIVE_AUTH_ALG so these signatures can never be confused with journal-entry / governance sigs.
HIVE_AUTH_ALG = "hive-sig-v1"
SYNC_AUTH_WINDOW = int(os.environ.get("HIVE_SYNC_AUTH_WINDOW", "300"))  # +/- wall-clock skew tolerated, seconds

# ── responder signatures (#107) ──────────────────────────────────────────────────────────────────
# The request envelope above proves who ASKS. A /sync/hello or /hive/info response proves who ANSWERED:
# when the caller sent a well-formed Hive-Auth-Nonce, the responder signs (the nonce, the path, its
# node_id / hive_id / advertised_addr / protocol_version, a digest of the whole response) with its
# device key and returns it as `hello_sig`. Domain-separated from request and entry signatures by
# HELLO_SIG_ALG; fresh by the caller's single-use nonce (no timestamp, so no clock-skew failure mode).
HELLO_SIG_ALG = "hive-hello-v1"
_NONCE_RE = re.compile(r"[0-9a-fA-F]{32,64}")

# ── path-MTU resilience (shared by the daemon + client) ──────────────────────────────────────
# Many tailnets ride an underlay whose effective path MTU is BELOW Tailscale's default 1280, which
# silently blackholes full-size packets: a small response (/sync/hello) passes, but a multi-KB
# /sync/chunk or /sync/ingest stalls until the read timeout and peers never converge. Clamping the
# outgoing TCP segment size keeps every segment under that ceiling, so sync works on the DEFAULT
# Tailscale MTU with NO per-node `ip link`/MTU tweaks. Override (or disable with 0) via
# HIVE_SYNC_MAXSEG.
SYNC_MAX_SEG = int(os.environ.get("HIVE_SYNC_MAXSEG", "1000"))


def _maxseg_supported():
    """True iff TCP_MAXSEG can actually be lowered on this platform. Linux/WSL/Android: yes.
    macOS (Darwin) REJECTS it with OSError [Errno 22] — and an unguarded setsockopt inside a
    urllib3 socket_option would then break EVERY sync connection there — so probe once and skip
    the clamp where it fails (pagination still carries sync)."""
    if SYNC_MAX_SEG <= 0 or not hasattr(socket, "TCP_MAXSEG"):
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, SYNC_MAX_SEG)
            return True
        finally:
            s.close()
    except OSError:
        return False


MAXSEG_OK = _maxseg_supported()


def clamp_mss(sock):
    """Best-effort: cap a socket's outgoing TCP segment size to SYNC_MAX_SEG. No-op where the
    platform can't lower it (see MAXSEG_OK) so callers never have to guard it."""
    if not MAXSEG_OK:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, SYNC_MAX_SEG)
    except OSError:
        pass


_hv = None


def load_hv():
    """Import ./hv as a module (cached). Honors $HIVE_HOME via hv's module-level
    globals, which read the environment at import time."""
    global _hv
    if _hv is None:
        loader = importlib.machinery.SourceFileLoader("hvmod", str(ROOT / "hv"))
        spec = importlib.util.spec_from_loader("hvmod", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _hv = mod
    return _hv


def hive_home():
    return Path(os.environ.get("HIVE_HOME", Path.home() / "projects" / "hive-mind"))


def peers_path():
    """The .peers.json that load_peers() reads: HIVE_HOME's, else the repo root's; None if neither
    exists. `hv doctor --fix` rewrites this same file (#7)."""
    for p in (hive_home() / ".peers.json", ROOT / ".peers.json"):
        if p.exists():
            return p
    return None


def load_peers():
    """Read .peers.json from HIVE_HOME (per-node), falling back to the repo root,
    then to a no-peers default. `bind` defaults to None (unset) — NOT "0.0.0.0" — so
    resolve_bind() can tell "operator chose all-interfaces" from "nobody set it"."""
    p = peers_path()
    if p is not None:
        cfg = json.loads(p.read_text())
        cfg.setdefault("self", socket.gethostname())
        cfg.setdefault("bind", None)
        cfg.setdefault("port", PORT_DEFAULT)
        cfg.setdefault("peers", [])
        return cfg
    return {"self": socket.gethostname(), "bind": None, "port": PORT_DEFAULT, "peers": []}


# ── sync request signing / bind resolution / auth-mode (GHSA-242f) ───────────────────────────────

def canonical_query(query):
    """Order-independent, encoding-normalized form of an HTTP query string. Both the client (from its
    params) and the daemon (from the received query) canonicalize the same way, so the signed bytes
    match regardless of param order or percent-encoding differences on the wire."""
    return urlencode(sorted(parse_qsl(query or "", keep_blank_values=True)))


def sync_signing_bytes(method, path, query, body_bytes, timestamp, nonce):
    """Canonical bytes an Ed25519 device signature covers for a sync request. The body is HASHED
    (not embedded) so the same helper covers the 32 MiB /sync/ingest cap. Newline-joined, with a
    domain-separation prefix that keeps these distinct from journal-entry/governance signatures."""
    body_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    parts = [HIVE_AUTH_ALG, str(method).upper(), path, canonical_query(query),
             "sha256:" + body_hash, str(int(timestamp)), str(nonce)]
    return "\n".join(parts).encode()


def sign_sync_request(method, path, query, body_bytes=b""):
    """Return the Hive-Auth-* header dict for an outbound sync request, signed with THIS device's
    Ed25519 key. Returns {} when the device has no key yet (pre-key-init) — a permissive/off server
    still accepts, so signing is always safe to attempt."""
    return signed_request_headers(load_hv()._device_seed(), method, path, query, body_bytes)


def signed_request_headers(seed, method, path, query, body_bytes=b""):
    """sign_sync_request with an explicit device `seed` ({} when it is None). hv's own probes pass their
    hive's seed, so a freshly loaded hv signs as the hive it runs for."""
    hv = load_hv()
    if seed is None or hv._ed25519 is None:
        return {}
    pub = hv._ed25519.pub_from_seed(seed)
    ts = int(time.time())
    nonce = os.urandom(16).hex()
    msg = sync_signing_bytes(method, path, query, body_bytes or b"", ts, nonce)
    sig = hv._ed25519.sign(msg, seed)
    return {
        "Hive-Auth-Alg": HIVE_AUTH_ALG,
        "Hive-Auth-Device": hv._device_id_for_pub(pub),
        "Hive-Auth-Pub": base64.b64encode(pub).decode(),
        "Hive-Auth-Ts": str(ts),
        "Hive-Auth-Nonce": nonce,
        "Hive-Auth-Sig": base64.b64encode(sig).decode(),
    }


def nonce_ok(nonce):
    """True iff `nonce` is a well-formed Hive-Auth-Nonce (32 to 64 hex characters), the only kind a
    responder signs over."""
    return isinstance(nonce, str) and bool(_NONCE_RE.fullmatch(nonce))


def hello_body_digest(body):
    """'sha256:' + the digest of the canonical JSON of a /sync/hello or /hive/info response without its
    `hello_sig`. Binds every field (chunks, genesis, …) into the responder's signature."""
    rest = {k: v for k, v in body.items() if k != "hello_sig"}
    return "sha256:" + hashlib.sha256(load_hv()._canonical(rest)).hexdigest()


def hello_signing_bytes(path, nonce, node_id, hive_id, advertised_addr, protocol_version, body_digest):
    """Canonical bytes a responder signature covers (#107): newline-joined like sync_signing_bytes, under
    its own domain tag. The path line keeps a /hive/info signature from standing in for a /sync/hello."""
    parts = [HELLO_SIG_ALG, str(path), str(nonce), str(node_id or ""), str(hive_id or ""),
             str(advertised_addr or ""), "" if protocol_version is None else str(protocol_version), body_digest]
    return "\n".join(parts).encode()


def sign_hello(body, nonce, path, seed, node_id):
    """The `hello_sig` for a /sync/hello or /hive/info response `body` asked for under `nonce`, or None
    when there is nothing to sign with: no well-formed nonce, no device key, or an identity that is not
    the key's (a HIVE_NODE_ID override or a legacy hostname node). Signs whether or not the caller's own
    envelope verified, so a caller this node does not yet admit still gets its proof."""
    hv = load_hv()
    if not nonce_ok(nonce) or seed is None or hv._ed25519 is None:
        return None
    pub = hv._ed25519.pub_from_seed(seed)
    device = hv._device_id_for_pub(pub)
    if device != node_id:
        return None
    wire = json.loads(json.dumps(body))            # digest what the caller will parse, not the Python dict
    msg = hello_signing_bytes(path, nonce, wire.get("node_id"), wire.get("hive_id"), wire.get("advertised_addr"),
                              wire.get("protocol_version"), hello_body_digest(wire))
    return {"alg": HELLO_SIG_ALG, "device": device, "pub": base64.b64encode(pub).decode(),
            "sig": base64.b64encode(hv._ed25519.sign(msg, seed)).decode()}


def addr_of(url_or_addr):
    """(host, port) of a base URL or a bare 'host:port', the host lower-cased; None when there is no host.
    A URL with no port takes its scheme's default. The daemon advertises an IPv6 bind unbracketed
    ('fd7a::1:9876'), so a bare address with several colons splits at the last one."""
    scheme, sep, rest = str(url_or_addr or "").strip().partition("://")
    if not sep:
        scheme, rest = "", scheme
    rest = rest.split("/", 1)[0].rsplit("@", 1)[-1]          # the netloc, without any userinfo
    if rest.startswith("["):
        host, _, p = rest[1:].partition("]")
        p = p[1:] if p.startswith(":") else ""
    elif ":" in rest:
        host, _, p = rest.rpartition(":")
    else:
        host, p = rest, ""
    if not host:
        return None
    return host.lower(), int(p) if p.isdigit() else {"http": 80, "https": 443}.get(scheme.lower())


def verify_hello(resp, nonce, path, contacted, gov, expected=None):
    """#107: what a /sync/hello or /hive/info response proves about who answered. `nonce` is the
    Hive-Auth-Nonce this node sent (None when it had no key to sign with), `path` the path it asked,
    `contacted` the base URL it asked, `gov` this node's governance state, `expected` the device the
    .peers.json entry names (a k1: id), if any. Returns {"outcome", "reason", "device", "other_device"}:
      verified       a valid signature over our nonce and path, by the device the response names, of
                     our hive (or no hive on one side), admitted and not purged once an owner exists,
                     advertising the host:port we contacted
      addr-unproven  as verified, except advertised_addr is missing or not the host:port we contacted
      unadmitted     identity proven; an owner exists and the device is not admitted
      purged         identity proven; the device is purged
      unsigned       no hello_sig (a pre-v3 peer, a legacy identity), or we sent no nonce
      invalid        anything else: a bad signature, a fingerprint or node_id mismatch, another nonce (a
                     replay) or path, a foreign key, another hive
    `other_device` is True when `expected` names a device other than the one that proved itself."""
    out = {"outcome": "invalid", "reason": "", "device": None, "other_device": False}
    hs = resp.get("hello_sig") if isinstance(resp, dict) else None
    if not nonce_ok(nonce) or hs is None:
        out.update(outcome="unsigned", reason="no-nonce-sent" if not nonce_ok(nonce) else "no-hello-sig")
        return out
    hv = load_hv()
    try:
        device, pub, sig = hs["device"], base64.b64decode(hs["pub"]), base64.b64decode(hs["sig"])
        alg = hs["alg"]
    except Exception:
        out["reason"] = "malformed-hello-sig"
        return out
    if alg != HELLO_SIG_ALG:
        out["reason"] = "bad-alg"
    elif len(pub) != 32 or hv._device_id_for_pub(pub) != device:
        out["reason"] = "pub-fingerprint-mismatch"
    elif device != resp.get("node_id"):
        out["reason"] = "node-id-mismatch"
    elif hv._ed25519 is None:
        out["reason"] = "no-verifier"
    if out["reason"]:
        return out
    msg = hello_signing_bytes(path, nonce, resp.get("node_id"), resp.get("hive_id"), resp.get("advertised_addr"),
                              resp.get("protocol_version"), hello_body_digest(resp))
    if not hv._ed25519.verify(msg, sig, pub):
        out["reason"] = "bad-signature"                # also another nonce (a replay) or another path
        return out
    out["device"] = device
    local_hive, peer_hive = gov.get("hive_id") or "", resp.get("hive_id") or ""
    if local_hive and peer_hive and local_hive != peer_hive:
        out["reason"] = "different-hive"
        return out
    out["other_device"] = bool(expected) and expected != device
    advertised = addr_of(resp.get("advertised_addr"))
    if gov.get("owner_id") and device in (gov.get("purged") or ()):
        out.update(outcome="purged", reason="purged")
    elif gov.get("owner_id") and device not in (gov.get("admitted") or ()):
        out.update(outcome="unadmitted", reason="not-admitted")
    elif advertised is None or advertised != addr_of(contacted):
        out.update(outcome="addr-unproven", reason=f"advertises {resp.get('advertised_addr') or 'no address'}")
    else:
        out.update(outcome="verified", reason="ok")
    return out


def outbound_action(mode, outcome):
    """#107: what a sync round does with a peer whose hello came back `outcome`, under outbound `mode`:
    "push" (pull and push), "pull" (pull only: pulled entries are still checked one by one on append,
    and pulling is how an unadmitted responder's admission reaches us) or "skip". Only `enforce` ever
    withholds anything; `permissive` flags and `off` does not look."""
    if mode != "enforce" or outcome == "verified":
        return "push"
    return "skip" if outcome in ("purged", "invalid") else "pull"


def _is_tailnet_ip(ip):
    """True iff `ip` is a Tailscale CGNAT address (100.64.0.0/10). Tighter than a bare '100.' match,
    which would also accept public 100.0-63.x / 100.128-255.x addresses."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        octs = [int(p) for p in parts]
    except ValueError:
        return False
    return octs[0] == 100 and 64 <= octs[1] <= 127 and all(0 <= o <= 255 for o in octs)


def _locally_bindable(ip):
    """True iff `ip` is assigned to a local interface (we can bind it). This is the WSL2 safety check:
    a `tailscale` that resolves to the Windows host's tailscale.exe (WSL interop, no native
    tailscaled) reports the WINDOWS node's tailnet IP, which is NOT on any WSL interface — binding it
    would fail. Rejecting an unbindable candidate makes us fall back to loopback instead of crashing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def tailscale_ip():
    """This node's OWN tailnet IPv4 (100.64.0.0/10), verified to be a LOCAL, bindable address — or
    None if Tailscale is absent/logged-out/unreachable, or the only address reported isn't local to
    THIS host (the WSL2-interop case above). Best-effort, short timeout, never raises."""
    candidates = []
    for cmd in (["tailscale", "ip", "-4"], ["tailscale", "ip"], ["/usr/bin/tailscale", "ip", "-4"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        candidates = [ln.strip() for ln in r.stdout.splitlines() if _is_tailnet_ip(ln.strip())]
        if candidates:
            break
    for ip in candidates:
        if _locally_bindable(ip):
            return ip
    return None


def bind_is_auto(cfg):
    """True when the daemon's bind is chosen automatically: no HIVE_BIND override and no specific
    `.peers.json` bind. A legacy '0.0.0.0'/'::'/'' counts as automatic, exactly as resolve_bind treats
    it. Only an automatic bind is ever re-evaluated while the daemon runs (#47)."""
    if os.environ.get("HIVE_BIND"):
        return False
    explicit = cfg.get("bind")
    return not (explicit and explicit not in ("0.0.0.0", "::", ""))


def is_loopback(addr):
    return addr in ("127.0.0.1", "::1", "localhost") or str(addr).startswith("127.")


def should_rebind(bound, resolved, auto):
    """#47: whether a running daemon bound to `bound` should restart to bind `resolved` (what
    resolve_bind answers now). Only an automatic bind moves, and only TOWARD a real address: loopback
    to the tailnet IP (it started before tailscaled), or tailnet A to tailnet B (the IP changed). Never
    toward loopback: if tailscale drops, the daemon keeps its tailnet bind rather than flapping, which
    would recreate the bug in reverse."""
    if not auto or not resolved or is_loopback(resolved):
        return False
    return resolved != bound


def resolve_bind(cfg):
    """The address the daemon should bind, in priority order:
       1. HIVE_BIND env override (any value, incl. '0.0.0.0' for operators who really want it — the
          only way to deliberately request all-interfaces).
       2. cfg['bind'] from .peers.json IF it's a SPECIFIC address. A legacy '0.0.0.0'/'::'/'' (what the
          old installer wrote) is treated as "auto", so existing nodes auto-harden on restart.
       3. the node's own, locally-bindable Tailscale IP if found.
       4. '127.0.0.1' (loopback-only; the safe single-node / no-tailnet / WSL-interop default).
    Binding the tailnet IP (not 0.0.0.0) is what makes loopback-trust in the daemon sound."""
    env = os.environ.get("HIVE_BIND")
    if env:
        return env
    if not bind_is_auto(cfg):
        return cfg["bind"]
    ip = tailscale_ip()
    if ip:
        return ip
    return "127.0.0.1"


def sync_auth_mode(cfg=None):
    """Read-auth enforcement mode for THIS node (local, not journaled — each node flips independently
    during a phased rollout). Priority: HIVE_SYNC_AUTH env → .peers.json 'sync_auth' → 'permissive'.
      off        — never require auth (pre-rollout / escape hatch).
      permissive — serve gated reads even if unsigned/invalid, but flag them (default).
      enforce    — gated reads require a valid signed envelope from an admitted device."""
    env = os.environ.get("HIVE_SYNC_AUTH", "").strip().lower()
    if env in ("off", "permissive", "enforce"):
        return env
    if cfg is None:
        cfg = load_peers()
    mode = str(cfg.get("sync_auth", "") or "").strip().lower()
    return mode if mode in ("off", "permissive", "enforce") else "permissive"


def sync_auth_outbound_mode(cfg=None):
    """#107: what THIS node requires of a peer's signed hello before it PUSHES to it (local, not
    journaled; separate from sync_auth, because inbound enforce needs every peer at protocol 2 and
    outbound enforce needs protocol 3). Priority: HIVE_SYNC_AUTH_OUTBOUND env → .peers.json
    'sync_auth_outbound' → 'permissive'. See outbound_action.
      off        — never check a hello.
      permissive — check and flag, never change what a round does (default).
      enforce    — push only to a verified peer; pull only from an unsigned, addr-unproven or unadmitted
                   one; skip a purged or invalid one."""
    env = os.environ.get("HIVE_SYNC_AUTH_OUTBOUND", "").strip().lower()
    if env in ("off", "permissive", "enforce"):
        return env
    if cfg is None:
        cfg = load_peers()
    mode = str(cfg.get("sync_auth_outbound", "") or "").strip().lower()
    return mode if mode in ("off", "permissive", "enforce") else "permissive"
