"""2.1 plan PR 9 (M7): the `modules` checks of `hv doctor`. One case per failure: a tampered file, a wrong
publisher, an inactive or drifted unit, a revoked device, missing config, a listener that is down, quota near its
limit, a `.modules.json` record that is not an object; `hv doctor` with no modules is unchanged, and the
fleet-contract check does not count a module device as an unreachable node."""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import _keys  # noqa: E402
import hive_modules  # noqa: E402
from test_module_lifecycle import hive, units, _publish, _add, _state, SERVICE, SEED, OTHER_SEED  # noqa: E402,F401
from test_succession import _run, _gov  # noqa: E402


@pytest.fixture
def listener(monkeypatch):
    """Something accepting on a loopback port, standing in for the module API."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    monkeypatch.setenv("HIVE_MODULE_PORT", str(s.getsockname()[1]))
    yield s
    s.close()


def _doctor(home, *extra):
    r = _run(home, "doctor", "--format", "json", *extra, check=False)
    return r, {c["name"]: c for c in json.loads(r.stdout)["checks"]}


def _mod(home, name="demo"):
    r, checks = _doctor(home)
    return r, checks.get(f"modules:{name}")


@pytest.fixture
def good(hive, tmp_path, units, listener):
    pub = _publish(tmp_path / "repo", service=SERVICE)
    _add(hive, tmp_path / "repo", pub)
    return hive, pub


def test_a_healthy_module_is_ok(good):
    home, _ = good
    r, c = _mod(home)
    assert c["status"] == "ok", c
    assert "signed by the pinned publisher" in c["detail"] and "device admitted" in c["detail"]


def test_a_node_without_modules_has_no_module_checks(hive, tmp_path):
    _r, checks = _doctor(hive)
    assert not [n for n in checks if n.startswith("modules")]


def test_a_tampered_file_fails_the_check_and_the_exit_code(good, tmp_path):
    home, _ = good
    (tmp_path / "modules" / "demo" / "run.sh").write_text("#!/bin/sh\necho owned\n")
    r, c = _mod(home)
    assert c["status"] == "fail" and "manifest not trusted" in c["detail"] and "run.sh" in c["detail"]
    assert r.returncode == 1


def test_a_bad_signature_fails(good, tmp_path):
    home, _ = good
    (tmp_path / "modules" / "demo" / "module.json.sig").write_text("AAAA")
    _r, c = _mod(home)
    assert c["status"] == "fail"


def test_a_manifest_signed_by_another_publisher_than_the_pin_fails(good, tmp_path):
    home, _ = good
    rec = _state(home)
    rec["demo"]["publisher"] = _publish_pub(OTHER_SEED)
    (home / ".modules.json").write_text(json.dumps(rec))
    r, c = _mod(home)
    assert c["status"] == "fail" and "pinned" in c["detail"] and r.returncode == 1


def _publish_pub(seed):
    import base64
    import ed25519
    return base64.b64encode(ed25519.pub_from_seed(seed)).decode()


def _shim(tmp_path, monkeypatch, active, enabled=True):
    """A `systemctl` that reports `active` for is-active and succeeds or fails is-enabled, logging every call."""
    log = tmp_path / "systemctl.log"
    sh = tmp_path / "shim" / "systemctl"
    sh.write_text('#!/bin/sh\necho "$*" >> %s\ncase "$*" in *is-active*) echo %s;; esac\n'
                  'case "$*" in *is-enabled*) exit %d;; esac\nexit 0\n' % (log, active, 0 if enabled else 1))
    return lambda: [l.removeprefix("--user ") for l in log.read_text().splitlines()] if log.exists() else []


def test_a_stopped_module_warns_and_fix_leaves_it_alone(good, units, monkeypatch, tmp_path):
    home, _ = good
    calls = _shim(tmp_path, monkeypatch, "inactive")
    _r, c = _mod(home)
    assert c["status"] == "warn" and "stopped; `systemctl --user start hive-module-demo.service`" in c["detail"]
    before = len(calls())
    r = _run(home, "doctor", "--fix", check=False)
    assert not [l for l in calls()[before:] if l.startswith(("restart", "start", "enable"))]
    assert "stopped; `systemctl --user start hive-module-demo.service` to resume (left alone)" in r.stdout


def test_fix_starts_a_failed_unit(good, units, monkeypatch, tmp_path):
    home, _ = good
    calls = _shim(tmp_path, monkeypatch, "failed")
    _r, c = _mod(home)
    assert c["status"] == "warn" and "service is failed" in c["detail"]
    before = len(calls())
    r = _run(home, "doctor", "--fix", check=False)
    assert "restart hive-module-demo.service" in calls()[before:] and "--fix: modules" in r.stdout


def test_fix_starts_an_inactive_unit_that_is_not_enabled(good, units, monkeypatch, tmp_path):
    home, _ = good
    calls = _shim(tmp_path, monkeypatch, "inactive", enabled=False)
    _r, c = _mod(home)
    assert c["status"] == "warn" and "inactive and not enabled" in c["detail"]
    before = len(calls())
    _run(home, "doctor", "--fix", check=False)
    assert "enable hive-module-demo.service" in calls()[before:] and "restart hive-module-demo.service" in calls()[before:]


def _acts(calls, before):
    return [l for l in calls()[before:] if l.startswith(("restart", "start", "stop", "enable", "disable", "kill"))]


def test_fix_re_renders_a_drifted_stopped_unit_and_enables_it_without_starting(good, units, monkeypatch, tmp_path):
    home, _ = good
    unit_dir, _c = units
    calls = _shim(tmp_path, monkeypatch, "inactive")
    f = unit_dir / "hive-module-demo.service"
    good_text = f.read_text()
    f.write_text(good_text.replace("Restart=always", "Restart=no"))
    before = len(calls())
    r = _run(home, "doctor", "--fix", check=False)
    assert f.read_text() == good_text
    acts = _acts(calls, before)
    assert "enable hive-module-demo.service" in acts and not [a for a in acts if a.startswith(("restart", "start"))]
    assert "unit enabled, left stopped" in r.stdout and "unit started" not in r.stdout


def test_fix_only_enables_a_running_unit_that_is_not_enabled(good, units, monkeypatch, tmp_path):
    home, _ = good
    calls = _shim(tmp_path, monkeypatch, "active", enabled=False)
    pid = tmp_path / "mainpid"
    pid.write_text("4242")
    sh = tmp_path / "shim" / "systemctl"
    # a MainPID that changes the moment anything is restarted or started
    sh.write_text('#!/bin/sh\necho "$*" >> %s\ncase "$*" in *is-active*) echo active;; *is-enabled*) exit 1;;\n'
                  '*restart*|*" start "*) echo 9999 > %s;; *MainPID*) cat %s;; esac\nexit 0\n'
                  % (tmp_path / "systemctl.log", pid, pid))
    before = len(calls())
    r = _run(home, "doctor", "--fix", check=False)
    acts = _acts(calls, before)
    assert "enable hive-module-demo.service" in acts
    assert not [a for a in acts if a.startswith(("restart", "start", "stop", "kill", "disable"))]
    assert pid.read_text().strip() == "4242"                   # MainPID unchanged: the process was never restarted
    assert "enabled (was running, not enabled)" in r.stdout


@pytest.mark.parametrize("how", ["no-runtime-dir", "manager-offline"])
@pytest.mark.parametrize("dry", [False, True])
def test_without_a_user_systemd_manager_fix_skips_every_unit_action_and_says_so_once(
        good, units, monkeypatch, tmp_path, how, dry):
    home, _ = good
    unit_dir, _c = units
    calls = _shim(tmp_path, monkeypatch, "failed")
    if how == "no-runtime-dir":
        monkeypatch.delenv("XDG_RUNTIME_DIR")
    else:
        sh = tmp_path / "shim" / "systemctl"
        sh.write_text(sh.read_text().replace("case", 'case "$*" in *is-system-running*) echo offline; exit 1;; esac\ncase', 1))
    f = unit_dir / "hive-module-demo.service"
    drifted = f.read_text().replace("Restart=always", "Restart=no")
    f.write_text(drifted)
    before = len(calls())
    r = _run(home, "doctor", "--fix", *(["--dry-run"] if dry else []), check=False)
    assert r.stdout.count("no user systemd: module units not managed here") == 1
    assert f.read_text() == drifted                            # nothing written
    assert not _acts(calls, before)                            # nothing started, enabled or reloaded
    assert not any(w in r.stdout for w in ("re-rendered", "would re-render", "would start", "would enable", "unit started"))


def test_fix_writes_through_the_stop_first_path_when_a_daemon_becomes_a_timer(good, units, tmp_path):
    home, _ = good
    unit_dir, calls = units
    repo = tmp_path / "repo"
    _publish(repo, version="1.1.0", service={"command": ["run.sh"], "interval": 60})
    # the installed tree is the timer version, its units still the daemon's: drift `fix` must repair
    for f in ("module.json", "module.json.sig"):
        (tmp_path / "modules" / "demo" / f).write_bytes((repo / f).read_bytes())
    r = _run(home, "doctor", "--fix", check=False)
    assert "--fix: modules" in r.stdout
    assert (unit_dir / "hive-module-demo.timer").exists()
    log = calls()
    assert log.index("disable --now hive-module-demo.service") < log.index("restart hive-module-demo.timer")


def test_a_drifted_unit_warns_and_fix_re_renders_it_only_when_the_manifest_verifies(good, units, tmp_path):
    home, _ = good
    unit_dir, _calls = units
    f = unit_dir / "hive-module-demo.service"
    good_text = f.read_text()
    f.write_text(good_text.replace("Restart=always", "Restart=no"))
    _r, c = _mod(home)
    assert c["status"] == "warn" and "differs from what the manifest renders" in c["detail"]
    _run(home, "doctor", "--fix", "--dry-run", check=False)
    assert "Restart=no" in f.read_text()                      # a dry run changes nothing
    _run(home, "doctor", "--fix", check=False)
    assert f.read_text() == good_text
    # a tampered module is not re-rendered: its manifest is not trusted
    f.write_text("[Service]\nExecStart=/bin/true\n")
    (tmp_path / "modules" / "demo" / "run.sh").write_text("#!/bin/sh\necho owned\n")
    _run(home, "doctor", "--fix", check=False)
    assert f.read_text() == "[Service]\nExecStart=/bin/true\n"


def test_a_missing_unit_warns(good, units):
    home, _ = good
    unit_dir, _ = units
    (unit_dir / "hive-module-demo.service").unlink()
    _r, c = _mod(home)
    assert c["status"] == "warn" and "is not installed" in c["detail"]


def test_a_revoked_device_warns_and_fix_never_readmits_it(good, units):
    home, _ = good
    dev = _state(home)["demo"]["device_id"]
    _run(home, "group", "revoke", dev)
    _r, c = _mod(home)
    assert c["status"] == "warn" and "revoked" in c["detail"]
    _run(home, "doctor", "--fix", check=False)
    _, gov = _gov(home)
    assert dev not in gov["admitted"]


def test_missing_and_ill_typed_config_warn(good, tmp_path):
    home, _ = good
    cfg = tmp_path / "modules" / "demo" / "config"
    cfg.write_text("{}")
    _r, c = _mod(home)
    assert c["status"] == "warn" and "config lacks poll" in c["detail"]
    cfg.write_text('{"poll": 30}')
    _r, c = _mod(home)
    assert "must be strings" in c["detail"] and "poll" in c["detail"]
    cfg.write_text("[1]")
    _r, c = _mod(home)
    assert "not an object" in c["detail"]
    cfg.unlink()
    _r, c = _mod(home)
    assert "config file is missing" in c["detail"]


def test_a_listener_that_is_down_warns(good, listener):
    home, _ = good
    listener.close()
    _r, c = _mod(home)
    assert c["status"] == "warn" and "module API is not listening" in c["detail"]


def test_quota_near_a_limit_warns(good):
    home, _ = good
    dev = _state(home)["demo"]["device_id"]
    import time
    now = time.time()
    (home / ".module-quota.json").write_text(json.dumps({dev: [now - 5 * i for i in range(50)]}))   # 50 of 60/h
    _r, c = _mod(home)
    assert c["status"] == "warn" and "per_hour 50/60" in c["detail"]


def test_a_record_that_is_not_an_object_is_reported_not_a_crash(good):
    home, _ = good
    rec = _state(home)
    rec["demo"] = "oops"
    rec["other"] = [1]
    (home / ".modules.json").write_text(json.dumps(rec))
    r, checks = _doctor(home)
    assert checks["modules:demo"]["status"] == "fail" and "not an object" in checks["modules:demo"]["detail"]
    assert checks["modules:other"]["status"] == "fail"
    assert "check error" not in json.dumps(checks)


def test_a_state_file_that_is_not_an_object_or_not_json_warns(good):
    home, _ = good
    for text in ("[1]", "{nope"):
        (home / ".modules.json").write_text(text)
        _r, checks = _doctor(home)
        assert checks["modules"]["status"] == "warn" and ".modules.json" in checks["modules"]["detail"]


def test_a_directory_the_state_does_not_know_warns(good, tmp_path):
    home, _ = good
    (tmp_path / "modules" / "stray").mkdir()
    _r, checks = _doctor(home)
    d = checks["modules"]["detail"]
    assert "stray" in d and "not managed by this hive" in d and "delete" not in d


def test_a_platform_without_a_backend_says_so(good, monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "1")
    home, _ = good
    _r, checks = _doctor(home)
    assert checks["modules"]["detail"] == hive_modules.NOT_SUPPORTED


def test_a_module_without_a_service_is_not_asked_for_a_unit(hive, tmp_path, units, listener):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    _r, c = _mod(hive)
    assert c["status"] == "ok", c


# ── the fleet-contract check ────────────────────────────────────────────────────────────────────────

def test_fleet_contract_does_not_count_a_module_device_as_unreachable(good):
    home, _ = good
    m, gov = _gov(home)
    dev = _state(home)["demo"]["device_id"]
    assert dev in gov["admitted"] and dev in gov["modules"]
    fc = m._fleet_contract(gov, {}, {})
    assert dev not in fc["unverified"] and dev not in [d for d, _c in fc["behind"]]
    gov["modules"].pop(dev)                                        # the guard is the marker, not the key
    assert dev in m._fleet_contract(gov, {}, {})["unverified"]


def test_a_node_on_an_old_contract_is_warned_about_when_elections_are_on(good, monkeypatch):
    home, _ = good
    m, gov = _gov(home)
    import hive_module_doctor
    gov["config"]["quorum_m"] = 2
    gov["admitted"].add("peer-old")
    out = hive_module_doctor.checks(m, gov, [], probe=lambda: ({"peer-old": "http://x"}, {"peer-old": "2.0"}))
    c = next(c for c in out if c["name"] == "modules-contract")
    assert c["status"] == "warn" and "peer-old"[:11] in c["detail"] and "2.0" in c["detail"]
    gov["config"]["quorum_m"] = 0
    out = hive_module_doctor.checks(m, gov, [], probe=lambda: ({"peer-old": "http://x"}, {"peer-old": "2.0"}))
    assert not [c for c in out if c["name"] == "modules-contract"]


def test_the_data_plane_imports_the_doctor_but_neither_it_nor_hive_modules_reach_the_owner_key():
    import ast
    for f in ("hive_module_doctor.py", "hive_modules.py"):
        got = {a.name.split(".")[0] for n in ast.walk(ast.parse((PROJECT / f).read_text()))
               if isinstance(n, ast.Import) for a in n.names}
        got |= {n.module.split(".")[0] for n in ast.walk(ast.parse((PROJECT / f).read_text()))
                if isinstance(n, ast.ImportFrom) and n.module}
        assert "ownerkey" not in got and "hivemind_owner" not in got, f


def test_plain_doctor_warns_that_an_active_unit_is_not_enabled(good, units, monkeypatch, tmp_path):
    home, _ = good
    _shim(tmp_path, monkeypatch, "active", enabled=False)
    _r, c = _mod(home)
    assert c["status"] == "warn" and "active but not enabled" in c["detail"]
    _shim(tmp_path, monkeypatch, "active", enabled=True)
    assert _mod(home)[1]["status"] == "ok"


def test_dry_run_says_would_enable_for_a_running_unit_that_is_not_enabled(good, units, monkeypatch, tmp_path):
    home, _ = good
    calls = _shim(tmp_path, monkeypatch, "active", enabled=False)
    before = len(calls())
    r = _run(home, "doctor", "--fix", "--dry-run", check=False)
    assert "would enable hive-module-demo.service" in r.stdout and "would start" not in r.stdout
    assert not _acts(calls, before)


def test_fix_counts_an_activating_unit_as_running_and_only_enables_it(good, units, monkeypatch, tmp_path):
    home, _ = good
    calls = _shim(tmp_path, monkeypatch, "activating", enabled=False)
    before = len(calls())
    r = _run(home, "doctor", "--fix", check=False)
    acts = _acts(calls, before)
    assert "enable hive-module-demo.service" in acts and not [a for a in acts if a.startswith(("restart", "start"))]
    assert "enabled (was running, not enabled)" in r.stdout


@pytest.mark.parametrize("word,rc,expected", [("degraded", 1, True), ("running", 0, True), ("offline", 1, False)])
def test_user_systemd_accepts_a_degraded_manager(tmp_path, monkeypatch, word, rc, expected):
    """A failed user unit makes the manager `degraded` (exit 1), and it still manages units."""
    sh = tmp_path / "bin" / "systemctl"
    sh.parent.mkdir()
    sh.write_text(f"#!/bin/sh\necho {word}\nexit {rc}\n")
    sh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{sh.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert hive_modules.user_systemd() is expected


def _hooked(tmp_path, home, files, hook_mode=None, **kw):
    pub = _publish(tmp_path / "repo", files=files, **kw)
    _add(home, tmp_path / "repo", pub)
    if hook_mode is not None:
        (tmp_path / "modules" / "demo" / "hooks" / "stop").chmod(hook_mode)


STOP = {"run.sh": "#!/bin/sh\nexit 0\n", "hooks/stop": "#!/bin/sh\nexit 0\n"}


def test_a_healthy_hook_adds_no_warning(hive, tmp_path, listener):
    _hooked(tmp_path, hive, STOP)
    assert _mod(hive)[1]["status"] == "ok"


@pytest.mark.parametrize("mode,why", [(0o644, "not executable"), (0o775, "writable by group or other"),
                                      (0o757, "writable by group or other")])
def test_a_hook_the_dispatcher_would_skip_warns_and_says_why(hive, tmp_path, listener, mode, why):
    _hooked(tmp_path, hive, STOP, hook_mode=mode)
    _r, c = _mod(hive)
    assert c["status"] == "warn" and "hook stop: the dispatcher skips it" in c["detail"] and why in c["detail"], c


def test_a_hook_that_became_a_link_fails_the_manifest_check(hive, tmp_path, listener):
    _hooked(tmp_path, hive, STOP)
    p = tmp_path / "modules" / "demo" / "hooks" / "stop"
    p.unlink()
    p.symlink_to("/bin/true")
    c = _mod(hive)[1]
    assert c["status"] == "fail" and "stop" in c["detail"]


def test_a_tampered_hook_fails(hive, tmp_path, listener):
    _hooked(tmp_path, hive, STOP)
    (tmp_path / "modules" / "demo" / "hooks" / "stop").write_text("#!/bin/sh\necho owned\n")
    c = _mod(hive)[1]
    assert c["status"] == "fail" and "does not match its manifest digest" in c["detail"]


def test_a_hook_the_manifest_does_not_list_fails(hive, tmp_path, listener):
    _hooked(tmp_path, hive, STOP)
    p = tmp_path / "modules" / "demo" / "hooks" / "precompact"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    c = _mod(hive)[1]
    assert c["status"] == "fail" and "hooks/precompact" in c["detail"]


def test_an_event_the_dispatcher_does_not_know_warns(hive, tmp_path, listener):
    _hooked(tmp_path, hive, {**STOP, "hooks/bogus": "#!/bin/sh\nexit 0\n"})
    c = _mod(hive)[1]
    assert c["status"] == "warn" and "hook bogus: not an event the dispatcher knows" in c["detail"], c


def test_hooks_behind_a_linked_directory_fail(hive, tmp_path, listener):
    _hooked(tmp_path, hive, STOP)
    hooks = tmp_path / "modules" / "demo" / "hooks"
    real = tmp_path / "elsewhere"
    hooks.rename(real)
    hooks.symlink_to(real)
    c = _mod(hive)[1]
    assert c["status"] == "fail" and "manifest not trusted" in c["detail"], c
