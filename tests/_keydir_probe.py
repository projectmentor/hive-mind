"""Inner probe for test_keydir_isolation.py; run by it in a child pytest, never collected on its own.
It mints a key at import (as a module-level loader would) and again inside a test."""
import os
import subprocess
import sys

from conftest import HV


def _mint():
    return subprocess.run([sys.executable, str(HV), "config", "identity", "init"], env=dict(os.environ),
                          capture_output=True, text=True)


assert _mint().returncode == 0
# Something re-exports it after conftest's import-time pop (a plugin, a sourced script): `isolation` must still clear it.
os.environ["HIVE_KEY_DIR"] = os.environ["PROBE_KEY_DIR"]


def test_mints_a_key_in_a_test():
    assert "HIVE_KEY_DIR" not in os.environ
    assert _mint().returncode == 0
