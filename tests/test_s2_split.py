"""The split (2.0 PR 2b, public #136): `hv` cannot owner-sign by any route (S2), and every command that moved
points at `hive-mind` and acts on nothing (S6).

S2 is enforced by two static checks, because either alone has a known hole:

* **the import graph** — `hv` never reaches `ownerkey` (nor the control plane, which imports it), directly
  or through anything it imports;
* **no `owner_sig` is produced** anywhere in that graph — which catches an INLINE signer, the mutant #152's
  tests missed, since inline code imports nothing.

Directive `h:157bd5e469`: a guard is not trusted on its docstring. So each checker is also run here against
MUTANTS of `hv` that violate the property, and must report them. A checker that passes on a mutant is the
failure this file exists to prevent.
"""

import ast
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import commandmap  # noqa: E402
import hivemind_ctl  # noqa: E402
import _keys  # noqa: E402  (where a hive's keys are, 2.0 PR 3a)

# Module names `hv` must never reach: the signer, and the control plane that imports it.
FORBIDDEN = {"ownerkey", "hivemind_owner", "hivemind_ctl"}


# ── the checkers ─────────────────────────────────────────────────────────────────────────────────────

def _imported_names(src):
    """Every module name a source imports — `import x`, `from x import y`, at any depth (a function body
    counts), plus the string forms of a dynamic load: a constant naming a forbidden module or its file."""
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.strip()
            stem = v[:-3] if v.endswith(".py") else v
            if stem in FORBIDDEN:                    # importlib.import_module("ownerkey"), ROOT / "ownerkey.py"
                names.add(stem)
    return names


def import_closure(entry, root):
    """The local modules reachable from `entry` (a path), following imports that resolve to `root/<name>.py`.
    Returns the set of module names reached, `entry` excluded."""
    seen, todo = set(), [Path(entry)]
    while todo:
        path = todo.pop()
        for name in _imported_names(path.read_text()):
            if name in seen:
                continue
            seen.add(name)
            f = Path(root) / f"{name}.py"
            if f.exists():
                todo.append(f)
    return seen


def owner_sig_writes(src):
    """Every place a source PRODUCES an `owner_sig`: a subscript store (`p["owner_sig"] = …`), a dict key
    (`{**p, "owner_sig": s}`), a keyword (`dict(p, owner_sig=s)`), or a `setdefault`/`update`/`__setitem__`
    call naming it. Reads (`p.get("owner_sig")`, `"owner_sig" in p`, `k != "owner_sig"`) are not writes."""
    hits = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(node.slice, ast.Constant) and node.slice.value == "owner_sig"):
            hits.append(("store", node.lineno))
        elif isinstance(node, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == "owner_sig" for k in node.keys):
                hits.append(("dict key", node.lineno))
        elif isinstance(node, ast.Call):
            if any(kw.arg == "owner_sig" for kw in node.keywords):
                hits.append(("keyword", node.lineno))
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in ("setdefault", "__setitem__")
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "owner_sig"):
                hits.append((f.attr, node.lineno))
    return hits


def _hv_graph_sources(hv_path, root):
    """`hv` plus every local module it reaches, as (name, source)."""
    out = [("hv", Path(hv_path).read_text())]
    for name in sorted(import_closure(hv_path, root)):
        f = Path(root) / f"{name}.py"
        if f.exists():
            out.append((name, f.read_text()))
    return out


# ── S2, check 1: the import graph ────────────────────────────────────────────────────────────────────

def test_hv_never_reaches_the_signer_or_the_control_plane():
    reached = import_closure(PROJECT / "hv", PROJECT)
    assert not (reached & FORBIDDEN), f"hv's import graph reaches {sorted(reached & FORBIDDEN)} (S2)"
    assert "merkle" in reached and "commandmap" in reached, "the walk must actually follow hv's imports"


def test_the_module_api_never_reaches_the_signer_or_the_control_plane():
    """2.1 A2/A3: `hive_module_api` is data plane; nothing it imports (the sync daemon, `hv` by path) reaches the owner key."""
    reached = import_closure(PROJECT / "hive_module_api.py", PROJECT)
    assert not (reached & FORBIDDEN), f"hive_module_api's import graph reaches {sorted(reached & FORBIDDEN)}"
    assert "hive_sync_daemon" in reached and "vocabulary" in reached, "the walk must actually follow its imports"
    assert not owner_sig_writes((PROJECT / "hive_module_api.py").read_text())


def _mutant(tmp_path, extra_hv="", helpers=None):
    """A copy of the project's modules with `extra_hv` appended to `hv` and optional helper modules."""
    root = tmp_path / "mutant"
    root.mkdir()
    for f in PROJECT.glob("*.py"):
        shutil.copy(f, root / f.name)
    (root / "hv").write_text((PROJECT / "hv").read_text() + "\n" + extra_hv + "\n")
    for name, src in (helpers or {}).items():
        (root / f"{name}.py").write_text(src)
    return root


def test_the_import_graph_check_fails_on_a_direct_import(tmp_path):
    root = _mutant(tmp_path, "import ownerkey  # mutant")
    assert "ownerkey" in import_closure(root / "hv", root)


def test_the_import_graph_check_fails_on_a_transitive_import(tmp_path):
    root = _mutant(tmp_path, "import innocent_helper  # mutant",
                   helpers={"innocent_helper": "import ownerkey\n"})
    reached = import_closure(root / "hv", root)
    assert "innocent_helper" in reached and "ownerkey" in reached


def test_the_import_graph_check_fails_on_a_dynamic_import(tmp_path):
    root = _mutant(tmp_path, "def _m():\n    import importlib\n    return importlib.import_module('ownerkey')")
    assert "ownerkey" in import_closure(root / "hv", root)


def test_the_import_graph_check_fails_on_importing_the_control_plane(tmp_path):
    root = _mutant(tmp_path, "import hivemind_ctl  # mutant")
    assert {"hivemind_ctl", "ownerkey"} <= import_closure(root / "hv", root)


# ── S2, check 2: no owner_sig produced ───────────────────────────────────────────────────────────────

def test_no_owner_sig_is_produced_anywhere_hv_reaches():
    for name, src in _hv_graph_sources(PROJECT / "hv", PROJECT):
        assert not owner_sig_writes(src), f"{name} produces an owner_sig: {owner_sig_writes(src)} (S2)"


def test_the_signer_really_is_where_the_check_says_it_is():
    """Not vacuous on real code: the one module that produces an owner signature is found doing it."""
    assert owner_sig_writes((PROJECT / "ownerkey.py").read_text())


INLINE_SIGNERS = {
    "subscript store": 'def _sneaky(p, seed):\n    p = dict(p)\n    p["owner_sig"] = base64.b64encode('
                       '_ed25519.sign(b"x", seed)).decode()\n    return p',
    "dict literal": 'def _sneaky(p, s):\n    return {**p, "owner_sig": s}',
    "keyword": 'def _sneaky(p, s):\n    return dict(p, owner_sig=s)',
    "setdefault": 'def _sneaky(p, s):\n    p.setdefault("owner_sig", s)\n    return p',
}


@pytest.mark.parametrize("shape", sorted(INLINE_SIGNERS))
def test_the_owner_sig_check_fails_on_an_inline_signer(tmp_path, shape):
    """The mutant the import-graph check cannot catch: signing inline imports nothing."""
    root = _mutant(tmp_path, INLINE_SIGNERS[shape])
    assert import_closure(root / "hv", root).isdisjoint(FORBIDDEN), "the mutant must pass check 1"
    assert owner_sig_writes((root / "hv").read_text()), f"inline signer ({shape}) not caught"


def test_the_owner_sig_check_ignores_reads():
    src = ('def f(p):\n    a = p.get("owner_sig")\n    b = "owner_sig" in p\n'
           '    c = {k: v for k, v in p.items() if k != "owner_sig"}\n    return a, b, c')
    assert owner_sig_writes(src) == []


# ── S2 at runtime ────────────────────────────────────────────────────────────────────────────────────

def _env(home, **extra):
    return dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"),
                HIVE_OWNER_PASSPHRASE="testpass", **extra)


def _hv(home, *argv, **extra):
    return subprocess.run([sys.executable, str(PROJECT / "hv"), *argv],
                          env=_env(home, **extra), capture_output=True, text=True)


def _ctl(home, *argv, **extra):
    return subprocess.run([sys.executable, str(PROJECT / "hivemind_ctl.py"), *argv],
                          env=_env(home, **extra), capture_output=True, text=True)


def _loaded_modules(home, *argv, script="hv"):
    r = subprocess.run([sys.executable, "-X", "importtime", str(PROJECT / script), *argv],
                       env=_env(home), capture_output=True, text=True)
    return {line.split("|")[-1].strip().split(".")[0] for line in r.stderr.splitlines() if "|" in line}


@pytest.mark.parametrize("argv", [["version"], ["whoami"], ["owner", "show"], ["search", "x"],
                                  ["owner", "init"]])
def test_an_hv_process_never_loads_the_signer(tmp_path, argv):
    """Including a moved command: pointing at `hive-mind` must not load what `hive-mind` would use."""
    mods = _loaded_modules(tmp_path / "h", *argv)
    assert mods, "importtime produced no module list; the check would be vacuous"
    assert not (mods & FORBIDDEN), f"`hv {' '.join(argv)}` loaded {sorted(mods & FORBIDDEN)}"


def test_a_control_plane_process_does_load_the_signer(tmp_path):
    """The positive half, so the runtime check is not vacuous: the control plane reaches `ownerkey`."""
    mods = _loaded_modules(tmp_path / "h", "owner", "init", script="hivemind_ctl.py")
    assert "ownerkey" in mods


# ── S6: every moved command points, exits 2, acts on nothing ─────────────────────────────────────────

def _journal_digest(home):
    h = hashlib.sha256()
    for f in sorted((Path(home) / "journal").glob("**/*")):
        if f.is_file():
            h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def owned_hive(tmp_path_factory):
    home = tmp_path_factory.mktemp("owned") / "h"
    r = _ctl(home, "owner", "init")
    assert r.returncode == 0, r.stdout + r.stderr
    return home


@pytest.mark.parametrize("key", sorted(commandmap.MOVED), ids=lambda k: " ".join(k))
def test_every_moved_command_points_and_acts_on_nothing(owned_hive, key):
    before = _journal_digest(owned_hive)
    r = _hv(owned_hive, *key)                          # no arguments at all: pointed at before parsing
    new_form = commandmap.MOVED[key][0]
    assert r.returncode == commandmap.POINTER_EXIT == 2, r.stdout + r.stderr
    assert f"Run: hive-mind {new_form}" in r.stderr
    assert _journal_digest(owned_hive) == before, "a pointer must act on nothing"


def test_a_pointer_carries_the_arguments_as_typed(owned_hive):
    r = _hv(owned_hive, "owner", "revoke-escrow", "all")
    assert r.returncode == 2 and "Run: hive-mind owner revoke all" in r.stderr     # the S5 rename
    r = _hv(owned_hive, "config", "quorum", "set", "quorum_m", "2")
    assert r.returncode == 2 and "Run: hive-mind config set quorum_m 2" in r.stderr  # the collapse
    r = _hv(owned_hive, "admit", "k1:0000000000000000", "--principal", "p")
    assert "Run: hive-mind group admit k1:0000000000000000 --principal p" in r.stderr


@pytest.mark.parametrize("argv, target", [
    (["retract", "h:0000000000", "--owner"], "hive-mind retract h:0000000000 --owner"),
    (["owner", "propose-election", "--mint"], "hive-mind owner mint"),
])
def test_the_flag_conditional_moves_point(owned_hive, argv, target):
    before = _journal_digest(owned_hive)
    r = _hv(owned_hive, *argv)
    assert r.returncode == 2 and f"Run: {target}" in r.stderr, r.stderr
    assert _journal_digest(owned_hive) == before


def _parser_paths_and_flags():
    import argparse
    paths = {}

    def walk(parser, prefix=()):
        flags = {s for a in parser._actions for s in a.option_strings}
        paths[prefix] = flags
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, prefix + (name,))

    walk(hivemind_ctl.hv().build_parser())
    return paths


@pytest.mark.parametrize("key", sorted(commandmap.FLAG_CONDITIONAL))
def test_every_flag_conditional_target_resolves_against_the_real_parser(key):
    """The verifier's ask (5851085843): `--mint` -> `owner mint` must name a command that exists, and a
    flag-conditional target's flag must be a real flag of that command, not a string in a table."""
    new_form = commandmap.FLAG_CONDITIONAL[key][0].split()
    words = tuple(w for w in new_form if not w.startswith("-"))
    flags = [w for w in new_form if w.startswith("-")]
    paths = _parser_paths_and_flags()
    assert words in paths, f"{key!r} points at `hive-mind {' '.join(new_form)}`, which the parser lacks"
    for f in flags:
        assert f in paths[words], f"`{' '.join(words)}` has no {f} flag"


def test_owner_mint_is_implemented_on_the_control_plane(tmp_path):
    home = tmp_path / "h"
    r = _ctl(home, "owner", "mint")
    assert r.returncode == 0 and "Minted a prospective owner key" in r.stdout, r.stdout + r.stderr
    assert "hv owner propose-election --pub" in r.stdout
    again = _ctl(home, "owner", "mint")
    assert "already exists" in again.stdout, "mint must not silently replace a key already here"


# ── key presence, without reading the seed ───────────────────────────────────────────────────────────

def _lib(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hv_s2_presence", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hv_s2_presence", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_presence_is_answered_without_reading_the_seed(tmp_path, monkeypatch):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    m = _lib(home, monkeypatch)
    gov = m._governance_state(m.merkle.read_all_entries(m.JOURNAL_DIR))
    real_read_text, real_read_bytes, real_open = Path.read_text, Path.read_bytes, open

    def guard(p):
        if Path(p).resolve() == m.OWNER_KEY_PATH.resolve():
            raise AssertionError("hv read the owner seed")

    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (guard(self), real_read_text(self, *a, **k))[1])
    monkeypatch.setattr(Path, "read_bytes", lambda self: (guard(self), real_read_bytes(self))[1])
    import builtins
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (guard(f), real_open(f, *a, **k))[1])
    assert m._owner_key_state(gov) == "held"
    assert m._self_membership(gov)[0] == "owner"


def test_presence_has_four_honest_answers(tmp_path, monkeypatch):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    m = _lib(home, monkeypatch)
    gov = m._governance_state(m.merkle.read_all_entries(m.JOURNAL_DIR))
    assert m._owner_key_state(gov) == "held"
    m.OWNER_PUB_PATH.unlink()
    assert m._owner_key_state(gov) == "present", "no recorded pub: hv cannot say whose key it is"
    m.OWNER_SEALED_PATH.unlink()
    assert m._owner_key_state(gov) == "absent"

    class Denied:
        def stat(self):
            raise PermissionError("key directory not searchable")
    for which in ("OWNER_SEALED_PATH", "OWNER_KEY_PATH"):
        with monkeypatch.context() as mp:
            mp.setattr(m, which, Denied())
            assert m._owner_key_state(gov) == "unknown", f"permission denied on {which} is not 'absent'"


# ── links: device-signed through hv, owner-signed through hive-mind (decision h:34cc1dbcd3) ──────────

def test_link_signing_follows_the_plane(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    d1 = _hv(home, "decide", "first", "--source", "manual")
    sid = next(w for w in d1.stdout.split() if w.startswith("h:"))
    via_hv = _hv(home, "decide", "second", "--supersedes", sid, "--source", "manual")
    via_ctl = _ctl(home, "decide", "third", "--supersedes", sid, "--source", "manual")
    via_ctl_agent = _ctl(home, "decide", "fourth", "--supersedes", sid, "--source", "claude-code")
    assert "(device-signed: source manual)" in via_hv.stdout, via_hv.stdout + via_hv.stderr
    assert "(owner-signed: source manual)" in via_ctl.stdout, via_ctl.stdout + via_ctl.stderr
    assert "(device-signed: source claude-code)" in via_ctl_agent.stdout, "#114: agents never borrow owner authority"


# ── owner-policy content writes ──────────────────────────────────────────────────────────────────────

def test_an_owner_policy_capsule_write_points_and_a_fertile_one_does_not(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0          # capsule_putters defaults to `owner`
    before = _journal_digest(home)
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "capsule", "put", "k", "--stdin"],
                       input="s3cret", env=_env(home), capture_output=True, text=True)
    assert r.returncode == 2 and "Run: hive-mind capsule put k --stdin" in r.stderr, r.stdout + r.stderr
    assert _journal_digest(home) == before
    assert _ctl(home, "config", "set", "capsule_putters", "fertile").returncode == 0
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "capsule", "put", "k", "--stdin"],
                       input="s3cret", env=_env(home), capture_output=True, text=True)
    assert r.returncode != 2 and "moved to the control plane" not in r.stderr, r.stderr


def test_an_owner_policy_cell_publish_points(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0          # cell_writers defaults to `owner`
    cell = tmp_path / "cell.json"
    cell.write_text(json.dumps({"name": "demo", "kind": "tool", "version": 1, "spec": {}}))
    r = _hv(home, "wire", "--add", str(cell))
    assert r.returncode == 2 and "Run: hive-mind wire --add" in r.stderr, r.stdout + r.stderr


# ── doctor --fix, split by what it touches ───────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_doctor_fix_repairs_the_device_key_on_hv_and_the_owner_key_on_hive_mind(tmp_path):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    owner_key = _keys.key_path(home, "owner-key.sealed")
    os.chmod(owner_key, 0o644)
    r = _hv(home, "doctor", "--fix")
    assert r.returncode != 2, "the 15-minute timer runs `hv doctor --fix`; it must not become a pointer"
    assert stat.S_IMODE(owner_key.stat().st_mode) == 0o644, "hv must not touch the owner key"
    assert "hive-mind doctor --fix" in r.stdout
    _ctl(home, "doctor", "--fix")
    assert stat.S_IMODE(owner_key.stat().st_mode) == 0o600


# ── placeholders: every one is replaced on the control plane ─────────────────────────────────────────

def _placeholders(src):
    """Functions whose body raises NotOnDataPlane: `hv`'s refusals for an owner step."""
    out = set()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call)
                        and getattr(sub.exc.func, "id", None) == "NotOnDataPlane"):
                    out.add(node.name)
    return out


def test_every_placeholder_is_replaced_on_the_control_plane():
    hv_placeholders = _placeholders((PROJECT / "hv").read_text())
    owner_defs = {n.name for n in ast.parse((PROJECT / "hivemind_owner.py").read_text()).body
                  if isinstance(n, ast.FunctionDef)}
    # owner_cmd raises for its moved actions but keeps its data-plane ones; the control plane wraps it.
    expected = hv_placeholders
    assert {"_append_governance", "_owner_forget", "unforget", "admit_cmd", "_group_change", "_config_set",
            "_holds_owner_key", "owner_cmd"} <= expected, "the placeholder set shrank: check the split"
    missing = expected - owner_defs
    assert not missing, f"hv placeholders with no control-plane replacement: {sorted(missing)}"


def test_install_replaces_the_placeholders_in_process(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path / "h"))
    loader = importlib.machinery.SourceFileLoader("hv_s2_install", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hv_s2_install", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    with pytest.raises(m.NotOnDataPlane):
        m._append_governance({"action": "heartbeat"})
    before = m._append_governance
    hivemind_ctl.install(m)
    assert m._append_governance is not before and m._CONTROL_PLANE
    assert hivemind_ctl.install(m) is m, "idempotent"


# ── the real `hive-mind` command, end to end ─────────────────────────────────────────────────────────

def test_the_dispatcher_runs_the_moved_verbs_end_to_end(tmp_path):
    home = tmp_path / "h"
    dispatcher = PROJECT / "scripts" / "installer" / "dispatcher.sh"
    env = _env(home)

    def hm(*argv):
        return subprocess.run(["bash", str(dispatcher), *argv], env=env, capture_output=True, text=True)

    r = hm("owner", "init")
    assert r.returncode == 0 and "Owner established:" in r.stdout, r.stdout + r.stderr
    assert "this device holds the owner key" in _hv(home, "owner", "show").stdout
    r = hm("config", "set", "cap_self", "0.6")
    assert r.returncode == 0 and "set cap_self = 0.6" in r.stdout, r.stdout + r.stderr
    r = hm("owner", "heartbeat")
    assert r.returncode == 0 and "Heartbeat recorded" in r.stdout, r.stdout + r.stderr
    fact = _hv(home, "remember", "a fact to forget", "--source", "manual").stdout
    fid = next(w for w in fact.split() if w.startswith("h:"))
    r = hm("retract", fid, "--owner")
    assert r.returncode == 0 and "FORGOTTEN (owner)" in r.stdout, r.stdout + r.stderr
    r = hm("unforget", fid, "--reason", "test")
    assert r.returncode == 0 and "UNFORGOTTEN (owner)" in r.stdout, r.stdout + r.stderr
    r = hm("owner", "show")
    assert r.returncode == 2 and "hv owner show" in r.stderr, "a data-plane read is refused with its hv form"


# ── adapters never reach the control plane (S9, and the federation negative in 5848736351) ───────────

ADAPTERS = [PROJECT / "integrations" / "mcp" / "hive_mcp.py", PROJECT / "integrations" / "hermes" / "__init__.py"]


def _names_the_control_plane(src):
    """Where a source would INVOKE the control plane: an argv list whose program is `hive-mind` (or its
    dispatcher or module), or any constant naming the control-plane files. The bare word "hive-mind" is
    also a directory and a product name (Hermes' provider is called that), so on its own it is not a hit."""
    hits = []
    files = ("hivemind_ctl.py", "hivemind_ctl", "hivemind_owner.py", "hivemind_owner", "dispatcher.sh")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.strip()
            if v in files or v.endswith(tuple("/" + f for f in files)):
                hits.append((v, node.lineno))
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            head = node.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and \
                    (head.value == "hive-mind" or head.value.endswith("/hive-mind")):
                hits.append((head.value, node.lineno))
    return hits


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda p: p.parent.name)
def test_no_adapter_reaches_the_control_plane_or_an_owner_signature(adapter):
    src = adapter.read_text()
    assert not (import_closure(adapter, PROJECT) & FORBIDDEN), "an adapter imports the signer or control plane"
    assert not _names_the_control_plane(src), f"an adapter invokes the control plane: {_names_the_control_plane(src)}"
    assert not owner_sig_writes(src), "an adapter produces an owner_sig"


def test_the_adapter_check_fails_on_an_adapter_that_shells_out_to_hive_mind():
    mutant = 'import subprocess\ndef hive_owner_init():\n    return subprocess.run(["hive-mind", "owner", "init"])'
    assert _names_the_control_plane(mutant)
