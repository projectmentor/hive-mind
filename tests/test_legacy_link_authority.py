"""#204: the pre-1.19 payload fields `supersedes_ref`, `supersedes` and `resolves_ref` go through the same
authority rule as a `link` entry. They command only when the owner key signed the entry or its signer wrote
the target; from anyone else they do nothing. An unsigned field has no signer and commands only while the
journal has no owner. `resolves_ref` must name a fact. A `link` entry is the only way to supersede or resolve."""

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _decision, _project, _entry, T0)  # noqa: E402


def _legacy_decision(hv, dev, content, ts, target, field="supersedes_ref", owner=None):
    """A decision carrying a legacy supersede field, as a pre-1.19 writer emitted it."""
    value = [target["node_id"], target["seq"]] if field == "supersedes_ref" else target
    return _entry(hv, dev, "decision", {"content": content, "rationale": "r", "source": "manual", "tags": [],
                                        field: value}, ts, owner=owner)


def _legacy_fact(hv, dev, content, ts, target, owner=None):
    return _entry(hv, dev, "fact", {"content": content, "tags": [], "importance": 0.5, "source": "manual",
                                    "resolves_ref": [target["node_id"], target["seq"]]}, ts, owner=owner)


def _unsigned(entry):
    """A pre-key entry: the device name stays, and nothing proves it."""
    return {k: v for k, v in entry.items() if k not in ("sig", "pub")}


def _local_id(conn, table, content):
    return conn.execute(f"SELECT id FROM {table} WHERE content = ?", (content,)).fetchone()[0]


def _superseded(conn, content):
    return conn.execute("SELECT superseded_by FROM decisions WHERE content = ?", (content,)).fetchone()[0]


def _resolves(conn, content):
    return conn.execute("SELECT resolves FROM facts WHERE content = ?", (content,)).fetchone()[0]


@pytest.mark.parametrize("field", ["supersedes_ref", "supersedes"])
def test_a_non_authors_legacy_supersede_leaves_the_target_standing(tmp_path, monkeypatch, field):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    # local ids follow projection order, which depends on device ids: read `old`'s id from the projection,
    # and check it is the same one in the journal that carries the attack
    probe = _project(hv, tmp_path, base + [old, _decision(hv, b, "ship never", "2026-01-03T00:00:00Z")])
    old_id = _local_id(probe, "decisions", "ship on friday")
    target = old if field == "supersedes_ref" else old_id
    attack = _legacy_decision(hv, b, "ship never", "2026-01-03T00:00:00Z", target, field)
    conn = _project(hv, tmp_path, base + [old, attack])
    assert _local_id(conn, "decisions", "ship on friday") == old_id
    assert _superseded(conn, "ship on friday") is None


@pytest.mark.parametrize("field", ["supersedes_ref", "supersedes"])
def test_an_authors_own_legacy_supersede_still_commands(tmp_path, monkeypatch, field):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, _b, _c), base = _owned_hive(hv)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    probe = _project(hv, tmp_path, base + [old])
    target = old if field == "supersedes_ref" else _local_id(probe, "decisions", "ship on friday")
    new = _legacy_decision(hv, a, "ship on monday", "2026-01-03T00:00:00Z", target, field)
    conn = _project(hv, tmp_path, base + [old, new])
    assert _superseded(conn, "ship on friday") is not None


def test_an_owner_signed_legacy_supersede_commands(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    new = _legacy_decision(hv, b, "ship on monday", "2026-01-03T00:00:00Z", old, owner=(oseed, opub))
    conn = _project(hv, tmp_path, base + [old, new])
    assert _superseded(conn, "ship on friday") is not None


def test_a_non_authors_legacy_resolves_ref_writes_nothing_and_an_authors_does(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    old = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:00Z")
    by_other = _legacy_fact(hv, b, "issue Z is fixed by b", "2026-01-03T00:00:00Z", old)
    by_author = _legacy_fact(hv, a, "issue Z is fixed by a", "2026-01-03T00:00:01Z", old)
    conn = _project(hv, tmp_path, base + [old, by_other])
    assert _resolves(conn, "issue Z is fixed by b") is None
    conn = _project(hv, tmp_path, base + [old, by_author])
    assert _resolves(conn, "issue Z is fixed by a") is not None


def test_a_shared_row_does_not_lend_its_author_to_a_legacy_field(tmp_path, monkeypatch):
    """Facts with the same content share one row. The authority judges the entry the ref names, not the row's
    first author: b writes A's fact's content, then a legacy `resolves_ref` naming A's entry commands nothing,
    and the doctor agrees with the projection."""
    hv = _loadhv(tmp_path, monkeypatch)
    _, devs, base = _owned_hive(hv)
    a, b = sorted(devs[:2], key=lambda d: d["id"], reverse=True)   # b's id sorts first: the row's first author
    theirs = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:00Z")
    mine = _fact(hv, b, "issue Z is open", "2026-01-02T00:00:01Z")
    attack = _legacy_fact(hv, b, "issue Z is fixed by b", "2026-01-03T00:00:00Z", theirs)
    entries = base + [theirs, mine, attack]
    conn = _project(hv, tmp_path, entries)
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE content = 'issue Z is open'").fetchone()[0] == 1
    assert _resolves(conn, "issue Z is fixed by b") is None
    assert len(hv._links_unauthorized(entries, hv._governance_state(entries))) == 1


def test_a_legacy_resolves_ref_naming_a_non_fact_writes_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, _b, _c), base = _owned_hive(hv)
    dec = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    bad = _legacy_fact(hv, a, "this resolves a decision", "2026-01-03T00:00:00Z", dec)   # same author: authority holds
    conn = _project(hv, tmp_path, base + [dec, bad])
    assert _resolves(conn, "this resolves a decision") is None


@pytest.mark.parametrize("when", ["before the owner", "after the owner"])
def test_an_unsigned_self_authored_legacy_field_is_evidence_once_an_owner_exists(tmp_path, monkeypatch, when):
    """#211 finding 4. Grandfathering reads the final governance state, not the entry's date. Once an
    owner exists, an unsigned legacy field is evidence even when its node_id matches the target's author,
    and `link-authz` lists it."""
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, _b, _c), base = _owned_hive(hv)
    ts_old, ts_new = (("2025-06-01T00:00:00Z", "2025-06-02T00:00:00Z") if when.startswith("before")
                      else ("2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z"))
    old = _unsigned(_decision(hv, a, "ship on friday", ts_old))
    new = _unsigned(_legacy_decision(hv, a, "ship on monday", ts_new, old))
    fact_old = _unsigned(_fact(hv, a, "issue Z is open", ts_old))
    fixed = _unsigned(_legacy_fact(hv, a, "issue Z is fixed", ts_new, fact_old))
    entries = base + [old, new, fact_old, fixed]
    conn = _project(hv, tmp_path, entries)
    assert _superseded(conn, "ship on friday") is None
    assert _resolves(conn, "issue Z is fixed") is None
    down = hv._links_unauthorized(entries, hv._governance_state(entries))
    assert len(down) == 2
    assert any("legacy supersedes_ref" in d and a["id"] in d for d in down)
    assert any("legacy resolves_ref" in d and a["id"] in d for d in down)


def test_an_unsigned_self_authored_legacy_field_commands_while_the_journal_has_no_owner(tmp_path, monkeypatch):
    """The other half of finding 4: with no owner, the unsigned grandfather reads the entry's node_id, so a
    self-authored legacy field still commands."""
    hv = _loadhv(tmp_path, monkeypatch)
    from test_links import _device
    a = _device(hv)
    old = _unsigned(_decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z"))
    new = _unsigned(_legacy_decision(hv, a, "ship on monday", "2026-01-03T00:00:00Z", old))
    fact_old = _unsigned(_fact(hv, a, "issue Z is open", "2026-01-02T00:00:01Z"))
    fixed = _unsigned(_legacy_fact(hv, a, "issue Z is fixed", "2026-01-03T00:00:01Z", fact_old))
    conn = _project(hv, tmp_path, [old, new, fact_old, fixed])
    assert _superseded(conn, "ship on friday") is not None
    assert _resolves(conn, "issue Z is fixed") is not None


def test_a_self_authored_pre_link_journal_projects_as_before(tmp_path, monkeypatch):
    """Device-signed entries and no governance: the author rule applies, so a self-authored field commands."""
    hv = _loadhv(tmp_path, monkeypatch)
    from test_links import _device
    a = _device(hv)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    f_old = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:01Z")
    new = _legacy_decision(hv, a, "ship on monday", "2026-01-03T00:00:00Z", old)
    fixed = _legacy_fact(hv, a, "issue Z is fixed", "2026-01-03T00:00:01Z", f_old)
    conn = _project(hv, tmp_path, [old, f_old, new, fixed])
    assert _superseded(conn, "ship on friday") is not None
    assert _resolves(conn, "issue Z is fixed") is not None


def test_doctor_lists_a_downgraded_legacy_field(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    f_old = _fact(hv, a, "issue Z is open", "2026-01-02T00:00:01Z")
    attack = _legacy_decision(hv, b, "ship never", "2026-01-03T00:00:00Z", old)
    attack_f = _legacy_fact(hv, b, "issue Z is fixed by b", "2026-01-03T00:00:01Z", f_old)
    own = _legacy_decision(hv, a, "ship on monday", "2026-01-03T00:00:02Z", old)
    entries = base + [old, f_old, attack, attack_f, own]
    down = hv._links_unauthorized(entries, hv._governance_state(entries))
    assert len(down) == 2
    assert any("legacy supersedes_ref" in d and b["id"] in d for d in down)
    assert any("legacy resolves_ref" in d and b["id"] in d for d in down)


def test_no_write_path_emits_a_legacy_field(hive):
    """remember, decide, --supersedes, --resolves and revoke write `link` entries; none writes a legacy field."""
    hive.run("remember", "issue Z is open")
    hive.run("remember", "issue Z is fixed", "--resolves", hive.sid(1))
    hive.run("decide", "plan A", "--rationale", "r")
    hive.run("decide", "plan B replaces A", "--rationale", "r", "--supersedes", hive.sid(1, "decision"))
    hive.run("decide", "plan C", "--rationale", "r")
    hive.run("decide", "--revoke", hive.sid(3, "decision"), "--rationale", "r")
    entries = hive.entries()
    assert {e["payload"].get("kind") for e in entries if e["type"] == "link"} == {"resolves", "supersedes"}
    for e in entries:
        assert not {"supersedes", "supersedes_ref", "resolves_ref"} & set(e["payload"]), e
