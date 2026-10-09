"""The envelope of a journal entry (node_id, seq, type, payload container, and the few payload fields readers key
on) is checked once at ingest, before any signature, and again by the reader, so an entry with a wrong-shaped
envelope neither lands nor breaks a read of a journal that already holds one (a journal is permanent)."""

import copy
import json
import random
from pathlib import Path

import pytest

import merkle
from test_links import _loadhv, _device, _entry, _fact, _project

TS = "2026-01-01T00:00:0%dZ"


def _jd(home):
    (Path(home) / "journal").mkdir(parents=True, exist_ok=True)


def _on_disk(home):
    return [json.loads(l) for f in sorted((Path(home) / "journal").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]


def _signed(hv, dev, seq, typ, payload, ts=TS % 1):
    """A device-signed entry with an arbitrary `seq` (the helpers only count up)."""
    e = {"node_id": dev["id"], "seq": seq, "type": typ, "timestamp": ts, "payload": payload}
    return hv._sign_entry(e, dev["seed"], dev["pub"])


def _assert_refused_and_journal_reads(hv, home, bad):
    """The entry is refused whole, and every reader of what is held still works."""
    _jd(home)
    d = _device(hv)
    good = _fact(hv, d, "kept", TS % 1)
    accepted, _ = hv.append_foreign_entries([bad, good])
    assert accepted == 1
    assert [e["seq"] for e in _on_disk(home)] == [good["seq"]]
    merkle.node_max_seq(merkle.read_all_entries(hv.JOURNAL_DIR))
    hv.rebuild_db()
    hv._governance_state(merkle.read_all_entries(hv.JOURNAL_DIR))


# ── the probes: each refused at ingest ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seq", ["7", "1", None, 1.0, [1], True, False, 0, -1, 2**53, 2**63, 2**64, 10**30],
                         ids=repr)
def test_bad_seq_is_refused(tmp_path, monkeypatch, seq):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    bad = _signed(hv, d, seq, "governance", {"action": "join-request", "device_id": d["id"], "label": "x"})
    _assert_refused_and_journal_reads(hv, tmp_path, bad)


def test_seq_far_past_the_held_tip_is_refused_but_a_gap_is_not(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    first = _fact(hv, d, "one", TS % 1)
    assert hv.append_foreign_entries([first])[0] == 1
    near = _signed(hv, d, first["seq"] + 500, "fact", {"content": "gap"}, TS % 2)
    far = _signed(hv, d, first["seq"] + merkle.SEQ_JUMP_MAX + 2, "fact", {"content": "far"}, TS % 3)
    assert hv.append_foreign_entries([far])[0] == 0
    assert hv.append_foreign_entries([near])[0] == 1
    # a first pull larger than the jump bound is not a jump: the batch carries the run it leads
    fresh = _device(hv)
    run = [_signed(hv, fresh, s, "fact", {"content": f"r{s}"}, TS % 4) for s in (1, 2, 3)]
    assert hv.append_foreign_entries(run)[0] == 3


@pytest.mark.parametrize("basis_ts,proposal_id", [(5, "e1:x"), ([1], "e1:x"), ({"a": 1}, "e1:x"),
                                                  ("2026-01-01T00:00:00Z", 5), ("2026-01-01T00:00:00Z", ["e1:x"]),
                                                  ("2026-01-01T00:00:00Z", {"a": 1})], ids=repr)
def test_bad_election_fields_are_refused(tmp_path, monkeypatch, basis_ts, proposal_id):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    for action in ("propose-election", "vote-election"):
        bad = _signed(hv, d, 1, "governance", {"action": action, "proposal_id": proposal_id,
                                               "new_owner_pub": "AAAA", "basis_ts": basis_ts})
        assert merkle.envelope_problem(bad) is not None
    _assert_refused_and_journal_reads(hv, tmp_path, _signed(
        hv, d, 1, "governance", {"action": "propose-election", "proposal_id": proposal_id,
                                 "new_owner_pub": "AAAA", "basis_ts": basis_ts}))


@pytest.mark.parametrize("device_id", [["k1:x"], {"a": 1}, 5, True])
def test_non_string_device_id_is_refused(tmp_path, monkeypatch, device_id):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    bad = _signed(hv, d, 1, "governance", {"action": "join-request", "device_id": device_id, "label": "x"})
    _assert_refused_and_journal_reads(hv, tmp_path, bad)


@pytest.mark.parametrize("typ", ["cell", "comb", "capsule"])
@pytest.mark.parametrize("payload", ["not an object", ["a"], 5, True])
def test_non_object_payload_is_refused(tmp_path, monkeypatch, typ, payload):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    _assert_refused_and_journal_reads(hv, tmp_path, _signed(hv, d, 1, typ, payload))


@pytest.mark.parametrize("typ", ["cell", "comb", "capsule"])
@pytest.mark.parametrize("field,value", [("name", ["x"]), ("name", {"a": 1}), ("version", "2"), ("version", [1]),
                                         ("version", 2**70)], ids=repr)
def test_bad_name_or_version_is_refused(tmp_path, monkeypatch, typ, field, value):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    p = {"name": "n", "version": 1}
    p[field] = value
    _assert_refused_and_journal_reads(hv, tmp_path, _signed(hv, d, 1, typ, p))


@pytest.mark.parametrize("field,value", [("node_id", ["k1:x"]), ("node_id", 5), ("node_id", ""),
                                         ("type", ["fact"]), ("type", 5), ("type", ""), ("type", None),
                                         ("prev_hash", 5), ("prev_hash", ["x"]), ("timestamp", 5), ("sig", 5),
                                         ("payload", "x"), ("payload", [1])], ids=repr)
def test_bad_envelope_field_is_refused(tmp_path, monkeypatch, field, value):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    e = _fact(hv, d, "x", TS % 1)
    e[field] = value
    assert merkle.envelope_problem(e) is not None
    _jd(tmp_path)
    assert hv.append_foreign_entries([e])[0] == 0
    assert _on_disk(tmp_path) == []


def test_a_non_object_entry_in_a_batch_does_not_abort_it(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    good = _fact(hv, d, "ok", TS % 1)
    assert hv.append_foreign_entries([5, None, "x", [1], good])[0] == 1


def test_unknown_type_still_lands(tmp_path, monkeypatch):
    """The vocabulary is open: a newer node's type lands and projects to nothing (vocabulary.py, version skew)."""
    hv = _loadhv(tmp_path, monkeypatch)
    _jd(tmp_path)
    d = _device(hv)
    e = _signed(hv, d, 1, "x-mod:thing", {"anything": [1, 2]})
    assert hv.append_foreign_entries([e])[0] == 1
    assert _signed(hv, d, 2, "x-mod:thing", None)["payload"] is None
    assert merkle.envelope_problem(_signed(hv, d, 2, "x-mod:thing", None)) is None


# ── a journal that already holds poison heals on upgrade ───────────────────────────────────────────────────

def test_reader_skips_and_counts_a_poisoned_journal(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    good = [_fact(hv, d, "one", TS % 1), _fact(hv, d, "two", TS % 2)]
    poison = [_signed(hv, d, "7", "governance", {"action": "join-request", "device_id": d["id"]}),
              _signed(hv, d, 2**63, "fact", {"content": "wide"}),
              _signed(hv, d, 50, "governance", {"action": "propose-election", "proposal_id": 5, "basis_ts": [1]}),
              _signed(hv, d, 51, "governance", {"action": "join-request", "device_id": ["k1:x"]}),
              _signed(hv, d, 52, "cell", "not an object"),
              _signed(hv, d, 53, "cell", ["a"])]
    conn = _project(hv, tmp_path, good[:1] + poison + good[1:])
    conn.close()
    held = merkle.read_all_entries(hv.JOURNAL_DIR)
    assert [e["seq"] for e in held] == [1, 2]
    assert merkle.corrupt_lines(hv.JOURNAL_DIR)[0] == len(poison)
    assert hv.rebuild_db()["skipped_lines"] == len(poison)
    assert {r[0] for r in __import__("sqlite3").connect(Path(tmp_path) / "store.db").execute("SELECT content FROM facts")} \
        == {"one", "two"}
    hv._pending_admissions(held, hv._governance_state(held))
    hv._cell_state(held)


# ── fuzz ───────────────────────────────────────────────────────────────────────────────────────────────────

_ODD = [None, True, False, 0, -1, 1, 7, 2**31, 2**53, 2**63, 2**64, 1.0, float("inf"), "", "7", "k1:x", "e1:x",
        [], [1], ["k1:x"], {}, {"a": 1}, [[1]], "\u0000", 10**40]


def _mutate(rng, e):
    e = copy.deepcopy(e)
    for _ in range(rng.randint(1, 3)):
        where = rng.choice(["top", "payload"])
        target = e if where == "top" or not isinstance(e.get("payload"), dict) else e["payload"]
        key = rng.choice(list(target) + ["node_id", "seq", "type", "prev_hash", "payload", "device_id", "name",
                                         "version", "basis_ts", "proposal_id", "action"])
        r = rng.random()
        if r < 0.15:
            target.pop(key, None)
        else:
            target[key] = rng.choice(_ODD)
    return e


def test_envelope_fuzz(tmp_path, monkeypatch):
    """Whatever the mutation: the validator never raises, and an entry it passes can be sorted, chunked, hashed
    and serialised by every merkle reader, and survives the journal round trip unchanged in verdict."""
    hv = _loadhv(tmp_path, monkeypatch)
    d = _device(hv)
    seeds = [_fact(hv, d, "f", TS % 1),
             _signed(hv, d, 2, "governance", {"action": "join-request", "device_id": d["id"], "label": "l"}),
             _signed(hv, d, 3, "governance", {"action": "propose-election", "proposal_id": "e1:x",
                                              "new_owner_pub": "AAAA", "basis_ts": "2026-01-01T00:00:00Z"}),
             _signed(hv, d, 4, "cell", {"name": "c", "version": 1, "kind": "tool"}),
             _signed(hv, d, 5, "x-mod:thing", None)]
    rng = random.Random(0xA1)
    passed = 0
    for _ in range(3000):
        e = _mutate(rng, rng.choice(seeds))
        problem = merkle.envelope_problem(e)                        # must not raise
        assert problem is None or isinstance(problem, str)
        assert (merkle.envelope_problem(e, 0) is None) <= (problem is None)   # a tip only ever narrows
        if problem is not None:
            continue
        passed += 1
        assert isinstance(e["node_id"], str) and type(e["seq"]) is int and 1 <= e["seq"] < 2**53
        assert isinstance(e["type"], str) and (e.get("payload") is None or isinstance(e["payload"], dict))
        if e["seq"] <= 10_000:
            es = merkle.read_all_entries(_write(tmp_path, [e]))
            assert len(es) == 1
            merkle.node_max_seq(es)
            merkle.node_chunk_hashes(es)
            merkle.chunk_hashes(es)
            merkle.entries_in_range(es, e["node_id"], 1, 10)
            if e["type"] == "governance":
                for p in (e.get("payload") or {},):
                    {p.get("device_id"), p.get("proposal_id"), p.get("action")}      # hashable, as the projection needs
        assert json.loads(json.dumps(e)) == e
    assert passed > 100


def _write(home, entries):
    jd = Path(home) / "fuzz"
    jd.mkdir(exist_ok=True)
    for f in jd.glob("*.jsonl"):
        f.unlink()
    (jd / "j.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return jd
