"""#170: a smoke run by hand writes nothing into the caller's account.

Under pytest the `isolation` fixture used to hide the problem: it set `HIVE_IDENTITY_STASH` and `HOME` per
test, so the smokes looked clean. Run from a shell, `owner init` stashed a throwaway owner key into the real
`~/.config/hive-mind/identity`, and every hive's key directory landed in the real `~/.hive/keys`. So these
tests run each smoke the way a person would, with nothing set: HOME is a stand-in for the real home, the
stash variable is unset, and `HIVE_KEY_DIR` is exported the way an operator who relocated their keys has it.
The stand-in must be byte-for-byte empty afterwards.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
COMMON = PROJECT / "scripts" / "common"
SMOKES = ("smoke.sh", "sync_smoke.sh", "sync_auth_smoke.sh")


def _files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl required for daemon readiness")
@pytest.mark.parametrize("smoke", SMOKES)
def test_a_smoke_run_by_hand_leaves_the_callers_home_alone(tmp_path, smoke):
    home = tmp_path / "real-home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), HIVE_KEY_DIR=str(home / "relocated-keys"))
    for var in ("HIVE_IDENTITY_STASH", "CLAUDE_CONFIG_DIR", "HIVE_HOME"):
        env.pop(var, None)
    r = subprocess.run(["bash", str(COMMON / smoke)], env=env, capture_output=True, text=True, timeout=180,
                       cwd=tmp_path)
    assert r.returncode == 0, f"{smoke} failed:\n{r.stdout}\n{r.stderr}"
    assert _files(home) == [], f"{smoke} wrote into the caller's home: {_files(home)}"


@pytest.mark.parametrize("smoke", SMOKES)
def test_every_smoke_isolates_before_its_first_hv_call(smoke):
    """The static half, cheap enough to fail first: the isolation call precedes every `$HV` use, so no hv
    process ever starts with the caller's HOME."""
    lines = (COMMON / smoke).read_text().splitlines()
    iso = next((i for i, l in enumerate(lines) if re.match(r"\s*smoke_isolate\b", l)), None)
    assert iso is not None, f"{smoke} never calls smoke_isolate"
    first_hv = next(i for i, l in enumerate(lines)
                    if '"$HV"' in l and not l.lstrip().startswith("#") and not re.match(r"\s*\w+\(\)", l))
    assert iso < first_hv, f"{smoke}: smoke_isolate (line {iso + 1}) comes after the first hv call ({first_hv + 1})"


def test_smoke_isolate_overrides_what_the_caller_exported(tmp_path):
    out = subprocess.run(
        ["bash", "-c", f'. "{COMMON / "_smoke_isolate.sh"}"; smoke_isolate "$1"; '
                       'printf "%s\\n" "$HOME" "$HIVE_IDENTITY_STASH" "$CLAUDE_CONFIG_DIR" "${HIVE_KEY_DIR-unset}"',
         "_", str(tmp_path)],
        env=dict(os.environ, HOME="/nonexistent-real-home", HIVE_IDENTITY_STASH="/real/stash",
                 CLAUDE_CONFIG_DIR="/real/.claude", HIVE_KEY_DIR="/real/keys"),
        capture_output=True, text=True, check=True).stdout.split("\n")
    assert out[:4] == [f"{tmp_path}/home", f"{tmp_path}/home/.config/hive-mind/identity",
                       f"{tmp_path}/home/.claude", "unset"]
