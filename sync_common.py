"""
Shared helpers for the Phase 2 sync daemon and client.

Loads the `hv` CLI as an importable module (it's a valid Python file with no
.py extension) so the daemon/client can reuse its journal + persistence code
without shelling out, and loads the per-node peer registry.
"""

import importlib.machinery
import importlib.util
import json
import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT_DEFAULT = 9876

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
    then to a no-peers default."""
    for p in (hive_home() / ".peers.json", ROOT / ".peers.json"):
        if p.exists():
            cfg = json.loads(p.read_text())
            cfg.setdefault("self", socket.gethostname())
            cfg.setdefault("bind", "0.0.0.0")
            cfg.setdefault("port", PORT_DEFAULT)
            cfg.setdefault("peers", [])
            return cfg
    return {"self": socket.gethostname(), "bind": "0.0.0.0", "port": PORT_DEFAULT, "peers": []}
