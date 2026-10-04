"""2.1 plan PR 4 (A1, A3, A4): the module API skeleton, `/v1`, read routes only.

Drives the real `ModuleHandler` over loopback HTTP against a temp hive whose journal is built with real
signatures: an owner, a plain admitted device, and two devices the owner admitted as modules' (`hwatch` and
`other`). Requests are signed with the sync daemon's Hive-Auth envelope. The quota, write and rate-limit
tests land with the write route (PR 5).
"""
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import hive_module_api as api  # noqa: E402
import hive_sync_daemon as daemon  # noqa: E402
import sync_common  # noqa: E402
import test_links as TL  # noqa: E402
from test_unforget import _act  # noqa: E402

T = "2026-01-05T00:00:%02dZ"


class Hive:
    def __init__(self, tmp_path, monkeypatch, with_owner=True):
        self.hv = hv = TL._loadhv(tmp_path, monkeypatch)
        monkeypatch.setattr(daemon, "hv", hv)
        self.home = tmp_path
        self.owner, (self.plain, self.mod, self.other), base = TL._owned_hive(hv)
        oseed, opub, _ = self.owner
        o = (oseed, opub)
        base += [TL._gov(hv, {"action": "admit", "device_id": self.mod["id"], "principal": "op", "module": "hwatch"},
                         oseed, opub, "2026-01-01T00:00:09Z", 5),
                 TL._gov(hv, {"action": "admit", "device_id": self.other["id"], "principal": "op", "module": "other"},
                         oseed, opub, "2026-01-01T00:00:10Z", 6)]
        self.fact = TL._fact(hv, self.plain, "the backup runs at 02:00", T % 1)
        self.gone = TL._fact(hv, self.plain, "a secret the owner forgot", T % 2)
        forget = _act(hv, self.plain, self.gone, T % 3, owner=o)
        self.gone_v = TL._entry(hv, self.plain, "fact", {"content": "a volatile secret the owner forgot", "tags": ["volatile"],
                                                         "importance": 0.5, "source": "manual"}, T % 4)
        forget_v = _act(hv, self.plain, self.gone_v, T % 5, owner=o)
        cfg = [TL._entry(hv, self.plain, "governance", {"action": "set-config", "key": k, "value": v}, T % (10 + i), owner=o)
               for i, (k, v) in enumerate([("x-hwatch:poll", "30s"), ("x-other:poll", "5m")])]
        self.idea = TL._entry(hv, self.plain, "idea", {"content": "the slow backup is the disk", "tags": [], "source": "manual",
                                                       "channel": "introspect"}, T % 6)
        self.entries = base + [self.fact, self.gone, forget, self.gone_v, forget_v, self.idea] + cfg
        if not with_owner:
            self.entries = []
        TL._project(hv, tmp_path, self.entries).close()
        self.srv = api.make_module_server(0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def get(self, path, dev="mod", method="GET", sign=True, headers=None, body=b""):
        seed = getattr(self, dev)["seed"] if isinstance(dev, str) else dev
        p, _, query = path.partition("?")
        h = signed(seed, method, p, query, body) if sign else {}
        h.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=h, method=method,
                                     data=body or None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def signed(seed, method, path, query, body=b""):
    return sync_common.signed_request_headers(seed, method, path, query, body)


@pytest.fixture
def hive(tmp_path, monkeypatch):
    h = Hive(tmp_path, monkeypatch)
    yield h
    h.close()


def test_route_table_is_exactly_the_documented_set():
    assert sorted(api.ROUTES) == ["/v1/", "/v1/feed", "/v1/item", "/v1/search"]
    assert api.API_VERSIONS == ["v1"]


def test_no_route_names_governance_or_owner_state():
    """A3: the module API is data plane only, and the write routes are not here yet."""
    for path in api.ROUTES:
        assert not re.search(r"governance|owner|group|config|capsule|cell|comb|escrow|entries", path), path


def test_root_names_the_caller_and_serves_only_its_own_module_config(hive):
    code, body = hive.get("/v1/")
    assert code == 200
    assert body == {"api": ["v1"], "contract": hive.hv.CONTRACT_VERSION, "hive_id": "h1", "device_id": hive.mod["id"],
                    "module": "hwatch", "quota": None, "config": {"x-hwatch:poll": "30s"}}
    code, other = hive.get("/v1/", dev="other")
    assert other["module"] == "other" and other["config"] == {"x-other:poll": "5m"}


def test_an_unknown_route_is_404_and_an_unsupported_version_names_the_supported_ones(hive):
    assert hive.get("/v1/governance")[0] == 404
    assert hive.get("/v1/entries")[0] == 404                      # PR 5
    code, body = hive.get("/v2/")
    assert code == 404 and body["supported"] == ["v1"]
    assert hive.get("/v2/feed")[1]["supported"] == ["v1"]
    assert hive.get("/sync/hello")[0] == 404 and hive.get("/api/overview")[0] == 404


def test_every_request_is_authenticated_the_right_way(hive):
    assert hive.get("/v1/", sign=False)[0] == 401
    h = signed(hive.mod["seed"], "GET", "/v1/", "")
    assert hive.get("/v1/", sign=False, headers=h)[0] == 200
    code, body = hive.get("/v1/", sign=False, headers=h)           # the same envelope again
    assert (code, body["detail"]) == (401, "replayed-nonce")
    # stale timestamp
    seed, ts = hive.mod["seed"], int(time.time()) - 3600
    stale = _signed_at(seed, "/v1/", "", ts)
    code, body = hive.get("/v1/", sign=False, headers=stale)
    assert (code, body["detail"]) == (401, "stale-timestamp")
    # signed for another path, and for another query
    wrong = signed(seed, "GET", "/v1/feed", "")
    assert hive.get("/v1/search", sign=False, headers=wrong)[1]["detail"] == "bad-signature"
    wrong = signed(seed, "GET", "/v1/search", "q=a")
    assert hive.get("/v1/search?q=b", sign=False, headers=wrong)[0] == 401


def _signed_at(seed, path, query, ts):
    import base64
    import ed25519
    pub = ed25519.pub_from_seed(seed)
    nonce = os.urandom(16).hex()
    sig = ed25519.sign(sync_common.sync_signing_bytes("GET", path, query, b"", ts, nonce), seed)
    return {"Hive-Auth-Alg": sync_common.HIVE_AUTH_ALG, "Hive-Auth-Device": daemon.hv._device_id_for_pub(pub),
            "Hive-Auth-Pub": base64.b64encode(pub).decode(), "Hive-Auth-Ts": str(ts), "Hive-Auth-Nonce": nonce,
            "Hive-Auth-Sig": base64.b64encode(sig).decode()}


def test_a_device_that_is_not_admitted_or_not_a_module_is_403(hive):
    stranger = os.urandom(32)
    code, body = hive.get("/v1/", dev=stranger)
    assert (code, body["detail"]) == (403, "not-admitted")
    code, body = hive.get("/v1/", dev="plain")                    # admitted, but an ordinary device
    assert (code, body["detail"]) == (403, "not-a-module-device")
    code, body = hive.get("/v1/feed", dev="plain")
    assert code == 403 and "entries" not in body


def test_a_hive_with_no_owner_refuses_every_module_request(tmp_path, monkeypatch):
    (tmp_path / "x").mkdir()
    h = Hive(tmp_path / "x", monkeypatch, with_owner=False)
    try:
        code, body = h.get("/v1/", dev=h.mod["seed"])
        assert code == 403 and body.get("detail") == "no-owner", body
    finally:
        h.close()


def test_the_api_is_read_only(hive):
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        code, body = hive.get("/v1/feed", method=m, body=b"{}")
        assert code == 405, m
    for m in ("HEAD", "OPTIONS", "TRACE", "PROPFIND", "FOO", "get"):      # any verb, JSON 405 (not the stdlib's HTML 501)
        conn = http.client.HTTPConnection("127.0.0.1", hive.port, timeout=10)
        conn.request(m, "/v1/feed", headers=signed(hive.mod["seed"], m, "/v1/feed", ""))
        r = conn.getresponse()
        assert r.status == 405 and r.getheader("Content-Type") == "application/json", m
        assert r.getheader("Allow") == "GET", m
        conn.close()
    assert hive.get("/v1/entries", method="POST", body=b"{}")[0] == 405
    assert len(hive.hv.feed_page({}, 1000)["entries"]) == len(hive.entries)     # nothing was written


def test_it_binds_loopback_whatever_the_environment_or_peers_file_says(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_BIND", "0.0.0.0")
    (tmp_path / ".peers.json").write_text(json.dumps({"bind": "0.0.0.0", "port": 9876, "peers": []}))
    srv = api.make_module_server(0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()
    assert api.BIND == "127.0.0.1"


def test_a_peer_that_is_not_loopback_is_refused():
    assert api.is_loopback_peer("127.0.0.1") and api.is_loopback_peer("::1") and api.is_loopback_peer("127.3.2.1")
    for ip in ("100.64.0.7", "192.168.1.5", "10.0.0.2", "0.0.0.0", "", None, "fe80::1"):
        assert not api.is_loopback_peer(ip), ip


def test_feed_is_hv_feed(hive):
    code, body = hive.get("/v1/feed")
    assert code == 200 and body == json.loads(json.dumps(hive.hv.feed_page({}, 200)))
    page = hive.get("/v1/feed?limit=3")[1]
    assert len(page["entries"]) == 3 and page["more"] is True
    rest = hive.get("/v1/feed?after=" + page["cursor"])[1]
    assert rest["entries"][0]["seq"] > 0 and len(page["entries"]) + len(rest["entries"]) == len(body["entries"])
    assert hive.get("/v1/feed?after=garbage")[0] == 400
    assert hive.get("/v1/feed?limit=x")[0] == 400


def test_search_and_item_read_the_projection_and_never_hand_over_forgotten_text(hive):
    code, body = hive.get("/v1/search?q=backup")
    assert code == 200 and [f["content"] for f in body["facts"]] == ["the backup runs at 02:00"]
    code, body = hive.get("/v1/search")
    assert "a secret the owner forgot" not in json.dumps(body)
    assert hive.get("/v1/search?status=forgotten")[0] == 400
    for q in ("min_confidence=-1", "min_confidence=-5", "min_confidence=nan", "min_confidence=-inf", "status=volatile",
              "status=volatile&min_confidence=0", "status=contested", "kind=fact&sort=recency"):
        code, body = hive.get("/v1/search?" + q)
        assert code in (200, 400), q
        assert "secret" not in json.dumps(body), q
    assert hive.get("/v1/search?min_confidence=-1")[0] == 400 and hive.get("/v1/search?min_confidence=nan")[0] == 400
    assert hive.get("/v1/search?status=volatile")[1]["facts"] == []
    assert hive.get("/v1/search")[1]["facts_total"] == 1                # the forgotten facts are not counted either
    # a forgotten fact that the projection also marks contested is still withheld from the contested branch
    hive.hv.get_conn().execute("UPDATE facts SET contested = 1 WHERE content LIKE '%secret%'")
    hive.hv.get_conn().commit()
    code, body = hive.get("/v1/search?status=contested")
    assert code == 200 and body["facts"] == [] and body["facts_total"] == 0
    ref = f"{hive.fact['node_id']}:{hive.fact['seq']}"
    code, body = hive.get("/v1/item?id=" + ref)
    assert code == 200 and body["item"]["content"] == "the backup runs at 02:00"
    gone = f"{hive.gone['node_id']}:{hive.gone['seq']}"
    code, body = hive.get("/v1/item?id=" + gone)
    assert code == 404 and "secret" not in json.dumps(body)
    assert hive.get("/v1/item")[0] == 400
    assert hive.get("/v1/item?id=h:nope")[0] == 404
    for bad in ("kind=bogus", "sort=bogus", "min_confidence=x"):
        assert hive.get("/v1/search?" + bad)[0] == 400, bad
    code, body = hive.get("/v1/search?kind=idea")
    assert code == 200 and [i["content"] for i in body["ideas"]] == ["the slow backup is the disk"]
    assert hive.get("/v1/search?limit=100000")[0] == 200            # clamped, not refused


def test_the_listener_is_started_by_the_daemon_and_never_blocks_it(monkeypatch):
    src = (PROJECT / "hive_sync_daemon.py").read_text()
    assert src.count("modules = _start_module_api()") == 2                  # serve_forever and run_daemon
    monkeypatch.setattr(api, "make_module_server", lambda port=None: (_ for _ in ()).throw(OSError("in use")))
    assert api.start_module_api() is None                         # a taken port is reported, not raised
