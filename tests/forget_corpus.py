"""The forget corpus (release 3.0 plan 3.6): small journals that exercise `_content_evidence`'s owner-forget rule.

Each entry is built with the real signing code (the `test_links` builders), so owner signatures, device
signatures and positions are the ones the projection verifies. `CORPUS` maps a scenario name to a builder
`(hv) -> Scenario`; `journal(scenario, policy)` appends the `forget_writers` setting to it.

`Scenario.expect` is what the 2.4 rule says, written by hand from the rule and not read back from the oracle:
`{content: (forgotten while the key is absent or `legacy`, forgotten once the key is `owner`)}`. Only
contents listed there are asserted; every other fact in the journal must be unforgotten under both.
PR 2 onward reuses these journals for the migration, `--check` and the 3.0 differential.
"""

import base64
import os
import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_links import _owner_key, _device, _gov, _entry, _fact, T0  # noqa: E402
from test_unforget import _act, _hive, _transfer, PRE  # noqa: E402

# `owner`: the genesis owner (seed, pub, id); `devs`: admitted devices; `policy_ts`: where a `forget_writers`
# act goes (after genesis, before any later owner change); `expect`: see the module docstring; `absent_is`: what
# the journal itself already sets when the key is "absent" (a hive that closed at genesis says `owner`).
Scenario = namedtuple("Scenario", "entries owner devs policy_ts expect absent_is", defaults=("legacy",))

POLICIES = ("absent", "legacy", "owner")
POLICY_TS = "2026-01-01T00:10:00Z"
D1, D2, D3, D4, D5, D6 = (f"2026-01-0{d}T00:00:00Z" for d in (2, 3, 4, 5, 6, 7))
_B = base64.b64encode


def _scn(entries, owner, devs, expect):
    return Scenario(entries, owner, devs, POLICY_TS, expect)


def _forget_at(hv, dev, fact, ts, owner=None, source="owner:owner/owner"):
    """An owner-source forget of `fact`; unsigned unless `owner=(seed, pub)`."""
    p = {"retracts_ref": [fact["node_id"], fact["seq"]], "reason": "r", "source": source}
    return _entry(hv, dev, "retract", p, ts, owner=owner)


def pre_genesis_unsigned_latest(hv):
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "pg: the unsigned forget is the latest act", D1)
    return _scn(base + [f, _act(hv, d1, f, PRE)], a, [d0, d1],
                {"pg: the unsigned forget is the latest act": (True, False)})


def pre_genesis_forget_then_signed_unforget(hv):
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "pg: forget, then an owner unforget", D1)
    back = _act(hv, d1, f, D3, owner=a[:2], unforget=True)
    return _scn(base + [f, _act(hv, d1, f, PRE), back], a, [d0, d1],
                {"pg: forget, then an owner unforget": (False, False)})


def pre_genesis_forget_reforgotten_post_genesis(hv):
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "pg: re-forgotten by the owner", D1)
    return _scn(base + [f, _act(hv, d1, f, PRE), _act(hv, d1, f, D3, owner=a[:2])], a, [d0, d1],
                {"pg: re-forgotten by the owner": (True, True)})


def member_device_backdated(hv):
    """The #122 shape: an admitted device writes `source=owner:owner/owner`, dated before genesis, unsigned."""
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "member: backdated before genesis", D1)
    g = _fact(hv, d0, "member: unsigned, dated after genesis", D1)
    h = _fact(hv, d0, "member: signed by a stranger, backdated", D1)
    stranger = _owner_key(hv)
    return _scn(base + [f, g, h, _act(hv, d1, f, PRE), _act(hv, d1, g, D3),
                        _forget_at(hv, d1, h, PRE, owner=stranger[:2])], a, [d0, d1],
                {"member: backdated before genesis": (True, False),
                 "member: unsigned, dated after genesis": (False, False),
                 "member: signed by a stranger, backdated": (True, False)})


def owner_transfer_between(hv):
    """Unsigned pre-genesis forget; owner A's forget before and after handing the hive to B; B's forget."""
    a, (d0, d1, _), base = _hive(hv)
    b = _owner_key(hv)
    fs = [_fact(hv, d0, f"transfer: fact {i}", D1) for i in range(4)]
    es = [fs[0], fs[1], fs[2], fs[3],
          _act(hv, d1, fs[0], PRE),
          _act(hv, d1, fs[1], D2, owner=a[:2]),              # A, while A is owner: honoured
          _transfer(hv, a, b[1], D3, 21),
          _act(hv, d1, fs[2], D4, owner=a[:2]),              # A after the transfer: ignored
          _act(hv, d1, fs[3], D5, owner=b[:2])]              # B, the current owner: honoured
    return _scn(base + es, a, [d0, d1],
                {"transfer: fact 0": (True, False), "transfer: fact 1": (True, True),
                 "transfer: fact 2": (False, False), "transfer: fact 3": (True, True)})


def owner_succession_between(hv):
    """Same, through nominate-successor and claim-succession (carried by an admitted device)."""
    a, (d0, d1, _), base = _hive(hv)
    o2 = _owner_key(hv)
    fs = [_fact(hv, d0, f"succession: fact {i}", D1) for i in range(4)]
    nom = _entry(hv, d0, "governance", {"action": "nominate-successor", "successor_owner_pub": _B(o2[1]).decode()},
                 D3, owner=a[:2])
    claim = hv._sign_governance_payload({"action": "claim-succession", "owner_id": o2[2],
                                         "owner_pub": _B(o2[1]).decode()}, o2[0], o2[1])
    d0["seq"] += 1
    carried = hv._sign_entry({"node_id": d0["id"], "seq": d0["seq"], "type": "governance", "timestamp": D4,
                              "payload": claim}, d0["seed"], d0["pub"])
    es = [*fs, _act(hv, d1, fs[0], PRE), _act(hv, d1, fs[1], D2, owner=a[:2]), nom, carried,
          _act(hv, d1, fs[2], D5, owner=a[:2]),              # A after the claim: ignored
          _act(hv, d1, fs[3], D6, owner=o2[:2])]             # the successor: honoured
    return _scn(base + es, a, [d0, d1],
                {"succession: fact 0": (True, False), "succession: fact 1": (True, True),
                 "succession: fact 2": (False, False), "succession: fact 3": (True, True)})


def owner_election_between(hv):
    """A quorum election (quorum_m 1, owner dark for over `dead_man_days`) installs C; A's later forget is ignored."""
    a, (d0, d1, _), base = _hive(hv)
    c = _owner_key(hv)
    basis = "2026-06-01T00:00:00Z"
    cpub = _B(c[1]).decode()
    f_before, f_after_a, f_after_c = (_fact(hv, d0, f"election: fact {i}", D1) for i in range(3))
    quorum = _gov(hv, {"action": "set-config", "key": "quorum_m", "value": 1}, a[0], a[1], POLICY_TS, 40)
    d1["seq"] += 1
    prop = hv._sign_entry({"node_id": d1["id"], "seq": d1["seq"], "type": "governance", "timestamp": basis,
                           "payload": {"action": "propose-election", "proposal_id": hv._election_id(cpub, basis),
                                       "new_owner_pub": cpub, "basis_ts": basis}}, d1["seed"], d1["pub"])
    es = [f_before, f_after_a, f_after_c, quorum, _act(hv, d1, f_before, PRE),
          _act(hv, d1, f_before, D2, owner=a[:2]),
          prop,
          _act(hv, d1, f_after_a, "2026-06-02T00:00:00Z", owner=a[:2]),     # A after the election: ignored
          _act(hv, d1, f_after_c, "2026-06-03T00:00:00Z", owner=c[:2])]     # C, the elected owner: honoured
    return _scn(base + es, a, [d0, d1],
                {"election: fact 0": (True, True), "election: fact 1": (False, False),
                 "election: fact 2": (True, True)})


def dangling_pre_genesis_forget(hv):
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "dangling: a neighbour that stays", D1)
    ghost = {"node_id": d0["id"], "seq": 9999}                  # no such entry in the journal
    return _scn(base + [f, _forget_at(hv, d1, ghost, PRE)], a, [d0, d1], {})


def shared_content_one_forgotten(hv):
    a, (d0, d1, d2), base = _hive(hv)
    f1 = _fact(hv, d0, "shared: the same words", D1)
    f2 = _fact(hv, d1, "shared: the same words", D1)
    other = _fact(hv, d2, "shared: other words", D1)
    return _scn(base + [f1, f2, other, _act(hv, d2, f1, PRE)], a, [d0, d1, d2],
                {"shared: the same words": (True, False)})


def pre_owner_hive(hv):
    """No `owner` act at all: the source-tag forget is honoured, an unforget is not, a peer retract is evidence."""
    d0, d1 = _device(hv), _device(hv)
    f, g, h = (_fact(hv, d0, f"pre-owner: fact {i}", D1) for i in range(3))
    peer = _entry(hv, d1, "retract", {"retracts_ref": [h["node_id"], h["seq"]], "reason": "r", "source": "claude-code"}, D2)
    es = [f, g, h, _act(hv, d1, f, D2), _act(hv, d1, g, D2), _act(hv, d1, g, D3, unforget=True), peer]
    return Scenario(es, None, [d0, d1], None,
                    {"pre-owner: fact 0": (True, True), "pre-owner: fact 1": (True, True)})


def closed_at_genesis(hv):
    """What `owner init` leaves: the grandfathered forget re-issued owner-signed, then `forget_writers=owner`."""
    a, (d0, d1, _), base = _hive(hv)
    kept, dropped = _fact(hv, d0, "closed: re-issued signed", D1), _fact(hv, d0, "closed: never re-issued", D1)
    reissue = _act(hv, d1, kept, "2026-01-01T00:00:30Z", owner=a[:2])
    close = _gov(hv, {"action": "set-config", "key": "forget_writers", "value": "owner"}, a[0], a[1],
                 "2026-01-01T00:01:00Z", 41)
    return Scenario(base + [kept, dropped, _act(hv, d1, kept, PRE), _act(hv, d1, dropped, PRE), reissue, close],
                    a, [d0, d1], "2026-01-01T00:20:00Z",       # a `legacy` act here reopens what genesis closed
                    {"closed: re-issued signed": (True, True), "closed: never re-issued": (True, False)}, "owner")


CORPUS = {fn.__name__: fn for fn in (
    pre_genesis_unsigned_latest, pre_genesis_forget_then_signed_unforget,
    pre_genesis_forget_reforgotten_post_genesis, member_device_backdated, owner_transfer_between,
    owner_succession_between, owner_election_between, dangling_pre_genesis_forget,
    shared_content_one_forgotten, pre_owner_hive, closed_at_genesis)}


def journal(hv, scn, policy):
    """`scn.entries` with `forget_writers` set to `policy` (`absent` appends nothing)."""
    assert policy in POLICIES
    if policy == "absent" or scn.owner is None:
        return list(scn.entries)
    a = scn.owner
    return [*scn.entries, _gov(hv, {"action": "set-config", "key": "forget_writers", "value": policy},
                               a[0], a[1], scn.policy_ts, 50)]


def effective(scn, policy):
    """The `forget_writers` value the journal ends up with: `legacy` or `owner`."""
    return scn.absent_is if policy == "absent" else policy
