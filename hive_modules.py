"""`hive-mind module add | remove | update | list | quota` — the module lifecycle (2.1, plan PR 6, M1).

Control plane only: it runs inside `hivemind_ctl.py`, where the owner key is loaded, and `hv` never imports it
(`tests/test_control_plane.py` holds that edge shut). A module is installed outside the checkout, under
`$HIVE_MODULES_DIR` (default `~/.hive/modules/<name>/`), so `hive-mind reset` and `hv verify` never touch it.

**Trust.** A module runs code on this machine, so nothing is installed until its manifest checks out. The
manifest is `module.json`, signed by the module's *publisher key* (`module.json.sig`, Ed25519 over the exact
bytes). It names every file with its sha256, and the fetched tree must hold exactly those files and no others.
The publisher key is not the release key (`hivemind.pub` never signs modules): it is shown at `add` and pinned
in `$HIVE_HOME/.modules.json` once the owner confirms it (trust on first use), and an `update` signed by a
different key is refused. What is installed is the bytes that were hashed: files are copied into a staging
directory and hashed again there, so a repo that changes between the check and the copy installs nothing.

The platform is checked first. Where there is no module backend (everything but Linux with a user supervisor
in 2.1) `add` says so and exits non-zero before it fetches, mints or installs anything.

Units (M2, PR 7) are rendered from the manifest's `service` block into `hive-module-<name>.service` (and `.timer`
when `interval` is set) in the operator's systemd user directory, installed on `add`, re-applied on `update` and
stopped and removed on `remove`. Hooks (M4, PR 8) and the doctor checks (M7, PR 9) read what this records.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import ed25519
import vocabulary

MANIFEST, SIGNATURE = "module.json", "module.json.sig"
STATE_FILE = ".modules.json"
NOT_SUPPORTED = "module management is not supported on this platform yet (2.1 ships on Linux only)"
HOOK_EVENTS = ("session-start", "user-prompt", "precompact", "sessionend", "notification", "stop")
MANIFEST_KEYS = {"name", "version", "min_core", "publisher", "files", "service", "hooks", "config", "quota"}
QUOTA_KEYS = ("per_hour", "per_day", "entry_bytes", "lifetime")
QUOTA_DEFAULTS = {"per_hour": 60, "per_day": 500, "entry_bytes": 16 * 1024, "lifetime": 50000}
MAX_FILES, MAX_FILE_BYTES, MAX_MANIFEST_BYTES = 500, 8 * 1024 * 1024, 256 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ModuleError(Exception):
    """A refusal the operator reads: nothing was installed, minted or signed for it."""


# ── places ──────────────────────────────────────────────────────────────────────────────────────────

def modules_dir():
    return Path(os.environ.get("HIVE_MODULES_DIR") or Path.home() / ".hive" / "modules")


def state_path(hv):
    return hv.HIVE_HOME / ".modules.json"


def load_state(lib):
    try:
        data = json.loads(state_path(lib).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(lib, state):
    path = state_path(lib)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=STATE_FILE + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def platform_supported():
    """(ok, why). The module backend is Linux with a user supervisor; Termux and WSL-less Linux kernels without
    systemd are not it, and say so before anything is touched."""
    if not sys.platform.startswith("linux"):
        return False, NOT_SUPPORTED
    if os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""):
        return False, NOT_SUPPORTED
    return True, ""


# ── the manifest ────────────────────────────────────────────────────────────────────────────────────

def fingerprint(pub_b64):
    """What the operator compares: the first 16 hex of the sha256 of the publisher's public key."""
    return hashlib.sha256(base64.b64decode(pub_b64)).hexdigest()[:16]


def _safe_rel(rel):
    p = Path(rel)
    return (isinstance(rel, str) and rel and not p.is_absolute() and ".." not in p.parts and "\\" not in rel
            and "\x00" not in rel and not rel.startswith("-") and rel not in (MANIFEST, SIGNATURE, "config")
            and ".git" not in p.parts)


def _core_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        raise ModuleError(f"min_core {v!r} is not a dotted version")


def validate_manifest(raw, core_version):
    """Parse and check a manifest's shape; returns the dict. Refuses anything it does not understand rather than
    ignoring it, because this is the text that decides what code gets installed."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ModuleError("manifest is too large")
    try:
        m = json.loads(raw)
    except ValueError as e:
        raise ModuleError(f"manifest is not JSON: {e}")
    if not isinstance(m, dict):
        raise ModuleError("manifest is not an object")
    extra = set(m) - MANIFEST_KEYS
    if extra:
        raise ModuleError(f"manifest names fields this node does not know: {', '.join(sorted(map(str, extra)))}")
    if not vocabulary.valid_module_name(m.get("name")):
        raise ModuleError(f"manifest name {m.get('name')!r} is not a valid module name")
    if not isinstance(m.get("version"), str) or not m["version"] or len(m["version"]) > 64:
        raise ModuleError("manifest needs a version string")
    if _core_tuple(m.get("min_core", "0")) > _core_tuple(core_version):
        raise ModuleError(f"module needs core {m['min_core']} or newer; this node is on {core_version}")
    try:
        if len(base64.b64decode(m.get("publisher", ""), validate=True)) != 32:
            raise ValueError
    except Exception:
        raise ModuleError("manifest publisher is not a base64 Ed25519 public key")
    files = m.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise ModuleError(f"manifest files must name 1 to {MAX_FILES} files")
    for rel, digest in files.items():
        if not _safe_rel(rel):
            raise ModuleError(f"manifest file path {rel!r} is not allowed")
        if not isinstance(digest, str) or not _HEX64.match(digest):
            raise ModuleError(f"manifest digest for {rel!r} is not a sha256")
    svc = m.get("service")
    if svc is not None:
        cmd = svc.get("command") if isinstance(svc, dict) else None
        if (not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) and a for a in cmd)
                or svc.get("restart", "on-failure") not in ("no", "on-failure", "always")
                or not (svc.get("interval") is None or (isinstance(svc["interval"], int) and svc["interval"] >= 60))
                or set(svc) - {"command", "restart", "interval"}):
            raise ModuleError("manifest service needs a command list, restart no|on-failure|always, optional interval >= 60")
        if not cmd[0].startswith("/") and ".." in Path(cmd[0]).parts:
            raise ModuleError("manifest service command may not leave the module's directory")
        if any(ord(c) < 0x20 for a in cmd for c in a):
            raise ModuleError("manifest service command arguments may not hold control characters")
    hooks = m.get("hooks", [])
    if not isinstance(hooks, list) or any(h not in HOOK_EVENTS for h in hooks):
        raise ModuleError(f"manifest hooks must be a list of: {', '.join(HOOK_EVENTS)}")
    for h in hooks:
        if f"hooks/{h}" not in files:
            raise ModuleError(f"manifest hook {h!r} has no hooks/{h} file in `files`")
    cfg = m.get("config", {})
    if not isinstance(cfg, dict) or not all(vocabulary.split_module_name(f"x-{m['name']}:{k}") and isinstance(v, str)
                                            and len(v) <= vocabulary.MODULE_VALUE_MAX for k, v in cfg.items()):
        raise ModuleError("manifest config must map valid key names to string defaults")
    quota = m.get("quota", {})
    if not isinstance(quota, dict) or set(quota) - set(QUOTA_KEYS):
        raise ModuleError(f"manifest quota may only name: {', '.join(QUOTA_KEYS)}")
    for k, v in quota.items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > QUOTA_DEFAULTS[k]:
            raise ModuleError(f"manifest quota {k}={v!r}: a manifest may ask for less than the default "
                              f"({QUOTA_DEFAULTS[k]}), never more; only the owner raises a limit")
    return m


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_tree(root, core_version, tracked=None, ignore=()):
    """Verify the module tree at `root`: the manifest is well-formed, the signature is valid under the manifest's
    own publisher key, every listed file is present, regular and matches its digest, and nothing else is there.
    `tracked` is the set of git-tracked paths when `root` is a checkout (an untracked stray file is not part of
    what was published). Returns the manifest. Raises ModuleError; touches nothing."""
    root = Path(root)
    try:
        raw = (root / MANIFEST).read_bytes()
    except OSError:
        raise ModuleError(f"no {MANIFEST} in the module repository")
    try:
        sig = base64.b64decode("".join((root / SIGNATURE).read_text().split()), validate=True)
    except Exception:
        raise ModuleError(f"no valid {SIGNATURE} in the module repository")
    m = validate_manifest(raw, core_version)
    if not ed25519.verify(raw, sig, base64.b64decode(m["publisher"])):
        raise ModuleError("the manifest signature is not valid under its publisher key: refusing")
    present = set(tracked) if tracked is not None else {
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    extra = {p for p in present if p not in m["files"] and p not in (MANIFEST, SIGNATURE, *ignore)}
    if extra:
        raise ModuleError(f"the repository holds files the manifest does not list: {', '.join(sorted(extra)[:5])}")
    for rel, digest in sorted(m["files"].items()):
        p = root / rel
        st = os.lstat(p) if os.path.lexists(p) else None
        if st is None:
            raise ModuleError(f"manifest lists {rel!r}, which is missing")
        if not os.path.isfile(p) or os.path.islink(p):
            raise ModuleError(f"{rel!r} is not a regular file (links are refused)")
        if st.st_size > MAX_FILE_BYTES:
            raise ModuleError(f"{rel!r} is over {MAX_FILE_BYTES} bytes")
        if _sha256_file(p) != digest:
            raise ModuleError(f"{rel!r} does not match its manifest digest: refusing")
    return m


def _fetch(repo, dest):
    """Clone `repo` (a path or URL) into `dest`, and return (tracked files, commit). Uses the operator's own git
    credentials; the `ext::` transport, which runs commands, is off."""
    if not repo or repo.startswith("-"):
        raise ModuleError(f"{repo!r} is not a repository")
    env = dict(os.environ, GIT_ALLOW_PROTOCOL="file:https:ssh:git")
    try:
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--", repo, str(dest)], env=env,
                       check=True, capture_output=True, text=True, timeout=300)
        ls = subprocess.run(["git", "-C", str(dest), "ls-files", "-z"], check=True, capture_output=True, timeout=30)
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise ModuleError(f"could not fetch {repo}: {(e.stderr or '').strip().splitlines()[-1:] or 'git failed'}")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ModuleError(f"could not fetch {repo}: {e}")
    return [n.decode() for n in ls.stdout.split(b"\x00") if n], head


def _stage(src, manifest, staging):
    """Copy the listed files into `staging` and hash them again there: what is installed is what was hashed."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for rel in [*manifest["files"], MANIFEST, SIGNATURE]:
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, dst)
        os.chmod(dst, 0o755 if os.access(src / rel, os.X_OK) else 0o644)
    for rel, digest in manifest["files"].items():
        if _sha256_file(staging / rel) != digest:
            raise ModuleError(f"{rel!r} changed while it was being copied: refusing")


def _confirm_publisher(manifest, expected, pinned=None):
    """Trust on first use. The operator must name the key they checked (`--publisher`) or confirm it at a
    prompt; there is no bare --yes, because a flag that skips the look is not a pin. A pinned key must match."""
    pub = manifest["publisher"]
    if pinned is not None:
        if pub != pinned:
            raise ModuleError(f"the update is signed by publisher {fingerprint(pub)}, but this module is pinned to "
                              f"{fingerprint(pinned)}: refusing. Remove and re-add it to change publishers.")
        return
    print(f"module {manifest['name']} {manifest['version']} is signed by publisher key {pub} (fingerprint {fingerprint(pub)})")
    if expected is not None:
        if expected != pub:
            raise ModuleError(f"--publisher does not match the manifest's publisher key ({fingerprint(pub)}): refusing")
        return
    if not sys.stdin.isatty():
        raise ModuleError("pin this publisher key by passing it: --publisher <key> (shown above)")
    if input("Trust this publisher for this module and pin it? [y/N] ").strip().lower() not in ("y", "yes"):
        raise ModuleError("publisher not confirmed: nothing installed")


# ── the owner steps ─────────────────────────────────────────────────────────────────────────────────

def _gov(lib):
    import merkle
    return lib._governance_state(merkle.read_all_entries(lib.JOURNAL_DIR))


def _require_owner(lib):
    """Unlock the owner key before anything is fetched into place, so a locked key or a non-owner device
    refuses with nothing written."""
    if not lib._owner_key_exists():
        raise ModuleError("this device does not hold the owner key: run module commands on the owner machine")
    if lib._owner_seed() is None:
        raise ModuleError("the owner key could not be unlocked")


def _operator_principal(lib, given):
    if given:
        return given
    p = (_gov(lib).get("principals") or {}).get(lib.NODE_ID)
    if not p:
        raise ModuleError("this device has no principal: pass --principal <the operator's principal>, so the "
                          "module falls under cap_self")
    return p


def _device_state(lib, device_id):
    gov = _gov(lib)
    if device_id in gov["purged"]:
        return "purged"
    return "admitted" if device_id in gov["admitted"] else "revoked"


# ── the unit (M2) ───────────────────────────────────────────────────────────────────────────────────

def unit_dir():
    return Path.home() / ".config" / "systemd" / "user"


def unit_names(name):
    return f"hive-module-{name}.service", f"hive-module-{name}.timer"


def _systemctl(*args):
    """Run `systemctl --user ...`; False when there is no user manager or the call fails. Never raises."""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _quote(arg):
    """One ExecStart word: systemd splits on whitespace and expands `%` and `$`, so quote and double them."""
    esc = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{esc}"'


def _env(key, value):
    """One `Environment="K=V"` line: the quotes keep a space in the value, `%` is doubled against specifiers."""
    esc = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{esc}"'


def render_units(name, manifest, module_dir, key_dir):
    """{filename: text} for a module's unit(s), or {} when its manifest has no `service` block.

    It runs as the operator's user (a user unit), in the module's own directory, with the module's own key
    directory and nothing of the owner's in its environment. The command's first word is made absolute: a path
    inside the module (one it ships, or any relative path) is anchored to its directory and any other bare name is
    looked up by `env`. Unlike the sync unit it
    has no `ExecStartPre` that kills by command line (such a pattern could match another module)."""
    svc = manifest.get("service")
    if not svc:
        return {}
    cmd = list(svc["command"])
    if not cmd[0].startswith("/"):
        own = "/" in cmd[0] or cmd[0] in manifest.get("files", {})
        cmd = [str(module_dir / cmd[0]), *cmd[1:]] if own else ["/usr/bin/env", *cmd]
    interval, restart = svc.get("interval"), svc.get("restart", "on-failure")
    service, timer = unit_names(name)
    lines = ["[Unit]", f"Description=Hive Mind module {name}", "After=network.target", ""]
    lines += ["[Service]", f"Type={'oneshot' if interval else 'simple'}", f"WorkingDirectory={str(module_dir).replace('%', '%%')}",
              "ExecStart=" + " ".join(_quote(a) for a in cmd),
              _env("HIVE_MODULE_NAME", name), _env("HIVE_MODULE_DIR", module_dir),
              _env("HIVE_MODULE_KEY_DIR", key_dir), "NoNewPrivileges=yes",
              "StandardOutput=journal", "StandardError=journal"]
    if not interval:                    # a timer re-fires a oneshot; a daemon is kept up by its restart policy
        lines += [f"Restart={restart}", "RestartSec=5", "KillMode=control-group"]
        lines += ["", "[Install]", "WantedBy=default.target"]
    out = {service: "\n".join(lines) + "\n"}
    if interval:
        out[timer] = "\n".join(["[Unit]", f"Description=Run Hive Mind module {name} every {interval}s", "",
                                 "[Timer]", f"OnBootSec={interval}", f"OnUnitActiveSec={interval}",
                                 "", "[Install]", "WantedBy=timers.target"]) + "\n"
    return out


def _write_units(lib, name, manifest, only_changed=False):
    """Stop whatever is installed, then write the module's unit files (and drop a timer it no longer declares).
    Returns the unit to enable and start (the timer when there is one), or None when there is nothing to start:
    no service block, or `only_changed` and the files are already what the manifest renders."""
    keep = render_units(name, manifest, modules_dir() / name, Path(lib._ensure_key_dir()) / "modules" / name)
    if not keep:
        remove_units(name)
        return None
    d = unit_dir()
    if only_changed and all((d / f).exists() and (d / f).read_text() == keep.get(f) for f in unit_names(name)
                            if f in keep) and not any((d / f).exists() for f in unit_names(name) if f not in keep):
        return None
    d.mkdir(parents=True, exist_ok=True)
    for fname in unit_names(name)[::-1]:                    # a daemon that becomes a timer must not keep running
        if (d / fname).exists():
            _systemctl("disable", "--now", fname)
            if fname not in keep:
                (d / fname).unlink()
    for fname, text in keep.items():
        (d / fname).write_text(text)
    return next(f for f in unit_names(name)[::-1] if f in keep)


def _start_unit(unit):
    """Enable and (re)start `unit`; a note for the operator when systemd would not."""
    if not _systemctl("enable", unit):
        return "the unit is written, but systemd would not enable it"
    if not _systemctl("restart", unit):
        return "the unit is enabled, but systemd would not start it"
    return ""


def install_units(lib, name, manifest):
    """Write the module's unit files and, where a user systemd manager is reachable, enable and (re)start them.
    Without one the files are still written; returns a note for the operator."""
    unit = _write_units(lib, name, manifest)
    if unit is None:
        return ""
    if not _systemctl("show-environment"):
        return "no user systemd manager is reachable: the unit is written, not started"
    _systemctl("daemon-reload")
    return _start_unit(unit)


def remove_units(name):
    """Stop, disable and delete the module's units. Safe when there are none."""
    service, timer = unit_names(name)
    d = unit_dir()
    if not any((d / f).exists() for f in (service, timer)):
        return
    for f in (timer, service):
        _systemctl("disable", "--now", f)
        (d / f).unlink(missing_ok=True)
    # an older renderer wrote Persistent=true, which left this stamp: it marks OnBootSec as elapsed, so a re-added timer never fires
    (Path.home() / ".local" / "share" / "systemd" / "timers" / f"stamp-{timer}").unlink(missing_ok=True)
    _systemctl("daemon-reload")
    _systemctl("reset-failed", service)


def main_unit(name):
    """The unit that is the module's switch: the timer when it has one, else the service."""
    service, timer = unit_names(name)
    return timer if (unit_dir() / timer).exists() else service


def unit_enabled(name):
    """Whether systemd has the module's main unit enabled (False without a user manager)."""
    return _systemctl("is-enabled", main_unit(name))


def service_state(name):
    """'none' without a unit, else systemd's word for it ('active', 'inactive', 'failed') or 'installed'."""
    service, timer = unit_names(name)
    d = unit_dir()
    if not (d / service).exists():
        return "none"
    unit = main_unit(name)
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=10)
        word = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        word = ""
    return word if word in ("active", "inactive", "failed", "activating") else "installed"


# ── verbs ───────────────────────────────────────────────────────────────────────────────────────────

def _write_config(dest_dir, defaults):
    """Per-node config (M3): `<module dir>/config`, local and never journaled. Existing values survive an update;
    only keys the file lacks take the manifest's default."""
    path = dest_dir / "config"
    have = {}
    try:
        have = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    have = have if isinstance(have, dict) else {}
    merged = {**defaults, **have}
    path.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def cmd_add(lib, args):
    ok, why = platform_supported()
    if not ok:
        raise ModuleError(why)
    name = args.name
    if not vocabulary.valid_module_name(name):
        raise ModuleError(f"{name!r} is not a valid module name")
    state = load_state(lib)
    root = modules_dir()
    if name in state or (root / name).exists():
        raise ModuleError(f"module {name!r} is already installed: use `hive-mind module update {name}`")
    _require_owner(lib)
    principal = _operator_principal(lib, args.principal)
    with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
        src = Path(tmp) / "src"
        tracked, head = _fetch(args.source, src)
        manifest = check_tree(src, lib.CONTRACT_VERSION, tracked)
        if manifest["name"] != name:
            raise ModuleError(f"the repository holds module {manifest['name']!r}, not {name!r}")
        _confirm_publisher(manifest, args.publisher)
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        staging = root / f".{name}.staging"
        try:
            _stage(src, manifest, staging)
            device_id, _pub = lib.mint_module_key(name)
        except (ModuleError, FileExistsError, OSError, ValueError, RuntimeError) as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise ModuleError(str(e))
        try:
            lib.admit_cmd(argparse.Namespace(device_id=device_id, principal=principal, module=name))
            gov = _gov(lib)
            if gov["modules"].get(device_id) != name or device_id not in gov["admitted"]:
                raise ModuleError("the owner could not admit the module's device: nothing installed")
            _write_config(staging, manifest.get("config", {}))
            os.rename(staging, root / name)
            note = install_units(lib, name, manifest)
            state[name] = {"version": manifest["version"], "publisher": manifest["publisher"], "device_id": device_id,
                           "source": args.source, "commit": head, "installed_at": int(time.time()),
                           "quota": dict(manifest.get("quota") or {}), "owner_quota": {}}
            save_state(lib, state)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            _undo_add(lib, name, device_id)
            raise
    print(f"installed module {name} {manifest['version']} (device {device_id}, principal {principal}); "
          + (f"service {unit_names(name)[0]}" + (" with a timer" if manifest["service"].get("interval") else "")
             if manifest.get("service") else "no service")
          + (f" ({note})" if note else "") + "; its hooks arrive with a later 2.1 change")


def _undo_add(lib, name, device_id):
    """A failed add leaves nothing: the key goes, and a device that was admitted is revoked."""
    try:
        if _device_state(lib, device_id) == "admitted":
            lib._group_change("revoke", device_id)
    except Exception:
        pass
    shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / name, ignore_errors=True)
    shutil.rmtree(modules_dir() / name, ignore_errors=True)
    remove_units(name)


def cmd_remove(lib, args):
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    _require_owner(lib)
    device_id = rec["device_id"]
    if _device_state(lib, device_id) == "admitted":
        lib._group_change("revoke", device_id)
    if _device_state(lib, device_id) == "admitted":
        raise ModuleError("the owner could not revoke the module's device: nothing removed")
    remove_units(args.name)                       # stop it before its files and key go
    shutil.rmtree(modules_dir() / args.name, ignore_errors=True)
    shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / args.name, ignore_errors=True)
    state.pop(args.name)
    save_state(lib, state)
    print(f"removed module {args.name}; its device {device_id} is revoked and its entries stay in the journal")


def cmd_update(lib, args):
    ok, why = platform_supported()
    if not ok:
        raise ModuleError(why)
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    _require_owner(lib)
    source = args.source or rec["source"]
    root = modules_dir()
    with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
        src = Path(tmp) / "src"
        tracked, head = _fetch(source, src)
        manifest = check_tree(src, lib.CONTRACT_VERSION, tracked)
        if manifest["name"] != args.name:
            raise ModuleError(f"the repository holds module {manifest['name']!r}, not {args.name!r}")
        _confirm_publisher(manifest, args.publisher, pinned=rec["publisher"])
        staging, old = root / f".{args.name}.staging", root / f".{args.name}.old"
        try:
            _stage(src, manifest, staging)
        except (ModuleError, OSError):
            shutil.rmtree(staging, ignore_errors=True)
            raise
        live = root / args.name
        swapped = False
        try:
            _write_config(staging, {**manifest.get("config", {}),
                                    **(json.loads((live / "config").read_text()) if (live / "config").exists() else {})})
            shutil.rmtree(old, ignore_errors=True)
            os.rename(live, old)                  # two renames: a crash between them leaves the old copy at `.old`
            swapped = True                        # only now is `.old` this update's; a stale one is never rolled back
            os.rename(staging, live)
            note = install_units(lib, args.name, manifest)
        except BaseException:
            if swapped:
                shutil.rmtree(live, ignore_errors=True)
                os.rename(old, live)
                try:                                  # put the old unit back beside the old files
                    install_units(lib, args.name, json.loads((live / MANIFEST).read_text()))
                except (OSError, ValueError, KeyError):
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(old, ignore_errors=True)
        rec.update(version=manifest["version"], source=source, commit=head,
                   quota=dict(manifest.get("quota") or {}))     # the owner's own limits (owner_quota) are kept
        save_state(lib, state)
    print(f"updated module {args.name} to {manifest['version']}; its key, device and pinned publisher are unchanged"
          + (f" ({note})" if note else ""))


def _installed_signature(lib, name):
    """'valid' when the installed tree still matches its signed manifest, else why not."""
    d = modules_dir() / name
    try:
        check_tree(d, lib.CONTRACT_VERSION, ignore=("config",))     # `config` is the node's own, not published
    except ModuleError as e:
        return f"INVALID ({e})"
    return "valid"


def _quota_use(lib, device_id):
    now = time.time()
    try:
        times = json.loads((Path(lib.HIVE_HOME) / ".module-quota.json").read_text()).get(device_id, [])
    except (OSError, ValueError, AttributeError):
        times = []
    times = [t for t in times if isinstance(t, (int, float)) and now - 86400 < t <= now]
    life = sum(1 for n, _ in lib.journal_keys() if n == device_id)
    return f"{sum(1 for t in times if t > now - 3600)}/h {len(times)}/d {life} total"


def cmd_list(lib, args):
    state = load_state(lib)
    if not state:
        print("No modules installed.")
        return
    print(f"{'NAME':<16}{'VERSION':<10}{'PUBLISHER':<18}{'SIGNATURE':<10}{'SERVICE':<14}{'DEVICE':<10}QUOTA USE")
    for name, rec in sorted(state.items()):
        sig = _installed_signature(lib, name)
        print(f"{name:<16}{rec['version']:<10}{fingerprint(rec['publisher']):<18}{sig.split(' ')[0]:<10}"
              f"{service_state(name):<14}{_device_state(lib, rec['device_id']):<10}{_quota_use(lib, rec['device_id'])}")
        if sig != "valid":
            print(f"  ! {sig}")


def cmd_quota(lib, args):
    """The owner's way to raise or lower one limit. A manifest may only ask for less; this is how it goes up."""
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    _require_owner(lib)
    mine = dict(rec.get("owner_quota") or {})
    for k in QUOTA_KEYS:
        v = getattr(args, k)
        if v is not None:
            if v < 0:
                raise ModuleError(f"{k} must be >= 0")
            mine[k] = v
    rec["owner_quota"] = mine
    save_state(lib, state)
    eff = {**QUOTA_DEFAULTS, **(rec.get("quota") or {}), **mine}
    print(f"{args.name} limits: " + ", ".join(f"{k}={eff[k]}" for k in QUOTA_KEYS))


def cmd_reapply(lib, args):
    """Internal, run by `hive-mind update` after a core update: render every installed module's units again, write
    the files that changed, reload systemd once and restart only those units. It never fails: a module that cannot
    be re-applied is a warning that names it."""
    if not platform_supported()[0]:
        return
    pending = []
    for name in sorted(load_state(lib)):
        try:
            manifest = check_tree(modules_dir() / name, lib.CONTRACT_VERSION, ignore=("config",))
            unit = _write_units(lib, name, manifest, only_changed=True)
        except (ModuleError, OSError, ValueError, KeyError) as e:
            print(f"hive-mind module: warning: {name}: units not re-applied ({e})", file=sys.stderr)
            continue
        if unit:
            pending.append((name, unit))
    if not pending:
        return
    if not _systemctl("show-environment"):
        print("hive-mind module: warning: no user systemd manager is reachable: units written, not restarted",
              file=sys.stderr)
        return
    _systemctl("daemon-reload")
    for name, unit in pending:
        note = _start_unit(unit)
        if note:
            print(f"hive-mind module: warning: {name}: {note}", file=sys.stderr)


def build_parser():
    p = argparse.ArgumentParser(prog="hive-mind module", description="Install and manage modules (Linux, 2.1).")
    sub = p.add_subparsers(dest="verb", required=True)
    a = sub.add_parser("add", help="fetch, verify and install a module, mint its device key and admit it")
    a.add_argument("name")
    a.add_argument("--from", dest="source", required=True, help="the module's git repository (path or URL)")
    a.add_argument("--publisher", help="the publisher key you checked (base64); pins it without a prompt")
    a.add_argument("--principal", help="the operator's principal for the module's device (default: this device's)")
    r = sub.add_parser("remove", help="revoke the module's device and delete it (its entries stay in the journal)")
    r.add_argument("name")
    u = sub.add_parser("update", help="re-verify and swap atomically; the publisher key must match the pin")
    u.add_argument("name")
    u.add_argument("--from", dest="source", help="a different repository (default: where it came from)")
    u.add_argument("--publisher", help="ignored unless it equals the pinned key")
    sub.add_parser("list", help="installed modules: version, publisher, signature, service, device, quota use")
    sub.add_parser("reapply", help=argparse.SUPPRESS)           # internal: `hive-mind update` re-renders the units
    q = sub.add_parser("quota", help="the owner raises or lowers a module's limits")
    q.add_argument("name")
    for k in QUOTA_KEYS:
        q.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int)
    return p


VERBS = {"add": cmd_add, "remove": cmd_remove, "update": cmd_update, "list": cmd_list, "quota": cmd_quota,
         "reapply": cmd_reapply}


def main(lib, argv):
    """Run `hive-mind module <argv>` against the control-plane library `lib`. Returns an exit status."""
    args = build_parser().parse_args(argv)
    try:
        VERBS[args.verb](lib, args)
    except ModuleError as e:
        print(f"hive-mind module: {e}", file=sys.stderr)
        return 1
    return 0
