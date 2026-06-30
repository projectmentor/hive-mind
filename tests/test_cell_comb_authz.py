"""Projection-level WRITE authorization for cells/combs (PR feat/cell-comb-authz, hive #60).

`_cell_state`/`_comb_state` apply the same authorization as capsules but under a SEPARATE
`cell_writers` policy (default owner), so executable-definition authority is independent of capsule
authority. A cell defines what an agent/tool runs, so a non-owner redefinition under the owner policy
is declined by the projection (and refused by `hv wire --add`).
"""

import base64
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
    loader = importlib.machinery.SourceFileLoader("hvmod_cellauthz", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_cellauthz", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _owner_key(hv):
    seed = os.urandom(32); pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._owner_id_for_pub(pub)


def _device_key(hv):
    seed = os.urandom(32); pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._device_id_for_pub(pub)


def _gov(hv, payload, oseed, opub, ts, seq=1, nid="ownerdev"):
    p = hv._sign_governance_payload(payload, oseed, opub)
    return {"node_id": nid, "seq": seq, "type": "governance", "timestamp": ts, "payload": p}


def _content(hv, ctype, name, version, dseed, dpub, ts, seq=1, owner=None, extra=None):
    """A device-signed cell/comb/capsule entry; `owner=(oseed,opub)` also owner-signs the payload."""
    payload = {"name": name, "version": version}
    if extra:
        payload.update(extra)
    if owner is not None:
        payload = hv._sign_governance_payload(payload, owner[0], owner[1])
    e = {"node_id": hv._device_id_for_pub(dpub), "seq": seq, "type": ctype, "timestamp": ts, "payload": payload}
    return hv._sign_entry(e, dseed, dpub)


def test_cell_nonowner_declined_owner_honored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    od_s, od_p, _ = _device_key(hv)            # owner's device
    d_s, d_p, d_nid = _device_key(hv)          # compromised admitted non-owner

    base = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _content(hv, "cell", "deploy", 1, od_s, od_p, "2026-01-03T00:00:00Z", 3,
                 owner=(oseed, opub), extra={"kind": "tool", "run": "safe.sh"}),
    ]
    # Attacker redefines the executable cell `deploy` by a direct journal append.
    attack = _content(hv, "cell", "deploy", 2, d_s, d_p, "2026-01-04T00:00:00Z", 1,
                      extra={"kind": "tool", "run": "evil.sh"})
    entries = base + [attack]
    gov = hv._governance_state(entries)
    cells = hv._cell_state(entries, gov)
    assert cells["deploy"]["run"] == "safe.sh"                # malicious redefinition DECLINED
    assert any("deploy v2" in s for s in
               hv._content_unauthorized(entries, gov, "cell", "cell_writers"))
    # convergence: the attack entry is still in the raw journal
    assert any(e["type"] == "cell" and e["payload"].get("run") == "evil.sh" for e in entries)

    # An OWNER-signed redefinition IS honored.
    legit = base + [_content(hv, "cell", "deploy", 2, od_s, od_p, "2026-01-04T00:00:00Z", 4,
                             owner=(oseed, opub), extra={"kind": "tool", "run": "safe-v2.sh"})]
    assert hv._cell_state(legit, hv._governance_state(legit))["deploy"]["run"] == "safe-v2.sh"


def test_comb_nonowner_declined(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    od_s, od_p, _ = _device_key(hv)
    d_s, d_p, d_nid = _device_key(hv)
    entries = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _content(hv, "comb", "pipeline", 1, od_s, od_p, "2026-01-03T00:00:00Z", 3,
                 owner=(oseed, opub), extra={"cells": ["a", "b"]}),
        _content(hv, "comb", "pipeline", 2, d_s, d_p, "2026-01-04T00:00:00Z", 1,
                 extra={"cells": ["evil"]}),                 # non-owner supersession
    ]
    combs = hv._comb_state(entries, hv._governance_state(entries))
    assert combs["pipeline"]["cells"] == ["a", "b"]          # non-owner redefinition declined


def test_cell_writers_fertile_admitted_honored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    d_s, d_p, d_nid = _device_key(hv)
    entries = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "set-config", "key": "cell_writers", "value": "fertile"}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-03T00:00:00Z", 3),
        _content(hv, "cell", "tool1", 1, d_s, d_p, "2026-01-04T00:00:00Z", 1, extra={"kind": "tool"}),
    ]
    gov = hv._governance_state(entries)
    assert gov["config"]["cell_writers"] == "fertile"
    assert "tool1" in hv._cell_state(entries, gov)           # fertile: admitted device's cell honored


def test_cell_and_capsule_policies_are_independent(tmp_path, monkeypatch):
    """capsule_putters=fertile but cell_writers=owner (default): an admitted non-owner device's
    capsule is honored, while its cell is declined — the two policies do not bleed into each other."""
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    d_s, d_p, d_nid = _device_key(hv)
    entries = [
        _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, "2026-01-01T00:00:00Z", 1),
        _gov(hv, {"action": "set-config", "key": "capsule_putters", "value": "fertile"}, oseed, opub, "2026-01-02T00:00:00Z", 2),
        _gov(hv, {"action": "admit", "device_id": d_nid}, oseed, opub, "2026-01-03T00:00:00Z", 3),
        _content(hv, "capsule", "TOK", 1, d_s, d_p, "2026-01-04T00:00:00Z", 1, extra={"alg": hv._CAPSULE_ALG, "wraps": []}),
        _content(hv, "cell", "deploy", 1, d_s, d_p, "2026-01-05T00:00:00Z", 2, extra={"kind": "tool"}),
    ]
    gov = hv._governance_state(entries)
    assert "TOK" in hv._capsule_state(entries, gov)          # capsule fertile → admitted device honored
    assert "deploy" not in hv._cell_state(entries, gov)      # cell still owner-only → declined


def test_point_in_time_prior_owner_cell_survives_transfer(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a_seed, a_pub, a_oid = _owner_key(hv)
    b_seed, b_pub, b_oid = _owner_key(hv)
    ad_s, ad_p, _ = _device_key(hv)
    entries = [
        _gov(hv, {"action": "owner", "owner_id": a_oid, "hive_id": "h1"}, a_seed, a_pub, "2026-01-01T00:00:00Z", 1),
        _content(hv, "cell", "deploy", 1, ad_s, ad_p, "2026-01-02T00:00:00Z", 1,
                 owner=(a_seed, a_pub), extra={"kind": "tool", "run": "a.sh"}),
        _gov(hv, {"action": "transfer", "new_owner_pub": base64.b64encode(b_pub).decode()},
             a_seed, a_pub, "2026-01-03T00:00:00Z", 2),
    ]
    gov = hv._governance_state(entries)
    assert gov["owner_id"] == b_oid
    assert hv._cell_state(entries, gov)["deploy"]["run"] == "a.sh"   # prior owner's cell survives transfer
