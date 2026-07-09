"""The `announce` act (kind:key) — capsule-addressability for silent devices.

A device the owner admits DIRECTLY (`hv admit k1:X`, no join-request) that never authors an entry
has no harvestable pub, so `_recipient_pubkeys` can never seal a capsule to it. `announce` is an
authority-less, DEVICE-signed governance no-op whose only value is to EXIST as a signed entry
carrying `pub`. These pin:

  • ingest: an announce is accepted pre-admission on its device signature alone (like a
    join-request), grants NO authority, and a forged one (node_id not bound to the signing key)
    is rejected; UNKNOWN announce kinds are accepted and ignored (future kinds ride through
    this contract's nodes with no version-skew rejection window);
  • harvest: the announce entry makes the device a capsule recipient (leaves `missing`);
  • CLI: `hv key announce` emits exactly once (idempotent), and ANY prior signed write
    suppresses it (guard subsumption);
  • guard hardening: a planted pub on a spoofed UNSIGNED entry (grandfathered by _verify_entry)
    can never fingerprint-match, so it must NOT suppress the announcement;
  • doctor --fix: auto-emits once on an owned hive, previews under --dry-run, and stays quiet
    with no owner established (no governance noise from standalone nodes).
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import merkle   # noqa: E402
import ed25519  # noqa: E402
import x25519   # noqa: E402


def _run(home, *args, check=True):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args],
                       env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_ann", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_ann", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _entries(home):
    return merkle.read_all_entries(str(Path(home) / "journal"))


def _announces_by(home, device_id):
    return [e for e in _entries(home)
            if e.get("type") == "governance"
            and e.get("payload", {}).get("action") == "announce"
            and e.get("node_id") == device_id]


def _foreign_device(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._device_id_for_pub(pub)


def _signed(hv, seed, pub, nid, seq, payload):
    e = {"node_id": nid, "seq": seq, "type": "governance",
         "timestamp": f"2026-07-{seq:02d}T00:00:00Z", "payload": payload, "prev_hash": "sha256:genesis"}
    return hv._sign_entry(e, seed, pub)


def _announce_payload(nid, kind="key", label="silent"):
    return {"action": "announce", "kind": kind, "data": {"device_id": nid, "label": label}}


def _no_peers(home):
    """Pin an empty peer list so doctor never falls back to the repo's real .peers.json and
    probes live nodes from inside a test."""
    (Path(home) / ".peers.json").write_text(json.dumps({"self": "test", "port": 9876, "peers": []}))


# ── ingest ────────────────────────────────────────────────────────────────────────────────────

def test_announce_accepted_pre_admission_grants_nothing(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    seed, pub, nid = _foreign_device(hv)
    ann = _signed(hv, seed, pub, nid, 1, _announce_payload(nid))

    accepted, _ = hv.append_foreign_entries([ann])
    assert accepted == 1                                   # accepted on its device signature alone
    gov = hv._governance_state(_entries(tmp_path))
    assert nid not in gov["admitted"]                      # projection-invisible: NO authority
    assert not any(r["device_id"] == nid                   # and not a join ask either
                   for r in hv._pending_admissions(_entries(tmp_path), gov))


def test_forged_announce_is_rejected(tmp_path, monkeypatch):
    """node_id not bound to the signing key → _verify_entry fails → rejected at ingest."""
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    seed, pub, _nid = _foreign_device(hv)
    victim = "k1:victimdevice00"
    forged = _signed(hv, seed, pub, victim, 1, _announce_payload(victim))

    accepted, _ = hv.append_foreign_entries([forged])
    assert accepted == 0
    assert not _announces_by(tmp_path, victim)


def test_unknown_announce_kind_rides_through(tmp_path, monkeypatch):
    """A future announce kind is accepted (no skew window) and changes nothing today."""
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    seed, pub, nid = _foreign_device(hv)
    future = _signed(hv, seed, pub, nid, 1,
                     {"action": "announce", "kind": "banner", "data": {"motd": "hello"}})

    accepted, _ = hv.append_foreign_entries([future])
    assert accepted == 1
    assert nid not in hv._governance_state(_entries(tmp_path))["admitted"]


# ── harvest ───────────────────────────────────────────────────────────────────────────────────

def test_announce_makes_device_harvestable(tmp_path, monkeypatch):
    """An admitted device with zero entries is `missing`; its announce alone makes it a
    capsule recipient (identity-derived key), not `suspect`."""
    hv = _loadhv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    ed_pub = ed25519.pub_from_seed(seed)
    nid = hv._device_id_for_pub(ed_pub)
    gov = {"admitted": {nid}, "purged": set()}

    recips, missing, suspect = hv._recipient_pubkeys([], gov)
    assert nid in missing and nid not in recips            # silent device: unaddressable

    ann = _signed(hv, seed, ed_pub, nid, 1, _announce_payload(nid))
    recips, missing, suspect = hv._recipient_pubkeys([ann], gov)
    assert recips[nid] == x25519.ed_pub_to_curve_pub(ed_pub)
    assert nid not in missing and nid not in suspect


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def test_key_announce_emits_once(tmp_path):
    r = _run(tmp_path, "key", "announce")                  # fresh home: auto-mints, then emits
    assert "Announced" in r.stdout
    did = (tmp_path / ".device-id").read_text().strip()
    assert len(_announces_by(tmp_path, did)) == 1

    r = _run(tmp_path, "key", "announce")                  # second run: guard short-circuits
    assert "already harvestable" in r.stdout
    assert len(_announces_by(tmp_path, did)) == 1


def test_any_signed_write_suppresses_announce(tmp_path):
    _run(tmp_path, "key", "init")
    _run(tmp_path, "remember", "a plain signed write")     # signed → pub already harvestable
    r = _run(tmp_path, "key", "announce")
    assert "already harvestable" in r.stdout
    did = (tmp_path / ".device-id").read_text().strip()
    assert not _announces_by(tmp_path, did)


def test_planted_pub_does_not_suppress_announce(tmp_path):
    """_verify_entry grandfathers UNSIGNED entries, so a spoofed entry with node_id=victim and a
    planted pub can land in a journal — but it can never fingerprint-match, so the guard must
    still emit (otherwise an attacker could keep a device capsule-blind forever)."""
    _run(tmp_path, "key", "init")
    did = (tmp_path / ".device-id").read_text().strip()
    jdir = tmp_path / "journal"
    jdir.mkdir(exist_ok=True)
    spoof = {"node_id": did, "seq": 1, "type": "fact", "timestamp": "2026-07-01T00:00:00Z",
             "payload": {"content": "spoof"}, "prev_hash": "sha256:genesis",
             "pub": base64.b64encode(os.urandom(32)).decode()}      # planted, unbound pub
    (jdir / "spoof.jsonl").write_text(json.dumps(spoof) + "\n")

    r = _run(tmp_path, "key", "announce")
    assert "Announced" in r.stdout
    assert len(_announces_by(tmp_path, did)) == 1


# ── doctor --fix self-heal ────────────────────────────────────────────────────────────────────

def _silent_member_home(home, owner_home):
    """A keyed device that synced an owned hive's journal but never authored anything — the
    direct-admit shape the self-heal exists for."""
    _run(home, "key", "init")
    _no_peers(home)
    shutil.copytree(owner_home / "journal", home / "journal", dirs_exist_ok=True)


def test_doctor_fix_announces_once_on_owned_hive(tmp_path):
    owner, member = tmp_path / "owner", tmp_path / "member"
    owner.mkdir(); member.mkdir()
    _run(owner, "owner", "init")
    _silent_member_home(member, owner)
    did = (member / ".device-id").read_text().strip()

    out = _run(member, "doctor", "--fix", check=False).stdout
    assert "announced this device's signing key" in out
    assert len(_announces_by(member, did)) == 1

    out = _run(member, "doctor", "--fix", check=False).stdout   # idempotent: guard re-satisfied
    assert "announced this device's signing key" not in out
    assert len(_announces_by(member, did)) == 1


def test_doctor_fix_dry_run_previews_without_emitting(tmp_path):
    owner, member = tmp_path / "owner", tmp_path / "member"
    owner.mkdir(); member.mkdir()
    _run(owner, "owner", "init")
    _silent_member_home(member, owner)
    did = (member / ".device-id").read_text().strip()

    out = _run(member, "doctor", "--fix", "--dry-run", check=False).stdout
    assert "would announce" in out
    assert not _announces_by(member, did)


def test_doctor_fix_stays_quiet_without_an_owner(tmp_path):
    """A standalone / pre-owner node must not auto-append governance noise on the 15-min timer;
    deliberate pre-owner use is `hv key announce`."""
    _run(tmp_path, "key", "init")
    _no_peers(tmp_path)
    did = (tmp_path / ".device-id").read_text().strip()

    out = _run(tmp_path, "doctor", "--fix", check=False).stdout
    assert "announce" not in out.lower()
    assert not _announces_by(tmp_path, did)
