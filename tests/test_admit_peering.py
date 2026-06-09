"""admit seeds RECIPROCAL peering: a device's join-request carries its reachable URL, and
`hv admit <device>` adds that URL to the owner's .peers.json so sync becomes symmetric (the owner
syncs to the member, not only the member to the owner). Connectivity is seeded by admission but
stays a separate, editable concern from the trust grant (admit still confers no governance)."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _run(home, *args):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args],
                       env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_ap", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_ap", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _signed_join_request(hv, url=None):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    nid = hv._device_id_for_pub(pub)
    payload = {"action": "join-request", "device_id": nid, "label": "joiner"}
    if url:
        payload["url"] = url
    e = {"node_id": nid, "seq": 1, "type": "governance", "timestamp": "2026-05-01T00:00:00Z",
         "payload": payload, "prev_hash": "sha256:genesis"}
    hv._sign_entry(e, seed, pub)
    return nid, e


def test_admit_seeds_reciprocal_peer_from_join_request_url(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    url = "http://100.0.0.9:9876"
    nid, jr = _signed_join_request(hv, url)
    assert hv.append_foreign_entries([jr])[0] == 1            # join-request accepted (Phase 1)

    out = _run(tmp_path, "admit", nid, "--principal", "bob").stdout
    assert "reciprocal peer" in out
    peers = json.loads((tmp_path / ".peers.json").read_text())["peers"]
    assert any(p.get("url") == url for p in peers)


def test_admit_without_advertised_url_seeds_nothing(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    nid, jr = _signed_join_request(hv, url=None)             # no URL advertised
    hv.append_foreign_entries([jr])

    _run(tmp_path, "admit", nid, "--principal", "bob")
    pj = tmp_path / ".peers.json"
    if pj.exists():
        assert json.loads(pj.read_text()).get("peers", []) == []


def test_add_peer_is_idempotent(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._add_peer("http://100.0.0.5:9876", "x") is True
    assert hv._add_peer("http://100.0.0.5:9876", "x") is False        # duplicate → no-op
    peers = json.loads((tmp_path / ".peers.json").read_text())["peers"]
    assert len([p for p in peers if p.get("url") == "http://100.0.0.5:9876"]) == 1
