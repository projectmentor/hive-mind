"""Offline tests for the telemetry lane (`hv telemetry`).

Drives the real `hv` CLI via subprocess against an isolated temp HIVE_HOME, with a synthetic
transcript fixture and a deterministic pricing override. Verifies capture, identity separation,
aggregation, and — critically — that telemetry never leaks into the knowledge corpus.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"


def run_hv(home, *args, stdin="", node="testnode", **env):
    e = dict(os.environ, HIVE_HOME=str(home), HIVE_NODE_ID=node)
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run([sys.executable, str(HV), *args], input=stdin,
                          env=e, capture_output=True, text=True)


def _fixture_transcript(home, session_id, rows):
    d = home / "tx"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def _price(home, mapping):
    d = home / ".telemetry"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pricing.json").write_text(json.dumps(mapping))


def _tel_rows(home):
    conn = sqlite3.connect(home / ".telemetry" / "telemetry.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM telemetry_sessions").fetchall()
    finally:
        conn.close()


def test_record_start_end_captures_usage_and_cost(tmp_path):
    _price(tmp_path, {"test-model": {"in": 2.0, "out": 8.0}})
    tx = _fixture_transcript(tmp_path, "sX", [
        {"type": "assistant", "message": {"model": "test-model",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}},
        {"type": "assistant", "message": {"model": "<synthetic>",
            "usage": {"input_tokens": 99, "output_tokens": 99}}},  # must be skipped
    ])
    run_hv(tmp_path, "telemetry", "record", "--event", "start", "--agent", "claude-code",
           "--identity", "u@n", "--session", "sX", "--cwd", str(tmp_path))
    run_hv(tmp_path, "telemetry", "record", "--event", "end", "--agent", "claude-code",
           "--identity", "u@n", "--session", "sX", "--cwd", str(tmp_path), "--transcript", str(tx))
    rows = _tel_rows(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["tokens_in"] == 1_000_000 and r["tokens_out"] == 1_000_000   # synthetic excluded
    assert abs(r["cost_usd"] - 10.0) < 1e-6                               # 1M*2 + 1M*8 per-M = $10
    assert r["ended_at"] and r["started_at"]


def test_identity_separation_two_same_named_agents(tmp_path):
    # Two `hermes` with different agent_identity must NOT collapse into one row.
    for ident in ("primary/default", "primary/sonnet"):
        run_hv(tmp_path, "telemetry", "record", "--event", "start", "--agent", "hermes",
               "--identity", ident, "--session", "s1", "--cwd", str(tmp_path))
    rows = _tel_rows(tmp_path)
    assert len(rows) == 2
    assert {r["agent_identity"] for r in rows} == {"primary/default", "primary/sonnet"}


def test_different_nodes_do_not_collide(tmp_path):
    run_hv(tmp_path, "telemetry", "record", "--event", "start", "--agent", "hermes",
           "--identity", "x", "--session", "s1", node="nodeA")
    run_hv(tmp_path, "telemetry", "record", "--event", "start", "--agent", "hermes",
           "--identity", "x", "--session", "s1", node="nodeB")
    assert {r["node_id"] for r in _tel_rows(tmp_path)} == {"nodeA", "nodeB"}


def test_report_json_aggregates_by_model(tmp_path):
    _price(tmp_path, {"m1": {"in": 1.0, "out": 1.0}})
    tx = _fixture_transcript(tmp_path, "sR", [
        {"type": "assistant", "message": {"model": "m1", "usage": {"input_tokens": 10, "output_tokens": 20}}},
    ])
    run_hv(tmp_path, "telemetry", "record", "--event", "end", "--agent", "claude-code",
           "--session", "sR", "--transcript", str(tx))
    out = run_hv(tmp_path, "telemetry", "report", "--format", "json").stdout
    data = json.loads(out)
    assert data["totals"]["sessions"] == 1
    assert data["by_model"]["m1"]["in"] == 10 and data["by_model"]["m1"]["out"] == 20


def test_no_leakage_into_corpus(tmp_path):
    run_hv(tmp_path, "telemetry", "record", "--event", "start", "--agent", "claude-code",
           "--session", "sL", "--cwd", str(tmp_path))
    # No journal entries.
    journal = list((tmp_path / "journal").glob("*.jsonl"))
    assert all(not f.read_text().strip() for f in journal), "telemetry must not write to the journal"
    # No facts, and no telemetry table inside store.db.
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'telemetry%'").fetchall() == []
    conn.close()
    # But it IS in the separate telemetry db.
    assert len(_tel_rows(tmp_path)) == 1


def test_record_never_errors_on_missing_transcript(tmp_path):
    r = run_hv(tmp_path, "telemetry", "record", "--event", "end", "--agent", "claude-code",
               "--session", "ghost", "--transcript", str(tmp_path / "nope.jsonl"))
    assert r.returncode == 0
    rows = _tel_rows(tmp_path)
    assert len(rows) == 1 and (rows[0]["tokens_in"] or 0) == 0   # no usage, but no crash


def test_rebuild_does_not_wipe_telemetry(tmp_path):
    run_hv(tmp_path, "telemetry", "record", "--event", "start", "--agent", "claude-code",
           "--session", "sK", "--cwd", str(tmp_path))
    assert len(_tel_rows(tmp_path)) == 1
    run_hv(tmp_path, "rebuild")   # rebuilds store.db from the journal
    assert len(_tel_rows(tmp_path)) == 1, "rebuild must not touch the separate telemetry db"
