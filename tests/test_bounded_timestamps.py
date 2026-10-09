"""#217: bounded entry timestamps.

Journal order is (timestamp, node_id, seq) and the writer picks the timestamp, so two rules bound it per device,
in the projection only (ingest stores what they would skip), once the owner signs a `freeze-timestamps` marker:

  1. monotonic: no earlier than the latest timestamp of the device's lower seqs, less one 5-minute budget;
  2. no earlier than the device's first honoured admit, less 5 minutes (`join-request` and `announce` exempt);
  and a checked entry carries the one canonical timestamp shape.

Grandfathering is the marker's frozen tip per device, by seq: never a timestamp cutoff, so a backdated entry cannot
claim it. Without a marker nothing is skipped. Arrival times (`future-dated`) and flood counts (`flood`) are
detection only. The mutants at the end each drop one rule or weaken one anchor, and each is caught.
"""

import base64
import itertools
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owner_key, _device, _gov, _entry, _fact, _project  # noqa: E402
from test_links import _planes  # noqa: E402

DAY = "2026-01-01T"


def ts(h, m=0, s=0, ms=0):
    return f"{DAY}{h:02d}:{m:02d}:{s:02d}.{ms:03d}+00:00"


def _journal(hv, entries):
    jd = hv.JOURNAL_DIR
    jd.mkdir(parents=True, exist_ok=True)
    for f in jd.glob("*.jsonl"):
        f.unlink()
    (jd / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _hive(hv, marker_tips=None, extra=()):
    """Genesis owner, devices d0 (admitted 01:00) and d1 (admitted 01:00:30), and, when `marker_tips` is given, an
    owner-signed marker. `marker_tips(devs)` -> {node_id: seq}. Returns (owner, devs, base entries)."""
    oseed, opub, oid = _owner_key(hv)
    d0, d1 = _device(hv), _device(hv)
    base = [_gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, ts(0), 1),
            _gov(hv, {"action": "admit", "device_id": d0["id"], "principal": "p0"}, oseed, opub, ts(1), 2),
            _gov(hv, {"action": "admit", "device_id": d1["id"], "principal": "p1"}, oseed, opub, ts(1, 0, 30), 3)]
    return (oseed, opub), (d0, d1), base


def _marker(hv, owner, tips, seq=4, at=ts(2)):
    return _gov(hv, {"action": "freeze-timestamps", "tips": tips}, owner[0], owner[1], at, seq)


def _contents(conn):
    return {r[0] for r in conn.execute("SELECT content FROM facts")}


def _scenario(hv, tmp_path):
    """One journal that exercises every rule; returns the set of fact contents that project."""
    owner, (d0, d1), base = _hive(hv)
    f = lambda d, name, t: _fact(hv, d, name, t)            # noqa: E731
    # d0: seq 1-2 are the live-journal shape (a 13-minute step back) and sit under the tip
    old1, old2 = f(d0, "old1", ts(12, 58, 20)), f(d0, "old2", ts(12, 45, 3))
    # seq 3: 4 minutes behind the chain's latest (12:58:20) lands; seq 4: 8 minutes behind it, only 4 behind seq 3
    a3, a4 = f(d0, "step4", ts(12, 54, 20)), f(d0, "step8", ts(12, 50, 20))
    a5 = f(d0, "equal", ts(12, 54, 20))                      # equal to an earlier timestamp is fine
    a6 = f(d0, "ahead", ts(13, 0, 0))
    a7 = f(d0, "offset", "2026-01-01T08:30:00.000-05:00")    # 13:30 UTC, sorts before 13:00: refused for its shape
    # d1 (admitted 01:00:30): a join-request before the admit is exempt, content before it is not
    jr = _entry(hv, d1, "governance", {"action": "join-request", "device_id": d1["id"]}, ts(0, 30))
    early = f(d1, "pre-admit", ts(0, 54, 0))                 # 6 minutes before the admit
    near = f(d1, "near-admit", ts(0, 56, 0))                 # 4.5 minutes before it: within the budget
    # d1 and d0 entries dated far in the past but at seqs above the tip: a timestamp cannot claim the tip
    tips = {"ownerdev": 3, d0["id"]: 2}
    marker = _marker(hv, owner, tips)
    entries = base + [marker, old1, old2, a3, a4, a5, a6, a7, jr, early, near]
    _journal(hv, entries)
    conn = _project(hv, tmp_path, entries)
    return _contents(conn)


EXPECT = {"old1", "old2", "step4", "equal", "ahead", "near-admit"}


def test_the_rules_skip_exactly_the_entries_outside_the_bounds(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    assert _scenario(hv, tmp_path) == EXPECT


def test_without_a_marker_nothing_is_skipped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    es = [_fact(hv, d0, "a", ts(12, 0)), _fact(hv, d0, "b", ts(11, 0)), _fact(hv, d1, "c", ts(0, 10))]
    assert _contents(_project(hv, tmp_path, base + es)) == {"a", "b", "c"}


def test_the_live_shapes_under_the_tips_still_project(tmp_path, monkeypatch):
    """Two June backward steps of 13 and 11 minutes, naive lines, a join-request and the owner act: all under tips."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    es = [_fact(hv, d0, "j1", "2026-06-03T12:58:20"), _fact(hv, d0, "j2", "2026-06-03T12:45:03"),
          _fact(hv, d0, "j3", "2026-06-03T13:00:13"), _fact(hv, d0, "j4", "2026-06-03T12:49:05"),
          _entry(hv, d1, "governance", {"action": "join-request", "device_id": d1["id"]}, "2026-05-01T00:00:00")]
    tips = {"ownerdev": 3, d0["id"]: 4, d1["id"]: 1}
    assert _contents(_project(hv, tmp_path, base + [_marker(hv, owner, tips)] + es)) == {"j1", "j2", "j3", "j4"}


def test_a_backdated_entry_cannot_claim_the_tip(tmp_path, monkeypatch):
    """The tip is a seq. An entry above it is checked whatever its timestamp says."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    held = _fact(hv, d0, "held", ts(12))
    sneaky = _fact(hv, d0, "sneaky", "2025-06-01T00:00:00.000+00:00")
    marker = _marker(hv, owner, {"ownerdev": 3, d0["id"]: 1})
    assert _contents(_project(hv, tmp_path, base + [marker, held, sneaky])) == {"held"}


def test_a_marker_is_honoured_only_from_the_owner(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    stranger = _owner_key(hv)
    fake = _marker(hv, stranger, {"ownerdev": 3})
    es = [_fact(hv, d0, "a", ts(12)), _fact(hv, d0, "b", ts(11))]
    assert _contents(_project(hv, tmp_path, base + [fake] + es)) == {"a", "b"}        # nothing armed


def test_a_governance_act_outside_the_bounds_is_read_as_absent(tmp_path, monkeypatch):
    """A device-signed governance entry is bounded too: a join-request needs the canonical shape above the tip."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    odd = _entry(hv, d1, "governance", {"action": "join-request", "device_id": d1["id"]}, "2026-01-01T09:00:00Z")
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), odd])
    assert (odd["node_id"], odd["seq"]) in hv._ts_violations(
        hv.merkle.read_all_entries(hv.JOURNAL_DIR), hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR)))


# ── ingest ─────────────────────────────────────────────────────────────────────────────────────────────

def test_ingest_stores_a_step_back_the_projection_skips(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    first = _fact(hv, d0, "first", ts(12))
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), first])
    ok = _fact(hv, d0, "four-back", ts(11, 56))             # 4 minutes behind the latest
    bad = _fact(hv, d0, "eight-back", ts(11, 52))           # 4 behind `ok`, 8 behind the latest
    assert hv.append_foreign_entries([ok])[0] == 1
    assert hv.append_foreign_entries([bad])[0] == 1         # stored: only the projection applies the bounds
    assert _skipped(hv) == {(bad["node_id"], bad["seq"])}
    assert _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR))) == {"first", "four-back"}
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), first])
    assert hv.append_foreign_entries([bad, ok])[0] == 2     # and the batch order changes nothing


def test_ingest_stores_an_equal_timestamp_and_a_non_canonical_shape_the_projection_skips(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    first = _fact(hv, d0, "first", ts(12))
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), first])
    same = _fact(hv, d0, "same", ts(12))
    offset = _fact(hv, d0, "offset", "2026-01-01T07:30:00.000-05:00")      # 12:30 UTC, sorts before 12:00
    z = _fact(hv, d0, "z", "2026-01-01T13:00:00Z")
    assert hv.append_foreign_entries([same, offset, z])[0] == 3
    assert _skipped(hv) == {(offset["node_id"], offset["seq"]), (z["node_id"], z["seq"])}


def test_a_pre_admit_content_entry_is_stored_and_skipped_and_a_join_request_is_honoured(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3})])
    jr = _entry(hv, d1, "governance", {"action": "join-request", "device_id": d1["id"]}, ts(0, 20))
    early = _fact(hv, d1, "early", ts(0, 54))                # 6 minutes before the admit
    ok = _fact(hv, d1, "ok", ts(0, 56))
    assert [hv.append_foreign_entries([e])[0] for e in (jr, early, ok)] == [1, 1, 1]
    assert _skipped(hv) == {(d1["id"], 2)}


def test_ingest_checks_nothing_until_the_marker_arms_it(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    _journal(hv, base)
    es = [_fact(hv, d0, "a", ts(12)), _fact(hv, d0, "b", ts(11)), _fact(hv, d0, "c", "2026-01-01T07:00:00-05:00")]
    assert hv.append_foreign_entries(es)[0] == 3


# ── detection only: arrival times and floods ─────────────────────────────────────────────────────────

def test_a_future_dated_arrival_is_flagged_with_its_signer_and_an_on_time_one_is_not(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    _journal(hv, base)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)                       # HIVE_NOW, the arrival clock
    iso = lambda dt: dt.isoformat(timespec="milliseconds")                 # noqa: E731
    future = _fact(hv, d0, "future", iso(now + timedelta(minutes=25)))
    on_time = _fact(hv, d1, "on-time", iso(now - timedelta(minutes=1)))
    slightly = _fact(hv, d1, "slightly", iso(now + timedelta(minutes=9)))   # under the 10-minute default
    assert hv.append_foreign_entries([future, on_time, slightly])[0] == 3   # nothing is refused
    entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    found = hv._future_dated(entries)
    assert set(found) == {d0["id"]} and found[d0["id"]] == {"count": 1, "worst_minutes": 25.0}
    check = {c["name"]: c for c in hv._doctor_status()}["future-dated"]["detail"]
    assert d0["id"] in check and "principal p0" in check and "25" in check
    hv.init_db()
    audit = hv._compute_audit()["timestamps"]
    assert audit and audit[0][0] == "future-dated" and audit[0][1] == d0["id"]


def test_the_arrival_record_is_local_and_never_changes_the_projection(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    _journal(hv, base)
    f = _fact(hv, d0, "late", "2026-01-10T01:00:00.000+00:00")
    hv.append_foreign_entries([f])
    assert (tmp_path / ".arrivals.jsonl").exists()
    with_record = _contents(_project(hv, tmp_path, base + [f]))
    (tmp_path / ".arrivals.jsonl").unlink()
    assert _contents(_project(hv, tmp_path, base + [f])) == with_record == {"late"}


def _flood_hive(hv, per_minute=None):
    owner, (d0, d1), base = _hive(hv)
    if per_minute is not None:
        base.append(_gov(hv, {"action": "set-config", "key": "flood_per_minute", "value": per_minute},
                         owner[0], owner[1], ts(2), 4))
    return base, d0, d1


def test_a_device_over_the_owner_signed_minute_threshold_is_flagged_and_one_under_is_not(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    base, d0, d1 = _flood_hive(hv, per_minute=3)
    es = [_fact(hv, d0, f"f{i}", ts(12, 0, i)) for i in range(5)] + [_fact(hv, d1, f"g{i}", ts(12, 0, i)) for i in range(3)]
    conn = _project(hv, tmp_path, base + es)
    assert len(_contents(conn)) == 8                                          # nothing is refused or skipped
    entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    gov = hv._governance_state(entries)
    assert gov["config"]["flood_per_minute"] == 3
    assert hv._floods(entries, gov) == [{"node_id": d0["id"], "per": "minute", "count": 5, "limit": 3}]


def test_the_threshold_comes_from_owner_signed_config_and_defaults_to_the_live_bound(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    base, d0, d1 = _flood_hive(hv)                                            # no set-config: the 120 / 2000 defaults
    es = [_fact(hv, d0, f"f{i}", ts(12, 0, i % 60)) for i in range(30)]
    _project(hv, tmp_path, base + es)
    entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    gov = hv._governance_state(entries)
    assert (gov["config"]["flood_per_minute"], gov["config"]["flood_per_day"]) == (120, 2000)
    assert hv._floods(entries, gov) == []
    # an unsigned set-config does not move the threshold
    forged = {"node_id": "ownerdev", "seq": 9, "type": "governance", "timestamp": ts(3),
              "payload": {"action": "set-config", "key": "flood_per_minute", "value": 1}}
    assert hv._governance_state(entries + [forged])["config"]["flood_per_minute"] == 120


def test_a_device_over_the_day_threshold_is_flagged(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    base.append(_gov(hv, {"action": "set-config", "key": "flood_per_day", "value": 4}, owner[0], owner[1], ts(2), 4))
    es = [_fact(hv, d0, f"f{i}", ts(10 + i, 0, 0)) for i in range(6)]
    _project(hv, tmp_path, base + es)
    entries = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    assert hv._floods(entries, hv._governance_state(entries)) == [
        {"node_id": d0["id"], "per": "day", "count": 6, "limit": 4}]
    assert "flood" in {c["name"] for c in hv._doctor_status()}


# ── the mutants: each drops a rule or weakens an anchor, and the scenario catches it ─────────────────

def _run_mutant(tmp_path, monkeypatch, patch):
    hv = _loadhv(tmp_path, monkeypatch)
    patch(hv)
    return _scenario(hv, tmp_path)


def _drop_rule_1(hv):
    orig = hv._ts_problem
    hv._ts_problem = lambda e, floor, prior_max: orig(e, floor, None)


def _drop_rule_2(hv):
    orig = hv._ts_problem
    hv._ts_problem = lambda e, floor, prior_max: orig(e, None, prior_max)


def _drop_the_shape_rule(hv):
    hv._CANONICAL_TS_RE = hv._TS_PARTS_RE


def _slack_against_seq_minus_one_only(hv):
    hv._ts_chain_max = lambda prior: prior[-1] if prior else None


def _grandfather_by_timestamp(hv):
    hv._ts_grandfathered = lambda e, tips: e.get("timestamp", "") < "2026-01-01T00:30:00"


@pytest.mark.parametrize("mutant", [_drop_rule_1, _drop_rule_2, _drop_the_shape_rule,
                                    _slack_against_seq_minus_one_only, _grandfather_by_timestamp],
                         ids=lambda m: m.__name__.strip("_"))
def test_each_mutant_is_caught(tmp_path, monkeypatch, mutant):
    assert _run_mutant(tmp_path, monkeypatch, mutant) != EXPECT


def test_a_mutant_that_grandfathers_by_timestamp_lets_a_backdated_entry_in(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _grandfather_by_timestamp(hv)
    owner, (d0, d1), base = _hive(hv)
    held = _fact(hv, d0, "held", ts(12))
    sneaky = _fact(hv, d0, "sneaky", ts(0, 10))
    marker = _marker(hv, owner, {"ownerdev": 3, d0["id"]: 1})
    assert "sneaky" in _contents(_project(hv, tmp_path, base + [marker, held, sneaky]))


# ── ingest and projection agree, whatever the batch split (#229 review) ─────────────────────────────

def _held(hv):
    return {(e["node_id"], e["seq"]) for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}


def _skipped(hv):
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    return set(hv._ts_violations(j, hv._governance_state(j)))


def test_the_journal_is_the_same_for_every_split_of_a_batch_and_the_projection_skips_the_same_entries(tmp_path, monkeypatch):
    """Ingest refuses nothing for a timestamp: every contiguous split of a run stores all of it, and the projection
    skips the announce (12:10 behind s2's 12:20), s4 and s5."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    run = [_fact(hv, d0, "s2", ts(12, 20)),
           _entry(hv, d0, "governance", {"action": "announce", "kind": "key", "device_id": d0["id"]}, ts(12, 10)),
           _fact(hv, d0, "s4", ts(12, 14)),
           _fact(hv, d0, "s5", "2026-01-01T13:00:00Z"),
           _fact(hv, d0, "s6", ts(12, 40))]
    n = len(run)
    for cuts in range(1 << (n - 1)):                         # every composition of the run into batches
        _journal(hv, base + [marker, s1])
        batch = [run[0]]
        for i in range(1, n):
            if cuts >> (i - 1) & 1:
                hv.append_foreign_entries(batch)
                batch = []
            batch.append(run[i])
        hv.append_foreign_entries(batch)
        assert len(_held(hv)) == 4 + 1 + n, bin(cuts)
        assert len(_skipped(hv)) == 3, bin(cuts)


# ── the bounds apply in the projection only; owner-signed governance beats a device-stamped entry (#230, #250) ──
# David's rulings of 2026-10-06 (h:e3a8401cdb, h:4c86fdea7a). Each case is offered one entry per batch and as one
# batch, in every order, each followed by a re-offer of the full set. Every run must end with the same honoured set,
# admitted set, projected facts AND the same raw journal, and the owner act must be the one that wins.

def _outcome(hv, tmp_path):
    """What the projection makes of what a node holds: the entries it honours (the bounds skip none of them and, for
    content, its device is admitted), who is admitted, the facts, and the root
    over the raw journal (the entries sorted by (node_id, seq), as the chunks a sync compares)."""
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    gov = hv._governance_state(j)
    root = hv.merkle.hash_entries(sorted(j, key=lambda e: (e["node_id"], e["seq"])))
    honoured = frozenset((e["node_id"], e["seq"]) for e in hv._admitted_content(hv._ts_unskipped(j, gov), gov))
    return (honoured, frozenset(gov["admitted"]), frozenset(_contents(_project(hv, tmp_path, j))), root, gov["owner_id"])


def _runs(hv, tmp_path, base, entries):
    """Every run of `entries` (one per batch and one batch, in every order, then a re-offer of the full set); the
    outcomes that differ between runs."""
    outcomes = []
    plans = []
    for perm in itertools.permutations(entries):
        n = len(perm)
        # every composition of the run into batches when it is small; one per batch and one batch otherwise
        for cuts in (range(1 << (n - 1)) if n <= 4 else (0, (1 << (n - 1)) - 1)):
            plan, cur = [], [perm[0]]
            for i in range(1, n):
                if cuts >> (i - 1) & 1:
                    plan.append(cur)
                    cur = []
                cur.append(perm[i])
            plans.append(plan + [cur])
    for plan in plans:
        _journal(hv, base)
        for batch in plan:
            hv.append_foreign_entries(batch)
        hv.append_foreign_entries(list(entries))
        o = _outcome(hv, tmp_path)
        if o not in outcomes:
            outcomes.append(o)
    return outcomes


def _own_admit(hv, x, owner, at):
    """An owner-signed (re-)admit of X carried on X's own chain. X is already admitted when it is read (SECREV A3: an
    owner-signed act counts only from a member, so a device cannot admit itself)."""
    return _entry(hv, x, "governance", {"action": "admit", "device_id": x["id"], "principal": "px"}, at, owner=owner)


def _admitted_x(hv, owner, base, x):
    return base + [_gov(hv, {"action": "admit", "device_id": x["id"], "principal": "px"}, owner[0], owner[1], ts(1, 1), 4),
                   _marker(hv, owner, {"ownerdev": 4}, seq=5)]


def _case_pair(hv):
    """#230: X, not admitted, holds a fact at 12:20 (seq 1) and an owner-signed admit of itself at 12:10 (seq 2): the
    admit is honoured and the fact is skipped."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    fact, admit = _fact(hv, x, "x content", ts(12, 20)), _own_admit(hv, x, owner, ts(12, 10))
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] in o[1] and not o[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [fact, admit], k


def _case_reviewers_probe(hv):
    """X is admitted by its own owner-signed admit (seq 10, 12:10); its fact at seq 5 (12:20) pushes past the owner acts
    behind it, and X's announce, admit of Y and set-config sit at seqs 6-8 (12:08). Y has content too."""
    owner, (d0, d1), base = _hive(hv)
    x, y = _device(hv), _device(hv)
    x["seq"] = 9
    own = _own_admit(hv, x, owner, ts(12, 10))
    x["seq"] = 4
    fact = _fact(hv, x, "x content", ts(12, 20))
    ann = _entry(hv, x, "governance", {"action": "announce", "kind": "key", "pub": x["pub"].hex()}, ts(12, 8))
    admit_y = _entry(hv, x, "governance", {"action": "admit", "device_id": y["id"], "principal": "py"}, ts(12, 8),
                     owner=owner)
    cfg = _entry(hv, x, "governance", {"action": "set-config", "key": "forget_writers", "value": "owner"},
                 ts(12, 8), owner=owner)
    k = lambda o: ((x["id"], 5) not in o[0] and {(x["id"], i) for i in (6, 7, 8, 10)} <= o[0]      # noqa: E731
                   and {x["id"], y["id"]} <= o[1] and o[2] == {"y content"})
    return _admitted_x(hv, owner, base, x), [own, fact, ann, admit_y, cfg, _fact(hv, y, "y content", ts(12, 30))], k


def _case_verifiers_probe_1(hv):
    """X's own admit at seq 10 (12:10), a fact at seq 5 (12:20), and a fact at seq 11 (12:12) behind the admit."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    x["seq"] = 9
    own = _own_admit(hv, x, owner, ts(12, 10))
    x["seq"] = 4
    f5 = _fact(hv, x, "x five", ts(12, 20))
    x["seq"] = 10
    k = lambda o: ((x["id"], 5) not in o[0] and (x["id"], 10) in o[0] and (x["id"], 11) in o[0]      # noqa: E731
                   and x["id"] in o[1] and o[2] == {"x eleven"})
    return _admitted_x(hv, owner, base, x), [own, f5, _fact(hv, x, "x eleven", ts(12, 12))], k


def _case_250_own_revoke(hv):
    """#250 case 1: X is admitted. Its fact at seq 1 (12:20) and an owner-signed revoke of X at seq 2 (12:10): the
    revoke is honoured, X is not admitted and its fact does not project."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    entries = [_fact(hv, x, "x content", ts(12, 20)),
               _entry(hv, x, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner)]
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] not in o[1] and not o[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), entries, k


def _case_250_own_revoke_three(hv):
    """The verifier's variant: fact1 12:20, revoke2 12:10, fact3 12:25. The revoke is honoured, X is not admitted, and
    neither fact projects, whether the fact at seq 3 reached this node before the revoke or after it."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    entries = [_fact(hv, x, "x one", ts(12, 20)),
               _entry(hv, x, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner),
               _fact(hv, x, "x three", ts(12, 25))]
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] not in o[1] and not o[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), entries, k


def _case_250_admit_of_y(hv, with_y_content=False):
    """#250 case 2: X is admitted. Its fact at seq 1 (12:20) and an owner-signed admit of Y on its chain at seq 2
    (12:10): Y's admit is honoured, Y is admitted, and with Y's content Y's fact projects."""
    owner, (d0, d1), base = _hive(hv)
    x, y = _device(hv), _device(hv)
    entries = [_fact(hv, x, "x content", ts(12, 20)),
               _entry(hv, x, "governance", {"action": "admit", "device_id": y["id"], "principal": "py"}, ts(12, 10),
                      owner=owner)]
    if with_y_content:
        entries.append(_fact(hv, y, "y content", ts(12, 30)))
    k = lambda o: ((x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and y["id"] in o[1]      # noqa: E731
                   and o[2] == ({"y content"} if with_y_content else set()))
    return _admitted_x(hv, owner, base, x), entries, k


def _case_250_admit_of_y_with_content(hv):
    return _case_250_admit_of_y(hv, with_y_content=True)


def _case_announce_and_join_request(hv):
    """An announce at seq 1 (12:30) and a join-request at seq 2 (12:40) are device-stamped too: the owner admit of X at
    seq 4 (12:10) is honoured and both are skipped."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    ann = _entry(hv, x, "governance", {"action": "announce", "kind": "key", "pub": x["pub"].hex()}, ts(12, 30))
    jr = _entry(hv, x, "governance", {"action": "join-request", "device_id": x["id"]}, ts(12, 40))
    x["seq"] = 3
    admit = _own_admit(hv, x, owner, ts(12, 10))
    k = lambda o: (x["id"], 4) in o[0] and not {(x["id"], 1), (x["id"], 2)} & o[0] and x["id"] in o[1]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [ann, jr, admit], k


def _case_throwaway_owner_act(hv):
    """The verifier's #1: X signs a `set-config` with a throwaway owner key embedded in its own payload at seq 1 (12:20).
    It is not the owner in authority, so it is device-stamped: it does not shield itself, and the real owner's revoke of X
    at seq 2 (12:10) is honoured, so X is not admitted and neither of its facts projects."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    rogue = _owner_key(hv)
    forged = _entry(hv, x, "governance", {"action": "set-config", "key": "forget_writers", "value": "owner"},
                    ts(12, 20), owner=(rogue[0], rogue[1]))
    revoke = _entry(hv, x, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner)
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] not in o[1] and not o[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [forged, revoke, _fact(hv, x, "x three", ts(12, 25))], k


def _case_throwaway_owner_act_from_a_member(hv):
    """The same forgery while X is still a member when it is read (nothing revokes X): a `set-config` signed with a throwaway
    key at seq 1 (12:20) must not shield itself against the owner's own `set-config` on X's chain at seq 2 (12:10)."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    rogue = _owner_key(hv)
    forged = _entry(hv, x, "governance", {"action": "set-config", "key": "forget_writers", "value": "owner"},
                    ts(12, 20), owner=(rogue[0], rogue[1]))
    real = _entry(hv, x, "governance", {"action": "set-config", "key": "cap_self", "value": 0.4}, ts(12, 10), owner=owner)
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] in o[1]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [forged, real], k


def _case_throwaway_owner_admit_of_itself(hv):
    """The verifier's #1, second form: X signs an `admit` of itself with a throwaway owner key at seq 1 (12:20). It is not
    the owner in authority, so it admits nothing and moves nothing for the owner's revoke of X (seq 2, 12:10)."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    rogue = _owner_key(hv)
    forged = _entry(hv, x, "governance", {"action": "admit", "device_id": x["id"], "principal": "px"},
                    ts(12, 20), owner=(rogue[0], rogue[1]))
    revoke = _entry(hv, x, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner)
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] not in o[1] and not o[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [forged, revoke, _fact(hv, x, "x three", ts(12, 25))], k


def _case_unverified_owner_sig_key(hv):
    """The verifier's #2: the owner's revoke of X is relayed on d0 (12:10) and X has a fact at 12:15 whose payload merely
    carries an `owner_sig` key. The key proves nothing, so the fact is X's content and does not project, whichever of
    the two a node held first."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    revoke = _entry(hv, d0, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner)
    fact = _entry(hv, x, "fact", {"content": "x content", "tags": [], "importance": 0.5, "source": "manual",
                                  "owner_sig": "AAAA"}, ts(12, 15))
    k = lambda o: (d0["id"], 1) in o[0] and (x["id"], 1) not in o[0] and x["id"] not in o[1] and not o[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [fact, revoke], k


def _case_transfer_beats_a_device_fact(hv):
    """The verifier's transfer probe: X is admitted and holds a fact at seq 1 (12:20); owner A signs a `transfer` to B on
    X at seq 2 (12:10). The old owner signs it and the walk records B at that position, so it is owner-signed: it wins
    the tiebreak, B owns the hive and the fact does not project."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    b = _owner_key(hv)
    fact = _fact(hv, x, "x content", ts(12, 20))
    xfer = _entry(hv, x, "governance", {"action": "transfer", "new_owner_pub": base64.b64encode(b[1]).decode()}, ts(12, 10), owner=owner)
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and not o[2] and o[4] == b[2]   # noqa: E731
    return _admitted_x(hv, owner, base, x), [fact, xfer], k


def _case_never_admitted_then_revoked(hv):
    """The verifier's non-blocking item, from an empty base: Z was never admitted and wrote a fact stamped 2025-01-01, before
    the genesis owner; the owner revokes Z (12:10), relayed on d0. The pre-genesis stay ends with the honoured revoke, so
    Z's fact never projects, in every order. (Without the revoke the stay splits on arrival order: #255.)"""
    owner, (d0, d1), base = _hive(hv)
    z = _device(hv)
    fact = _fact(hv, z, "bootstrap", "2025-01-01T00:00:00.000+00:00")
    revoke = _entry(hv, d0, "governance", {"action": "revoke", "device_id": z["id"]}, ts(12, 10), owner=owner)
    k = lambda o: (z["id"], 1) not in o[0] and z["id"] not in o[1] and not o[2]   # noqa: E731
    return [], [fact, base[0], base[1], revoke], k


def _case_backdated_after_revoke(hv):
    """The reviewer's #3, verifier's form: no freeze marker. X is admitted, then the owner revokes it (seq 2, 12:10); its
    fact at seq 1 is stamped 2025-01-01, which puts it before the genesis owner in the journal's order. The stamp is the
    writer's, so the pre-genesis stay does not apply to a device with an honoured revoke: the fact does not project,
    whichever of the two a node held first."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    entries = [_fact(hv, x, "x old", "2025-01-01T00:00:00.000+00:00"),
               _entry(hv, x, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner)]
    k = lambda o: (x["id"], 2) in o[0] and (x["id"], 1) not in o[0] and x["id"] not in o[1] and not o[2]   # noqa: E731
    admit = _gov(hv, {"action": "admit", "device_id": x["id"], "principal": "px"}, owner[0], owner[1], ts(1, 1), 4)
    return base + [admit], entries, k


_CASES = [_case_backdated_after_revoke, _case_pair, _case_reviewers_probe, _case_verifiers_probe_1, _case_250_own_revoke, _case_250_own_revoke_three,
          _case_250_admit_of_y, _case_250_admit_of_y_with_content, _case_announce_and_join_request, _case_throwaway_owner_act, _case_throwaway_owner_act_from_a_member,
          _case_throwaway_owner_admit_of_itself, _case_unverified_owner_sig_key, _case_transfer_beats_a_device_fact, _case_never_admitted_then_revoked]


# The two revoke cases end with the same projection but not the same journal, by the admission gate that stays as it was:
# a fact of X that arrives after the revoke is held finds X unadmitted and is refused, and one that arrived before it
# was stored while X was admitted (the never-admitted case likewise: a refused Z fact never lands). The revoke race, left to #255; every other case ends with one root.
_JOURNALS_DIFFER = ("_case_backdated_after_revoke", "_case_250_own_revoke", "_case_250_own_revoke_three", "_case_throwaway_owner_act",
                    "_case_throwaway_owner_admit_of_itself", "_case_unverified_owner_sig_key", "_case_never_admitted_then_revoked")


def _case_holds(hv, tmp_path, case):
    """Whether every run of `case` ends with the same honoured set, admitted set and facts (and the same journal,
    except where the admission gate decides it) and the owner act wins."""
    base, entries, wins = case(hv)
    outcomes = _runs(hv, tmp_path, base, entries)
    if case.__name__ in _JOURNALS_DIFFER:
        outcomes = {o[:3] + o[4:] for o in outcomes}
        o = next(iter(outcomes))
        return len(outcomes) == 1 and wins(o[:3] + (None,) + o[3:])
    return len(outcomes) == 1 and wins(outcomes[0])


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.__name__.strip("_"))
def test_every_order_ends_the_same_and_the_owner_act_wins(tmp_path, monkeypatch, case):
    assert _case_holds(_loadhv(tmp_path, monkeypatch), tmp_path, case)


def _bootstrap_fact_journal(hv, z, extra):
    """A bootstrap hive: Z wrote a fact before any owner existed (stamped 2025, so before the genesis owner), then the
    owner declared genesis. Z was never admitted."""
    owner, (d0, d1), base = _hive(hv)
    fact = _fact(hv, z, "bootstrap", "2025-01-01T00:00:00.000+00:00")
    _journal(hv, [fact] + base + extra(owner, d0))
    gov = hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    return z["id"] in {e["node_id"] for e in hv._admitted_content(hv._ts_unskipped(j, gov), gov)}, owner


def test_a_bootstrap_writer_stays_until_the_owner_revokes_it(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    z = _device(hv)
    assert _bootstrap_fact_journal(hv, z, lambda owner, d0: [])[0]
    revoke = lambda owner, d0: [_entry(hv, d0, "governance", {"action": "revoke", "device_id": z["id"]},   # noqa: E731
                                   ts(12, 10), owner=owner)]
    assert not _bootstrap_fact_journal(hv, z, revoke)[0]       # revoked, never admitted: the exemption ends
    purge = lambda owner, d0: [_entry(hv, d0, "governance", {"action": "purge", "device_id": z["id"]},     # noqa: E731
                                  ts(12, 10), owner=owner)]
    assert not _bootstrap_fact_journal(hv, z, purge)[0]


def _genesis_device_projects(hv, *acts):
    """Whether a fact of the genesis device projects after the owner's `acts` (purge, revoke) against it."""
    owner, (d0, d1), base = _hive(hv)
    fact = {"node_id": "ownerdev", "seq": 9, "type": "fact", "timestamp": ts(12),
            "payload": {"content": "owner device", "tags": [], "importance": 0.5, "source": "manual"}}
    _journal(hv, base + [fact] + [_entry(hv, d0, "governance", {"action": a, "device_id": "ownerdev"}, ts(12, 10),
                                          owner=owner) for a in acts])
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    gov = hv._governance_state(j)
    return ("ownerdev", 9) in {(e["node_id"], e["seq"]) for e in hv._admitted_content(hv._ts_unskipped(j, gov), gov)}


def test_the_genesis_device_is_removed_by_a_purge_or_a_revoke(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    assert _genesis_device_projects(hv)
    assert not _genesis_device_projects(hv, "purge")
    assert not _genesis_device_projects(hv, "revoke")


def test_a_genesis_device_that_survives_a_purge_is_caught(tmp_path, monkeypatch):
    hv = _mutated_hv(tmp_path, monkeypatch, '    ok = ok - gov.get("purged", set())          # the genesis device',
                     '    ok = ok | ({gen} if gen is not None else set())          # the genesis device')
    assert _genesis_device_projects(hv, "purge")


def test_a_second_marker_stamped_behind_the_first_is_exempt(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3})])
    hv.append_foreign_entries([_marker(hv, owner, {"ownerdev": 5}, seq=5, at=ts(1, 40))])
    assert ("ownerdev", 5) in _held(hv) - _skipped(hv)


def test_an_unadmitted_device_still_cannot_land_content(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    stranger = _device(hv)
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3})])
    assert hv.append_foreign_entries([_fact(hv, stranger, "stranger", ts(12))])[0] == 0


def _mutated_hv(tmp_path, monkeypatch, old, new):
    src = (PROJECT / "hv").read_text()
    assert src.count(old) == 1, old
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    monkeypatch.setenv("HIVE_NOW", "2026-01-10T00:00:00Z")
    m = types.ModuleType("hvmod_mutant")
    m.__file__ = str(PROJECT / "hv")
    exec(compile(src.replace(old, new), str(PROJECT / "hv"), "exec"), m.__dict__)
    _planes.install_control_plane(m)
    return m


_REFUSE_ON_THE_BOUNDS = ("held = merkle.read_all_entries(JOURNAL_DIR)\n"
                         "        if (e[\"node_id\"], e[\"seq\"]) in _ts_violations(held + [e], _governance_state(held + [e])):\n"
                         "            return\n"
                         "        _append_line(_journal_path_for(e[\"timestamp\"]), json.dumps(e))")

_MUTANTS = {
    "owner_signed_without_checking_the_owner": (
        "    return oid is not None and oid in (_owner_at(tl, pos), _owner_at([t for t in tl if t[:3] < pos], pos))\n",
        "    return oid is not None\n"),
    "owner_signed_ignores_the_owner_before_a_transfer": (
        "    return oid is not None and oid in (_owner_at(tl, pos), _owner_at([t for t in tl if t[:3] < pos], pos))\n",
        "    return oid is not None and oid == _owner_at(tl, pos)\n"),
    "owner_sig_key_exempts_content": (
        '''                and _is_authorized_writer(None, (e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0)),
                                          e["payload"], gov, "owner"))                  # verified, as of its position''',
        '''                and "owner_sig" in e["payload"])'''),
    "pre_genesis_stay_ignores_a_revoke": (
        '            or (e.get("node_id") not in ended\n                and _owner_at(',
        '            or (True\n                and _owner_at('),
    "admitted_gate_removed_from_content_projection": (
        '    if gov.get("owner_id") is None:\n        return entries\n    gen = ',
        '    if True:\n        return entries\n    gen = '),
    "ingest_still_refuses_on_the_bounds": (
        '_append_line(_journal_path_for(e["timestamp"]), json.dumps(e))   # validated above in both passes',
        _REFUSE_ON_THE_BOUNDS),
    "skipped_device_stamped_entry_still_moves_prior": (
        '                    out[(nid, e["seq"])] = why\n                    continue',
        '                    out[(nid, e["seq"])] = why\n                    if not why.startswith("timestamp would"):\n                        continue'),
}


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_each_mutant_of_the_tiebreak_is_caught(tmp_path, monkeypatch, name):
    hv = _mutated_hv(tmp_path, monkeypatch, *_MUTANTS[name])
    assert not all(_case_holds(hv, tmp_path, case) for case in _CASES), name


def test_a_marker_the_projection_does_not_honour_is_checked_like_any_entry(tmp_path, monkeypatch):
    """A `freeze-timestamps` signed by a key that is not the current owner arms nothing and is not exempt."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    rogue = _owner_key(hv)
    fake = _entry(hv, d0, "governance", {"action": "freeze-timestamps", "tips": {}}, "2026-01-01T09:00:00Z",
                  owner=(rogue[0], rogue[1]))
    j = base + [marker, fake]
    gov = hv._governance_state(j)
    assert (d0["id"], 1) not in gov["ts_markers"] and ("ownerdev", 4) in gov["ts_markers"]
    assert (d0["id"], 1) in hv._ts_violations(j, gov)
    _journal(hv, base + [marker])
    assert hv.append_foreign_entries([fake])[:2] == (1, 0)      # stored (ingest refuses nothing for a timestamp) ...
    assert (d0["id"], 1) in _skipped(hv)                        # ... and the projection skips it


def test_a_batch_that_carries_the_marker_stores_everything(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1, s2 = _fact(hv, d0, "s1", ts(12)), _fact(hv, d0, "s2", ts(11))
    _journal(hv, base)
    hv.append_foreign_entries([s1, s2, marker])
    assert _held(hv) == {("ownerdev", i) for i in (1, 2, 3, 4)} | {(d0["id"], 1), (d0["id"], 2)}
    assert _skipped(hv) == {(d0["id"], 2)}


# ── a device's own clock stepping back is clamped, not refused ──────────────────────────────────────

def test_a_clock_stepped_back_is_clamped_and_the_fact_still_lands_everywhere(tmp_path, monkeypatch, capsys):
    import base64
    hv = _loadhv(tmp_path / "local", monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    first = _fact(hv, d0, "first", ts(12, 30))
    _journal(hv, base + [marker, first])
    hv.NODE_ID = d0["id"]
    hv.DEVICE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    hv.DEVICE_KEY_PATH.write_text(base64.b64encode(d0["seed"]).decode())
    monkeypatch.setattr(hv, "_now_iso", lambda: ts(12, 20))              # the clock stepped back 10 minutes
    e = hv.append_journal("fact", {"content": "after", "tags": [], "importance": 0.5, "source": "manual"})
    assert e["timestamp"] == ts(12, 30)
    assert "10 min behind" in capsys.readouterr().err
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    assert "after" in _contents(_project(hv, tmp_path / "local", j))     # projects here
    peer = _loadhv(tmp_path / "peer", monkeypatch)
    _journal(peer, base + [marker, first])
    assert peer.append_foreign_entries([e])[:2] == (1, 0)                # and passes a peer's ingest


def test_a_small_step_back_is_clamped_quietly_and_a_forward_clock_is_untouched(tmp_path, monkeypatch, capsys):
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._clamp_timestamp(ts(12, 28), ts(12, 30)) == ts(12, 30)
    assert capsys.readouterr().err == ""                                 # within the tolerance: no noise
    assert hv._clamp_timestamp(ts(12, 31), ts(12, 30)) == ts(12, 31)
    assert hv._clamp_timestamp(ts(12, 30), ts(12, 30)) == ts(12, 30)
    assert hv._clamp_timestamp(ts(12, 31), None) == ts(12, 31)
    assert hv._clamp_timestamp(ts(12, 20), "2026-01-01T12:30:00Z") == ts(12, 30)   # an old naive/Z shape is rewritten canonical


def test_a_clock_behind_the_chain_max_is_clamped_to_the_max_not_the_previous_entry(tmp_path, monkeypatch, capsys):
    """The live-journal shape: the last grandfathered entry sits 13 minutes behind an earlier one. Rule 1 measures
    against the max, so clamping to the previous entry would still be refused by peers."""
    import base64
    hv = _loadhv(tmp_path / "local", monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    old1, old2 = _fact(hv, d0, "old1", ts(12, 58, 20)), _fact(hv, d0, "old2", ts(12, 45, 3))
    marker = _marker(hv, owner, {"ownerdev": 3, d0["id"]: 2})
    _journal(hv, base + [old1, old2, marker])
    hv.NODE_ID = d0["id"]
    hv.DEVICE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    hv.DEVICE_KEY_PATH.write_text(base64.b64encode(d0["seed"]).decode())
    monkeypatch.setattr(hv, "_now_iso", lambda: ts(12, 40))
    e = hv.append_journal("fact", {"content": "after", "tags": [], "importance": 0.5, "source": "manual"})
    assert e["timestamp"] == ts(12, 58, 20)
    peer = _loadhv(tmp_path / "peer", monkeypatch)
    _journal(peer, base + [old1, old2, marker])
    assert peer.append_foreign_entries([e])[:2] == (1, 0)


def _coarrival(hv, n_facts=30):
    """#284: X is admitted. Its own-chain revoke sits at seq 2 and `n_facts` facts at seq 3.. ride in the same call."""
    owner, (d0, d1), base = _hive(hv)
    x = _device(hv)
    held = _admitted_x(hv, owner, base, x)
    x["seq"] = 1
    revoke = _entry(hv, x, "governance", {"action": "revoke", "device_id": x["id"]}, ts(12, 10), owner=owner)
    facts = [_fact(hv, x, f"x fact {i}", ts(12, 20, i)) for i in range(n_facts)]
    return held, x, revoke, facts


def _stored(hv, x):
    return {e["seq"] for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR) if e["node_id"] == x["id"]}


@pytest.mark.parametrize("reverse", [False, True], ids=["revoke-first", "facts-first"])
def test_a_revoke_in_the_call_is_read_before_the_facts_behind_it(tmp_path, monkeypatch, reverse):
    """The call stores what delivering the same entries one by one, in chain order, stores: the revoke, and none of
    the facts it makes inadmissible. The input order of the call does not matter."""
    hv = _loadhv(tmp_path / "one", monkeypatch)
    held, x, revoke, facts = _coarrival(hv)
    _journal(hv, held)
    for e in [revoke] + facts:
        hv.append_foreign_entries([e])
    one_by_one = _stored(hv, x)
    assert one_by_one == {2}

    batch = [revoke] + facts
    _journal(hv, held)
    hv.append_foreign_entries(batch[::-1] if reverse else batch)
    assert _stored(hv, x) == one_by_one
    assert x["id"] not in hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))["admitted"]


def test_no_fact_from_a_revoked_co_arrival_projects(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    held, x, revoke, facts = _coarrival(hv)
    _journal(hv, held)
    hv.append_foreign_entries([revoke] + facts)
    j = hv.merkle.read_all_entries(hv.JOURNAL_DIR)
    assert _contents(_project(hv, tmp_path, j)) == set()
