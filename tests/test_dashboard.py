"""Dashboard read-API + static-serving guard.

The sync daemon serves a read-only `/api/*` (overview / search / peers) and the committed dashboard
SPA on the sync port. These are pure read projections that reuse hv's search/confidence/governance,
so they must stay in step with the CLI and never mutate the corpus. We drive the REAL daemon over
HTTP against an isolated HIVE_HOME (same pattern as test_sync).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(port, path, timeout=3):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "")


def _wait(port, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _get(port, "/sync/merkle-root")[0] == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise AssertionError("daemon did not start serving in time")


@pytest.fixture
def daemon(hive):
    """A live sync daemon over an isolated home seeded with one tagged fact + one tagged decision."""
    hive.run("remember", "android termux installer works", "--tags", "android,installer")
    hive.run("decide", "use runit on termux", "--rationale", "no systemd on android", "--tags", "android")
    port = _free_port()
    (hive.home / ".peers.json").write_text(json.dumps(
        {"self": "test-node", "bind": "127.0.0.1", "port": port, "peers": []}))
    env = dict(os.environ, HIVE_HOME=str(hive.home))
    proc = subprocess.Popen(
        [sys.executable, str(HV), "sync", "daemon"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait(port)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_api_overview(daemon):
    st, body, ct = _get(daemon, "/api/overview")
    assert st == 200 and "json" in ct
    o = json.loads(body)
    assert o["facts"] == 1 and o["decisions"] == 1
    assert o["contract"] and o["merkle"]


def test_api_search_returns_facts_and_decisions(daemon):
    o = json.loads(_get(daemon, "/api/search?q=android")[1])
    assert any(f["content"].startswith("android termux") for f in o["facts"])
    assert any(d["content"].startswith("use runit") for d in o["decisions"])


def test_api_search_tag_scopes_results(daemon):
    # `installer` tags only the fact, so the decision must drop out — the 1.15 project-scoping payoff.
    o = json.loads(_get(daemon, "/api/search?tag=installer")[1])
    assert [f["id"] for f in o["facts"]] == [1]
    assert o["decisions"] == []


def test_api_peers_shape(daemon):
    o = json.loads(_get(daemon, "/api/peers")[1])
    assert "peers" in o and isinstance(o["peers"], list)


def test_serves_spa_and_assets(daemon):
    st, body, ct = _get(daemon, "/")
    assert st == 200 and "text/html" in ct
    assert b"<title>HiveMind</title>" in body and b'src="logo.svg"' in body
    assert _get(daemon, "/logo.svg")[0] == 200
    assert _get(daemon, "/favicon.svg")[0] == 200


def test_unknown_path_is_404(daemon):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(daemon, "/no-such-route")
    assert ei.value.code == 404


def test_sync_endpoints_unaffected(daemon):
    # The dashboard routes are additive — the sync surface must be byte-for-byte unchanged.
    assert _get(daemon, "/sync/hello")[0] == 200
    assert _get(daemon, "/sync/merkle-root")[0] == 200
