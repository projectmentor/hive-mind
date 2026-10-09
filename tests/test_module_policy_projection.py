"""2.1 (hive-mind-private #50): the module write policy is enforced by the projection, not only by `POST /v1/entries`.

A module device is an admitted device like any other to `/sync/ingest`, which appends what a device signed. So a module's
key can land a `retract` or a `manual`-source fact on the sync path, past the API gate. The projection now judges every
entry a module device carried with the API's own rules (`hv _module_policy_problem`), so what the API would have refused
does not project, on every node, whichever way it arrived.

Same temp hive as `test_module_api.py`: an owner, a plain device, and two module devices (`hwatch`, `other`).
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import hive_module_client as client  # noqa: E402
import hive_sync_daemon as daemon  # noqa: E402
import sync_common  # noqa: E402
import vocabulary  # noqa: E402
import test_links as TL  # noqa: E402
from test_module_api import Hive, hive  # noqa: E402,F401
from test_module_write import post, fact, SRC  # noqa: E402

TS = "2026-01-06T00:00:%02d.000+00:00"
TARGET = "the backup runs at 02:00"          # the plain device's fact the fixture projects


@pytest.fixture
def ingest(hive, monkeypatch):
    """`POST /sync/ingest` on the real Handler, over loopback (the local operator's path)."""
    monkeypatch.setenv("HIVE_SYNC_AUTH", "permissive")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def push(entries, dev="mod"):
        """Signed by device `dev`'s key, as a peer's push is: loopback also wants the signed envelope or the CSRF token."""
        body = json.dumps({"entries": entries}).encode()
        headers = dict(sync_common.signed_request_headers(getattr(hive, dev)["seed"], "POST", "/sync/ingest", "", body),
                       **{"Content-Type": "application/json"})
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/sync/ingest", method="POST",
                                     data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield push
    srv.shutdown()
    srv.server_close()


def chain(hive, dev, specs, first_ts=10):
    """Signed entries of module device `dev` ('mod'), chained from its live tip: [(type, payload)]."""
    seed = getattr(hive, dev)["seed"]
    tip = hive.get("/v1/tip", dev=dev)[1]
    out = []
    for i, (etype, payload) in enumerate(specs):
        e = client.build_entry(seed, etype, payload, tip, TS % (first_ts + i))
        out.append(e)
        tip = {"seq": e["seq"], "hash": hive.hv.compute_hash(e)}
    return out


def retract(ref, source=SRC):
    return {"retracts_ref": ref, "reason": "probe", "source": source}


def conf_of(hive, content):
    row = hive.hv.get_conn().execute("SELECT confidence FROM facts WHERE content = ?", (content,)).fetchone()
    return None if row is None else row["confidence"]


def projected(hive, content):
    return conf_of(hive, content) is not None


def held(hive, entries):
    keys = {(e["node_id"], e["seq"]) for e in hive.hv.merkle.read_all_entries(hive.hv.JOURNAL_DIR)}
    return all((e["node_id"], e["seq"]) in keys for e in entries)


def target_ref(hive):
    return [hive.fact["node_id"], hive.fact["seq"]]


# ── a module's retract and manual fact, pushed past the API ─────────────────────────────────────────────

def test_a_module_retract_pushed_through_ingest_does_not_project(hive, ingest):
    before = conf_of(hive, TARGET)
    entries = chain(hive, "mod", [("retract", retract(target_ref(hive))),
                                  ("retract", retract(target_ref(hive), source="manual"))])
    code, body = ingest(entries)
    assert code == 200, body
    assert conf_of(hive, TARGET) == before             # no peer evidence, in the module's name or any other
    hive.hv.rebuild_db()
    assert conf_of(hive, TARGET) == before             # and a full rebuild reads the journal the same way


def test_a_module_manual_fact_pushed_through_ingest_does_not_project(hive, ingest):
    entries = chain(hive, "mod", [("fact", fact("hwatch claims to be a person typing", source="manual")),
                                  ("fact", fact("hwatch claims to be the owner", source="owner:owner")),
                                  ("fact", fact("hwatch writes under another module's name", source="x-other"))])
    code, body = ingest(entries)
    assert code == 200, body
    hive.hv.rebuild_db()
    for e in entries:
        assert not projected(hive, e["payload"]["content"])


def test_a_module_entry_the_gate_would_refuse_for_any_other_reason_does_not_project(hive, ingest):
    refused = [("fact", fact("refused: a foreign tag", tags=["x-other:tag"])),
               ("fact", fact("refused: a legacy ref", resolves_ref=[hive.fact["node_id"], hive.fact["seq"]])),
               ("fact", fact("refused: a bad importance", importance="high")),
               ("idea", {"content": "refused: no source", "tags": []}),
               ("entity", {"name": "unprefixed-entity", "type": "service", "source": SRC}),
               ("link", {"kind": "same-as", "from_ref": target_ref(hive), "to_ref": [hive.idea["node_id"], hive.idea["seq"]],
                         "source": SRC}),
               ("governance", {"action": "set-config", "key": "x-hwatch:poll", "value": "1s"})]
    entries = chain(hive, "mod", refused)
    code, body = ingest(entries)
    assert code == 200, body
    hive.hv.rebuild_db()
    conn = hive.hv.get_conn()
    for content in ("refused: a foreign tag", "refused: a legacy ref", "refused: a bad importance"):
        assert not projected(hive, content)
    assert conn.execute("SELECT count(*) FROM ideas WHERE content = 'refused: no source'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM entities WHERE name = 'unprefixed-entity'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM links WHERE kind = 'same-as'").fetchone()[0] == 0


def test_a_genuine_module_write_still_projects_through_ingest(hive, ingest):
    entries = chain(hive, "mod", [("fact", fact("hwatch saw the deploy finish")),
                                  ("entity", {"name": "x-hwatch:deploys", "type": "service", "source": SRC}),
                                  ("decision", {"content": "hwatch alerts at 90%", "rationale": "disk", "tags": [],
                                                "source": SRC}),
                                  ("idea", {"content": "the disk is the bottleneck", "tags": [], "source": SRC})])
    code, body = ingest(entries)
    assert code == 200, body
    assert held(hive, entries)
    assert projected(hive, "hwatch saw the deploy finish")
    conn = hive.hv.get_conn()
    assert conn.execute("SELECT count(*) FROM entities WHERE name = 'x-hwatch:deploys'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM decisions WHERE content = 'hwatch alerts at 90%'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM ideas WHERE content = 'the disk is the bottleneck'").fetchone()[0] == 1


def test_a_genuine_module_write_still_projects_through_the_api(hive):
    (code, body), _ = post(hive, "mod", "fact", fact("hwatch saw the deploy finish"))
    assert code == 200 and body["accepted"] == 1
    assert projected(hive, "hwatch saw the deploy finish")


# ── a non-module device's same entries are untouched ────────────────────────────────────────────────────

def test_a_non_module_devices_retract_and_manual_fact_still_project(hive, ingest):
    before = conf_of(hive, TARGET)
    entries = [TL._fact(hive.hv, hive.plain, "a person typing at the plain device", TS % 20, source="manual"),
               TL._entry(hive.hv, hive.plain, "retract", retract(target_ref(hive), source="manual"), TS % 21)]
    code, body = ingest(entries, dev="plain")
    assert code == 200, body
    assert held(hive, entries)
    assert projected(hive, "a person typing at the plain device")
    assert conf_of(hive, TARGET) != before             # the plain device's retract is evidence and weighs


# ── what a module key landed before the upgrade stops projecting ────────────────────────────────────────

def test_facts_a_module_already_landed_in_breach_stop_projecting_and_doctor_counts_them(hive):
    hv = hive.hv
    breach = [TL._fact(hv, hive.mod, "landed before the upgrade: manual", TS % 30, source="manual"),
              TL._entry(hv, hive.mod, "retract", retract(target_ref(hive)), TS % 31)]
    ok = TL._fact(hv, hive.mod, "landed before the upgrade: genuine", TS % 32, source=SRC)
    TL._project(hv, hive.home, hive.entries + breach + [ok]).close()
    assert not projected(hive, "landed before the upgrade: manual")
    assert projected(hive, "landed before the upgrade: genuine")
    checks = {c["name"]: c for c in hv._doctor_status()}
    detail = checks["module-policy"]["detail"]
    assert detail.startswith("2 entries") and "module hwatch" in detail
    assert f"{hive.mod['id']}:{breach[0]['seq']}" in detail and f"{hive.mod['id']}:{ok['seq']}" not in detail


def test_doctor_is_quiet_when_no_module_wrote_in_breach(hive):
    post(hive, "mod", "fact", fact("a clean module write"))
    assert "module-policy" not in {c["name"] for c in hive.hv._doctor_status()}


# ── one table: the API and the projection cannot disagree ───────────────────────────────────────────────

def test_the_api_and_the_projection_read_the_same_rules(hive, monkeypatch):
    import hive_module_api as api
    assert api.MODULE_ENTRY_TYPES is vocabulary.MODULE_ENTRY_TYPES
    monkeypatch.setattr(vocabulary, "MODULE_ENTRY_TYPES", ("fact",))        # narrow the one table...
    (code, _), _ = post(hive, "mod", "idea", {"content": "narrowed away", "tags": [], "source": SRC})
    assert 400 <= code < 500                                                 # ...the API follows it
    e = TL._entry(hive.hv, hive.mod, "idea", {"content": "narrowed away too", "tags": [], "source": SRC}, TS % 40)
    gov = {"modules": {hive.mod["id"]: "hwatch"}}
    assert hive.hv._module_policy_declined([e], gov) == {(e["node_id"], e["seq"])}    # ...and so does the projection
