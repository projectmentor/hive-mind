"""2.0 refuses local ids (#59, #64), on every flag that takes a reference, on both planes and through both
adapters (Fable's ask on public #136, before 4a's code).

A local id (`118`, `d17`, `i5`) is a SQLite rowid, reassigned on every rebuild, so one read before a sync
could name a different row by the time it was used. 1.x accepted them with a warning; contract §7 promised
the break for the next MAJOR. Each case here passes a local id that WOULD have resolved, and asserts exit
1, the exact message, and a journal that did not move. `--resolves` gets its own case: it downgrades an
unresolvable target to a warning, and a naive refusal there would have written the fact without its link.
"""

import importlib
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
MESSAGE = "is a local id, which 2.0 no longer accepts"


def _run(home, entry, *argv):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home).parent / "stash"))
    return subprocess.run([sys.executable, str(PROJECT / entry), *argv], env=env, capture_output=True, text=True)


def _journal(home):
    return b"".join(p.read_bytes() for p in sorted((Path(home) / "journal").glob("*.jsonl")))


@pytest.fixture
def seeded(tmp_path):
    """An owner node with one fact (#1), one decision (d1), one idea (i1) and one entity, all of whose local
    ids exist, so a refusal can only be the refusal and never "does not resolve"."""
    home = tmp_path / "h"
    for entry, *argv in (("hivemind_ctl.py", "owner", "init"),
                         ("hv", "remember", "the build is red", "--source", "alice"),
                         ("hv", "decide", "ship on friday", "--rationale", "r"),
                         ("hv", "propose", "the build breaks on fridays", "--source", "bob"),
                         ("hv", "entity", "add", "--name", "Build", "--type", "concept")):
        r = _run(home, entry, *argv)
        assert r.returncode == 0, (argv, r.stdout, r.stderr)
    return home


# (entry, argv) for every ref-taking flag. Local ids as each flag would have read them in 1.x.
_CASES = [
    ("hv", ["remember", "x", "--resolves", "1", "--source", "alice"]),
    ("hv", ["remember", "x", "--outcome-of", "d1", "--source", "alice"]),
    ("hv", ["remember", "x", "--outcome-of", "1", "--source", "alice"]),
    ("hv", ["remember", "x", "--supports", "1", "--source", "carol"]),
    ("hv", ["remember", "x", "--supports", "i1", "--source", "carol"]),
    ("hv", ["remember", "x", "--contradicts", "1", "--source", "carol"]),
    ("hv", ["remember", "x", "--extends", "d1", "--source", "alice"]),
    ("hv", ["decide", "x", "--rationale", "r", "--supersedes", "1"]),
    ("hv", ["decide", "--revoke", "d1", "--rationale", "r"]),
    ("hv", ["decide", "x", "--rationale", "r", "--informed", "1"]),
    ("hv", ["decide", "x", "--rationale", "r", "--informed", "i1"]),
    ("hv", ["retract", "1", "--source", "bob"]),
    ("hv", ["entity", "link", "--name", "Build", "--fact-id", "1"]),
    # the control plane: the owner-signed link verbs, and the governance acts that take a fact
    ("hivemind_ctl.py", ["remember", "x", "--resolves", "1", "--source", "manual"]),
    ("hivemind_ctl.py", ["decide", "x", "--rationale", "r", "--supersedes", "d1", "--source", "manual"]),
    ("hivemind_ctl.py", ["decide", "x", "--rationale", "r", "--informed", "1", "--source", "manual"]),
    ("hivemind_ctl.py", ["entity", "link", "--name", "Build", "--fact-id", "1", "--source", "manual"]),
    ("hivemind_ctl.py", ["retract", "1", "--owner", "--reason", "r"]),
    ("hivemind_ctl.py", ["unforget", "1", "--reason", "r"]),
]


@pytest.mark.parametrize("entry, argv", _CASES, ids=[f"{'ctl' if e != 'hv' else 'hv'}:{' '.join(a)}" for e, a in _CASES])
def test_every_ref_taking_flag_refuses_a_local_id_and_writes_nothing(seeded, entry, argv):
    before = _journal(seeded)
    r = _run(seeded, entry, *argv)
    assert r.returncode == 1 and MESSAGE in r.stderr, (r.stdout, r.stderr)
    assert "Pass the sid (`h:…`) or ref (`node_id:seq`)" in r.stderr
    assert _journal(seeded) == before


def test_resolves_aborts_on_a_local_id_instead_of_writing_without_the_link(seeded):
    """The one flag where a naive refusal would have been silent: `--resolves` turns an UNRESOLVABLE target
    into "warn, then write the fact without the link". A local id must abort instead."""
    before = _journal(seeded)
    r = _run(seeded, "hv", "remember", "the build is green", "--resolves", "1", "--source", "alice")
    assert r.returncode == 1 and MESSAGE in r.stderr and "writing the fact without" not in r.stdout
    assert _journal(seeded) == before
    # an unresolvable sid is still the documented warning, and the fact is written
    r = _run(seeded, "hv", "remember", "the build is green", "--resolves", "h:0000000000", "--source", "alice")
    assert r.returncode == 0 and "writing the fact without a resolve link" in r.stdout


def _load_hermes(home, monkeypatch):
    """The Hermes plugin, with the stub `test_hermes_plugin.py` uses for the Hermes runtime (`agent`), loaded
    by path with HIVE_HOME set, since it runs `$HIVE_HOME/hv`."""
    agent = types.ModuleType("agent")
    provider = types.ModuleType("agent.memory_provider")
    provider.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = provider
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", provider)
    monkeypatch.setenv("HIVE_HOME", str(home))
    if not (Path(home) / "hv").exists():
        (Path(home) / "hv").symlink_to(PROJECT / "hv")
    loader = importlib.machinery.SourceFileLoader("hermes_refusal", str(PROJECT / "integrations" / "hermes" / "__init__.py"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("hermes_refusal", loader))
    loader.exec_module(mod)
    return mod


@pytest.mark.skipif(os.name == "nt", reason="symlink")
def test_hermes_refuses_a_local_id_through_the_real_hv(seeded, monkeypatch):
    hermes = _load_hermes(seeded, monkeypatch)
    monkeypatch.setattr(hermes, "_hv_telemetry", lambda *a, **k: None)
    p = hermes.HiveMindMemoryProvider()
    p.initialize("abcdef1234567890", agent_context="primary", agent_identity="coder")
    before = _journal(seeded)
    for tool, args in (("hive_remember", {"content": "x", "resolves": "1"}),
                       ("hive_remember", {"content": "x", "supports": "1"}),
                       ("hive_decide", {"content": "x", "informed_by": "1"}),
                       ("hive_decide", {"content": "x", "supersedes": "d1"})):
        out = p.handle_tool_call(tool, args)
        assert MESSAGE in out, (tool, out)
    assert _journal(seeded) == before


def _load_mcp(home, monkeypatch):
    """The MCP adapter, with a stub for the `mcp` SDK, which CI does not install: FastMCP's `tool()` just
    returns the function, so each tool runs for real against a real `hv`. The adapter runs
    `$HIVE_HOME/hv`, so the hive gets a link to this checkout's."""
    (Path(home) / "hv").symlink_to(PROJECT / "hv")
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn

        def run(self, *a, **k):
            pass

    fastmcp.FastMCP = FastMCP
    for name, mod in (("mcp", types.ModuleType("mcp")), ("mcp.server", types.ModuleType("mcp.server")),
                      ("mcp.server.fastmcp", fastmcp)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("HIVE_HOME", str(home))
    monkeypatch.syspath_prepend(str(PROJECT / "integrations" / "mcp"))
    monkeypatch.delitem(sys.modules, "hive_mcp", raising=False)
    return importlib.import_module("hive_mcp")


@pytest.mark.skipif(os.name == "nt", reason="symlink")
def test_mcp_refuses_a_local_id_through_the_real_hv(seeded, monkeypatch):
    mcp = _load_mcp(seeded, monkeypatch)
    before = _journal(seeded)
    calls = [lambda: mcp.hive_remember("x", resolves="1"),
             lambda: mcp.hive_remember("x", outcome_of="d1"),
             lambda: mcp.hive_remember("x", supports="1"),
             lambda: mcp.hive_decide("x", informed_by="1"),
             lambda: mcp.hive_decide("x", supersedes="d1"),
             lambda: mcp.hive_retract("1"),
             lambda: mcp.hive_entity("link", name="Build", fact_id="1")]
    for call in calls:
        with pytest.raises(RuntimeError) as e:
            call()
        assert MESSAGE in str(e.value)
    assert _journal(seeded) == before
