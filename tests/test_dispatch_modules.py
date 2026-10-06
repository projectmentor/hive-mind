"""Plan PR 8 (M4): `hive_dispatch.sh` runs each installed module's `hooks/<event>` after the core behaviors, and
`Notification` / `Stop` are registered as shims. A module hook never changes the exit code, never writes to stdout,
never delays past its cap, and with no modules the dispatcher is byte-for-byte what it was."""
import json
import os
import platform
import stat
import subprocess
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
DISPATCH = PROJECT / "scripts" / "common" / "hive_dispatch.sh"
# module hooks run only on Linux (not Termux), like module management; elsewhere the loop is skipped
linux_only = pytest.mark.skipif(platform.system() != "Linux" or bool(os.environ.get("TERMUX_VERSION")),
                                reason="module hooks run on Linux only")
EVENTS = ("session-start", "user-prompt", "precompact", "sessionend", "notification", "stop")


def _env(tmp_path, **extra):
    home = tmp_path / "hive"
    (home / "scripts" / "common").mkdir(parents=True, exist_ok=True)
    return {**os.environ, "HIVE_HOME": str(home), "HIVE_MODULES_DIR": str(tmp_path / "modules"), **extra}


def _install(tmp_path, name, body, event="stop", mode=0o755, record=True):
    d = tmp_path / "modules" / name / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / event).write_text(body)
    (d / event).chmod(mode)
    if record:
        st = tmp_path / "hive" / ".modules.json"
        st.parent.mkdir(parents=True, exist_ok=True)
        cur = json.loads(st.read_text()) if st.exists() else {}
        cur[name] = {"version": "1.0.0"}
        st.write_text(json.dumps(cur, indent=1, sort_keys=True))
    return d / event


def _run(tmp_path, event="stop", payload='{"x":1}', **extra):
    t0 = time.monotonic()
    r = subprocess.run(["bash", str(DISPATCH), event], input=payload, env=_env(tmp_path, **extra),
                       capture_output=True, text=True, timeout=60)
    return r, time.monotonic() - t0


def _bus(tmp_path):
    p = tmp_path / "hive" / ".bus" / "modules.log"
    return p.read_text() if p.exists() else ""


@linux_only
def test_a_module_hook_runs_with_the_payload_and_in_name_order(tmp_path):
    out = tmp_path / "out"
    _install(tmp_path, "bravo", f"#!/bin/sh\ncat >> {out}.b\necho b >> {out}\n")
    _install(tmp_path, "alpha", f"#!/bin/sh\ncat >> {out}.a\necho a >> {out}\n")
    r, _ = _run(tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    assert out.read_text() == "a\nb\n"
    assert Path(f"{out}.a").read_text() == '{"x":1}'


@linux_only
def test_every_event_reaches_its_own_hook_only(tmp_path):
    for ev in EVENTS:
        out = tmp_path / f"ran-{ev}"
        _install(tmp_path, "mod", f"#!/bin/sh\ntouch {out}\n", event=ev)
    for ev in EVENTS:
        _run(tmp_path, ev)
        assert (tmp_path / f"ran-{ev}").exists(), ev
        for other in EVENTS:
            if other != ev:
                (tmp_path / f"ran-{other}").unlink(missing_ok=True)
    assert not any((tmp_path / f"ran-{e}").exists() for e in EVENTS if e != EVENTS[-1])


def test_a_failing_noisy_hook_never_changes_the_exit_or_the_stdout(tmp_path):
    _install(tmp_path, "noisy", "#!/bin/sh\necho LEAK\necho LEAK >&2\nexit 7\n")
    r, _ = _run(tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


@linux_only
def test_a_hanging_hook_is_cut_at_the_cap_and_logged(tmp_path):
    _install(tmp_path, "hang", "#!/bin/sh\nsleep 30 &\nsleep 30\n")
    r, dt = _run(tmp_path, HIVE_MODULE_HOOK_TIMEOUT="1", HIVE_MODULE_EVENT_BUDGET="5")
    assert r.returncode == 0 and r.stdout == ""
    assert dt < 6, dt
    assert "hook-timeout hang stop" in _bus(tmp_path)


@linux_only
def test_the_event_budget_bounds_the_whole_loop(tmp_path):
    for n in ("a", "b", "c", "d"):
        _install(tmp_path, f"m{n}", "#!/bin/sh\nsleep 30\n")
    r, dt = _run(tmp_path, HIVE_MODULE_HOOK_TIMEOUT="2", HIVE_MODULE_EVENT_BUDGET="3")
    assert r.returncode == 0 and dt < 8, dt
    assert "hook-skipped" in _bus(tmp_path)


@linux_only
def test_a_hook_that_does_not_read_its_stdin_is_harmless(tmp_path):
    _install(tmp_path, "deaf", "#!/bin/sh\nexit 0\n")
    r, _ = _run(tmp_path, payload="x" * 1_000_000)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


@linux_only
def test_a_hook_that_is_not_executable_is_skipped(tmp_path):
    out = tmp_path / "out"
    _install(tmp_path, "plain", f"#!/bin/sh\ntouch {out}\n", mode=0o644)
    r, _ = _run(tmp_path)
    assert r.returncode == 0 and not out.exists()


@linux_only
def test_unsafe_hooks_are_skipped_and_logged(tmp_path):
    out = tmp_path / "out"
    _install(tmp_path, "gw", f"#!/bin/sh\ntouch {out}.gw\n", mode=0o775)            # group-writable
    _install(tmp_path, "ow", f"#!/bin/sh\ntouch {out}.ow\n", mode=0o757)            # other-writable
    _install(tmp_path, "unrecorded", f"#!/bin/sh\ntouch {out}.un\n", record=False)  # not in .modules.json
    real = tmp_path / "real-hook"
    real.write_text(f"#!/bin/sh\ntouch {out}.ln\n")
    real.chmod(0o755)
    _install(tmp_path, "linked", "#!/bin/sh\n")
    (tmp_path / "modules" / "linked" / "hooks" / "stop").unlink()
    (tmp_path / "modules" / "linked" / "hooks" / "stop").symlink_to(real)
    r, _ = _run(tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    assert not list(tmp_path.glob("out.*"))
    log = _bus(tmp_path)
    for n in ("gw", "ow", "unrecorded", "linked"):
        assert f"hook-skipped {n} stop" in log, (n, log)


def test_a_symlinked_modules_directory_runs_nothing(tmp_path):
    out = tmp_path / "out"
    _install(tmp_path, "mod", f"#!/bin/sh\ntouch {out}\n")
    (tmp_path / "elsewhere").symlink_to(tmp_path / "modules")
    r, _ = _run(tmp_path, HIVE_MODULES_DIR=str(tmp_path / "elsewhere"))
    assert r.returncode == 0 and not out.exists()


def test_hooks_of_dotted_or_odd_names_are_not_run(tmp_path):
    out = tmp_path / "out"
    for n in (".mod.staging", "Bad_Name"):
        _install(tmp_path, n, f"#!/bin/sh\ntouch {out}\n")
    r, _ = _run(tmp_path)
    assert r.returncode == 0 and not out.exists()


def test_with_no_modules_the_dispatcher_is_what_it_was(tmp_path):
    for ev in EVENTS + ("bogus", ""):
        r, _ = _run(tmp_path, ev)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), ev
    assert not (tmp_path / "hive" / ".bus").exists()


def test_an_unknown_event_never_runs_a_module_hook(tmp_path):
    out = tmp_path / "out"
    _install(tmp_path, "mod", f"#!/bin/sh\ntouch {out}\n", event="bogus")
    r, _ = _run(tmp_path, "bogus")
    assert r.returncode == 0 and not out.exists()


def test_the_two_new_events_are_in_the_spec_and_a_legacy_node_reconciles_to_them():
    import importlib.machinery, importlib.util
    ld = importlib.machinery.SourceFileLoader("hv_m4", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hv_m4", ld)
    hv = importlib.util.module_from_spec(spec)
    ld.exec_module(hv)
    by_event = {ev: cmd for ev, cmd, _t in hv._CLAUDE_HOOK_SPEC}
    assert by_event["Notification"].endswith("hive_dispatch.sh notification")
    assert by_event["Stop"].endswith("hive_dispatch.sh stop")
    legacy = {"hooks": {ev: [{"hooks": [{"type": "command", "command": cmd}]}]
                        for ev, cmd, _t in hv._CLAUDE_HOOK_SPEC if ev not in ("Notification", "Stop")}}
    missing, stale = hv._claude_hook_reconcile(legacy)
    assert sorted(ev for ev, _c, _t in missing) == ["Notification", "Stop"] and stale == []


def test_a_dir_named_like_a_nested_state_key_is_not_recorded(tmp_path):
    out = tmp_path / "out"
    _install(tmp_path, "alpha", "#!/bin/sh\nexit 0\n")
    st = tmp_path / "hive" / ".modules.json"
    st.write_text(json.dumps({"alpha": {"commit": "x", "source": "y", "version": "1", "publisher": "p", "quota": 1}},
                             indent=1, sort_keys=True))
    for n in ("source", "commit", "version", "publisher", "quota"):
        _install(tmp_path, n, f"#!/bin/sh\ntouch {out}.{n}\n", record=False)
    r, _ = _run(tmp_path)
    assert r.returncode == 0 and not list(tmp_path.glob("out.*"))


@linux_only
def test_a_term_ignoring_hook_stays_inside_the_budget(tmp_path):
    for n in ("a", "b", "c"):
        _install(tmp_path, f"m{n}", "#!/bin/sh\ntrap '' TERM\nsleep 100\n")
    r, dt = _run(tmp_path)   # defaults: cap 3, budget 6
    # a: 3s (TERM at 2, KILL at 3); b: left 6-3-1 = 2, so TERM at 1, KILL at 2s; c: skipped. About 5s in all; the
    # old whole-second `SECONDS` timing, or a grace added on top of the cap, ran past 6s.
    assert r.returncode == 0 and 4.9 < dt < 5.6, dt
    log = _bus(tmp_path)
    assert log.count("hook-timeout") == 2 and log.count("hook-skipped") == 1, log


@linux_only
def test_a_single_hooks_cap_includes_its_kill_grace(tmp_path):
    _install(tmp_path, "stubborn", "#!/bin/sh\ntrap '' TERM\nsleep 100\n")
    r, dt = _run(tmp_path, HIVE_MODULE_HOOK_TIMEOUT="3")
    assert r.returncode == 0 and 2.9 < dt < 3.6, dt


@linux_only
def test_a_cap_of_one_second_still_has_a_limit(tmp_path):
    _install(tmp_path, "tight", "#!/bin/sh\nsleep 100\n")   # `timeout 0` would mean no limit at all
    r, dt = _run(tmp_path, HIVE_MODULE_HOOK_TIMEOUT="1")
    assert r.returncode == 0 and dt < 3, dt
    assert "hook-timeout tight stop" in _bus(tmp_path)


@linux_only
def test_the_budget_is_clamped_to_the_shim_timeout(tmp_path):
    _install(tmp_path, "slow", "#!/bin/sh\nsleep 100\n", event="user-prompt")
    r, dt = _run(tmp_path, event="user-prompt", HIVE_MODULE_EVENT_BUDGET="60", HIVE_MODULE_HOOK_TIMEOUT="60")
    assert r.returncode == 0 and dt < 9.5, dt


@linux_only
def test_a_detached_child_is_reaped_after_a_normal_exit(tmp_path):
    pidf = tmp_path / "pid"
    _install(tmp_path, "fork", f"#!/bin/sh\nsleep 30 &\necho $! > {pidf}\nexit 0\n")
    r, _ = _run(tmp_path)
    pid = int(pidf.read_text())
    time.sleep(0.3)
    assert r.returncode == 0
    try:
        os.kill(pid, 0)
        alive = Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        alive = False
    assert not alive


@linux_only
def test_a_slow_core_behaviour_shrinks_the_module_budget(tmp_path):
    out = tmp_path / "ran"
    nudge = tmp_path / "hive" / "scripts" / "common" / "nudge_hook.sh"
    nudge.parent.mkdir(parents=True, exist_ok=True)
    nudge.write_text("#!/bin/sh\nsleep 8\necho context\n")
    nudge.chmod(0o755)
    _install(tmp_path, "late", f"#!/bin/sh\ntouch {out}\nsleep 100\n", event="user-prompt")
    r, dt = _run(tmp_path, event="user-prompt")
    assert r.returncode == 0 and r.stdout == "context\n"
    assert dt < 9.6, dt   # 10s shim cap less 1s: the core's output is never cut off


@pytest.mark.skipif(platform.system() != "Linux", reason="fakes a non-Linux uname on a Linux host")
@pytest.mark.parametrize("fake", [{"uname": "Darwin"}, {"uname": "Linux", "TERMUX_VERSION": "0.118"}])
def test_off_linux_the_module_loop_is_skipped_silently(tmp_path, fake):
    out = tmp_path / "out"
    _install(tmp_path, "mod", f"#!/bin/sh\ntouch {out}\n")
    _install(tmp_path, "hang", "#!/bin/sh\nsleep 30\n")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "uname").write_text(f"#!/bin/sh\necho {fake['uname']}\n")
    (shim / "uname").chmod(0o755)
    extra = {"TERMUX_VERSION": fake["TERMUX_VERSION"]} if "TERMUX_VERSION" in fake else {}
    r, dt = _run(tmp_path, PATH=f"{shim}:{os.environ['PATH']}", **extra)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not out.exists() and dt < 5, dt
    assert not (tmp_path / "hive" / ".bus").exists()
