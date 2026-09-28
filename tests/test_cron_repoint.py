"""2.0 removes the `sync_daemon.py` shim (#136), and `hive-mind update` repoints the one place that could
still name it: the installer's @reboot cron fallback, which update never rewrote. Driven with a fake
`crontab` backed by a file, so the real one is never read or written."""

import os
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
LIB = PROJECT / "scripts" / "installer" / "_service.sh"

if os.name == "nt":
    pytest.skip("bash and crontab", allow_module_level=True)


def _fake_crontab(tmp_path, content):
    state = tmp_path / "crontab.txt"
    if content is not None:
        state.write_text(content)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "crontab"
    fake.write_text(f'''#!/bin/sh
state="{state}"
if [ "$1" = "-l" ]; then [ -f "$state" ] && cat "$state" || exit 1; exit 0; fi
if [ "$1" = "-" ]; then cat > "$state"; exit 0; fi
exit 2
''')
    fake.chmod(0o755)
    return state, bin_dir


def _repoint(bin_dir):
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", f'. "{LIB}"; cron_repoint_legacy_daemon'], env=env,
                          capture_output=True, text=True)


def test_an_old_cron_line_is_repointed_and_the_rest_kept(tmp_path):
    old = ("MAILTO=\"\"\n"
           "@reboot HIVE_HOME=/h /usr/bin/python3 /h/sync_daemon.py >> /tmp/hive-sync.log 2>&1\n"
           "0 3 * * * /usr/local/bin/backup.sh\n"
           "0 4 * * * /opt/other/sync_daemon.py --unrelated\n")         # not ours: no HIVE_HOME= (#172)
    state, bin_dir = _fake_crontab(tmp_path, old)
    r = _repoint(bin_dir)
    assert r.returncode == 0 and "repointed the @reboot cron entry" in r.stdout
    assert state.read_text() == old.replace("/h/sync_daemon.py", "/h/hive_sync_daemon.py")


@pytest.mark.parametrize("content", [
    "@reboot HIVE_HOME=/h /usr/bin/python3 /h/hive_sync_daemon.py >> /tmp/hive-sync.log 2>&1\n",   # current
    "0 3 * * * /usr/local/bin/backup.sh\n",                                                           # unrelated
    "0 3 * * * /opt/other/sync_daemon.py --unrelated\n",             # another program's sync_daemon.py (#172)
    None,                                                                                              # no crontab
])
def test_nothing_else_is_touched(tmp_path, content):
    state, bin_dir = _fake_crontab(tmp_path, content)
    before = state.read_text() if content is not None else None
    r = _repoint(bin_dir)
    assert r.returncode == 0 and r.stdout == ""
    assert (state.read_text() if state.exists() else None) == before


def test_the_shim_is_gone_and_update_calls_the_repoint():
    assert not (PROJECT / "sync_daemon.py").exists()
    update = (PROJECT / "scripts" / "installer" / "_update.sh").read_text()
    assert "cron_repoint_legacy_daemon" in update
