"""`hv doctor` checks for installed modules (2.1, plan PR 9, M7).

Data plane and read-only, except `fix`: it imports no owner-key code (the S2 boundary) and `hv` imports it, not
`hive_modules`. It reuses `hive_modules`' pure helpers (`check_tree`, `render_units`, `service_state`), so what the
doctor expects of an install is what `hive-mind module add` produced. One result per module, named
`modules:<name>`; a node with no modules gets none, so its `hv doctor` is unchanged.

What fails and what warns. A module runs code on this machine, so a manifest that is missing, tampered or signed
by anything but the pinned publisher is a **fail**. A unit that is absent, differs from its rendering or is not
active, a revoked device, missing config, a down listener and quota near a limit are **warns**: the module is
degraded, not hostile. A record in `.modules.json` that is not an object is reported, never a crash.

`fix` is `hv doctor --fix`'s part: re-render a unit that differs from the manifest (through the stop-first path
`module add` and `update` use) and start one that is `failed` or not enabled. A unit that is enabled but `inactive`
was stopped on purpose: it is reported and left alone, and a running unit that is only not enabled is enabled,
never restarted. With no user systemd manager `fix` touches no unit and says so. `fix` acts only on a module whose
manifest verifies, never re-admits a device, and never touches governance.
"""

import json
import os
import socket
import stat
import time
from pathlib import Path

import hive_modules
import vocabulary

QUOTA_WARN = 0.8                  # warn when a limit is this much used
CONTRACT_WITH_MODULES = "2.1"     # a node below this counts a module device's election vote (§7)
RANK = {"ok": 0, "warn": 1, "fail": 2}


def _load(hv):
    """({name: record}, problem or None). A missing file is no modules; one that cannot be read is a problem."""
    path = hv.HIVE_HOME / hive_modules.STATE_FILE
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return {}, f"{hive_modules.STATE_FILE} cannot be read ({e})"
    if not isinstance(data, dict):
        return {}, f"{hive_modules.STATE_FILE} is not an object"
    return data, None


def _limits(rec):
    """`hive_module_api.limits_for`, tolerant of a hand-edited record: defaults, the manifest's ask clamped to the
    default, then the owner's own."""
    limits = dict(hive_modules.QUOTA_DEFAULTS)
    for table, ceiling in ((rec.get("quota"), True), (rec.get("owner_quota"), False)):
        for k, v in (table.items() if isinstance(table, dict) else ()):
            if k in limits and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                limits[k] = min(v, hive_modules.QUOTA_DEFAULTS[k]) if ceiling else v
    return limits


def _window_times(hv, device_id, now):
    try:
        data = json.loads((hv.HIVE_HOME / ".module-quota.json").read_text())
        times = data.get(device_id, []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        times = []
    return [t for t in times if isinstance(t, (int, float)) and not isinstance(t, bool) and now - 86400 < t <= now]


def listener_up(timeout=1.0):
    """Whether something accepts on the module API's loopback port (an unauthenticated probe: connect only)."""
    try:
        port = int(os.environ.get("HIVE_MODULE_PORT") or 9886)
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True, port
    except (OSError, ValueError):
        return False, os.environ.get("HIVE_MODULE_PORT") or 9886


def _device(gov, name, dev):
    if not isinstance(dev, str) or not dev:
        return [("fail", "the record names no device")]
    if dev in gov["purged"]:
        return [("warn", f"device {dev[:11]}… is purged: the module cannot write (`hive-mind module remove {name}`)")]
    if dev not in gov["admitted"]:
        return [("warn", f"device {dev[:11]}… is revoked: the module cannot write, and doctor never re-admits it "
                         f"(`hive-mind module remove {name}`)")]
    if gov["modules"].get(dev) != name:
        return [("warn", f"device {dev[:11]}… is admitted without its module marker, so it would count as an "
                         f"election voter")]
    return []


def _units(name, manifest, key_dir):
    """Problems with the module's unit files against what its manifest renders."""
    expected = hive_modules.render_units(name, manifest, hive_modules.modules_dir() / name, key_dir)
    d, out = hive_modules.unit_dir(), []
    for f in hive_modules.unit_names(name):
        have = (d / f).read_text() if (d / f).exists() else None
        if f in expected and have is None:
            out.append(("warn", f"unit {f} is not installed (`hive-mind doctor --fix` re-renders it)"))
        elif f in expected and have != expected[f]:
            out.append(("warn", f"unit {f} differs from what the manifest renders (`hive-mind doctor --fix`)"))
        elif f not in expected and have is not None:
            out.append(("warn", f"unit {f} is installed, but the manifest declares none"))
    if expected and not any(p[1].startswith("unit ") for p in out):
        state = hive_modules.service_state(name)
        if state == "installed":
            out.append(("warn", "service state unknown: no user systemd manager is reachable"))
        elif state == "inactive" and hive_modules.unit_enabled(name):
            out.append(("warn", f"service is stopped; `systemctl --user start {hive_modules.main_unit(name)}` to resume "
                                f"(`hive-mind doctor --fix` leaves a stopped module alone)"))
        elif state == "active":
            if not hive_modules.unit_enabled(name):
                out.append(("warn", f"service is active but not enabled, so it will not start at login "
                                    f"(`hive-mind doctor --fix` enables it, without restarting it)"))
        else:
            why = state if hive_modules.unit_enabled(name) else f"{state} and not enabled"
            out.append(("warn", f"service is {why} (`hive-mind doctor --fix` starts it)"))
    return out


def _hooks(moddir, manifest):
    """Warnings (a hook the manifest's `hooks` list does not declare is a fail) for every file under `hooks/`: the dispatcher (`hive_dispatch.sh`) runs `hooks/<event>` only for a known
    event, and only a regular, non-link, executable, own-user file with no group or other write bit, in real
    directories. A file it would skip, or one the signed manifest does not list, is named with the reason. Digests are
    `check_tree`'s part, which has already passed."""
    hdir = moddir / "hooks"
    if moddir.is_symlink() or hdir.is_symlink():
        return [("warn", "hooks/ is reached through a link, so the dispatcher skips every hook")]
    try:
        names = sorted(p.name for p in hdir.iterdir())
    except FileNotFoundError:
        return []
    except OSError as e:
        return [("warn", f"hooks/ cannot be read ({e})")]
    out, uid = [], os.getuid()
    for n in names:
        gate = []                                               # what makes the dispatcher skip it
        try:
            st = os.lstat(hdir / n)
        except OSError as e:
            out.append(("warn", f"hook {n} cannot be examined ({e})"))
            continue
        if not stat.S_ISREG(st.st_mode):
            gate.append("not a regular file")
        else:
            if not st.st_mode & stat.S_IXUSR:
                gate.append("not executable")
            if st.st_uid != uid:
                gate.append("not owned by this user")
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                gate.append("writable by group or other")
        why = []
        if n not in hive_modules.HOOK_EVENTS:
            why.append("not an event the dispatcher knows, so it never runs")
        elif gate:
            why.append(f"the dispatcher skips it: {', '.join(gate)}")
        if f"hooks/{n}" not in manifest["files"]:
            why.append("not listed in the signed manifest")
        if f"hooks/{n}" in manifest["files"] and n not in manifest.get("hooks", []):
            out.append(("fail", f"hook {n}: hook not declared in the manifest"))
        elif why:
            out.append(("warn", f"hook {n}: {'; '.join(why)}"))
    return out


def _config(moddir, manifest):
    try:
        cfg = json.loads((moddir / "config").read_text())
    except (OSError, ValueError):
        return [("warn", "its config file is missing or not JSON")]
    if not isinstance(cfg, dict):
        return [("warn", "its config file is not an object")]
    out = []
    missing = sorted(k for k in manifest.get("config", {}) if k not in cfg)
    if missing:
        out.append(("warn", f"config lacks {', '.join(missing)}"))
    bad = sorted(k for k, v in cfg.items() if not isinstance(v, str) or len(v) > vocabulary.MODULE_VALUE_MAX)
    if bad:
        out.append(("warn", f"config values must be strings of at most {vocabulary.MODULE_VALUE_MAX}: {', '.join(bad)}"))
    return out


def _quota(hv, rec, dev, lifetime, now):
    limits, times = _limits(rec), _window_times(hv, dev, now)
    used = {"per_hour": sum(1 for t in times if t > now - 3600), "per_day": len(times), "lifetime": lifetime}
    near = [f"{k} {used[k]}/{limits[k]}" for k in ("per_hour", "per_day", "lifetime")
            if limits[k] > 0 and used[k] >= QUOTA_WARN * limits[k]]
    return ([("warn", f"quota use is near a limit: {', '.join(near)} (`hive-mind module quota` raises it)")]
            if near else []), used


def _module(hv, gov, name, rec, entries, listener, now):
    if not isinstance(rec, dict):
        return {"name": f"modules:{name}", "status": "fail",
                "detail": f"its {hive_modules.STATE_FILE} record is not an object; `hive-mind module remove {name}` "
                          f"cannot reach it: remove the record and re-add the module"}
    problems, moddir, dev = [], hive_modules.modules_dir() / name, rec.get("device_id")
    key_dir = Path(hv.KEY_DIR) / "modules" / name
    manifest = None
    try:
        manifest = hive_modules.check_tree(moddir, hv.CONTRACT_VERSION, ignore=("config",))
    except (hive_modules.ModuleError, OSError, ValueError, KeyError, TypeError) as e:
        problems.append(("fail", f"manifest not trusted, nothing below is checked against it: {e}"))
    if manifest is not None:
        if manifest["name"] != name:
            problems.append(("fail", f"the installed manifest is for module {manifest['name']!r}"))
        elif not isinstance(rec.get("publisher"), str) or manifest["publisher"] != rec["publisher"]:
            problems.append(("fail", "the manifest's publisher key does not match the pinned key"))
        if rec.get("version") != manifest["version"]:
            problems.append(("warn", f"the record says version {rec.get('version')!r}, the manifest {manifest['version']!r}"))
    problems += _device(gov, name, dev)
    if not key_dir.exists():
        problems.append(("warn", "its device key directory is missing"))
    if manifest is not None and manifest["name"] == name:
        problems += _units(name, manifest, key_dir)
        problems += _hooks(moddir, manifest)
        problems += _config(moddir, manifest)
    if not listener[0]:
        problems.append(("warn", f"the module API is not listening on 127.0.0.1:{listener[1]} (the sync daemon serves it)"))
    used = None
    if isinstance(dev, str) and dev:
        q, used = _quota(hv, rec, dev, sum(1 for e in entries if e.get("node_id") == dev), now)
        problems += q
    status = max((s for s, _ in problems), key=RANK.get, default="ok")
    if problems:
        detail = "; ".join(msg for _s, msg in problems)
    else:
        detail = (f"{rec.get('version')} signed by the pinned publisher, device admitted, "
                  + (f"quota {used['per_hour']}/h {used['per_day']}/d {used['lifetime']} total" if used else ""))
    return {"name": f"modules:{name}", "status": status, "detail": detail}


def checks(hv, gov, entries, probe=None, now=None):
    """The `modules` results for `hv doctor`: [] on a node with no modules. `probe()` returns (probed peer map,
    contracts by peer) for the old-contract warning and is only called when it can matter."""
    state, problem = _load(hv)
    if problem:
        return [{"name": "modules", "status": "warn", "detail": problem}]
    modules_dir = hive_modules.modules_dir()
    try:
        stray = sorted(p.name for p in modules_dir.iterdir()
                       if p.is_dir() and not p.name.startswith(".") and p.name not in state)
    except OSError:
        stray = []
    if not state and not stray:
        return []
    out = []
    ok, why = hive_modules.platform_supported()
    if not ok:
        return [{"name": "modules", "status": "warn", "detail": why}]
    now = time.time() if now is None else now
    listener = listener_up()
    for name in sorted(state):
        out.append(_module(hv, gov, name, state[name], entries, listener, now))
    if stray:
        out.append({"name": "modules", "status": "warn",
                    "detail": f"{', '.join(stray)} under {modules_dir} not managed by this hive (not in "
                              f"{hive_modules.STATE_FILE}; another hive on this machine may manage "
                              f"{'it' if len(stray) == 1 else 'them'}, as {modules_dir} is shared)"})
    try:
        quorum = int(gov["config"].get("quorum_m", 0) or 0)
    except (TypeError, ValueError):
        quorum = 0
    if quorum > 0 and probe is not None and state:
        probed, contracts = probe()
        fc = hv._fleet_contract(gov, probed, contracts, minimum=CONTRACT_WITH_MODULES)
        if fc["behind"]:
            out.append({"name": "modules-contract", "status": "warn",
                        "detail": f"quorum_m={quorum} and this hive has modules, but "
                                  + ", ".join(f"{d[:11]}… ({c})" for d, c in fc["behind"])
                                  + f" run a contract below {CONTRACT_WITH_MODULES} and would count a module device's "
                                    f"election vote: update them (`hive-mind update`)"})
    return out


def fix(hv, dry=False, now=None):
    """Re-render a unit that differs from its manifest and start one that is failed or not enabled (a running one
    that is only not enabled is enabled, never restarted). Without a user systemd manager it does nothing but say so.
    Returns lines to print. A unit that is enabled but inactive was stopped on purpose: reported, never restarted.
    Only a module whose manifest verifies, whose device is admitted, on a platform with a backend. Nothing here
    admits, revokes or signs anything."""
    state, _problem = _load(hv)
    if not state or not hive_modules.platform_supported()[0]:
        return []
    gov = hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))
    lines, plan, no_manager = [], [], False
    manager = None                                              # detected once, on the first module with units
    for name, rec in sorted(state.items()):
        if not isinstance(rec, dict) or _device(gov, name, rec.get("device_id")):
            continue
        try:
            manifest = hive_modules.check_tree(hive_modules.modules_dir() / name, hv.CONTRACT_VERSION,
                                               ignore=("config",))
        except (hive_modules.ModuleError, OSError, ValueError, KeyError, TypeError):
            continue
        if manifest["name"] != name or manifest["publisher"] != rec.get("publisher"):
            continue
        expected = hive_modules.render_units(name, manifest, hive_modules.modules_dir() / name,
                                             Path(hv.KEY_DIR) / "modules" / name)
        if not expected:
            continue
        if manager is None:
            manager = hive_modules.user_systemd()
        if not manager:                                         # no write, no start: nothing here would be true
            no_manager = True
            continue
        d = hive_modules.unit_dir()
        changed = [f for f, text in expected.items() if not (d / f).exists() or (d / f).read_text() != text]
        before = hive_modules.service_state(name)
        enabled = hive_modules.unit_enabled(name)
        stopped = before == "inactive" and enabled              # the operator's own doing
        running = before in ("active", "activating") and not enabled
        unit = next(f for f in hive_modules.unit_names(name)[::-1] if f in expected)
        for f in changed:
            lines.append(f"  {'would re-render' if dry else 're-rendered'} unit {f} (module {name})")
        if changed and not dry:
            hive_modules._write_units(hv, name, manifest)      # stop first: a daemon that becomes a timer must not keep running
        if stopped:
            lines.append(f"  module {name} is stopped; `systemctl --user start {unit}` to resume (left alone)")
        if stopped and not changed:
            continue
        if running and not changed:                             # a live process is never restarted for a missing enable
            plan.append((name, unit, "running"))
        elif stopped or changed or before == "failed" or not enabled:
            plan.append((name, unit, "stopped" if stopped else "start"))
    if no_manager:
        lines.append("  no user systemd: module units not managed here")
    if dry:
        lines += [f"  would {'start' if mode == 'start' else 'enable'} {unit} (module {name})" for name, unit, mode in plan]
    elif plan:
        hive_modules._systemctl("daemon-reload")
        for name, unit, mode in plan:
            if mode == "start":
                note = hive_modules._start_unit(unit)
                done = "unit started"
            else:                                               # enable only: never start or restart
                note = "" if hive_modules._systemctl("enable", unit) else "the unit is written, but systemd would not enable it"
                done = "enabled (was running, not enabled)" if mode == "running" else "unit enabled, left stopped"
            lines.append(f"  module {name}: {note or done}")
    return lines
