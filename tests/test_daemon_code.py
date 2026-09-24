"""#112: `hv doctor` daemon-code. A sync daemon still running code that no longer matches its own
checkout (a bare `git pull` moved the files, not the process) is reported, and `--fix` restarts it once.

The daemon's /api/daemon record is tested on the real Handler, in-process on 127.0.0.1. Doctor is tested
end to end against a stand-in daemon on a free port (the port in a temp HIVE_HOME's .peers.json, with an
explicit bind so the sync-bind check stays out of the way) and a `systemctl` stub that reports a managed
hive-sync unit and logs every call, so a restart is read from the log, never from doctor's text. No real
daemon or service is touched.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-daemoncode-")   # always, even if exported

import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402

hv = d.hv


# ── the verdict ─────────────────────────────────────────────────────────────────────────────────

LOADED = {"root": "/opt/hive", "digest": "a" * 64, "started_at": "2026-09-24T08:42:33Z"}


@pytest.mark.parametrize("loaded, current, hive_listener, expected", [
    (LOADED, "a" * 64, True, "ok"),
    (LOADED, "b" * 64, True, "warn"),                   # the checkout moved under the running daemon
    ("missing", None, True, "warn"),                    # a hive daemon from before #112
    ("missing", None, False, None),                     # some other process 404s on that port
    (None, None, True, None),                           # nothing usable answered
    ({**LOADED, "digest": None}, "a" * 64, True, None),  # the daemon could not fingerprint itself
    (LOADED, None, True, None),                         # doctor could not fingerprint the daemon's root
])
def test_daemon_code_verdict(loaded, current, hive_listener, expected):
    v = hv._daemon_code_verdict(loaded, current, hive_listener)
    assert (v[0] if v else None) == expected
    if expected == "warn" and loaded != "missing":
        assert "does not match /opt/hive's source digest" in v[1] and "2026-09-24T08:42:33Z" in v[1]
        assert "older" not in v[1]                      # a docs-only commit also changes the digest


# ── the daemon's record and route ───────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _real_daemon(from_ip=None):
    class H(d.Handler):
        def setup(self):
            super().setup()
            if from_ip:
                self.client_address = (from_ip, 40000)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def test_api_daemon_answers_loopback_with_the_loaded_code():
    with _real_daemon() as port:
        status, body = _get(port, "/api/daemon")
    assert status == 200
    assert body["root"] == str(sync_common.ROOT)
    assert body["digest"] == hv._source_manifest(sync_common.ROOT)["digest"]
    assert body["pid"] == os.getpid() and body["started_at"] and body["contract"] == hv.CONTRACT_VERSION


def test_api_daemon_refuses_an_unsigned_remote_caller_and_stays_off_hive_info():
    with _real_daemon("100.64.0.9") as port:
        assert _get(port, "/api/daemon")[0] == 403
    with _real_daemon() as port:
        _status, info = _get(port, "/hive/info")
    assert "digest" not in json.dumps(info)             # open discovery never shows which code runs


def test_the_loaded_record_never_fails_startup(monkeypatch):
    def boom(_root):
        raise OSError("git missing")
    monkeypatch.setattr(hv, "_source_manifest", boom)
    rec = d._loaded_code()
    assert rec["digest"] is None and rec["root"] and rec["pid"] == os.getpid()


# ── doctor, end to end, against a stand-in daemon ───────────────────────────────────────────────

@contextlib.contextmanager
def _standin(routes):
    """A stand-in daemon: `routes` maps a path to (status, JSON body); anything else is 404."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = routes.get(self.path.split("?")[0], (404, {"error": "not found"}))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _doctor(tmp_path, port, *args):
    """Run `hv doctor` against the stand-in on `port`, with a systemctl stub that reports a managed,
    active hive-sync unit and logs every call. Returns (stdout, the list of systemctl calls)."""
    home, bin_dir, log = tmp_path / "hive", tmp_path / "bin", tmp_path / "systemctl.log"
    home.mkdir(exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    (home / ".peers.json").write_text(json.dumps({"port": port, "bind": "127.0.0.1", "peers": []}))
    stub = bin_dir / "systemctl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\ncase "$*" in\n'
                    '  *LoadState*) echo loaded ;;\n  *is-active*) echo active ;;\n  *MainPID*) echo 4242 ;;\nesac\nexit 0\n')
    stub.chmod(0o755)
    env = dict(os.environ, HIVE_HOME=str(home), PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "doctor", *args], env=env,
                       capture_output=True, text=True, timeout=120)
    calls = log.read_text().splitlines() if log.exists() else []
    return r.stdout, calls


def _restarts(calls):
    return [c for c in calls if c.startswith("--user restart hive-sync")]


def _check(out):
    data = json.loads(out)
    checks = data["checks"] if isinstance(data, dict) else data
    return next((c for c in checks if c["name"] == "daemon-code"), None)


def _root(tmp_path):
    """A checkout for the stand-in daemon: not this repo, so a doctor run from here is the worktree case."""
    root = tmp_path / "daemon-checkout"
    root.mkdir(exist_ok=True)
    (root / "hv").write_text("# the daemon's code\n")
    return root


def _hive_routes(record):
    return {"/sync/merkle-root": (200, {"root_hash": "sha256:0"}),
            "/hive/info": (200, {"hive_id": "h1:test", "advertised_addr": None}),
            "/api/daemon": record}


def test_a_daemon_on_stale_code_is_reported_and_fix_restarts_it_once(tmp_path):
    root = _root(tmp_path)
    rec = {"root": str(root), "digest": "0" * 64, "started_at": "2026-09-24T08:42:33Z"}
    with _standin(_hive_routes((200, rec))) as port:
        c = _check(_doctor(tmp_path, port, "--format", "json")[0])
        assert c["status"] == "warn" and "does not match" in c["detail"] and str(root) in c["detail"]
        out, calls = _doctor(tmp_path, port, "--fix")
    assert len(_restarts(calls)) == 1, calls
    assert "(daemon-code:" in out


def test_a_daemon_on_its_checkouts_current_code_is_ok_even_from_another_checkout(tmp_path):
    """Doctor runs from this repo; the daemon's root is elsewhere and current. The comparison is with
    the daemon's OWN root, so nothing is flagged and nothing restarts (the #109 worktree lesson)."""
    root = _root(tmp_path)
    rec = {"root": str(root), "digest": hv._source_manifest(root)["digest"], "started_at": "t"}
    with _standin(_hive_routes((200, rec))) as port:
        assert _check(_doctor(tmp_path, port, "--format", "json")[0])["status"] == "ok"
        _out, calls = _doctor(tmp_path, port, "--fix")
    assert _restarts(calls) == []


def test_a_hive_daemon_from_before_112_is_reported_and_restarted(tmp_path):
    with _standin(_hive_routes((404, {"error": "not found"}))) as port:
        c = _check(_doctor(tmp_path, port, "--format", "json")[0])
        assert c["status"] == "warn" and "predates" in c["detail"]
        _out, calls = _doctor(tmp_path, port, "--fix")
    assert len(_restarts(calls)) == 1, calls


def test_a_non_hive_listener_on_the_port_gets_no_verdict_and_no_restart(tmp_path):
    with _standin({}) as port:                           # 404s everything: not a hive daemon
        assert _check(_doctor(tmp_path, port, "--format", "json")[0]) is None
        _out, calls = _doctor(tmp_path, port, "--fix")
    assert _restarts(calls) == []


def test_dry_run_says_it_would_restart_and_does_not(tmp_path):
    root = _root(tmp_path)
    rec = {"root": str(root), "digest": "0" * 64, "started_at": "t"}
    with _standin(_hive_routes((200, rec))) as port:
        out, calls = _doctor(tmp_path, port, "--fix", "--dry-run")
    assert "would restart the managed hive-sync service to load the current code (daemon-code)" in out
    assert _restarts(calls) == []
