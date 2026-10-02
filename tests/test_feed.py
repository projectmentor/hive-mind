"""2.1 PR 1: `hv feed`, the read-only journal feed for modules (plan §4).

Cursor is the journal's own identity, a set of (node_id, seq); entries come from the journal files in
(node_id, seq) order; `forgotten` (facts) and `affects` (retracts) are computed by the core. Entries are built
with real signatures (the test_links builders) and the CLI is driven against a temp hive.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _fact, _decision  # noqa: E402
from test_unforget import _act, _hive  # noqa: E402

HV = PROJECT / "hv"
T = "2026-01-05T00:00:%02dZ"


def _journal(home, entries):
    jd = Path(home) / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    for f in jd.glob("*.jsonl"):
        f.unlink()
    (jd / "2026-01-05.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _all(hv, after=None, limit=1000):
    return hv.feed_page(hv._feed_cursor_parse(after or ""), limit)


def _by_ref(page):
    return {e["ref"]: e for e in page["entries"]}


def test_cursor_is_strictly_after_per_node_and_splits_at_the_last_colon(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, _d2), base = _hive(hv)
    f = [_fact(hv, d0, f"a{i}", T % i) for i in range(3)] + [_fact(hv, d1, f"b{i}", T % (10 + i)) for i in range(2)]
    _journal(tmp_path, base + f)
    page = _all(hv)
    assert [e["type"] for e in page["entries"]].count("fact") == 5
    assert d0["id"].count(":") >= 1                       # node ids contain a colon
    again = _all(hv, page["cursor"])
    assert again["entries"] == [] and again["cursor"] == page["cursor"] and again["more"] is False
    two = _all(hv, f"ownerdev:4,{d0['id']}:2,{d1['id']}:1")
    assert sorted(e["ref"] for e in two["entries"]) == sorted([f"{d0['id']}:3", f"{d1['id']}:2"])
    for bad in ("nocolon", "k1:x", ":3"):
        with pytest.raises(ValueError):
            hv._feed_cursor_parse(bad)


def test_an_entry_written_between_calls_appears_once(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, _d1, _d2), base = _hive(hv)
    f1 = _fact(hv, d0, "one", T % 1)
    _journal(tmp_path, base + [f1])
    first = _all(hv)
    f2 = _fact(hv, d0, "two", T % 2)
    _journal(tmp_path, base + [f1, f2])
    second = _all(hv, first["cursor"])
    assert [e["ref"] for e in second["entries"]] == [f"{d0['id']}:2"]
    assert _all(hv, second["cursor"])["entries"] == []


def test_a_renumbered_store_changes_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, _d1, _d2), base = _hive(hv)
    _journal(tmp_path, base + [_fact(hv, d0, "x", T % 1)])
    before = _all(hv)
    hv.rebuild_db()
    assert _all(hv) == before


def test_limit_and_continuation_lose_nothing_and_repeat_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, _d2), base = _hive(hv)
    facts = [_fact(hv, d0, f"a{i}", T % i) for i in range(4)] + [_fact(hv, d1, f"b{i}", T % (20 + i)) for i in range(3)]
    _journal(tmp_path, base + facts)
    seen, cursor, pages = [], "", 0
    while True:
        page = _all(hv, cursor, limit=2)
        seen += [e["ref"] for e in page["entries"]]
        cursor, pages = page["cursor"], pages + 1
        if not page["more"]:
            break
    assert len(seen) == len(set(seen)) == len(_all(hv)["entries"]) and pages > 2


def test_a_bad_signature_is_absent_and_the_raw_entry_is_attributed_not_authenticated(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, _d2), base = _hive(hv)
    good, bad = _fact(hv, d0, "good", T % 1), _fact(hv, d1, "bad", T % 2)
    bad["payload"] = dict(bad["payload"], content="tampered")
    _journal(tmp_path, base + [good, bad])
    page = _by_ref(_all(hv))
    assert f"{d0['id']}:1" in page and f"{d1['id']}:1" not in page
    assert page[f"{d0['id']}:1"]["sig"] and page[f"{d0['id']}:1"]["pub"]
    shape = page[f"{d0['id']}:1"]
    assert {"node_id", "seq", "ref", "sid", "type", "timestamp", "payload"} <= set(shape)
    assert shape["sid"] == hv._short_id(d0["id"], 1)


def test_forgotten_is_content_level_and_owner_only(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, d2), base = _hive(hv)
    f1, f2 = _fact(hv, d0, "same text", T % 1), _fact(hv, d1, "same text", T % 2)
    other = _fact(hv, d0, "other text", T % 3)
    dec = _decision(hv, d0, "a decision", T % 4)
    forget = _act(hv, d2, f1, T % 10, owner=(owner[0], owner[1]))
    _journal(tmp_path, base + [f1, f2, other, dec, forget])
    page = _by_ref(_all(hv))
    assert page[f"{d0['id']}:1"]["forgotten"] is True and page[f"{d1['id']}:1"]["forgotten"] is True
    assert page[f"{d0['id']}:2"]["forgotten"] is False
    assert "forgotten" not in page[f"{d0['id']}:3"]                       # a decision cannot be forgotten
    assert page[f"{d0['id']}:1"]["type"] == "fact"                        # the raw entry stays
    act = page[f"{d2['id']}:1"]
    assert act["type"] == "retract" and act["affects"] == sorted([f"{d0['id']}:1", f"{d1['id']}:1"])


def test_a_forget_not_signed_by_the_owner_is_not_flagged(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, _d2), base = _hive(hv)
    f = _fact(hv, d0, "kept", T % 1)
    forged = _act(hv, d1, f, T % 10)                                      # owner source, no owner signature
    _journal(tmp_path, base + [f, forged])
    page = _by_ref(_all(hv))
    assert page[f"{d0['id']}:1"]["forgotten"] is False
    assert page[f"{d1['id']}:1"]["affects"] == []


def test_unforget_clears_the_flag_and_forget_then_unforget_list_the_same_refs(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, d2), base = _hive(hv)
    f = _fact(hv, d0, "back and forth", T % 1)
    o = (owner[0], owner[1])
    forget = _act(hv, d1, f, T % 10, owner=o)
    unforget = _act(hv, d2, f, T % 20, owner=o, unforget=True)
    _journal(tmp_path, base + [f, forget, unforget])
    page = _by_ref(_all(hv))
    assert page[f"{d0['id']}:1"]["forgotten"] is False
    assert page[f"{d1['id']}:1"]["affects"] == page[f"{d2['id']}:1"]["affects"] == [f"{d0['id']}:1"]
    _journal(tmp_path, base + [f, forget])
    assert _by_ref(_all(hv))[f"{d0['id']}:1"]["forgotten"] is True


def test_a_retract_arrives_before_or_after_its_target(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    owner, (d0, d1, d2), base = _hive(hv)
    f = _fact(hv, d0, "late arrival", T % 1)
    forget = _act(hv, d1, f, T % 10, owner=(owner[0], owner[1]))
    _journal(tmp_path, base + [forget])                                   # the target has not synced yet
    page = _by_ref(_all(hv))
    assert page[f"{d1['id']}:1"]["affects"] == []
    _journal(tmp_path, base + [f, forget])                                # now it has
    page = _by_ref(_all(hv))
    assert page[f"{d0['id']}:1"]["forgotten"] is True
    assert page[f"{d1['id']}:1"]["affects"] == [f"{d0['id']}:1"]


def test_the_cli_prints_json_and_exits_2_on_a_bad_cursor(tmp_path):
    env = dict(os.environ, HIVE_HOME=str(tmp_path))
    ok = subprocess.run([sys.executable, str(HV), "feed"], env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and json.loads(ok.stdout)["entries"] == []
    bad = subprocess.run([sys.executable, str(HV), "feed", "--after", "oops"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert bad.returncode == 2 and "cursor" in bad.stderr


def test_feed_never_reads_the_owner_key():
    src = (PROJECT / "hv").read_text()
    body = src[src.index("def _feed_cursor_parse"):src.index("def journal_keys")]
    for banned in ("ownerkey", "owner_seed", "_owner_seed", "hivemind_owner"):
        assert banned not in body
