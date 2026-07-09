"""Wire tests for sync read-authentication (GHSA-242f-7fxg-f7wm).

Drive the REAL daemon over HTTP against an isolated HIVE_HOME. The daemon binds 0.0.0.0 so we can
exercise BOTH the loopback path (127.0.0.1 → trusted local operator) and the remote path (this host's
non-loopback egress IP → must authenticate). Remote assertions skip when the runner has no usable
non-loopback address.
"""

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
HV = PROJECT / "hv"
import ed25519  # noqa: E402
import sync_common  # noqa: E402

CANARY = "SECRET-CANARY-XYZZY"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _lan_ip():
    """This host's non-loopback egress IP, or None (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = None
    finally:
        s.close()
    if not ip or ip.startswith("127."):
        return None
    return ip


def _req(host, port, path, headers=None, method="GET", data=None, timeout=6):
    req = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers or {},
                                 method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _wait(port, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _req("127.0.0.1", port, "/sync/merkle-root")[0] == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise AssertionError("daemon did not start serving in time")


def _sign(home, method, path, query, body=b"", seed=None):
    """Hive-Auth-* headers signed with `home`'s device key (or a supplied seed = a different device)."""
    if seed is None:
        seed = base64.b64decode((Path(home) / ".device-key").read_text().strip())
    pub = ed25519.pub_from_seed(seed)
    ts = int(time.time())
    nonce = os.urandom(16).hex()
    msg = sync_common.sync_signing_bytes(method, path, query, body, ts, nonce)
    sig = ed25519.sign(msg, seed)
    return {
        "Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG,
        "Hive-Auth-Device": "k1:" + hashlib.sha256(pub).hexdigest()[:16],
        "Hive-Auth-Pub": base64.b64encode(pub).decode(),
        "Hive-Auth-Ts": str(ts),
        "Hive-Auth-Nonce": nonce,
        "Hive-Auth-Sig": base64.b64encode(sig).decode(),
    }


@pytest.fixture
def node(hive, monkeypatch):
    """Owner + admitted-self hive holding a secret canary, with a factory to start the daemon in a
    given sync_auth mode (bound to 0.0.0.0). Yields helpers; daemons are torn down at teardown."""
    monkeypatch.setenv("HIVE_OWNER_PASSPHRASE", "testpass")
    hive.run("key", "init")
    hive.run("owner", "init")
    dev = (hive.home / ".device-id").read_text().strip()
    hive.run("group", "admit", dev, "--principal", "me")
    hive.run("remember", CANARY, "--tags", "secret")
    procs = []

    def start(mode, bind="0.0.0.0"):
        port = _free_port()
        (hive.home / ".peers.json").write_text(json.dumps({"self": "t", "port": port, "peers": []}))
        env = dict(os.environ, HIVE_HOME=str(hive.home), HIVE_BIND=bind,
                   HIVE_SYNC_AUTH=mode, HIVE_OWNER_PASSPHRASE="testpass")
        p = subprocess.Popen([sys.executable, str(HV), "sync", "daemon"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(p)
        _wait(port)
        return port

    yield SimpleNamespace(start=start, home=hive.home, device_id=dev, lan=_lan_ip())
    for p in procs:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _need_remote(node):
    if not node.lan:
        pytest.skip("no non-loopback egress IP on this host — remote-path assertions skipped")
    return node.lan


def test_off_reproduces_disclosure(node):
    """MODE=off: an unauthenticated remote read returns the journal (the advisory, pre-fix behavior)."""
    lan = _need_remote(node)
    port = node.start("off")
    st, body = _req(lan, port, f"/sync/chunk?node={node.device_id}&start=1&end=9999")
    assert st == 200 and CANARY in body


def test_permissive_serves_unsigned_but_api_closed(node):
    """MODE=permissive: /sync/* still serves an old (unsigned) peer for backward-compat, but the
    /api/* disclosure surface is closed to anonymous remotes even here."""
    lan = _need_remote(node)
    port = node.start("permissive")
    st, body = _req(lan, port, f"/sync/chunk?node={node.device_id}&start=1&end=9999")
    assert st == 200 and CANARY in body                       # backward-compat sync
    st2, _ = _req(lan, port, "/api/overview")
    assert st2 == 403                                         # corpus API never for anonymous remote


def test_enforce_matrix(node):
    """MODE=enforce: one daemon, the full authorization matrix."""
    port = node.start("enforce")
    dev = node.device_id

    # loopback (local operator) is always trusted — journal + api both served
    st, body = _req("127.0.0.1", port, f"/sync/chunk?node={dev}&start=1&end=9999")
    assert st == 200 and CANARY in body
    assert _req("127.0.0.1", port, "/api/overview")[0] == 200

    # open discovery is reachable unauthenticated, and leaks no journal content
    for path in ("/hive/info", "/sync/merkle-root", "/api/verify"):
        assert _req("127.0.0.1", port, path)[0] == 200
    info = json.loads(_req("127.0.0.1", port, "/hive/info")[1])
    assert info["protocol_version"] == 2
    assert "advertised_addr" in info
    assert CANARY not in json.dumps(info)

    lan = _need_remote(node)
    # remote unsigned: journal blocked (401), api forbidden (403), discovery open (200)
    assert _req(lan, port, f"/sync/chunk?node={dev}&start=1&end=9999")[0] == 401
    assert _req(lan, port, "/api/overview")[0] == 403
    assert _req(lan, port, "/hive/info")[0] == 200

    # remote signed by the ADMITTED device → allowed
    hdrs = _sign(node.home, "GET", "/sync/chunk", f"node={dev}&start=1&end=9999")
    st, body = _req(lan, port, f"/sync/chunk?node={dev}&start=1&end=9999", headers=hdrs)
    assert st == 200 and CANARY in body

    # remote signed by a DIFFERENT (unadmitted) device → blocked
    hdrs2 = _sign(node.home, "GET", "/sync/chunk", f"node={dev}&start=1&end=9999", seed=os.urandom(32))
    assert _req(lan, port, f"/sync/chunk?node={dev}&start=1&end=9999", headers=hdrs2)[0] == 401


def test_dual_bind_serves_loopback(node):
    """Bound to a SPECIFIC non-loopback address (the tailnet-IP case), the daemon must ALSO serve
    127.0.0.1 so the local dashboard/hv stay reachable and trusted — otherwise binding the tailnet IP
    would break local access and force the operator through remote auth on their own node."""
    lan = _need_remote(node)
    port = node.start("enforce", bind=lan)          # primary bind = a specific non-loopback address
    dev = node.device_id
    # loopback is served AND trusted (dashboard + journal), even though the primary bind isn't loopback
    assert _req("127.0.0.1", port, "/api/overview")[0] == 200
    st, body = _req("127.0.0.1", port, f"/sync/chunk?node={dev}&start=1&end=9999")
    assert st == 200 and CANARY in body
    # the primary (non-loopback) address still enforces auth
    assert _req(lan, port, f"/sync/chunk?node={dev}&start=1&end=9999")[0] == 401
