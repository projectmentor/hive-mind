"""Phase 2 sync test — runs the local two-node convergence harness under pytest.

The heavy lifting lives in scripts/sync_smoke.sh (spins two daemons on
127.0.0.1, seeds distinct data, syncs, asserts convergence + dedup + cross-node
link resolution). This wrapper makes it part of `pytest`. Requires curl.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SMOKE = PROJECT / "scripts" / "sync_smoke.sh"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
def test_two_node_convergence():
    r = subprocess.run([str(SMOKE)], capture_output=True, text=True)
    assert r.returncode == 0, f"sync_smoke.sh failed:\n{r.stdout}\n{r.stderr}"
