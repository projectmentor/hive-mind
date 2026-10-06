"""#253: a filesystem that refuses flock must stop the write with one clear line, never write unlocked."""

import errno
import fcntl
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hardening_batch2 import _load  # noqa: E402


def _no_locks(monkeypatch, hv):
    def flock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(hv, "_fcntl", types.SimpleNamespace(flock=flock, LOCK_EX=fcntl.LOCK_EX))


def _journal_bytes(hv):
    return sorted((p.name, p.read_bytes()) for p in Path(hv.JOURNAL_DIR).glob("*.jsonl"))


def test_remember_stops_and_writes_nothing(tmp_path, monkeypatch, capsys):
    hv = _load("hv", tmp_path, monkeypatch)
    hv.init_db()
    Path(hv.JOURNAL_DIR).mkdir(parents=True, exist_ok=True)
    hv.remember(hv.build_parser().parse_args(["remember", "baseline", "--tags", "t"]))
    before = _journal_bytes(hv)
    _no_locks(monkeypatch, hv)
    monkeypatch.setattr(sys, "argv", ["hv", "remember", "should not land", "--tags", "t"])
    capsys.readouterr()
    rc = hv.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "ENOLCK" in err and "Move HIVE_HOME to a local filesystem" in err
    assert "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert _journal_bytes(hv) == before            # no journal line
    monkeypatch.undo()
    hv2 = _load("hv", tmp_path, monkeypatch)
    hv2.remember(hv2.build_parser().parse_args(["remember", "after", "--tags", "t"]))
    seqs = sorted(e["seq"] for e in hv2.merkle.read_all_entries(hv2.JOURNAL_DIR) if e["node_id"] == hv2.NODE_ID)
    assert seqs == list(range(1, len(seqs) + 1))   # no seq consumed, no gap


def test_doctor_reports_failing_probe(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)
    ok = [c for c in hv._doctor_status() if c["name"] == "journal-lock"]
    assert ok and ok[0]["status"] == "ok"
    _no_locks(monkeypatch, hv)
    bad = [c for c in hv._doctor_status() if c["name"] == "journal-lock"]
    assert bad[0]["status"] == "fail"
    assert "local filesystem" in bad[0]["detail"]
