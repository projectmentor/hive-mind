"""`api_peers(probe=False)` (the module API's `/v1/peers?probe=0`, read by hcns) answers from the last probe map.

A directly-admitted peer has no join request, so without the map it has no address and hcns never dials it: the bus
was a star around the one node that joined by request. The read itself never waits on a fan-out; a stale or empty map
starts one background refresh, at most one at a time."""
import importlib.machinery
import importlib.util
import os
import threading
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _hv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader(f"hvmod_probe0_{os.urandom(4).hex()}", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _gov(hv, monkeypatch, *devices):
    gov = {"admitted": set(devices), "principals": {}, "purged": set()}
    monkeypatch.setattr(hv, "_governance_state", lambda entries: gov)
    monkeypatch.setattr(hv.merkle, "read_all_entries", lambda d: [])


def test_probe_off_gives_a_directly_admitted_peer_its_cached_address(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "home", monkeypatch)
    _gov(hv, monkeypatch, "k1:egmbl5a")
    monkeypatch.setattr(hv, "_probe_peer_map", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fan-out")))
    hv._PEER_PROBE_CACHE.update(t=hv.time.monotonic(), map={"k1:egmbl5a": ("100.1.2.3:9876", "in sync", "EGMBL5A")})
    row, = hv.api_peers(probe=False)
    assert (row["addr"], row["status"], row["name"]) == ("100.1.2.3:9876", "in sync", "EGMBL5A")


def test_probe_off_with_no_map_answers_at_once_and_starts_one_refresh(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "home", monkeypatch)
    _gov(hv, monkeypatch, "k1:egmbl5a")
    release, calls = threading.Event(), []

    def slow_probe(*a, **k):
        calls.append(1)
        release.wait(5)
        return {"k1:egmbl5a": ("100.1.2.3:9876", "reachable", "")}

    monkeypatch.setattr(hv, "_probe_peer_map", slow_probe)
    row, = hv.api_peers(probe=False)                 # does not wait on the probe that is still running
    assert row["addr"] == "—" and row["status"] == "offline"
    hv.api_peers(probe=False)                        # a second stale read starts no second fan-out
    release.set()
    for t in [t for t in threading.enumerate() if t.name == "hv-peer-probe"]:
        t.join(5)
    assert len(calls) == 1
    row, = hv.api_peers(probe=False)
    assert row["addr"] == "100.1.2.3:9876"


def test_probe_off_with_a_fresh_map_starts_no_refresh(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "home", monkeypatch)
    _gov(hv, monkeypatch, "k1:egmbl5a")
    started = []
    monkeypatch.setattr(hv.threading, "Thread", lambda *a, **k: started.append(k) or threading.Thread())
    hv._PEER_PROBE_CACHE.update(t=hv.time.monotonic(), map={})
    hv.api_peers(probe=False)
    assert started == []


def test_a_failed_refresh_keeps_the_last_map(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "home", monkeypatch)
    _gov(hv, monkeypatch, "k1:egmbl5a")
    hv._PEER_PROBE_CACHE.update(t=hv.time.monotonic() - hv._PEER_PROBE_REFRESH - 1,
                                map={"k1:egmbl5a": ("100.1.2.3:9876", "in sync", "")})

    def boom(*a, **k):
        raise RuntimeError("network")

    monkeypatch.setattr(hv, "_probe_peer_map", boom)
    hv.api_peers(probe=False)
    for t in [t for t in threading.enumerate() if t.name == "hv-peer-probe"]:
        t.join(5)
    assert hv._PEER_PROBE_BG["running"] is False
    row, = hv.api_peers(probe=False)
    assert row["addr"] == "100.1.2.3:9876"
