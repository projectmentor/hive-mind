"""SECREV A3: an owner-signed governance act counts only when the device carrying it is a member at that position.

An owner signature proves the KEY signed, not WHEN, and a journal position is a stamp the writer picks. So a former
owner's key on a device nobody admitted could place an act anywhere inside its own term. The projection now honours an
owner-signed act (governance, forget, link, capsule, cell) only when the carrying device is the genesis device or was
admitted by an honoured owner act at or before that position. Succession acts are also pinned on the node, as the
genesis is, and `hv doctor` compares the order they arrived in with the order they sort in.

Each probe below fails on 9a32be4 and passes here; the last of each pair shows the same act honoured from a member.
"""

import base64
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owner_key, _device, _gov, _entry, _fact  # noqa: E402
from test_unforget import _act  # noqa: E402

DAY = "2026-01-01T"


def ts(h, m=0):
    return f"{DAY}{h:02d}:{m:02d}:00.000+00:00"


def _b64(k):
    return base64.b64encode(k[1]).decode()


def _world(hv):
    """O1 founds the hive on `ownerdev`, admits d0, and hands over to O2 at 10:00 (carried by d0). F is a device no one
    admitted. Returns (o1, o2, o3, d0, f, base, real_transfer)."""
    o1, o2, o3 = _owner_key(hv), _owner_key(hv), _owner_key(hv)
    d0, f = _device(hv), _device(hv)
    base = [_gov(hv, {"action": "owner", "owner_id": o1[2], "hive_id": "h1"}, o1[0], o1[1], ts(0), 1),
            _gov(hv, {"action": "admit", "device_id": d0["id"], "principal": "p0"}, o1[0], o1[1], ts(1), 2)]
    real = _entry(hv, d0, "governance", {"action": "transfer", "new_owner_pub": _b64(o2)}, ts(10), owner=o1)
    return o1, o2, o3, d0, f, base, real


def _state(hv, entries):
    return hv._governance_state(entries)


# --- the transfer ------------------------------------------------------------------------------------------------

def test_a_fresh_device_cannot_re_route_succession_with_a_backdated_transfer(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    fake = _entry(hv, f, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(5), owner=o1)
    gov = _state(hv, base + [real, fake])
    assert gov["owner_id"] == o2[2] and gov["owner_term"] == 1
    assert (f["id"], fake["seq"], "transfer") in gov["uncarried"]


def test_the_same_transfer_from_a_member_is_honoured(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    early = _entry(hv, d0, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(5), owner=o1)
    assert _state(hv, base + [early])["owner_id"] == o3[2]       # d0 is admitted at 05:00, so this act stands


# --- the other acts inside the old term --------------------------------------------------------------------------

def _backdated(hv, f, o1, payload, at=ts(5)):
    return _entry(hv, f, "governance", payload, at, owner=o1)


def test_a_backdated_admit_from_a_fresh_device_admits_nobody(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    z = _device(hv)
    gov = _state(hv, base + [real, _backdated(hv, f, o1, {"action": "admit", "device_id": f["id"], "principal": "x"}),
                             _backdated(hv, f, o1, {"action": "admit", "device_id": z["id"], "principal": "z"})])
    assert gov["admitted"] == {d0["id"]}


def test_a_backdated_purge_from_a_fresh_device_purges_nobody(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    gov = _state(hv, base + [real, _backdated(hv, f, o1, {"action": "purge", "device_id": d0["id"]})])
    assert d0["id"] in gov["admitted"] and d0["id"] not in gov["purged"]


def test_a_backdated_set_config_from_a_fresh_device_changes_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    gov = _state(hv, base + [real, _backdated(hv, f, o1, {"action": "set-config", "key": "quorum_m", "value": 1})])
    assert gov["config"]["quorum_m"] == _state(hv, base + [real])["config"]["quorum_m"]


def test_a_backdated_freeze_timestamps_from_a_fresh_device_arms_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    gov = _state(hv, base + [real, _backdated(hv, f, o1, {"action": "freeze-timestamps", "tips": {}})])
    assert not gov["ts_markers"] and not gov["ts_tips"]


def test_a_backdated_forget_from_a_fresh_device_forgets_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    fact = _fact(hv, d0, "keep me", ts(3))
    forget = _act(hv, f, fact, ts(5), owner=o1[:2])
    entries = base + [real, fact, forget]
    ev = hv._content_evidence(entries, _state(hv, entries))
    assert ev["keep me"]["forget"] is False
    member = _act(hv, d0, fact, ts(5), owner=o1[:2])                 # the same forget carried by a member stands
    entries = base + [real, fact, member]
    assert hv._content_evidence(entries, _state(hv, entries))["keep me"]["forget"] is True


def test_a_backdated_hard_link_from_a_fresh_device_is_not_hard(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    old, new = _fact(hv, d0, "old", ts(3)), _fact(hv, d0, "new", ts(4))

    def link(dev):
        p = {"kind": "supersedes", "from_ref": [new["node_id"], new["seq"]], "to_ref": [old["node_id"], old["seq"]],
             "data": {}, "source": "manual"}
        return _entry(hv, dev, "link", p, ts(5), owner=o1)

    for dev, want in ((f, False), (d0, True)):
        e = link(dev)
        gov = _state(hv, base + [real, old, new, e])
        assert (hv._link_authority(e, old, gov) == "hard") is want


def test_a_capsule_put_from_a_fresh_device_is_not_honoured(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    p = {"name": "secret", "version": 1, "alg": "x", "recipients": {}}
    gov = _state(hv, base + [real])
    pos = (ts(5), f["id"], 1)
    assert hv._is_authorized_writer(f["id"], pos, hv._sign_governance_payload(p, o1[0], o1[1]), gov, "owner") is False
    pos = (ts(5), d0["id"], 1)
    assert hv._is_authorized_writer(d0["id"], pos, hv._sign_governance_payload(p, o1[0], o1[1]), gov, "owner") is True


# --- membership is positional ------------------------------------------------------------------------------------

def test_a_member_before_its_admit_and_after_its_revoke_is_not_a_carrier(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    d1 = _device(hv)
    adm = _entry(hv, d0, "governance", {"action": "admit", "device_id": d1["id"], "principal": "p1"}, ts(2), owner=o1)
    rev = _entry(hv, d0, "governance", {"action": "revoke", "device_id": d1["id"]}, ts(4), owner=o1)
    early = _entry(hv, d1, "governance", {"action": "set-config", "key": "quorum_m", "value": 2}, ts(1, 30), owner=o1)
    inside = _entry(hv, d1, "governance", {"action": "set-config", "key": "cap_self", "value": 0.3}, ts(3), owner=o1)
    late = _entry(hv, d1, "governance", {"action": "set-config", "key": "dead_man_days", "value": 1}, ts(6), owner=o1)
    gov = _state(hv, base + [adm, rev, early, inside, late])
    assert gov["config"]["cap_self"] == 0.3
    assert gov["config"]["quorum_m"] != 2 and gov["config"]["dead_man_days"] != 1


def test_the_genesis_device_carries_without_an_admit(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    cfg = _gov(hv, {"action": "set-config", "key": "cap_self", "value": 0.4}, o1[0], o1[1], ts(2), 3)    # on `ownerdev`
    assert _state(hv, base + [cfg])["config"]["cap_self"] == 0.4


# --- the pin -----------------------------------------------------------------------------------------------------

def test_a_pinned_succession_refuses_an_act_dated_before_the_tip(tmp_path, monkeypatch):
    """A compromised ADMITTED device carries the retired key's transfer, stamped into the old term. Unpinned it stands
    (the legacy rule); once the real handoff is pinned it is refused."""
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    d1 = _device(hv)
    base = base + [_gov(hv, {"action": "admit", "device_id": d1["id"], "principal": "p1"}, o1[0], o1[1], ts(1, 30), 3)]
    fake = _entry(hv, d1, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(5), owner=o1)
    assert _state(hv, base + [real, fake])["owner_id"] == o3[2]            # no pin: positioned, so it wins
    pin = hv._pin_successions(base + [real])
    assert pin and pin["acts"] == [hv.compute_hash(real)] and tuple(pin["tip"]) == (ts(10), d0["id"], real["seq"])
    gov = _state(hv, base + [real, fake])
    assert gov["owner_id"] == o2[2] and (d1["id"], fake["seq"]) in gov["unpinned"]


def test_a_later_succession_extends_the_pin(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    hv._pin_successions(base + [real])
    nxt = _entry(hv, d0, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(12), owner=o2)
    assert _state(hv, base + [real, nxt])["owner_id"] == o3[2]
    pin = hv._pin_successions(base + [real, nxt])
    assert pin["acts"] == [hv.compute_hash(real), hv.compute_hash(nxt)]


def test_a_pin_taken_against_another_genesis_is_ignored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    hv._pin_successions(base + [real])
    pin = json.loads(hv.SUCCESSION_PIN_PATH.read_text())
    pin["genesis_hash"] = "sha256:" + "0" * 64
    hv.SUCCESSION_PIN_PATH.write_text(json.dumps(pin))
    assert _state(hv, base + [real])["owner_id"] == o2[2]                  # read as no pin; nothing refused
    assert not _state(hv, base + [real])["unpinned"]


def test_a_new_hive_pins_its_genesis_position_as_the_tip(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    pin = hv._pin_successions(base)
    assert pin["acts"] == [] and tuple(pin["tip"]) == (ts(0), "ownerdev", 1)
    assert oct(hv.SUCCESSION_PIN_PATH.stat().st_mode & 0o777) == "0o600"


# --- the doctor check --------------------------------------------------------------------------------------------

def test_arrival_order_against_journal_order_flags_a_backdated_succession_act(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    d1 = _device(hv)
    fake = _entry(hv, d1, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(5), owner=o1)
    monkeypatch.setenv("HIVE_NOW", "2026-02-01T00:00:00+00:00")
    hv._record_arrivals([(real["node_id"], real["seq"])])
    monkeypatch.setenv("HIVE_NOW", "2026-02-02T00:00:00+00:00")
    hv._record_arrivals([(fake["node_id"], fake["seq"])])
    es = base + [real, fake]
    assert hv._succession_inversions(es) == [(f"{d1['id']}:{fake['seq']}", f"{d0['id']}:{real['seq']}")]
    assert hv._pin_successions(es) is None                                  # nothing is pinned over a disagreement
    assert hv._succession_inversions(base + [real]) == []


def test_arrival_order_that_matches_journal_order_is_clean(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    nxt = _entry(hv, d0, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(12), owner=o2)
    monkeypatch.setenv("HIVE_NOW", "2026-02-01T00:00:00+00:00")
    hv._record_arrivals([(real["node_id"], real["seq"])])
    monkeypatch.setenv("HIVE_NOW", "2026-02-02T00:00:00+00:00")
    hv._record_arrivals([(nxt["node_id"], nxt["seq"])])
    assert hv._succession_inversions(base + [real, nxt]) == []


def test_doctor_reports_the_succession_check(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    d1 = _device(hv)
    fake = _entry(hv, d1, "governance", {"action": "transfer", "new_owner_pub": _b64(o3)}, ts(5), owner=o1)
    monkeypatch.setenv("HIVE_NOW", "2026-02-01T00:00:00+00:00")
    hv._record_arrivals([(real["node_id"], real["seq"])])
    monkeypatch.setenv("HIVE_NOW", "2026-02-02T00:00:00+00:00")
    hv._record_arrivals([(fake["node_id"], fake["seq"])])
    jd = hv.JOURNAL_DIR
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in base + [real, fake]) + "\n")
    checks = {c["name"]: c for c in hv._doctor_status()}
    assert checks["succession"]["status"] == "fail" and "reached this node after" in checks["succession"]["detail"]


# --- the claim: a successor's carrier is not admitted by replaying it ------------------------------------------------

def _claim_world(hv, preadmit_c=True):
    """O1 founds the hive and admits d0 (and c, the successor's device, when `preadmit_c`). d0 carries O1's nomination of
    O2 at 08:00; O2's claim is carried by c at 10:00. Returns (o1, o2, d0, c, x, base, nominate, claim_payload)."""
    o1, o2 = _owner_key(hv), _owner_key(hv)
    d0, c, x = _device(hv), _device(hv), _device(hv)
    base = [_gov(hv, {"action": "owner", "owner_id": o1[2], "hive_id": "h1"}, o1[0], o1[1], ts(0), 1),
            _gov(hv, {"action": "admit", "device_id": d0["id"], "principal": "p0"}, o1[0], o1[1], ts(1), 2)]
    if preadmit_c:
        base.append(_gov(hv, {"action": "admit", "device_id": c["id"], "principal": "p1"}, o1[0], o1[1], ts(1, 30), 3))
    nom = _entry(hv, d0, "governance", {"action": "nominate-successor", "successor_owner_pub": _b64(o2)}, ts(8), owner=o1)
    claim = hv._sign_governance_payload({"action": "claim-succession", "owner_id": o2[2], "owner_pub": _b64(o2)}, o2[0], o2[1])
    return o1, o2, d0, c, x, base, nom, claim


def _carry(hv, dev, payload, at):
    dev["seq"] += 1
    e = {"node_id": dev["id"], "seq": dev["seq"], "type": "governance", "timestamp": at, "payload": payload}
    return hv._sign_entry(e, dev["seed"], dev["pub"])


def test_a_claim_from_an_admitted_device_succeeds_and_the_new_owner_acts_from_it(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, d0, c, x, base, nom, claim = _claim_world(hv)
    real = _carry(hv, c, claim, ts(10))
    z = _device(hv)
    admit = _entry(hv, c, "governance", {"action": "admit", "device_id": z["id"], "principal": "p2"}, ts(11), owner=o2)
    gov = _state(hv, base + [nom, real, admit])
    assert gov["owner_id"] == o2[2] and gov["owner_term"] == 1
    assert z["id"] in gov["admitted"]


def test_a_keyless_device_replaying_the_claim_is_not_admitted(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, d0, c, x, base, nom, claim = _claim_world(hv)
    real = _carry(hv, c, claim, ts(10))
    replay = _carry(hv, x, claim, ts(9))                 # the claim is public: x holds no owner key, and re-carries it
    z = _device(hv)
    admit = _entry(hv, c, "governance", {"action": "admit", "device_id": z["id"], "principal": "p2"}, ts(11), owner=o2)
    gov = _state(hv, base + [nom, real, replay, admit])
    assert x["id"] not in gov["admitted"]
    assert c["id"] in gov["admitted"] and z["id"] in gov["admitted"]       # the successor's own device is unaffected
    assert gov["owner_id"] == o2[2]


def test_a_claim_from_an_unadmitted_device_is_uncarried_and_not_honoured(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, d0, c, x, base, nom, claim = _claim_world(hv, preadmit_c=False)
    gov = _state(hv, base + [nom, _carry(hv, c, claim, ts(10))])
    assert gov["owner_id"] == o1[2]
    assert c["id"] not in gov["admitted"]
    assert (c["id"], 1, "claim-succession") in gov["uncarried"]


def test_a_keyless_replay_of_the_claim_cannot_lock_out_the_successor(tmp_path, monkeypatch):
    """O1 nominates O2 (08:00), then admits c (08:30); O2 claims from c (10:00) and admits z (11:00). A keyless x
    re-carries the public claim stamped 08:15. x is no member, so the replay is uncarried and moves nothing."""
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, d0, c, x, base, nom, claim = _claim_world(hv, preadmit_c=False)
    admit_c = _entry(hv, d0, "governance", {"action": "admit", "device_id": c["id"], "principal": "p1"}, ts(8, 30), owner=o1)
    real = _carry(hv, c, claim, ts(10))
    z = _device(hv)
    admit_z = _entry(hv, c, "governance", {"action": "admit", "device_id": z["id"], "principal": "p2"}, ts(11), owner=o2)
    replay = _carry(hv, x, claim, ts(8, 15))
    gov = _state(hv, base + [nom, admit_c, real, admit_z, replay])
    assert gov["owner_id"] == o2[2] and gov["owner_term"] == 1
    assert c["id"] in gov["admitted"] and z["id"] in gov["admitted"]
    assert gov["uncarried"] == [(x["id"], 1, "claim-succession")]


# --- the end of a membership span, on the content paths and the timestamp shield ---------------------------------------

def _revoked_member(hv):
    """d1 is admitted at 02:00 and revoked at 04:00, both carried by d0. Returns (o1, d0, d1, f, base, real, rev)."""
    o1, o2, o3, d0, f, base, real = _world(hv)
    d1 = _device(hv)
    adm = _entry(hv, d0, "governance", {"action": "admit", "device_id": d1["id"], "principal": "p1"}, ts(2), owner=o1)
    rev = _entry(hv, d0, "governance", {"action": "revoke", "device_id": d1["id"]}, ts(4), owner=o1)
    return o1, d0, d1, f, base + [adm, rev], real


def test_a_forget_carried_after_the_carriers_revoke_is_not_honoured(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, d0, d1, f, base, real = _revoked_member(hv)
    fact = _fact(hv, d0, "keep me", ts(3))
    for at, want in ((ts(3, 30), True), (ts(6), False)):
        forget = _act(hv, d1, fact, at, owner=o1[:2])
        entries = base + [real, fact, forget]
        assert hv._content_evidence(entries, _state(hv, entries))["keep me"]["forget"] is want


def test_a_hard_link_carried_after_the_carriers_revoke_is_not_hard(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, d0, d1, f, base, real = _revoked_member(hv)
    old, new = _fact(hv, d0, "old", ts(3)), _fact(hv, d0, "new", ts(3, 10))
    p = {"kind": "supersedes", "from_ref": [new["node_id"], new["seq"]], "to_ref": [old["node_id"], old["seq"]],
         "data": {}, "source": "manual"}
    for at, want in ((ts(3, 30), True), (ts(6), False)):
        e = _entry(hv, d1, "link", p, at, owner=o1)
        gov = _state(hv, base + [real, old, new, e])
        assert (hv._link_authority(e, old, gov) == "hard") is want


def test_a_capsule_carried_after_the_carriers_revoke_is_not_honoured(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, d0, d1, f, base, real = _revoked_member(hv)
    p = {"name": "secret", "version": 1, "alg": "x", "recipients": {}}
    gov = _state(hv, base + [real])
    signed = hv._sign_governance_payload(p, o1[0], o1[1])
    assert hv._is_authorized_writer(d1["id"], (ts(3, 30), d1["id"], 1), signed, gov, "owner") is True
    assert hv._is_authorized_writer(d1["id"], (ts(6), d1["id"], 1), signed, gov, "owner") is False


def test_an_uncarried_owner_act_loses_the_timestamp_shield(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    o1, o2, o3, d0, f, base, real = _world(hv)
    gov = _state(hv, base + [real])
    cfg = {"action": "set-config", "key": "cap_self", "value": 0.3}
    assert hv._ts_owner_signed(_entry(hv, d0, "governance", cfg, ts(5), owner=o1), gov) is True
    assert hv._ts_owner_signed(_entry(hv, f, "governance", cfg, ts(5), owner=o1), gov) is False
