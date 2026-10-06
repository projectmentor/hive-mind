"""#142: the suite never reads the operator's repo-root `.peers.json`.

A throwaway checkout (never the live one) gets a `.peers.json` with `sync_auth: enforce` at its root and
the real suite fixtures run in a child pytest: the daemon under test must still come up permissive.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

def test_repo_root_peers_json_is_invisible_to_the_suite(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "tests").mkdir(parents=True)
    for f in ("sync_common.py", "hv"):
        shutil.copy(PROJECT / f, checkout / f)
    for f in PROJECT.glob("*.py"):
        shutil.copy(f, checkout / f.name)
    for f in (PROJECT / "tests").glob("*.py"):
        if f.name == "conftest.py" or f.name.startswith("_"):
            shutil.copy(f, checkout / "tests" / f.name)
    (checkout / "tests" / "test_probe.py").write_text(
        "import sync_common\n"
        "def test_probe():\n"
        "    assert sync_common.ROOT.name == 'checkout'\n"
        "    assert (sync_common.ROOT / '.peers.json').exists()\n"
        "    assert sync_common.peers_path() is None\n"
        "    assert sync_common.sync_auth_mode() == 'permissive'\n"
    )
    (checkout / ".peers.json").write_text(json.dumps({"sync_auth": "enforce"}))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_probe.py"],
                       cwd=checkout, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
