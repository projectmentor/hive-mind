"""2.1 plan PR 5b (#208): who may write an entity, and how a module's entity meets a shared one.

Option 3 (h:5c80e1429b): a module creates entities and updates its own, never another writer's. The addendum:
a module's entity is named `x-<module>:<name>`; a module links only its own entities; the core joins an
`x-<module>:` entity to a shared one with a `same-as` link, which shows by default and which its writer or the
owner withdraws. Each rule holds at two layers, and each test names the layer it fails without: the gate
(`POST /v1/entries`, 403 and nothing written) and the projection (`rebuild_db`, so every node agrees).

Same temp hive as `test_module_api.py`: an owner, a plain device, and two modules' devices (`hwatch`, `other`).
A journal built by hand carries the entries a gate would have refused, as a peer could have synced them.
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import hive_module_api as api  # noqa: E402,F401
import test_links as TL  # noqa: E402
from test_module_api import Hive, hive  # noqa: E402,F401
from test_module_write import SRC, fact, journal, post  # noqa: E402

T = "2026-01-06T00:00:%02dZ"
GOV_SEQ = iter(range(40, 400))


def ent(hv, dev, name, ts, type_="person", attrs=None):
    return TL._entry(hv, dev, "entity", {"name": name, "type": type_, "attributes": attrs or {}, "source": "manual"}, ts)


def mod_ent(hv, dev, name, ts, type_="person", attrs=None):
    return TL._entry(hv, dev, "entity", {"name": name, "type": type_, "attributes": attrs or {}, "source": SRC}, ts)


def ref(e):
    return [e["node_id"], e["seq"]]


def link(hv, dev, kind, frm, to, ts, source="manual", owner=None, **data):
    return TL._link(hv, dev, kind, frm, to, ts, data=data, source=source, owner=owner)


def device_sorted(h, before=None, after=None):
    """A new device, admitted by the owner, whose node_id sorts before (or after) `before` (`after`): replay order
    is (node_id, seq), so a test that needs one writer ahead of another must choose their ids."""
    oseed, opub, _ = h.owner
    while True:
        dev = TL._device(h.hv)
        if (before is None or dev["id"] < before["id"]) and (after is None or dev["id"] > after["id"]):
            break
    h.entries.append(TL._gov(h.hv, {"action": "admit", "device_id": dev["id"], "principal": f"x{next(GOV_SEQ)}"}, oseed, opub,
                             "2026-01-02T00:00:00Z", next(GOV_SEQ)))
    return dev


def project(h, entries):
    return TL._project(h.hv, h.home, entries)


def rows(conn):
    return [tuple(r) for r in conn.execute("SELECT name, type, attributes FROM entities ORDER BY name")]


def edges(conn, kind):
    return conn.execute("SELECT count(*) FROM links WHERE kind = ?", (kind,)).fetchone()[0]


def refused(code_body, error):
    code, body = code_body
    assert code == 403 and body["error"] == error, (code, body)


# ── the gate: the prefix (requirement 1) ───────────────────────────────────────────────────────────────

def test_a_module_may_write_only_its_own_prefixed_entity_name_at_the_gate(hive):
    before = journal(hive)
    for name in ("david", "x-other:david", "x-hwatch", "x-hwatch:", "hwatch:david"):
        refused(post(hive, "mod", "entity", {"name": name, "source": SRC})[0], "entity-prefix-required")
    assert journal(hive) == before                                     # nothing written
    assert post(hive, "mod", "entity", {"name": "x-hwatch:david", "type": "person", "source": SRC})[0][0] == 200
    refused(post(hive, "other", "entity", {"name": "x-hwatch:david", "source": "x-other"})[0], "entity-prefix-required")
    assert post(hive, "other", "entity", {"name": "x-other:david", "source": "x-other"})[0][0] == 200


def test_a_module_creates_and_then_updates_its_own_entity(hive):
    assert post(hive, "mod", "entity", {"name": "x-hwatch:host", "type": "host", "attributes": {"os": "linux"}, "source": SRC})[0][0] == 200
    assert post(hive, "mod", "entity", {"name": "x-hwatch:host", "type": "server", "attributes": {"os": "bsd"}, "source": SRC})[0][0] == 200
    got = hive.hv.get_conn().execute("SELECT type, attributes FROM entities WHERE name = 'x-hwatch:host'").fetchone()
    assert (got["type"], json.loads(got["attributes"])) == ("server", {"os": "bsd"})


def test_a_second_module_cannot_rewrite_the_first_modules_entity(hive):
    assert post(hive, "mod", "entity", {"name": "x-hwatch:host", "type": "host", "source": SRC})[0][0] == 200
    before = journal(hive)
    refused(post(hive, "other", "entity", {"name": "x-hwatch:host", "type": "stolen", "source": "x-other"})[0], "entity-prefix-required")
    assert journal(hive) == before
    assert hive.hv.get_conn().execute("SELECT type FROM entities WHERE name = 'x-hwatch:host'").fetchone()["type"] == "host"


def test_a_name_a_device_created_first_stays_with_the_device_even_when_it_is_the_modules_prefix(hive):
    seed = ent(hive.hv, hive.plain, "x-hwatch:seeded", T % 1, "pre-seeded")          # the owner pre-seeds a module's entity
    project(hive, hive.entries + [seed]).close()
    before = journal(hive)
    refused(post(hive, "mod", "entity", {"name": "x-hwatch:seeded", "type": "mine", "source": SRC})[0], "not-entity-owner")
    assert journal(hive) == before
    assert hive.hv.get_conn().execute("SELECT type FROM entities WHERE name = 'x-hwatch:seeded'").fetchone()["type"] == "pre-seeded"


def test_an_owner_or_plain_device_still_updates_by_name_as_before(hive):
    hv = hive.hv
    early = device_sorted(hive, before=hive.plain)
    a = ent(hv, early, "david", T % 1, "person", {"v": 1})
    b = ent(hv, hive.plain, "david", T % 2, "human", {"v": 2})       # a later writer in replay order wins, as ever
    c = ent(hv, hive.plain, "x-hwatch:note", T % 3, "n1")           # a plain device may use a module's prefix too
    conn = project(hive, hive.entries + [a, b, c])
    assert rows(conn) == [("david", "human", '{"v": 2}'), ("x-hwatch:note", "n1", "{}")]
    assert hv._entity_declined(journal(hive), hv._governance_state(journal(hive))) == set()


# ── the projection (requirement 1 and 4) ───────────────────────────────────────────────────────────────

def test_a_module_cannot_rewrite_the_owners_entity_in_a_journal_built_by_hand(hive):
    hv = hive.hv
    owner_side = ent(hv, hive.plain, "david", T % 1, "person", {"role": "owner"})
    rewrite = mod_ent(hv, hive.mod, "david", T % 2, "hacked", {"role": "none"})     # signed, valid, and not gated here
    conn = project(hive, hive.entries + [owner_side, rewrite])
    assert rows(conn) == [("david", "person", '{"role": "owner"}')]
    assert hv._entity_declined(journal(hive), hv._governance_state(journal(hive))) == {(rewrite["node_id"], rewrite["seq"])}
    assert conn.execute("SELECT count(*) FROM journal_index WHERE node_id = ? AND seq = ?", (rewrite["node_id"], rewrite["seq"])).fetchone()[0] == 0


def test_a_backdated_module_entity_for_an_unprefixed_name_does_not_take_the_name(hive):
    hv = hive.hv
    backdated = mod_ent(hv, hive.mod, "david", "2019-01-01T00:00:00Z", "front-run", {"who": "module"})
    later = ent(hv, hive.plain, "david", T % 1, "person", {"who": "device"})
    conn = project(hive, hive.entries + [backdated, later])
    assert rows(conn) == [("david", "person", '{"who": "device"}')]


def test_a_module_cannot_update_a_prefixed_entity_a_device_created_first_in_replay_order(hive):
    hv = hive.hv
    device = device_sorted(hive, before=hive.mod)
    seeded = ent(hv, device, "x-hwatch:seeded", T % 1, "seed", {"by": "owner"})
    rewrite = mod_ent(hv, hive.mod, "x-hwatch:seeded", T % 2, "mine", {"by": "module"})
    conn = project(hive, hive.entries + [seeded, rewrite])
    assert rows(conn) == [("x-hwatch:seeded", "seed", '{"by": "owner"}')]


def test_a_module_updates_its_own_entity_in_a_journal(hive):
    hv = hive.hv
    one = mod_ent(hv, hive.mod, "x-hwatch:host", T % 1, "host", {"n": 1})
    two = mod_ent(hv, hive.mod, "x-hwatch:host", T % 2, "server", {"n": 2})
    assert rows(project(hive, hive.entries + [one, two])) == [("x-hwatch:host", "server", '{"n": 2}')]


def test_a_second_module_cannot_rewrite_the_first_modules_entity_in_a_journal(hive):
    hv = hive.hv
    first = mod_ent(hv, hive.mod, "x-hwatch:host", T % 1, "host")
    steal = TL._entry(hv, hive.other, "entity", {"name": "x-hwatch:host", "type": "stolen", "attributes": {}, "source": "x-other"}, T % 2)
    assert rows(project(hive, hive.entries + [first, steal])) == [("x-hwatch:host", "host", "{}")]


def test_two_nodes_given_one_journal_in_different_arrival_orders_project_the_same_entity_rows(hive):
    hv = hive.hv
    early = device_sorted(hive, before=hive.mod)
    all_ = [ent(hv, hive.plain, "david", T % 1, "person", {"a": 1}),
            mod_ent(hv, hive.mod, "david", T % 2, "hacked"),
            mod_ent(hv, hive.mod, "x-hwatch:host", T % 3, "host", {"n": 1}),
            ent(hv, early, "x-hwatch:seeded", T % 4, "seed"),
            mod_ent(hv, hive.mod, "x-hwatch:seeded", T % 5, "mine"),
            ent(hv, hive.plain, "david", T % 6, "human", {"a": 2})]
    base = list(hive.entries)
    forward = rows(project(hive, base + all_))
    backward = rows(project(hive, list(reversed(base + all_))))
    assert forward == backward
    assert dict((n, t) for n, t, _ in forward) == {"david": "human", "x-hwatch:host": "host", "x-hwatch:seeded": "seed"}


# ── mutants: each layer, removed, lets the rewrite through (these are what make the tests above bite) ────────────

def test_mutant_without_the_gate_check_a_module_entity_write_lands(hive, monkeypatch):
    monkeypatch.setattr(api, "_check_entity", lambda ctx, payload: None)
    before = len(journal(hive))
    assert post(hive, "mod", "entity", {"name": "david", "source": SRC})[0][0] == 200      # the refusal above is the gate's alone
    assert len(journal(hive)) == before + 1


def test_mutant_without_the_projection_check_the_modules_rewrite_takes_the_row(hive, monkeypatch):
    hv = hive.hv
    monkeypatch.setattr(hv, "_entity_declined", lambda entries, gov: set())
    owner_side = ent(hv, device_sorted(hive, before=hive.mod), "david", T % 1, "person")      # replayed first, so the module's lands over it
    rewrite = mod_ent(hv, hive.mod, "david", T % 2, "hacked")
    assert rows(project(hive, hive.entries + [owner_side, rewrite])) == [("david", "hacked", "{}")]


# ── links: a module links only its own entities (requirement 2) ───────────────────────────────────────────

def _world(hive):
    """A shared entity `david` and a plain fact, a module's entity `x-hwatch:david` and another module's."""
    hv = hive.hv
    david = ent(hv, hive.plain, "david", T % 1)
    fact_ = TL._fact(hv, hive.plain, "david prefers tea", T % 2)
    mine = mod_ent(hv, hive.mod, "x-hwatch:david", T % 3)
    theirs = TL._entry(hv, hive.other, "entity", {"name": "x-other:david", "type": "person", "attributes": {}, "source": "x-other"}, T % 4)
    hive.entries += [david, fact_, mine, theirs]
    project(hive, hive.entries).close()
    return david, fact_, mine, theirs


def test_a_module_link_to_a_shared_entity_is_403_at_the_gate_and_writes_nothing(hive):
    david, fact_, mine, theirs = _world(hive)
    before = journal(hive)
    for end in (david, theirs):
        refused(post(hive, "mod", "link", {"kind": "entity", "from_ref": ref(end), "to_ref": ref(fact_), "data": {}, "source": SRC})[0], "shared-entity-link")
        refused(post(hive, "mod", "link", {"kind": "supports", "from_ref": ref(fact_), "to_ref": ref(end), "data": {}, "source": SRC})[0], "shared-entity-link")
    own_kind = {"kind": "x-hwatch:observes", "from": hive.hv._short_id(*ref(fact_)), "to": ref(david), "data": {}, "source": SRC}
    refused(post(hive, "mod", "link", own_kind)[0], "shared-entity-link")           # any kind, either field
    assert journal(hive) == before
    assert hive.hv.get_conn().execute("SELECT count(*) FROM entity_facts").fetchone()[0] == 0


def test_a_module_may_link_its_own_entities_and_a_human_fact_as_evidence(hive):
    david, fact_, mine, theirs = _world(hive)
    assert post(hive, "mod", "link", {"kind": "entity", "from_ref": ref(mine), "to_ref": ref(fact_), "data": {}, "source": SRC})[0][0] == 200
    own = post(hive, "mod", "entity", {"name": "x-hwatch:host", "source": SRC})[0]
    assert own[0] == 200
    conn = hive.hv.get_conn()
    host = ["%s" % hive.mod["id"], int(own[1]["ref"].rsplit(":", 1)[1])]
    assert post(hive, "mod", "link", {"kind": "extends", "from_ref": host, "to_ref": ref(mine), "data": {}, "source": SRC})[0][0] == 200
    assert conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0] == 1
    # `supports` on a fact another writer wrote stays evidence, never command
    assert post(hive, "mod", "link", {"kind": "supports", "from_ref": ref(mine), "to_ref": ref(fact_), "data": {}, "source": SRC})[0][0] == 200
    row = conn.execute("SELECT authority FROM links WHERE kind = 'supports'").fetchone()
    assert row["authority"] == "evidence"


def test_a_module_link_to_a_shared_entity_is_not_projected_when_it_is_already_in_a_journal(hive):
    hv = hive.hv
    david, fact_, mine, theirs = _world(hive)
    bad = [link(hv, hive.mod, "entity", david, fact_, T % 10, source=SRC),
           link(hv, hive.mod, "entity", theirs, fact_, T % 11, source=SRC),
           link(hv, hive.mod, "supports", fact_, david, T % 12, source=SRC)]
    good = [link(hv, hive.mod, "entity", mine, fact_, T % 13, source=SRC)]
    conn = project(hive, hive.entries + bad + good)
    assert edges(conn, "entity") == 1
    assert conn.execute("SELECT count(*) FROM links WHERE kind = 'supports'").fetchone()[0] == 0
    named = {r["name"] for r in conn.execute("SELECT e.name FROM entity_facts ef JOIN entities e ON e.id = ef.entity_id")}
    assert named == {"x-hwatch:david"}                                  # no entity_facts row for `david` or `x-other:david`


def test_a_plain_device_may_still_link_a_shared_entity_to_a_fact(hive):
    hv = hive.hv
    david, fact_, mine, theirs = _world(hive)
    conn = project(hive, hive.entries + [link(hv, hive.plain, "entity", david, fact_, T % 10)])
    assert conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0] == 1


def test_mutant_without_the_gate_link_check_a_module_link_to_a_shared_entity_lands(hive, monkeypatch):
    david, fact_, mine, theirs = _world(hive)
    monkeypatch.setattr(api, "_check_link_ends", lambda module, payload: None)
    assert post(hive, "mod", "link", {"kind": "entity", "from_ref": ref(david), "to_ref": ref(fact_), "data": {}, "source": SRC})[0][0] == 200


def test_mutant_without_the_projection_link_check_the_shared_entity_gets_the_modules_fact(hive, monkeypatch):
    hv = hive.hv
    david, fact_, mine, theirs = _world(hive)
    monkeypatch.setattr(hv, "_shared_entity_end", lambda conn, module, kind, local_id: False)
    conn = project(hive, hive.entries + [link(hv, hive.mod, "entity", david, fact_, T % 10, source=SRC)])
    assert conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0] == 1


# ── same-as: the core's join (requirement 2, revised) ─────────────────────────────────────────────────────

def _join_world(hive):
    """`david` (a device's) joined to `x-hwatch:david` (the module's), which holds one module fact."""
    hv = hive.hv
    david = ent(hv, hive.plain, "david", T % 1)
    mine = mod_ent(hv, hive.mod, "x-hwatch:david", T % 2)
    hr_fact = TL._entry(hv, hive.mod, "fact", fact("david joined in 2019"), T % 3)
    hive.entries += [david, mine, hr_fact, link(hv, hive.mod, "entity", mine, hr_fact, T % 4, source=SRC)]
    return david, mine, hr_fact


def view(hive, name, **kw):
    conn = hive.hv.get_conn()
    try:
        return hive.hv.entity_view(conn, name, **kw)
    finally:
        conn.close()


def test_a_module_same_as_is_403_always_and_not_applied_in_a_journal(hive):
    hv = hive.hv
    david, mine, _ = _join_world(hive)
    project(hive, hive.entries).close()
    refused(post(hive, "mod", "link", {"kind": "same-as", "from_ref": ref(mine), "to_ref": ref(david), "data": {}, "source": SRC})[0], "same-as-core-only")
    # unresolved ends and its own entities: still the core's alone
    refused(post(hive, "mod", "link", {"kind": "same-as", "from_ref": ["k1:nobody", 9], "to_ref": ["k1:nobody", 8], "data": {}, "source": SRC})[0], "same-as-core-only")
    own = mod_ent(hv, hive.mod, "x-hwatch:other", T % 20)
    conn = project(hive, hive.entries + [own, link(hv, hive.mod, "same-as", mine, own, T % 21, source=SRC),
                                         link(hv, hive.mod, "same-as", mine, david, T % 22, source=SRC)])
    assert edges(conn, "same-as") == 0


def test_only_a_same_as_from_x_module_entity_to_an_unprefixed_one_is_a_join(hive):
    hv = hive.hv
    david, mine, _ = _join_world(hive)
    other_plain = ent(hv, hive.plain, "ada", T % 5)
    wrong = [link(hv, hive.plain, "same-as", david, mine, T % 10),               # the wrong way round
             link(hv, hive.plain, "same-as", david, other_plain, T % 11),        # neither is prefixed
             link(hv, hive.plain, "same-as", mine, mine, T % 12)]               # both prefixed
    conn = project(hive, hive.entries + [other_plain] + wrong)
    assert edges(conn, "same-as") == 0 and view(hive, "david")["joined"] == []


def test_a_device_same_as_shows_by_default_and_plain_show_without_it_is_the_core_view(hive):
    hv = hive.hv
    david, mine, hr_fact = _join_world(hive)
    join = link(hv, hive.plain, "same-as", mine, david, T % 10)
    conn = project(hive, hive.entries)
    assert view(hive, "david")["joined"] == []                                 # before the join: the core view
    conn = project(hive, hive.entries + [join])
    assert edges(conn, "same-as") == 1
    assert conn.execute("SELECT count(*) FROM entity_facts WHERE entity_id = (SELECT id FROM entities WHERE name = 'david')").fetchone()[0] == 0
    v = view(hive, "david")
    assert [(j["name"], j["ref"], j["signer"]) for j in v["joined"]] == [("x-hwatch:david", f"{join['node_id']}:{join['seq']}", join["node_id"])]
    assert [f["content"] for f in v["joined"][0]["facts"]] == ["david joined in 2019"]
    assert v["facts"] == []                                                    # the shared entity's own facts are untouched
    assert view(hive, "david", joins=False)["joined"] == []
    assert view(hive, "x-hwatch:david")["joined"] == []                        # the join is listed on the shared side only


def test_the_entity_route_returns_the_same_view_and_hides_forgotten_facts(hive):
    hv = hive.hv
    david, mine, hr_fact = _join_world(hive)
    gone = TL._entry(hv, hive.mod, "fact", fact("a forgotten one"), T % 6)
    from test_unforget import _act
    forget = _act(hv, hive.plain, gone, T % 7, owner=(hive.owner[0], hive.owner[1]))
    hive.entries += [gone, link(hv, hive.mod, "entity", mine, gone, T % 8, source=SRC), forget,
                     link(hv, hive.plain, "same-as", mine, david, T % 10)]
    project(hive, hive.entries).close()
    code, body = hive.get("/v1/entity?name=david")
    assert code == 200 and body["name"] == "david" and "id" not in body
    assert [j["name"] for j in body["joined"]] == ["x-hwatch:david"]
    assert [f["content"] for f in body["joined"][0]["facts"]] == ["david joined in 2019"]
    assert all("id" not in f for f in body["joined"][0]["facts"])
    assert hive.get("/v1/entity?name=nobody")[0] == 404 and hive.get("/v1/entity")[0] == 400


# ── withdrawing a join ──────────────────────────────────────────────────────────────────────────────────────

def retract(hv, dev, target, ts, owner=None):
    return TL._entry(hv, dev, "retract", {"retracts_ref": ref(target), "reason": "no longer the same", "source": "manual"}, ts, owner=owner)


def joined(hive, entries):
    project(hive, entries).close()
    return [j["name"] for j in view(hive, "david")["joined"]]


def test_the_writers_withdrawal_drops_the_join_on_a_rebuild_and_a_later_same_as_rejoins(hive):
    hv = hive.hv
    david, mine, _ = _join_world(hive)
    join = link(hv, hive.plain, "same-as", mine, david, T % 10)
    assert joined(hive, hive.entries + [join]) == ["x-hwatch:david"]
    out = retract(hv, hive.plain, join, T % 11)
    assert joined(hive, hive.entries + [join, out]) == []
    conn = project(hive, hive.entries + [join, out])
    assert edges(conn, "same-as") == 0
    again = link(hv, hive.plain, "same-as", mine, david, T % 12)
    assert joined(hive, hive.entries + [join, out, again]) == ["x-hwatch:david"]
    # a withdrawal that is EARLIER than the join does not undo it
    assert joined(hive, hive.entries + [retract(hv, hive.plain, join, T % 9)] + [join]) == ["x-hwatch:david"]


def test_a_withdrawal_by_anyone_else_is_ignored_and_is_not_fact_evidence(hive):
    hv = hive.hv
    david, mine, hr_fact = _join_world(hive)
    join = link(hv, hive.plain, "same-as", mine, david, T % 10)
    stranger = device_sorted(hive)
    before = joined(hive, hive.entries + [join])
    entries = hive.entries + [join]
    gov = hv._governance_state(entries)
    conf_before = {k: hv._content_confidence(v, gov) for k, v in hv._content_evidence(entries, gov).items()}
    by_device = retract(hv, stranger, join, T % 11)
    by_module = retract(hv, hive.mod, join, T % 12)                        # a module cannot send a retract; hand-built here
    by_fake_owner = TL._entry(hv, stranger, "retract", {"retracts_ref": ref(join), "reason": "x", "source": "owner:owner/owner"}, T % 13)
    after = joined(hive, hive.entries + [join, by_device, by_module, by_fake_owner])
    assert before == after == ["x-hwatch:david"]
    entries = hive.entries + [join, by_device, by_module, by_fake_owner]
    gov = hv._governance_state(entries)
    assert {k: hv._content_confidence(v, gov) for k, v in hv._content_evidence(entries, gov).items()} == conf_before


def test_an_owner_withdrawal_judged_in_governance_order_not_replay_order(hive):
    hv = hive.hv
    david, mine, _ = _join_world(hive)
    oseed, opub, _ = hive.owner
    first = device_sorted(hive, before=hive.plain)                         # the owner's retract sorts BEFORE the join in replay
    join = link(hv, hive.plain, "same-as", mine, david, T % 10)
    owner_out = retract(hv, first, join, T % 11, owner=(oseed, opub))      # ...and is LATER in time
    entries = hive.entries + [join, owner_out]
    assert sorted((e["node_id"], e["seq"]) for e in (owner_out, join))[0] == (owner_out["node_id"], owner_out["seq"])
    assert joined(hive, entries) == []
    # not a ban: a later same-as re-joins
    assert joined(hive, entries + [link(hv, hive.plain, "same-as", mine, david, T % 12)]) == ["x-hwatch:david"]
    # an owner signature that was not the owner as of its position (before genesis) counts for nothing
    early = retract(hv, first, join, "2019-01-01T00:00:00Z", owner=(oseed, opub))
    assert joined(hive, hive.entries + [join, early]) == ["x-hwatch:david"]
    # ...even when the join is earlier still: the owner key signs after genesis only if it was the owner then
    old_join = link(hv, hive.plain, "same-as", mine, david, "2018-01-01T00:00:00Z")
    old_out = retract(hv, first, old_join, "2019-01-01T00:00:00Z", owner=(oseed, opub))
    assert joined(hive, hive.entries + [old_join, old_out]) == ["x-hwatch:david"]


def test_a_withdrawal_of_a_same_as_that_was_never_a_honoured_join_does_not_drop_anothers_join(hive):
    hv = hive.hv
    david, mine, _ = _join_world(hive)
    # (a) a module's own `same-as` (never honoured), then the module's withdrawal of it, over a device's join
    device_join = link(hv, hive.plain, "same-as", mine, david, T % 10)
    mod_join = link(hv, hive.mod, "same-as", mine, david, T % 9)
    mod_out = retract(hv, hive.mod, mod_join, T % 11)
    assert joined(hive, hive.entries + [device_join, mod_join, mod_out]) == ["x-hwatch:david"]
    # (b) a stranger device's own join and its withdrawal of that join leave the plain device's join standing
    stranger = device_sorted(hive)
    own = link(hv, stranger, "same-as", mine, david, T % 11)
    own_out = retract(hv, stranger, own, T % 12)
    assert joined(hive, hive.entries + [device_join, own, own_out]) == ["x-hwatch:david"]
    # the writer's own withdrawal still drops its own join
    assert joined(hive, hive.entries + [device_join, retract(hv, hive.plain, device_join, T % 13)]) == []


def test_a_module_entity_fact_onto_a_shared_entity_wires_nothing(hive):
    hv = hive.hv
    david, fact_, mine, theirs = _world(hive)
    mfact = TL._entry(hv, hive.mod, "fact", fact("a module fact"), T % 6)

    def ef(dev, entity, fct, ts):
        return TL._entry(hv, dev, "entity_fact", {"entity_ref": ref(entity), "fact_ref": ref(fct), "source": SRC}, ts)
    count = lambda conn: conn.execute("SELECT count(*) FROM entity_facts").fetchone()[0]
    assert count(project(hive, hive.entries + [mfact, ef(hive.mod, david, mfact, T % 10)])) == 0
    assert count(project(hive, hive.entries + [mfact, ef(hive.mod, theirs, mfact, T % 10)])) == 0
    assert count(project(hive, hive.entries + [mfact, ef(hive.mod, mine, mfact, T % 10)])) == 1       # its own entity: fine
    assert count(project(hive, hive.entries + [mfact, ef(hive.plain, david, mfact, T % 10)])) == 1     # a device: fine


def test_mutant_ignoring_the_withdrawal_leaves_the_join(hive, monkeypatch):
    hv = hive.hv
    david, mine, _ = _join_world(hive)
    join = link(hv, hive.plain, "same-as", mine, david, T % 10)
    out = retract(hv, hive.plain, join, T % 11)
    real = hv._same_as_live
    monkeypatch.setattr(hv, "_same_as_live", lambda entries, gov, by_ref=None: real([e for e in entries if e["type"] != "retract"], gov))
    assert joined(hive, hive.entries + [join, out]) == ["x-hwatch:david"]


# ── the CLI: join, unjoin, show ──────────────────────────────────────────────────────────────────────────────

def test_cli_join_show_no_joins_and_unjoin(tmp_path, monkeypatch, capsys):
    """A bootstrap hive (no owner yet): one device writes both entities, joins them, reads the view, withdraws."""
    hv = TL._loadhv(tmp_path, monkeypatch)
    hv.init_db()

    def run(**kw):
        ns = dict(action=None, name=None, type=None, attr=None, fact_id=None, confidence=None, source=None, to=None,
                  no_joins=False, owner=False)
        hv.entity(SimpleNamespace(**dict(ns, **kw)))
        return capsys.readouterr().out

    run(action="add", name="david", type="person")
    run(action="add", name="x-hr:david", type="person")
    assert "Joined 'x-hr:david' to 'david'" in run(action="join", name="x-hr:david", to="david", source="manual")
    shown = run(action="show", name="david")
    assert "Joined: x-hr:david (person)  same-as " in shown
    assert "Joined:" not in run(action="show", name="david", no_joins=True)
    assert "Joined:" not in run(action="show", name="x-hr:david")
    out = run(action="unjoin", name="x-hr:david", to="david", source="manual")
    assert "Withdrew the join" in out and "Joined:" not in run(action="show", name="david")
    with pytest.raises(SystemExit):                                              # the ends must be x-<module>: → unprefixed
        run(action="join", name="david", to="x-hr:david")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run(action="unjoin", name="x-hr:david", to="david")                      # nothing live to withdraw
    capsys.readouterr()
