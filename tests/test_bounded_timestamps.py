"""#217: bounded entry timestamps.

Journal order is (timestamp, node_id, seq) and the writer picks the timestamp, so two rules bound it per device,
at ingest and in the projection alike, once the owner signs a `freeze-timestamps` marker:

  1. monotonic: no earlier than the latest timestamp of the device's lower seqs, less one 5-minute budget;
  2. no earlier than the device's first honoured admit, less 5 minutes (`join-request` and `announce` exempt);
  and a checked entry carries the one canonical timestamp shape.

Grandfathering is the marker's frozen tip per device, by seq: never a timestamp cutoff, so a backdated entry cannot
claim it. Without a marker nothing is skipped. Arrival times (`future-dated`) and flood counts (`flood`) are
detection only. The mutants at the end each drop one rule or weaken one anchor, and each is caught.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owner_key, _device, _gov, _entry, _fact, _project  # noqa: E402

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

def test_ingest_lands_a_step_within_the_budget_and_refuses_the_next_that_walks_back(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    first = _fact(hv, d0, "first", ts(12))
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), first])
    ok = _fact(hv, d0, "four-back", ts(11, 56))             # 4 minutes behind the latest
    bad = _fact(hv, d0, "eight-back", ts(11, 52))           # 4 behind `ok`, 8 behind the latest
    assert hv.append_foreign_entries([ok])[0] == 1
    assert hv.append_foreign_entries([bad])[0] == 0
    kept = {(e["node_id"], e["seq"]) for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}
    assert (bad["node_id"], bad["seq"]) not in kept
    # the same two in one batch, in reverse order: the verdict follows the chain, not the batch
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), first])
    assert hv.append_foreign_entries([bad, ok])[0] == 1


def test_ingest_lands_an_equal_timestamp_and_refuses_a_non_canonical_shape(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    first = _fact(hv, d0, "first", ts(12))
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3}), first])
    same = _fact(hv, d0, "same", ts(12))
    offset = _fact(hv, d0, "offset", "2026-01-01T07:30:00.000-05:00")      # 12:30 UTC, sorts before 12:00
    assert hv.append_foreign_entries([same])[0] == 1
    assert hv.append_foreign_entries([offset])[0] == 0
    z = _fact(hv, d0, "z", "2026-01-01T13:00:00Z")
    assert hv.append_foreign_entries([z])[0] == 0


def test_ingest_refuses_a_pre_admit_content_entry_but_lands_a_join_request(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    _journal(hv, base + [_marker(hv, owner, {"ownerdev": 3})])
    jr = _entry(hv, d1, "governance", {"action": "join-request", "device_id": d1["id"]}, ts(0, 20))
    early = _fact(hv, d1, "early", ts(0, 54))                # 6 minutes before the admit
    ok = _fact(hv, d1, "ok", ts(0, 56))
    assert hv.append_foreign_entries([jr])[0] == 1
    assert hv.append_foreign_entries([early])[0] == 0
    assert hv.append_foreign_entries([ok])[0] == 1


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


def test_ingest_chain_leaves_out_an_entry_the_projection_skips(tmp_path, monkeypatch):
    """A held entry above the tip that the projection skips for its shape must not raise the bar for a later one:
    a node that held it before the marker arrived refuses what a node that got the marker first lands."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    s2 = _fact(hv, d0, "s2z", "2026-01-01T13:00:00Z")       # non-canonical, later instant
    s3 = _fact(hv, d0, "s3", ts(12, 50))
    _journal(hv, base + [s1, s2])                            # node A held s1, s2 before the marker reached it
    hv.append_foreign_entries([marker])
    hv.append_foreign_entries([s3])
    a = _held(hv)
    ca = _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR)))
    _journal(hv, base + [marker])                            # node B had the marker first
    hv.append_foreign_entries([s1, s2, s3])
    b = _held(hv)
    cb = _contents(_project(hv, tmp_path, hv.merkle.read_all_entries(hv.JOURNAL_DIR)))
    assert ca == cb == {"s1", "s3"}
    assert (d0["id"], 3) in a and (d0["id"], 3) in b


def test_a_governance_entry_is_judged_after_the_same_batchs_lower_seq_content(tmp_path, monkeypatch):
    """d0 holds s1 12:00; s2 is a fact at 12:20 and s3 an `announce` at 12:10, 10 minutes behind s2."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    s2 = _fact(hv, d0, "s2", ts(12, 20))
    s3 = _entry(hv, d0, "governance", {"action": "announce", "kind": "key", "device_id": d0["id"]}, ts(12, 10))
    _journal(hv, base + [marker, s1])
    hv.append_foreign_entries([s2, s3])                      # node A: one batch
    a = _held(hv)
    _journal(hv, base + [marker, s1])
    hv.append_foreign_entries([s2])                          # node B: two batches
    hv.append_foreign_entries([s3])
    assert a == _held(hv) == {("ownerdev", i) for i in (1, 2, 3, 4)} | {(d0["id"], 1), (d0["id"], 2)}
    assert (d0["id"], 3) not in a


def test_ingest_refuses_a_governance_entry_for_its_timestamp(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    behind = _entry(hv, d0, "governance", {"action": "announce", "kind": "key", "device_id": d0["id"]}, ts(11, 40))
    shaped = _entry(hv, d0, "governance", {"action": "announce", "kind": "key", "device_id": d0["id"]},
                    "2026-01-01T12:30:00Z")
    _journal(hv, base + [marker, s1])
    assert hv.append_foreign_entries([behind, shaped])[:2] == (0, 0)
    assert _held(hv) == {("ownerdev", i) for i in (1, 2, 3, 4)} | {(d0["id"], 1)}


def test_a_batch_whose_lower_seq_carries_the_later_timestamp(tmp_path, monkeypatch):
    """s2 at 12:20, s3 at 12:10: in one batch, in either order, s3 is judged after s2 and refused."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    s2 = _fact(hv, d0, "s2", ts(12, 20))
    s3 = _fact(hv, d0, "s3", ts(12, 10))
    for batch in ([s3, s2], [s2, s3]):
        _journal(hv, base + [marker, s1])
        assert hv.append_foreign_entries(batch)[:2] == (1, 0)
        assert _held(hv) == {("ownerdev", i) for i in (1, 2, 3, 4)} | {(d0["id"], 1), (d0["id"], 2)}


def test_ingest_agrees_with_the_projection_for_every_split_of_a_batch(tmp_path, monkeypatch):
    """Ingest refuses `e` iff `_ts_violations` over held + landed + `e` skips `e`, for any contiguous split."""
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    run = [_fact(hv, d0, "s2", ts(12, 20)),
           _entry(hv, d0, "governance", {"action": "announce", "kind": "key", "device_id": d0["id"]}, ts(12, 10)),
           _fact(hv, d0, "s4", ts(12, 14)),
           _fact(hv, d0, "s5", "2026-01-01T13:00:00Z"),
           _fact(hv, d0, "s6", ts(12, 40))]
    _journal(hv, base + [marker, s1] + run)
    expect = _held(hv) - _skipped(hv)
    assert len(expect) == len(_held(hv)) - 3                 # the announce, s4 and s5 are skipped
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
        assert _held(hv) == expect, bin(cuts)


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
    assert hv.append_foreign_entries([fake])[:2] == (0, 0)


def test_a_marker_that_arrives_in_the_batch_arms_the_rest_of_it(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1), base = _hive(hv)
    marker = _marker(hv, owner, {"ownerdev": 3})
    s1 = _fact(hv, d0, "s1", ts(12))
    s2 = _fact(hv, d0, "s2", ts(11))
    _journal(hv, base)
    hv.append_foreign_entries([s1, s2, marker])
    assert _held(hv) == {("ownerdev", i) for i in (1, 2, 3, 4)} | {(d0["id"], 1)}


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
