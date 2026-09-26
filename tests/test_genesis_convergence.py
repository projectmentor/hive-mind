"""Two nodes, one pin each: what a rival `owner` declaration does to a FLEET (hive-mind-private #14).

The single-node rules live in `test_genesis_pin.py`. These tests answer the question that decides whether
the fix is safe to ship: does it keep the fleet convergent? The journal is a G-Set over an append-only
log, so the only way two honest nodes can disagree is if they PROJECT the same entries differently. The
pin is deliberately local and unsynced, so that is exactly the risk worth testing.

Entries move the way a sync round moves them: everything one node holds, offered to the other through
`append_foreign_entries`. Each node is a separate `hv` module instance with its own HIVE_HOME, so they
have genuinely independent pins, journals and stores.
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_genesis_pin import (HIVE_ID, T_BACKDATED, T_GENESIS, T_LATER,  # noqa: E402
                              _device_key, _fact, _gov, _owner_act, _owner_key)


def _node(tmp_path, monkeypatch, name):
    """An independent node: its own HIVE_HOME, journal, store and pin."""
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader(f"hvmod_conv_{name}", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    m.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return m


def _put(node, entries, day="2026-01-01"):
    """Seed a node's journal directly (what it already held before any of this)."""
    path = node.JOURNAL_DIR / f"{day}.jsonl"
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return node.merkle.read_all_entries(node.JOURNAL_DIR)


def _entries(node):
    return node.merkle.read_all_entries(node.JOURNAL_DIR)


def _offer(src, dst, **kw):
    """One sync round's worth: everything src holds, offered to dst."""
    return dst.append_foreign_entries(_entries(src), **kw)


def _root(node):
    es = _entries(node)
    return node.merkle.merkle_root(node.merkle.chunk_hashes(es))


def _owner_of(node):
    return node._governance_state(_entries(node))["owner_id"]


def _genesis(node):
    return node._governance_state(_entries(node))["genesis"]


def _pin_to(node, entry):
    node._write_genesis_pin(node._genesis_pin_for(entry))


# ── 1. The honest case has to keep working ───────────────────────────────────────────────────────

def test_two_honest_nodes_converge_on_one_owner_and_one_root(tmp_path, monkeypatch):
    a = _node(tmp_path, monkeypatch, "a")
    b = _node(tmp_path, monkeypatch, "b")
    owner = _owner_key(a)
    dseed, dpub, b_dev = _device_key(a)
    genesis = _owner_act(a, owner, T_GENESIS)
    admit = _gov(a, {"action": "admit", "device_id": b_dev, "principal": "p"}, owner[0], owner[1], T_GENESIS, seq=2)
    work = _fact(a, b_dev, T_LATER, "b did some work", seq=1, dev=(dseed, dpub))
    _put(a, [genesis, admit])
    _pin_to(a, genesis)
    _put(b, [work])

    _offer(a, b)                      # b pulls the hive: genesis + the admit
    b._auto_pin_genesis(_entries(b))  # and pins what it pulled (what `doctor --fix` / update does)
    _offer(b, a)                      # a pulls b's work

    assert _owner_of(a) == _owner_of(b) == owner[2]
    assert _root(a) == _root(b)
    assert _genesis(a)["pinned"] and _genesis(b)["pinned"]
    assert _genesis(a)["ref"] == _genesis(b)["ref"]


# ── 2. A racing genesis: no silent winner, in either timestamp order ─────────────────────────────

@pytest.mark.parametrize("rival_ts", [T_BACKDATED, T_LATER], ids=["rival-earlier", "rival-later"])
def test_two_pinned_nodes_each_refuse_the_others_genesis(tmp_path, monkeypatch, rival_ts):
    """Two hives that each ran `owner init` and then met. Neither may silently adopt the other, and the
    timestamp must not decide it — the timestamp is the attacker-controlled part."""
    a = _node(tmp_path, monkeypatch, "a")
    b = _node(tmp_path, monkeypatch, "b")
    a_owner, b_owner = _owner_key(a), _owner_key(b)
    a_gen = _owner_act(a, a_owner, T_GENESIS)
    b_gen = _owner_act(b, b_owner, rival_ts, nid="bdev")      # same hive_id: the guard cannot help
    _put(a, [a_gen]); _pin_to(a, a_gen)
    _put(b, [b_gen]); _pin_to(b, b_gen)

    assert _offer(b, a)[:2] == (0, 0)          # a refuses b's declaration at ingest
    assert _offer(a, b)[:2] == (0, 0)          # and b refuses a's
    assert _owner_of(a) == a_owner[2]
    assert _owner_of(b) == b_owner[2]
    # Neither node ever stores a second candidate, so neither is silently converted. The operator
    # resolves it by re-joining against the fingerprint they trust.
    assert _genesis(a)["candidates"] == _genesis(b)["candidates"] == 1


def test_an_unpinned_node_that_already_holds_both_fails_its_doctor_check(tmp_path, monkeypatch):
    """The upgrade case: a rival landed BEFORE the node pinned, so it is already on disk and the journal
    never removes it. The projection cannot silently prefer the attacker once pinned, and doctor must
    surface the pair rather than pick for the operator."""
    a = _node(tmp_path, monkeypatch, "a")
    real, attacker = _owner_key(a), _owner_key(a)
    genesis = _owner_act(a, real, T_GENESIS)
    rival = _owner_act(a, attacker, T_BACKDATED, nid="attackerdev")
    _put(a, [genesis, rival])

    g = _genesis(a)
    assert g["candidates"] == 2 and g["pinned"] is False
    assert _owner_of(a) == attacker[2]              # the legacy rule: the earlier timestamp wins
    assert a._auto_pin_genesis(_entries(a)) is None  # and auto-pin refuses to guess between them
    _pin_to(a, genesis)                             # the operator pins the one they trust
    assert _owner_of(a) == real[2]
    assert _genesis(a)["candidates"] == 2            # still two on disk — doctor keeps failing


# ── 3. Partition, then merge ──────────────────────────────────────────────────────────────────────

def test_a_rival_injected_during_a_partition_never_takes_the_owner(tmp_path, monkeypatch):
    a = _node(tmp_path, monkeypatch, "a")
    attacker_node = _node(tmp_path, monkeypatch, "x")
    owner, rogue = _owner_key(a), _owner_key(a)
    genesis = _owner_act(a, owner, T_GENESIS)
    _put(a, [genesis]); _pin_to(a, genesis)

    # While partitioned, the attacker builds a journal around a backdated rival reusing the hive_id.
    rival = _owner_act(attacker_node, rogue, T_BACKDATED, nid="attackerdev")
    stolen = _gov(attacker_node, {"action": "admit", "device_id": "k1:00000000000000ff", "principal": "x"},
                  rogue[0], rogue[1], T_LATER, seq=2, nid="attackerdev")
    _put(attacker_node, [rival, stolen])

    accepted, _dups = _offer(attacker_node, a)[:2]
    assert accepted == 0                            # rival refused, and its admit has no authority here
    assert _owner_of(a) == owner[2]
    assert "k1:00000000000000ff" not in a._governance_state(_entries(a))["admitted"]


# ── 4. A joiner that pinned a fingerprint cannot be captured by a squatter ────────────────────────

def test_a_fingerprint_pinned_joiner_refuses_a_squatter_then_accepts_the_real_hive(tmp_path, monkeypatch):
    real_node = _node(tmp_path, monkeypatch, "real")
    squatter = _node(tmp_path, monkeypatch, "squat")
    joiner = _node(tmp_path, monkeypatch, "join")
    owner, rogue = _owner_key(real_node), _owner_key(real_node)
    genesis = _owner_act(real_node, owner, T_GENESIS)
    _put(real_node, [genesis])
    fake = _owner_act(squatter, rogue, T_BACKDATED, nid="squatdev")      # advertises the same hive_id
    _put(squatter, [fake])

    # The joiner pins from the invite before it pulls anything.
    fp8 = joiner.compute_hash(genesis).split(":", 1)[-1][:8]
    joiner._write_genesis_pin({"hive_id": HIVE_ID, "owner_id": owner[2], "genesis_hash8": fp8})

    assert _offer(squatter, joiner)[:2] == (0, 0)        # the squatter is refused outright
    assert _owner_of(joiner) is None                     # and cannot make the joiner adopt it
    assert _genesis(joiner)["mismatch"] is True          # fails closed until the real one arrives

    # The real declaration is accepted — note it is UNSIGNED here on purpose: a hive whose genesis
    # predates device identity has an unsigned one, and the pin names that exact entry by hash, which
    # is stronger evidence than a device signature. Without the exemption a pinned joiner could never
    # accept the very declaration it pinned.
    assert _offer(real_node, joiner)[:2] == (1, 0)
    joiner._auto_pin_genesis(_entries(joiner))            # the pin is completed to the exact entry
    assert _owner_of(joiner) == owner[2]
    assert joiner._load_genesis_pin()["genesis_hash"] == joiner.compute_hash(genesis)


# ── 5. A mixed fleet: the consequence, stated as a test ──────────────────────────────────────────

def test_a_mixed_fleet_disagrees_visibly_rather_than_silently(tmp_path, monkeypatch):
    """One node pinned (1.27), one not (a pre-1.27 peer, or one that has not run the pin yet), both
    holding a rival that is already on disk. They project different owners. That is the documented
    upgrade consequence: the disagreement is VISIBLE — different owner, different projection, doctor
    failing on the unpinned node — not a silent takeover. It resolves when every node pins."""
    pinned = _node(tmp_path, monkeypatch, "new")
    legacy = _node(tmp_path, monkeypatch, "old")
    real, attacker = _owner_key(pinned), _owner_key(pinned)
    genesis = _owner_act(pinned, real, T_GENESIS)
    rival = _owner_act(pinned, attacker, T_BACKDATED, nid="attackerdev")
    for n in (pinned, legacy):
        _put(n, [genesis, rival])
    _pin_to(pinned, genesis)

    assert _owner_of(pinned) == real[2]
    assert _owner_of(legacy) == attacker[2]
    assert _genesis(legacy)["pinned"] is False and _genesis(legacy)["candidates"] == 2
    assert _root(pinned) == _root(legacy)              # same journal: the DIVERGENCE IS PROJECTION ONLY

    _pin_to(legacy, genesis)                            # the upgrade completes
    assert _owner_of(legacy) == real[2]
