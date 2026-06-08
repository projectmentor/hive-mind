"""D0-v2 governance: owner identity, journaled membership/config, and how they govern the
confidence projection (admission gate, same-device discount config, CAP_self). Each test drives
the real `hv` CLI against an isolated temp HIVE_HOME, writing the same content from DISTINCT
devices (HIVE_NODE_ID) where independent corroboration is intended.
"""

import copy
import importlib.machinery
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _run(home, *args, node_id=None, check=True):
    env = dict(os.environ, HIVE_HOME=str(home))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _conf(home, content):
    conn = sqlite3.connect(home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT confidence FROM facts WHERE content=?", (content,)).fetchone()
        return row["confidence"] if row else None
    finally:
        conn.close()


def _loadhv():
    loader = importlib.machinery.SourceFileLoader("hvmod_gov", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_gov", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_owner_admit_config_round_trip(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "admit", "k1:aaa", "--principal", "david")
    _run(home, "config", "set", "same_device_lambda", "0.4")
    hv = _loadhv()
    import merkle
    gov = hv._governance_state(merkle.read_all_entries(str(home / "journal")))
    assert gov["owner_id"] and gov["owner_id"].startswith("o1:")
    assert "k1:aaa" in gov["admitted"] and gov["principals"]["k1:aaa"] == "david"
    assert gov["config"]["same_device_lambda"] == 0.4


def test_reader_ignores_forged_and_non_owner_governance():
    hv = _loadhv()
    owner_seed = os.urandom(32)
    owner_pub = hv._ed25519.pub_from_seed(owner_seed)
    oid = hv._owner_id_for_pub(owner_pub)
    rogue_seed = os.urandom(32)
    rogue_pub = hv._ed25519.pub_from_seed(rogue_seed)

    def gov_entry(payload, seed, pub, seq):
        p = hv._sign_governance_payload(payload, seed, pub)
        return {"node_id": "n", "seq": seq, "type": "governance",
                "timestamp": f"2026-01-0{seq}T00:00:00Z", "payload": p, "prev_hash": "sha256:genesis"}

    entries = [
        gov_entry({"action": "owner", "owner_id": oid}, owner_seed, owner_pub, 1),
        gov_entry({"action": "admit", "device_id": "k1:legit"}, owner_seed, owner_pub, 2),  # owner
        gov_entry({"action": "admit", "device_id": "k1:rogue"}, rogue_seed, rogue_pub, 3),  # not owner
    ]
    gov = hv._governance_state(entries)
    assert "k1:legit" in gov["admitted"]
    assert "k1:rogue" not in gov["admitted"]          # signed by a non-owner key → ignored

    # tampering a genuine admit breaks its owner signature
    forged = copy.deepcopy(entries[1]["payload"])
    forged["device_id"] = "k1:evil"
    assert hv._verify_governance(forged) is None


def test_admission_gate_excludes_unadmitted_device(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "admit", "dev-a")
    _run(home, "remember", "gated claim", "--source", "agent", node_id="dev-a")
    _run(home, "remember", "gated claim", "--source", "agent", node_id="dev-x")  # NOT admitted
    # Only dev-a counts → one device → 0.45 (without the gate, two devices would be 0.675).
    assert abs(_conf(home, "gated claim") - 0.45) < 1e-6


def test_cap_self_clamps_single_principal(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    for d in ("dev-a", "dev-b", "dev-c"):
        _run(home, "admit", d, "--principal", "david")
        _run(home, "remember", "owned claim", "--source", "agent", node_id=d)
    # 3 devices → base 0.7875, but all one principal → clamped to cap_self (0.70).
    assert abs(_conf(home, "owned claim") - 0.70) < 1e-6


def test_cap_self_not_applied_across_principals(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    for d, pr in (("dev-a", "david"), ("dev-b", "david"), ("dev-c", "alice")):
        _run(home, "admit", d, "--principal", pr)
        _run(home, "remember", "shared claim", "--source", "agent", node_id=d)
    # Mixed principals → independent → no CAP_self clamp.
    assert abs(_conf(home, "shared claim") - 0.7875) < 1e-6


def test_config_lambda_zero_collapses_same_device(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "admit", "dev-a")
    _run(home, "config", "set", "same_device_lambda", "0")
    _run(home, "remember", "one box", "--source", "alice", node_id="dev-a")
    _run(home, "remember", "one box", "--source", "bob", node_id="dev-a")
    # lambda=0 → a device is one voice no matter how many agents → 0.45.
    assert abs(_conf(home, "one box") - 0.45) < 1e-6


def test_owner_forget_authority():
    # The forget-authorization rule (pure reader test): once an owner exists, only an owner-SIGNED
    # forget (or one predating the owner) is honored; an unsigned `owner:owner/owner` is ignored.
    hv = _loadhv()
    oseed = os.urandom(32)
    opub = hv._ed25519.pub_from_seed(oseed)
    oid = hv._owner_id_for_pub(opub)

    def gov_owner(ts):
        p = hv._sign_governance_payload({"action": "owner", "owner_id": oid}, oseed, opub)
        return {"node_id": "n", "seq": 1, "type": "governance", "timestamp": ts, "payload": p,
                "prev_hash": "sha256:genesis"}

    fact = {"node_id": "dev", "seq": 1, "type": "fact", "timestamp": "2026-02-01T00:00:00Z",
            "payload": {"content": "X", "source": "alice"}, "prev_hash": "sha256:genesis"}

    def forget(ts, signed):
        pl = {"retracts_ref": ["dev", 1], "reason": "", "source": "owner:owner/owner"}
        if signed:
            pl = hv._sign_governance_payload(pl, oseed, opub)
        return {"node_id": "dev", "seq": 2, "type": "retract", "timestamp": ts, "payload": pl,
                "prev_hash": "sha256:genesis"}

    owner = gov_owner("2026-01-01T00:00:00Z")   # owner established before the fact
    gov = hv._governance_state([owner])

    # unsigned forget AFTER the owner -> ignored (the closed hole)
    ev = hv._content_evidence([owner, fact, forget("2026-03-01T00:00:00Z", signed=False)], gov)
    assert ev["X"]["forget"] is False
    # owner-signed forget -> honored
    ev = hv._content_evidence([owner, fact, forget("2026-03-01T00:00:00Z", signed=True)], gov)
    assert ev["X"]["forget"] is True
    # unsigned forget that PREDATES the owner -> grandfathered (honored)
    late_owner = gov_owner("2026-04-01T00:00:00Z")
    gov2 = hv._governance_state([late_owner])
    ev = hv._content_evidence([late_owner, fact, forget("2026-03-01T00:00:00Z", signed=False)], gov2)
    assert ev["X"]["forget"] is True
    # with NO owner at all -> legacy unsigned forget honored
    ev = hv._content_evidence([fact, forget("2026-03-01T00:00:00Z", signed=False)], None)
    assert ev["X"]["forget"] is True
