"""#260: retire a device and keep its past content.

`hive-mind group retire <device>` writes the owner's `purge` with two signed fields, `keep_through` (a seq) and
`keep_hash` (`compute_hash` of the entry at that seq). The latest honoured purge decides: with a well-formed pair the
projection keeps the entries the head names, walking `prev_hash` down the device's chain, and they keep their
corroboration weight; a plain purge, or a malformed pair, drops everything. Ingest stores a verified entry with
`seq <= keep_through` from a purged device in any order. The reviewer's seven cases (comment 6025770390) each run as
one batch and one entry per batch, in every order, then a re-offer of the full set; the mutants at the end each break
one rule and are caught.
"""

import itertools
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _device, _gov, _entry, _project, _conf  # noqa: E402
from test_bounded_timestamps import _hive, _journal, _marker, _mutated_hv, _contents, ts  # noqa: E402

GENESIS_PREV = "sha256:genesis"


def _cfact(hv, dev, name, at, seq, prev):
    """A fact of `dev` at `seq` carrying `prev_hash`, as `append_journal` writes it, signed by the device."""
    dev["seq"] = seq - 1
    e = _entry(hv, dev, "fact", {"content": name, "tags": [], "importance": 0.5, "source": "manual"}, at)
    e.pop("sig"), e.pop("pub")
    e["prev_hash"] = prev
    return hv._sign_entry(e, dev["seed"], dev["pub"])


def _chain(hv, dev, names):
    """A contiguous chain seq 1..n of facts, each `prev_hash` the hash of the one before."""
    out, prev = [], GENESIS_PREV
    for i, name in enumerate(names, 1):
        e = _cfact(hv, dev, name, ts(3 + i), i, prev)
        prev = hv.compute_hash(e)
        out.append(e)
    return out


def _act(hv, owner, x, head, seq, at, plain=False, **override):
    """The owner's purge of X on the owner's chain; with a `head` entry it is a retire."""
    p = {"action": "purge", "device_id": x["id"]}
    if not plain:
        p.update(keep_through=head["seq"], keep_hash=hv.compute_hash(head))
    p.update(override)
    return _gov(hv, p, owner[0], owner[1], at, seq)


def _world(hv):
    """Genesis owner, d0 and d1 admitted, and X admitted at ownerdev seq 4 (01:02)."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    base = base + [_gov(hv, {"action": "admit", "device_id": x["id"], "principal": "px"}, owner[0], owner[1], ts(1, 2), 4)]
    return owner, x, base


def _outcome(hv, tmp_path, root=True):
    """What the projection makes of what a node holds: the honoured entries, who is admitted and who is purged, the
    projected facts, and the root of the raw journal."""
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    gov = hv._governance_state(j)
    honoured = frozenset((e["node_id"], e["seq"]) for e in hv._admitted_content(hv._ts_unskipped(j, gov), gov))
    r = hv.merkle.hash_entries(sorted(j, key=lambda e: (e["node_id"], e["seq"]))) if root else None
    return (honoured, frozenset(gov["admitted"]), frozenset(gov["purged"]), frozenset(_contents(_project(hv, tmp_path, j))), r)


def _runs(hv, tmp_path, base, entries, root=True):
    """Every order of `entries`, as one batch and as one entry per batch, then a re-offer of the full set."""
    seen = []
    for perm in itertools.permutations(entries):
        for plan in ([list(perm)], [[e] for e in perm]):
            _journal(hv, base)
            for batch in plan:
                hv.append_foreign_entries(batch)
            hv.append_foreign_entries(list(entries))
            o = _outcome(hv, tmp_path, root)
            if o not in seen:
                seen.append(o)
    return seen


def _one(hv, tmp_path, base, entries, root=True):
    seen = _runs(hv, tmp_path, base, entries, root)
    assert len(seen) == 1, f"{len(seen)} different outcomes"
    return seen[0]


def _facts_of(o):
    return set(o[3])


# ── the seven cases ──────────────────────────────────────────────────────────────────────────────────────────────

def _case1(hv):
    owner, x, base = _world(hv)
    x["seq"] = 4
    fact = _cfact(hv, x, "x5", ts(12), 5, "sha256:" + "1" * 64)
    act = _act(hv, owner, x, fact, 5, ts(13))
    readmit = _gov(hv, {"action": "admit", "device_id": x["id"], "principal": "px"}, owner[0], owner[1], ts(14), 6)
    return base, [fact, act, readmit], x


def test_case1_the_prefix_projects_against_the_act_and_a_later_admit_changes_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    base, entries, x = _case1(hv)
    honoured, admitted, purged, facts, _root = _one(hv, tmp_path, base, entries)
    assert (x["id"], 5) in honoured and facts == {"x5"}
    assert x["id"] in purged and x["id"] not in admitted - purged      # the later admit clears no tombstone


def _case2(hv, stamp, with_marker):
    owner, x, base = _world(hv)
    x["seq"] = 4
    head = _cfact(hv, x, "x5", ts(12), 5, "sha256:" + "1" * 64)
    x["seq"] = 10
    above = _cfact(hv, x, "x11", stamp, 11, "sha256:" + "2" * 64)
    act = _act(hv, owner, x, head, 5, ts(13))
    if with_marker:
        base = base + [_marker(hv, owner, {"ownerdev": 4}, seq=5)]
        act = _act(hv, owner, x, head, 6, ts(13))
    return base, [head, above, act], x


@pytest.mark.parametrize("stamp", [ts(14), "2020-01-01T00:00:00.000+00:00"], ids=["stamped-later", "backdated-2020"])
@pytest.mark.parametrize("with_marker", [False, True], ids=["no-marker", "marker"])
def test_case2_above_the_cutoff_nothing_projects_whatever_its_stamp(tmp_path, monkeypatch, stamp, with_marker):
    hv = _loadhv(tmp_path, monkeypatch)
    base, entries, x = _case2(hv, stamp, with_marker)
    honoured, admitted, purged, facts, _root = _one(hv, tmp_path, base, entries, root=False)   # roots may differ above
    assert facts == {"x5"} and (x["id"], 11) not in honoured and x["id"] in purged


def _case3(hv):
    owner, x, base = _world(hv)
    s = _chain(hv, x, ["x1", "x2", "x3", "x4", "x5"])
    act = _act(hv, owner, x, s[4], 5, ts(13))
    return base + [s[0], s[1], s[4]], [act, s[2], s[3]], x       # the signing node lacks seq 3 and 4: they arrive later


def test_case3_an_honest_gap_fills_in_after_the_act_in_every_order(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    base, entries, x = _case3(hv)
    honoured, _adm, purged, facts, _root = _one(hv, tmp_path, base, entries)       # the same root in every order too
    assert facts == {"x1", "x2", "x3", "x4", "x5"} and x["id"] in purged


def _case4(hv):
    owner, x, base = _world(hv)
    s = _chain(hv, x, ["x1", "x2", "x3", "x4", "x5"])
    act = _act(hv, owner, x, s[4], 5, ts(13))
    x["seq"] = 2
    other = _cfact(hv, x, "x3 forged", ts(6), 3, hv.compute_hash(s[1]))    # a different signed body at seq 3
    return base + [s[0], s[1], s[3], s[4]], [act, other], x


def test_case4_an_unlinked_gap_does_not_project_in_any_order(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    base, entries, x = _case4(hv)
    honoured, _adm, _purged, facts, _root = _one(hv, tmp_path, base, entries)
    # the head names x4 and x4 names a seq 3 that is not held: x1, x2 and the other body are not on the chain
    assert facts == {"x4", "x5"} and (x["id"], 3) not in honoured


def test_case5_a_plain_purge_after_the_retire_drops_the_prefix_in_every_order(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, x, base = _world(hv)
    x["seq"] = 4
    fact = _cfact(hv, x, "x5", ts(12), 5, "sha256:" + "1" * 64)
    retire = _act(hv, owner, x, fact, 5, ts(13))
    plain = _act(hv, owner, x, fact, 6, ts(14), plain=True)
    honoured, admitted, purged, facts, _root = _one(hv, tmp_path, base + [fact], [retire, plain])
    assert facts == set() and x["id"] in purged and (x["id"], 5) not in honoured
    # and a retire stamped after the plain purge wins instead: the latest honoured purge decides
    retire_late = _act(hv, owner, x, fact, 7, ts(15))
    assert _one(hv, tmp_path, base + [fact], [plain, retire_late])[3] == frozenset({"x5"})


def test_case6_a_revoke_drops_the_content_and_a_readmit_brings_every_seq_back(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, x, base = _world(hv)
    x["seq"] = 4
    f5 = _cfact(hv, x, "x5", ts(12), 5, "sha256:" + "1" * 64)
    f11 = _cfact(hv, x, "x11", ts(12, 5), 11, "sha256:" + "2" * 64)
    revoke = _gov(hv, {"action": "revoke", "device_id": x["id"]}, owner[0], owner[1], ts(13), 5)
    readmit = _gov(hv, {"action": "admit", "device_id": x["id"], "principal": "px"}, owner[0], owner[1], ts(14), 6)
    held = base + [f5, f11]
    assert _one(hv, tmp_path, held, [revoke])[3] == frozenset()
    o = _one(hv, tmp_path, held, [revoke, readmit])
    assert o[3] == {"x5", "x11"} and x["id"] in o[1] and not o[2]


def test_case7_the_pre_genesis_exemption_never_applies_to_a_purged_device(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    z = _device(hv)
    old = "2025-01-01T00:00:00.000+00:00"
    z1 = _cfact(hv, z, "z1", old, 1, GENESIS_PREV)
    z2 = _cfact(hv, z, "z2", "2025-01-01T00:00:01.000+00:00", 2, hv.compute_hash(z1))
    retire = _act(hv, owner, z, z1, 4, ts(13))
    _journal(hv, [z1, z2] + base + [retire])
    assert _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR))) == {"z1"}
    _journal(hv, [z1, z2] + base)             # no act: the bootstrap writer stays (the pre-genesis exemption)
    assert _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR))) == {"z1", "z2"}


# ── a malformed pair is a plain purge ────────────────────────────────────────────────────────────────────────────

def _malformed(hv, tmp_path, how):
    owner, x, base = _world(hv)
    s = _chain(hv, x, ["x1", "x2"])
    good = {"keep_through": 2, "keep_hash": hv.compute_hash(s[1])}
    bad = {"bool": {"keep_through": True, "keep_hash": hv.compute_hash(s[0])},
           "wrong-hash": {"keep_through": 2, "keep_hash": hv.compute_hash(s[0])},      # names seq 1, not 2
           "wrong-seq": {"keep_through": 1, "keep_hash": hv.compute_hash(s[1])},       # the hashed entry is seq 2
           "string-seq": {"keep_through": "2", "keep_hash": hv.compute_hash(s[1])},
           "no-hash": {"keep_through": 2},
           "short-hash": {"keep_through": 2, "keep_hash": "sha256:abc"},
           "good": good}[how]
    act = _gov(hv, {"action": "purge", "device_id": x["id"], **bad}, owner[0], owner[1], ts(13), 5)
    _journal(hv, base + s + [act])
    return _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR)))


@pytest.mark.parametrize("how", ["bool", "wrong-hash", "wrong-seq", "string-seq", "no-hash", "short-hash"])
def test_a_malformed_pair_is_a_plain_purge(tmp_path, monkeypatch, how):
    assert _malformed(_loadhv(tmp_path, monkeypatch), tmp_path, how) == set()


def test_a_well_formed_pair_keeps_the_chain(tmp_path, monkeypatch):
    assert _malformed(_loadhv(tmp_path, monkeypatch), tmp_path, "good") == {"x1", "x2"}


def test_a_lower_cutoff_cuts_the_tail(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, x, base = _world(hv)
    s = _chain(hv, x, ["x1", "x2", "x3"])
    _journal(hv, base + s + [_act(hv, owner, x, s[1], 5, ts(13))])
    assert _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR))) == {"x1", "x2"}


def test_a_down_level_node_reads_the_retire_as_a_plain_purge(tmp_path, monkeypatch):
    """The fields ride on `purge`: a node that does not know them still tombstones the device."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, x, base = _world(hv)
    s = _chain(hv, x, ["x1"])
    act = _act(hv, owner, x, s[0], 5, ts(13))
    assert act["payload"]["action"] == "purge"
    _journal(hv, base + s + [act])
    gov = hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))
    assert x["id"] in gov["purged"] and x["id"] not in gov["admitted"]


# ── a kept entry keeps its weight ────────────────────────────────────────────────────────────────────────────────

def _weight_world(hv, tmp_path, retire):
    owner, x, base = _world(hv)
    s = _chain(hv, x, ["claim"])
    es = base + s + ([_act(hv, owner, x, s[0], 5, ts(13))] if retire else [])
    return _conf(_project(hv, tmp_path, es), "claim")


def test_a_kept_fact_weighs_what_it_weighed_before_the_retire(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    before = _weight_world(hv, tmp_path, False)
    assert before > 0 and _weight_world(hv, tmp_path, True) == before


# ── the CLI ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _cli_hive(tmp_path):
    from test_group import _run, _gov as _govstate
    _run(tmp_path, "owner", "init")
    _run(tmp_path, "group", "admit", "dev-a", "--principal", "alice")
    for c in ("one", "two", "three"):
        _run(tmp_path, "remember", c, "--source", "agent", node_id="dev-a")
    return _run, _govstate


def _purge_acts(tmp_path):
    import json
    out = []
    for f in sorted((tmp_path / "journal").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            e = json.loads(line)
            if e.get("type") == "governance" and e["payload"].get("action") == "purge":
                out.append(e["payload"])
    return out


def test_retire_defaults_to_the_highest_held_seq_and_keeps_the_content(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    r = _run(tmp_path, "group", "retire", "dev-a")
    assert "Retired dev-a" in r.stdout
    act = _purge_acts(tmp_path)[0]
    assert act["keep_through"] == 3 and act["keep_hash"].startswith("sha256:")
    m, gov = _govstate(tmp_path)
    assert "dev-a" in gov["purged"] and "dev-a" not in gov["admitted"]
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    assert {r[0] for r in conn.execute("SELECT content FROM facts")} == {"one", "two", "three"}


def test_retire_through_a_lower_held_seq_cuts_the_tail_and_a_seq_not_held_is_refused(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    assert "does not hold seq 9" in _run(tmp_path, "group", "retire", "dev-a", "--through", "9").stdout
    assert _purge_acts(tmp_path) == []
    _run(tmp_path, "group", "retire", "dev-a", "--through", "2")
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    assert {r[0] for r in conn.execute("SELECT content FROM facts")} == {"one", "two"}


def test_retiring_an_already_purged_device_needs_confirm(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    _run(tmp_path, "group", "purge", "dev-a")
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
    assert "--confirm" in _run(tmp_path, "group", "retire", "dev-a").stdout
    assert len(_purge_acts(tmp_path)) == 1
    _run(tmp_path, "group", "retire", "dev-a", "--confirm")
    conn = sqlite3.connect(tmp_path / "store.db")
    assert {r[0] for r in conn.execute("SELECT content FROM facts")} == {"one", "two", "three"}
    assert "dev-a" in _run(tmp_path, "group", "list").stdout


def test_a_plain_purge_still_drops_everything(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    _run(tmp_path, "group", "purge", "dev-a")
    assert "keep_through" not in _purge_acts(tmp_path)[0]
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


# ── a revoked former member (#271) ───────────────────────────────────────────────────────────────────────────────

def _cli_contents(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    return {r[0] for r in conn.execute("SELECT content FROM facts")}


def test_a_revoked_former_member_retired_with_confirm_projects_its_prefix(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    _run(tmp_path, "group", "revoke", "dev-a")
    assert _cli_contents(tmp_path) == set()
    r = _run(tmp_path, "group", "retire", "dev-a", "--confirm")
    assert "Retired dev-a" in r.stdout
    act = _purge_acts(tmp_path)[0]
    assert act["keep_through"] == 3 and act["keep_hash"].startswith("sha256:")
    assert _cli_contents(tmp_path) == {"one", "two", "three"}


def test_a_revoked_former_member_without_confirm_is_refused(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    _run(tmp_path, "group", "revoke", "dev-a")
    out = _run(tmp_path, "group", "retire", "dev-a").stdout
    assert "--confirm" in out and "revoke dropped" in out
    assert _purge_acts(tmp_path) == []
    assert _cli_contents(tmp_path) == set()


def test_a_never_admitted_revoked_id_is_refused_even_with_confirm(tmp_path):
    _run, _govstate = _cli_hive(tmp_path)
    _run(tmp_path, "group", "revoke", "dev-x")
    out = _run(tmp_path, "group", "retire", "dev-x", "--confirm").stdout
    assert "not an admitted or purged device" in out
    assert _purge_acts(tmp_path) == []


# ── gaps the verifier found in #264 (#266) ───────────────────────────────────────────────────────────────────────

def _raw_confidence(hv, with_x_tail):
    """X retired through seq 1; admitted d0 asserts `c`; X asserts `c` at seq 2, above the cutoff. The confidence of `c`
    from `_content_evidence` over the raw entries, as `unforget` and `hv feed` call it."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    base = base + [_gov(hv, {"action": "admit", "device_id": x["id"], "principal": "px"}, owner[0], owner[1], ts(1, 2), 4)]
    x1 = _cfact(hv, x, "x1", ts(5), 1, GENESIS_PREV)
    tail = _cfact(hv, x, "c", ts(6), 2, hv.compute_hash(x1))
    y = _cfact(hv, d0, "c", ts(7), 1, GENESIS_PREV)
    es = base + [x1, y, _act(hv, owner, x, x1, 5, ts(13))] + ([tail] if with_x_tail else [])
    gov = hv._governance_state(es)
    return hv._content_confidence(hv._content_evidence(es, gov)["c"], gov)


def _evidence_check(hv, tmp_path):
    return _raw_confidence(hv, True), _raw_confidence(hv, False)


def test_the_raw_evidence_leaves_out_what_a_retire_does_not_keep(tmp_path, monkeypatch):
    with_tail, without = _evidence_check(_loadhv(tmp_path, monkeypatch), tmp_path)
    assert with_tail == without > 0


def _n_plus_one_check(hv, tmp_path):
    """The seqs of X held after the act and the entries arrive one per batch, in two orders; keep_through is 5."""
    owner, x, base = _world(hv)
    x["seq"] = 4
    head = _cfact(hv, x, "x5", ts(12), 5, "sha256:" + "1" * 64)
    above = _cfact(hv, x, "x6", ts(12, 5), 6, hv.compute_hash(head))
    act = _act(hv, owner, x, head, 5, ts(13))
    out = []
    for order in ([act, above, head], [head, act, above]):
        _journal(hv, base)
        for e in order:
            hv.append_foreign_entries([e])
        out.append(sorted(e["seq"] for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR) if e["node_id"] == x["id"]))
    return out


def test_an_entry_one_above_the_cutoff_is_refused_at_ingest(tmp_path, monkeypatch):
    got = _n_plus_one_check(_loadhv(tmp_path, monkeypatch), tmp_path)
    assert got == [[5], [5]]


def _walk_check(hv, tmp_path):
    """The head (seq 2) whose `prev_hash` names a held body of the same device at seq 3: the walk must not follow it."""
    owner, x, base = _world(hv)
    x1 = _cfact(hv, x, "x1", ts(4), 1, GENESIS_PREV)
    x3 = _cfact(hv, x, "x3", ts(6), 3, hv.compute_hash(x1))
    head = _cfact(hv, x, "x2", ts(5), 2, hv.compute_hash(x3))
    _journal(hv, base + [x1, head, x3, _act(hv, owner, x, head, 5, ts(13))])
    return _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR)))


def test_a_head_naming_a_higher_seq_body_does_not_pull_it_in(tmp_path, monkeypatch):
    assert _walk_check(_loadhv(tmp_path, monkeypatch), tmp_path) == {"x2"}


def _late_prefix_check(hv, tmp_path):
    """Retire and plain purge held, then the prefix arrives: refused. With the plain purge first and a later retire,
    the prefix is stored and projects."""
    owner, x, base = _world(hv)
    x["seq"] = 4
    fact = _cfact(hv, x, "x5", ts(12), 5, "sha256:" + "1" * 64)
    retire = _act(hv, owner, x, fact, 5, ts(13))
    plain = _act(hv, owner, x, fact, 6, ts(14), plain=True)
    retire_late = _act(hv, owner, x, fact, 7, ts(15))
    out = []
    for held in ([retire, plain], [plain, retire_late]):
        _journal(hv, base)
        hv.append_foreign_entries(held)
        hv.append_foreign_entries([fact])
        j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
        out.append(((x["id"], 5) in {(e["node_id"], e["seq"]) for e in j}, frozenset(_contents(_project(hv, tmp_path, j)))))
    return out


def test_case5_the_prefix_arriving_after_the_plain_purge_is_refused(tmp_path, monkeypatch):
    assert _late_prefix_check(_loadhv(tmp_path, monkeypatch), tmp_path) == [(False, frozenset()), (True, frozenset({"x5"}))]


# ── the mutants: each one is caught ──────────────────────────────────────────────────────────────────────────────

def _m_refusal_stays(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "            if keep is None or not isinstance(seq, int) or seq > keep[0]:",
                       "            if True:")


def _m_seq_cutoff(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "    return kept\n\n\ndef _purge_unkept",
                       "    return {(e['node_id'], e['seq']) for e in entries if e.get('node_id') in keep\n"
                       "            and isinstance(e.get('seq'), int) and e['seq'] <= keep[e['node_id']][0]}\n\n\ndef _purge_unkept")


def _m_bool_seq(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1",
                       "isinstance(seq, int) and seq >= 1")


def _m_governed_side_zeroes(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "if has_owner and dev not in keep and (dev not in admitted",
                       "if has_owner and (dev not in admitted")


def _m_pre_genesis_exempt(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch,
                       '    ended = gov.get("purged", set()) | gov.get("revoked", set())',
                       '    ended = gov.get("revoked", set())')


def _m_hash_not_checked(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "        if e is None or e.get(\"seq\") != seq:",
                       "        if e is None:")


def _probe(hv, tmp_path):
    """Every behaviour above, as one tuple, so a mutant that changes any of them differs from the real one."""
    out = []
    for case in (_case1, _case4):
        base, entries, x = case(hv)
        out.append(_one(hv, tmp_path, base, entries)[3])
    base, entries, x = _case3(hv)
    out.append(_one(hv, tmp_path, base, entries)[3])
    for how in ("bool", "wrong-hash", "wrong-seq"):
        out.append(frozenset(_malformed(hv, tmp_path, how)))
    out.append(_weight_world(hv, tmp_path, True))
    owner, (d0, d1), base = _hive(hv)
    z = _device(hv)
    z1 = _cfact(hv, z, "z1", "2025-01-01T00:00:00.000+00:00", 1, GENESIS_PREV)
    z2 = _cfact(hv, z, "z2", "2025-01-01T00:00:01.000+00:00", 2, hv.compute_hash(z1))
    _journal(hv, [z1, z2] + base + [_act(hv, owner, z, z1, 4, ts(13))])
    out.append(frozenset(_contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR)))))
    return out


def test_the_real_projection_gives_the_expected_probe(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    got = _probe(hv, tmp_path)
    assert got[0] == {"x5"} and got[1] == {"x4", "x5"} and got[2] == {"x1", "x2", "x3", "x4", "x5"}
    assert got[3] == got[4] == got[5] == frozenset() and got[6] > 0 and got[7] == {"z1"}


@pytest.mark.parametrize("mutant", [_m_refusal_stays, _m_seq_cutoff, _m_bool_seq, _m_governed_side_zeroes,
                                    _m_pre_genesis_exempt, _m_hash_not_checked],
                         ids=lambda m: m.__name__.strip("_"))
def test_each_mutant_is_caught(tmp_path, monkeypatch, mutant):
    (tmp_path / "real").mkdir()
    (tmp_path / "mut").mkdir()
    real = _probe(_loadhv(tmp_path / "real", monkeypatch), tmp_path / "real")
    try:
        got = _probe(mutant(tmp_path / "mut", monkeypatch), tmp_path / "mut")
    except AssertionError:
        return                        # the orders no longer agree: caught
    assert got != real


# ── the #266 mutants: each new test fails against its mutant ─────────────────────────────────────────────────────

def _m_evidence_unfiltered(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch,
                       "zero-weight link.\"\"\"\n    gov = gov or _DEFAULT_GOV\n    entries = _purge_unkept(entries, gov)    # #260: a purged device's content counts only as far as a retire keeps it\n",
                       "zero-weight link.\"\"\"\n    gov = gov or _DEFAULT_GOV\n")


def _m_ingest_n_plus_one(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "if keep is None or not isinstance(seq, int) or seq > keep[0]:",
                       "if keep is None or not isinstance(seq, int) or seq > keep[0] + 1:")


def _m_walk_no_seq_guard(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, 'e = nxt if nxt is not None and nxt["seq"] < e["seq"] else None', "e = nxt")


def _m_plain_purge_keeps_pair(tmp_path, monkeypatch):
    return _mutated_hv(tmp_path, monkeypatch, "            if keep is None or not isinstance(seq, int) or seq > keep[0]:",
                       "            if False:")


@pytest.mark.parametrize("mutant, check", [(_m_evidence_unfiltered, _evidence_check),
                                           (_m_ingest_n_plus_one, _n_plus_one_check),
                                           (_m_walk_no_seq_guard, _walk_check),
                                           (_m_plain_purge_keeps_pair, _late_prefix_check)],
                         ids=lambda v: v.__name__.strip("_"))
def test_each_new_test_fails_against_its_mutant(tmp_path, monkeypatch, mutant, check):
    (tmp_path / "real").mkdir()
    (tmp_path / "mut").mkdir()
    real = check(_loadhv(tmp_path / "real", monkeypatch), tmp_path / "real")
    got = check(mutant(tmp_path / "mut", monkeypatch), tmp_path / "mut")
    assert got != real
