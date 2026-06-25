"""Security hardening — capsule recipient-key trust.

`_recipient_pubkeys` must seal capsules ONLY to a Curve25519 key it can derive from a device's
own signed Ed25519 identity (which is cryptographically bound to its device_id). A `curve_pub`
merely *carried* on a join-request/admit payload has no proof-of-possession and must NEVER be
trusted as a key source — it is only checked for disagreement (a tamper tripwire → `suspect`).

Also pins `hv join` idempotency: a device cannot pile up duplicate (potentially curve_pub-churning)
join-requests before admission.
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import x25519        # noqa: E402
import ed25519       # noqa: E402


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_rpk", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_rpk", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _b64(b):
    return base64.b64encode(b).decode()


def _identity_entry(hv, ed_pub, seq=1):
    """A signed-shape entry whose node_id is the fingerprint of ed_pub (the binding harvest relies
    on). _recipient_pubkeys checks node_id == _device_id_for_pub(pub); signature isn't re-verified
    here (that happens at ingest), so a plain dict with the bound pub/node_id suffices."""
    nid = hv._device_id_for_pub(ed_pub)
    return nid, {"type": "fact", "node_id": nid, "seq": seq, "pub": _b64(ed_pub)}


def _carried(device_id, curve_pub, signer_id="k1:owner000", action="join-request", seq=9):
    return {"type": "governance", "node_id": signer_id, "seq": seq,
            "payload": {"action": action, "device_id": device_id, "curve_pub": _b64(curve_pub)}}


def _gov(admitted, purged=()):
    return {"admitted": set(admitted), "purged": set(purged)}


def test_identity_derived_key_is_used_on_the_honest_path(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    ed_pub = ed25519.pub_from_seed(seed)
    derived = x25519.ed_pub_to_curve_pub(ed_pub)
    dev, ent = _identity_entry(hv, ed_pub)
    recips, missing, suspect = hv._recipient_pubkeys([ent], _gov([dev]))
    assert recips[dev] == derived          # sealed to the key derived from the signed identity
    assert not missing and not suspect


def test_carried_curve_pub_without_identity_is_never_trusted(tmp_path, monkeypatch):
    """The core vuln: a device admitted with NO signed identity entry, but a curve_pub carried on a
    join-request. Pre-fix this was sealed-to on sight (no proof-of-possession). It must now be
    refused → the device is `missing`, never a recipient, and flagged `suspect`."""
    hv = _loadhv(tmp_path, monkeypatch)
    attacker_cp = os.urandom(32)
    dev = "k1:victimdevice00"          # admitted, but never wrote a signed entry → no derivable key
    entries = [_carried(dev, attacker_cp)]
    recips, missing, suspect = hv._recipient_pubkeys(entries, _gov([dev]))
    assert dev not in recips           # attacker-chosen key is NOT used
    assert dev in missing
    assert dev in suspect


def test_carried_curve_pub_contradicting_identity_is_ignored_and_flagged(tmp_path, monkeypatch):
    """Device HAS a signed identity, but an attacker also planted a different carried curve_pub.
    The identity-derived key must win (carried never overrides), and the contradiction is a
    tamper signal → `suspect`."""
    hv = _loadhv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    ed_pub = ed25519.pub_from_seed(seed)
    derived = x25519.ed_pub_to_curve_pub(ed_pub)
    dev, ident = _identity_entry(hv, ed_pub, seq=1)
    wrong_cp = os.urandom(32)
    entries = [ident, _carried(dev, wrong_cp)]
    recips, missing, suspect = hv._recipient_pubkeys(entries, _gov([dev]))
    assert recips[dev] == derived      # derived wins; the planted value is ignored
    assert dev not in missing
    assert dev in suspect


def test_carried_curve_pub_matching_identity_is_not_suspect(tmp_path, monkeypatch):
    """A carried curve_pub that exactly equals the derived value is honest/redundant — used (via
    derivation) and NOT flagged."""
    hv = _loadhv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    ed_pub = ed25519.pub_from_seed(seed)
    derived = x25519.ed_pub_to_curve_pub(ed_pub)
    dev, ident = _identity_entry(hv, ed_pub, seq=1)
    entries = [ident, _carried(dev, derived)]
    recips, missing, suspect = hv._recipient_pubkeys(entries, _gov([dev]))
    assert recips[dev] == derived
    assert not missing and not suspect


def test_purged_device_never_a_recipient_or_missing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    seed = os.urandom(32)
    ed_pub = ed25519.pub_from_seed(seed)
    dev, ident = _identity_entry(hv, ed_pub)
    recips, missing, suspect = hv._recipient_pubkeys([ident], _gov([dev], purged=[dev]))
    assert dev not in recips and dev not in missing


def _run(home, *args, node_id=None):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _join_requests_for(home, device_id):
    import merkle
    entries = merkle.read_all_entries(str(Path(home) / "journal"))
    return [e for e in entries
            if e.get("type") == "governance"
            and e.get("payload", {}).get("action") == "join-request"
            and e["payload"].get("device_id") == device_id]


def test_join_is_idempotent_no_duplicate_requests(tmp_path):
    """A second `hv join` while a request is already pending must NOT append another join-request
    (journal hygiene; denies pre-admission curve_pub churn)."""
    home = tmp_path
    _run(home, "owner", "init")                      # owner established (owner device)
    member = "k1:joiner00000001"
    _run(home, "join", node_id=member)               # first ask → one join-request
    assert len(_join_requests_for(home, member)) == 1
    out = _run(home, "join", node_id=member).stdout   # second ask → short-circuits
    assert "already pending" in out.lower()
    assert len(_join_requests_for(home, member)) == 1   # still exactly one
