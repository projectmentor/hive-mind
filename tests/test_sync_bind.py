"""#47: a daemon that started before tailscaled (or whose tailnet IP changed) rebinds by itself, and
`hv doctor` sees a daemon stuck on loopback.

Everything is exercised in-process with `sync_common.tailscale_ip` stubbed, the way
tests/test_sync_request_signing.py stubs it: no network, no supervisor, no real tailscale.
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-bindtest-")   # always, even if exported: never the live hive

import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402

TAILNET, OTHER = "100.100.1.2", "100.100.9.9"


def _hv():
    loader = importlib.machinery.SourceFileLoader("hvmod_bind", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_bind", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


# ── one definition of "automatic" ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,cfg,auto", [
    (None, {}, True),
    (None, {"bind": None}, True),
    (None, {"bind": "0.0.0.0"}, True),          # legacy installer value: automatic, as resolve_bind treats it
    (None, {"bind": "::"}, True),
    (None, {"bind": ""}, True),
    (None, {"bind": "192.168.1.5"}, False),     # an explicit, specific bind is fixed
    ("10.9.8.7", {}, False),                     # HIVE_BIND always fixes it
    ("0.0.0.0", {}, False),
])
def test_bind_is_auto(monkeypatch, env, cfg, auto):
    if env is None:
        monkeypatch.delenv("HIVE_BIND", raising=False)
    else:
        monkeypatch.setenv("HIVE_BIND", env)
    assert sync_common.bind_is_auto(cfg) is auto


# ── the rebind rule ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bound,resolved,auto,expect", [
    ("127.0.0.1", TAILNET, True, True),          # started before tailscaled
    (TAILNET, OTHER, True, True),                # tailnet IP changed
    (TAILNET, TAILNET, True, False),             # nothing changed
    (TAILNET, "127.0.0.1", True, False),         # never toward loopback (tailscale dropped)
    ("127.0.0.1", "127.0.0.1", True, False),     # no tailnet at all: loopback is right
    ("127.0.0.1", TAILNET, False, False),        # an explicit bind never moves
    (TAILNET, OTHER, False, False),
])
def test_should_rebind_truth_table(bound, resolved, auto, expect):
    assert sync_common.should_rebind(bound, resolved, auto) is expect


# ── the start-wait ──────────────────────────────────────────────────────────────────────────────────

def test_start_wait_returns_the_tailnet_as_soon_as_it_appears(monkeypatch):
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "load_peers", lambda: {})
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/tailscale")
    answers = iter([None, None, TAILNET])
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: next(answers))
    assert d._wait_for_tailnet(max_s=5, step=0) == TAILNET


def test_start_wait_skips_without_tailscale_or_with_a_fixed_bind(monkeypatch):
    calls = []
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: calls.append(1))
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "load_peers", lambda: {})
    monkeypatch.setattr(d.shutil, "which", lambda name: None)                 # no tailscale CLI
    assert d._wait_for_tailnet(max_s=30, step=0) is None
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(sync_common, "load_peers", lambda: {"bind": "192.168.1.5"})   # explicit bind
    assert d._wait_for_tailnet(max_s=30, step=0) is None
    assert calls == []                                                         # never polled


def test_start_wait_gives_up_and_says_so(monkeypatch, capsys):
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "load_peers", lambda: {})
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: None)
    assert d._wait_for_tailnet(max_s=0, step=0) is None
    assert "will rebind when one appears" in capsys.readouterr().out


# ── one loop iteration, in-process ───────────────────────────────────────────────────────────────────

class _Srv:
    def __init__(self):
        self.stopped = False

    def shutdown(self):
        self.stopped = True


def _round(monkeypatch, bound, auto, answers, interval=0.05):
    monkeypatch.delenv("HIVE_BIND", raising=False)
    monkeypatch.setattr(sync_common, "load_peers", lambda: {})
    it = iter(answers)
    monkeypatch.setattr(sync_common, "tailscale_ip", lambda: next(it, answers[-1]))
    monkeypatch.setattr(d, "_DEGRADED_TICK", 0.01)
    monkeypatch.setattr(d.hv, "_ensure_store_current", lambda: False)
    server, extra = _Srv(), [_Srv()]
    return server, extra, lambda: d._run_round(server, extra, bound, auto, interval, lambda: None)


def test_a_loopback_daemon_exits_75_once_the_tailnet_is_up(monkeypatch):
    server, extra, run = _round(monkeypatch, "127.0.0.1", True, [TAILNET])
    with pytest.raises(SystemExit) as e:
        run()
    assert e.value.code == 75 and server.stopped and extra[0].stopped      # clean shutdown first


def test_the_degraded_tick_catches_a_tailnet_that_appears_between_rounds(monkeypatch):
    server, extra, run = _round(monkeypatch, "127.0.0.1", True, [None, None, TAILNET], interval=5)
    with pytest.raises(SystemExit) as e:
        run()                                    # would otherwise sleep 5 s; exits on a 0.01 s tick
    assert e.value.code == 75


def test_no_exit_when_nothing_changed_or_toward_loopback_or_fixed(monkeypatch):
    for bound, auto, answers in ((TAILNET, True, [TAILNET]), (TAILNET, True, [None]),
                                 ("127.0.0.1", False, [TAILNET])):
        server, extra, run = _round(monkeypatch, bound, auto, answers)
        run()                                    # returns after `interval`, no SystemExit
        assert not server.stopped


def test_a_changed_tailnet_ip_exits_75(monkeypatch):
    _server, _extra, run = _round(monkeypatch, TAILNET, True, [OTHER])
    with pytest.raises(SystemExit) as e:
        run()
    assert e.value.code == 75


# ── the doctor verdict ──────────────────────────────────────────────────────────────────────────────

def test_sync_bind_verdict_truth_table():
    hv = _hv()
    v = hv._sync_bind_verdict
    assert v(TAILNET, None, True)[0] == "warn"                       # stuck on loopback, tailnet up
    assert "127.0.0.1" in v(TAILNET, None, True)[1]
    assert v(TAILNET, f"{OTHER}:9876", True)[0] == "warn"           # tailnet IP changed
    assert v(TAILNET, f"{TAILNET}:9876", True)[0] == "ok"
    assert v("127.0.0.1", None, True)[0] == "ok"                    # no tailnet: loopback is right
    assert v("127.0.0.1", f"{TAILNET}:9876", True)[0] == "ok"       # never toward loopback
    assert v(TAILNET, None, False)[0] == "ok"                       # a fixed bind is the operator's call
