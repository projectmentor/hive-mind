"""1.19 PR7 — trust velocity (design §8; hive #56).

Per-signer reliability over governed short/long windows; the DELTA is the signal. Advisory only:
`doctor trust-drift` and a `hv peers` DRIFT column, and — pinned here — NO effect on admission,
corroboration weight, or link authority. Uses the in-memory entry builders from test_links.py with
`$HIVE_NOW` fixed so windows are deterministic.
"""

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import (_loadhv, _owned_hive, _fact, _decision, _link, _project, _entry, _device, _gov)  # noqa: E402

NOW = "2026-06-30T00:00:00Z"


def _hv(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    monkeypatch.setenv("HIVE_NOW", NOW)
    return hv


def _retract(hv, dev, fact, ts):
    return _entry(hv, dev, "retract", {"retracts_ref": [fact["node_id"], fact["seq"]], "reason": "", "source": "manual"}, ts)


def _history(hv, a, b, base):
    """A: 10 uncontradicted facts in March, 4 recent facts of which 3 are retracted by B in the last days;
    an old decision that worked out (+1) and a recent one that did not (−1). B: clean."""
    old = [_fact(hv, a, f"old claim {i} by a", f"2026-03-{10 + i:02d}T00:00:00Z") for i in range(10)]
    new = [_fact(hv, a, f"new claim {i} by a", f"2026-06-2{i}T00:00:00Z") for i in range(4)]
    hits = [_retract(hv, b, f, "2026-06-29T00:00:00Z") for f in new[:3]]
    d_old = _decision(hv, a, "old decision", "2026-03-01T00:00:00Z")
    d_new = _decision(hv, a, "new decision", "2026-06-25T00:00:00Z")
    o1 = _fact(hv, b, "it worked", "2026-03-05T00:00:00Z")
    o2 = _fact(hv, b, "it failed", "2026-06-28T00:00:00Z")
    outs = [_link(hv, b, "outcome-of", o1, d_old, "2026-03-06T00:00:00Z", data={"polarity": 1}),
            _link(hv, b, "outcome-of", o2, d_new, "2026-06-28T00:00:01Z", data={"polarity": -1})]
    return base + old + new + hits + [d_old, d_new, o1, o2] + outs


def test_window_math_change_not_level(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    entries = _history(hv, a, b, base)
    gov = hv._governance_state(entries)
    lo = hv._signer_reliability(entries, gov, 180, NOW)[a["id"]]
    sh = hv._signer_reliability(entries, gov, 14, NOW)[a["id"]]
    assert (lo["asserted"], lo["contradicted"]) == (14, 3) and abs(lo["reliability"] - (1 - 3 / 14)) < 1e-6
    assert (sh["asserted"], sh["contradicted"]) == (4, 3) and abs(sh["reliability"] - 0.25) < 1e-6
    assert lo["decisions"] == 2 and sh["decisions"] == 1 and sh["outcome_mean"] < 0 <= lo["outcome_mean"] + 1e-9
    v = hv._trust_velocity(entries, gov, NOW)
    assert v[a["id"]]["delta"] < -0.5 and v[a["id"]]["outcome_delta"] < 0          # sharply negative: recent decline
    # B is uniformly reliable: no change → delta 0 (level irrelevant)
    assert v[b["id"]]["delta"] == 0.0 and v[b["id"]]["outcome_delta"] is None
    # a device with nothing in the short window has no velocity, not a bad one
    c = _device(hv)
    assert c["id"] not in v


def test_always_mediocre_is_not_drift_and_self_correction_does_not_count(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    # a is contradicted at a steady 50% in both windows → delta 0
    facts = [_fact(hv, a, f"claim {i}", f"2026-0{3 + (i // 4)}-{10 + (i % 4):02d}T00:00:00Z") for i in range(8)]
    recent = [_fact(hv, a, f"recent {i}", f"2026-06-2{i}T00:00:00Z") for i in range(4)]
    hits = [_retract(hv, b, f, "2026-06-01T00:00:00Z") for f in facts[::2]] + [_retract(hv, b, f, "2026-06-29T00:00:00Z") for f in recent[::2]]
    entries = base + facts + recent + hits
    gov = hv._governance_state(entries)
    assert hv._trust_velocity(entries, gov, NOW)[a["id"]]["delta"] == 0.0
    assert hv._trust_drifting(entries, gov, NOW) == []
    # a retracting ITS OWN recent facts is self-correction — no reliability hit
    selfhits = [_retract(hv, a, f, "2026-06-29T00:00:00Z") for f in recent]
    entries2 = base + facts + recent + selfhits
    sh = hv._signer_reliability(entries2, hv._governance_state(entries2), 14, NOW)[a["id"]]
    assert sh["contradicted"] == 0 and sh["reliability"] == 1.0


def test_per_channel_split_and_contradicts_links_count(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    sense = [_fact(hv, a, f"observed {i}", f"2026-06-2{i}T00:00:00Z") for i in range(3)]
    intro = [_entry(hv, a, "fact", {"content": f"reasoned {i}", "source": "manual", "channel": "introspect"},
                    f"2026-06-2{i}T00:00:01Z") for i in range(3)]
    other = _fact(hv, b, "counter-observation", "2026-06-28T00:00:00Z")
    # b contradicts all three of a's introspect facts via LINKS, none of the sense ones
    hits = [_link(hv, b, "contradicts", other, f, "2026-06-29T00:00:00Z") for f in intro]
    entries = base + sense + intro + [other] + hits
    gov = hv._governance_state(entries)
    sh = hv._signer_reliability(entries, gov, 14, NOW)[a["id"]]
    assert sh["channels"]["sense"]["reliability"] == 1.0
    assert sh["channels"]["introspect"]["reliability"] == 0.0
    assert sh["contradicted"] == 3 and sh["asserted"] == 6
    # the contradicted-introspect signal does not move the sense reliability, and vice versa
    v = hv._trust_velocity(entries, gov, NOW)[a["id"]]
    # both windows see the same recent facts → per-channel deltas are 0 (no change), and the sense
    # channel is unaffected by the contradicted reasoning
    assert v["channels"]["sense"] == 0.0 and v["channels"]["introspect"] == 0.0
    lo = hv._signer_reliability(entries, gov, 180, NOW)[a["id"]]
    assert lo["channels"]["sense"]["reliability"] == 1.0 and lo["channels"]["introspect"]["reliability"] == 0.0


def test_doctor_lists_only_nodes_under_threshold_and_knobs_are_governed(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    entries = _history(hv, a, b, base)
    gov = hv._governance_state(entries)
    drift = hv._trust_drifting(entries, gov, NOW)
    assert len(drift) == 1 and drift[0].startswith(a["id"]) and "reliability" in drift[0]
    # loosen the threshold via a governed set-config → nothing listed (identically on every node)
    knob = _gov(hv, {"action": "set-config", "key": "trust_drift_threshold", "value": -0.9},
                oseed, opub, "2026-01-01T00:00:09Z", 9)
    e2 = base + [knob] + entries[len(base):]
    assert hv._trust_drifting(e2, hv._governance_state(e2), NOW) == []
    # shrink the long window below the March history → no long baseline → no delta
    knob2 = _gov(hv, {"action": "set-config", "key": "trust_long_days", "value": 20},
                 oseed, opub, "2026-01-01T00:00:09Z", 9)
    e3 = base + [knob2] + entries[len(base):]
    v = hv._trust_velocity(e3, hv._governance_state(e3), NOW)[a["id"]]
    assert v["long"] == 0.25 and v["short"] == 0.25 and v["delta"] == 0.0


def test_no_effect_on_admission_corroboration_or_link_authority(tmp_path, monkeypatch):
    """The hard promise of §8: a device at maximal negative drift still ingests, still corroborates with
    full identity weight, and its hard links still resolve."""
    hv = _hv(tmp_path, monkeypatch)
    _, (a, b, c), base = _owned_hive(hv)
    entries = _history(hv, a, b, base)
    gov = hv._governance_state(entries)
    assert hv._trust_velocity(entries, gov, NOW)[a["id"]]["delta"] < -0.5
    # corroboration: a's assertion of c's fact still counts as a second identity
    shared = "the deploy succeeded at commit abc123"
    f_c = _fact(hv, c, shared, "2026-06-29T00:00:00Z")
    f_a = _fact(hv, a, shared, "2026-06-29T00:00:01Z")
    conn = _project(hv, tmp_path, entries + [f_c, f_a])
    conf = conn.execute("SELECT confidence FROM facts WHERE content = ?", (shared,)).fetchone()[0]
    assert round(conf, 6) == round(hv._confidence_for(2.0), 6)
    # link authority: a's supersede of ITS OWN decision is still hard
    d1 = _decision(hv, a, "first", "2026-06-29T00:00:02Z")
    d2 = _decision(hv, a, "second", "2026-06-29T00:00:03Z")
    conn = _project(hv, tmp_path, entries + [d1, d2, _link(hv, a, "supersedes", d2, d1, "2026-06-29T00:00:04Z")])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='first'").fetchone()[0] is not None
    # ingest: a's new entries are still accepted (admitted, valid signature)
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    newf = _fact(hv, a, "still ingested after drift", "2026-06-29T00:00:05Z")
    accepted, _dups = hv.append_foreign_entries([newf])
    assert accepted == 1


def test_config_bounds_and_defaults(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    cfg = hv._governance_state([])["config"]
    assert (cfg["trust_long_days"], cfg["trust_short_days"], cfg["trust_drift_threshold"]) == (180.0, 14.0, -0.3)


def test_two_node_differential_velocity(tmp_path, monkeypatch):
    hv = _hv(tmp_path / "n1", monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    entries = _history(hv, a, b, base)
    gov = hv._governance_state(entries)
    v1 = hv._trust_velocity(entries, gov, NOW)
    v2 = hv._trust_velocity(list(reversed(entries)), gov, NOW)
    assert v1 == v2
