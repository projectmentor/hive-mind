"""#7: a peer whose tailnet IP changed is learned from its VERIFIED inbound requests, and `hv doctor --fix`
repoints its .peers.json entry.

In-process only. The daemon's Handler runs on a 127.0.0.1 ThreadingHTTPServer whose handler reports a
chosen client address (a real peer's source address is its tailnet IP, never loopback). Requests are
signed the way tests/test_sync_request_signing.py signs them, every HIVE_HOME is a temp dir, doctor's
process-killing and service-restarting helpers are stubbed, and `tailscale` is stubbed or absent. No
real peer, tailnet or network is contacted.
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-peeraddr-")   # always, even if exported: never the live hive

import ed25519  # noqa: E402
import sync_common  # noqa: E402
import hive_sync_daemon as d  # noqa: E402

OLD, NEW, OTHER = "100.64.0.2", "100.64.0.9", "100.64.0.7"


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────

def _dev():
    seed = os.urandom(32)
    return seed, "k1:" + hashlib.sha256(ed25519.pub_from_seed(seed)).hexdigest()[:16]


def _headers(seed, method, path, query="", body=b""):
    pub = ed25519.pub_from_seed(seed)
    ts, nonce = int(time.time()), os.urandom(16).hex()
    sig = ed25519.sign(sync_common.sync_signing_bytes(method, path, query, body, ts, nonce), seed)
    return {
        "Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG,
        "Hive-Auth-Device": "k1:" + hashlib.sha256(pub).hexdigest()[:16],
        "Hive-Auth-Pub": base64.b64encode(pub).decode(),
        "Hive-Auth-Ts": str(ts),
        "Hive-Auth-Nonce": nonce,
        "Hive-Auth-Sig": base64.b64encode(sig).decode(),
    }


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _load_hv(home, monkeypatch):
    """A fresh hv module bound to `home` (hv reads HIVE_HOME at import), wired into the daemon module."""
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader(f"hvmod_peeraddr_{os.urandom(4).hex()}", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    monkeypatch.setattr(d, "hv", m)
    monkeypatch.setattr(d, "_peer_seen", {})
    monkeypatch.delenv("HIVE_SYNC_AUTH", raising=False)
    _pin_hive(m, monkeypatch)
    return m


def _pin_hive(m, monkeypatch):
    """Leave the temp hive PINNED. Since contract 1.27 a node that has not pinned its genesis serves
    nothing of its journal to a REMOTE caller (hive-mind-private #14), and every daemon test in this
    file and in test_hello_sig.py speaks to the daemon as a remote peer — so without this they would be
    testing the pin gate instead of the read-auth gate they are about. A hive that really has a genesis
    is pinned for real; one whose governance is monkeypatched (the common case here, see `_admit`) gets
    a stand-in pin, in exactly the same spirit."""
    m.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if m._auto_pin_genesis(m.merkle.read_all_entries(m.JOURNAL_DIR)) is None:
        # No genesis to pin, so satisfy the GATE rather than invent a pin. A stand-in pin would make the
        # projection report `mismatch` (pinned, genesis absent), which correctly fails ingest closed —
        # and would break the convergence tests here, which are about read-auth and signing.
        monkeypatch.setattr(d.Handler, "_pinned", lambda self, u: True)


def _run(home, *args):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_OWNER_PASSPHRASE="testpass",
               HIVE_IDENTITY_STASH=str(Path(home) / "stash"))      # `owner init` must not touch ~/.config
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


@contextlib.contextmanager
def _daemon(from_ip):
    """The real Handler on 127.0.0.1, seeing every request as coming from `from_ip` (None = the real
    loopback address). Yields the port."""
    class H(d.Handler):
        def setup(self):
            super().setup()
            if from_ip:
                self.client_address = (from_ip, 40000)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _admit(monkeypatch, *devices):
    """Governance as the daemon sees it: an owned hive admitting `devices`."""
    gov = {"owner_id": "o1:test", "admitted": set(devices), "purged": set()}
    monkeypatch.setattr(d.Handler, "_gov", lambda self: gov)


def _write_peers(home, cfg, age=3600):
    """Write .peers.json and date it `age` seconds back: the stored binding predates what follows."""
    p = Path(home) / ".peers.json"
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def _doctor_fix(hvm, monkeypatch, dry=False, stub_tailscale=True):
    """Run `hv doctor --fix` in-process on `hvm`'s hive, with everything that could reach outside the
    temp hive stubbed: orphan-daemon kills, service restarts, the Claude config, the tailnet."""
    monkeypatch.setattr(hvm, "_orphan_daemons", lambda: ([], False))
    monkeypatch.setattr(hvm, "_restart_managed_daemon", lambda: pytest.fail("doctor tried to restart a service"))
    if stub_tailscale:
        monkeypatch.setattr(hvm, "_tailscale_hosts", lambda: [])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(hvm.HIVE_HOME / "no-claude"))
    with pytest.raises(SystemExit):
        hvm.doctor_cmd(argparse.Namespace(doctor_action=None, format="text", fix=True, dry_run=dry))


# ── the verdict (pure) ────────────────────────────────────────────────────────────────────────────────

DEV = "k1:0123456789abcdef"


@pytest.mark.parametrize("answered,device,seen,since,expect", [
    (True, DEV, {NEW: 500}, 100, ("ok", None)),             # a working address is never rewritten
    (True, None, {}, 0, ("ok", None)),
    (False, DEV, {NEW: 500}, 100, ("move", NEW)),           # fails; verified elsewhere since
    (False, DEV, {OLD: 400, NEW: 500}, 100, ("move", NEW)),
    (False, DEV, {OLD: 500, NEW: 400}, 100, ("report", None)),   # the stored address is the newest sighting
    (False, DEV, {OLD: 500, NEW: 500}, 100, ("report", None)),   # a tie is not "newer"
    (False, DEV, {NEW: 50}, 100, ("report", None)),         # seen before .peers.json last changed
    (False, DEV, {OLD: 500}, 100, ("report", None)),        # only ever seen where it is stored
    (False, DEV, {}, 100, ("report", None)),                # device known, never verified
    (False, None, {NEW: 500}, 100, ("unknown", None)),      # no known device: report, never rewrite
])
def test_verdict_table(answered, device, seen, since, expect):
    hv = d.hv
    assert hv._peer_address_verdict(OLD, answered, device, seen, since) == expect


def test_entry_maps_to_a_device_by_id_or_by_verified_host_never_by_name():
    hv = d.hv
    other = "k1:fedcba9876543210"
    cands = {DEV: {OLD: {"first_seen": 1, "last_seen": 1}}, other: {OTHER: {"first_seen": 1, "last_seen": 1}}}
    assert hv._peer_entry_device({"id": other, "url": f"http://{OLD}:9876"}, cands) == other   # (a) the id wins
    assert hv._peer_entry_device({"id": "david", "url": f"http://{OLD}:9876"}, cands) == DEV    # (b) seen there
    assert hv._peer_entry_device({"id": "node-b", "label": "node-b", "url": f"http://{NEW}:9876"}, cands) is None
    both = dict(cands, **{other: {OLD: {"first_seen": 1, "last_seen": 1}}})
    assert hv._peer_entry_device({"id": "david", "url": f"http://{OLD}:9876"}, both) is None   # ambiguous


# ── recording: verified inbound only ──────────────────────────────────────────────────────────────────

def test_a_verified_request_records_its_device_at_its_source_address(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    seed, dev = _dev()
    _admit(monkeypatch, dev)
    with _daemon(NEW) as port:
        assert _get(port, "/sync/hello", _headers(seed, "GET", "/sync/hello")) == 200
    rec = hvm._load_peer_candidates()
    assert list(rec) == [dev] and list(rec[dev]) == [NEW]
    assert rec[dev][NEW]["first_seen"] <= rec[dev][NEW]["last_seen"] <= int(time.time())


def test_unsigned_bad_signature_and_unadmitted_requests_record_nothing(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    seed, dev = _dev()
    stranger_seed, _stranger = _dev()
    _admit(monkeypatch, dev)
    bad = _headers(seed, "GET", "/sync/hello")
    bad["Hive-Auth-Sig"] = base64.b64encode(b"\x00" * 64).decode()
    with _daemon(NEW) as port:
        assert _get(port, "/sync/hello") == 200                    # permissive: served, but unsigned
        assert _get(port, "/sync/hello", bad) == 200               # served, but the signature is bad
        assert _get(port, "/sync/hello", _headers(stranger_seed, "GET", "/sync/hello")) == 200  # not admitted
        assert _get(port, "/sync/merkle-root", bad) == 200
    assert hvm._load_peer_candidates() == {}
    assert not hvm.PEER_CANDIDATES_PATH.exists()


def test_loopback_records_nothing(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    seed, dev = _dev()
    _admit(monkeypatch, dev)
    with _daemon(None) as port:                                    # the real 127.0.0.1 client address
        assert _get(port, "/sync/hello", _headers(seed, "GET", "/sync/hello")) == 200
        assert _get(port, "/sync/merkle-root", _headers(seed, "GET", "/sync/merkle-root")) == 200
    assert not hvm.PEER_CANDIDATES_PATH.exists()
    assert hvm._record_peer_candidate(dev, "127.0.0.1") is False   # refused below the daemon too
    assert hvm._record_peer_candidate(dev, "::1") is False
    assert not hvm.PEER_CANDIDATES_PATH.exists()


def test_an_in_sync_round_is_enough(tmp_path, monkeypatch):
    """A peer already in sync only asks /sync/merkle-root (open discovery, never gated). Its signature is
    checked anyway, to learn the address; in `off` mode the gated paths learn the same way."""
    hvm = _load_hv(tmp_path, monkeypatch)
    seed, dev = _dev()
    _admit(monkeypatch, dev)
    with _daemon(NEW) as port:
        assert _get(port, "/sync/merkle-root", _headers(seed, "GET", "/sync/merkle-root")) == 200
    assert list(hvm._load_peer_candidates()[dev]) == [NEW]

    monkeypatch.setenv("HIVE_SYNC_AUTH", "off")
    with _daemon(OTHER) as port:
        assert _get(port, "/sync/hello", _headers(seed, "GET", "/sync/hello")) == 200
    assert set(hvm._load_peer_candidates()[dev]) == {NEW, OTHER}


def test_history_is_bounded_and_an_unchanged_sighting_is_not_rewritten(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    for i in range(12):
        assert hvm._record_peer_candidate(DEV, f"100.64.1.{i}", now=1000 + i) is True
    hist = hvm._load_peer_candidates()[DEV]
    assert len(hist) == hvm._PEER_CANDIDATES_PER_DEVICE
    assert set(hist) == {f"100.64.1.{i}" for i in range(4, 12)}     # the most recent survive
    before = hvm.PEER_CANDIDATES_PATH.stat().st_mtime_ns
    assert hvm._record_peer_candidate(DEV, "100.64.1.11", now=1011) is False   # nothing new: no write
    assert hvm.PEER_CANDIDATES_PATH.stat().st_mtime_ns == before
    assert hvm._record_peer_candidate(DEV, "100.64.1.11", now=2000) is False   # known ip, later: refreshed
    assert hvm._load_peer_candidates()[DEV]["100.64.1.11"] == {"first_seen": 1011, "last_seen": 2000}
    assert (hvm.PEER_CANDIDATES_PATH.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_failing_write_never_fails_the_request(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    seed, dev = _dev()
    _admit(monkeypatch, dev)
    monkeypatch.setattr(hvm, "_record_peer_candidate", lambda *a, **k: 1 / 0)
    with _daemon(NEW) as port:
        assert _get(port, "/sync/hello", _headers(seed, "GET", "/sync/hello")) == 200


# ── the rewrite: surgical and atomic ──────────────────────────────────────────────────────────────────

def _peers_cfg(url_b, id_b=DEV):
    return {
        "self": "node-a", "port": 9876, "bind": "100.64.0.1", "sync_auth": "enforce",
        "x-future": {"keep": [1, 2]},
        "peers": [
            {"id": "node-c", "url": "http://100.64.0.3:9876", "label": "zfold-6"},
            {"url": url_b, "id": id_b, "label": "node-b", "x-note": "keep me"},
        ],
    }


def test_repoint_changes_only_that_entrys_host(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    p = _write_peers(tmp_path, _peers_cfg(f"http://{OLD}:9877/"))
    os.chmod(p, 0o640)
    move = {"index": 1, "device": DEV, "url": f"http://{OLD}:9877/", "to": hvm._url_with_host(f"http://{OLD}:9877/", NEW)}
    assert hvm._repoint_peers(p, [move]) == [move]
    got = json.loads(p.read_text())
    want = _peers_cfg(f"http://{NEW}:9877/")
    assert got == want
    assert list(got) == list(want) and [list(e) for e in got["peers"]] == [list(e) for e in want["peers"]]
    assert (p.stat().st_mode & 0o777) == 0o640
    assert [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")] == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_repoint_keeps_a_symlinked_peers_json_a_symlink(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    store = tmp_path / "cfg"
    store.mkdir()
    target = _write_peers(store, _peers_cfg(f"http://{OLD}:9876"))
    link = tmp_path / ".peers.json"
    link.symlink_to(target)
    move = {"index": 1, "device": DEV, "url": f"http://{OLD}:9876", "to": f"http://{NEW}:9876"}
    assert hvm._repoint_peers(link, [move]) == [move]
    assert link.is_symlink()
    assert json.loads(target.read_text())["peers"][1]["url"] == f"http://{NEW}:9876"


def test_repoint_skips_an_entry_edited_since_the_check(tmp_path, monkeypatch):
    hvm = _load_hv(tmp_path, monkeypatch)
    p = _write_peers(tmp_path, _peers_cfg(f"http://{OTHER}:9876"))     # the operator already fixed it
    before = p.read_text()
    move = {"index": 1, "device": DEV, "url": f"http://{OLD}:9876", "to": f"http://{NEW}:9876"}
    assert hvm._repoint_peers(p, [move]) == []
    assert p.read_text() == before


def test_url_with_host_keeps_scheme_port_and_path():
    hv = d.hv
    assert hv._url_with_host("http://100.64.0.2:9876", NEW) == f"http://{NEW}:9876"
    assert hv._url_with_host("https://old.example.ts.net:9443/x/", NEW) == f"https://{NEW}:9443/x/"
    assert hv._url_with_host("http://100.64.0.2", NEW) == f"http://{NEW}"
    assert hv._url_with_host("http://100.64.0.2:9876", "fd7a:115c::5") == "http://[fd7a:115c::5]:9876"


# ── the check: report-only paths and tailscale hints ──────────────────────────────────────────────────

def test_no_candidate_reports_unverified_hints_and_never_moves():
    hv = d.hv
    peers = [{"id": "david", "label": "node-b", "url": f"http://{OLD}:9876"}]
    ts = [("node-b", [OTHER, OLD]), ("node-c", ["100.64.0.3"])]
    c = hv._peer_address_check(peers, {f"http://{OLD}:9876": False}, {}, 0, lambda: ts)
    assert c["status"] == "warn" and c["moves"] == []
    assert "unverified" in c["detail"] and OTHER in c["detail"] and "100.64.0.3" not in c["detail"]
    assert f"{OLD} (" not in c["detail"]                             # the stale address is no hint


def test_tailscale_binary_missing_is_no_error_and_no_change(tmp_path, monkeypatch, capsys):
    hvm = _load_hv(tmp_path, monkeypatch)
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))                          # Android/Termux: no tailscale CLI
    assert hvm._tailscale_hosts() == []
    peers = [{"id": "node-b", "url": f"http://{OLD}:9876"}]
    c = hvm._peer_address_check(peers, {f"http://{OLD}:9876": False}, {}, 0, hvm._tailscale_hosts)
    assert c["status"] == "ok" and c["moves"] == [] and "hint" not in c["detail"]

    # the whole doctor --fix, with the real (absent) tailscale: no error line, the file untouched
    p = _write_peers(tmp_path, {"self": "a", "port": _free_port(),
                                "peers": [{"id": "node-b", "url": f"http://127.0.0.1:{_free_port()}"}]})
    before = p.read_text()
    _doctor_fix(hvm, monkeypatch, stub_tailscale=False)
    out = capsys.readouterr().out
    assert "peer-address  0 stored peer address(es) answer; 1 unreachable" in out
    assert "check error" not in out and "heal error" not in out
    assert p.read_text() == before


def test_tailscale_failing_is_no_error(monkeypatch):
    hv = d.hv

    class _LoggedOut:
        returncode, stdout = 1, ""
    monkeypatch.setattr(hv.subprocess, "run", lambda *a, **k: _LoggedOut())
    assert hv._tailscale_hosts() == []
    monkeypatch.setattr(hv.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert hv._tailscale_hosts() == []


def test_a_working_address_is_never_rewritten_even_with_a_newer_candidate(tmp_path, monkeypatch, capsys):
    hvm = _load_hv(tmp_path, monkeypatch)
    with _daemon(None) as live:                                     # the stored address answers
        p = _write_peers(tmp_path, {"self": "a", "port": _free_port(),
                                    "peers": [{"id": DEV, "url": f"http://127.0.0.1:{live}"}]})
        hvm._record_peer_candidate(DEV, NEW)                        # newer than the file, elsewhere
        before = p.read_text()
        _doctor_fix(hvm, monkeypatch)
    out = capsys.readouterr().out
    assert p.read_text() == before
    assert "repointed" not in out
    assert "peer-address  1 stored peer address(es) answer" in out


# ── end to end: B moves, reaches A once, A's doctor --fix heals ───────────────────────────────────────

def test_b_moves_contacts_a_once_and_a_doctor_fix_repoints_it(tmp_path, monkeypatch, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _run(b, "key", "init")
    b_dev = (b / ".device-id").read_text().strip()
    b_seed = base64.b64decode((b / ".device-key").read_text().strip())
    _run(a, "key", "init")
    _run(a, "owner", "init")
    _run(a, "group", "admit", b_dev, "--principal", "b")           # real governance: B is admitted on A

    stale = f"http://127.0.0.1:{_free_port()}"                      # B's old address: nothing answers there
    cfg = {"self": "node-a", "port": _free_port(), "bind": "127.0.0.1", "sync_auth": "permissive",
           "x-future": {"keep": True},
           "peers": [{"id": b_dev, "label": "node-b", "url": stale, "x-note": "keep me"}]}
    peers = _write_peers(a, cfg)
    hva = _load_hv(a, monkeypatch)

    # doctor before B reaches out: unreachable, nothing verified, nothing to do
    _doctor_fix(hva, monkeypatch)
    assert json.loads(peers.read_text()) == cfg
    assert "repointed" not in capsys.readouterr().out

    # B's daemon round reaches A once, from B's new tailnet address (verified against A's real journal)
    with _daemon(NEW) as port:
        assert _get(port, "/sync/hello", _headers(b_seed, "GET", "/sync/hello")) == 200
    assert list(hva._load_peer_candidates()[b_dev]) == [NEW]

    # the preview changes nothing
    _doctor_fix(hva, monkeypatch, dry=True)
    out = capsys.readouterr().out
    assert "peer-address" in out and f"verified at {NEW}" in out and "would repoint" in out
    assert json.loads(peers.read_text()) == cfg

    # A's `hv doctor --fix` repoints B's entry: host only, every other key and the key order kept
    _doctor_fix(hva, monkeypatch)
    out = capsys.readouterr().out
    assert f"repointed peer {b_dev[:11]}…" in out
    port_b = stale.rsplit(":", 1)[1]
    want = dict(cfg, peers=[dict(cfg["peers"][0], url=f"http://{NEW}:{port_b}")])
    got = json.loads(peers.read_text())
    assert got == want and list(got) == list(cfg) and list(got["peers"][0]) == list(cfg["peers"][0])

    # nothing about it reached the journal (local operational state only)
    journal = "".join(f.read_text() for f in (a / "journal").glob("*.jsonl"))
    assert NEW not in journal and "peer_candidates" not in journal
