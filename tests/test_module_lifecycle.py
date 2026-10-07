"""2.1 plan PR 6 (M1): `hive-mind module add | remove | update | list | quota`. Each security check has a test that
fails without it: the signature, the digests, the exact file set, the publisher pin, the platform check that runs
before anything is fetched or minted, and the quota ceiling. Drives the real `hive-mind` CLI against a module
repository built in a temp git repo."""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import _keys  # noqa: E402
import ed25519  # noqa: E402
import hive_modules  # noqa: E402
from test_succession import _run, _gov, _device_id  # noqa: E402

LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="2.1 module management is Linux-only")

SEED = bytes(range(1, 33))
OTHER_SEED = bytes(range(101, 133))


def _b64(b):
    return base64.b64encode(b).decode()


def _publish(repo, name="demo", version="1.0.0", seed=SEED, extra=None, files=None, tamper=None, symlink=None,
             unlisted=None, sign_with=None, **manifest_extra):
    """Write a signed module into the git repo `repo` and commit it. `tamper` edits a file after the manifest was
    signed; `unlisted` adds a tracked file the manifest does not name; `sign_with` signs with another key."""
    repo.mkdir(parents=True, exist_ok=True)
    body = files or {"run.sh": "#!/bin/sh\nexit 0\n", "hooks/stop": "#!/bin/sh\nexit 0\n"}
    for rel, text in body.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
        (repo / rel).chmod(0o755)
    m = {"name": name, "version": version, "min_core": "2.0", "publisher": _b64(ed25519.pub_from_seed(seed)),
         "files": {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in body.items()},
         "hooks": ["stop"], "config": {"poll": "30"}, **manifest_extra}
    raw = json.dumps(m, indent=1).encode()
    (repo / "module.json").write_bytes(raw)
    (repo / "module.json.sig").write_text(_b64(ed25519.sign(raw, sign_with or seed)))
    if tamper:
        (repo / tamper).write_text("#!/bin/sh\necho owned\n")
    if unlisted:
        (repo / unlisted).write_text("stray")
    if symlink:
        (repo / symlink).unlink(missing_ok=True)
        os.symlink("/etc/passwd", repo / symlink)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A", "-f"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m"],
                   check=True)
    return _b64(ed25519.pub_from_seed(seed))


@pytest.fixture
def hive(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_MODULES_DIR", str(tmp_path / "modules"))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    home = tmp_path / "A"
    _run(home, "config", "identity", "init")
    _run(home, "owner", "init")
    _run(home, "group", "admit", _device_id(home), "--principal", "op")
    return home


def _add(home, repo, pub, name="demo", check=True, *extra):
    return _run(home, "module", "add", name, "--from", str(repo), "--publisher", pub, *extra, check=check)


def _state(home):
    return json.loads((home / ".modules.json").read_text())


def _key_dir_has_module(home, name="demo"):
    return (_keys.key_dir(home) / "modules" / name).exists()


@LINUX_ONLY
def test_add_installs_verified_files_mints_a_key_and_admits_it_as_a_module(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    r = _add(hive, tmp_path / "repo", pub)
    assert "installed module demo 1.0.0" in r.stdout
    d = tmp_path / "modules" / "demo"
    assert (d / "run.sh").read_text().startswith("#!/bin/sh") and (d / "module.json").exists()
    assert not (d / ".git").exists()
    assert json.loads((d / "config").read_text()) == {"poll": "30"}
    rec = _state(hive)["demo"]
    assert rec["publisher"] == pub and rec["version"] == "1.0.0"
    _, gov = _gov(hive)
    dev = rec["device_id"]
    assert gov["modules"] == {dev: "demo"} and dev in gov["admitted"]
    assert gov["principals"][dev] == "op"                      # the operator's principal: cap_self bounds it
    assert _key_dir_has_module(hive)


@LINUX_ONLY
@pytest.mark.parametrize("how", ["tamper", "unlisted", "symlink", "badsig"])
def test_a_module_that_fails_the_check_installs_nothing(hive, tmp_path, how):
    kw = {"tamper": dict(tamper="run.sh"), "unlisted": dict(unlisted="extra.sh"),
          "symlink": dict(symlink="hooks/stop"), "badsig": dict(sign_with=OTHER_SEED)}[how]
    pub = _publish(tmp_path / "repo", **kw)
    before = sorted(p.read_text() for p in (hive / "journal").glob("*.jsonl"))
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and r.stderr.startswith("hive-mind module:")
    assert not (tmp_path / "modules" / "demo").exists()
    assert not (hive / ".modules.json").exists()
    assert not _key_dir_has_module(hive)                       # nothing was minted
    assert sorted(p.read_text() for p in (hive / "journal").glob("*.jsonl")) == before   # nothing was admitted
    assert _gov(hive)[1]["modules"] == {}


@LINUX_ONLY
def test_the_publisher_must_be_named_or_confirmed(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    r = _run(hive, "module", "add", "demo", "--from", str(tmp_path / "repo"), check=False)    # no tty, no --publisher
    assert r.returncode == 1 and "--publisher" in r.stderr and pub in r.stdout
    other = _b64(ed25519.pub_from_seed(OTHER_SEED))
    r = _run(hive, "module", "add", "demo", "--from", str(tmp_path / "repo"), "--publisher", other, check=False)
    assert r.returncode == 1 and "does not match" in r.stderr
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(hive)


@LINUX_ONLY
def test_a_hook_file_the_manifest_does_not_declare_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", hooks=[])
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "does not declare" in r.stderr and not _key_dir_has_module(hive)


@LINUX_ONLY
def test_a_manifest_for_another_name_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", name="other")
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "not 'demo'" in r.stderr and not _key_dir_has_module(hive)


@LINUX_ONLY
def test_a_module_that_needs_a_newer_core_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", min_core="9.0")
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "9.0" in r.stderr and not (tmp_path / "modules" / "demo").exists()


@LINUX_ONLY
def test_a_manifest_may_ask_for_less_quota_but_never_more(hive, tmp_path):
    pub = _publish(tmp_path / "repo", quota={"per_hour": 600})
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "never more" in r.stderr and not _key_dir_has_module(hive)
    pub = _publish(tmp_path / "repo2", quota={"per_hour": 5})
    _add(hive, tmp_path / "repo2", pub)
    assert _state(hive)["demo"]["quota"] == {"per_hour": 5}




@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_only_linux_has_a_module_backend(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    ok, why = hive_modules.platform_supported()
    assert not ok and "not supported on this platform yet" in why


def test_the_cli_says_not_supported_and_leaves_nothing_behind(hive, tmp_path, monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "1")                     # Termux: a Linux kernel with no module backend
    r = _run(hive, "module", "add", "demo", "--from", str(tmp_path / "no-such-repo"), "--publisher", "x", check=False)
    assert r.returncode == 1 and "not supported on this platform yet" in r.stderr
    assert not (tmp_path / "modules").exists() and not (hive / ".modules.json").exists()
    assert not _key_dir_has_module(hive)
    assert "no-such-repo" not in r.stderr                         # it never tried to fetch


@LINUX_ONLY
def test_remove_revokes_the_device_and_leaves_the_journal(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    dev = _state(hive)["demo"]["device_id"]
    journal = sorted(p.name for p in (hive / "journal").glob("*.jsonl"))
    _run(hive, "module", "remove", "demo")
    assert dev not in _gov(hive)[1]["admitted"]
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(hive)
    assert "demo" not in (_state(hive) if (hive / ".modules.json").exists() else {})
    assert sorted(p.name for p in (hive / "journal").glob("*.jsonl")) == journal   # the journal keeps every entry
    r = _run(hive, "module", "remove", "demo", check=False)
    assert r.returncode == 1 and "not installed" in r.stderr


@LINUX_ONLY
def test_update_swaps_atomically_keeps_the_key_the_device_and_local_config(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    d = tmp_path / "modules" / "demo"
    cfg = json.loads((d / "config").read_text())
    cfg["poll"] = "99"
    (d / "config").write_text(json.dumps(cfg))
    dev = _state(hive)["demo"]["device_id"]
    _publish(tmp_path / "repo", version="1.1.0", files={"run.sh": "#!/bin/sh\necho v2\n", "hooks/stop": "#!/bin/sh\n"},
             config={"poll": "30", "new": "x"})
    r = _run(hive, "module", "update", "demo")
    assert "to 1.1.0" in r.stdout
    assert "v2" in (d / "run.sh").read_text() and not (tmp_path / "modules" / ".demo.old").exists()
    assert json.loads((d / "config").read_text()) == {"poll": "99", "new": "x"}     # the node's value survives
    assert _state(hive)["demo"]["device_id"] == dev and dev in _gov(hive)[1]["admitted"]


@LINUX_ONLY
def test_an_update_signed_by_a_different_publisher_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    _publish(tmp_path / "repo", version="2.0.0", seed=OTHER_SEED)
    r = _run(hive, "module", "update", "demo", check=False)
    assert r.returncode == 1 and "pinned" in r.stderr
    assert _state(hive)["demo"]["version"] == "1.0.0"
    assert "v2" not in (tmp_path / "modules" / "demo" / "run.sh").read_text()


@LINUX_ONLY
def test_a_tampered_update_leaves_the_installed_copy_alone(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    before = (tmp_path / "modules" / "demo" / "run.sh").read_text()
    _publish(tmp_path / "repo", version="1.1.0", tamper="run.sh")
    assert _run(hive, "module", "update", "demo", check=False).returncode == 1
    assert (tmp_path / "modules" / "demo" / "run.sh").read_text() == before


@LINUX_ONLY
def test_reset_and_update_leave_the_modules_directory_outside_the_checkout(hive, tmp_path):
    """`hive-mind reset` force-aligns the checkout and `hv verify` reads git-tracked files only: the modules
    directory is in neither. The default is under the home directory, never under HIVE_HOME."""
    assert not str(hive_modules.modules_dir()).startswith(str(hive))
    default = Path.home() / ".hive" / "modules"
    os.environ.pop("HIVE_MODULES_DIR", None)
    try:
        assert hive_modules.modules_dir() == default
    finally:
        os.environ["HIVE_MODULES_DIR"] = str(tmp_path / "modules")


@LINUX_ONLY
def test_list_shows_the_fields_and_flags_a_tampered_install(hive, tmp_path):
    assert "No modules installed" in _run(hive, "module", "list").stdout
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    out = _run(hive, "module", "list").stdout
    row = next(l for l in out.splitlines() if l.startswith("demo"))
    assert "1.0.0" in row and hive_modules.fingerprint(pub) in row and "valid" in row and "admitted" in row
    (tmp_path / "modules" / "demo" / "run.sh").write_text("#!/bin/sh\necho owned\n")
    out = _run(hive, "module", "list").stdout
    assert "INVALID" in out and "run.sh" in out


@LINUX_ONLY
def test_quota_raises_a_limit_and_the_api_reads_it(hive, tmp_path, monkeypatch):
    pub = _publish(tmp_path / "repo", quota={"per_hour": 5})
    _add(hive, tmp_path / "repo", pub)
    _run(hive, "module", "quota", "demo", "--per-day", "900", "--per-hour", "100")
    import hive_module_api as api
    monkeypatch.setattr(api.daemon, "hv", type("H", (), {"HIVE_HOME": hive})(), raising=False)
    assert api.limits_for("demo") == {**api.QUOTA_DEFAULTS, "per_hour": 100, "per_day": 900}
    assert api.limits_for("other") == api.QUOTA_DEFAULTS


@LINUX_ONLY
def test_a_hand_edited_manifest_ask_cannot_raise_a_limit(hive, tmp_path, monkeypatch):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    st = _state(hive)
    st["demo"]["quota"] = {"per_hour": 10 ** 6, "per_day": 7}
    (hive / ".modules.json").write_text(json.dumps(st))
    import hive_module_api as api
    monkeypatch.setattr(api.daemon, "hv", type("H", (), {"HIVE_HOME": hive})(), raising=False)
    lim = api.limits_for("demo")
    assert lim["per_hour"] == api.QUOTA_DEFAULTS["per_hour"] and lim["per_day"] == 7


def test_the_data_plane_cannot_reach_module_management():
    """S2: `hv` imports neither this module nor the owner key, and this module reaches the owner steps only
    through the control plane's library (it imports no `ownerkey`)."""
    import ast

    def imports(path):
        got = set()
        for n in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(n, ast.Import):
                got |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                got.add(n.module.split(".")[0])
        return got
    assert "hive_modules" not in imports(PROJECT / "hv")
    assert "ownerkey" not in imports(PROJECT / "hive_modules.py")
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "module", "list"], capture_output=True, text=True)
    assert r.returncode != 0


@LINUX_ONLY
def test_a_signed_manifest_with_an_unknown_top_level_key_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", user="root")
    r = _add(hive, tmp_path / "repo", pub, "demo", False)
    assert r.returncode == 1 and "does not know" in r.stderr
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(hive)


@LINUX_ONLY
def test_an_owner_raise_of_entry_bytes_reaches_the_route_but_only_for_that_module(hive, tmp_path, monkeypatch):
    import io
    import hive_module_api as api
    from hive_module_api import _Refused
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    pub2 = _publish(tmp_path / "repo2", name="other")
    _add(hive, tmp_path / "repo2", pub2, name="other")
    big = api.QUOTA_DEFAULTS["entry_bytes"] * 4
    _run(hive, "module", "quota", "demo", "--entry-bytes", str(big))
    monkeypatch.setattr(api.daemon, "hv", type("H", (), {"HIVE_HOME": hive})(), raising=False)
    mid = api.QUOTA_DEFAULTS["entry_bytes"] + 100

    def read(n):
        h = api.ModuleHandler.__new__(api.ModuleHandler)
        h.headers = {"Content-Length": str(n)}
        h.rfile = io.BytesIO(b"x" * n)
        return h._read_body()

    assert len(read(mid)) == mid                                   # within the raised module's size cap
    with pytest.raises(_Refused) as e:
        read(big + 1)
    assert e.value.code == 413
    assert api.limits_for("other")["entry_bytes"] == api.QUOTA_DEFAULTS["entry_bytes"]
    assert api.limits_for("demo")["entry_bytes"] == big
    # a module without the raise is still held to the default by route_entries
    with pytest.raises(_Refused) as e:
        api.route_entries({"device_id": "d", "module": "other"}, {}, b"x" * mid)
    assert e.value.code == 413
    # a manifest that asks for more than the default is still refused at add
    pub3 = _publish(tmp_path / "repo3", name="greedy", quota={"entry_bytes": mid})
    assert _add(hive, tmp_path / "repo3", pub3, name="greedy", check=False).returncode != 0


# ── 2.1 plan PR 7 (M2): the module's systemd unit ───────────────────────────────────────────────────

SERVICE = {"command": ["run.sh", "--poll", "a b", "100%$HOME", 'q"x', "back\\slash"], "restart": "always"}


@pytest.fixture
def units(tmp_path, monkeypatch):
    """A `systemctl` that succeeds and logs, so install/remove reach it; unit files land in the sandbox HOME."""
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "systemctl.log"
    (shim / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {log}\ncase "$*" in *is-active*) echo active;; esac\n'
                                    f'[ -n "$FAIL_SYSTEMCTL" ] && case "$*" in *"$FAIL_SYSTEMCTL"*) exit 1;; esac\nexit 0\n')
    (shim / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))         # a user manager exists, as far as the doctor can tell
    unit_dir = Path(os.environ["HOME"]) / ".config" / "systemd" / "user"
    return unit_dir, (lambda: [l.removeprefix("--user ") for l in log.read_text().splitlines()] if log.exists() else [])


@LINUX_ONLY
def test_a_service_block_is_rendered_as_a_user_unit_and_installed(hive, tmp_path, units):
    unit_dir, calls = units
    pub = _publish(tmp_path / "repo", service=SERVICE)
    r = _add(hive, tmp_path / "repo", pub)
    assert "hive-module-demo.service" in r.stdout
    text = (unit_dir / "hive-module-demo.service").read_text()
    d = tmp_path / "modules" / "demo"
    assert "Type=simple" in text and "Restart=always" in text and f"WorkingDirectory={d}" in text
    assert f'ExecStart="{d}/run.sh" "--poll" "a b" "100%%$$HOME" "q\\"x" "back\\\\slash"' in text      # anchored to the module, one word each
    assert "User=" not in text                                 # a user unit: it runs as the operator
    key_dir = _keys.key_dir(hive) / "modules" / "demo"
    assert f'Environment="HIVE_MODULE_KEY_DIR={key_dir}"' in text and 'Environment="HIVE_MODULE_NAME=demo"' in text
    assert "owner" not in text.lower() and "HIVE_HOME" not in text     # no owner-key path, no hive home
    assert "pkill" not in text and "ExecStartPre" not in text
    assert not (unit_dir / "hive-module-demo.timer").exists()
    assert "enable hive-module-demo.service" in calls() and "restart hive-module-demo.service" in calls()


@LINUX_ONLY
def test_an_interval_makes_a_oneshot_service_and_a_timer(hive, tmp_path, units):
    unit_dir, calls = units
    pub = _publish(tmp_path / "repo", service={"command": ["python3", "run.sh"], "interval": 300})
    _add(hive, tmp_path / "repo", pub)
    svc = (unit_dir / "hive-module-demo.service").read_text()
    timer = (unit_dir / "hive-module-demo.timer").read_text()
    assert "Type=oneshot" in svc and "Restart=" not in svc and 'ExecStart="/usr/bin/env" "python3" "run.sh"' in svc
    assert "OnUnitActiveSec=300" in timer and "WantedBy=timers.target" in timer
    assert "enable hive-module-demo.timer" in calls()          # the timer is what is enabled and started


@LINUX_ONLY
def test_a_module_without_a_service_gets_no_unit(hive, tmp_path, units):
    unit_dir, calls = units
    _add(hive, tmp_path / "repo", _publish(tmp_path / "repo"))
    assert not list(unit_dir.glob("hive-module-*")) and calls() == []
    row = next(l for l in _run(hive, "module", "list").stdout.splitlines() if l.startswith("demo"))
    assert " none " in row


@LINUX_ONLY
def test_remove_stops_and_deletes_the_units(hive, tmp_path, units):
    unit_dir, calls = units
    _add(hive, tmp_path / "repo", _publish(tmp_path / "repo", service={"command": ["run.sh"], "interval": 60}))
    assert len(list(unit_dir.glob("hive-module-demo.*"))) == 2
    _run(hive, "module", "remove", "demo")
    assert not list(unit_dir.glob("hive-module-*"))
    assert "disable --now hive-module-demo.timer" in calls() and "disable --now hive-module-demo.service" in calls()


@LINUX_ONLY
def test_update_reapplies_the_unit_and_drops_a_timer_it_no_longer_declares(hive, tmp_path, units):
    unit_dir, calls = units
    repo = tmp_path / "repo"
    pub = _publish(repo, service={"command": ["run.sh"], "interval": 60})
    _add(hive, repo, pub)
    _publish(repo, version="1.1.0", service={"command": ["run.sh", "--v2"], "restart": "no"})
    _run(hive, "module", "update", "demo")
    assert "--v2" in (unit_dir / "hive-module-demo.service").read_text()
    assert not (unit_dir / "hive-module-demo.timer").exists()
    assert "disable --now hive-module-demo.timer" in calls() and "restart hive-module-demo.service" in calls()
    _publish(repo, version="1.2.0")                            # no service block any more: the unit goes
    _run(hive, "module", "update", "demo")
    assert not list(unit_dir.glob("hive-module-*"))


@LINUX_ONLY
def test_without_a_user_systemd_the_unit_is_written_and_not_started(hive, tmp_path):
    # the suite's stub `systemctl` always fails: no user manager
    pub = _publish(tmp_path / "repo", service=SERVICE)
    r = _add(hive, tmp_path / "repo", pub)
    assert "written, not started" in r.stdout
    assert (Path(os.environ["HOME"]) / ".config/systemd/user/hive-module-demo.service").exists()


@LINUX_ONLY
def test_uninstall_removes_module_units(tmp_path, units):
    unit_dir, calls = units
    unit_dir.mkdir(parents=True)
    for f in ("hive-module-demo.service", "hive-module-demo.timer"):
        (unit_dir / f).write_text("[Unit]\n")
    hive_dir = tmp_path / "hivedir"
    hive_dir.mkdir()
    env = dict(os.environ, HIVE_DIR=str(hive_dir), BIN_DIR=str(tmp_path / "bin"), SETTINGS=str(tmp_path / "s.json"),
               HIVE_UNINSTALL_TEST="1")
    r = subprocess.run(["bash", str(PROJECT / "scripts/installer/_uninstall.sh"), "--yes"], env=env,
                       capture_output=True, text=True, input="")
    assert r.returncode == 0, r.stderr
    assert not list(unit_dir.glob("hive-module-*"))
    assert "disable --now hive-module-demo.timer" in calls()


@LINUX_ONLY
def test_a_service_command_cannot_leave_the_module_directory(hive, tmp_path, units):
    unit_dir, _ = units
    pub = _publish(tmp_path / "repo", service={"command": ["../other/run.sh"]})
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "leave the module's directory" in r.stderr
    assert not list(unit_dir.glob("hive-module-*")) and not _key_dir_has_module(hive)


@LINUX_ONLY
def test_update_from_a_daemon_to_a_timer_stops_the_daemon(hive, tmp_path, units):
    unit_dir, calls = units
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo, service={"command": ["run.sh"], "restart": "always"}))
    _publish(repo, version="1.1.0", service={"command": ["run.sh"], "interval": 60})
    _run(hive, "module", "update", "demo")
    assert any(c in calls() for c in ("disable --now hive-module-demo.service", "stop hive-module-demo.service"))
    assert "enable hive-module-demo.timer" in calls() and "Type=oneshot" in (unit_dir / "hive-module-demo.service").read_text()


@LINUX_ONLY
def test_a_control_character_in_a_service_argument_is_refused(hive, tmp_path, units):
    unit_dir, _ = units
    pub = _publish(tmp_path / "repo", service={"command": ["run.sh", "x\nEnvironment=HIVE_HOME=/elsewhere"]})
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1
    assert not list(unit_dir.glob("hive-module-*"))


@LINUX_ONLY
def test_a_stale_old_copy_is_not_rolled_back_over_a_good_install(hive, tmp_path, units):
    """A crash after the swap leaves `.old` beside a good live copy; a later update that fails before its own swap
    must not put that older copy back."""
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo))
    live, stale = tmp_path / "modules" / "demo", tmp_path / "modules" / ".demo.old"
    stale.mkdir()
    (stale / "run.sh").write_text("#!/bin/sh\necho STALE\n")
    (live / "config").write_text("not json")                    # _write_config fails before the swap
    _publish(repo, version="1.1.0")
    assert _run(hive, "module", "update", "demo", check=False).returncode != 0
    assert "STALE" not in (live / "run.sh").read_text() and (live / "module.json").exists()


@LINUX_ONLY
def test_a_unit_that_will_not_start_is_reported(hive, tmp_path, units, monkeypatch):
    monkeypatch.setenv("FAIL_SYSTEMCTL", "restart")
    repo = tmp_path / "repo"
    r = _add(hive, repo, _publish(repo, service=SERVICE))
    assert "would not start" in r.stdout


@LINUX_ONLY
def test_list_shows_the_state_of_a_module_with_a_service(hive, tmp_path, units):
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo, service=SERVICE))
    row = next(l for l in _run(hive, "module", "list").stdout.splitlines() if l.startswith("demo"))
    assert " active " in row


@LINUX_ONLY
def test_reapply_carries_a_renderer_change_to_an_installed_module_and_restarts_only_that_unit(hive, tmp_path, units):
    unit_dir, calls = units
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo, service=SERVICE))
    _add(hive, tmp_path / "r2", _publish(tmp_path / "r2", name="plain"), name="plain")     # no service: untouched
    unit = unit_dir / "hive-module-demo.service"
    good = unit.read_text()
    unit.write_text(good.replace("NoNewPrivileges=yes\n", ""))          # what an older renderer wrote
    log = tmp_path / "systemctl.log"
    log.write_text("")
    r = _run(hive, "module", "reapply")
    assert r.returncode == 0 and r.stdout == "" and unit.read_text() == good
    assert calls().count("daemon-reload") == 1 and "restart hive-module-demo.service" in calls()
    log.write_text("")
    _run(hive, "module", "reapply")                                       # nothing changed: nothing restarted
    assert calls() == []


@LINUX_ONLY
def test_reapply_warns_about_a_module_it_cannot_re_apply_and_still_succeeds(hive, tmp_path, units):
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo, service=SERVICE))
    (tmp_path / "modules" / "demo" / "run.sh").write_text("#!/bin/sh\necho owned\n")
    r = _run(hive, "module", "reapply")
    assert r.returncode == 0 and "warning: demo" in r.stderr


@LINUX_ONLY
def test_a_timer_unit_writes_no_persistent_stamp():
    units = hive_modules.render_units("demo", {"service": {"command": ["run.sh"], "interval": 60}}, Path("/m"), Path("/k"))
    assert "OnUnitActiveSec=60" in units["hive-module-demo.timer"]
    assert "Persistent=" not in units["hive-module-demo.timer"]


@LINUX_ONLY
def test_a_percent_in_a_module_path_is_doubled_in_the_environment_and_working_directory():
    text = hive_modules.render_units("demo", {"service": {"command": ["run.sh"]}}, Path("/m 50%dir"), Path("/k 5%"))["hive-module-demo.service"]
    assert "WorkingDirectory=/m 50%%dir" in text
    assert 'Environment="HIVE_MODULE_DIR=/m 50%%dir"' in text and 'Environment="HIVE_MODULE_KEY_DIR=/k 5%%"' in text


@LINUX_ONLY
def test_remove_deletes_the_stamp_an_older_timer_left(hive, tmp_path, units):
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo, service={"command": ["run.sh"], "interval": 60}))
    stamp = Path(os.environ["HOME"]) / ".local" / "share" / "systemd" / "timers" / "stamp-hive-module-demo.timer"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("")
    _run(hive, "module", "remove", "demo")
    assert not stamp.exists()


@LINUX_ONLY
def test_reapply_warns_when_a_restart_fails(hive, tmp_path, units, monkeypatch):
    unit_dir, _ = units
    repo = tmp_path / "repo"
    _add(hive, repo, _publish(repo, service=SERVICE))
    unit = unit_dir / "hive-module-demo.service"
    unit.write_text(unit.read_text().replace("NoNewPrivileges=yes\n", ""))
    monkeypatch.setenv("FAIL_SYSTEMCTL", "restart")
    r = _run(hive, "module", "reapply")
    assert r.returncode == 0 and "warning: demo" in r.stderr and "would not start" in r.stderr
