"""Not collected by default (the name does not match `test_*.py`): `test_hive_home_sandbox.py` runs this
file by path, in a nested session, to prove the session guard fails a run that writes into the exported
hive — under xdist as well as serially (#160, Fable on #165)."""

import os
from pathlib import Path


def test_writes_into_the_exported_hive_on_purpose():
    # The target comes on its own variable, not from anything the guard computes, so a guard that lost
    # track of the exported hive cannot also make this write miss it.
    target = os.environ.get("HIVE_TEST_PROBE_TARGET")
    assert target, "run only by test_hive_home_sandbox.py, with HIVE_HOME exported to a decoy"
    (Path(target) / ".key-dir").write_text("written on purpose by the guard probe\n")
