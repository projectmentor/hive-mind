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
POSIX = os.name != "nt"
HV = PROJECT / "hv"


# A smoke run takes ~20s. Past this it is hung, and the test fails with its output rather than
# holding an xdist worker until the CI job is cancelled with none (#159's macOS run, #161).
SMOKE_TIMEOUT = 300


def _stop_group(proc):
    """Stop a smoke run and everything it started. SIGTERM to the process group first, so the script's
    EXIT trap kills its daemons and removes its temp hives; SIGKILL would skip the trap and orphan them."""
    import signal
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _start_smoke(env=None):
    return subprocess.Popen([str(SMOKE)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env, start_new_session=POSIX)


def _finish_smoke(proc, timeout=SMOKE_TIMEOUT):
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        out, _ = proc.communicate()
        pytest.fail(f"sync_smoke.sh hung past {timeout}s:\n{out}")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, "")


def _smoke(env=None, timeout=SMOKE_TIMEOUT):
    return _finish_smoke(_start_smoke(env), timeout)


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_two_node_convergence():
    r = _smoke()
    assert r.returncode == 0, f"sync_smoke.sh failed:\n{r.stdout}\n{r.stderr}"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_two_node_convergence_under_aggressive_pagination():
    # Path-MTU resilience: force 1-entry PULL/PUSH pages and a tiny TCP MSS clamp. These change only
    # the transport granularity (smaller requests, smaller segments) — the G-Set union result must be
    # byte-identical, so the same harness must still converge. Guards against pagination or the clamp
    # accidentally dropping/duplicating entries. Runs on macos-latest too (where the clamp no-ops and
    # pagination alone carries it).
    env = dict(os.environ, HIVE_SYNC_PULL_PAGE="1", HIVE_SYNC_PUSH_PAGE="1", HIVE_SYNC_MAXSEG="600")
    r = _smoke(env)
    assert r.returncode == 0, f"sync_smoke.sh under pagination/clamp failed:\n{r.stdout}\n{r.stderr}"


# ── #161: two smoke runs at once, and a smoke whose port is taken ────────────────────────────────────

@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_two_smoke_runs_at_once_do_not_share_daemons():
    """The race itself: before #161 both runs bound 19876/19877, so under xdist one run's B pulled the
    other run's A (a failure) or the two deadlocked (a hang). Each run now picks its own free ports."""
    procs = [_start_smoke() for _ in range(2)]
    try:
        runs = [_finish_smoke(p) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                _stop_group(p)
    for r in runs:
        assert r.returncode == 0, f"a concurrent sync_smoke.sh run failed:\n{r.stdout}"


class _Squatter:
    """Holds a port the smoke is told to use.

    - `silent` accepts and never answers.
    - `hive` answers /sync/merkle-root like another hive's daemon: the smoke's daemon sees a healthy hive
      there and exits ("already owns"), which is #161's collision.
    - `impostor` answers that only to curl. The daemon's own probe (urllib) gets a 404, so it takes the
      port for a non-hive squatter, moves to the next port and stays up (`make_server`'s fallback). Only
      the smoke's root comparison can then tell it is talking to the wrong process."""

    def __init__(self, kind):
        self.kind = kind
        import http.server
        import threading
        if kind == "silent":
            self.sock = socket.socket()
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(8)
            self.port = self.sock.getsockname()[1]
            self._close = self.sock.close
            return

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                curl = self.headers.get("User-Agent", "").startswith("curl/")
                if self.path.split("?")[0] != "/sync/merkle-root" or (kind == "impostor" and not curl):
                    self.send_error(404)
                    return
                body = json.dumps({"root_hash": "sha256:" + "0" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._close = lambda: (srv.shutdown(), srv.server_close())

    def close(self):
        self._close()


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
@pytest.mark.parametrize("kind, why", [
    ("silent", ("did not serve",)),                        # accepts, never answers: the wait is bounded
    ("hive", ("exited before serving", "Another process holds the port")),     # whichever it sees first
    ("impostor", ("Another process holds the port",)),     # our daemon is up elsewhere; the root says so
])
def test_a_taken_port_fails_the_smoke_quickly_and_says_why(kind, why):
    squatter = _Squatter(kind)
    try:
        env = dict(os.environ, HIVE_SMOKE_PORT_A=str(squatter.port), SMOKE_WAIT_SECONDS="8")
        t0 = time.time()
        r = _smoke(env, timeout=120)
        elapsed = time.time() - t0
    finally:
        squatter.close()
    assert r.returncode != 0 and "daemon A failed to start" in r.stdout, r.stdout
    assert any(w in r.stdout for w in why), r.stdout
    assert elapsed < 90, f"a taken port must fail the smoke, not stall it ({elapsed:.0f}s)"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_a_daemon_that_dies_at_startup_is_reported_at_once():
    """Port 70000 does not exist: the daemon raises on bind and exits. The smoke says so straight away,
    with the daemon's log, rather than polling a dead port until its deadline."""
    t0 = time.time()
    r = _smoke(dict(os.environ, HIVE_SMOKE_PORT_A="70000", SMOKE_WAIT_SECONDS="60"), timeout=120)
    elapsed = time.time() - t0
    assert r.returncode != 0 and "exited before serving" in r.stdout, r.stdout
    assert elapsed < 30, f"a dead daemon was polled until the deadline ({elapsed:.0f}s)"


def _curl_calls(script):
    """Each curl invocation in a smoke script, as its argument text up to the end of the command."""
    import re
    code = "\n".join(line for line in script.read_text().splitlines() if not line.lstrip().startswith("#"))
    return re.findall(r"\bcurl\b[^|;)\n]*", code)


@pytest.mark.parametrize("name", ["sync_smoke.sh", "sync_auth_smoke.sh", "_smoke_daemon.sh"])
def test_every_curl_in_the_smokes_is_bounded(name):
    """A curl with no --max-time against a daemon that accepts and never answers hangs the script: the
    orphaned curl on #159's cancelled macOS job. And --retry-delay 0 is curl's backoff to 10 minutes."""
    calls = _curl_calls(PROJECT / "scripts" / "common" / name)
    assert calls, f"no curl found in {name}; the check would pass vacuously"
    for c in calls:
        assert "--max-time" in c, f"unbounded curl in {name}: {c.strip()}"
        assert "--retry" not in c, f"curl retry backoff in {name}: {c.strip()}"


def test_the_smokes_pick_their_ports_per_run():
    for name in ("sync_smoke.sh", "sync_auth_smoke.sh"):
        text = (PROJECT / "scripts" / "common" / name).read_text()
        for fixed in ("19876", "19877", "19886", "19887", "29876"):
            assert fixed not in text, f"{name} still binds the fixed port {fixed} (#161)"


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


def test_smoke_ports_come_from_below_the_ephemeral_range():
    """#173: `bind(0)`-then-close let two concurrent smoke runs be handed the same port. The picker draws from
    20000-32767, below the kernel's ephemeral range, so no `bind(0)` anywhere can be given its port, and each
    draw is bindable when made."""
    lib = PROJECT / "scripts" / "common" / "_smoke_daemon.sh"
    r = subprocess.run(["bash", "-c", f'. "{lib}"; for i in $(seq 1 40); do smoke_free_port; done'],
                       capture_output=True, text=True, timeout=120)
    ports = [int(x) for x in r.stdout.split()]
    assert len(ports) == 40 and all(20000 <= p <= 32767 for p in ports), ports
    assert len(set(ports)) >= 30, "draws should be spread, not the kernel's next ephemeral port"
