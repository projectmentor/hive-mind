"""B10 (hive-mind-private #58): no write path on the daemon or dashboard side can receive the owner passphrase.

The design is "the daemon proposes, the CLI signs": the owner passphrase is read only by `hive-mind` on a terminal
(or handed to its own `owner unlock` prompt), never by the sync daemon, the module API or the dashboard, and
every child process those start is given an environment without it. Three things are asserted:

  * the daemon-side source never names the passphrase variables or the owner-key reader;
  * the child environments built for cells, the dashboard's `hv` status reads and the owner agent drop both
    passphrase variables even when the parent holds them (the #295 agent and the #399 `_child_env` drop);
  * module processes are launched with the same scrub list.
"""
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

PASSPHRASES = {"HIVE_OWNER_KEY_PASSPHRASE": "key-pass-1", "HIVE_OWNER_PASSPHRASE": "export-pass-2"}
DAEMON_SIDE = ["hive_sync_daemon.py", "hive_module_api.py", "hive_module_client.py", "sync_client.py", "sync_common.py"]


def _hv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader(f"hvmod_pass_{os.urandom(4).hex()}", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _code_text(path):
    """The names, imports and string literals of a module, without its docstrings and comments: what the code
    can reach, not what its prose says it does not."""
    import ast
    tree = ast.parse(path.read_text())
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
        elif isinstance(n, ast.alias):
            out.append(n.name)
        elif isinstance(n, ast.ImportFrom):
            out.append(n.module or "")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.append(n.value)
    return "\n".join(out)


@pytest.mark.parametrize("name", DAEMON_SIDE)
def test_daemon_side_source_never_reads_the_owner_passphrase_or_key(name):
    src = _code_text(PROJECT / name)
    for needle in ("PASSPHRASE", "read_passphrase", "ownerkey", "load_seed", "_owner_signer", "owner_sig"):
        if name == "hive_sync_daemon.py" and needle == "PASSPHRASE":
            continue                       # the one allowed mention is hv.OWNER_PASSPHRASE_ENVS, asserted below
        assert needle not in src, f"{name} mentions {needle}"
    if name == "hive_sync_daemon.py":
        assert src.count("PASSPHRASE") == 1 and "OWNER_PASSPHRASE_ENVS" in src


def test_a_cells_shell_never_gets_either_passphrase(tmp_path, monkeypatch):
    hv = _hv(tmp_path, monkeypatch)
    for k, v in PASSPHRASES.items():
        monkeypatch.setenv(k, v)
    env = hv._child_env({"MYTOK": "tok"})
    assert not set(PASSPHRASES) & set(env) and env["MYTOK"] == "tok"


def test_the_dashboards_hv_status_reads_never_get_either_passphrase(tmp_path, monkeypatch):
    import hive_sync_daemon as d
    hv = _hv(tmp_path, monkeypatch)
    monkeypatch.setattr(d, "hv", hv)
    for k, v in PASSPHRASES.items():
        monkeypatch.setenv(k, v)
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw["env"])
        raise RuntimeError("stop")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    d._run_hv_text(["status"], 5)
    assert seen and not set(PASSPHRASES) & set(seen) and "PATH" in seen


def test_the_owner_agent_is_started_without_either_passphrase(monkeypatch):
    import subprocess
    import owner_agent
    for k, v in PASSPHRASES.items():
        monkeypatch.setenv(k, v)
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw["env"])
        raise RuntimeError("stop")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        owner_agent.spawn(b"\0" * 32, "/nonexistent/sock", 60, "pub", [])
    assert seen and not set(PASSPHRASES) & set(seen)


def test_module_processes_scrub_the_same_variables():
    import hive_modules
    assert set(PASSPHRASES) <= set(hive_modules.SCRUBBED_ENV)
