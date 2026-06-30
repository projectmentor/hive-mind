"""Projection-level WRITE authorization for capsules (PR feat/projection-authz, hive #58/#59/#61).

`_capsule_state` folds only entries whose signer was the AUTHORIZED writer:
  - capsule_putters=owner  -> the payload must carry an owner signature, and that owner must have
    been the legitimate owner AS OF the entry's journal position (POINT-IN-TIME).
  - capsule_putters=fertile -> any currently admitted, non-purged device.
The unauthorized entry still LANDS in the journal (convergence-safe); only the projection declines
it. These tests pin that a compromised admitted device can no longer supersede/tombstone a capsule.
"""

import base64
import hashlib
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import ed25519  # noqa: E402


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_authz", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_authz", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _owner_key(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._owner_id_for_pub(pub)


def _device_key(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._device_id_for_pub(pub)


def _gov(hv, payload, oseed, opub, ts, seq=1, nid="ownerdev"):
    p = hv._sign_governance_payload(payload, oseed, opub)
    return {"node_id": nid, "seq": seq, "type": "governance", "timestamp": ts, "payload": p}


def _capsule(hv, name, version, dseed, dpub, ts, seq=1, tombstone=False, owner=None):
    """A device-signed capsule entry. `owner=(oseed,opub)` additionally owner-signs the payload
    (proving owner-key possession, as `hv capsule put` does on the owner device)."""
    payload = {"capsule_id": hashlib.sha256(name.encode()).hexdigest()[:16], "name": name,
               "kind": "tombstone" if tombstone else "credential", "version": version,
               "alg": "tombstone-v1" if tombstone else hv._CAPSULE_ALG, "wraps": []}
    if owner is not None:
        payload = hv._sign_governance_payload(payload, owner[0], owner[1])
    e = {"node_id": hv._device_id_for_pub(dpub), "seq": seq, "type": "capsule",
         "timestamp": ts, "payload": payload}
    return hv._sign_entry(e, dseed, dpub)


def test_nonowner_tombstone_declined_owner_signed_honored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    od_s, od_p, od_nid = _device_key(hv)        # the owner's device (seals owner-signed)
    d_s, d_p, d_nid = _device_key(hv)           # a compromised admitted NON-owner device

    base = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "admit", "device_id": d_nid, "principal": "p"}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _capsule(hv, "MYTOK", 1, od_s, od_p, "2026-01-03T00:00:00Z", 3, owner=(oseed, opub)),
    ]
    # The attacker (admitted, non-owner) tries to tombstone MYTOK by a direct journal append.
    attack = _capsule(hv, "MYTOK", 2, d_s, d_p, "2026-01-04T00:00:00Z", 1, tombstone=True)
    entries = base + [attack]
    gov = hv._governance_state(entries)
    caps = hv._capsule_state(entries, gov)
    assert caps["MYTOK"]["alg"] == hv._CAPSULE_ALG          # tombstone DECLINED — the seal stands
    # Convergence: the attack entry still LANDS in the journal; only the projection ignores it.
    assert any(e["type"] == "capsule" and e["payload"].get("alg") == "tombstone-v1" for e in entries)
    # The doctor visibility check surfaces exactly that declined entry.
    assert any("MYTOK v2" in s for s in hv._content_unauthorized(entries, gov, "capsule", "capsule_putters"))

    # The OWNER's own tombstone (owner-signed) IS honored.
    legit = base + [_capsule(hv, "MYTOK", 2, od_s, od_p, "2026-01-04T00:00:00Z", 4, tombstone=True, owner=(oseed, opub))]
    caps2 = hv._capsule_state(legit, hv._governance_state(legit))
    assert caps2["MYTOK"]["alg"] == "tombstone-v1"


def test_nonowner_seal_declined_under_owner_policy(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    d_s, d_p, d_nid = _device_key(hv)
    entries = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _capsule(hv, "FOO", 1, d_s, d_p, "2026-01-03T00:00:00Z", 1),   # admitted but NOT owner-signed
    ]
    caps = hv._capsule_state(entries, hv._governance_state(entries))
    assert "FOO" not in caps                                # no owner_sig → declined under owner policy


def test_point_in_time_prior_owner_seal_survives_transfer(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a_seed, a_pub, a_oid = _owner_key(hv)        # owner A (genesis)
    b_seed, b_pub, b_oid = _owner_key(hv)        # owner B (successor)
    ad_s, ad_p, _ = _device_key(hv)              # A's device

    entries = [
        _gov(hv, {"action": "owner", "owner_id": a_oid, "hive_id": "h1"}, a_seed, a_pub, "2026-01-01T00:00:00Z", 1),
        _capsule(hv, "MYTOK", 1, ad_s, ad_p, "2026-01-02T00:00:00Z", 1, owner=(a_seed, a_pub)),  # A seals while owner
        _gov(hv, {"action": "transfer", "new_owner_pub": base64.b64encode(b_pub).decode()},
             a_seed, a_pub, "2026-01-03T00:00:00Z", 2),                                          # A -> B
    ]
    gov = hv._governance_state(entries)
    assert gov["owner_id"] == b_oid                          # B is the current owner
    assert [t[3] for t in gov["owner_timeline"]] == [a_oid, b_oid]
    caps = hv._capsule_state(entries, gov)
    assert caps["MYTOK"]["alg"] == hv._CAPSULE_ALG           # A's prior seal SURVIVES the transfer

    # But a NEW seal by retired owner A, positioned AFTER the transfer, is declined (A no longer owner there).
    late = entries + [_capsule(hv, "MYTOK", 2, ad_s, ad_p, "2026-01-04T00:00:00Z", 2, owner=(a_seed, a_pub))]
    caps2 = hv._capsule_state(late, hv._governance_state(late))
    assert caps2["MYTOK"]["version"] == 1                    # v2 by retired A declined; v1 still honored


def test_pre_owner_is_permissive(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d_s, d_p, _ = _device_key(hv)
    entries = [_capsule(hv, "FOO", 1, d_s, d_p, "2026-01-01T00:00:00Z", 1)]   # no owner anywhere
    gov = hv._governance_state(entries)
    assert gov["owner_id"] is None
    assert "FOO" in hv._capsule_state(entries, gov)          # bootstrap: permissive


def test_fertile_admitted_honored_purged_declined(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    d_s, d_p, d_nid = _device_key(hv)
    base = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "set-config", "key": "capsule_putters", "value": "fertile"}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-03T00:00:00Z", 3),
        _capsule(hv, "FOO", 1, d_s, d_p, "2026-01-04T00:00:00Z", 1),    # admitted device, no owner_sig
    ]
    gov = hv._governance_state(base)
    assert gov["config"]["capsule_putters"] == "fertile"
    assert "FOO" in hv._capsule_state(base, gov)             # fertile: admitted device's write honored

    purged = base + [_gov(hv, {"action": "purge", "device_id": d_nid}, oseed, opub, "2026-01-05T00:00:00Z", 4)]
    gov2 = hv._governance_state(purged)
    assert "FOO" not in hv._capsule_state(purged, gov2)      # purge retroactively unhonors (documented)


def test_stripped_build_keeps_honoring(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    d_s, d_p, d_nid = _device_key(hv)
    entries = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _capsule(hv, "MYTOK", 1, d_s, d_p, "2026-01-03T00:00:00Z", 1, tombstone=True),  # non-owner tombstone
    ]
    gov = hv._governance_state(entries)                      # computed while crypto is present
    monkeypatch.setattr(hv, "_ed25519", None)               # now simulate a crypto-less (stripped) build
    caps = hv._capsule_state(entries, gov)
    assert caps["MYTOK"]["alg"] == "tombstone-v1"            # keeps honoring rather than silently emptying
    assert hv._content_unauthorized(entries, gov, "capsule", "capsule_putters") == []     # authz inactive on a stripped build
