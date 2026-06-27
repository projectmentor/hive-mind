"""Phase 2 sync test — runs the local two-node convergence harness under pytest.

The heavy lifting lives in scripts/common/sync_smoke.sh (spins two daemons on
127.0.0.1, seeds distinct data, syncs, asserts convergence + dedup + cross-node
link resolution). This wrapper makes it part of `pytest`. Requires curl.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SMOKE = PROJECT / "scripts" / "common" / "sync_smoke.sh"
HV = PROJECT / "hv"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_two_node_convergence():
    r = subprocess.run([str(SMOKE)], capture_output=True, text=True)
    assert r.returncode == 0, f"sync_smoke.sh failed:\n{r.stdout}\n{r.stderr}"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_two_node_convergence_under_aggressive_pagination():
    # Path-MTU resilience: force 1-entry PULL/PUSH pages and a tiny TCP MSS clamp. These change only
    # the transport granularity (smaller requests, smaller segments) — the G-Set union result must be
    # byte-identical, so the same harness must still converge. Guards against pagination or the clamp
    # accidentally dropping/duplicating entries. Runs on macos-latest too (where the clamp no-ops and
    # pagination alone carries it).
    env = dict(os.environ, HIVE_SYNC_PULL_PAGE="1", HIVE_SYNC_PUSH_PAGE="1", HIVE_SYNC_MAXSEG="600")
    r = subprocess.run([str(SMOKE)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"sync_smoke.sh under pagination/clamp failed:\n{r.stdout}\n{r.stderr}"


def test_mss_clamp_lowers_segment_size_where_supported():
    """The MTU fix relies on TCP_MAXSEG genuinely lowering the negotiated MSS where it's settable.
    On platforms that reject it (macOS [Errno 22]) the clamp must DEGRADE GRACEFULLY — sync_common
    probes this into MAXSEG_OK and the client/daemon skip the clamp there, so connections never break
    and pagination carries sync (see the convergence test above, which runs on macos-latest)."""
    import sync_common
    if not sync_common.MAXSEG_OK:
        pytest.skip(f"TCP_MAXSEG not settable on {sys.platform}; pagination is the fallback")
    clamp = 600
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, clamp)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    import threading
    acc = []
    t = threading.Thread(target=lambda: acc.append(srv.accept()[0]))
    t.start()
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, clamp)
    cli.connect(("127.0.0.1", port))
    t.join(5)
    try:
        cm = cli.getsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG)
        sm = acc[0].getsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG)
        assert cm <= clamp and sm <= clamp, f"TCP_MAXSEG did not clamp MSS: client={cm} server={sm}"
    finally:
        cli.close()
        if acc:
            acc[0].close()
        srv.close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_serving(port, timeout=45):
    # Generous budget: cold daemon startup imports hv + crypto and binds the port, which
    # can take >15s on a loaded, slow CI runner (e.g. GitHub macos-latest, ~2x slower).
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/sync/merkle-root"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise AssertionError("daemon did not start serving in time")


def test_daemon_singleton_guard(hive):
    """A second `hv sync daemon` against the same bind:port must detect the
    healthy incumbent and exit 0 cleanly — not crash-loop on EADDRINUSE."""
    hive.run("remember", "seed fact for daemon test", "--tags", "test")
    port = _free_port()
    (hive.home / ".peers.json").write_text(json.dumps(
        {"self": "test-node", "bind": "127.0.0.1", "port": port, "peers": []}))
    env = dict(os.environ, HIVE_HOME=str(hive.home))

    first = subprocess.Popen(
        [sys.executable, str(HV), "sync", "daemon"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_until_serving(port)
        second = subprocess.run(
            [sys.executable, str(HV), "sync", "daemon"],
            env=env, capture_output=True, text=True, timeout=20,
        )
        out = (second.stdout + second.stderr).lower()
        assert second.returncode == 0, (
            f"duplicate daemon should exit cleanly, got {second.returncode}:\n{out}")
        assert "already" in out, f"expected an 'already running' notice, got:\n{out}"
    finally:
        first.terminate()
        try:
            first.wait(timeout=5)
        except subprocess.TimeoutExpired:
            first.kill()
