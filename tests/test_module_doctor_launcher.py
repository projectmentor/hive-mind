"""`hv doctor` checks a module's CLI launcher (hwatch-dev#146): it must be the core's own and run this install, and a
launcher left behind for a module this hive does not manage, whose directory is gone, is reported. The case that
prompted it: a test with a decoy HOME but the real PATH rewrote the operator's ~/.local/bin/hwatch to run a copy under
/tmp, and left alpha, beta and hcns launchers on other nodes; nothing noticed until someone typed the command."""

import hashlib
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import hive_modules  # noqa: E402
from test_module_doctor import _doctor, _mod, listener  # noqa: E402,F401
from test_module_lifecycle import LINUX_ONLY, _add, hive, units  # noqa: E402,F401
from test_module_runtimes_launcher import CLI, bindir, publish  # noqa: E402,F401
from test_succession import _run  # noqa: E402


def _install(hive, tmp_path, bindir):
    _add(hive, tmp_path / "repo", publish(tmp_path, cli=CLI))
    return bindir / "democtl"


def _render_as(src, dest, module, moddir):
    """A launcher the core would have written for `module` at `moddir`: the leaking test's output, digest and all."""
    body = src.read_text().split("\n", 2)[2].replace(str(hive_modules.modules_dir() / "demo"), str(moddir))
    body = body.replace("HIVE_MODULE_NAME=demo", f"HIVE_MODULE_NAME={module}")
    dest.write_text(f"#!/bin/sh\n{hive_modules.LAUNCHER_MARK} module={module} "
                    f"sha256={hashlib.sha256(body.encode()).hexdigest()}\n{body}")


@LINUX_ONLY
def test_a_launcher_that_runs_this_install_is_ok(hive, tmp_path, units, bindir, listener):
    _install(hive, tmp_path, bindir)
    _r, c = _mod(hive)
    assert c["status"] == "ok", c


@LINUX_ONLY
def test_a_launcher_rewritten_to_run_a_copy_elsewhere_warns_with_the_fix(hive, tmp_path, units, bindir, listener):
    launcher = _install(hive, tmp_path, bindir)
    decoy = tmp_path / "pytest-of-x" / "decoy" / "modules" / "demo"
    _render_as(launcher, launcher, "demo", decoy)
    assert hive_modules.launcher_target(launcher) == ("demo", str(decoy))      # still the core's own, unedited file
    _r, c = _mod(hive)
    assert c["status"] == "warn" and f"runs {decoy}, not this install" in c["detail"]
    assert "hive-mind module update demo" in c["detail"]


@LINUX_ONLY
def test_an_edited_or_missing_launcher_warns(hive, tmp_path, units, bindir, listener):
    launcher = _install(hive, tmp_path, bindir)
    launcher.write_text(launcher.read_text() + "echo extra\n")
    _r, c = _mod(hive)
    assert c["status"] == "warn" and "is not the core's launcher for demo" in c["detail"]
    launcher.unlink()
    _r, c = _mod(hive)
    assert c["status"] == "warn" and "democtl is missing" in c["detail"]


@LINUX_ONLY
def test_a_dead_launcher_is_reported_even_on_a_node_with_no_modules(hive, tmp_path, units, bindir, listener):
    launcher = _install(hive, tmp_path, bindir)
    left = bindir / "alpha"
    _render_as(launcher, left, "alpha", tmp_path / "gone" / "modules" / "alpha")
    _run(hive, "module", "remove", "demo")
    _r, checks = _doctor(hive)
    c = checks["modules"]
    assert c["status"] == "warn" and str(left) in c["detail"] and "module alpha" in c["detail"], c
    assert "safe to delete" in c["detail"]


@LINUX_ONLY
def test_a_live_launcher_of_another_hive_and_foreign_files_are_left_alone(hive, tmp_path, units, bindir, listener):
    launcher = _install(hive, tmp_path, bindir)
    other = tmp_path / "other-hive" / "modules" / "beta"
    other.mkdir(parents=True)                                       # another hive's install, still there
    _render_as(launcher, bindir / "beta", "beta", other)
    (bindir / "tool").write_text("#!/bin/sh\necho mine\n")
    _r, checks = _doctor(hive)
    assert "modules" not in checks and checks["modules:demo"]["status"] == "ok", checks
    assert hive_modules.launcher_target(bindir / "tool") is None
