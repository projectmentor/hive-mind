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


def test_an_inactive_unit_warns_and_fix_starts_it(good, units, monkeypatch, tmp_path):
    home, _ = good
    unit_dir, calls = units
    shim = tmp_path / "shim" / "systemctl"
    shim.write_text('#!/bin/sh\necho "$*" >> %s\ncase "$*" in *is-active*) echo inactive;; esac\nexit 0\n'
                    % (tmp_path / "systemctl.log"))
    _r, c = _mod(home)
    assert c["status"] == "warn" and "service is inactive" in c["detail"]
    before = len(calls())
    r = _run(home, "doctor", "--fix", check=False)
    assert "restart hive-module-demo.service" in calls()[before:]
    assert "--fix: modules" in r.stdout


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
    assert "stray" in checks["modules"]["detail"] and "not managed" in checks["modules"]["detail"]


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
