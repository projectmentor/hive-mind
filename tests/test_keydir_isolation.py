"""#178: an exported HIVE_KEY_DIR never receives a key minted by the suite.

Runs a child pytest with HIVE_KEY_DIR exported to a temp directory; the probe mints a key at import and in a
test. Dropping conftest's import-time pop fails the first mint; the probe then re-exports it, and dropping the
`isolation` delenv fails the second.
"""
import os
import subprocess
import sys

from conftest import PROJECT


def test_exported_hive_key_dir_stays_untouched(tmp_path):
    kd = tmp_path / "real-keys"
    kd.mkdir()
    env = dict(os.environ, HIVE_KEY_DIR=str(kd), PROBE_KEY_DIR=str(kd))
    env.pop("PYTEST_XDIST_WORKER", None)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                        str(PROJECT / "tests" / "_keydir_probe.py")],
                       env=env, capture_output=True, text=True, cwd=PROJECT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert list(kd.iterdir()) == [], "a key was minted into the exported HIVE_KEY_DIR"
