"""#522: a session without XDG_RUNTIME_DIR (Tailscale SSH runs `bash -c` with no PAM session) gets the account's own
/run/user/<uid>, and only the account's own login does: a decoy HOME (every module test suite) gets none, so it can never
reach the real user systemd manager. The fallback reaches `user_systemd`, the owner agent's socket path, a module's
environment, `narrow_env` and the rendered launcher."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import hive_modules  # noqa: E402
import hive_runtime  # noqa: E402
import owner_agent  # noqa: E402

LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/run/user is Linux's")


@pytest.fixture
def login(tmp_path, monkeypatch):
    """This account's login, seen from a session with no XDG_RUNTIME_DIR: HOME is the account's home, and RUN_USER
    points at a private stand-in for /run/user."""
    home = tmp_path / "home"
    home.mkdir()
    run = tmp_path / "run"
    (run / str(os.getuid())).mkdir(parents=True, mode=0o700)
    os.chmod(run / str(os.getuid()), 0o700)
    monkeypatch.setattr(hive_runtime, "RUN_USER", str(run))
    monkeypatch.setattr(hive_runtime, "_account_home", lambda: str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "x")       # recorded first, so whatever `ensure` writes is undone after the test
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    return home, run / str(os.getuid())


def test_the_accounts_own_login_gets_its_runtime_dir(login):
    _home, d = login
    assert hive_runtime.fallback() == str(d) and hive_runtime.runtime_dir() == str(d)


def test_a_decoy_home_gets_none(login, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "decoy" / "user"))
    assert hive_runtime.fallback() is None and hive_runtime.ensure() is None
    assert "XDG_RUNTIME_DIR" not in os.environ


def test_a_shared_a_symlinked_or_a_missing_dir_is_refused(login, tmp_path, monkeypatch):
    _home, d = login
    os.chmod(d, 0o755)
    assert hive_runtime.fallback() is None
    os.chmod(d, 0o700)
    d.rename(d.with_name("real"))
    d.symlink_to(d.with_name("real"))
    assert hive_runtime.fallback() is None
    d.unlink()
    assert hive_runtime.fallback() is None


def test_a_set_xdg_runtime_dir_always_wins_and_ensure_sets_only_an_unset_one(login, monkeypatch):
    _home, d = login
    env = {"HOME": os.environ["HOME"], "XDG_RUNTIME_DIR": "/elsewhere"}
    assert hive_runtime.ensure(env) == "/elsewhere" and env["XDG_RUNTIME_DIR"] == "/elsewhere"
    env = {"HOME": os.environ["HOME"]}
    assert hive_runtime.ensure(env) == str(d) and env["XDG_RUNTIME_DIR"] == str(d)
    assert hive_runtime.ensure({"HOME": os.environ["HOME"], "XDG_RUNTIME_DIR": "relative"}) == str(d)


def test_user_systemd_with_a_decoy_home_never_runs_systemctl(tmp_path, monkeypatch):
    """The property every module test suite relies on: unset XDG_RUNTIME_DIR and a decoy HOME mean no user manager."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "x")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "decoy"))
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    assert hive_modules.user_systemd() is False and called == []
    assert "XDG_RUNTIME_DIR" not in os.environ


def test_user_systemd_in_the_accounts_login_asks_the_manager_with_the_runtime_dir(login, monkeypatch):
    _home, d = login
    seen = []

    def run(argv, **kw):
        seen.append((argv, os.environ.get("XDG_RUNTIME_DIR")))
        return subprocess.CompletedProcess(argv, 0, "running\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert hive_modules.user_systemd() is True
    assert seen == [(["systemctl", "--user", "is-system-running"], str(d))]


def test_the_owner_agent_socket_dir_follows_the_login(login):
    _home, d = login
    assert owner_agent._runtime_dir() == str(d / "hive-mind")
    assert owner_agent._runtime_dir({"HOME": "/decoy", "TMPDIR": "/t"}) == f"/t/hive-mind-{os.getuid()}"


def test_narrow_env_carries_the_runtime_dir_only_when_there_is_one(login, tmp_path, monkeypatch):
    _home, d = login
    env = hive_modules.narrow_env("demo", {}, tmp_path / "keys")
    assert env["XDG_RUNTIME_DIR"] == str(d)
    monkeypatch.setenv("HOME", str(tmp_path / "decoy"))
    assert "XDG_RUNTIME_DIR" not in hive_modules.narrow_env("demo", {}, tmp_path / "keys")


@LINUX_ONLY
@pytest.mark.parametrize("home_is_login, xdg, expect", [
    (True, None, "fallback"), (False, None, ""), (True, "/set/by/pam", "/set/by/pam")])
def test_the_launcher_lines_set_the_fallback_only_for_the_login(login, tmp_path, home_is_login, xdg, expect):
    home, d = login
    script = "\n".join(hive_runtime.launcher_lines() + ['printf %s "${XDG_RUNTIME_DIR:-}"'])
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home) if home_is_login else str(tmp_path / "decoy")}
    if xdg:
        env["XDG_RUNTIME_DIR"] = xdg
    out = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True).stdout
    assert out == (str(d) if expect == "fallback" else expect)


def test_a_home_with_a_quote_renders_no_launcher_line(monkeypatch):
    monkeypatch.setattr(hive_runtime, "_account_home", lambda: "/home/o'brien")
    assert hive_runtime.launcher_lines() == []


@LINUX_ONLY
def test_a_rendered_launcher_gives_the_module_the_logins_runtime_dir(login, tmp_path, monkeypatch):
    home, d = login
    monkeypatch.setenv("HIVE_MODULES_DIR", str(tmp_path / "modules"))
    mod = tmp_path / "modules" / "demo"
    mod.mkdir(parents=True)
    (mod / "cli.sh").write_text('#!/bin/sh\nprintf %s "${XDG_RUNTIME_DIR:-}"\n')
    (mod / "cli.sh").chmod(0o755)
    lib = type("Lib", (), {"_ensure_key_dir": staticmethod(lambda: str(tmp_path / "keys"))})
    manifest = {"name": "demo", "cli": {"name": "democtl", "command": ["cli.sh"]}, "files": {"cli.sh": "x"}}
    launcher = tmp_path / "democtl"
    launcher.write_text(hive_modules.launcher_text(lib, "demo", manifest, "/usr/bin:/bin"))
    launcher.chmod(0o755)
    assert hive_modules._own_launcher(launcher, "demo")
    run = lambda env: subprocess.run([str(launcher)], env=env, capture_output=True, text=True).stdout   # noqa: E731
    assert run({"HOME": str(home)}) == str(d)
    assert run({"HOME": str(tmp_path / "decoy")}) == ""


def test_narrow_env_gives_a_confined_service_the_units_runtime_dir(login, tmp_path):
    _home, d = login
    svc = {"service": {"command": ["x"]}}
    confined = {**svc, "trust": "confined"}
    keys = tmp_path / "keys"
    assert hive_modules.narrow_env("demo", {}, keys, confined)["HIVE_MODULE_RUNTIME_DIR"] == f"{d}/hive-mind-mod-demo"
    assert hive_modules.narrow_env("demo", {"trust_local": "confined"}, keys, svc)["HIVE_MODULE_RUNTIME_DIR"] == f"{d}/hive-mind-mod-demo"
    for manifest in (svc, {"trust": "confined"}, None):
        assert "HIVE_MODULE_RUNTIME_DIR" not in hive_modules.narrow_env("demo", {}, keys, manifest)
