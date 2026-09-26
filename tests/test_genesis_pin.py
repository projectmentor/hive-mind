"""The accepted genesis declaration is PINNED, so a rival `owner` act can never take the hive
(hive-mind-private #14).

Genesis used to be "the earliest valid self-signed `owner` act wins" (TOFU by timestamp), and a
journal timestamp is attacker-controlled, so "first" never meant "first to arrive". A rival `owner`
act carrying an EARLIER timestamp — and reusing the victim's `hive_id`, so the daemon's cross-hive
guard waves it through — was accepted at ingest and projected as the owner. The real owner's later
acts then stopped counting, because only the CURRENT owner's acts are honoured. That is a full
governance takeover, before or after genesis.

The fix is a local pin: `$HIVE_HOME/.genesis-pin` (0600, never synced, never journaled) records the
one `owner` act this node accepted. It is the authority:

  * ingest refuses any `owner` act that is not the pinned one, and refuses a NEW unsigned entry
    once a genesis is pinned (an unsigned entry could otherwise claim an admitted device's future
    `(node_id, seq)` and permanently fork the merkle root);
  * the projection resolves genesis THROUGH the pin, so a rival already on disk — which the
    append-only journal never removes — still loses;
  * a node with no pin keeps the legacy rule, and `hv doctor genesis` fails so the operator sees it.

Historical unsigned lines already on disk keep projecting: the read path never changes.
"""

import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

HIVE_ID = "h1:cf5b2e8adbe05936"
T_GENESIS = "2026-01-01T00:00:00Z"
T_BACKDATED = "2025-12-31T00:00:00Z"        # earlier than genesis: what the old rule preferred
T_LATER = "2026-02-01T00:00:00Z"


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_pin", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_pin", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    m.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return m


@pytest.fixture
def hv(tmp_path, monkeypatch):
    return _loadhv(tmp_path, monkeypatch)


def _owner_key(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._owner_id_for_pub(pub)


def _device_key(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._device_id_for_pub(pub)


def _gov(hv, payload, oseed, opub, ts, seq=1, nid="ownerdev", dev=None):
    """An owner-signed governance entry. `dev=(seed,pub)` also DEVICE-signs it, as a real node does."""
    e = {"node_id": nid, "seq": seq, "type": "governance", "timestamp": ts,
         "payload": hv._sign_governance_payload(payload, oseed, opub)}
    return hv._sign_entry(e, dev[0], dev[1]) if dev else e


def _owner_act(hv, key, ts, seq=1, nid="ownerdev", hive_id=HIVE_ID, dev=None):
    oseed, opub, oid = key
    return _gov(hv, {"action": "owner", "owner_id": oid, "owner_pub": hv._canon_pub(hv.base64.b64encode(opub).decode()),
                     "hive_id": hive_id}, oseed, opub, ts, seq, nid, dev)


def _fact(hv, nid, ts, content, seq=1, dev=None):
    e = {"node_id": nid, "seq": seq, "type": "fact", "timestamp": ts,
         "payload": {"content": content, "tags": [], "source": "manual"}}
    return hv._sign_entry(e, dev[0], dev[1]) if dev else e


def _on_disk(hv, entries, day="2026-01-01"):
    """Put entries in the journal the way a synced node holds them, and project from disk."""
    (hv.JOURNAL_DIR / f"{day}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return hv.merkle.read_all_entries(hv.JOURNAL_DIR)


def _pin(hv, genesis_entry):
    hv._write_genesis_pin(hv._genesis_pin_for(genesis_entry))
    return hv._load_genesis_pin()


# ── Step 0, the proof of concept: the rival must not win ────────────────────────────────────────

def test_a_backdated_rival_cannot_displace_the_pinned_owner(hv):
    real, attacker = _owner_key(hv), _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    rival = _owner_act(hv, attacker, T_BACKDATED, nid="attackerdev")   # earlier ts, same hive_id
    _pin(hv, genesis)

    gov = hv._governance_state([genesis, rival])
    assert gov["owner_id"] == real[2]                  # the pin decides, not the timestamp
    assert gov["hive_id"] == HIVE_ID
    assert gov["genesis"]["pinned"] is True


def test_the_real_owners_later_acts_still_count_after_a_rival_is_stored(hv):
    """The takeover's real damage: displacing genesis silently voids every later act of the true owner."""
    real, attacker = _owner_key(hv), _owner_key(hv)
    _, _, dev_id = _device_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    rival = _owner_act(hv, attacker, T_BACKDATED, nid="attackerdev")
    admit = _gov(hv, {"action": "admit", "device_id": dev_id, "principal": "p"}, real[0], real[1], T_LATER, seq=2)
    _pin(hv, genesis)

    gov = hv._governance_state([genesis, rival, admit])
    assert gov["owner_id"] == real[2]
    assert dev_id in gov["admitted"]                   # the owner's admit is honoured again


def test_a_rival_already_on_disk_still_loses(hv):
    """The append-only journal never removes the rival, so the projection — not a delete — is the fix."""
    real, attacker = _owner_key(hv), _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    rival = _owner_act(hv, attacker, T_BACKDATED, nid="attackerdev")
    entries = _on_disk(hv, [genesis, rival])
    _pin(hv, genesis)

    assert hv._governance_state(entries)["owner_id"] == real[2]
    assert len(entries) == 2                           # both lines stay in the journal


def test_with_no_pin_the_legacy_rule_still_applies_and_doctor_can_see_it(hv):
    """A legacy node must keep converging exactly as before; the pin is opt-in per node, and its
    absence is an operator warning, never a silent behaviour change."""
    real, attacker = _owner_key(hv), _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    rival = _owner_act(hv, attacker, T_BACKDATED, nid="attackerdev")

    gov = hv._governance_state([genesis, rival])
    assert gov["owner_id"] == attacker[2]              # unchanged legacy projection (earliest wins)
    assert gov["genesis"]["pinned"] is False
    assert gov["genesis"]["candidates"] == 2           # what `hv doctor genesis` fails on


# ── Ingest: the rival never lands in the first place ────────────────────────────────────────────

def test_ingest_refuses_a_rival_owner_act_once_pinned(hv):
    real, attacker = _owner_key(hv), _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    _on_disk(hv, [genesis])
    _pin(hv, genesis)

    rival = _owner_act(hv, attacker, T_BACKDATED, nid="attackerdev")
    assert hv.append_foreign_entries([rival])[:2] == (0, 0)
    assert not any(e["payload"].get("owner_id") == attacker[2]
                   for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR))


def test_ingest_still_takes_the_pinned_genesis_as_a_duplicate(hv):
    """Re-pushing the genesis this node already holds is a duplicate, not a rejection."""
    real = _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    _on_disk(hv, [genesis])
    _pin(hv, genesis)

    assert hv.append_foreign_entries([genesis])[:2] == (0, 1)


def _owned(hv, real, extra=()):
    """A pinned hive: genesis + an admitted device, plus whatever else is already on disk."""
    dseed, dpub, dev_id = _device_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    admit = _gov(hv, {"action": "admit", "device_id": dev_id, "principal": "p"},
                 real[0], real[1], T_GENESIS, seq=2)
    _on_disk(hv, [genesis, admit] + list(extra))
    _pin(hv, genesis)
    return (dseed, dpub), dev_id


def test_an_unsigned_entry_above_a_held_chain_tip_is_refused(hv):
    """Finding 3, the sequence squat. An unsigned entry claiming a seq beyond a device's tip used to
    BECOME that device's chain tip, so the device's own later entry was then dropped as a duplicate:
    two different bodies under one (node_id, seq), which is a permanent merkle fork."""
    real = _owner_key(hv)
    dev, dev_id = _device_key(hv)[:2], None
    dseed, dpub, dev_id = _device_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    admit = _gov(hv, {"action": "admit", "device_id": dev_id, "principal": "p"},
                 real[0], real[1], T_GENESIS, seq=2)
    held = _fact(hv, dev_id, T_GENESIS, "the device's own first entry", seq=1, dev=(dseed, dpub))
    _on_disk(hv, [genesis, admit, held])
    _pin(hv, genesis)

    squat = _fact(hv, dev_id, T_LATER, "squatted", seq=2)                  # unsigned, above the tip
    assert hv.append_foreign_entries([squat])[:2] == (0, 0)
    mine = _fact(hv, dev_id, T_LATER, "the device's own second entry", seq=2, dev=(dseed, dpub))
    assert hv.append_foreign_entries([mine])[:2] == (1, 0)                 # signed: lands
    contents = {e["payload"].get("content") for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}
    assert "squatted" not in contents and "the device's own second entry" in contents


def test_a_different_unsigned_body_at_a_held_sequence_is_refused(hv):
    """The other half of the fork: rewriting history at a seq this node already holds."""
    real = _owner_key(hv)
    dseed, dpub, dev_id = _device_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    admit = _gov(hv, {"action": "admit", "device_id": dev_id, "principal": "p"},
                 real[0], real[1], T_GENESIS, seq=2)
    held = _fact(hv, dev_id, T_GENESIS, "the real body", seq=1, dev=(dseed, dpub))
    _on_disk(hv, [genesis, admit, held])
    _pin(hv, genesis)

    rewrite = _fact(hv, dev_id, T_GENESIS, "a forged body", seq=1)         # unsigned, same seq
    assert hv.append_foreign_entries([rewrite])[:2] == (0, 0)             # refused, not counted a dup
    contents = {e["payload"].get("content") for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}
    assert "a forged body" not in contents and "the real body" in contents


def test_an_unsigned_entry_on_an_unheld_chain_is_refused_when_pushed(hv):
    """A push carries no advertised chunk hashes, so it can never take the first-pull door. This is
    why a single crafted high seq for a device this node has never seen does not land."""
    real = _owner_key(hv)
    _dev, dev_id = _owned(hv, real)
    stray = _fact(hv, "k1:00000000deadbeef", T_LATER, "from a chain we hold none of", seq=500)
    assert hv.append_foreign_entries([stray])[:2] == (0, 0)


# ── The first-pull door: how a FRESH node still gets the fleet's historical unsigned entries ─────

def test_a_verified_first_pull_brings_in_an_unheld_chain_then_the_tip_rule_applies(hv):
    """Grok's door. The batch must reproduce the peer's WHOLE advertised chain for that node, from
    seq 1, window hash for window hash — and once it is stored, the tip rule governs everything after."""
    real = _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    admit_legacy = _gov(hv, {"action": "admit", "device_id": "k1:00000000000000ff", "principal": "legacy"},
                        real[0], real[1], T_GENESIS, seq=2)
    _on_disk(hv, [genesis, admit_legacy])
    _pin(hv, genesis)

    legacy_id = "k1:00000000000000ff"                                      # a chain this node holds none of
    history = [_fact(hv, legacy_id, "2025-12-0%dT00:00:00Z" % (i + 1), f"historical {i}", seq=i + 1)
               for i in range(3)]                                          # unsigned, like the live 225
    advertised = hv.merkle.node_chunk_hashes(list(history))                # what the peer's /sync/hello says

    assert hv.append_foreign_entries(history, advertised_chunks=advertised)[:2] == (3, 0)
    contents = {e["payload"].get("content") for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}
    assert {"historical 0", "historical 1", "historical 2"} <= contents

    # The door closes behind it: the chain is now held, so the tip rule refuses the next unsigned seq
    # even if a peer advertises it.
    nxt = _fact(hv, legacy_id, T_LATER, "appended after the pull", seq=4)
    assert hv.append_foreign_entries([nxt], advertised_chunks=hv.merkle.node_chunk_hashes([nxt]))[:2] == (0, 0)


def test_the_door_refuses_a_batch_that_does_not_match_what_the_peer_advertised(hv):
    """The door's whole strength: one altered, missing or extra entry changes its window hash."""
    real = _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    admit_legacy = _gov(hv, {"action": "admit", "device_id": "k1:00000000000000fe", "principal": "legacy"},
                        real[0], real[1], T_GENESIS, seq=2)
    _on_disk(hv, [genesis, admit_legacy])
    _pin(hv, genesis)
    legacy_id = "k1:00000000000000fe"
    history = [_fact(hv, legacy_id, "2025-12-0%dT00:00:00Z" % (i + 1), f"honest {i}", seq=i + 1)
               for i in range(3)]
    advertised = hv.merkle.node_chunk_hashes([dict(e) for e in history])

    tampered = [dict(e) for e in history]
    tampered[1]["payload"] = {"content": "injected", "tags": [], "source": "manual"}
    assert hv.append_foreign_entries(tampered, advertised_chunks=advertised)[:2] == (0, 0)

    truncated = [dict(history[0])]                                          # missing seq 2 and 3
    assert hv.append_foreign_entries(truncated, advertised_chunks=advertised)[:2] == (0, 0)

    extra = [dict(e) for e in history] + [_fact(hv, legacy_id, T_LATER, "smuggled", seq=4)]
    assert hv.append_foreign_entries(extra, advertised_chunks=advertised)[:2] == (0, 0)
    assert not any(e["payload"].get("content") in {"injected", "smuggled"}
                   for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR))


def test_historical_unsigned_lines_on_disk_still_project(hv):
    """The 225 pre-genesis unsigned entries in the live journal keep projecting: write-side only."""
    real = _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    legacy = [_fact(hv, "legacyhost", "2025-12-01T00:00:00Z", f"old {i}", seq=i + 1) for i in range(3)]
    entries = _on_disk(hv, legacy + [genesis])
    _pin(hv, genesis)

    assert len(entries) == 4
    assert hv._governance_state(entries)["owner_id"] == real[2]


# ── The pin itself ─────────────────────────────────────────────────────────────────────────────

def test_auto_pin_takes_exactly_one_self_signed_owner_act(hv):
    real = _owner_key(hv)
    genesis = _owner_act(hv, real, T_GENESIS)
    entries = _on_disk(hv, [genesis])

    pin = hv._auto_pin_genesis(entries)
    assert pin is not None and pin["owner_id"] == real[2] and pin["hive_id"] == HIVE_ID
    assert pin["genesis_ref"] == "ownerdev:1"
    assert pin["genesis_hash"] == hv.compute_hash(genesis)
    assert hv._load_genesis_pin() == pin


def test_auto_pin_refuses_to_guess_between_two_owner_acts(hv):
    """Two acts means the operator must choose (re-join with the winner's fingerprint). There is no
    timestamp tie-break, because the timestamp is exactly what the attacker controls."""
    real, other = _owner_key(hv), _owner_key(hv)
    entries = _on_disk(hv, [_owner_act(hv, real, T_GENESIS),
                            _owner_act(hv, other, T_BACKDATED, nid="otherdev")])

    assert hv._auto_pin_genesis(entries) is None
    assert hv._load_genesis_pin() is None
    assert not hv.GENESIS_PIN_PATH.exists()


def test_the_pin_is_private_to_the_node(hv):
    real = _owner_key(hv)
    _pin(hv, _owner_act(hv, real, T_GENESIS))

    assert hv.GENESIS_PIN_PATH.stat().st_mode & 0o777 == 0o600
    assert hv.GENESIS_PIN_PATH.name in hv._VERIFY_EXCLUDE_NAMES     # never in the signed manifest
    assert not list(hv.JOURNAL_DIR.glob("*.jsonl"))                 # and never journaled
