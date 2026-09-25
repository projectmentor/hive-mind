"""An entry's timestamp names its journal file (`journal/YYYY-MM-DD.jsonl`), so it is validated before anything is
written (hive-mind-private #29). Ingest rejects an entry whose timestamp is not an ISO-8601 time, and
`_journal_path_for` refuses a date prefix that is not `YYYY-MM-DD` or a path outside journal/, for every caller.
Both shapes the journal really holds stay valid, and the read path never checks: lines on disk are unchanged.
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv  # noqa: E402

LIVE_SHAPES = ["2026-09-25T10:00:00.123+00:00",     # _now_iso(): 1198 of 1212 live lines
               "2026-06-05T01:05:50"]                # naive: the 14 early-June lines
ALSO_ISO = ["2026-09-25T10:00:00Z", "2026-09-25T10:00:00.123456+00:00", "2026-09-25T10:00:00-05:00"]
BAD = ["../PWNED", "/tmp/abcde", "..\\..\\x", "2026-09-25", "2026-13-40T00:00:00", "25-09-2026T10:00:00",
       "2026-09-25T10:00:00.123+00:00/../../x", "", None, 1727258400]


@pytest.fixture
def hv(tmp_path, monkeypatch):
    m = _loadhv(tmp_path, monkeypatch)
    m.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return m


@pytest.mark.parametrize("ts", LIVE_SHAPES + ALSO_ISO)
def test_valid_timestamps_name_a_day_file_inside_journal(hv, ts):
    assert hv._valid_timestamp(ts)
    assert hv._journal_path_for(ts) == hv.JOURNAL_DIR / f"{ts[:10]}.jsonl"


@pytest.mark.parametrize("ts", BAD)
def test_bad_timestamps_are_invalid(hv, ts):
    assert not hv._valid_timestamp(ts)


@pytest.mark.parametrize("ts", ["../PWNED", "/tmp/abcde", "..\\..\\x", "", None, "a/b/c/d/e/f"])
def test_journal_path_for_refuses_anything_but_a_date(hv, ts):
    with pytest.raises(ValueError):
        hv._journal_path_for(ts)


def test_ingest_rejects_a_traversing_timestamp_and_writes_nothing_outside(hv, tmp_path):
    jr = {"node_id": "k1:0000000000000000", "seq": 1, "type": "governance", "timestamp": "../PWNED",
          "payload": {"action": "join-request", "device_id": "k1:0000000000000000"}}
    fact = {"node_id": "k1:0000000000000001", "seq": 1, "type": "fact", "timestamp": "/tmp/abcde",
            "payload": {"content": "x", "tags": [], "source": "manual"}}
    assert hv.append_foreign_entries([jr, fact])[:2] == (0, 0)
    assert not (tmp_path / "PWNED.jsonl").exists() and not Path("/tmp/abcde.jsonl").exists()
    assert not list(tmp_path.rglob("*.jsonl"))                         # nothing written anywhere


@pytest.mark.parametrize("ts", LIVE_SHAPES + ALSO_ISO)
def test_ingest_accepts_every_valid_shape(hv, ts):
    fact = {"node_id": "k1:0000000000000002", "seq": 1, "type": "fact", "timestamp": ts,
            "payload": {"content": f"fact at {ts}", "tags": [], "source": "manual"}}
    assert hv.append_foreign_entries([fact])[:2] == (1, 0)             # pre-owner hive: content lands
    assert (hv.JOURNAL_DIR / f"{ts[:10]}.jsonl").exists()


def test_ingest_rejects_a_missing_timestamp(hv):
    fact = {"node_id": "k1:0000000000000003", "seq": 1, "type": "fact",
            "payload": {"content": "no time", "tags": [], "source": "manual"}}
    assert hv.append_foreign_entries([fact])[:2] == (0, 0)


def test_append_journal_refuses_a_bad_timestamp_and_writes_nothing(hv):
    with pytest.raises(ValueError):
        hv.append_journal("fact", {"content": "x", "tags": [], "source": "manual"}, timestamp="../PWNED")
    assert not list(hv.JOURNAL_DIR.glob("*.jsonl"))


def test_the_read_path_keeps_lines_already_on_disk(hv):
    """Rebuild reads whatever is in journal/; validation is write-side only."""
    lines = [{"node_id": "legacy", "seq": i + 1, "type": "fact", "timestamp": ts,
              "payload": {"content": f"old {i}", "tags": [], "source": "manual"}} for i, ts in enumerate(LIVE_SHAPES)]
    (hv.JOURNAL_DIR / "2026-06-05.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    assert len(hv.merkle.read_all_entries(hv.JOURNAL_DIR)) == 2


# ── Fable + Grok (private #29): one bad entry must not abort a batch or 500 the ingest handler ─────────

def _fact(nid, ts, content):
    return {"node_id": nid, "seq": 1, "type": "fact", "timestamp": ts,
            "payload": {"content": content, "tags": [], "source": "manual"}}


def test_a_bad_entry_is_rejected_alone_and_the_rest_of_the_batch_lands(hv):
    batch = [_fact("k1:00000000000000a1", LIVE_SHAPES[0], "first"),
             _fact("k1:00000000000000a2", "../PWNED", "bad"),
             _fact("k1:00000000000000a3", LIVE_SHAPES[1], "third")]
    assert hv.append_foreign_entries(batch)[:2] == (2, 0)
    contents = {e["payload"]["content"] for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}
    assert contents == {"first", "third"}


def test_a_refusal_from_the_path_helper_is_caught_inside_ingest(hv, monkeypatch):
    """If the two checks ever drift, the helper's ValueError is counted as a rejection, not raised."""
    monkeypatch.setattr(hv, "_valid_timestamp", lambda ts: True)      # simulate the ingest gate missing it
    batch = [_fact("k1:00000000000000b1", LIVE_SHAPES[0], "ok"), _fact("k1:00000000000000b2", "../PWNED", "bad")]
    assert hv.append_foreign_entries(batch)[:2] == (1, 0)


def test_the_ingest_handler_answers_200_and_lands_the_valid_entries(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from test_hardening_batch2 import _load, _free_port
    sd = _load("hive_sync_daemon", tmp_path, monkeypatch)
    sd.hv.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    server, _bind, port = sd.make_server(bind="127.0.0.1", port=_free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        batch = [_fact("k1:00000000000000c1", LIVE_SHAPES[0], "one"),
                 _fact("k1:00000000000000c2", "../PWNED", "bad"),
                 _fact("k1:00000000000000c3", LIVE_SHAPES[1], "two")]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/sync/ingest", data=json.dumps({"entries": batch}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
            assert json.loads(r.read())["accepted"] == 2
        assert not (tmp_path / "PWNED.jsonl").exists()
    finally:
        server.shutdown()
