"""Shared test fixtures.

Every test drives the REAL `hv` CLI via subprocess against an isolated, temp
HIVE_HOME (the CLI honors $HIVE_HOME). No network, no shared state.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"


class Hive:
    def __init__(self, home):
        self.home = Path(home)
        self.journal = self.home / "journal"
        self.db = self.home / "store.db"

    def run(self, *args, check=True):
        env = dict(os.environ, HIVE_HOME=str(self.home))
        r = subprocess.run(
            [sys.executable, str(HV), *args],
            env=env, capture_output=True, text=True,
        )
        if check and r.returncode != 0:
            raise AssertionError(f"`hv {' '.join(args)}` failed ({r.returncode}):\n{r.stderr}")
        return r

    def entries(self):
        out = []
        for f in sorted(self.journal.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


@pytest.fixture
def hive(tmp_path):
    return Hive(tmp_path)
