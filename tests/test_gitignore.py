"""Runtime data and secrets must never be committable to the public repo (#74).

Asks git itself, so the test follows real .gitignore semantics rather than re-implementing them."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = [
    # the SQLite projection and its side files, which hold hive content
    "store.db", "store.db-wal", "store.db-shm", "store.db-journal",
    "store.db.bak.20260101",
    "journal/k1_0123456789abcdef.jsonl",
    # keys and per-node config
    ".device-key", ".owner-key", "hive-owner-k1_0123456789abcdef.key",
    ".peers.json", "nudge.env",
    # verified peer addresses (#7), and the temp files an atomic rewrite leaves if interrupted
    ".peer_candidates.json", ".peer_candidates.json.a1b2c3.tmp", ".peers.json.a1b2c3.tmp",
    # local-only telemetry, including its own SQLite side files
    ".telemetry/telemetry.db", ".telemetry/telemetry.db-wal",
    # the local bus log, and any log written into the checkout
    ".bus/introspect.log", "hive-sync.log",
    # live cells can carry private infrastructure details
    "cells/prod-site.json",
]


def _git_ok():
    if shutil.which("git") is None:
        return False
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


@pytest.mark.skipif(not _git_ok(), reason="needs git and a checkout")
@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_runtime_data_and_secrets_are_ignored(path):
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path])
    assert r.returncode == 0, f"{path} is not gitignored; it could be committed to the public repo"


@pytest.mark.skipif(not _git_ok(), reason="needs git and a checkout")
def test_example_cells_stay_tracked():
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", "cells/example-x.json"])
    assert r.returncode == 1
