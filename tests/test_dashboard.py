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


def test_api_search_kind_is_exclusive_and_min_confidence_spares_raw_ideas(daemon, hive):
    # #209: kind=idea used to return every live fact and decision too. Each kind returns only its own type.
    hive.run("propose", "android needs a termux shim", "--source", "alice")
    def get(q):
        o = json.loads(_get(daemon, f"/api/search?q=android&{q}")[1])
        return {k: len(o[k]) for k in ("facts", "decisions", "ideas")}
    assert get("kind=fact") == {"facts": 1, "decisions": 0, "ideas": 0}
    assert get("kind=decision") == {"facts": 0, "decisions": 1, "ideas": 0}
    assert get("kind=idea") == {"facts": 0, "decisions": 0, "ideas": 1}
    assert get("kind=all") == {"facts": 1, "decisions": 1, "ideas": 0}      # a raw idea stays hidden under all
    # min_confidence filters facts only: a fact at 0.45 drops, the raw idea at 0.0 stays (David, h:0167a9c01a)
    assert get("kind=fact&min_confidence=0.9") == {"facts": 0, "decisions": 0, "ideas": 0}
    assert get("kind=idea&min_confidence=0.9") == {"facts": 0, "decisions": 0, "ideas": 1}


def test_api_search_unknown_kind_is_a_400(daemon):
    # the CLI's argparse `choices` refuse `--kind facts`; the API answers 400, not an empty 200
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(daemon, "/api/search?q=android&kind=facts")
    assert e.value.code == 400


def test_api_search_tag_scopes_results(daemon):
    # `installer` tags only the fact, so the decision must drop out — the 1.15 project-scoping payoff.
    o = json.loads(_get(daemon, "/api/search?tag=installer")[1])
    assert [f["id"] for f in o["facts"]] == [1]
    assert o["decisions"] == []


def test_api_search_paginates(daemon):
    o = json.loads(_get(daemon, "/api/search?q=&limit=1&offset=0")[1])
    assert o["facts_total"] >= 1 and o["decisions_total"] >= 1
    assert len(o["facts"]) <= 1
    # offset past the end → empty page, totals still reported
    o2 = json.loads(_get(daemon, "/api/search?q=&kind=fact&limit=50&offset=9999")[1])
    assert o2["facts"] == [] and o2["facts_total"] >= 1


def test_api_search_sort_and_status(daemon):
    # sort=recency must not error and returns the same shape
    o = json.loads(_get(daemon, "/api/search?q=&sort=recency&limit=50")[1])
    assert "facts" in o
    # a status filter the seed data doesn't hit still returns a well-formed page
    o2 = json.loads(_get(daemon, "/api/search?q=&kind=fact&status=contested")[1])
    assert o2["facts"] == [] and "facts_total" in o2


def test_api_audit_shape(daemon):
    a = json.loads(_get(daemon, "/api/audit")[1])
    for k in ("recheck", "stale", "duplicate", "contravened"):
        assert isinstance(a[k], int)
    assert "samples" in a


def test_api_tags_lists_distinct_tags(daemon):
    o = json.loads(_get(daemon, "/api/tags")[1])
    assert "tags" in o and isinstance(o["tags"], list)
    # seeded: fact tags android,installer + decision tag android
    assert "android" in o["tags"] and "installer" in o["tags"]
    assert o["tags"] == sorted(o["tags"])


def test_api_telemetry_shape(daemon):
    t = json.loads(_get(daemon, "/api/telemetry?limit=25&offset=0")[1])
    assert "totals" in t and "recent" in t and "by_day" in t and "by_model" in t
    assert "recent_total" in t and isinstance(t["recent"], list)


def test_api_peers_shape(daemon):
    o = json.loads(_get(daemon, "/api/peers")[1])
    assert "peers" in o and isinstance(o["peers"], list)


def test_api_peers_probe_off_is_fast(daemon):
    # probe=0 skips the network probe entirely — same shape, no hang.
    o = json.loads(_get(daemon, "/api/peers?probe=0")[1])
    assert "peers" in o and isinstance(o["peers"], list)


def test_api_status_runs_hv_commands(daemon):
    # /api/status shells out to hv whoami/stats/doctor — give doctor room to run.
    s = json.loads(_get(daemon, "/api/status", timeout=45)[1])
    for k in ("whoami", "stats", "doctor"):
        assert isinstance(s[k], str) and s[k]


def test_api_verify(daemon):
    v = json.loads(_get(daemon, "/api/verify")[1])
    assert "ok" in v and isinstance(v["lines"], list) and v["official_source"]


def test_api_telemetry_facets_and_sort(daemon):
    t = json.loads(_get(daemon, "/api/telemetry?sort=cost&limit=5")[1])
    assert "facets" in t and "projects" in t["facets"] and "nodes" in t["facets"]


def test_api_telemetry_hive_node_accounting(daemon):
    t = json.loads(_get(daemon, "/api/telemetry?scope=hive")[1])
    # single isolated node → it lists itself with an explicit status, and the admitted/online/
    # reporting counts are present so the offline gap is never silent.
    assert all(k in t for k in ("nodes", "admitted", "online", "reporting"))
    assert t["nodes"] and t["nodes"][0]["status"] == "self"


def test_api_related_by_tags(daemon):
    a = json.loads(_get(daemon, "/api/search?q=&kind=fact&limit=1")[1])
    fid = a["facts"][0]["id"]
    r = json.loads(_get(daemon, f"/api/related?kind=fact&id={fid}")[1])
    # the seed fact is tagged android,installer; related is by shared tags, excluding itself
    assert "related" in r and "android" in r["tags"]
    assert all(x["id"] != fid for x in r["related"])


def test_node_proxy_unknown_node_is_graceful(daemon):
    # A node id we can't resolve to a reachable peer must not error — it returns reachable:false.
    o = json.loads(_get(daemon, "/api/overview?node=k1:doesnotexist")[1])
    assert o.get("reachable") is False


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


# ── loopback hardening: Host check, cross-site POST guard, CSP, no script execution from data ────────

def _raw(port, method, path, headers=None, body=b""):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    h.setdefault("Host", f"127.0.0.1:{port}")
    if body:
        h["Content-Length"] = str(len(body))
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    c.endheaders(body or None)
    r = c.getresponse()
    out = (r.status, r.read(), dict(r.getheaders()))
    c.close()
    return out


def test_a_loopback_request_with_a_foreign_host_is_refused(daemon):
    for host in ("evil.example", f"evil.example:{daemon}", f"127.0.0.1.evil.example:{daemon}", "127.0.0.1:1"):
        for path in ("/api/overview", "/", "/api/search?q=android", "/sync/merkle-root"):
            st, body, _ = _raw(daemon, "GET", path, {"Host": host})
            assert st == 421, (host, path, st)
            assert b"android" not in body
        assert _raw(daemon, "POST", "/sync/ingest", {"Host": host}, b"{}")[0] == 421
    for host in (f"127.0.0.1:{daemon}", f"localhost:{daemon}", "localhost", f"[::1]:{daemon}"):
        assert _raw(daemon, "GET", "/api/overview", {"Host": host})[0] == 200, host


def _tok(hive):
    return (hive.home / ".csrf-token").read_text().strip()


def test_a_cross_site_post_to_ingest_is_refused(daemon, hive):
    body = json.dumps({"entries": []}).encode()
    # a browser-sent request carries Origin / Sec-Fetch-Site and cannot carry the token
    for hdrs in ({"Origin": "https://evil.example"},
                 {"Origin": "null"},
                 {"Sec-Fetch-Site": "cross-site"},
                 {"Sec-Fetch-Site": "same-site"},
                 {"Origin": f"http://127.0.0.1:{daemon}"},                       # same-origin but no token
                 {"Origin": "https://evil.example", "Hive-CSRF": "wrong"}):
        st, _, _ = _raw(daemon, "POST", "/sync/ingest", hdrs, body)
        assert st == 403, hdrs
    # a foreign Origin is refused even when the token is right
    assert _raw(daemon, "POST", "/sync/ingest", {"Origin": "https://evil.example", "Hive-CSRF": _tok(hive)}, body)[0] == 403
    # the local client with the token, and no browser labels, is accepted
    st, out, _ = _raw(daemon, "POST", "/sync/ingest", {"Hive-CSRF": _tok(hive)}, body)
    assert st == 200 and json.loads(out)["accepted"] == 0
    st, _, _ = _raw(daemon, "POST", "/sync/ingest",
                    {"Hive-CSRF": _tok(hive), "Origin": f"http://127.0.0.1:{daemon}", "Sec-Fetch-Site": "same-origin"}, body)
    assert st == 200


def test_the_dashboard_is_served_with_a_content_security_policy(daemon):
    st, body, h = _raw(daemon, "GET", "/")
    assert st == 200 and h.get("Content-Security-Policy") == "default-src 'self'"
    assert h.get("X-Content-Type-Options") == "nosniff"
    st, js, h = _raw(daemon, "GET", "/app.js")
    assert st == 200 and "javascript" in h["Content-Type"] and b"function html(" in js
    assert _raw(daemon, "GET", "/app.css")[0] == 200


def test_the_dashboard_builds_its_dom_without_executing_data():
    """A tag, fact or peer string is only ever a text node or an attribute value: no markup is built from data, and
    no script runs from an attribute (`new Function` over `data-onclick` was the sink for a tag with a quote)."""
    import re
    d = PROJECT / "dashboard"
    page, js = (d / "index.html").read_text(), (d / "app.js").read_text()
    assert "<script src=\"app.js\"></script>" in page
    assert not re.search(r"<script(?![^>]*\bsrc=)", page) and "<style" not in page      # nothing inline for the CSP to block
    assert not re.search(r"\son[a-z]+\s*=|\sstyle\s*=", page)
    for sink in ("new Function", "eval(", "data-onclick", "javascript:", "document.write", "insertAdjacentHTML", "outerHTML", "esc("):
        assert sink not in js, sink
    assert re.findall(r"\.innerHTML\s*=", js) == [".innerHTML="]                         # only html()'s own static template
    assert "t.innerHTML=s;" in js
    # the quote that broke out of the old data-onclick string now lands in a text node and a closure
    assert "onclick=${e=>{e.stopPropagation();filterTag(t);}}" in js and ">${t}</span>" in js
    css = (d / "app.css").read_text()
    assert "url(http" not in css and "@import" not in css
