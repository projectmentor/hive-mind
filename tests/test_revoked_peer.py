"""SECREV B6 (hive-mind-private #54): a revoked or purged device must not stay a listed peer.

Node A is admitted and lists device R in its .peers.json. R is then revoked. The signed request envelope
names no responder and the nonce cache is per node, so every signed read A sends R is a bearer token R can
replay to a third node C inside the freshness window. A's fix is node-local: once a revoke or purge
projects, A drops R from .peers.json and sends it nothing. No wire format changes.
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from test_peer_address import _free_port, _pin_hive, _run  # noqa: E402

os.environ["HIVE_HOME"] = tempfile.mkdtemp(prefix="hive-revpeer-")   # never the live hive

import hive_sync_daemon as d  # noqa: E402
import sync_common  # noqa: E402
from test_hello_sig import _hvmod, _req, _serve_as  # noqa: E402
from test_peer_address import _daemon  # noqa: E402


@contextlib.contextmanager
def _capture():
    """Stand-in for the revoked device R's daemon: answers a differing root so A goes on to ask for more,
    and records every request (path, headers) A sends it."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, {k: v for k, v in self.headers.items()}))
            body = json.dumps({"root_hash": "sha256:" + "0" * 64}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], seen
    finally:
        srv.shutdown()
        srv.server_close()


def _hive(tmp_path, revoke):
    """A (owner, admitted), R (admitted, then `revoke`d or `purge`d), C (a copy of A's journal)."""
    a, r, c = (tmp_path / n for n in "arc")
    for h in (a, r, c):
        h.mkdir()
        _run(h, "config", "identity", "init")
    a_dev, r_dev, c_dev = ((h / ".device-id").read_text().strip() for h in (a, r, c))
    _run(a, "owner", "init")
    for dev, who in ((a_dev, "a"), (r_dev, "r"), (c_dev, "c")):
        _run(a, "group", "admit", dev, "--principal", who)
    _run(a, "remember", "a fact so the roots differ")
    if revoke:
        _run(a, "group", revoke, r_dev)
    shutil.copytree(a / "journal", c / "journal", dirs_exist_ok=True)
    return a, c, a_dev, r_dev


@pytest.mark.parametrize("revoke", ["revoke", "purge"])
@pytest.mark.parametrize("named_by", ["device", "label"])
def test_a_revoked_device_receives_no_signed_read_from_a_peer_that_listed_it(tmp_path, monkeypatch, revoke, named_by):
    a, c, a_dev, r_dev = _hive(tmp_path, revoke)
    with _capture() as (pr, seen):
        entry = {"id": r_dev if named_by == "device" else "r-box", "url": f"http://127.0.0.1:{pr}"}
        (a / ".peers.json").write_text(json.dumps({"self": "a", "port": _free_port(), "peers": [entry]}))
        if named_by == "label":
            # The label names no device, so the entry is judged by the address the device was seen at.
            (a / ".peer_candidates.json").write_text(json.dumps(
                {"devices": {r_dev: {"127.0.0.1": {"first_seen": 1, "last_seen": 2, "via": "outbound"}}}}))
        out = _run(a, "sync", "now").stdout
        print(out)
        # What R captured is what it could replay. Nothing signed by A may reach it.
        signed = [(p, h) for p, h in seen if h.get("Hive-Auth-Device") == a_dev]
        assert signed == [], f"A sent the revoked device {len(signed)} signed read(s): {[p for p, _ in signed]}"
        cfg = json.loads((a / ".peers.json").read_text())
        assert cfg["peers"] == [], cfg


def test_a_captured_read_replays_to_a_third_node_when_the_peer_is_not_dropped(tmp_path, monkeypatch):
    """The reproduction: whatever R holds of A's is accepted by C. Kept as a regression guard that the
    envelope really is audience-free (the residual THREAT_MODEL states)."""
    a, c, a_dev, r_dev = _hive(tmp_path, None)          # R still admitted: A is allowed to talk to it
    with _capture() as (pr, seen):
        (a / ".peers.json").write_text(json.dumps({"self": "a", "port": _free_port(),
                                                   "peers": [{"id": r_dev, "url": f"http://127.0.0.1:{pr}"}]}))
        _run(a, "sync", "now")
        got = [h for p, h in seen if h.get("Hive-Auth-Device") == a_dev and p.startswith("/sync/hello")]
        assert got, seen
        hvc = _hvmod(c, monkeypatch)
        _serve_as(hvc, monkeypatch)
        monkeypatch.setenv("HIVE_SYNC_AUTH", "enforce")
        with _daemon("100.64.0.9") as pc:
            hdr = {k: v for k, v in got[0].items() if k.startswith("Hive-Auth-")}
            status, _ = _req(pc, "/sync/hello", hdr)
    assert status == 200


@pytest.mark.parametrize("named_by", ["device", "label"])
def test_a_live_device_at_the_same_host_as_an_ended_one_is_kept_and_synced(tmp_path, monkeypatch, named_by):
    """A reinstalled machine: a new device id at the old address. Both identities were verified at one host;
    the entry for the live one must survive the prune and still be synced."""
    a, c, a_dev, r_dev = _hive(tmp_path, "revoke")
    live_dev = (c / ".device-id").read_text().strip()      # admitted, never revoked
    with _capture() as (pr, seen):
        entry = {"id": live_dev if named_by == "device" else "r-box", "url": f"http://127.0.0.1:{pr}"}
        (a / ".peers.json").write_text(json.dumps({"self": "a", "port": _free_port(), "peers": [entry]}))
        seen_at = {"first_seen": 1, "last_seen": 2, "via": "outbound"}
        (a / ".peer_candidates.json").write_text(json.dumps(
            {"devices": {r_dev: {"127.0.0.1": seen_at}, live_dev: {"127.0.0.1": seen_at}}}))
        _run(a, "sync", "now")
        cfg = json.loads((a / ".peers.json").read_text())
        assert cfg["peers"] == [entry], cfg
        assert [p for p, h in seen if h.get("Hive-Auth-Device") == a_dev], "the live peer was not synced"


def test_peer_ended_device_rules(tmp_path, monkeypatch):
    hv = _hvmod(tmp_path, monkeypatch)
    r, n = "k1:" + "a" * 16, "k1:" + "b" * 16
    seen = {"127.0.0.1": {"first_seen": 1, "last_seen": 2}}
    url = "http://127.0.0.1:1"
    ended = {r}
    # id is a device id: decided on id alone, whatever the host history says.
    assert hv._peer_ended_device({"id": n, "url": url}, ended, {r: seen, n: seen}) is None
    assert hv._peer_ended_device({"id": r, "url": "http://10.9.9.9:1"}, ended, {}) == r
    # No device id: matched by host only when the ended device is the only one verified there.
    assert hv._peer_ended_device({"id": "box", "url": url}, ended, {r: seen}) == r
    assert hv._peer_ended_device({"id": "box", "url": url}, ended, {r: seen, n: seen}, None, {n}) is None
    assert hv._peer_ended_device({"url": url}, ended, {n: seen}) is None


def test_peer_ended_device_matches_the_join_request_url(tmp_path, monkeypatch):
    """`group admit --principal` seeds the entry from the join-request URL with the principal as its id: no
    device id and, with no candidate sighting, no host history. The ended device's join-request URL names it."""
    hv = _hvmod(tmp_path, monkeypatch)
    r, n = "k1:" + "a" * 16, "k1:" + "b" * 16
    url = "http://100.64.0.7:8765"
    ended = {r}
    entry = {"id": "r", "url": url}
    assert hv._peer_ended_device(entry, ended, {}, {r: url}) == r
    assert hv._peer_ended_device({"id": "r", "url": url + "/"}, ended, {}, {r: url}) == r
    assert hv._peer_ended_device({"id": "r", "url": "http://100.64.0.8:8765"}, ended, {}, {r: url}) is None
    # A live device verified at the host, or advertising it, keeps the entry (a reinstall at the old address).
    seen = {"100.64.0.7": {"first_seen": 1, "last_seen": 2}}
    adm = {n}
    assert hv._peer_ended_device(entry, ended, {n: seen}, {r: url}, adm) is None
    assert hv._peer_ended_device(entry, ended, {}, {r: url, n: "http://100.64.0.7:9999"}, adm) is None
    assert hv._peer_ended_device(entry, ended, {}, {r: url, n: "http://100.64.0.9:8765"}, adm) == r
    # The same claims from a device outside `admitted` keep nothing: this node would still send the ended one reads.
    assert hv._peer_ended_device(entry, ended, {n: seen}, {r: url}) == r
    assert hv._peer_ended_device(entry, ended, {}, {r: url, n: "http://100.64.0.7:9999"}) == r
    assert hv._peer_ended_device(entry, ended, {}, {r: url, n: "http://100.64.0.7:9999"}, {"k1:" + "c" * 16}) == r
    # An entry that names a live device id is still decided on the id alone.
    assert hv._peer_ended_device({"id": n, "url": url}, ended, {}, {r: url}) is None


def _join(device, seq, ts, url):
    return {"type": "governance", "node_id": device, "seq": seq, "timestamp": ts,
            "payload": {"action": "join-request", "device_id": device, "url": url}}


def test_a_join_request_written_after_the_device_was_ended_leaves_its_url_where_it_was(tmp_path, monkeypatch):
    """The principal-labelled entry is matched by the ended device's join-request URL. A join-request the ended
    device writes after the revoke or purge must not move that URL (last-wins would let it re-point the match)."""
    hv = _hvmod(tmp_path, monkeypatch)
    r, n = "k1:" + "a" * 16, "k1:" + "b" * 16
    old, new = "http://100.64.0.7:9876", "http://100.64.0.8:9876"
    journal = [_join(r, 1, "2026-01-01T00:00:00Z", old), _join(n, 1, "2026-01-01T00:00:01Z", old),
               _join(r, 2, "2026-01-03T00:00:00Z", new)]
    monkeypatch.setattr(hv.merkle, "read_all_entries", lambda _d: journal)
    cut = ("2026-01-02T00:00:00Z", "o1:owner", 5)
    assert hv._join_request_urls() == {r: new, n: old}                       # no ending act: last wins
    assert hv._join_request_urls({r: cut}) == {r: old, n: old}               # after the act: ignored
    assert hv._join_request_urls({r: ("2026-01-03T00:00:00Z", r, 2)}) == {r: new, n: old}   # at the act: kept
    entry = {"id": "bob", "url": old}
    assert hv._peer_ended_device(entry, {r}, {}, hv._join_request_urls()) is None   # re-pointed: the row is missed
    assert hv._peer_ended_device(entry, {r}, {}, hv._join_request_urls({r: cut})) == r


def test_a_join_request_this_node_received_after_the_ending_act_leaves_its_url_where_it_was(tmp_path, monkeypatch):
    """A join-request stamped before the act but received after it (a backdated stamp) must not re-point the URL."""
    hv = _hvmod(tmp_path, monkeypatch)
    r = "k1:" + "a" * 16
    old, new = "http://100.64.0.7:9876", "http://100.64.0.8:9876"
    journal = [_join(r, 1, "2026-01-01T00:00:00Z", old), _join(r, 2, "2026-01-01T12:00:00Z", new)]   # both before the cut
    monkeypatch.setattr(hv.merkle, "read_all_entries", lambda _d: journal)
    cut = ("2026-01-02T00:00:00Z", "o1:owner", 5)
    assert hv._join_request_urls({r: cut}) == {r: new}                       # no arrival records: the stamp rule keeps it
    (hv.HIVE_HOME / ".arrivals.jsonl").write_text(
        '{"n": "%s", "s": 1, "at": "2026-01-01T00:00:05Z"}\n{"n": "%s", "s": 2, "at": "2026-01-02T00:00:09Z"}\n' % (r, r))
    assert hv._join_request_urls({r: cut}) == {r: old}                       # received after the act: ignored
    assert hv._join_request_urls() == {r: new}                               # no ending act: last wins


def test_the_projection_records_where_each_device_was_ended(tmp_path, monkeypatch):
    a, c, a_dev, r_dev = _hive(tmp_path, "revoke")
    hv = _hvmod(a, monkeypatch)
    gov = hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))
    assert set(gov["ended_at"]) == {r_dev} and gov["ended_at"][r_dev] > ("", "", 0)
    _run(a, "group", "admit", r_dev, "--principal", "r")
    gov = hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))
    assert gov["ended_at"] == {}                                             # re-admitted: no longer ended
    _run(a, "group", "purge", r_dev)
    gov = hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))
    assert set(gov["ended_at"]) == {r_dev}
