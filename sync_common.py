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


def load_peers():
    """Read .peers.json from HIVE_HOME (per-node), falling back to the repo root,
    then to a no-peers default. `bind` defaults to None (unset) — NOT "0.0.0.0" — so
    resolve_bind() can tell "operator chose all-interfaces" from "nobody set it"."""
    for p in (hive_home() / ".peers.json", ROOT / ".peers.json"):
        if p.exists():
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
    hv = load_hv()
    seed = hv._device_seed()
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
    explicit = cfg.get("bind")
    if explicit and explicit not in ("0.0.0.0", "::", ""):
        return explicit
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
