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

**Without the owner key (M1b).** A device that does not hold the owner key can still run a module the owner admits.
`module add` there fetches, verifies, stages and mints the module's key, writes a pending record beside the staging
directory, prints the owner's `hive-mind group admit <device> --module <name> --principal <p>` line with the
publisher fingerprint and the commit that was verified, and stops: no unit, no `.modules.json` row, no admit. Once the
owner's admit has reached this node, a second `module add` installs the staged tree (re-checked against the signed
manifest and the digest recorded at the first run); `module add --abort` removes the staging and the key.
`update` needs no owner key (it checks the tree and the pinned publisher as before and changes no membership or
quota), `remove` deletes locally and prints the owner's `group revoke` line, and `quota` stays owner-only.

Units (M2, PR 7) are rendered from the manifest's `service` block into `hive-module-<name>.service` (and `.timer`
when `interval` is set) in the operator's systemd user directory, installed on `add`, re-applied on `update` and
stopped and removed on `remove`. Hooks (M4, PR 8) and the doctor checks (M7, PR 9) read what this records.
"""

import argparse
import base64
import contextlib
import getpass
import hashlib
import io
import json
import os
import re
import shlex
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import ed25519
import hive_runtime
import vocabulary

MANIFEST, SIGNATURE = "module.json", "module.json.sig"
STATE_FILE = ".modules.json"
NOT_SUPPORTED = "module management is not supported on this platform yet (2.1 ships on Linux only)"
HOOK_EVENTS = ("session-start", "user-prompt", "precompact", "sessionend", "notification", "stop")
BASE_KEYS = {"name", "version", "min_core", "publisher", "files", "service", "hooks", "config", "quota"}
# Manifest contract 1 (3.1): optional, and only under `contract`. A manifest without `contract` is a 2.1/3.0 manifest.
CONTRACT_KEYS = ("cli", "runtimes", "config_schema", "status", "doctor", "reads", "ui", "trust", "commands")
MANIFEST_KEYS = BASE_KEYS | {"contract", *CONTRACT_KEYS}
CONTRACTS = (1,)                                    # the manifest contracts this core knows
TRUST_TIERS = ("confined", "trusted")                # lowest first; a manifest without `trust` is `trusted`
CONTRACT_MIN_CORE = {1: (3, 1)}                     # the oldest core a manifest of that contract may name in `min_core`
QUOTA_KEYS = ("per_hour", "per_day", "entry_bytes", "lifetime")
QUOTA_DEFAULTS = {"per_hour": 60, "per_day": 500, "entry_bytes": 16 * 1024, "lifetime": 50000}
MAX_FILES, MAX_FILE_BYTES, MAX_MANIFEST_BYTES = 500, 8 * 1024 * 1024, 256 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ModuleError(Exception):
    """A refusal the operator reads: nothing was installed, minted or signed for it."""


class OwnerWait(ModuleError):
    """The install waits for the owner's admit to reach this node (`module install` exits 3)."""


class OwnerLocked(ModuleError):
    """The owner key is sealed and could not be used without asking (`module install` exits 5)."""


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


MAX_STOP_GRACE = 3600       # seconds a service may take to drain on stop before systemd kills it


def validate_manifest(raw, core_version):
    """Parse and check a manifest's shape; returns the dict. Refuses anything it does not understand rather than
    ignoring it, because this is the text that decides what code gets installed."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ModuleError("manifest is too large")
    try:
        m = json.loads(raw)
    except (ValueError, RecursionError) as e:
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
                or not (svc.get("stop_grace") is None or (isinstance(svc["stop_grace"], int) and not isinstance(svc["stop_grace"], bool)
                                                         and 1 <= svc["stop_grace"] <= MAX_STOP_GRACE))
                or set(svc) - {"command", "restart", "interval", "stop_grace"}):
            raise ModuleError("manifest service needs a command list, restart no|on-failure|always, optional interval >= 60, "
                              f"optional stop_grace 1 to {MAX_STOP_GRACE} seconds")
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
    for rel in files:
        if rel.startswith("hooks/") and rel[len("hooks/"):] not in hooks:
            raise ModuleError(f"manifest file {rel!r} is a hook the manifest's `hooks` list does not declare")
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
    _validate_contract(m)
    return m


# ── manifest contract 1 (3.1) ───────────────────────────────────────────────────────────────────────

MAX_ARGV, MAX_ARG = 64, 1024
CLI_NAME = re.compile(r"[a-z][a-z0-9-]{1,31}\Z")
CLI_RESERVED = frozenset({"hv", "hive-mind", "hive-dispatch", "hive-sync", "hive-doctor", "hive-module", "hive-installer",
                          "hivemind", "module", "modules"})
RUNTIME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAX_RUNTIMES = 16
READ_ROUTES = ("overview", "tags", "related", "audit", "peers", "verify", "node", "series")
TAILNET_READS = ("peers", "node")                   # carry tailnet addresses
NODE_LOCAL_READS = ("overview", "peers", "verify")  # carry this node's own state, not only the feed's


def _reads_record(manifest):
    """{"reads": [...]} for the `.modules.json` row of a manifest that declares read routes, else {}: the row of a
    manifest without `contract` is byte-for-byte what it was. `hive_module_api` serves a route only if it is here."""
    return {"reads": list(manifest["reads"])} if manifest.get("reads") else {}


def signed_trust(manifest):
    """The tier the publisher signed: `trusted` when the manifest does not say."""
    return manifest.get("trust", "trusted")


def effective_trust(rec):
    """The tier this node holds a module to: the lower of what the publisher signed and what the operator set locally.
    A row from before the field existed reads as `trusted`, as a manifest without it does."""
    signed, local = rec.get("trust", "trusted"), rec.get("trust_local")
    return local if local in TRUST_TIERS and TRUST_TIERS.index(local) < TRUST_TIERS.index(signed) else signed


def manifest_trust(manifest, rec):
    """The tier a unit is rendered for: what the manifest signs, lowered by the record's node-local setting. The
    manifest, not the record, supplies the signed tier, so an update's new tier takes effect in the same render."""
    return effective_trust({"trust": signed_trust(manifest), "trust_local": (rec or {}).get("trust_local")})


def _trust_record(manifest):
    """{"trust": "confined"} for a module signed `confined`, else {}: a `trusted` row is byte-for-byte what it was."""
    return {"trust": "confined"} if signed_trust(manifest) == "confined" else {}


def trust_summary(manifest):
    """The install-summary line for a manifest's tier. `trusted` is stated loudly: it is the tier of a manifest that
    says nothing, and it runs with this user's access."""
    if signed_trust(manifest) == "confined":
        return "trust: confined (the publisher signed it to run with reduced access)"
    return "trust: TRUSTED (runs as your user, with your user's access; the publisher did not sign it as confined)"


def reads_summary(manifest):
    """The install-summary line for a manifest's `reads`, or "" when it declares none. Marks what carries tailnet
    addresses and what is node-local, so the operator sees what the module is being given before it is admitted."""
    reads = manifest.get("reads") or []
    if not reads:
        return ""
    marks = [r + (" [tailnet addresses]" if r in TAILNET_READS else "") + (" [node-local]" if r in NODE_LOCAL_READS else "")
             for r in reads]
    return "reads (/v1, read-only): " + ", ".join(marks)
CONFIG_TYPES, CONFIG_SCOPES = ("string", "int", "bool", "enum"), ("node", "fleet", "fleet-node")
CONFIG_SPEC_KEYS = {"type", "default", "scope", "description", "max", "min", "values", "required_at_install",
                    "secret", "identifying", "help"}
MAX_SCHEMA_KEYS, MAX_DESCRIPTION, MAX_ENUM_VALUES, MAX_ENUM_VALUE = 128, 400, 64, 80
_SEGMENT_WORD = re.compile(r"\{[a-z][a-z0-9_]{0,31}\}\Z")
_INT_TEXT = re.compile(r"-?[0-9]{1,18}\Z")
UI_SCHEMA_FILE = "ui_block.schema.json"
UI_SCHEMAS = (1,)                                   # the `ui.schema` versions this core knows
MAX_UI_BYTES = 64 * 1024
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _has_control(text):
    return any(ord(c) < 0x20 or ord(c) == 0x7f for c in text)


def _has_unprintable(text):
    """Control, format (bidi, zero-width) and line-break characters: C0, DEL, C1, U+200B, U+202E, U+2028 and so on."""
    return any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text)


def _check_argv(label, cmd):
    """An argv list with `service.command`'s rules: strings, no control characters, and a relative first word may not
    leave the module's directory. It is run without a shell."""
    if (not isinstance(cmd, list) or not cmd or len(cmd) > MAX_ARGV
            or not all(isinstance(a, str) and a and len(a) <= MAX_ARG for a in cmd)):
        raise ModuleError(f"manifest {label} needs a list of 1 to {MAX_ARGV} non-empty strings of at most {MAX_ARG} characters")
    if not cmd[0].startswith("/") and ".." in Path(cmd[0]).parts:
        raise ModuleError(f"manifest {label} may not leave the module's directory")
    if any(_has_control(a) for a in cmd):
        raise ModuleError(f"manifest {label} arguments may not hold control characters")
    _check_bare_word(label, cmd[0])


def _check_bare_word(label, first):
    """A first word with no slash is looked up on PATH through `env`, which would take `-x` for an option and
    `A=b` for an assignment: neither is a program name."""
    if "/" not in first and (first.startswith("-") or "=" in first):
        raise ModuleError(f"manifest {label} starts with {first!r}, which is not a program name")


def _config_key_ok(module, key):
    """A `config_schema` key: a `x-<module>:` local name, where a whole dot-segment `{word}` stands for any one
    segment (`seat.{role}.runtime`)."""
    if not isinstance(key, str):
        return False
    probe = ".".join("w" if _SEGMENT_WORD.match(seg) else seg for seg in key.split("."))
    return vocabulary.split_module_name(f"x-{module}:{probe}") is not None


def config_value_problem(spec, value):
    """None when `value` (a string, as the journal and the node file hold it) fits the schema entry `spec`, else why."""
    if not isinstance(value, str) or len(value) > vocabulary.MODULE_VALUE_MAX or _has_control(value):
        return "not a string of printable characters within the value limit"
    kind = spec["type"]
    if kind == "string":
        return None if len(value) <= spec.get("max", vocabulary.MODULE_VALUE_MAX) else f"longer than {spec['max']}"
    if kind == "bool":
        return None if value in ("true", "false") else "not true or false"
    if kind == "enum":
        return None if value in spec["values"] else f"not one of {', '.join(spec['values'])}"
    if not _INT_TEXT.match(value):
        return "not an integer"
    n = int(value)
    if "min" in spec and n < spec["min"] or "max" in spec and n > spec["max"]:
        return f"outside {spec.get('min', '-inf')}..{spec.get('max', 'inf')}"
    return None


def _check_config_entry(module, key, spec):
    where = f"manifest config_schema {key!r}"
    if not _config_key_ok(module, key):
        raise ModuleError(f"{where}: not a valid config key name")
    if not isinstance(spec, dict) or set(spec) - CONFIG_SPEC_KEYS:
        raise ModuleError(f"{where} is an object that may only name: {', '.join(sorted(CONFIG_SPEC_KEYS))}")
    kind = spec.get("type")
    if kind not in CONFIG_TYPES:
        raise ModuleError(f"{where}: type must be one of {', '.join(CONFIG_TYPES)}")
    scope = spec.get("scope", "node")
    if scope not in CONFIG_SCOPES:
        raise ModuleError(f"{where}: scope must be one of {', '.join(CONFIG_SCOPES)}")
    desc = spec.get("description")
    if not isinstance(desc, str) or not desc.strip() or len(desc) > MAX_DESCRIPTION or _has_control(desc):
        raise ModuleError(f"{where}: description is required (a string of at most {MAX_DESCRIPTION} characters)")
    if "help" in spec:
        text = spec["help"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_DESCRIPTION or _has_control(text):
            raise ModuleError(f"{where}: help must be a string of at most {MAX_DESCRIPTION} characters")
    for flag in ("required_at_install", "secret", "identifying"):
        if flag in spec and not isinstance(spec[flag], bool):
            raise ModuleError(f"{where}: {flag} must be true or false")
    allowed = {"string": {"max"}, "int": {"min", "max"}, "bool": set(), "enum": {"values"}}[kind]
    for bound in ("min", "max", "values"):
        if bound in spec and bound not in allowed:
            raise ModuleError(f"{where}: {bound} does not apply to type {kind}")
    if kind == "string" and "max" in spec and not (_is_int(spec["max"]) and 1 <= spec["max"] <= vocabulary.MODULE_VALUE_MAX):
        raise ModuleError(f"{where}: max must be 1 to {vocabulary.MODULE_VALUE_MAX}")
    if kind == "int":
        for bound in ("min", "max"):
            if bound in spec and not (_is_int(spec[bound]) and abs(spec[bound]) < 10 ** 18):
                raise ModuleError(f"{where}: {bound} must be an integer")
        if "min" in spec and "max" in spec and spec["min"] > spec["max"]:
            raise ModuleError(f"{where}: min is above max")
    if kind == "enum":
        vals = spec.get("values")
        if (not isinstance(vals, list) or not 1 <= len(vals) <= MAX_ENUM_VALUES or len(set(map(str, vals))) != len(vals)
                or not all(isinstance(v, str) and v and len(v) <= MAX_ENUM_VALUE and not _has_control(v) for v in vals)):
            raise ModuleError(f"{where}: an enum needs 1 to {MAX_ENUM_VALUES} distinct strings in `values`")
    if spec.get("secret") and scope != "node":
        raise ModuleError(f"{where}: a secret key is node-scoped (a fleet value is journaled for every device)")
    if "default" in spec:
        if spec.get("identifying") or spec.get("secret"):
            raise ModuleError(f"{where}: a key marked identifying or secret may not ship a default")
        problem = config_value_problem(spec, spec["default"])
        if problem:
            raise ModuleError(f"{where}: default is {problem}")


def _check_ui(m):
    ui = m["ui"]
    if not isinstance(ui, dict) or len(json.dumps(ui, ensure_ascii=False).encode()) > MAX_UI_BYTES:
        raise ModuleError(f"manifest ui must be an object of at most {MAX_UI_BYTES // 1024} KiB")
    if ui.get("schema") not in UI_SCHEMAS or not _is_int(ui.get("schema")):
        raise ModuleError(f"manifest ui schema must be one this node knows ({', '.join(map(str, UI_SCHEMAS))})")
    todo = [ui]
    while todo:                                             # text is shown as text, but never carries control characters
        node = todo.pop()
        for k, v in (node.items() if isinstance(node, dict) else enumerate(node)):
            if isinstance(k, str) and _has_control(k) or isinstance(v, str) and _has_control(v):
                raise ModuleError("manifest ui holds a string with control characters")
            if isinstance(v, (dict, list)):
                todo.append(v)
    _schema_check(ui, load_ui_schema(), "manifest ui")
    for group in ("metrics", "panels", "actions"):
        ids = [x["id"] for x in ui.get(group, [])]
        if len(set(ids)) != len(ids):
            raise ModuleError(f"manifest ui {group} repeat an id")
    own = f"x-{m['name']}:"
    for item in (*ui.get("metrics", []), *ui.get("panels", [])):
        if item.get("from") == "entities" and not str(item.get("prefix", "")).startswith(own):
            raise ModuleError(f"manifest ui {item['id']!r} reads entities, so it needs a `prefix` starting {own!r}: "
                              "a module's page shows its own entities")
    for act in ui.get("actions", []):
        _check_argv(f"ui action {act['id']!r} command", act["command"])
        used = {n for a in act["command"] for n in _PLACEHOLDER.findall(a)}
        params = set(act.get("params", {}))
        if _PLACEHOLDER.search(act["command"][0]):
            raise ModuleError(f"manifest ui action {act['id']!r}: the command itself may not be a parameter")
        if used != params:
            raise ModuleError(f"manifest ui action {act['id']!r}: the parameters {sorted(params)} and the placeholders "
                              f"{sorted(used)} in its command must match")


# ── declared commands (4.2) ─────────────────────────────────────────────────────────────────────────

COMMANDS_MIN_CORE = (4, 2)
MAX_COMMANDS, MAX_UNDO_WORDS, MAX_UNDO_WORD = 64, 16, 128
COMMAND_KEY = re.compile(r"[a-z][a-z0-9-]{0,31}( [a-z][a-z0-9-]{0,31}){0,3}\Z")
COMMAND_FIELDS = ("dry_run", "fleet", "mutates", "reversible", "undo")
_UNDO_PLACEHOLDER = re.compile(r"\{[a-z][a-z0-9_]{0,31}\}\Z")


def _undo_words(undo):
    """The undo template's words up to the first placeholder or option: the words that name the undo verb."""
    words = []
    for w in undo:
        if w.startswith("-") or "{" in w:
            break
        words.append(w)
    return words


def find_verb(commands, words):
    """The longest key of `commands` that the leading non-option `words` begin with, else None."""
    lead = []
    for w in words:
        if w.startswith("-"):
            break
        lead.append(w)
    for n in range(min(len(lead), 4), 0, -1):
        if " ".join(lead[:n]) in commands:
            return " ".join(lead[:n])
    return None


def _check_commands(m):
    """The `commands` map (4.2): which verbs of the module's cli change state, whether they can be undone, and how."""
    cmds, where = m["commands"], "manifest commands"
    if not isinstance(cmds, dict) or len(cmds) > MAX_COMMANDS:
        raise ModuleError(f"{where} must be an object of at most {MAX_COMMANDS} verbs")
    if "contract" not in m or not m.get("cli"):
        raise ModuleError(f'{where} needs "contract": 1 and a cli')
    if _core_tuple(m.get("min_core", "0")) < COMMANDS_MIN_CORE:
        raise ModuleError(f"{where} needs min_core 4.2 or newer (a 4.1 core refuses the field)")
    for key, e in cmds.items():
        if not isinstance(key, str) or not COMMAND_KEY.match(key):
            raise ModuleError(f"{where} {key!r}: a verb is 1 to 4 words of a-z, 0-9, -, separated by single spaces")
        if isinstance(e, dict) and set(e) - set(COMMAND_FIELDS):
            raise ModuleError(f"{where} {key}: may only name: {', '.join(COMMAND_FIELDS)}")
        if not isinstance(e, dict) or not isinstance(e.get("mutates"), bool):
            raise ModuleError(f"{where} {key}: mutates must be true or false")
        for field in ("dry_run", "fleet"):
            if field in e and not isinstance(e[field], bool):
                raise ModuleError(f"{where} {key}: {field} must be true or false")
        mutates, rev = e["mutates"], e.get("reversible")
        if mutates and rev not in ("yes", "no", "n/a"):
            raise ModuleError(f"{where} {key}: reversible must be one of: yes, no, n/a")
        if not mutates and rev not in (None, "n/a"):
            raise ModuleError(f"{where} {key}: a read-only verb is reversible n/a")
        if ("undo" in e) != (rev == "yes"):
            raise ModuleError(f"{where} {key}: undo is required for reversible yes and only then")
        if "undo" in e:
            undo = e["undo"]
            if (not isinstance(undo, list) or not 1 <= len(undo) <= MAX_UNDO_WORDS
                    or not all(isinstance(w, str) and 0 < len(w) <= MAX_UNDO_WORD and not _has_unprintable(w)
                               and (_UNDO_PLACEHOLDER.match(w) if "{" in w else True) for w in undo)
                    or undo[0].startswith("-")):
                raise ModuleError(f"{where} {key}: undo is an argv list of 1 to {MAX_UNDO_WORDS} words (placeholders {{name}}), "
                                  "no control characters")
        if mutates and e.get("dry_run") is False:
            raise ModuleError(f"{where} {key}: a mutating verb takes --dry-run (4.2): dry_run must be true")
        if not mutates and e.get("dry_run"):
            raise ModuleError(f"{where} {key}: dry_run applies to a mutating verb")
        if e.get("fleet") and rev == "no":
            raise ModuleError(f"{where} {key}: an irreversible verb is never fleet-runnable")
    for key, e in cmds.items():
        if e.get("reversible") == "yes":
            verb = find_verb(cmds, e["undo"])
            if verb is None or verb != " ".join(_undo_words(e["undo"])) or not cmds[verb]["mutates"]:
                raise ModuleError(f"{where} {key}: undo names {' '.join(_undo_words(e['undo']))}, which is not a declared mutating verb")


def command_entry(e):
    """One declared verb with every default filled in (what `module commands --json` prints)."""
    out = {"mutates": e["mutates"], "reversible": e.get("reversible", "n/a") if e["mutates"] else "n/a"}
    if "undo" in e:
        out["undo"] = list(e["undo"])
    out["dry_run"] = e.get("dry_run", e["mutates"])
    out["fleet"] = e.get("fleet", False)
    return out


def commands_lines(manifest):
    """The text table of a manifest's declared verbs (read-only first), or [] for a manifest without the map."""
    cmds = manifest.get("commands")
    if cmds is None:
        return []
    cli = manifest["cli"]["name"]
    rows = sorted(((k, command_entry(e)) for k, e in cmds.items()), key=lambda r: r[1]["mutates"])   # stable: manifest order within each group
    width = max((len(k) for k, _ in rows), default=0)
    head = f"{manifest['name']} {manifest['version']} (contract {manifest['contract']}, min_core {manifest['min_core']}): "
    lines = [head + f"{len(rows)} declared verb{'' if len(rows) == 1 else 's'}"]
    for key, e in rows:
        if not e["mutates"]:
            cols = ["read-only"]
        else:
            cols = ["changes state", {"yes": "reversible", "no": "CANNOT BE UNDONE: needs --yes when not at a terminal",
                                      "n/a": "nothing to restore"}[e["reversible"]]]
            if "undo" in e:
                cols.append("undo: " + " ".join([cli, *(re.sub(r"\{(\w+)\}", r"<\1>", w) for w in e["undo"])]))
            if e["dry_run"]:
                cols.append("--dry-run")
        if e["fleet"]:
            cols.append("fleet")
        lines.append(f"  {key.ljust(width)}  " + "  ".join(cols))
    lines.append("a verb not listed here is undeclared: treat it as one that cannot be undone")
    return lines



def _validate_contract(m):
    """The contract-1 fields (3.1). A manifest without `contract` is a 2.1/3.0 manifest and may name none of them."""
    new = [k for k in CONTRACT_KEYS if k in m]
    if "contract" not in m:
        if "commands" in m:
            _check_commands(m)                      # refuses: names its own message for the missing contract
        if new:
            raise ModuleError(f"manifest names {', '.join(new)} without `contract`: declare `\"contract\": 1`")
        return
    contract = m["contract"]
    if not _is_int(contract) or contract not in CONTRACTS:
        raise ModuleError(f"manifest contract {contract!r} is not one this node knows (it knows: {', '.join(map(str, CONTRACTS))})")
    if "config" in m:
        raise ModuleError("manifest names both `contract` and `config`: under a contract, `config_schema` replaces `config`")
    if "min_core" not in m or _core_tuple(m["min_core"]) < CONTRACT_MIN_CORE[contract]:
        need = ".".join(map(str, CONTRACT_MIN_CORE[contract]))
        raise ModuleError(f"a contract {contract} manifest needs min_core {need} or newer")
    cli = m.get("cli")
    if "cli" in m:
        if (not isinstance(cli, dict) or set(cli) != {"name", "command"} or not isinstance(cli["name"], str)
                or not CLI_NAME.match(cli["name"])):
            raise ModuleError("manifest cli needs exactly a name (a-z, 0-9, -; 2 to 32 characters, starting with a letter) and a command")
        if cli["name"] in CLI_RESERVED or cli["name"].startswith("hive-"):
            raise ModuleError(f"manifest cli name {cli['name']!r} is a name the core uses")
        _check_argv("cli command", cli["command"])
    for field in ("status", "doctor"):
        if field in m:
            _check_argv(field, m[field])
    if m.get("service"):
        _check_bare_word("service command", m["service"]["command"][0])
    if "runtimes" in m:
        rt = m["runtimes"]
        if (not isinstance(rt, list) or len(rt) > MAX_RUNTIMES or len(set(map(str, rt))) != len(rt)
                or not all(isinstance(r, str) and RUNTIME_NAME.match(r) and r not in (".", "..") for r in rt)):
            raise ModuleError(f"manifest runtimes must be at most {MAX_RUNTIMES} distinct program names (letters, digits, . _ -; "
                              "no slash: a module names programs, never directories)")
    if "trust" in m and m["trust"] not in TRUST_TIERS:
        raise ModuleError(f"manifest trust must be one of: {', '.join(TRUST_TIERS)} (absent means trusted)")
    if "reads" in m:
        rd = m["reads"]
        if not isinstance(rd, list) or len(set(map(str, rd))) != len(rd) or not all(r in READ_ROUTES for r in rd):
            raise ModuleError(f"manifest reads must be distinct names from: {', '.join(READ_ROUTES)}")
    if "config_schema" in m:
        cs = m["config_schema"]
        if not isinstance(cs, dict) or len(cs) > MAX_SCHEMA_KEYS:
            raise ModuleError(f"manifest config_schema must be an object of at most {MAX_SCHEMA_KEYS} keys")
        for key, spec in cs.items():
            _check_config_entry(m["name"], key, spec)
    if "ui" in m:
        _check_ui(m)
    if "commands" in m:
        _check_commands(m)


# A deliberately small JSON Schema subset, enough for docs/ui_block.schema.json (the one definition of the `ui` block)
# without a dependency. `tests/test_manifest_contract.py` fails if that file uses a keyword this does not implement.
SCHEMA_KEYWORDS = frozenset({"$schema", "$id", "title", "description", "type", "const", "enum", "properties", "required",
                             "additionalProperties", "items", "minItems", "maxItems", "minLength", "maxLength", "pattern",
                             "minimum", "maximum", "minProperties", "maxProperties"})
_SCHEMA_TYPES = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
                 "string": lambda v: isinstance(v, str), "integer": _is_int, "boolean": lambda v: isinstance(v, bool)}
_ui_schema_cache = []


def ui_schema_path():
    return Path(__file__).resolve().parent / "docs" / UI_SCHEMA_FILE


def load_ui_schema():
    """The `ui` block's JSON Schema, shipped in the core's own tree. Fails closed if it is not there."""
    if not _ui_schema_cache:
        try:
            _ui_schema_cache.append(json.loads(ui_schema_path().read_text()))
        except (OSError, ValueError):
            raise ModuleError(f"this core is missing docs/{UI_SCHEMA_FILE}: cannot check a manifest's ui block")
    return _ui_schema_cache[0]



def _schema_check(value, schema, path):
    t = schema.get("type")
    if t is not None and not _SCHEMA_TYPES[t](value):
        raise ModuleError(f"{path} must be of type {t}")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise ModuleError(f"{path} must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ModuleError(f"{path} must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise ModuleError(f"{path} must be {schema.get('minLength', 0)} to {schema.get('maxLength', 'any')} characters")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ModuleError(f"{path} is not in the form {schema['pattern']}")
    if _is_int(value) and ("minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]):
        raise ModuleError(f"{path} is out of range")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise ModuleError(f"{path} must hold {schema.get('minItems', 0)} to {schema.get('maxItems', 'any number of')} items")
        for i, item in enumerate(value):
            if "items" in schema:
                _schema_check(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", len(value)):
            raise ModuleError(f"{path} must hold {schema.get('minProperties', 0)} to {schema.get('maxProperties', 'any number of')} fields")
        for req in schema.get("required", ()):
            if req not in value:
                raise ModuleError(f"{path} needs {req!r}")
        props, extra = schema.get("properties", {}), schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                _schema_check(v, props[k], f"{path}.{k}")
            elif extra is False:
                raise ModuleError(f"{path} names {k!r}, which this node does not know")
            elif isinstance(extra, dict):
                _schema_check(v, extra, f"{path}.{k}")


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
    _verify_files(root, m, tracked, ignore)
    return m


def _verify_files(root, m, tracked=None, ignore=()):
    """The tree at `root` holds exactly the manifest's files (plus the manifest, its signature and `ignore`), each a
    regular file under the size cap with the listed digest. Raises ModuleError."""
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


def _fetch(repo, dest):
    """Clone `repo` (a path or URL) into `dest`, and return (tracked files, commit). Uses the operator's own git
    credentials; the `ext::` transport, which runs commands, is off."""
    if not repo or repo.startswith("-"):
        raise ModuleError(f"{repo!r} is not a repository")
    env = dict(os.environ, GIT_ALLOW_PROTOCOL="file:https:ssh:git")
    try:
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--", repo, str(dest)], env=env,
                       check=True, capture_output=True, text=True, timeout=300)
        ls = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(dest), "ls-files", "-z"], check=True, capture_output=True, timeout=30)
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
    print("  " + trust_summary(manifest))
    if reads_summary(manifest):
        print("  " + reads_summary(manifest))
    for line in commands_lines(manifest):
        print("  " + line)
    if expected is not None:
        if expected != pub:
            raise ModuleError(f"--publisher does not match the manifest's publisher key ({fingerprint(pub)}): refusing")
        return
    if not sys.stdin.isatty():
        raise ModuleError("pin this publisher key by passing it: --publisher <key> (shown above)")
    if input("Trust this publisher for this module and pin it? [y/N] ").strip().lower() not in ("y", "yes"):
        raise ModuleError("publisher not confirmed: nothing installed")


# ── the owner steps ─────────────────────────────────────────────────────────────────────────────────

def _planning(lib):
    """Whether a `--plan --json` run is recording bodies instead of signing them."""
    return getattr(lib, "_PLAN", None) is not None


def _gov(lib):
    import merkle
    return lib._governance_state(merkle.read_all_entries(lib.JOURNAL_DIR))


def _require_owner(lib, acts=(), prompt=True):
    """Unlock the owner key before anything is fetched into place, so a locked key or a non-owner device
    refuses with nothing written. `acts` are the acts the command will make: with the unlock agent covering all of
    them (`owner_agent.gate_ok`) the gate passes without a prompt (#295); otherwise it unlocks, as ever. With
    `prompt=False` (`module install --non-interactive|--json`) a key that would need a prompt is `OwnerLocked`."""
    if not lib._owner_key_exists():
        raise ModuleError("this device does not hold the owner key: run module commands on the owner machine")
    if _planning(lib):
        return              # `--plan --json` signs nothing: no unlock, no prompt, no agent
    if not prompt and not lib._owner_usable_quietly(acts):
        raise OwnerLocked("the owner key is locked: run `hive-mind owner unlock`, then install again")
    if not lib._owner_gate(acts):
        raise ModuleError("the owner key could not be unlocked")


def _may_prompt(args):
    return not getattr(args, "no_owner_prompt", False)


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


# ── the non-owner install (M1b) ─────────────────────────────────────────────────────────────────────

def _pending_path(name):
    return modules_dir() / f".{name}.pending.json"


def _read_pending(name):
    try:
        rec = json.loads(_pending_path(name).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("device_id") else None


def _admit_line(rec, name):
    return f"hive-mind group admit {rec['device_id']} --module {name} --principal {rec['principal']}"


def _clear_pending(lib, name):
    """Remove everything a staged, not yet installed module left: the staging directory, the pending record and the
    minted key."""
    shutil.rmtree(modules_dir() / f".{name}.staging", ignore_errors=True)
    _pending_path(name).unlink(missing_ok=True)
    shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / name, ignore_errors=True)


def _abort_add(lib, name):
    if name in load_state(lib):
        raise ModuleError(f"module {name!r} is installed: use `hive-mind module remove {name}`")
    if _read_pending(name) is None:
        raise ModuleError(f"module {name!r} has no pending add")
    _clear_pending(lib, name)
    print(f"aborted the pending add of module {name}: its staging and key are removed")


def _stage_for_owner(lib, args, name, root):
    """First run of `module add` on a device without the owner key: verify, stage, mint, print the owner's line and
    install nothing."""
    principal = _operator_principal(lib, args.principal)
    with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
        src = Path(tmp) / "src"
        tracked, head = _fetch(args.source, src)
        manifest = check_tree(src, lib.CONTRACT_VERSION, tracked)
        if manifest["name"] != name:
            raise ModuleError(f"the repository holds module {manifest['name']!r}, not {name!r}")
        _confirm_publisher(manifest, args.publisher)
        contract_plan(name, manifest, load_state(lib))                      # a launcher collision refuses before staging
        install_config(lib, args, manifest, owner=False, enforce=False)     # values are asked for at the install run
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        staging = root / f".{name}.staging"
        minted = False
        try:
            _stage(src, manifest, staging)
            device_id, _pub = lib.mint_module_key(name)
            minted = True
            rec = {"device_id": device_id, "principal": principal, "publisher": manifest["publisher"],
                   "version": manifest["version"], "source": args.source, "commit": head,
                   "manifest_sha256": _sha256_file(staging / MANIFEST), "staged_at": int(time.time())}
            _pending_path(name).write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
        except (ModuleError, FileExistsError, OSError, ValueError, RuntimeError) as e:
            shutil.rmtree(staging, ignore_errors=True)
            _pending_path(name).unlink(missing_ok=True)
            if minted:
                shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / name, ignore_errors=True)
            raise ModuleError(str(e))
    print(f"module {name} {manifest['version']} is staged on this device and NOT installed: it has no admit yet.\n"
          f"On the owner machine, after checking commit {head} of {args.source} "
          f"(publisher {fingerprint(manifest['publisher'])}), run:\n  {_admit_line(rec, name)}\n"
          f"When that has reached this node, run `hive-mind module add {name} --from {args.source}` again to install it, "
          f"or `hive-mind module add --abort {name}` to drop it.")


def _complete_add(lib, args, name, root, rec):
    """Second run: the owner's admit has projected here, so install the staged tree. The tree is re-checked against
    the signed manifest and against the digest of the manifest this node verified at the first run."""
    if args.source != rec["source"]:
        raise ModuleError(f"module {name!r} is staged from {rec['source']}, not {args.source}: finish that add, or "
                          f"`hive-mind module add --abort {name}`")
    if args.publisher is not None and args.publisher != rec["publisher"]:
        raise ModuleError("--publisher does not match the publisher key pinned at the first run: refusing")
    gov = _gov(lib)
    dev = rec["device_id"]
    if gov["modules"].get(dev) != name or dev not in gov["admitted"] or dev in gov["purged"]:
        raise OwnerWait(f"the owner has not admitted {dev} as module {name} on this node yet (it may not have synced). "
                          f"On the owner machine: {_admit_line(rec, name)}")
    staging = root / f".{name}.staging"
    key = Path(lib._ensure_key_dir()) / "modules" / name / "device-key"
    if not key.exists():
        raise ModuleError(f"the module's key is gone: `hive-mind module add --abort {name}` and start again")
    try:
        derived = lib._device_id_for_pub(ed25519.pub_from_seed(base64.b64decode(key.read_text().strip())))
    except Exception:
        derived = None
    if derived != dev:       # the admitted id must be the one this key file really is
        raise ModuleError(f"the module's key no longer matches the device id {dev} the owner admitted: "
                          f"`hive-mind module add --abort {name}` and start again")
    manifest = check_tree(staging, lib.CONTRACT_VERSION)
    if _sha256_file(staging / MANIFEST) != rec["manifest_sha256"]:       # the bytes include the publisher key
        raise ModuleError("the staged tree is not the one this node verified: refusing. "
                          f"`hive-mind module add --abort {name}` and start again")
    if (root / name).exists():
        raise ModuleError(f"module {name!r} is already installed: use `hive-mind module update {name}`")
    state = load_state(lib)
    plan = contract_plan(name, manifest, state)
    node_vals, _fleet = install_config(lib, args, manifest, owner=False)
    renamed = False
    try:
        _write_config(staging, {**manifest.get("config", {}), **node_vals})
        os.rename(staging, root / name)
        renamed = True
        note = install_units(lib, name, manifest, plan and plan["path"])
        apply_launcher(lib, name, manifest, plan)
        state[name] = {"version": manifest["version"], "publisher": manifest["publisher"], "device_id": dev,
                       "source": rec["source"], "commit": rec["commit"], "installed_at": int(time.time()),
                       "quota": dict(manifest.get("quota") or {}), "owner_quota": {},
                       **_reads_record(manifest), **_trust_record(manifest)}
        record_plan(state[name], plan)
        save_state(lib, state)
    except BaseException:
        if plan and plan.get("launcher"):
            remove_launcher(plan["bin_dir"], plan["launcher"], name)
        try:
            remove_units(name)
        except ModuleError:                          # still draining: the add's own error is the one to show
            pass
        if renamed and not staging.exists():
            os.rename(root / name, staging)          # the pending add stays whole, so it can be run again
            (staging / "config").unlink(missing_ok=True)       # staging is again exactly the tree that was verified
        raise
    _pending_path(name).unlink(missing_ok=True)
    warn_missing(name, plan)
    print(f"installed module {name} {manifest['version']} (device {dev}, principal {gov['principals'].get(dev)}); "
          + (f"service {unit_names(name)[0]}" + (" with a timer" if manifest["service"].get("interval") else "")
             if manifest.get("service") else "no service")
          + (f" ({note})" if note else "") + "; hooks: " + (", ".join(manifest.get("hooks") or []) or "none"))
    return _healthy_rc(args, doctor_after(lib, name, "install"))


# ── the unit (M2) ───────────────────────────────────────────────────────────────────────────────────

def unit_dir():
    return Path.home() / ".config" / "systemd" / "user"


def unit_names(name):
    return f"hive-module-{name}.service", f"hive-module-{name}.timer"


def _base_timeout():
    """Seconds a quick `systemctl` call may take (30; HIVE_SYSTEMCTL_TIMEOUT lets a test shrink it)."""
    try:
        return float(os.environ.get("HIVE_SYSTEMCTL_TIMEOUT") or 30)
    except ValueError:
        return 30


def _stop_timeout(grace):
    """A call that stops a unit waits out its `stop_grace` drain, plus the usual allowance."""
    return (grace or 0) + _base_timeout()


def _unit_grace(path):
    """The TimeoutStopSec a rendered unit file declares, or 0 (the file is missing, unreadable or has none)."""
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("TimeoutStopSec="):
                return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 0


def _systemctl(*args, timeout=None, timed_out=None):
    """Run `systemctl --user ...`; False when there is no user manager or the call fails. Never raises. A call
    that outlives `timeout` fails too, and sets `timed_out[0]` when the caller passed a list to say so."""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                              timeout=timeout or _base_timeout()).returncode == 0
    except subprocess.TimeoutExpired:
        if timed_out is not None:
            timed_out[:] = [True]
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def user_systemd():
    """Whether a user systemd manager is reachable: a runtime dir, and `is-system-running` answering (a `degraded`
    manager still manages units). A session without `XDG_RUNTIME_DIR` (Tailscale SSH, #522) gets the account's own
    `/run/user/<uid>` in `os.environ`, so every `systemctl --user` after this reaches the same manager. Never raises."""
    if not hive_runtime.ensure():
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 or r.stdout.strip() in ("running", "degraded", "starting", "initializing", "stopping")


def _quote(arg):
    """One ExecStart word: systemd splits on whitespace and expands `%` and `$`, so quote and double them."""
    esc = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{esc}"'


def _env(key, value):
    """One `Environment="K=V"` line: the quotes keep a space in the value, `%` is doubled against specifiers."""
    esc = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{esc}"'


def _unit_path_arg(p):
    """A path as one systemd path word: whitespace escaped, `%` doubled against specifiers."""
    return str(p).replace("\\", "\\x5c").replace(" ", "\\x20").replace("\t", "\\x09").replace("%", "%%")


CLIENT_FILES = ("hive_module_client.py", "ed25519.py", "merkle.py", "sync_common.py")


def _confined_lines(module_dir, key_dir, path, name=None):
    """The sandbox of a `confined` unit: the filesystem read-only, the home directory replaced by an empty tmpfs, then
    only what the module needs put back. Its own directory and the PATH entries under it are read-only (it must
    still run); its key directory, the module's own state, is the one thing it may write.

    With a `name` (a unit that has a service) it also gets its own runtime directory, `RuntimeDirectory=` (mode 0700,
    exported as `HIVE_MODULE_RUNTIME_DIR`) for its sockets, and its own persistent state directory, `StateDirectory=` (mode
    0700, `~/.local/state/hive-mind-mod-<name>`, exported as `HIVE_MODULE_STATE_DIR`; kept across restarts and updates,
    #529): never the shared `%t/hive-mind`, which holds the owner-unlock
    agent's socket. And the reference client's files, read-only and one by one, so it can `import hive_module_client`;
    not the checkout, `.peers.json` or the key store."""
    keep = [module_dir, *(d for d in (path or "").split(":") if d.startswith("/") and d not in SYSTEM_PATH)]
    root = Path(__file__).resolve().parent
    lines = ["ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "CapabilityBoundingSet=",
             "BindPaths=" + _unit_path_arg(key_dir),
             *("BindReadOnlyPaths=-" + _unit_path_arg(d) for d in dict.fromkeys(map(str, keep)))]
    if name:
        lines += [f"RuntimeDirectory=hive-mind-mod-{name}", "RuntimeDirectoryMode=0700",
                  f'Environment="HIVE_MODULE_RUNTIME_DIR=%t/hive-mind-mod-{name}"',
                  f"StateDirectory=hive-mind-mod-{name}", "StateDirectoryMode=0700",
                  f'Environment="HIVE_MODULE_STATE_DIR=%S/hive-mind-mod-{name}"',
                  *("BindReadOnlyPaths=" + _unit_path_arg(root / f) for f in CLIENT_FILES)]
    return lines


def render_units(name, manifest, module_dir, key_dir, path=None, trust=None):
    """{filename: text} for a module's unit(s), or {} when its manifest has no `service` block.

    It runs as the operator's user (a user unit), in the module's own directory, with the module's own key
    directory and nothing of the owner's in its environment. The command's first word is made absolute: a path
    inside the module (one it ships, or any relative path) is anchored to its directory and any other bare name is
    looked up by `env`. Unlike the sync unit it
    has no `ExecStartPre` that kills by command line (such a pattern could match another module).

    `path` (a contract-1 module's recorded PATH, see `unit_path`) becomes the unit's `PATH`; without it the unit has
    none, as in 2.1.

    `trust` is the effective tier (`manifest_trust`); a `trusted` unit renders as it always did, a `confined` one
    gets `_confined_lines`. Without it the manifest's own tier is used."""
    svc = manifest.get("service")
    if not svc:
        return {}
    cmd = list(svc["command"])
    if not cmd[0].startswith("/"):
        own = "/" in cmd[0] or cmd[0] in manifest.get("files", {})
        cmd = [str(module_dir / cmd[0]), *cmd[1:]] if own else ["/usr/bin/env", *cmd]
    interval, restart = svc.get("interval"), svc.get("restart", "on-failure")
    service, timer = unit_names(name)
    lines = ["[Unit]", f"Description=Hive Mind module {name}", "After=network.target hive-sync.service", ""]
    lines += ["[Service]", f"Type={'oneshot' if interval else 'simple'}", f"WorkingDirectory={str(module_dir).replace('%', '%%')}",
              "ExecStart=" + " ".join(_quote(a) for a in cmd),
              _env("HIVE_MODULE_NAME", name), _env("HIVE_MODULE_DIR", module_dir),
              _env("HIVE_MODULE_KEY_DIR", key_dir), *([_env("PATH", path)] if path else []), "NoNewPrivileges=yes",
              "StandardOutput=journal", "StandardError=journal"]
    if (trust or signed_trust(manifest)) == "confined":
        lines += _confined_lines(module_dir, key_dir, path, name)
    grace = svc.get("stop_grace")
    if grace:                           # SIGTERM the main process only, let it drain, SIGKILL the group after `grace`
        lines += [f"TimeoutStopSec={grace}", "KillMode=mixed"]
    if not interval:                    # a timer re-fires a oneshot; a daemon is kept up by its restart policy
        lines += [f"Restart={restart}", "RestartSec=5"] + ([] if grace else ["KillMode=control-group"])
        lines += ["", "[Install]", "WantedBy=default.target"]
    out = {service: "\n".join(lines) + "\n"}
    if interval:
        out[timer] = "\n".join(["[Unit]", f"Description=Run Hive Mind module {name} every {interval}s", "",
                                 "[Timer]", f"OnBootSec={interval}", f"OnUnitActiveSec={interval}",
                                 "", "[Install]", "WantedBy=timers.target"]) + "\n"
    return out


def _write_units(lib, name, manifest, only_changed=False, path=None):
    """Stop whatever is installed, then write the module's unit files (and drop a timer it no longer declares).
    Returns the unit to enable and start (the timer when there is one), or None when there is nothing to start:
    no service block, or `only_changed` and the files are already what the manifest renders."""
    keep = render_units(name, manifest, modules_dir() / name, Path(lib._ensure_key_dir()) / "modules" / name, path,
                        manifest_trust(manifest, load_state(lib).get(name)))
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
            late = [False]
            if not _systemctl("disable", "--now", fname, timeout=_stop_timeout(_unit_grace(d / fname)), timed_out=late) and late[0]:
                raise ModuleError(f"{fname} did not stop in time: it is still draining. Wait for it, then run this again")
            if fname not in keep:
                (d / fname).unlink()
    for fname, text in keep.items():
        (d / fname).write_text(text)
    return next(f for f in unit_names(name)[::-1] if f in keep)


def _start_unit(unit):
    """Enable and (re)start `unit`; a note for the operator when systemd would not."""
    if not _systemctl("enable", unit):
        return "the unit is written, but systemd would not enable it"
    late = [False]
    if not _systemctl("restart", unit, timeout=_stop_timeout(_unit_grace(unit_dir() / unit)), timed_out=late):
        if late[0]:
            return f"the unit is enabled, but systemd had not finished restarting it in time (is it still draining?): check `systemctl --user status {unit}`"
        return "the unit is enabled, but systemd would not start it"
    return ""


def install_units(lib, name, manifest, path=None):
    """Write the module's unit files and, where a user systemd manager is reachable, enable and (re)start them.
    Without one the files are still written; returns a note for the operator."""
    unit = _write_units(lib, name, manifest, path=path)
    if unit is None:
        return ""
    if not _systemctl("show-environment"):
        return "no user systemd manager is reachable: the unit is written, not started"
    _systemctl("daemon-reload")
    return _start_unit(unit)


def remove_units(name):
    """Stop, disable and delete the module's units. Safe when there are none. Waits out a `stop_grace` drain; a
    unit still draining after that raises ModuleError with its unit file kept, so nothing of the module is
    deleted under it."""
    service, timer = unit_names(name)
    d = unit_dir()
    if not any((d / f).exists() for f in (service, timer)):
        return
    for f in (timer, service):
        late = [False]
        if not _systemctl("disable", "--now", f, timeout=_stop_timeout(_unit_grace(d / f)), timed_out=late) and late[0]:
            raise ModuleError(f"{f} did not stop in time: it is still draining, so nothing was deleted. Wait for it, then run this again")
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


# ── runtimes, the unit PATH and the CLI launcher (3.1, #294 PR 3) ───────────────────────────────────
# A contract-1 module names the programs it needs (`runtimes`), never directories. The core finds each in a directory
# only the operator (or root) can write, records those directories in `.modules.json`, and gives the unit and the
# launcher one PATH built from them. A manifest without `contract` gets none of this: no PATH in its unit, no launcher.

RUNTIME_SEARCH = ("~/.local/bin", "~/.npm-global/bin", "~/.cargo/bin", "~/.bun/bin", "/opt/homebrew/bin", "/usr/local/bin")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
LAUNCHER_MARK = "# hive-mind module launcher v1"
SCRUBBED_ENV = ("HIVE_OWNER_PASSPHRASE", "HIVE_OWNER_KEY_PASSPHRASE", "HIVE_OWNER_SEED", "HIVE_KEY_DIR")
_LAUNCHER_HEAD = re.compile(re.escape(LAUNCHER_MARK) + r" module=([a-z0-9_-]+) sha256=([0-9a-f]{64})\Z")


def safe_dir(d):
    """Whether `d` may sit on a service PATH: absolute, no `:` or control character in it, a directory, owned by the
    operator or root, and writable by neither group nor world (a writable directory on a PATH is a hijack)."""
    d = str(d)
    if not d.startswith("/") or ":" in d or _has_control(d):
        return False
    try:
        st = os.stat(d)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid in (os.getuid(), 0) and not st.st_mode & 0o022


def _program_in(d, program):
    p = Path(d) / program
    return p.is_file() and os.access(p, os.X_OK)


def resolve_runtimes(names, overrides=None):
    """({program: directory}, [programs not found]). Each program is looked for in the invoking PATH, then the fixed
    operator directories, then the operator's per-module override; only a `safe_dir` counts."""
    overrides = overrides or {}
    search = [d for d in os.environ.get("PATH", "").split(":") if d] + [os.path.expanduser(d) for d in RUNTIME_SEARCH]
    found, missing = {}, []
    for program in names:
        order = search + ([overrides[program]] if program in overrides else [])
        hit = next((d for d in order if safe_dir(d) and _program_in(d, program)), None)
        if hit:
            found[program] = hit
        else:
            missing.append(program)
    return found, missing


def bin_dir():
    """Where `hive-mind` and `hv` live, so a launcher sits beside them: $HIVE_BIN_DIR, else the directory `hive-mind`
    is found in, else ~/.local/bin."""
    env = os.environ.get("HIVE_BIN_DIR")
    if env:
        return Path(env)
    found = shutil.which("hive-mind")
    return Path(found).parent if found and found.startswith("/") else Path.home() / ".local" / "bin"


def _bin_ok(bdir):
    """A bin directory a launcher may go in: a `safe_dir`, or one that does not exist yet under a `safe_dir`."""
    return safe_dir(bdir) or (not os.path.lexists(bdir) and safe_dir(Path(bdir).parent))


def unit_path(runtime_dirs, bdir):
    """The PATH a contract-1 module's unit, launcher and `module run` get: the directories its runtimes resolved to,
    the bin directory, then the system's."""
    out = []
    for d in [*(runtime_dirs or []), bdir, *SYSTEM_PATH]:
        if d and d not in out:
            out.append(d)
    return ":".join(out)


def recorded_path(rec):
    """The PATH recorded for an installed module, or None for one installed without a contract."""
    if "runtime_dirs" not in rec and "bin_dir" not in rec:
        return None
    return unit_path(rec.get("runtime_dirs"), rec.get("bin_dir"))


def module_argv(manifest, cmd, module_dir):
    """The argv to run for one of a manifest's commands: a path inside the module is anchored to its directory, an
    absolute one stays, and a bare name is looked up on PATH by `env` (as the unit does)."""
    first = cmd[0]
    if first.startswith("/"):
        return list(cmd)
    if "/" in first or first in manifest.get("files", {}):
        return [str(module_dir / first), *cmd[1:]]
    return ["/usr/bin/env", *cmd]


def module_env(lib, name, path):
    """The environment of a module process started by the operator: theirs, without the owner's secrets, plus the
    module's own name, directory, key directory and PATH."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    hive_runtime.ensure(env)                                    # a module finds its runtime dir as in a login (#522)
    env.update(HIVE_MODULE_NAME=name, HIVE_MODULE_DIR=str(modules_dir() / name),
               HIVE_MODULE_KEY_DIR=str(Path(lib._ensure_key_dir()) / "modules" / name))
    if path:
        env["PATH"] = path
    return env


def launcher_text(lib, name, manifest, path):
    """The launcher file the core renders: the module supplies a name and an argv, never shell text. Every word is
    single-quoted; the environment is the module's own, and the owner's secrets are unset."""
    body = "\n".join([
        "unset " + " ".join(SCRUBBED_ENV),
        *hive_runtime.launcher_lines(),                         # no XDG_RUNTIME_DIR (Tailscale SSH): the login's (#522)
        f"HIVE_MODULE_NAME={shlex.quote(name)}",
        f"HIVE_MODULE_DIR={shlex.quote(str(modules_dir() / name))}",
        f"HIVE_MODULE_KEY_DIR={shlex.quote(str(Path(lib._ensure_key_dir()) / 'modules' / name))}",
        f"PATH={shlex.quote(path)}",
        "export HIVE_MODULE_NAME HIVE_MODULE_DIR HIVE_MODULE_KEY_DIR PATH",
        "exec " + " ".join(shlex.quote(a) for a in module_argv(manifest, manifest["cli"]["command"], modules_dir() / name)) + ' "$@"',
        ""])
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f"#!/bin/sh\n{LAUNCHER_MARK} module={name} sha256={digest}\n{body}"


def _own_launcher(path, name):
    """Whether `path` is a launcher this core wrote for module `name`, unedited: a regular file, the marker line
    naming the module, and the digest of the rest matching."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode) or os.lstat(path).st_size > 64 * 1024:
            return False
        lines = Path(path).read_text().split("\n", 2)
    except (OSError, UnicodeDecodeError):
        return False
    m = _LAUNCHER_HEAD.match(lines[1]) if len(lines) == 3 and lines[0] == "#!/bin/sh" else None
    return bool(m and m.group(1) == name and m.group(2) == hashlib.sha256(lines[2].encode()).hexdigest())


def launcher_target(path):
    """(module name, the HIVE_MODULE_DIR it runs) of a launcher this core wrote, unedited; None for any other file."""
    try:
        lines = Path(path).read_text().split("\n", 2) if stat.S_ISREG(os.lstat(path).st_mode) else []
    except (OSError, UnicodeDecodeError):
        return None
    m = _LAUNCHER_HEAD.match(lines[1]) if len(lines) == 3 and lines[0] == "#!/bin/sh" else None
    if not (m and _own_launcher(path, m.group(1))):
        return None
    for line in lines[2].splitlines():
        if line.startswith("HIVE_MODULE_DIR="):
            try:
                words = shlex.split(line[len("HIVE_MODULE_DIR="):])
            except ValueError:
                return None
            return (m.group(1), words[0]) if len(words) == 1 else None
    return None


def check_launcher_free(bdir, name, cli_name, state):
    """Refuse, before anything is written, a launcher name another module claims, a file that is not this module's
    own launcher, or a bin directory others can write to."""
    for other, rec in state.items():
        if other != name and rec.get("launcher") == cli_name:
            raise ModuleError(f"cli name {cli_name!r} is already taken by module {other!r}")
    if not _bin_ok(bdir):
        raise ModuleError(f"{bdir} cannot hold a launcher: it must be an absolute directory only you (or root) can write")
    target = Path(bdir) / cli_name
    if os.path.lexists(target) and not _own_launcher(target, name):
        raise ModuleError(f"{target} exists and is not this module's launcher: move it, or pick another cli name")


def write_launcher(lib, name, manifest, path, bdir):
    """Write (or refresh) the module's launcher. Nothing is touched when it already holds the rendered text."""
    target = Path(bdir) / manifest["cli"]["name"]
    text = launcher_text(lib, name, manifest, path)
    try:
        if target.read_text() == text and not target.is_symlink() and os.access(target, os.X_OK):
            return
    except (OSError, UnicodeDecodeError):
        pass
    fd, tmp = tempfile.mkstemp(dir=bdir, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def remove_launcher(bdir, cli_name, name):
    """Delete the launcher if it is still this module's own; anything else at that name is left alone."""
    if bdir and cli_name and _own_launcher(Path(bdir) / cli_name, name):
        try:
            (Path(bdir) / cli_name).unlink()
        except OSError:
            pass


def contract_plan(name, manifest, state, overrides=None):
    """What a contract-1 manifest adds to an install, or None for any other manifest: the resolved runtime
    directories, the bin directory and the launcher name, plus the runtimes not found. Refuses a launcher
    collision, so call it before anything is written."""
    if "contract" not in manifest:
        return None
    found, missing = resolve_runtimes(manifest.get("runtimes", []), overrides)
    bdir = bin_dir()
    plan = {"runtime_dirs": sorted(set(found.values())), "bin_dir": str(bdir) if _bin_ok(bdir) else "", "missing": missing}
    if manifest.get("cli"):
        check_launcher_free(bdir, name, manifest["cli"]["name"], state)
        plan["launcher"] = manifest["cli"]["name"]
    plan["path"] = unit_path(plan["runtime_dirs"], plan["bin_dir"])
    return plan


def apply_launcher(lib, name, manifest, plan):
    if plan and plan.get("launcher"):
        Path(plan["bin_dir"]).mkdir(mode=0o755, exist_ok=True)
        write_launcher(lib, name, manifest, plan["path"], plan["bin_dir"])


def record_plan(rec, plan):
    """Put a plan's durable part into a module's `.modules.json` row (a no-op for a manifest without `contract`)."""
    if plan is None:
        for k in ("runtime_dirs", "bin_dir", "launcher", "runtime_overrides"):
            rec.pop(k, None)
        return
    rec.update(runtime_dirs=plan["runtime_dirs"], bin_dir=plan["bin_dir"])
    if plan.get("launcher"):
        rec["launcher"] = plan["launcher"]
    else:
        rec.pop("launcher", None)


def warn_missing(name, plan):
    for program in (plan or {}).get("missing", []):
        print(f"hive-mind module: warning: {name}: runtime {program!r} not found; install it, or run "
              f"`hive-mind module runtime {name} {program} <dir>`", file=sys.stderr)


def _restore_launcher(lib, name, rec, old_manifest):
    """After a failed update: put the previous launcher back and drop a new one under another name."""
    try:
        if rec.get("launcher") and old_manifest.get("cli"):
            write_launcher(lib, name, old_manifest, recorded_path(rec), rec["bin_dir"])
    except (OSError, KeyError, ValueError):
        pass


def refuse_disabled(name, rec):
    if is_disabled(rec):
        raise ModuleError(f"module {name!r} is disabled: {rec['disabled'].get('reason')}. "
                          f"Fix it, then run: hive-mind module update {name}")


def cmd_run(lib, args):
    """`module run <name> [--] args…`: the launcher's job without the launcher."""
    rec = load_state(lib).get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    refuse_disabled(args.name, rec)
    manifest = installed_manifest(lib, args.name)
    if not manifest.get("cli"):
        raise ModuleError(f"module {args.name!r} declares no cli")
    argv = module_argv(manifest, manifest["cli"]["command"], modules_dir() / args.name) + list(args.args)
    try:
        rc = subprocess.run(argv, env=module_env(lib, args.name, recorded_path(rec))).returncode
    except OSError as e:
        raise ModuleError(f"could not run {argv[0]}: {e}")
    return rc if rc >= 0 else 128 - rc


def cmd_commands(lib, args):
    """`module commands <name> [--json]`: the verbs the module's signed manifest declares. The tree is verified
    (signature, digests) and must be signed by the pinned publisher first: callers act on this listing."""
    rec = load_state(lib).get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    refuse_disabled(args.name, rec)
    manifest = check_tree(modules_dir() / args.name, lib.CONTRACT_VERSION, ignore=("config",))
    if manifest["name"] != args.name or manifest["publisher"] != rec.get("publisher"):
        raise ModuleError(f"module {args.name!r}: the installed manifest is not the one signed by the pinned publisher: refusing")
    cmds = manifest.get("commands")
    cli = (manifest.get("cli") or {}).get("name")
    if args.json:
        print(json.dumps({"schema": 1, "module": manifest["name"], "version": manifest["version"], "cli": cli,
                          "declared": cmds is not None,
                          "commands": {k: command_entry(e) for k, e in (cmds or {}).items()}}, indent=1))
    elif cmds is None:
        print(f"{manifest['name']} {manifest['version']}: declares no commands (a 4.1 manifest); treat every verb as one that cannot be undone")
    else:
        print("\n".join(commands_lines(manifest)))
    return 0


DOCTOR_PROBE_TIMEOUT, DOCTOR_PROBE_MAX = 5, 8


def probe_help(argv, env, cwd, timeout=DOCTOR_PROBE_TIMEOUT):
    """Run `argv` (a verb plus `--help`) without a shell or stdin and with its output discarded; (returncode or None,
    why or ""). The group is killed at the deadline."""
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return None, f"could not run: {e.strerror or e}"
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.wait()
        return None, f"did not finish in {timeout} s"
    return proc.returncode, ""


def undo_verbs(manifest):
    """The distinct undo verbs the manifest's `reversible: yes` verbs name, in manifest order."""
    cmds, out = manifest.get("commands") or {}, []
    for e in cmds.values():
        verb = find_verb(cmds, e["undo"]) if e.get("reversible") == "yes" else None
        if verb and verb not in out:
            out.append(verb)
    return out


def unit_runtime_dir(name, manifest, rec):
    """The `HIVE_MODULE_RUNTIME_DIR` the module's unit gets (`%t/hive-mind-mod-<name>`, `_confined_lines`), resolved
    from the same runtime directory; None for a module without a `service` or not confined."""
    if not (manifest or {}).get("service") or manifest_trust(manifest, rec) != "confined":
        return None
    runtime = hive_runtime.runtime_dir()
    return f"{runtime}/hive-mind-mod-{name}" if runtime else None


def unit_state_dir(name, manifest, rec):
    """The `HIVE_MODULE_STATE_DIR` the module's unit gets (`%S/hive-mind-mod-<name>`, `_confined_lines`; a user unit's
    `%S` is `$XDG_STATE_HOME`, else `~/.local/state`); None for a module without a `service` or not confined."""
    if not (manifest or {}).get("service") or manifest_trust(manifest, rec) != "confined":
        return None
    return str(state_dir_path(name))


def state_dir_path(name):
    base = os.environ.get("XDG_STATE_HOME", "")
    base = Path(base) if base.startswith("/") else Path(os.environ.get("HOME", str(Path.home()))) / ".local" / "state"
    return base / f"hive-mind-mod-{name}"


def purge_state_dir(name):
    d = state_dir_path(name)
    if d.is_symlink():
        d.unlink()
    else:
        shutil.rmtree(d, ignore_errors=True)


def narrow_env(name, rec, key_dir, manifest=None, **extra):
    """The environment `status` and `doctor` run in: built from nothing, so no owner secret, identity stash or other
    `HIVE_*` variable of the caller's reaches module code. The module's PATH, its name, directory and key directory,
    and the runtime directory (#522: a module's status and doctor look for its socket there). Given the `manifest` of a
    confined module with a `service`, also its unit's `HIVE_MODULE_RUNTIME_DIR` (#516), so its CLI finds the same socket, and its `HIVE_MODULE_STATE_DIR` (#529)."""
    env = {"PATH": recorded_path(rec) or unit_path([], ""), "HOME": os.environ.get("HOME", str(Path.home())), "LANG": "C.UTF-8",
           "HIVE_MODULE_NAME": name, "HIVE_MODULE_DIR": str(modules_dir() / name), "HIVE_MODULE_KEY_DIR": str(key_dir)}
    runtime = hive_runtime.runtime_dir()
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    unit_dir = unit_runtime_dir(name, manifest, rec)
    if unit_dir:
        env["HIVE_MODULE_RUNTIME_DIR"] = unit_dir
    state = unit_state_dir(name, manifest, rec)
    if state:
        env["HIVE_MODULE_STATE_DIR"] = state
    return {**env, **extra}


def module_status(lib, name, rec, manifest):
    """The module's `status` envelope, or an `unknown` one saying why not. Runs the manifest's argv without a shell,
    in the module's directory, with a narrow environment, 20 s and 64 KiB at most."""
    mod_dir = modules_dir() / name
    env = narrow_env(name, rec, Path(lib._ensure_key_dir()) / "modules" / name, manifest)
    rc, obj, why = _run_json(manifest["status"], manifest["files"], mod_dir, env)
    problems = [why] if why else ([] if rc == 0 else [f"status exited {rc}, not 0"]) + status_problems(obj)
    if problems:
        return {"schema": 1, "state": "unknown", "version": "", "problems": problems, "units": [], "detail": {}}
    return obj


def cmd_status(lib, args):
    """`module status <name> [--json]`: proxies the module's `status`. Exit 0 when it says `ok`, else 1."""
    rec = load_state(lib).get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    refuse_disabled(args.name, rec)
    manifest = installed_manifest(lib, args.name)
    if not manifest.get("status"):
        raise ModuleError(f"module {args.name!r} declares no status command")
    env = module_status(lib, args.name, rec, manifest)
    if args.json:
        print(json.dumps(env, indent=1, sort_keys=True))
    else:
        print(f"{args.name}: {env['state']}" + (f" ({env['version']})" if env["version"] else ""))
        for u in env.get("units", []):
            print(f"  unit {u['name']}: {u['state']}")
        for p in env["problems"]:
            print(f"  ! {p}")
    return 0 if env["state"] == "ok" else 1


def cmd_runtime(lib, args):
    """`module runtime <name> <program> <dir>`: the operator tells the core where a runtime lives when the core cannot
    see it (a dashboard-spawned install has the unit's narrow PATH, not the login PATH). `--unset` forgets it."""
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    manifest = installed_manifest(lib, args.name)
    if args.program not in manifest.get("runtimes", []):
        raise ModuleError(f"module {args.name!r} does not declare the runtime {args.program!r}")
    mine = dict(rec.get("runtime_overrides") or {})
    if args.unset:
        mine.pop(args.program, None)
    else:
        if not args.dir:
            raise ModuleError("module runtime needs a directory (or --unset)")
        d = os.path.abspath(os.path.expanduser(args.dir))
        if not safe_dir(d):
            raise ModuleError(f"{d} cannot go on a service PATH: it must be an absolute directory only you (or root) can write")
        if not _program_in(d, args.program):
            raise ModuleError(f"{args.program!r} is not an executable file in {d}")
        mine[args.program] = d
    if mine:
        rec["runtime_overrides"] = mine
    else:
        rec.pop("runtime_overrides", None)
    save_state(lib, state)
    _reapply_names(lib, [args.name])
    now = load_state(lib).get(args.name, {})
    print(f"{args.name}: PATH is now {recorded_path(now)}")


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
    _save_config(path, {**defaults, **have})                 # a 0600 temp file renamed in: never readable by others


def _config_path(lib, name):
    if name not in load_state(lib):
        raise ModuleError(f"module {name!r} is not installed")
    return modules_dir() / name / "config"


def _read_config(path):
    try:
        have = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ModuleError(f"{path} is not readable JSON ({e}): fix or remove it first")
    if not isinstance(have, dict):
        raise ModuleError(f"{path} is not a JSON object: fix or remove it first")
    return have


def _save_config(path, have):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(have, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── per-module configuration (3.1, #294 PR 2) ───────────────────────────────────────────────────────
# A module with a `config_schema` has typed, scoped keys. Values live in three layers: this node's own file
# (`node`: local, 0600, never journaled, no owner key), the owner-signed `x-<module>:<key>` (`fleet`) and the same
# under `x-<module>:node.<16hex>.<key>` (`fleet-node`, one device). A key is read from the one layer its scope names,
# else its schema default. A module without a schema keeps the 2.1/3.0 behaviour: free-form keys in the node file.

MASK = "********"

_SECRET_KEY_RE = re.compile(r"token|secret|passw|passphrase|credential|api[._-]?key|private|(^|[._-])key$", re.I)


def _is_secret_key(key):
    """A config key that holds a secret, by name: its value never travels on argv or into output."""
    return bool(_SECRET_KEY_RE.search(key or ""))


def _read_secret_file(path):
    try:
        value = Path(path).expanduser().read_text().rstrip("\n")
    except (OSError, ValueError) as e:
        raise ModuleError(f"could not read {path}: {e}")
    if len(value) > vocabulary.MODULE_VALUE_MAX:
        raise ModuleError(f"the value is longer than {vocabulary.MODULE_VALUE_MAX} characters")
    return value


_DEVICE_TAG = re.compile(r"(?:k1:)?([0-9a-f]{16})\Z")
_SEGMENT = re.compile(r"[a-z0-9_-]+\Z")
_NODE_KEY = re.compile(r"node\.([0-9a-f]{16})\.(.+)\Z")
SCOPE_FLAG = {"node": "(no flag)", "fleet": "--fleet", "fleet-node": "--fleet-node <device>"}
PROMPT_TRIES = 3


def installed_manifest(lib, name):
    try:
        return validate_manifest((modules_dir() / name / MANIFEST).read_bytes(), lib.CONTRACT_VERSION)
    except (OSError, ModuleError) as e:
        raise ModuleError(f"{name}'s installed manifest is not usable ({e}): `hive-mind module update {name}`")


def secret_config_key(contract_version, full_key):
    """Whether the module key `x-<module>:<name>` holds a secret: its name looks like one, or the installed module's
    config_schema marks it `secret`. A plan prints the value it would sign, so a secret key cannot be planned."""
    sp = vocabulary.split_module_name(full_key)
    if sp is None:
        return False
    name, local = sp
    if _is_secret_key(local):
        return True
    try:
        schema = validate_manifest((modules_dir() / name / MANIFEST).read_bytes(), contract_version).get("config_schema")
    except (OSError, ModuleError):
        return False
    ent = schema_entry(schema, local) if isinstance(schema, dict) else None
    return bool(ent and ent[1].get("secret"))


def schema_entry(schema, key):
    """(schema key, entry) for a concrete config key, or None: an exact name first, then a `{word}` template whose
    segments each match one `[a-z0-9_-]+` segment."""
    if key in schema:
        return key, schema[key]
    parts = key.split(".")
    for pattern in sorted(schema):
        want = pattern.split(".")
        if len(want) == len(parts) and all(a == b or (_SEGMENT_WORD.match(a) and _SEGMENT.match(b))
                                           for a, b in zip(want, parts)):
            return pattern, schema[pattern]
    return None


def _node_tag(device_id):
    m = _DEVICE_TAG.match(device_id or "")
    return m.group(1) if m else None


def _device_tag(text):
    tag = _node_tag(text)
    if tag is None:
        raise ModuleError(f"{text!r} is not a device id (k1:<16 hex>, or the 16 hex digits)")
    return tag


def fleet_layers(gov, name, node_id):
    """(fleet, fleet-node) values for this node: the module's journaled `x-<name>:` keys, split by the
    `node.<16hex>.` convention (a naming convention over `set-config`; nothing in the journal differs)."""
    prefix, tag = f"x-{name}:", _node_tag(node_id)
    fleet, mine = {}, {}
    for full, value in sorted((gov.get("module_config") or {}).items()):
        if not full.startswith(prefix):
            continue
        local = full[len(prefix):]
        m = _NODE_KEY.match(local)
        if m:
            if m.group(1) == tag:
                mine[m.group(2)] = value
        else:
            fleet[local] = value
    return fleet, mine


def merged_config(schema, node, fleet, mine):
    """One row per key: {key, value, source, secret, unknown}. A key with a schema entry is read only from the layer
    its scope names, then its default; with no schema (a 2.1 module) every layer counts, node first."""
    layers = {"node": node, "fleet-node": mine, "fleet": fleet}
    keys = {k for k in schema or () if "{" not in k} | set(node) | set(fleet) | set(mine)
    rows = []
    for key in sorted(keys):
        ent = schema_entry(schema, key) if schema is not None else None
        spec = ent[1] if ent else None
        allowed = (spec.get("scope", "node"),) if spec else tuple(layers)
        value = source = None
        for layer in layers:
            if layer in allowed and key in layers[layer]:
                value, source = layers[layer][key], layer
                break
        else:
            if spec and "default" in spec:
                value, source = spec["default"], "default"
        rows.append({"key": key, "value": value, "source": source, "secret": bool(spec and spec.get("secret")),
                     "unknown": schema is not None and spec is None})
    return rows


def config_problems(schema, node, fleet, mine):
    """(errors, warnings) for the values a module holds against its schema. A message names a key and a rule, never a
    value, so a secret cannot leak through it."""
    errors, warnings = [], []
    for layer, label, values in (("node", "node config", node), ("fleet-node", "fleet-node config", mine),
                                 ("fleet", "fleet config", fleet)):
        for key in sorted(values):
            ent = schema_entry(schema, key)
            if ent is None:
                warnings.append(f"{label} {key}: not in the schema (kept, unused)")
                continue
            scope = ent[1].get("scope", "node")
            problem = config_value_problem(ent[1], values[key])
            if scope != layer:
                warnings.append(f"{label} {key}: ignored, its scope is {scope}")
            elif problem:
                errors.append(f"{label} {key}: {problem}")
    value = {r["key"]: r["value"] for r in merged_config(schema, node, fleet, mine)}
    for key, spec in sorted(schema.items()):
        if spec.get("required_at_install") and "{" not in key and spec.get("scope", "node") != "fleet-node" \
                and value.get(key) is None:
            errors.append(f"{key}: required and has no value (scope {spec.get('scope', 'node')})")
    return errors, warnings


def _shown(row):
    if row["value"] is None:
        return None
    return MASK if row["secret"] or row["unknown"] else row["value"]       # a key the schema dropped may have been a secret


def _print_rows(rows, as_json):
    if as_json:
        print(json.dumps([{"key": r["key"], "value": _shown(r), "source": r["source"], "secret": r["secret"],
                           "set": r["value"] is not None, **({"unknown": True} if r["unknown"] else {})}
                          for r in rows], indent=1))
        return
    for r in rows:
        note = ", ".join(x for x in (r["source"] or "unset", "secret" if r["secret"] else "",
                                     "not in schema" if r["unknown"] else "") if x)
        print(f"{r['key']}={_shown(r)}  ({note})" if r["value"] is not None else f"{r['key']}  ({note})")


def _read_secret_line():
    line = sys.stdin.readline(vocabulary.MODULE_VALUE_MAX + 2)
    if len(line.rstrip("\n")) > vocabulary.MODULE_VALUE_MAX:
        raise ModuleError(f"the value is longer than {vocabulary.MODULE_VALUE_MAX} characters")
    return line.rstrip("\n")


def _prompt(key, spec):
    label = f"{key}: {spec['description']}"
    if spec["type"] == "enum":
        label += f" [{'/'.join(spec['values'])}]"
    for _ in range(PROMPT_TRIES):
        raw = getpass.getpass(f"{label} (hidden): ") if spec.get("secret") else input(f"{label}: ")
        problem = "a value is required" if raw == "" else config_value_problem(spec, raw)
        if problem is None:
            return raw
        print(f"hive-mind module: {key}: {problem}", file=sys.stderr)
    raise ModuleError(f"no valid value for {key}")


def _pairs(args, field):
    out = {}
    for item in getattr(args, field, None) or []:
        key, eq, value = item.partition("=")
        if not eq or not key:
            raise ModuleError(f"--{field.replace('_', '-')} wants key=value, not {item!r}")
        if key in out:
            raise ModuleError(f"--{field.replace('_', '-')} names {key} twice")
        out[key] = value
    return out


def install_config(lib, args, manifest, owner, enforce=True, asked=None, have=None):
    """Settle a module's install-time values from `--set`, `--set-fleet`, `--set-env` and, on a terminal, prompts, before
    anything is staged, minted or signed. Returns (node values, fleet values). `enforce=False` only checks what was
    given (the first, staging run on a device without the owner key); `asked` limits the required keys to ask about
    (an update asks only for keys the new schema added); `have` is this node's current file."""
    schema = manifest.get("config_schema")
    sets, fleet_sets, env_sets = _pairs(args, "set"), _pairs(args, "set_fleet"), _pairs(args, "set_env")
    if schema is None:
        if sets or fleet_sets or env_sets:
            raise ModuleError("this module declares no config_schema: --set, --set-fleet and --set-env do not apply "
                              "(use `hive-mind module config` after the install)")
        return {}, {}
    if set(sets) & set(env_sets):
        raise ModuleError("a key is named by both --set and --set-env")
    for key, var in env_sets.items():
        if var not in os.environ:
            raise ModuleError(f"--set-env {key}={var}: {var} is not set in the environment")
        sets[key] = os.environ[var]
    node_vals, fleet_vals = {}, {}
    for given, want, flag, into in ((sets, "node", "--set", node_vals), (fleet_sets, "fleet", "--set-fleet", fleet_vals)):
        for key, value in given.items():
            ent = schema_entry(schema, key) if vocabulary.split_module_name(f"x-{manifest['name']}:{key}") else None
            if ent is None:
                raise ModuleError(f"{flag} {key}: not a key in this module's config_schema")
            spec = ent[1]
            if spec.get("scope", "node") != want:
                raise ModuleError(f"{key} has scope {spec.get('scope', 'node')}: install sets it with "
                                  + {"node": "--set", "fleet": "--set-fleet",
                                     "fleet-node": "`hive-mind module config --fleet-node` after the install"}[spec.get("scope", "node")])
            if spec.get("secret") and key not in env_sets:
                raise ModuleError(f"{key} is a secret: pass it with --set-env {key}=<VARIABLE>, or leave it out to be "
                                  "asked (a secret on the command line sits in shell history and `ps`)")
            problem = config_value_problem(spec, value)
            if problem:
                raise ModuleError(f"{flag} {key}: {problem}")
            into[key] = value
    if fleet_vals and not owner:
        raise ModuleError("--set-fleet needs the owner key: set fleet values on the owner machine")
    if enforce:
        fleet_have = None
        interactive = sys.stdin is not None and sys.stdin.isatty() and not getattr(args, "non_interactive", False)
        missing = []
        for key, spec in sorted(schema.items()):
            scope = spec.get("scope", "node")
            if (not spec.get("required_at_install") or (asked is not None and key not in asked) or "default" in spec
                    or "{" in key or scope == "fleet-node"):
                continue                  # install asks for one concrete node or fleet value; a template or a device value is set later
            if scope == "node" and (key in node_vals or key in (have or {})):
                continue
            if scope == "fleet":
                if key in fleet_vals:
                    continue
                if fleet_have is None:
                    fleet_have = fleet_layers(_gov(lib), manifest["name"], lib.NODE_ID)[0]
                if key in fleet_have or not owner:
                    continue                  # set already, or only the owner can set it: `config validate` reports it
            if interactive:
                (fleet_vals if scope == "fleet" else node_vals)[key] = _prompt(key, spec)
            else:
                missing.append(f"{key} ({spec['description']})" + (" [secret: --set-env]" if spec.get("secret") else ""))
        if missing:
            raise ModuleError("this module needs these settings before it installs, and no terminal is available to ask: "
                              + "; ".join(missing) + ". Pass --set key=value (--set-fleet for a fleet key)")
    if fleet_vals:
        _require_owner(lib, [("set-config", manifest["name"])], prompt=_may_prompt(args))
    return node_vals, fleet_vals


def store_fleet(lib, name, key, value, spec, tag=None):
    """Sign one `x-<name>:` value as the owner; `tag` makes it a `fleet-node` key for that device. Nothing is signed
    when the journal already holds this value."""
    full = f"x-{name}:" + (f"node.{tag}.{key}" if tag else key)
    if _gov(lib)["module_config"].get(full) == value:
        return False
    if _planning(lib) and ((spec and spec.get("secret")) or (spec is None and _is_secret_key(key))):
        raise ModuleError(f"{key} is a secret: a plan prints the value, so it cannot be planned; set it on the owner machine")
    if spec and spec.get("identifying"):
        print(f"note: {key} is marked identifying; a fleet value is journaled and visible to every admitted device "
              "for good", file=sys.stderr)
    try:
        lib._config_set(full, value)
    except SystemExit:
        pass
    if not _planning(lib) and _gov(lib)["module_config"].get(full) != value:
        raise ModuleError(f"{full} was not stored (the owner key is locked or the entry was refused)")
    return True


def store_fleet_values(lib, name, schema, fleet_vals):
    try:
        for key, value in fleet_vals.items():
            store_fleet(lib, name, key, value, schema_entry(schema, key)[1])
    except ModuleError as e:
        raise ModuleError(f"module {name} is installed, but a fleet setting was not stored: {e}. "
                          f"Set it with `hive-mind module config {name} set <key> <value> --fleet`")


def _config_value_arg(args, key, spec):
    secret = bool(spec and spec.get("secret")) or (spec is None and _is_secret_key(key))
    if getattr(args, "plan", False) and (getattr(args, "stdin", False) or getattr(args, "file", None)):
        raise ModuleError("--plan prints the value it would sign: give it as an argument, not with --stdin or --file")
    if getattr(args, "stdin", False) and getattr(args, "file", None):
        raise ModuleError("give the value with --stdin or --file, not both")
    if getattr(args, "stdin", False):
        if args.value is not None:
            raise ModuleError("--stdin takes the value from standard input: do not also give it as an argument")
        return _read_secret_line()
    if getattr(args, "file", None):
        if args.value is not None:
            raise ModuleError("--file takes the value from a file: do not also give it as an argument")
        return _read_secret_file(args.file)
    if secret:
        if args.value is not None:
            raise ModuleError(f"{key} is a secret: do not put it on the command line (shell history, `ps`); "
                              "it is never taken from the command line. "
                              "Omit the value to be asked, or pass --stdin or --file PATH")
        if getattr(args, "non_interactive", False) or sys.stdin is None or not sys.stdin.isatty():
            raise ModuleError(f"{key} is a secret: no terminal to ask on, so pass --stdin or --file PATH")
        return getpass.getpass(f"{key} (hidden): ")
    if args.value is None:
        raise ModuleError("config set needs a value")
    return args.value


def _config_set_verb(lib, args, name, schema, node, path):
    key = args.key
    ent = schema_entry(schema, key) if schema is not None else None
    if schema is not None and ent is None:
        raise ModuleError(f"{name} has no config key {key!r} in its schema (`hive-mind module config {name} list`)")
    spec = ent[1] if ent else None
    if args.fleet and args.fleet_node:
        raise ModuleError("--fleet and --fleet-node are two layers: give one")
    layer = "fleet" if args.fleet else "fleet-node" if args.fleet_node else "node"
    if spec and spec.get("scope", "node") != layer:
        raise ModuleError(f"{key} has scope {spec.get('scope', 'node')}: set it with {SCOPE_FLAG[spec.get('scope', 'node')]}")
    quiet = bool(getattr(args, "non_interactive", False))
    if quiet:
        lib._NON_INTERACTIVE = True            # every passphrase prompt the verb could raise becomes exit 5 (4.0 C2)
        if layer != "node":                    # the owner gate comes BEFORE the value is read, so a locked key never reads stdin
            if layer == "fleet-node":
                _device_tag(args.fleet_node)
            _require_owner(lib, [("set-config", name)], prompt=False)
    value = _config_value_arg(args, key, spec)
    if spec:
        problem = config_value_problem(spec, value)
        if problem:
            raise ModuleError(f"{key}: {problem}")
    elif len(value) > vocabulary.MODULE_VALUE_MAX:
        raise ModuleError(f"config value is longer than {vocabulary.MODULE_VALUE_MAX} characters")
    if layer == "node":
        if schema is not None and {r["key"]: r["value"] for r in merged_config(schema, node, {}, {})}.get(key) == value:
            print(f"{name}: {key} is already set: nothing changed")
            return
        have = dict(node)
        have[key] = value
        _save_config(path, have)
        print(f"{name}: set {key}")
        return
    tag = _device_tag(args.fleet_node) if layer == "fleet-node" else None
    if not quiet:
        _require_owner(lib)
    if store_fleet(lib, name, key, value, spec, tag):
        print(f"{name}: set {key} ({layer})")
    else:
        print(f"{name}: {key} is already set ({layer}): nothing signed")


def _config_validate(schema, node, fleet, mine, as_json):
    if schema is None:
        errors, warnings = [], ["this module declares no config_schema: nothing to validate"]
    else:
        errors, warnings = config_problems(schema, node, fleet, mine)
    if as_json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, indent=1))
    else:
        for e in errors:
            print(f"error: {e}")
        for w in warnings:
            print(f"warning: {w}")
        if not errors:
            print("config ok")
    return 1 if errors else 0


def cmd_config(lib, args):
    """`module config <name> list|get|set|unset|validate`. Node values are this node's own file (local, never
    journaled, kept by update, no owner key); `--fleet` and `--fleet-node <device>` are owner-signed. With a
    `config_schema` the key, type, enum, bounds and scope are checked and `list`/`get` show the merged value and its
    source; secrets are masked everywhere and are never taken from the command line."""
    path = _config_path(lib, args.name)
    schema = installed_manifest(lib, args.name).get("config_schema")
    action = args.action
    if action == "schema":              # the installed manifest's block, as signed: for a UI that renders a form (4.0 C2)
        if schema is None:
            raise ModuleError(f"{args.name} declares no config_schema")
        print(json.dumps(schema, indent=1, sort_keys=True))
        return
    node = _read_config(path)
    merged = schema is not None or args.merged
    fleet, mine = fleet_layers(_gov(lib), args.name, lib.NODE_ID) if merged or action == "validate" else ({}, {})
    if action == "validate":
        return _config_validate(schema, node, fleet, mine, args.json)
    if action == "list":
        if not merged and not args.json:
            for k in sorted(node):
                print(f"{k}={MASK if _is_secret_key(k) and node[k] else node[k]}")
            return
        _print_rows(merged_config(schema, node, fleet, mine), args.json)
        return
    if not vocabulary.split_module_name(f"x-{args.name}:{args.key}"):
        raise ModuleError(f"bad config key {args.key!r}: 1 to {vocabulary.MODULE_KEY_MAX} letters, digits, . _ - "
                          "starting with a letter or digit")
    if action == "get":
        if schema is not None and schema_entry(schema, args.key) is None:
            raise ModuleError(f"{args.name} has no config key {args.key!r} in its schema")
        row = next((r for r in merged_config(schema, node, fleet, mine) if r["key"] == args.key), None)
        if row is None or row["value"] is None:
            raise ModuleError(f"{args.name}: {args.key} has no value")
        if args.json:
            _print_rows([row], True)
        else:
            print(_shown(row))
        return
    if action == "set":
        return _config_set_verb(lib, args, args.name, schema, node, path)
    if args.fleet or args.fleet_node:
        raise ModuleError("a fleet value is journaled and cannot be removed; set it to a new value instead")
    if args.key not in node:
        raise ModuleError(f"{args.name} has no config key {args.key!r}")
    have = dict(node)
    del have[args.key]
    _save_config(path, have)
    print(f"{args.name}: unset {args.key}")


def cmd_add(lib, args):
    ok, why = platform_supported()
    if not ok:
        raise ModuleError(why)
    name = args.name
    if not vocabulary.valid_module_name(name):
        raise ModuleError(f"{name!r} is not a valid module name")
    if args.abort:
        return _abort_add(lib, name)
    if not args.source:
        raise ModuleError("module add needs --from <repository> (or --abort to drop a pending add)")
    state = load_state(lib)
    root = modules_dir()
    if name in state or (root / name).exists():
        raise ModuleError(f"module {name!r} is already installed: use `hive-mind module update {name}`")
    pending = _read_pending(name)
    if not lib._owner_key_exists():
        if pending is not None:
            return _complete_add(lib, args, name, root, pending)
        return _stage_for_owner(lib, args, name, root)
    if pending is not None:
        raise ModuleError(f"module {name!r} has a pending add from a device without the owner key: "
                          f"`hive-mind module add --abort {name}` first")
    _require_owner(lib, [("admit", name), ("revoke", None)], prompt=_may_prompt(args))
    principal = _operator_principal(lib, args.principal)
    with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
        src = Path(tmp) / "src"
        tracked, head = _fetch(args.source, src)
        manifest = check_tree(src, lib.CONTRACT_VERSION, tracked)
        if manifest["name"] != name:
            raise ModuleError(f"the repository holds module {manifest['name']!r}, not {name!r}")
        _confirm_publisher(manifest, args.publisher)
        plan = contract_plan(name, manifest, state)
        node_vals, fleet_vals = install_config(lib, args, manifest, owner=True)
        if _planning(lib):
            return _plan_add(lib, args, name, principal, manifest, fleet_vals)
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        staging = root / f".{name}.staging"
        try:
            _stage(src, manifest, staging)
            device_id = _module_key(lib, name, getattr(args, "device_id", None))
        except (ModuleError, FileExistsError, OSError, ValueError, RuntimeError) as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise ModuleError(str(e))
        try:
            try:
                lib.admit_cmd(argparse.Namespace(device_id=device_id, principal=principal, module=name))
            except SystemExit:
                pass              # a refused admit: the check below reports it as a ModuleError
            gov = _gov(lib)
            if gov["modules"].get(device_id) != name or device_id not in gov["admitted"]:
                raise ModuleError("the owner could not admit the module's device: nothing installed")
            _write_config(staging, {**manifest.get("config", {}), **node_vals})
            os.rename(staging, root / name)
            note = install_units(lib, name, manifest, plan and plan["path"])
            apply_launcher(lib, name, manifest, plan)
            state[name] = {"version": manifest["version"], "publisher": manifest["publisher"], "device_id": device_id,
                           "source": args.source, "commit": head, "installed_at": int(time.time()),
                           "quota": dict(manifest.get("quota") or {}), "owner_quota": {},
                           **_reads_record(manifest), **_trust_record(manifest)}
            record_plan(state[name], plan)
            save_state(lib, state)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            _undo_add(lib, name, device_id, plan)
            raise
        if fleet_vals:
            store_fleet_values(lib, name, manifest["config_schema"], fleet_vals)
    warn_missing(name, plan)
    print(f"installed module {name} {manifest['version']} (device {device_id}, principal {principal}); "
          + (f"service {unit_names(name)[0]}" + (" with a timer" if manifest["service"].get("interval") else "")
             if manifest.get("service") else "no service")
          + (f" ({note})" if note else "") + "; hooks: " + (", ".join(manifest.get("hooks") or []) or "none"))
    return _healthy_rc(args, doctor_after(lib, name, "install"))


def _key_device_id(lib, name):
    """The device id of module `name`'s key in the key dir, or None if there is none or it is unreadable."""
    key = Path(lib._ensure_key_dir()) / "modules" / name / "device-key"
    try:
        return lib._device_id_for_pub(ed25519.pub_from_seed(base64.b64decode(key.read_text().strip())))
    except Exception:
        return None


def _module_key(lib, name, given):
    """The module's device id: a fresh key, or with `--device-id` (a plan's replay) the key the plan minted, which
    must be there and must be the one that id names."""
    if not given:
        return lib.mint_module_key(name)[0]
    if _key_device_id(lib, name) != given:
        raise ModuleError(f"--device-id {given}: no module key of that id is here (run `module add {name} --plan --json` again)")
    return given


def _plan_add(lib, args, name, principal, manifest, fleet_vals):
    """`module add --plan --json`: the bodies the add would sign (the module's admit, then each `--set-fleet` value) and
    the argv that makes the real run produce the same ones. The one minted field, the module's device id, comes from a
    module key this mints (or finds, from an earlier plan) in the key dir: it is a device key, not owner material, and
    the real run takes it over with `--device-id`. Nothing is staged, installed or signed."""
    given = getattr(args, "device_id", None)
    try:
        device_id = given and _module_key(lib, name, given) or _key_device_id(lib, name) or lib.mint_module_key(name)[0]
    except (FileExistsError, OSError, ValueError, RuntimeError) as e:
        raise ModuleError(str(e))
    args._replay = ["--device-id", device_id] + ([] if args.principal else ["--principal", principal])
    try:
        lib.admit_cmd(argparse.Namespace(device_id=device_id, principal=principal, module=name))
    except SystemExit:
        raise ModuleError("the owner could not admit the module's device: nothing to plan")
    try:
        store_fleet_values(lib, name, manifest["config_schema"], fleet_vals)
    except ModuleError as e:
        raise ModuleError(str(e).split(". Set it with")[0])


def _healthy_rc(args, worst):
    """`--require-healthy`: a module whose doctor reported a `fail` makes the verb exit 1. The install stays."""
    return 1 if getattr(args, "require_healthy", False) and worst == "fail" else 0


def _undo_add(lib, name, device_id, plan=None):
    """A failed add leaves nothing: the key goes, and a device that was admitted is revoked."""
    if plan and plan.get("launcher"):
        remove_launcher(plan["bin_dir"], plan["launcher"], name)
    try:
        if _device_state(lib, device_id) == "admitted":
            lib._group_change("revoke", device_id)
    except Exception:
        pass
    try:
        still = _device_state(lib, device_id) == "admitted"
    except Exception:
        still = False
    if still:        # the revoke could not be signed (cancelled prompt, agent gone): never silent
        print(f"device {device_id} is still admitted as a module device: run `hive-mind group revoke {device_id}`",
              file=sys.stderr)
    shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / name, ignore_errors=True)
    shutil.rmtree(modules_dir() / name, ignore_errors=True)
    try:
        remove_units(name)
    except ModuleError:
        pass


def cmd_remove(lib, args):
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    device_id = rec["device_id"]
    if not lib._owner_key_exists():
        return _remove_without_owner(lib, args.name, device_id, state, args.purge)
    _require_owner(lib, [("revoke", device_id)] if _device_state(lib, device_id) == "admitted" else [])
    if _device_state(lib, device_id) == "admitted":
        lib._group_change("revoke", device_id)
    if _planning(lib):
        return
    if _device_state(lib, device_id) == "admitted":
        raise ModuleError("the owner could not revoke the module's device: nothing removed")
    remove_units(args.name)                       # stop it before its files and key go
    remove_launcher(rec.get("bin_dir"), rec.get("launcher"), args.name)
    shutil.rmtree(modules_dir() / args.name, ignore_errors=True)
    shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / args.name, ignore_errors=True)
    if args.purge:
        purge_state_dir(args.name)
    state.pop(args.name)
    save_state(lib, state)
    drop_doctor_cache(lib.HIVE_HOME, args.name)
    print(f"removed module {args.name}; its device {device_id} is revoked and its entries stay in the journal")


def _remove_without_owner(lib, name, device_id, state, purge=False):
    """Remove on a device without the owner key: stop the unit, delete the tree and the key, and say what only the
    owner can finish. The device id stays admitted until the owner's revoke."""
    remove_units(name)                            # stop it before its files and key go
    remove_launcher(state[name].get("bin_dir"), state[name].get("launcher"), name)
    shutil.rmtree(modules_dir() / name, ignore_errors=True)
    _clear_pending(lib, name)
    shutil.rmtree(Path(lib._ensure_key_dir()) / "modules" / name, ignore_errors=True)
    if purge:
        purge_state_dir(name)
    state.pop(name)
    save_state(lib, state)
    drop_doctor_cache(lib.HIVE_HOME, name)
    if _device_state(lib, device_id) == "admitted":
        print(f"removed module {name} from this node; its device {device_id} STAYS ADMITTED until the owner revokes it. "
              f"On the owner machine run:\n  hive-mind group revoke {device_id}")
    else:
        print(f"removed module {name} from this node; its device {device_id} is already {_device_state(lib, device_id)}")


def _check_trust_raise(lib, rec, manifest, prompt=True):
    """An update may keep or lower the tier the module holds; one that signs a higher tier than the node last held it
    at is a raise, and only the owner's key may make it. Checked before anything is fetched into place."""
    old = rec.get("trust", "trusted")
    if TRUST_TIERS.index(signed_trust(manifest)) > TRUST_TIERS.index(old):
        print(f"module {manifest['name']}: this update raises the signed trust tier from {old} to {signed_trust(manifest)}")
        _require_owner(lib, prompt=prompt)


def cmd_trust(lib, args):
    """Show a module's tier, or set the node-local one. `confined` lowers it and needs no key; `trusted` clears that
    lowering, which is a raise, so it needs the owner's key. Neither goes above what the publisher signed."""
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    if args.tier is None:
        print(f"{args.name}: {effective_trust(rec)} (signed {rec.get('trust', 'trusted')}"
              + (f", lowered on this node to {rec['trust_local']}" if rec.get("trust_local") else "") + ")")
        return
    if args.tier == "trusted":
        if rec.get("trust_local"):
            _require_owner(lib)
            rec.pop("trust_local")
            save_state(lib, state)
        if rec.get("trust", "trusted") == "confined":
            print(f"{args.name} stays confined: the publisher signed it that way, and only a signed manifest raises it")
            return
    elif rec.get("trust", "trusted") != "confined":
        rec["trust_local"] = "confined"
        save_state(lib, state)
    _reapply_names(lib, [args.name])           # the unit is rendered for the tier: re-render, and restart if it changed
    print(f"{args.name}: {effective_trust(rec)}")


def cmd_update(lib, args):
    ok, why = platform_supported()
    if not ok:
        raise ModuleError(why)
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    source = args.source or rec["source"]            # no owner key needed: it admits, revokes and re-limits nothing
    with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
        src = Path(tmp) / "src"
        tracked, head = _fetch(source, src)
        manifest = _candidate(lib, args.name, rec, src, tracked, args.publisher)
        return _update_tree(lib, args, state, rec, source, src, manifest, head)


def _candidate(lib, name, rec, src, tracked, publisher=None):
    """The verified manifest of the fetched tree `src` as an update of installed module `name`: the same tree check as
    an add, the same module, signed by the pinned publisher. Raises ModuleError."""
    manifest = check_tree(src, lib.CONTRACT_VERSION, tracked)
    if manifest["name"] != name:
        raise ModuleError(f"the repository holds module {manifest['name']!r}, not {name!r}")
    _confirm_publisher(manifest, publisher, pinned=rec["publisher"])
    return manifest


def _update_tree(lib, args, state, rec, source, src, manifest, head):
    """Swap the verified `manifest` tree at `src` in for the installed module, its units and launcher with it, and roll
    back on failure. A successful update lifts a `disabled` mark: this version is one the core admits."""
    root = modules_dir()
    _check_trust_raise(lib, rec, manifest, prompt=_may_prompt(args))
    print(f"module {args.name} {manifest['version']} declares: {trust_summary(manifest)}")
    if reads_summary(manifest):                   # the pinned key needs no prompt, but the new read access is shown
        print(f"module {args.name} {manifest['version']} declares: {reads_summary(manifest)}")
    for line in commands_lines(manifest):
        print(line)
    try:
        before = installed_manifest(lib, args.name).get("config_schema") or {}
        have = _read_config(modules_dir() / args.name / "config")
    except ModuleError:                       # update is the remedy for a broken install: ask for every required key
        before, have = {}, {}
    plan = contract_plan(args.name, manifest, state, rec.get("runtime_overrides"))
    node_vals, fleet_vals = install_config(lib, args, manifest, owner=lib._owner_key_exists(),
                                           asked=set(manifest.get("config_schema") or {}) - set(before), have=have)
    staging, old = root / f".{args.name}.staging", root / f".{args.name}.old"
    try:
        _stage(src, manifest, staging)
    except (ModuleError, OSError):
        shutil.rmtree(staging, ignore_errors=True)
        raise
    live = root / args.name
    swapped = False
    try:
        _write_config(staging, {**manifest.get("config", {}), **node_vals,
                                **(json.loads((live / "config").read_text()) if (live / "config").exists() else {})})
        shutil.rmtree(old, ignore_errors=True)
        os.rename(live, old)                  # two renames: a crash between them leaves the old copy at `.old`
        swapped = True                        # only now is `.old` this update's; a stale one is never rolled back
        os.rename(staging, live)
        note = install_units(lib, args.name, manifest, plan and plan["path"])
        apply_launcher(lib, args.name, manifest, plan)
    except BaseException:
        if swapped:
            shutil.rmtree(live, ignore_errors=True)
            os.rename(old, live)
            try:                                  # put the old unit back beside the old files
                before_manifest = json.loads((live / MANIFEST).read_text())
                if is_disabled(rec):                  # still disabled: the failed attempt's start is undone, nothing is revived
                    _stop_units(args.name)
                else:
                    install_units(lib, args.name, before_manifest, recorded_path(rec))
                if plan and plan.get("launcher") != rec.get("launcher"):
                    remove_launcher(plan["bin_dir"], plan.get("launcher"), args.name)
                if not is_disabled(rec):              # a disabled module's launcher stays gone: it would run the rejected tree
                    _restore_launcher(lib, args.name, rec, before_manifest)
            except (OSError, ValueError, KeyError):
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    gone = (rec.get("bin_dir"), rec.get("launcher"))
    rec.update(version=manifest["version"], source=source, commit=head,
               quota=dict(manifest.get("quota") or {}))     # the owner's own limits (owner_quota) are kept
    record_plan(rec, plan)
    rec.pop("reads", None)                                  # the new manifest's list replaces the old, or none
    rec.update(_reads_record(manifest))
    rec.pop("trust", None)                                  # the signed tier is the new manifest's; trust_local stays
    rec.update(_trust_record(manifest))
    rec.pop("disabled", None)                               # a disabled-incompatible module is back
    if gone[1] and gone[1] != rec.get("launcher"):          # the new version renamed (or dropped) its cli
        remove_launcher(*gone, args.name)
    save_state(lib, state)
    warn_missing(args.name, plan)
    if fleet_vals:
        store_fleet_values(lib, args.name, manifest["config_schema"], fleet_vals)
    print(f"updated module {args.name} to {manifest['version']}; its key, device and pinned publisher are unchanged"
          + (f" ({note})" if note else ""))
    return _healthy_rc(args, doctor_after(lib, args.name, "update"))


def is_disabled(rec):
    """Whether `hive-mind update` disabled this module because no version of it admits this core."""
    return isinstance(rec, dict) and isinstance(rec.get("disabled"), dict)


def _stop_units(name):
    """Stop and disable the module's units without deleting them, so the update that fixes it can start them again.
    Only the module's own two units are named: seat sessions it started in their own scopes are not touched. Returns
    False when a unit would not stop."""
    ok = True
    d = unit_dir()
    for f in reversed(unit_names(name)):
        if (d / f).exists():
            ok = _systemctl("disable", "--now", f, timeout=_stop_timeout(_unit_grace(d / f))) and ok
    return ok


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


# ── `module install`: the one idempotent command (3.1, #294 PR 5, design §8) ───────────────────────

EXIT_REFUSED, EXIT_WAITING, EXIT_UNHEALTHY, EXIT_LOCKED = 1, 3, 4, 5      # 0 converged, 2 usage (argparse)


def _doctor_row(lib, name):
    """The doctor's `modules:<name>` row for one installed module (step 11). It never raises."""
    import hive_module_doctor
    import merkle
    try:
        return hive_module_doctor._module(lib, _gov(lib), name, load_state(lib)[name],
                                          merkle.read_all_entries(lib.JOURNAL_DIR), hive_module_doctor.listener_up(),
                                          time.time())
    except Exception as e:
        return {"name": f"modules:{name}", "status": "warn", "detail": f"the doctor could not run ({e})"}


def _install_present(lib, args, rec):
    """An installed module: a different commit (or a tree that no longer verifies) takes the `update` path; the same
    commit converges the rest (config given on the command line, units, launcher) and changes nothing else."""
    name = args.name
    if args.publisher is not None and args.publisher != rec["publisher"]:
        raise ModuleError("--publisher does not match the publisher key pinned at the first install: refusing")
    with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
        _tracked, head = _fetch(args.source, Path(tmp) / "src")
    if head != rec.get("commit") or args.source != rec["source"] or _installed_signature(lib, name) != "valid":
        cmd_update(lib, args)
        return "updated"
    manifest = installed_manifest(lib, name)
    path = modules_dir() / name / "config"
    have = _read_config(path)
    node_vals, fleet_vals = install_config(lib, args, manifest, owner=lib._owner_key_exists(), enforce=False, have=have)
    changed = {k: v for k, v in node_vals.items() if have.get(k) != v}
    gov_cfg = _gov(lib)["module_config"]
    fleet_changed = {k: v for k, v in fleet_vals.items() if gov_cfg.get(f"x-{name}:{k}") != v}
    if changed:
        _save_config(path, {**have, **changed})
    if fleet_changed:
        store_fleet_values(lib, name, manifest["config_schema"], fleet_changed)
    _reapply_names(lib, [name])                  # units and launcher: rewritten only when their text differs
    if changed or fleet_changed:
        print(f"module {name} {rec['version']} is installed at {head}: configuration updated")
        return "configured"
    print(f"module {name} {rec['version']} is installed at {head}: nothing to do")
    return "unchanged"


def _install(lib, args):
    ok, why = platform_supported()
    if not ok:
        raise ModuleError(why)
    name = args.name
    if not vocabulary.valid_module_name(name):
        raise ModuleError(f"{name!r} is not a valid module name")
    rec = load_state(lib).get(name)
    args.source = args.source or (rec or {}).get("source")
    if not args.source:
        raise ModuleError(f"module {name!r} is not installed: module install needs --from <repository>")
    args.abort = False
    if rec is not None:
        return _install_present(lib, args, rec)
    cmd_add(lib, args)
    return "installed" if name in load_state(lib) else "staged"


def cmd_install(lib, args):
    """Converge on the installed module the manifest and config describe. Exit 0 converged (or nothing to do), 1
    refused with nothing changed, 3 waiting for the owner's admit (a member node: staged), 4 installed but the doctor
    reported `fail` (`--require-healthy`), 5 owner key locked. `--json` prints one object and never prompts."""
    if args.json:
        args.non_interactive = True
    args.no_owner_prompt = args.non_interactive
    out, err = io.StringIO(), io.StringIO()
    outcome, rc, error, row = "refused", EXIT_REFUSED, None, None
    with contextlib.redirect_stdout(out) if args.json else contextlib.nullcontext(), \
            contextlib.redirect_stderr(err) if args.json else contextlib.nullcontext():
        try:
            outcome = _install(lib, args)
            rc = 0
        except OwnerWait as e:
            outcome, rc, error = "waiting", EXIT_WAITING, str(e)
        except OwnerLocked as e:
            outcome, rc, error = "locked", EXIT_LOCKED, str(e)
        except lib.OwnerKeyLocked as e:
            outcome, rc, error = "locked", EXIT_LOCKED, str(e)
        except ModuleError as e:
            error = str(e)
        if outcome == "staged":
            rc = EXIT_WAITING
        if rc == 0:
            row = _doctor_row(lib, args.name)
            print(f"doctor: {row['name']} {row['status']}: {row['detail']}")
            if args.require_healthy and row["status"] == "fail":
                outcome, rc = "unhealthy", EXIT_UNHEALTHY
        if error and not args.json:
            print(f"hive-mind module: {error}", file=sys.stderr)
    if args.json:
        rec = load_state(lib).get(args.name) or {}
        pending = _read_pending(args.name)
        print(json.dumps({"ok": rc == 0, "exit": rc, "name": args.name, "result": outcome,
                          "version": rec.get("version") or (pending or {}).get("version"),
                          "commit": rec.get("commit") or (pending or {}).get("commit"),
                          "admit_line": _admit_line(pending, args.name) if pending and rc == EXIT_WAITING else None,
                          "error": error, "doctor": row,
                          "messages": out.getvalue().splitlines(), "warnings": err.getvalue().splitlines()},
                         indent=1, sort_keys=True))
    return rc


def _list_rows(lib):
    state = load_state(lib)
    rows = []
    for name, rec in sorted(state.items()):
        sig = _installed_signature(lib, name)
        rows.append({"name": name, "version": rec["version"], "publisher": fingerprint(rec["publisher"]),
                     "signature": sig.split(" ")[0], "service": service_state(name),
                     "device": _device_state(lib, rec["device_id"]), "trust": effective_trust(rec),
                     "quota_use": _quota_use(lib, rec["device_id"]), "problem": None if sig == "valid" else sig,
                     "config_schema": installed_manifest(lib, name).get("config_schema") is not None})
    return rows


def cmd_list(lib, args):
    rows = _list_rows(lib)
    if getattr(args, "json", False):
        print(json.dumps({"schema": 1, "modules": rows}, indent=2))
        return
    if not rows:
        print("No modules installed.")
        return
    print(f"{'NAME':<16}{'VERSION':<10}{'PUBLISHER':<18}{'SIGNATURE':<10}{'SERVICE':<14}{'DEVICE':<10}{'TRUST':<10}QUOTA USE")
    for r in rows:
        print(f"{r['name']:<16}{r['version']:<10}{r['publisher']:<18}{r['signature']:<10}{r['service']:<14}"
              f"{r['device']:<10}{r['trust']:<10}{r['quota_use']}")
        if r["problem"]:
            print(f"  ! {r['problem']}")


def cmd_quota(lib, args):
    """The owner's way to raise or lower one limit. A manifest may only ask for less; this is how it goes up."""
    state = load_state(lib)
    rec = state.get(args.name)
    if rec is None:
        raise ModuleError(f"module {args.name!r} is not installed")
    _require_owner(lib)                           # nothing is signed: the agent's public-key check is enough
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
    _reapply_names(lib, sorted(load_state(lib)))


def _reapply_names(lib, names):
    """Re-render the units (and, for a contract-1 module, re-resolve its runtimes and refresh its launcher and
    recorded PATH) of the named modules. Never raises."""
    if not platform_supported()[0]:
        return
    pending, dirty = [], False
    state = load_state(lib)
    for name in names:
        rec = state.get(name)
        if rec is None or is_disabled(rec):       # a disabled module's units stay off until an update fixes it
            continue
        try:
            manifest = check_tree(modules_dir() / name, lib.CONTRACT_VERSION, ignore=("config",))
            plan = contract_plan(name, manifest, state, rec.get("runtime_overrides"))
            unit = _write_units(lib, name, manifest, only_changed=True, path=plan and plan["path"])
            if plan:
                gone = (rec.get("bin_dir"), rec.get("launcher"))
                apply_launcher(lib, name, manifest, plan)
                before = dict(rec)
                record_plan(rec, plan)
                dirty = dirty or rec != before
                if gone[1] and gone[1] != rec.get("launcher"):
                    remove_launcher(*gone, name)
                warn_missing(name, plan)
        except (ModuleError, OSError, ValueError, KeyError) as e:
            print(f"hive-mind module: warning: {name}: units not re-applied ({e})", file=sys.stderr)
            continue
        if unit:
            pending.append((name, unit))
    if dirty:
        try:
            save_state(lib, state)
        except OSError as e:
            print(f"hive-mind module: warning: could not record the runtimes ({e})", file=sys.stderr)
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


# ── `hive-mind update` brings the installed modules along (3.1, #390) ───────────────────────────────

def _version_key(v):
    """A version that is only one to three dotted integers (`1`, `1.2`, `1.2.3`), as a tuple; None for anything else
    (a suffix, a leading non-digit, a fourth component), which cannot be ordered."""
    if not (isinstance(v, str) and re.fullmatch(r"\d{1,9}(\.\d{1,9}){0,2}", v)):
        return None
    parts = tuple(int(x) for x in v.split("."))
    return parts + (0,) * (3 - len(parts))


def _refuse_unless_newer(have, new):
    """An automatic update installs a strictly newer version, or a republish of the same version string. Anything the
    two strings cannot be ordered by is refused, so a signed older tree cannot pass as an update."""
    if have == new:
        return
    hk, nk = _version_key(have), _version_key(new)
    if hk and nk and nk > hk:
        return
    why = "older than" if hk and nk and nk < hk else "not provably newer than"
    raise ModuleError(f"the source offers {new}, {why} the installed {have}: an automatic update only goes forward "
                      "(`hive-mind module update` takes any tree the pinned publisher signed)")


def _installed_compat(lib, name):
    """None when the installed tree still verifies under this core (so it is compatible), else why not. An OSError
    is not an answer about compatibility: it is None too, so a read error never disables a module."""
    try:
        check_tree(modules_dir() / name, lib.CONTRACT_VERSION, ignore=("config",))
    except ModuleError as e:
        return str(e)
    except OSError:
        return None
    return None


def _refresh_args(name):
    return argparse.Namespace(name=name, source=None, publisher=None, set=[], set_fleet=[], set_env=[],
                              non_interactive=True, no_owner_prompt=True, require_healthy=False)


def _loud(lines):
    bar = "!" * 72
    print("\n".join(["", bar, *lines, bar, ""]), file=sys.stderr)


def _refresh_one(lib, name, state, check):
    """One module. Returns "current", "updated", "kept" (update failed, the installed version still admits this core)
    or "disabled". Nothing here raises."""
    rec = state[name]
    off = is_disabled(rec)
    reason, manifest = None, None
    try:
        if not isinstance(rec.get("source"), str) or not rec["source"]:
            raise ModuleError("no source is recorded for it")
        with tempfile.TemporaryDirectory(prefix="hive-module-") as tmp:
            src = Path(tmp) / "src"
            tracked, head = _fetch(rec["source"], src)
            manifest = _candidate(lib, name, rec, src, tracked)
            _refuse_unless_newer(rec.get("version"), manifest["version"])
            if not off and head == rec.get("commit") and _installed_signature(lib, name) == "valid":
                print(f"module {name} {rec['version']} is current")
                return "current"
            if check:
                print(f"module {name}: would {'re-enable and ' if off else ''}update {rec['version']} -> {manifest['version']}")
                return "updated"
            _update_tree(lib, _refresh_args(name), state, rec, rec["source"], src, manifest, head)
            return "updated"
    except (ModuleError, OSError, ValueError, KeyError, TypeError, lib.OwnerKeyLocked) as e:
        reason = str(e) or type(e).__name__
    except subprocess.SubprocessError as e:
        reason = f"git failed ({e})"
    problem = _installed_compat(lib, name)
    if off:
        _loud([f"module {name} stays DISABLED: {reason}",
               f"fix it, then run: hive-mind module update {name}"])
        return "disabled"
    if problem is None:
        _loud([f"module {name} {rec.get('version')} was NOT updated: {reason}",
               "The installed version still works with this core and keeps running.",
               f"To retry: hive-mind module update {name}"])
        return "kept"
    if check:
        print(f"module {name}: would DISABLE (no version admits this core: {reason}; installed: {problem})")
        return "disabled"
    _disable(lib, name, state, f"{reason}; the installed version does not admit this core ({problem})")
    return "disabled"


def _disable(lib, name, state, why):
    rec = state[name]
    stopped = _stop_units(name)
    if rec.get("bin_dir") and rec.get("launcher"):
        with contextlib.suppress(OSError, ModuleError):
            remove_launcher(rec["bin_dir"], rec["launcher"], name)      # the launcher would still run its code
    rec["disabled"] = {"reason": why[:500], "core": str(lib.CONTRACT_VERSION), "at": int(time.time())}
    save_state(lib, state)
    _loud([f"module {name} is DISABLED: no version of it works with this core.", f"why: {why}",
           "Its units are stopped and disabled, its hooks and command are off; nothing else of it was deleted."
           + ("" if stopped else f" A unit would not stop: check `systemctl --user status hive-module-{name}`."),
           f"`hv doctor` reports modules:{name} as disabled-incompatible until it is fixed.",
           f"To bring it back once a compatible version is published: hive-mind module update {name}",
           "The core update itself succeeded."])


def cmd_refresh(lib, args):
    """Internal, run by `hive-mind update` after the core update: bring each installed module to the newest signed
    version at its recorded source that this core admits, or say loudly why not. It never fails the core update.
    `--check` plans the same run and changes nothing."""
    if not platform_supported()[0]:
        return 0
    state = load_state(lib)
    keep = []
    for name in sorted(state):
        if not isinstance(state[name], dict) or not vocabulary.valid_module_name(name):
            continue
        outcome = _refresh_one(lib, name, state, args.check)
        if outcome in ("current", "kept") and not args.check:
            keep.append(name)
    if keep:
        _reapply_names(lib, keep)       # the new core's unit text, for a module that stays on its installed version
    return 0


# ── `module check`: the publisher's conformance run ─────────────────────────────────────────────────

CHECK_TIMEOUT, CHECK_OUTPUT_CAP = 20, 64 * 1024
STATUS_STATES = ("ok", "degraded", "down", "unknown")
DOCTOR_LEVELS = ("ok", "warn", "fail")
_TEXT_LIMIT = 1024 * 1024
_CODE_SUFFIXES = {".py", ".sh", ".js", ".ts", ".rb", ".pl", ".rs", ".go", ".c", ".h", ".java"}
_LINTS = (
    ("an email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+[A-Za-z]"), False),
    ("a home directory", re.compile(r"/(?:home|Users)/(?![<{$%]|user\b|you\b|name\b|username\b)[A-Za-z0-9._-]+"), False),
    ("an @-handle", re.compile(r"(?<![\w@.])@[A-Za-z0-9][A-Za-z0-9-]{1,38}\b(?!\.[a-z])"), True),
    ("a repository", re.compile(r"github\.com[/:](?![<{$%])[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"), False),
)


def status_problems(obj):
    """Why `obj` is not the fixed `status` envelope, as a list of lines (empty when it is)."""
    if not isinstance(obj, dict):
        return ["status did not print a JSON object"]
    out = []
    if obj.get("schema") != 1 or not _is_int(obj.get("schema")):
        out.append("status `schema` must be 1")
    if obj.get("state") not in STATUS_STATES:
        out.append(f"status `state` must be one of {', '.join(STATUS_STATES)}")
    if not isinstance(obj.get("version"), str):
        out.append("status `version` must be a string")
    if not isinstance(obj.get("problems"), list) or not all(isinstance(p, str) for p in obj["problems"]):
        out.append("status `problems` must be a list of strings")
    units = obj.get("units", [])
    if not isinstance(units, list) or not all(isinstance(u, dict) and isinstance(u.get("name"), str)
                                              and isinstance(u.get("state"), str) for u in units):
        out.append("status `units` must be a list of {name, state}")
    if not isinstance(obj.get("detail", {}), dict):
        out.append("status `detail` must be an object")
    return out


def doctor_problems(obj, returncode):
    """Why `obj` and the exit code are not a conforming `doctor` answer (empty when they are)."""
    if not isinstance(obj, dict):
        return ["doctor did not print a JSON object"]
    out = []
    if obj.get("schema") != 1 or not _is_int(obj.get("schema")):
        out.append("doctor `schema` must be 1")
    results = obj.get("results")
    if not isinstance(results, list) or len(results) > 100:
        return out + ["doctor `results` must be a list of at most 100 results"]
    failed = False
    for i, r in enumerate(results):
        if not (isinstance(r, dict) and r.get("level") in DOCTOR_LEVELS and isinstance(r.get("check"), str)
                and isinstance(r.get("message"), str) and isinstance(r.get("fix_hint", ""), str)
                and set(r) <= {"level", "check", "message", "fix_hint"}
                and all(len(v) <= 400 for v in r.values())):
            out.append(f"doctor result {i} must be {{level: ok|warn|fail, check, message, fix_hint?}} with strings of at most 400 characters")
        else:
            failed = failed or r["level"] == "fail"
    if (returncode != 0) != failed:
        out.append("doctor must exit 0 unless a result is `fail`" if returncode else "doctor reported a `fail` but exited 0")
    return out


# ── the doctor slot (3.1, #294 PR 4) ─────────────────────────────────────────────────────────────────
# The core runs a contract-1 module's own `doctor` (an argv, no shell, 20 s, 64 KiB) and reads what it prints as text.
# Whatever the module does wrong becomes a `warn` that names it; the core's own checks never wait on it. Results are
# cached beside the hive (never journaled) so a dashboard shows them and their age without starting module code.

DOCTOR_MODES = ("manual", "install", "update", "boot", "timer")
DOCTOR_MAX_RESULTS, DOCTOR_MAX_STRING = 100, 400
DOCTOR_CACHE_DIR = ".module-doctor"
_DOCTOR_KEYS = {"level", "check", "message", "fix_hint"}


def clean_text(value, limit=DOCTOR_MAX_STRING):
    """Text from a module: control, format (bidi, zero-width) and line-break characters out, whitespace made plain,
    at most `limit` characters. Rendered as text, never as markup."""
    out = []
    for ch in str(value):
        cat = unicodedata.category(ch)
        if ch in "\t\n\r":
            out.append(" ")
        elif cat not in ("Cc", "Cf", "Zl", "Zp"):
            out.append(ch)
    return "".join(out)[:limit]


def _doctor_result(level, check, message, fix_hint=None):
    r = {"level": level, "check": clean_text(check), "message": clean_text(message)}
    if fix_hint:
        r["fix_hint"] = clean_text(fix_hint)
    return r


def doctor_results(rc, obj, why):
    """What the core makes of one doctor run: a list of {level, check, message, fix_hint?}. Never raises. A crash, a
    timeout, a missing command, an over-long or non-JSON answer, a wrong shape and a bad exit code are each a `warn`;
    a `fail` the module reported stays a `fail`."""
    if why:
        return [_doctor_result("warn", "doctor", f"doctor {why}" + (f" (exit {rc})" if rc else ""))]
    if not isinstance(obj, dict):
        return [_doctor_result("warn", "doctor", "doctor did not print a JSON object")]
    if obj.get("schema") != 1 or not _is_int(obj.get("schema")):
        return [_doctor_result("warn", "doctor", "doctor `schema` must be 1: its results were not read")]
    raw = obj.get("results")
    if not isinstance(raw, list):
        return [_doctor_result("warn", "doctor", "doctor `results` must be a list: its results were not read")]
    out = []
    for i, r in enumerate(raw[:DOCTOR_MAX_RESULTS]):
        if not (isinstance(r, dict) and r.get("level") in DOCTOR_LEVELS and isinstance(r.get("check"), str)
                and isinstance(r.get("message"), str) and isinstance(r.get("fix_hint", ""), str) and set(r) <= _DOCTOR_KEYS):
            out.append(_doctor_result("warn", "doctor", f"doctor result {i} is not {{level: ok|warn|fail, check, message, fix_hint?}}: ignored"))
        else:
            out.append(_doctor_result(r["level"], r["check"], r["message"], r.get("fix_hint")))
    if len(raw) > DOCTOR_MAX_RESULTS:
        out.append(_doctor_result("warn", "doctor", f"doctor printed {len(raw)} results: only the first {DOCTOR_MAX_RESULTS} are kept"))
    if rc != 0 and not any(r["level"] == "fail" for r in out):
        out.append(_doctor_result("warn", "doctor", f"doctor exited {rc}"))
    return out


def run_doctor(name, rec, manifest, key_dir, mode="manual"):
    """Run the module's `doctor` and return `doctor_results` for it ([] for a manifest without one). The caller has
    already established that the manifest is the pinned publisher's: this runs module code, with `narrow_env` and
    `HIVE_DOCTOR_MODE` (manual|install|update|boot|timer) and nothing else of the caller's."""
    if not manifest.get("doctor"):
        return []
    env = narrow_env(name, rec, key_dir, manifest, HIVE_DOCTOR_MODE=mode if mode in DOCTOR_MODES else "manual")
    rc, obj, why = _run_json(manifest["doctor"], manifest["files"], modules_dir() / name, env)
    return doctor_results(rc, obj, why)


def doctor_worst(results):
    return max((r["level"] for r in results), key=DOCTOR_LEVELS.index, default="ok")


def _cache_dir(home, create=False):
    d = Path(home) / DOCTOR_CACHE_DIR
    if create:
        try:
            d.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            return None
    try:
        st = os.lstat(d)
    except OSError:
        return None
    return d if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022 else None


def write_doctor_cache(home, name, mode, version, results, now=None):
    """Keep the latest results in `<home>/.module-doctor/<name>.json` (0600, a 0700 directory of the operator's: a
    link or a shared directory is not written). Returns whether it was written. Never raises."""
    if not vocabulary.valid_module_name(name):
        return False
    d = _cache_dir(home, create=True)
    if d is None:
        return False
    doc = {"schema": 1, "module": name, "version": clean_text(version, 64), "mode": mode, "at": int(time.time() if now is None else now),
           "worst": doctor_worst(results), "results": results}
    tmp = d / f".{name}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, sort_keys=True)
        os.replace(tmp, d / f"{name}.json")
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def read_doctor_cache(home, name):
    """The cached doctor document for `name`, or None (absent, unreadable, over 256 KiB or not shaped as written).
    Reads a file; runs nothing. Every string is cleaned again, whatever wrote the file."""
    d = _cache_dir(home)
    if d is None or not vocabulary.valid_module_name(name):
        return None
    try:
        path = d / f"{name}.json"
        if not stat.S_ISREG(os.lstat(path).st_mode) or os.lstat(path).st_size > 256 * 1024:
            return None
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not (isinstance(doc, dict) and doc.get("schema") == 1 and _is_int(doc.get("at")) and isinstance(doc.get("results"), list)):
        return None
    results = [_doctor_result(r["level"], r["check"], r["message"], r.get("fix_hint")) for r in doc["results"][:DOCTOR_MAX_RESULTS]
               if isinstance(r, dict) and r.get("level") in DOCTOR_LEVELS and isinstance(r.get("check"), str)
               and isinstance(r.get("message"), str) and isinstance(r.get("fix_hint", ""), str)]
    return {"module": name, "version": clean_text(doc.get("version", ""), 64), "mode": doc.get("mode") if doc.get("mode") in DOCTOR_MODES else "manual",
            "at": doc["at"], "worst": doctor_worst(results), "results": results}


def drop_doctor_cache(home, name):
    d = _cache_dir(home)
    if d is not None and vocabulary.valid_module_name(name):
        try:
            os.unlink(d / f"{name}.json")
        except OSError:
            pass


def doctor_after(lib, name, mode):
    """The end of `module add` and `update`: run the new module's doctor, cache and print what it says. It never rolls
    the install back. Returns the worst level ('ok' for a module without a doctor)."""
    rec = load_state(lib).get(name)
    try:
        manifest = installed_manifest(lib, name)
    except ModuleError:
        return "ok"
    if rec is None or not manifest.get("doctor"):
        return "ok"
    results = run_doctor(name, rec, manifest, Path(lib._ensure_key_dir()) / "modules" / name, mode)
    write_doctor_cache(lib.HIVE_HOME, name, mode, manifest["version"], results)
    for r in results:
        print(f"  doctor {r['level']}: {r['check']}: {r['message']}" + (f" ({r['fix_hint']})" if r.get("fix_hint") else ""))
    return doctor_worst(results)


def lint_identifying(root, manifest, deny=()):
    """Warnings (not a gate, not a claim): text in the manifest or a shipped file that looks like it names a person,
    host, account or repository. A placeholder (`<user>`, `{org}`, `$HOME`) is not flagged."""
    found = []
    deny = [d.lower() for d in deny if d]
    for rel in [MANIFEST, *sorted(manifest["files"])]:
        path = root / rel
        try:
            if path.stat().st_size > _TEXT_LIMIT:
                continue
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for what, rx, skip_code in _LINTS:
            if skip_code and path.suffix in _CODE_SUFFIXES:
                continue
            for hit in sorted({m.group(0) for m in rx.finditer(text)})[:3]:
                found.append(f"{rel}: looks like {what}: {hit}")
        low = text.lower()
        found += [f"{rel}: holds the denied term {d!r}" for d in deny if d in low]
    return found[:50]


def _run_capture(argv, files, mod_dir, env, timeout=None, merge_stderr=False):
    """Run one manifest argv in the throwaway module dir, no shell and no stdin; (returncode, output bytes or None,
    problem or None). The deadline is wall-clock and the group is killed at it; the output is capped."""
    timeout = CHECK_TIMEOUT if timeout is None else timeout
    first = argv[0]
    if first.startswith("/"):
        cmd = list(argv)
    elif "/" in first or first in files:
        cmd = [str(mod_dir / first), *argv[1:]]
    else:
        found = shutil.which(first, path=env["PATH"])
        if not found:
            return None, None, f"{first!r} is not on PATH"
        cmd = [found, *argv[1:]]
    try:
        proc = subprocess.Popen(cmd, cwd=str(mod_dir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return None, None, f"could not run: {e}"
    deadline, chunks, size, fd = time.monotonic() + timeout, [], 0, proc.stdout.fileno()
    timed_out = False
    try:
        while True:                         # a wall-clock deadline on the read: a child that keeps the pipe open must not extend it
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > CHECK_OUTPUT_CAP:
                _kill_group(proc)
                proc.wait()
                return None, None, f"printed more than {CHECK_OUTPUT_CAP} bytes"
        if not timed_out:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            _kill_group(proc)
            proc.wait()
            return None, None, f"did not finish in {timeout} seconds"
    finally:
        proc.stdout.close()
    return proc.returncode, b"".join(chunks), None


def _run_json(argv, files, mod_dir, env):
    """Run one manifest argv in the throwaway module dir; (returncode, parsed JSON or None, problem or None)."""
    rc, out, why = _run_capture(argv, files, mod_dir, env)
    if why:
        return None, None, why
    try:
        return rc, json.loads(out), None
    except ValueError:
        return rc, None, "did not print JSON"


CHECK_HELP_TIMEOUT, CHECK_HELP_TOTAL = 5, 60
IRREVERSIBLE_HELP = ("This cannot be undone.", "--yes", "--dry-run")


def help_warnings(m, mod_dir, env):
    """`module check --run`: run `<cli.command> <verb> --help` for each declared verb (5 s each, 60 s in all) and warn
    on a non-zero exit, on a mutating verb whose help does not name --dry-run, and on an irreversible verb whose help
    lacks the plain words. Warnings only: help text is provisional. Runs nothing but `--help`."""
    out, started, cmds = [], time.monotonic(), m["commands"]
    for i, (verb, e) in enumerate(cmds.items()):
        left = CHECK_HELP_TOTAL - (time.monotonic() - started)
        if left <= 0:
            out.append(f"probing the help of declared verbs stopped after {CHECK_HELP_TOTAL} s; not run: {', '.join(list(cmds)[i:])}")
            break
        rc, text, why = _run_capture([*m["cli"]["command"], *verb.split(), "--help"], m["files"], mod_dir, env,
                                     timeout=min(CHECK_HELP_TIMEOUT, left), merge_stderr=True)
        if why:
            out.append(f"`{verb} --help` {why}")
            continue
        if rc != 0:
            out.append(f"`{verb} --help` exited {rc}, not 0: every declared verb answers --help")
        text = text.decode("utf-8", "replace")
        if e.get("reversible") == "no":
            missing = [w for w in IRREVERSIBLE_HELP if w not in text]
            if missing:
                out.append(f"help of irreversible verb `{verb}` lacks: {', '.join(missing)}")
        elif e["mutates"] and "--dry-run" not in text:
            out.append(f"help of mutating verb `{verb}` does not name --dry-run")
    return out


def _kill_group(proc):
    """Signal the group while its leader is still unreaped (an exited, unreaped leader keeps the group id from being
    reused). Callers `wait()` only after this."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


NO_COMMANDS_WARNING = "manifest declares no commands (4.2): callers cannot tell which verbs change state or can be undone"


def check_module(root, core_version, run=False, deny=()):
    """Everything `module check` reports: (errors, warnings). Errors are what `install` would refuse or what a
    contract-1 module must answer; warnings are the publisher's to read. Validates through `validate_manifest`, the
    code `install` uses, and runs nothing unless `run`."""
    root = Path(root)
    errors, warnings = [], []
    try:
        raw = (root / MANIFEST).read_bytes()
    except OSError:
        return [f"no {MANIFEST} in {root}"], warnings
    try:
        m = validate_manifest(raw, core_version)
    except ModuleError as e:
        return [str(e)], warnings
    if (root / SIGNATURE).exists():
        try:
            sig = base64.b64decode("".join((root / SIGNATURE).read_text().split()), validate=True)
            if not ed25519.verify(raw, sig, base64.b64decode(m["publisher"])):
                errors.append("the manifest signature is not valid under its publisher key")
        except Exception:
            errors.append(f"{SIGNATURE} is not a valid signature file")
    else:
        warnings.append(f"no {SIGNATURE}: unsigned, so `module add` would refuse it")
    tracked = None
    if (root / ".git").exists():
        try:
            ls = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files", "-z"],
                                check=True, capture_output=True, timeout=30)
            tracked = [n.decode() for n in ls.stdout.split(b"\x00") if n]
        except (OSError, subprocess.SubprocessError):
            warnings.append("could not list the git-tracked files; checked every file on disk")
    try:
        _verify_files(root, m, tracked, ignore=("config",))
    except ModuleError as e:
        errors.append(str(e))
    if "contract" not in m:
        warnings.append("no `contract`: a 3.0-shaped manifest, which installs with none of the 3.1 slots")
    else:
        for field in ("status", "doctor"):
            if run and field not in m:
                warnings.append(f"no `{field}` command: nothing to run for it")
        if "doctor" not in m:
            warnings.append("no `doctor`: every contract-1 module should provide its own")
        if "cli" in m and "commands" not in m:
            warnings.append(NO_COMMANDS_WARNING)
        na = [k for k, e in (m.get("commands") or {}).items() if e["mutates"] and e["reversible"] == "n/a"]
        if na:
            warnings.append(f"verbs declared reversible n/a (nothing to restore; the core cannot check it, read them): {', '.join(na)}")
    warnings += lint_identifying(root, m, deny)
    if run and not errors and "contract" in m:
        with tempfile.TemporaryDirectory(prefix="hive-module-check-") as tmp:
            tmp = Path(tmp)
            mod_dir, keys, home = tmp / "module", tmp / "keys", tmp / "home"
            for d in (keys, home):
                d.mkdir(mode=0o700)
            for rel in [*m["files"], MANIFEST, SIGNATURE]:
                if (root / rel).is_file():
                    (mod_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / rel, mod_dir / rel)
                    os.chmod(mod_dir / rel, 0o755 if os.access(root / rel, os.X_OK) else 0o644)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
                   "HIVE_MODULE_NAME": m["name"], "HIVE_MODULE_DIR": str(mod_dir), "HIVE_MODULE_KEY_DIR": str(keys),
                   "HIVE_DOCTOR_MODE": "manual"}
            for field, judge in (("status", lambda rc, obj: ([] if rc == 0 else [f"status exited {rc}, not 0"]) + status_problems(obj)),
                                 ("doctor", lambda rc, obj: doctor_problems(obj, rc))):
                if field not in m:
                    continue
                rc, obj, why = _run_json(m[field], m["files"], mod_dir, env)
                errors += [f"{field} {why}"] if why else [f"{p}" for p in judge(rc, obj)]
            if m.get("commands") and m.get("cli"):
                warnings += help_warnings(m, mod_dir, env)
    return errors, warnings


def cmd_check(lib, args):
    errors, warnings = check_module(args.dir, lib.CONTRACT_VERSION, run=args.run, deny=args.deny or ())
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, indent=1))
    else:
        for w in warnings:
            print(f"warning: {w}")
        for e in errors:
            print(f"error: {e}")
        print(f"{args.dir}: {'FAILED' if errors else 'ok'} ({len(errors)} error(s), {len(warnings)} warning(s))")
    return 1 if errors else 0


CONFIG_ACTIONS = ("list", "get", "set", "unset", "validate", "schema")


def _install_flags(p):
    p.add_argument("--require-healthy", action="store_true", help="exit 1 when the module's doctor reports a fail after the install (the install is kept)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="a node config value of the module's config_schema (repeatable)")
    p.add_argument("--set-fleet", action="append", default=[], metavar="KEY=VALUE", help="a fleet config value, owner-signed after the install (repeatable)")
    p.add_argument("--set-env", action="append", default=[], metavar="KEY=VARIABLE", help="a node config value read from an environment variable (for a secret)")
    p.add_argument("--non-interactive", action="store_true", help="never prompt: a required setting that is not given refuses before anything is installed")


def build_parser():
    p = argparse.ArgumentParser(prog="hive-mind module", description="Install and manage modules (Linux, 2.1).")
    sub = p.add_subparsers(dest="verb", required=True)
    a = sub.add_parser("add", help="fetch, verify and install a module, mint its device key and admit it (without the owner key: stage it and print the owner's admit line)")
    a.add_argument("name")
    a.add_argument("--from", dest="source", help="the module's git repository (path or URL); required except with --abort")
    a.add_argument("--abort", action="store_true", help="drop a staged add that is waiting for the owner's admit")
    a.add_argument("--publisher", help="the publisher key you checked (base64); pins it without a prompt")
    a.add_argument("--principal", help="the operator's principal for the module's device (default: this device's)")
    a.add_argument("--device-id", help="the device id a `--plan --json` run minted (the executor passes it): use that key, mint none")
    a.add_argument("--plan", action="store_true", help="print the exact canonical governance bodies this would sign, and sign nothing (needs --json)")
    a.add_argument("--json", action="store_true", help="with --plan: print the plan as JSON")
    _install_flags(a)
    r = sub.add_parser("remove", help="revoke the module's device and delete it (its entries stay in the journal)")
    r.add_argument("name")
    r.add_argument("--purge", action="store_true", help="also delete the module's persistent state directory (kept by default)")
    r.add_argument("--plan", action="store_true", help="print the exact canonical governance bodies this would sign, and sign nothing (needs --json)")
    r.add_argument("--json", action="store_true", help="with --plan: print the plan as JSON")
    u = sub.add_parser("update", help="re-verify and swap atomically; the publisher key must match the pin")
    u.add_argument("name")
    u.add_argument("--from", dest="source", help="a different repository (default: where it came from)")
    u.add_argument("--publisher", help="ignored unless it equals the pinned key")
    _install_flags(u)
    i = sub.add_parser("install", help="converge on an installed module: install, update or nothing to do; stable exit codes (0 ok, 1 refused, 2 usage, 3 waiting for the owner, 4 unhealthy, 5 owner key locked)")
    i.add_argument("name")
    i.add_argument("--from", dest="source", help="the module's git repository (default: where it was installed from)")
    i.add_argument("--publisher", help="the publisher key you checked (base64); pins it without a prompt")
    i.add_argument("--principal", help="the operator's principal for the module's device (default: this device's)")
    i.add_argument("--json", action="store_true", help="print one JSON object; never prompts")
    _install_flags(i)
    t = sub.add_parser("trust", help="show a module's trust tier, or lower it on this node (confined); clearing the lowering needs the owner key")
    t.add_argument("name")
    t.add_argument("tier", nargs="?", choices=TRUST_TIERS)
    sub.add_parser("list", help="installed modules: version, publisher, signature, service, device, quota use").add_argument(
        "--json", action="store_true", help="print one JSON object (the same columns, plus the problem text and whether the module declares a config_schema)")
    sub.add_parser("reapply", help=argparse.SUPPRESS)           # internal: re-renders the units
    rf = sub.add_parser("refresh", help="update every installed module to the newest version this core admits, or disable one that has none (`hive-mind update` runs this)")
    rf.add_argument("--check", action="store_true", help="show what would be updated or disabled; change nothing")
    q = sub.add_parser("quota", help="the owner raises or lowers a module's limits")
    q.add_argument("name")
    for k in QUOTA_KEYS:
        q.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int)
    c = sub.add_parser("config", help="list, get, set, unset or validate a module's config (node values are local and kept "
                                      "by update; --fleet and --fleet-node are owner-signed)")
    c.add_argument("name")
    c.add_argument("action", choices=CONFIG_ACTIONS)
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.add_argument("--fleet", action="store_true", help="the fleet value: owner-signed, journaled, seen by every admitted device")
    c.add_argument("--fleet-node", metavar="DEVICE", help="the value for one device (k1:<16 hex>): owner-signed, journaled")
    c.add_argument("--merged", action="store_true", help="a module without a config_schema: list/get across the layers too")
    c.add_argument("--json", action="store_true", help="list, get and validate print JSON (a secret is masked); with --plan, the plan")
    c.add_argument("--plan", action="store_true", help="set --fleet|--fleet-node: print the exact canonical governance body this would sign, and sign nothing (needs --json)")
    c.add_argument("--stdin", action="store_true", help="set: read the value from standard input (the way to give a secret in a script)")
    c.add_argument("--file", metavar="PATH", help="set: read the value from a file (the way to give a secret in a script)")
    c.add_argument("--non-interactive", action="store_true", help="set: never ask for the owner passphrase and never read stdin to unlock: exit 5 when the unlock agent is absent, locked or would not sign the act")
    ru = sub.add_parser("run", help="run a module's cli command without its launcher: module run <name> [--] <args>")
    ru.add_argument("name")
    ru.add_argument("args", nargs="*", help="passed to the module's command unchanged")
    st = sub.add_parser("status", help="run a module's status command; exit 0 when it says ok")
    st.add_argument("name")
    st.add_argument("--json", action="store_true", help="print the module's status envelope as JSON")
    rt = sub.add_parser("runtime", help="tell the core which directory holds one of a module's runtimes (when it cannot find it)")
    rt.add_argument("name")
    rt.add_argument("program")
    rt.add_argument("dir", nargs="?")
    rt.add_argument("--unset", action="store_true", help="forget the override")
    cm = sub.add_parser("commands", help="list the verbs a module's signed manifest declares: which change state, which can be undone")
    cm.add_argument("name")
    cm.add_argument("--json", action="store_true", help="print one JSON object with every default filled in")
    k = sub.add_parser("check", help="check a module directory against the module contract (what `add` would refuse; for publishers and CI)")
    k.add_argument("dir")
    k.add_argument("--run", action="store_true", help="also run its `status` and `doctor` commands in a throwaway directory and check what they print")
    k.add_argument("--deny", action="append", metavar="TERM", help="warn on a file that holds TERM (a host, user or organisation name); repeatable")
    k.add_argument("--json", action="store_true", help="print {ok, errors, warnings} as JSON")
    return p


VERBS = {"add": cmd_add, "install": cmd_install, "remove": cmd_remove, "update": cmd_update, "list": cmd_list, "quota": cmd_quota,
         "reapply": cmd_reapply, "refresh": cmd_refresh, "config": cmd_config, "check": cmd_check, "run": cmd_run, "status": cmd_status,
         "runtime": cmd_runtime, "trust": cmd_trust, "commands": cmd_commands}


def main(lib, argv):
    """Run `hive-mind module <argv>` against the control-plane library `lib`. Returns an exit status."""
    if argv[:1] == ["run"]:                  # everything after the name belongs to the module's command, flags included
        if len(argv) < 2 or argv[1].startswith("-"):
            print("usage: hive-mind module run <name> [--] <args>", file=sys.stderr)
            return 2
        argv = ["run", argv[1], *argv[2:]] if argv[2:3] != ["--"] else ["run", argv[1], *argv[3:]]
        args = argparse.Namespace(verb="run", name=argv[1], args=argv[2:])
    else:
        args = build_parser().parse_args(argv)
    if args.verb == "config":
        takes_key = args.action in ("get", "set", "unset")
        extra = (args.key if not takes_key else args.value if args.action != "set" else None)
        if args.action != "set" and (args.stdin or args.file or args.non_interactive):
            print("hive-mind module: --stdin/--file/--non-interactive only apply to config set", file=sys.stderr)
            return 2
        if extra is not None or (takes_key and args.key is None):
            print(f"hive-mind module: config {args.action} takes the wrong arguments", file=sys.stderr)
            return 2
    plan = getattr(args, "plan", False)
    if plan and (not args.json or (args.verb == "config" and not (args.action == "set" and (args.fleet or args.fleet_node)))):
        print("hive-mind module: --plan needs --json, and on `config` applies to `set --fleet|--fleet-node`", file=sys.stderr)
        return 2
    if plan and not lib._owner_key_exists():
        print("hive-mind module: --plan needs the owner key: plan on the owner machine", file=sys.stderr)
        return 2
    if getattr(args, "device_id", None) and not lib._owner_key_exists():
        print("hive-mind module: --device-id replays a plan and needs the owner key", file=sys.stderr)
        return 2
    try:
        if plan:
            args.non_interactive = True          # the executor has no terminal; a plan never asks
            args._replay = []
            lib.plan_verb("module config set" if args.verb == "config" else f"module {args.verb}",
                                     lambda: VERBS[args.verb](lib, args), replay=lambda: args._replay)
            return 0
        rc = VERBS[args.verb](lib, args)
        return (rc or 0) if args.verb in ("check", "config", "run", "status", "add", "update", "install", "commands") else 0
    except OwnerLocked as e:
        print(f"hive-mind module: {e}", file=sys.stderr)
        return EXIT_LOCKED
    except getattr(lib, "OwnerPromptRefused", ()) as e:           # `--non-interactive` met a prompt (the agent refused after the gate passed)
        print(f"hive-mind module: {e}", file=sys.stderr)
        return EXIT_LOCKED
    except ModuleError as e:
        print(f"hive-mind module: {e}", file=sys.stderr)
        return 1
