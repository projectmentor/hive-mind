"""Cross-platform supervisor seam (scripts/installer/_service.sh + _launchd.sh).

These run on every CI OS (ubuntu + macos). They exercise the NEW macOS code paths
without needing a Mac: the launchd plists are rendered by bash and validated with
Python's stdlib ``plistlib`` (no ``plutil``), and ``_service_kind`` dispatch is checked
by shimming ``uname`` onto PATH. A golden-parity check proves sourcing ``_service.sh``
leaves the systemd unit writer byte-identical, so the Linux path carries no regression.
"""
import os
import plistlib
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "installer"
LAUNCHD = INSTALLER / "_launchd.sh"
SERVICE = INSTALLER / "_service.sh"
UNITS = INSTALLER / "_units.sh"


def _bash(script, env=None, path_prefix=None):
    """Run a bash snippet; return CompletedProcess. Optionally prepend path_prefix to PATH."""
    e = dict(os.environ)
    if env:
        e.update(env)
    if path_prefix:
        e["PATH"] = f"{path_prefix}:{e['PATH']}"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


def test_launchd_renders_well_formed_plists(tmp_path):
    """_launchd_render writes two plists that parse and carry the systemd-equivalent keys."""
    agents = tmp_path / "agents"
    r = _bash(
        f'. "{LAUNCHD}"; _launchd_render "{REPO}"',
        env={"_LAUNCHD_AGENTS_DIR": str(agents), "_LAUNCHD_LOG_DIR": str(tmp_path / "logs")},
    )
    assert r.returncode == 0, r.stderr

    sync = plistlib.loads((agents / "com.projectmentor.hive-sync.plist").read_bytes())
    assert sync["Label"] == "com.projectmentor.hive-sync"
    assert sync["RunAtLoad"] is True
    assert sync["KeepAlive"] is True          # ≙ systemd Restart=always
    assert sync["ThrottleInterval"] == 5      # ≙ RestartSec=5
    assert sync["ProgramArguments"][-1].endswith("/sync_daemon.py")
    assert sync["EnvironmentVariables"]["HIVE_HOME"] == str(REPO)

    doctor = plistlib.loads((agents / "com.projectmentor.hive-doctor.plist").read_bytes())
    assert doctor["StartInterval"] == 900     # ≙ hive-doctor.timer OnUnitActiveSec=15min
    assert doctor["ProgramArguments"][-2:] == ["doctor", "--fix"]


def test_launchd_render_rejects_bad_hive_dir(tmp_path):
    """A hive_dir without sync_daemon.py must fail loudly rather than write a broken plist."""
    r = _bash(f'. "{LAUNCHD}"; _launchd_render "{tmp_path}"')
    assert r.returncode != 0
    assert "no sync_daemon.py" in r.stderr


def test_service_kind_dispatch():
    """_service_kind returns systemd on this (Linux/CI) host, and launchd when uname says Darwin."""
    here = _bash(f'. "{SERVICE}"; _service_kind')
    assert here.stdout.strip() in ("systemd", "launchd")

    shim = REPO / "tests"  # any dir; we drop a fake `uname` into a temp dir instead
    fake = subprocess.run(["mktemp", "-d"], capture_output=True, text=True).stdout.strip()
    Path(fake, "uname").write_text("#!/bin/sh\necho Darwin\n")
    os.chmod(Path(fake, "uname"), 0o755)
    darwin = _bash(
        f'. "{SERVICE}"; _service_kind; type launchd_install >/dev/null 2>&1 && echo BACKEND',
        path_prefix=fake,
    )
    assert darwin.stdout.split()[0] == "launchd"
    assert "BACKEND" in darwin.stdout, "launchd backend must be sourced on Darwin"


def test_systemd_writer_byte_identical_via_service():
    """Sourcing _service.sh must not alter write_systemd_units — the Linux path stays verbatim."""
    direct = _bash(f'. "{UNITS}"; declare -f write_systemd_units')
    viaserv = _bash(f'. "{SERVICE}"; declare -f write_systemd_units')
    assert direct.returncode == 0 and viaserv.returncode == 0
    assert direct.stdout == viaserv.stdout
    assert "write_systemd_units" in direct.stdout
