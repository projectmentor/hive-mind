"""Stable ids for tests (2.0, #59/#64): the CLI and MCP reject local ids, so a test that knows a row by
its local id asks the hive for the row's sid and passes that."""

import sqlite3
from pathlib import Path


def sid(home, local_id, kind="fact"):
    """The sid of `home`'s row `local_id` of `kind`. A corroborated fact maps several journal entries to
    one row; its sid is the first entry's by (node_id, seq), the same rule `hv` uses to name it."""
    conn = sqlite3.connect(Path(home) / "store.db")
    try:
        row = conn.execute("SELECT sid FROM journal_index WHERE kind = ? AND local_id = ? "
                           "ORDER BY node_id, seq LIMIT 1", (kind, int(local_id))).fetchone()
    finally:
        conn.close()
    assert row, f"no {kind} #{local_id} in {home}"
    return row[0]
