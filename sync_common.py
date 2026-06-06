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
