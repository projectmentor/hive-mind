"""2.0 (#136): a build without its bundled crypto refuses to run, instead of honouring entries it cannot
verify (through 1.x only `hv doctor` failed on it). Proven on a copy of the build with one crypto module
missing: every entry point, the CLI, the control plane and the daemon's library import, exits 1 and says why."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


def _stripped(tmp_path, missing):
    build = tmp_path / "build"
    build.mkdir()
    for f in PROJECT.iterdir():
        if f.is_file() and (f.suffix == ".py" or f.name == "hv") and f.name != missing:
            shutil.copy2(f, build / f.name)
    return build


@pytest.mark.parametrize("missing", ["ed25519.py", "x25519.py", "chacha20poly1305.py"])
@pytest.mark.parametrize("entry", [["hv", "version"], ["hivemind_ctl.py", "owner", "init"],
                                   ["-c", "import sync_common; sync_common.load_hv()"]])
def test_a_build_missing_its_crypto_refuses_to_run(tmp_path, missing, entry):
    build = _stripped(tmp_path, missing)
    argv = [sys.executable] + ([str(build / entry[0])] + entry[1:] if entry[0] != "-c" else entry)
    r = subprocess.run(argv, cwd=build, capture_output=True, text=True,
                       env={"HIVE_HOME": str(tmp_path / "h"), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "missing its bundled crypto" in r.stderr and missing in r.stderr and "refusing to run" in r.stderr
    assert not (tmp_path / "h" / "journal").exists() or not any((tmp_path / "h" / "journal").iterdir())


def test_the_whole_build_runs(tmp_path):
    build = _stripped(tmp_path, missing=None)
    r = subprocess.run([sys.executable, str(build / "hv"), "version"], cwd=build, capture_output=True, text=True,
                       env={"HIVE_HOME": str(tmp_path / "h"), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
