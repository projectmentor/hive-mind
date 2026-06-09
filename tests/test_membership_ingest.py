"""Two-tier membership at the SYNC INGEST boundary (append_foreign_entries).

  • an authority-less device-signed `join-request` is ACCEPTED (so the owner's session-start
    tickle can surface the joiner) — but it grants NO authority;
  • a forged authority-bearing act (`admit`) WITHOUT an owner signature is REJECTED;
  • once an owner exists, CONTENT from an UNADMITTED device is REJECTED (read-only member: it reads
    the hive but does not pollute the shared journal) and ACCEPTED once the owner admits it;
  • a pre-owner (bootstrap) hive accepts content so the genesis owner declaration can propagate.

These exercise the real append_foreign_entries against an in-process hv bound to a temp HIVE_HOME,
with foreign entries signed by a fresh device key — the same path a peer sync drives.
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import merkle  # noqa: E402


def _run(home, *args):
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args],
                       env=dict(os.environ, HIVE_HOME=str(home)), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _loadhv(home, monkeypatch):
    """Load hv as a module bound to `home` (it reads HIVE_HOME at import)."""
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_mi", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_mi", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _entries(hv):
    return merkle.read_all_entries(str(hv.JOURNAL_DIR))


def _has_fact(hv, content):
    return any(e.get("type") == "fact" and e.get("payload", {}).get("content") == content
               for e in _entries(hv))


def _foreign_device(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._device_id_for_pub(pub)


def _signed(hv, seed, pub, nid, seq, etype, payload):
    e = {"node_id": nid, "seq": seq, "type": etype,
         "timestamp": f"2026-05-{seq:02d}T00:00:00Z", "payload": payload, "prev_hash": "sha256:genesis"}
    return hv._sign_entry(e, seed, pub)


def test_join_request_accepted_but_grants_no_authority(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    seed, pub, nid = _foreign_device(hv)
    jr = _signed(hv, seed, pub, nid, 1, "governance",
                 {"action": "join-request", "device_id": nid, "label": "joiner"})

    accepted, _ = hv.append_foreign_entries([jr])
    assert accepted == 1                                  # accepted on its device signature alone

    gov = hv._governance_state(_entries(hv))
    assert nid not in gov["admitted"]                     # but it confers NO authority
    pend = hv._pending_admissions(_entries(hv), gov)
    assert any(r["device_id"] == nid for r in pend)       # surfaces as pending → the tickle source


def test_forged_admit_without_owner_sig_is_rejected(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    seed, pub, nid = _foreign_device(hv)
    # Valid DEVICE envelope (passes _verify_entry) but NO owner signature in the payload.
    forged = _signed(hv, seed, pub, nid, 1, "governance", {"action": "admit", "device_id": nid})

    accepted, _ = hv.append_foreign_entries([forged])
    assert accepted == 0                                  # authority-bearing act needs an owner sig
    assert nid not in hv._governance_state(_entries(hv))["admitted"]   # no self-admission hole


def test_content_from_unadmitted_rejected_then_accepted_after_admit(tmp_path, monkeypatch):
    _run(tmp_path, "owner", "init")
    hv = _loadhv(tmp_path, monkeypatch)
    seed, pub, nid = _foreign_device(hv)
    fact = _signed(hv, seed, pub, nid, 1, "fact", {"content": "joiner claim", "source": "agent"})

    accepted, _ = hv.append_foreign_entries([fact])
    assert accepted == 0 and not _has_fact(hv, "joiner claim")   # read-only: content does not land

    _run(tmp_path, "admit", nid, "--principal", "joiner")        # owner admits the device
    accepted, _ = hv.append_foreign_entries([fact])
    assert accepted == 1 and _has_fact(hv, "joiner claim")       # now it lands


def test_preowner_bootstrap_accepts_content(tmp_path, monkeypatch):
    # No owner yet → bootstrap → accept everything so a genesis owner declaration can propagate.
    hv = _loadhv(tmp_path, monkeypatch)
    hv.init_db()                      # the real ingest caller (sync_now) inits dirs first
    seed, pub, nid = _foreign_device(hv)
    fact = _signed(hv, seed, pub, nid, 1, "fact", {"content": "early claim", "source": "agent"})

    accepted, _ = hv.append_foreign_entries([fact])
    assert accepted == 1 and _has_fact(hv, "early claim")
