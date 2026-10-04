"""2.1 plan PR 5 (A2, A3, A4, A6): `POST /v1/entries`, `GET /v1/tip`, quotas and rate limits, the reference signer.

Same temp hive as `test_module_api.py` (an owner, a plain device, and two modules' devices, `hwatch` and
`other`, both under the operator's principal `op`). A module's entry is signed by the module, here through
`hive_module_client`, the reference signer, which imports no `hv`.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import hive_module_api as api  # noqa: E402
import hive_module_client as client  # noqa: E402
import hive_sync_daemon as daemon  # noqa: E402
import test_links as TL  # noqa: E402
from test_module_api import Hive, hive, signed  # noqa: E402,F401

SRC = "x-hwatch"


def fact(content, **kw):
    return dict({"content": content, "tags": [], "importance": 0.5, "source": SRC}, **kw)


def post(h, dev, etype, payload, tip=True, ts=None, over=None):
    """Sign the next entry for module device `dev` ('mod' or 'other') on the live tip and POST it. `over` changes
    envelope fields, and the entry is signed again over them, so the gate under test is the shape, not the signature."""
    seed = getattr(h, dev)["seed"]
    entry = client.build_entry(seed, etype, payload, h.get("/v1/tip", dev=dev)[1] if tip else None, ts or "2026-01-06T00:00:00.000+00:00")
    if over:
        entry = client.sign_entry({k: v for k, v in dict(entry, **over).items() if k not in ("sig", "pub")}, seed)
    return h.get("/v1/entries", dev=dev, method="POST", body=json.dumps(entry).encode()), entry


def journal(h):
    return h.hv.merkle.read_all_entries(h.hv.JOURNAL_DIR)


def conf(h, content, entries=None):
    entries = entries if entries is not None else journal(h)
    gov = h.hv._governance_state(entries)
    return h.hv._content_confidence(h.hv._content_evidence(entries, gov)[content], gov)


# ── the write, and what it is attributed to (A2) ────────────────────────────────────────────────────────

def test_a_module_entry_is_attributed_to_the_modules_node_id_not_the_hosts(hive):
    (code, body), entry = post(hive, "mod", "fact", fact("hwatch saw the deploy finish"))
    assert code == 200 and body["accepted"] == 1 and body["ref"] == f"{hive.mod['id']}:1"
    assert body["sid"] == hive.hv._short_id(hive.mod["id"], 1) and body["tip"]["seq"] == 1
    held = [e for e in journal(hive) if e["node_id"] == hive.mod["id"]]
    assert held == [entry]                                           # byte for byte what the module signed
    assert hive.mod["id"] != hive.hv.NODE_ID and hive.mod["id"] != hive.plain["id"]
    feed = hive.get("/v1/feed")[1]["entries"]
    assert [e["node_id"] for e in feed if e["payload"].get("content") == "hwatch saw the deploy finish"] == [hive.mod["id"]]
    row = hive.hv.get_conn().execute("SELECT node_id FROM journal_index WHERE sid = ?", (body["sid"],)).fetchone()
    assert row["node_id"] == hive.mod["id"]                           # the projection was rebuilt, with the module as author


def test_revoking_the_modules_device_makes_its_entries_stop_counting(hive):
    content = "the vendor rotates keys every 90 days"
    hive.entries.append(TL._fact(hive.hv, hive.plain, content, "2026-01-05T01:00:00Z"))
    TL._project(hive.hv, hive.home, hive.entries).close()
    assert post(hive, "mod", "fact", fact(content))[0][0] == 200      # a second identity corroborates
    entries = journal(hive)
    before = conf(hive, content, entries)
    oseed, opub, _ = hive.owner
    revoke = TL._gov(hive.hv, {"action": "revoke", "device_id": hive.mod["id"]}, oseed, opub, "2026-01-07T00:00:00Z", 7)
    after = conf(hive, content, entries + [revoke])
    assert after < before                                             # the module no longer counts
    assert round(after, 6) == round(hive.hv._confidence_for(1.0), 6)  # only the plain device is left
    # ...and a revoked device cannot write at all: its request is refused at the door
    TL._project(hive.hv, hive.home, entries + [revoke]).close()
    assert post(hive, "mod", "fact", fact("one more"))[0][0] == 403


def test_the_same_principal_means_cap_self_bounds_corroboration(hive):
    hv = hive.hv
    oseed, opub, _ = hive.owner
    same, apart = "the cache warms in four minutes", "the cache is flushed nightly"
    extra, seq = [], 8
    for n in range(3):
        for content, principal in ((same, "op"), (apart, f"q{n}")):       # three more voices: the operator's own, or three others
            dev = TL._device(hv)
            extra += [TL._gov(hv, {"action": "admit", "device_id": dev["id"], "principal": principal}, oseed, opub,
                              "2026-01-02T00:00:00Z", seq), TL._fact(hv, dev, content, f"2026-01-05T02:00:0{n}Z")]
            seq += 1
    hive.entries += extra
    TL._project(hv, hive.home, hive.entries).close()
    assert post(hive, "mod", "fact", fact(same))[0][0] == 200 and post(hive, "mod", "fact", fact(apart))[0][0] == 200
    # four identities in ONE principal (the module's is the operator's `op`) are one voice, bounded by cap_self;
    # in four principals they add up past it
    assert conf(hive, same) <= hv.CONF_CAP_SELF + 1e-9
    assert conf(hive, apart) > hv.CONF_CAP_SELF + 1e-9


def test_the_tip_is_the_callers_own_chain_and_a_client_can_walk_it(hive):
    assert hive.get("/v1/tip")[1] == {"node_id": hive.mod["id"], "seq": 0, "hash": "sha256:genesis"}
    (_, b1), e1 = post(hive, "mod", "fact", fact("one"))
    (_, b2), e2 = post(hive, "mod", "fact", fact("two"))
    assert e2["prev_hash"] == hive.hv.compute_hash(e1) and b2["tip"] == hive.get("/v1/tip")[1]
    assert b2["tip"] == {"node_id": hive.mod["id"], "seq": 2, "hash": hive.hv.compute_hash(e2)}
    assert hive.get("/v1/tip", dev="other")[1]["seq"] == 0            # not shared
    assert hive.get("/v1/tip", method="POST", body=b"{}")[0] == 405


# ── idempotent, conflict-safe append ──────────────────────────────────────────────────────────────────────

def test_a_byte_identical_retry_is_200_and_a_resigned_body_at_the_held_seq_is_409(hive):
    (code, first), entry = post(hive, "mod", "fact", fact("retry me"))
    assert code == 200 and first["accepted"] == 1
    again = hive.get("/v1/entries", method="POST", body=json.dumps(entry).encode())
    assert again[0] == 200 and again[1]["accepted"] == 0 and again[1]["duplicate"] is True
    other = client.build_entry(hive.mod["seed"], "fact", fact("a different body"), None, entry["timestamp"])
    code, body = hive.get("/v1/entries", method="POST", body=json.dumps(other).encode())
    assert code == 409 and body["tip"]["seq"] == 1 and "different entry" in body["error"]
    assert sum(1 for e in journal(hive) if e["node_id"] == hive.mod["id"]) == 1


def test_an_entry_built_on_a_stale_tip_is_409_with_the_tip_and_a_fresh_one_lands_as_a_second_copy(hive):
    (_, _), e1 = post(hive, "mod", "fact", fact("first"))
    stale = client.build_entry(hive.mod["seed"], "fact", fact("racer"), {"seq": 0, "hash": "sha256:genesis"}, "2026-01-06T00:00:01.000+00:00")
    stale["seq"] = 2                                                  # right seq, wrong prev_hash
    stale = client.sign_entry({k: v for k, v in stale.items() if k not in ("sig", "pub")}, hive.mod["seed"])
    code, body = hive.get("/v1/entries", method="POST", body=json.dumps(stale).encode())
    assert code == 409 and body["tip"] == {"node_id": hive.mod["id"], "seq": 1, "hash": hive.hv.compute_hash(e1)}
    gap = client.build_entry(hive.mod["seed"], "fact", fact("gap"), {"seq": 4, "hash": body["tip"]["hash"]})
    assert hive.get("/v1/entries", method="POST", body=json.dumps(gap).encode())[0] == 409      # no gaps in a module's chain
    (code, again), _ = post(hive, "mod", "fact", fact("first"))      # same content on a fresh tip: a duplicate, not a loss
    assert code == 200 and again["accepted"] == 1 and again["tip"]["seq"] == 2


def test_two_posts_racing_for_one_seq_land_exactly_one(hive):
    seed, tip = hive.mod["seed"], hive.get("/v1/tip")[1]
    bodies = [json.dumps(client.build_entry(seed, "fact", fact(f"racer {i}"), tip, "2026-01-06T00:00:00.000+00:00")).encode() for i in range(2)]
    out = []
    ts = [threading.Thread(target=lambda b=b: out.append(hive.get("/v1/entries", method="POST", body=b)[0])) for b in bodies]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [200, 409]
    assert sum(1 for e in journal(hive) if e["node_id"] == hive.mod["id"]) == 1


# ── what a module may write (A3) and the names it may use (A4) ────────────────────────────────────────────

@pytest.mark.parametrize("etype", ["governance", "capsule", "cell", "comb", "retract", "entity_fact", "mystery"])
def test_a_refused_entry_type_is_4xx_and_writes_nothing(hive, etype):
    before = len(journal(hive))
    (code, body), _ = post(hive, "mod", etype, fact("x"))
    assert 400 <= code < 500 and len(journal(hive)) == before, body


def test_a_module_signed_governance_entry_is_refused_though_its_signature_is_valid(hive):
    (code, body), entry = post(hive, "mod", "governance", {"action": "vote-election", "election_id": "e1", "candidate": hive.mod["id"]})
    assert code == 403 and hive.hv._verify_entry(entry)               # the signature is fine; the type is not allowed
    assert len(journal(hive)) == len(hive.entries)


def test_the_allowed_types_are_registered_and_the_refused_ones_are_too():
    import vocabulary
    assert set(api.MODULE_ENTRY_TYPES) <= set(vocabulary.ENTRY_TYPES)
    assert not {"governance", "capsule", "cell", "comb", "retract"} & set(api.MODULE_ENTRY_TYPES)


@pytest.mark.parametrize("source", ["manual", "owner", "hermes", "x-other", "x-other:primary/i", "x-hwatch:owner/o", "", None, 7])
def test_the_source_must_be_the_modules_own_app(hive, source):
    (code, body), _ = post(hive, "mod", "fact", fact("x", source=source))
    assert code in (400, 403) and len(journal(hive)) == len(hive.entries), (source, body)


def test_the_modules_own_source_forms_pass(hive):
    for src in ("x-hwatch", "x-hwatch:subagent/inst/sess1234", "x-hwatch:x-hwatch:ctx/inst"):
        assert post(hive, "mod", "fact", fact(f"from {src}", source=src))[0][0] == 200, src


def test_a_channel_outside_the_three_is_refused_not_coerced(hive):
    for ch in ("sense", "act", "introspect"):
        assert post(hive, "mod", "fact", fact(f"on {ch}", channel=ch))[0][0] == 200
    (code, _), _ = post(hive, "mod", "fact", fact("odd", channel="broadcast"))
    assert code == 400


def test_a_module_may_use_a_core_tag_or_its_own_and_not_a_bare_or_anothers(hive):
    assert post(hive, "mod", "fact", fact("tagged", tags=["volatile", "ttl:2h", "x-hwatch:deploy"]))[0][0] == 200
    for bad in (["deploy"], ["x-other:deploy"], ["x-hwatch"], [7]):
        (code, _), _ = post(hive, "mod", "fact", fact("bad tags", tags=bad))
        assert code in (400, 403), bad


def test_content_and_name_are_required_where_the_projection_reads_them(hive):
    (code, _), _ = post(hive, "mod", "fact", {"source": SRC})
    assert code == 400
    (code, _), _ = post(hive, "mod", "idea", {"source": SRC, "content": ""})
    assert code == 400
    (code, _), _ = post(hive, "mod", "entity", {"source": SRC})
    assert code == 400
    assert post(hive, "mod", "entity", {"source": SRC, "name": "backup-host", "type": "host", "attributes": {"os": "linux"}})[0][0] == 200
    assert post(hive, "mod", "decision", {"source": SRC, "content": "rotate", "rationale": "r", "tags": []})[0][0] == 200
    assert post(hive, "mod", "idea", {"source": SRC, "content": "maybe the disk", "tags": [], "channel": "introspect"})[0][0] == 200


def test_the_entry_is_exactly_the_envelope_and_the_signer_is_the_sender(hive):
    for over in ({"extra": 1}, {"node_id": hive.other["id"]}, {"seq": "1"}, {"seq": True}, {"payload": []}, {"timestamp": "../../x"},
                 {"timestamp": "yesterday"}):
        (code, body), _ = post(hive, "mod", "fact", fact("x"), over=over)
        assert 400 <= code < 500 and len(journal(hive)) == len(hive.entries), (over, body)
    (code, body), entry = post(hive, "mod", "fact", fact("x"))
    assert code == 200
    unsigned = {k: v for k, v in client.build_entry(hive.mod["seed"], "fact", fact("y"), body["tip"]).items() if k not in ("sig", "pub")}
    assert hive.get("/v1/entries", method="POST", body=json.dumps(unsigned).encode())[0] == 400
    forged = client.build_entry(hive.mod["seed"], "fact", fact("z"), body["tip"])
    forged["payload"]["content"] = "tampered after signing"
    assert hive.get("/v1/entries", method="POST", body=json.dumps(forged).encode())[0] == 400
    assert hive.get("/v1/entries", method="POST", body=b"not json")[0] == 400
    assert hive.get("/v1/entries", method="POST", body=b"[]")[0] == 400


def test_a_request_signed_for_other_bytes_is_refused_and_the_door_is_the_same_as_for_reads(hive):
    (_, _), entry = post(hive, "mod", "fact", fact("x"), tip=True)
    other = json.dumps(client.build_entry(hive.mod["seed"], "fact", fact("swapped"), {"seq": 1, "hash": hive.hv.compute_hash(entry)})).encode()
    h = signed(hive.mod["seed"], "POST", "/v1/entries", "", b"some other body")
    code, body = hive.get("/v1/entries", method="POST", sign=False, headers=h, body=other)
    assert (code, body["detail"]) == (401, "bad-signature")
    assert hive.get("/v1/entries", method="POST", sign=False, body=other)[0] == 401
    assert hive.get("/v1/entries", method="POST", dev="plain", body=other)[0] == 403
    assert hive.get("/v1/entries", method="POST", dev=os.urandom(32), body=other)[0] == 403


# ── links and refs ─────────────────────────────────────────────────────────────────────────────────────────

def test_a_link_of_a_core_kind_carries_pairs_and_projects_as_evidence(hive):
    a, b = ([hive.fact["node_id"], hive.fact["seq"]], [hive.idea["node_id"], hive.idea["seq"]])
    link = {"kind": "supports", "from_ref": b, "to_ref": a, "data": {}, "source": SRC}
    (code, _), _ = post(hive, "mod", "link", link)
    assert code == 200
    assert hive.hv.get_conn().execute("SELECT count(*) FROM links WHERE kind='supports'").fetchone()[0] == 1
    (code, _), _ = post(hive, "mod", "link", dict(link, from_ref="h:0123456789", to_ref=a))
    assert code == 400                                                 # a core kind takes pairs: that is what `resolve_link` reads


def test_a_link_of_the_modules_own_kind_carries_from_and_to_as_a_pair_or_an_h_id(hive):
    a, sid = [hive.fact["node_id"], hive.fact["seq"]], hive.hv._short_id(hive.idea["node_id"], hive.idea["seq"])
    own = {"kind": "x-hwatch:observes", "from": sid, "to": a, "data": {}, "source": SRC}
    (code, _), _ = post(hive, "mod", "link", own)
    assert code == 200
    assert hive.hv.get_conn().execute("SELECT count(*) FROM links").fetchone()[0] == 0     # no resolver: lands, projects to nothing
    for bad in (dict(own, to="h:nope"), dict(own, to="node:3"), dict(own, to=[1, 2]), dict(own, **{"from": None}),
                {k: v for k, v in own.items() if k != "to"}, dict(own, kind="x-other:observes"), dict(own, kind="observes"),
                dict(own, kind=None)):
        (code, _), _ = post(hive, "mod", "link", bad)
        assert code in (400, 403), bad
    dangling = dict(own, to=["k1:nobody", 99])                         # absent yet: allowed; a projection skips it
    assert post(hive, "mod", "link", dangling)[0][0] == 200


def test_resolve_ref_binds_a_pair_and_never_a_string(hive):
    hv, conn = hive.hv, hive.hv.get_conn()
    pair = [hive.fact["node_id"], hive.fact["seq"]]
    assert hv._resolve_ref(conn, pair) is not None
    assert hv._resolve_ref(conn, hv._short_id(*pair)) is None          # core's own readers never bound a string
    assert "short_ids" not in hv._resolve_ref.__code__.co_varnames


# ── quotas and rate limits (A4) ─────────────────────────────────────────────────────────────────────────────

def lower(monkeypatch, **kw):
    monkeypatch.setattr(api, "QUOTA_DEFAULTS", dict(api.QUOTA_DEFAULTS, **kw))


def test_each_window_trips_at_its_limit_with_a_retry_after_and_writes_nothing(hive, monkeypatch):
    lower(monkeypatch, per_hour=2)
    for i in range(2):
        assert post(hive, "mod", "fact", fact(f"w{i}"))[0][0] == 200
    seed = hive.mod["seed"]
    over = json.dumps(client.build_entry(seed, "fact", fact("w2"), hive.get("/v1/tip")[1])).encode()
    h = signed(seed, "POST", "/v1/entries", "", over)
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{hive.port}/v1/entries", data=over, headers=h, method="POST"), timeout=10)
        raise AssertionError("expected 429")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        assert e.code == 429 and 1 <= int(e.headers["Retry-After"]) <= 3600
        assert body["limit"] == "per_hour" and body["quota"]["used"]["per_hour"] == 2 and body["quota"]["remaining"]["per_hour"] == 0
    assert sum(1 for e in journal(hive) if e["node_id"] == hive.mod["id"]) == 2
    # ...it recovers when the window moves on
    path = hive.home / api.QUOTA_FILE
    data = json.loads(path.read_text())
    data[hive.mod["id"]] = [t - 3700 for t in data[hive.mod["id"]]]
    path.write_text(json.dumps(data))
    assert post(hive, "mod", "fact", fact("w2"))[0][0] == 200


def test_the_daily_window_and_the_per_entry_size(hive, monkeypatch):
    lower(monkeypatch, per_day=1, entry_bytes=600)
    assert post(hive, "mod", "fact", fact("d0"))[0][0] == 200
    (code, body), _ = post(hive, "mod", "fact", fact("d1"))
    assert code == 429 and body["limit"] == "per_day"
    path = hive.home / api.QUOTA_FILE
    path.write_text(json.dumps({hive.mod["id"]: [time.time() - 90000]}))
    (code, body), _ = post(hive, "mod", "fact", fact("x" * 700))
    assert code == 413 and body["max_bytes"] == 600
    assert post(hive, "mod", "fact", fact("d1"))[0][0] == 200


def test_two_modules_do_not_share_a_bucket(hive, monkeypatch):
    lower(monkeypatch, per_hour=1)
    assert post(hive, "mod", "fact", fact("m1"))[0][0] == 200 and post(hive, "mod", "fact", fact("m2"))[0][0] == 429
    assert post(hive, "other", "fact", dict(fact("o1"), source="x-other"))[0][0] == 200
    assert hive.get("/v1/", dev="other")[1]["quota"]["used"]["per_hour"] == 1


def test_the_lifetime_ceiling_is_the_journals_so_no_file_restart_or_rebuild_resets_it(hive, monkeypatch):
    lower(monkeypatch, lifetime=2)
    assert post(hive, "mod", "fact", fact("l1"))[0][0] == 200 and post(hive, "mod", "fact", fact("l2"))[0][0] == 200
    (code, body), _ = post(hive, "mod", "fact", fact("l3"))
    assert code == 429 and body["limit"] == "lifetime" and body["retry_after"] is None
    (hive.home / api.QUOTA_FILE).unlink()                              # a deleted side file
    hive.hv.rebuild_db()                                               # a rebuild
    (code, body), _ = post(hive, "mod", "fact", fact("l3"))
    assert code == 429 and body["limit"] == "lifetime"
    assert hive.get("/v1/")[1]["quota"]["used"]["lifetime"] == 2       # counted from the journal, not the file
    (hive.home / api.QUOTA_FILE).write_text("{not json")               # a corrupt file only loosens the hour and the day
    assert hive.get("/v1/")[1]["quota"]["used"] == {"per_hour": 0, "per_day": 0, "lifetime": 2}


def test_a_refused_or_repeated_entry_does_not_use_quota_and_the_file_is_private(hive, monkeypatch):
    lower(monkeypatch, per_hour=2)
    (_, _), entry = post(hive, "mod", "fact", fact("q1"))
    post(hive, "mod", "governance", {"action": "x"})
    hive.get("/v1/entries", method="POST", body=json.dumps(entry).encode())     # the idempotent retry
    assert hive.get("/v1/")[1]["quota"]["used"]["per_hour"] == 1
    path = hive.home / api.QUOTA_FILE
    assert (path.stat().st_mode & 0o777) == 0o600
    assert set(json.loads(path.read_text())) == {hive.mod["id"]}
    assert not any(api.QUOTA_FILE in json.dumps(e) for e in journal(hive))      # never journaled


def test_the_quota_file_is_in_the_local_files_table():
    import vocabulary
    assert api.QUOTA_FILE in vocabulary.LOCAL_FILES


# ── the listener's own cap (it must not push sync peers into 503) ──────────────────────────────────────────

def test_the_module_listener_has_its_own_cap_and_never_takes_the_daemons_slots(hive, monkeypatch):
    assert api.MODULE_MAX_CONCURRENT < daemon.MAX_CONCURRENT_REQUESTS
    taken = []
    while api._module_slots.acquire(blocking=False):
        taken.append(1)
    try:
        assert len(taken) == api.MODULE_MAX_CONCURRENT
        assert hive.get("/v1/")[0] == 503                              # the module listener is full...
        assert daemon._request_slots.acquire(blocking=False)           # ...and the sync daemon's slots are all free
        daemon._request_slots.release()
    finally:
        for _ in taken:
            api._module_slots.release()
    assert hive.get("/v1/")[0] == 200
    assert "_request_slots" not in re.sub(r'"""[\s\S]*?"""|#.*', "", (PROJECT / "hive_module_api.py").read_text())


def test_a_slot_is_freed_after_every_kind_of_answer(hive):
    for _ in range(api.MODULE_MAX_CONCURRENT + 3):
        hive.get("/v1/", sign=False)                                   # 401
        hive.get("/v1/entries", method="POST", body=b"nope")           # 400
        hive.get("/v1/feed", method="PUT", body=b"{}")                 # 405
    assert hive.get("/v1/")[0] == 200


def test_an_oversize_or_unbounded_post_is_refused_before_it_is_read(hive):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", hive.port, timeout=10)
    conn.request("POST", "/v1/entries", body=b"x" * (api.QUOTA_DEFAULTS["entry_bytes"] + 1),
                 headers=signed(hive.mod["seed"], "POST", "/v1/entries", "", b"x"))
    r = conn.getresponse()
    assert r.status == 413
    conn.close()


# ── the reference signer ────────────────────────────────────────────────────────────────────────────────────

def test_the_reference_signer_matches_the_cores_bytes_and_imports_no_core_code(hive):
    hv, seed = hive.hv, hive.mod["seed"]
    e = client.build_entry(seed, "fact", fact("same bytes"), None, "2026-01-06T00:00:00.000+00:00")
    assert hv._verify_entry(e) and client.canonical(e) == hv._canonical(e) and client.entry_hash(e) == hv.compute_hash(e)
    assert e["node_id"] == hive.mod["id"] == client.device_id_for_pub(hv._ed25519.pub_from_seed(seed))
    mine = hv._ed25519.sign(hv._entry_signing_bytes(e), seed)
    import base64
    assert base64.b64decode(e["sig"]) == mine                          # Ed25519 is deterministic: the same signature
    src = (PROJECT / "hive_module_client.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+(hv|hive_sync_daemon|hive_module_api|ownerkey|hivemind_owner)\b", src, re.M)


def test_the_client_walks_the_api_end_to_end(hive):
    c = client.ModuleClient(hive.mod["seed"], port=hive.port)
    assert c.tip()["seq"] == 0
    code, body = c.write("fact", fact("written by the reference client"))
    assert code == 200 and c.tip()["seq"] == 1 == body["tip"]["seq"]
    assert c.request("GET", "/v1/search", "q=reference+client")[1]["facts"][0]["content"] == "written by the reference client"


# ── review rulings on c165b17: legacy refs, bound types, the signer check, a zero limit ───────────────────

def _victim(h):
    """A human decision the module must not be able to supersede: [node_id, seq] and its local row id."""
    e = TL._entry(h.hv, h.plain, "decision", {"content": "we deploy on tuesdays", "tags": [], "source": "manual"}, "2026-01-05T00:00:00.000+00:00")
    assert h.hv.append_foreign_entries([e])[0] == 1
    h.hv.rebuild_db()
    return [e["node_id"], e["seq"]], h.hv.get_conn().execute("SELECT id FROM decisions").fetchone()["id"]


@pytest.mark.parametrize("field", ["supersedes_ref", "supersedes", "resolves_ref", "entity_ref", "fact_ref", "entity_id", "fact_id"])
def test_a_legacy_ref_field_is_refused_and_writes_nothing(hive, field):
    pair, lid = _victim(hive)
    value = lid if field in ("supersedes", "entity_id", "fact_id") else pair
    for etype, payload in (("decision", {"source": SRC, "content": "we deploy on fridays", "tags": []}),
                           ("fact", {"source": SRC, "content": "x", "tags": []})):
        before = len(journal(hive))
        (code, body), _ = post(hive, "mod", etype, dict(payload, **{field: value}))
        assert code == 400 and field in body["fields"] and len(journal(hive)) == before, (etype, body)
    assert hive.hv.get_conn().execute("SELECT superseded_by FROM decisions WHERE id = ?", (lid,)).fetchone()[0] is None


def test_every_legacy_row_of_ref_fields_is_refused(hive):
    import vocabulary
    for k, r in vocabulary.REF_FIELDS.items():
        if r["status"] == vocabulary.LEGACY:
            (code, _), _ = post(hive, "mod", "fact", fact("legacy probe", **{k: 1}))
            assert code == 400, k


BOUND = [("fact", "importance"), ("fact", "confidence"), ("fact", "source_session"), ("fact", "created_at"), ("fact", "access_count"),
         ("decision", "rationale"), ("decision", "created_at"), ("entity", "type"), ("entity", "created_at"), ("entity", "attributes"),
         ("idea", "source_session")]


@pytest.mark.parametrize("etype,field", BOUND)
@pytest.mark.parametrize("bad", [[1], {"a": 1}])
def test_a_field_the_projection_binds_must_be_the_right_type_and_the_projection_survives(hive, etype, field, bad):
    base = {"source": SRC, "tags": [], "content": "bound probe", "name": "probe-host"}
    base = {k: v for k, v in base.items() if not (etype == "entity" and k in ("content", "tags")) and not (etype != "entity" and k == "name")}
    if etype == "entity":
        base["tags"] = []
    before = journal(hive)
    (code, _), _ = post(hive, "mod", etype, dict(base, **{field: bad}))
    if field == "attributes" and bad == {"a": 1}:
        assert code == 200                                             # a dict is what an attributes field is
        return
    assert 400 <= code < 500 and journal(hive) == before
    hive.hv.rebuild_db()


def test_the_backstop_refuses_a_payload_the_checks_do_not_know(hive, monkeypatch):
    """A field added to the projection later: the dry run in a SAVEPOINT refuses what raises, writes nothing, and leaves
    the store as it was."""
    before, rows = journal(hive), hive.hv.get_conn().execute("SELECT count(*) FROM facts").fetchone()[0]
    monkeypatch.setattr(api, "_BOUND_FIELDS", {})
    (code, body), _ = post(hive, "mod", "fact", fact("backstop", importance=[1]))
    assert code == 400 and "project" in body["error"] and journal(hive) == before
    assert hive.hv.get_conn().execute("SELECT count(*) FROM facts").fetchone()[0] == rows
    assert post(hive, "mod", "fact", fact("backstop ok"))[0][0] == 200 and hive.hv.rebuild_db() is not False


def test_an_entry_signed_by_another_module_and_posted_as_this_one_is_403_and_writes_nothing(hive):
    seed = hive.other["seed"]
    entry = client.build_entry(seed, "fact", fact("from the other module"), hive.get("/v1/tip", dev="other")[1], "2026-01-06T00:00:00.000+00:00")
    before = journal(hive)
    code, body = hive.get("/v1/entries", dev="mod", method="POST", body=json.dumps(entry).encode())
    assert code == 403 and journal(hive) == before


@pytest.mark.parametrize("window", ["per_hour", "per_day"])
def test_a_window_limit_of_zero_refuses_instead_of_raising(hive, monkeypatch, window):
    lower(monkeypatch, **{window: 0})
    before = journal(hive)
    (code, body), _ = post(hive, "mod", "fact", fact("nothing fits"))
    assert code == 429 and body["limit"] == window and journal(hive) == before


@pytest.mark.parametrize("bad", [[5], 5, "abc", {"a": 1}, [["k1:x"]], [[1, 2, 3]], [["n", True]]])
def test_a_malformed_informed_by_is_refused_and_search_still_reads(hive, bad):
    """`informed_by` is stored as JSON and read back by `api_search`; a shape the readers cannot walk would 500 every search."""
    before = journal(hive)
    (code, body), _ = post(hive, "mod", "decision", {"source": SRC, "content": "probe decision zz", "tags": [], "informed_by": bad})
    assert 400 <= code < 500 and "informed_by" in body["error"] and journal(hive) == before
    hive.hv.rebuild_db()
    assert hive.hv.api_search("") is not None and hive.get("/v1/search?q=probe")[0] == 200


def test_a_well_formed_informed_by_is_accepted(hive):
    pair, _ = _victim(hive)
    (code, _), _ = post(hive, "mod", "decision", {"source": SRC, "content": "probe decision ok", "tags": [], "informed_by": [pair]})
    assert code == 200
    hive.hv.rebuild_db()
    assert hive.hv.api_search("probe")["decisions"]
