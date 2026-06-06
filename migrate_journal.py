#!/usr/bin/env python3
"""
One-time fresh-start migration to the enhanced journal format.

The pre-Phase-1 journal used a 4-field shape (ts/agent/op/data) with no
node_id/seq/prev_hash. Per the chosen migration strategy ("fresh start"), this
script regenerates the journal from the CURRENT SQLite state in the new format,
preserving content, ids (via deterministic replay order), timestamps, and trust.

Steps:
  1. Back up the existing journal/ directory.
  2. Emit one new-format entry per row in store.db, in id order:
        facts -> decisions -> entities -> entity_facts
     (entity_facts last so the facts/entities they reference already exist on
     replay). Each entry carries a per-node monotonic seq and a prev_hash chain.
  3. Replace journal/ with the regenerated entries.

After running, run `./hv rebuild` to reconstruct store.db from the new journal
and confirm the counts match.

Idempotent-ish: re-running re-backs-up and regenerates from the (unchanged) DB.
"""

import hashlib
import json
import os
import shutil
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HIVE_HOME = Path(os.environ.get("HIVE_HOME", Path.home() / "projects" / "hive-mind"))
DB_PATH = HIVE_HOME / "store.db"
JOURNAL_DIR = HIVE_HOME / "journal"
NODE_ID = socket.gethostname()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _compute_hash(entry):
    return "sha256:" + hashlib.sha256(_canonical(entry)).hexdigest()


def _normalize_ts(value):
    """Coerce a stored created_at into an ISO8601 string. Accepts both the old
    SQLite default ('YYYY-MM-DD HH:MM:SS') and ISO strings."""
    if not value:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    value = str(value)
    if "T" not in value and " " in value:
        value = value.replace(" ", "T", 1)
    return value


def build_entries(conn):
    """Produce new-format journal entries from current DB state, ordered so that
    replay reproduces the same row ids."""
    rows = []  # (type, payload, timestamp)

    # Replay assigns fact ids sequentially per DISTINCT content (dedup collapses
    # any true duplicate rows) and entity ids sequentially per unique name. We
    # precompute those old->new id maps so entity_fact links stay valid after
    # the collapse, without embedding SQLite ids in the (node-portable) journal.
    fact_oldid_to_newid = {}
    content_to_newid = {}
    for r in conn.execute("SELECT id, content FROM facts ORDER BY id"):
        newid = content_to_newid.setdefault(r["content"], len(content_to_newid) + 1)
        fact_oldid_to_newid[r["id"]] = newid

    entity_oldid_to_newid = {}
    name_to_newid = {}
    for r in conn.execute("SELECT id, name FROM entities ORDER BY id"):
        newid = name_to_newid.setdefault(r["name"], len(name_to_newid) + 1)
        entity_oldid_to_newid[r["id"]] = newid

    for r in conn.execute("""
        SELECT content, tags, importance, source_agent, source_session,
               created_at, trust_score, access_count
        FROM facts ORDER BY id
    """):
        rows.append(("fact", {
            "content": r["content"],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "importance": r["importance"],
            "source": r["source_agent"],
            "source_session": r["source_session"],
            "created_at": _normalize_ts(r["created_at"]),
            "trust_score": r["trust_score"],
            "access_count": r["access_count"],
        }, _normalize_ts(r["created_at"])))

    for r in conn.execute("""
        SELECT content, rationale, superseded_by, source_agent, created_at
        FROM decisions ORDER BY id
    """):
        rows.append(("decision", {
            "content": r["content"],
            "rationale": r["rationale"],
            "supersedes": r["superseded_by"],
            "source": r["source_agent"],
            "created_at": _normalize_ts(r["created_at"]),
        }, _normalize_ts(r["created_at"])))

    for r in conn.execute("""
        SELECT name, type, attributes, first_seen FROM entities ORDER BY id
    """):
        rows.append(("entity", {
            "name": r["name"],
            "type": r["type"],
            "attributes": json.loads(r["attributes"]) if r["attributes"] else {},
            "created_at": _normalize_ts(r["first_seen"]),
        }, _normalize_ts(r["first_seen"])))

    for r in conn.execute("""
        SELECT entity_id, fact_id, confidence FROM entity_facts
        ORDER BY entity_id, fact_id
    """):
        rows.append(("entity_fact", {
            "entity_id": entity_oldid_to_newid[r["entity_id"]],
            "fact_id": fact_oldid_to_newid[r["fact_id"]],
            "confidence": r["confidence"],
        }, datetime.now(timezone.utc).isoformat(timespec="milliseconds")))

    # Chain into full entries with per-node seq + prev_hash.
    entries = []
    prev_hash = "sha256:genesis"
    for seq, (etype, payload, ts) in enumerate(rows, start=1):
        entry = {
            "node_id": NODE_ID,
            "seq": seq,
            "type": etype,
            "timestamp": ts,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        entries.append(entry)
        prev_hash = _compute_hash(entry)
    return entries


def main():
    if not DB_PATH.exists():
        print(f"No store.db at {DB_PATH}; nothing to migrate.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    entries = build_entries(conn)
    conn.close()

    # 1. Back up existing journal
    if JOURNAL_DIR.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = HIVE_HOME / f"journal.bak.{stamp}"
        shutil.copytree(JOURNAL_DIR, backup)
        print(f"Backed up existing journal -> {backup}")
        for f in JOURNAL_DIR.glob("*.jsonl"):
            f.unlink()
    else:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Write regenerated entries, grouped into files by their timestamp date.
    by_date = {}
    for e in entries:
        by_date.setdefault(e["timestamp"][:10], []).append(e)
    for date, ents in sorted(by_date.items()):
        with open(JOURNAL_DIR / f"{date}.jsonl", "a") as f:
            for e in ents:
                f.write(json.dumps(e) + "\n")

    print(f"Regenerated {len(entries)} entries across {len(by_date)} journal file(s).")
    print("Next: run `./hv rebuild` to reconstruct store.db and verify counts.")


if __name__ == "__main__":
    main()
