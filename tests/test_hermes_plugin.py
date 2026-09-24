from pathlib import Path
import importlib
import importlib.machinery
import importlib.util
import sys
import types


# integrations/hermes imports agent.memory_provider from the Hermes runtime, which
# is not present in this repo. Stub the minimal interface so we can test the plugin
# behavior in isolation.
agent_mod = types.ModuleType("agent")
memory_provider_mod = types.ModuleType("agent.memory_provider")


class MemoryProvider:
    pass


memory_provider_mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]
agent_mod.memory_provider = memory_provider_mod  # type: ignore[attr-defined]
sys.modules.setdefault("agent", agent_mod)
sys.modules.setdefault("agent.memory_provider", memory_provider_mod)

# Load the plugin from its new home under integrations/ (a plain folder, not an
# importable package — the "claude-code" sibling has a hyphen — so load by path).
_HERMES = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "__init__.py"
_loader = importlib.machinery.SourceFileLoader("hermes_plugin", str(_HERMES))
_spec = importlib.util.spec_from_loader("hermes_plugin", _loader)
hermes_plugin = importlib.util.module_from_spec(_spec)
_loader.exec_module(hermes_plugin)


def test_system_prompt_block_includes_session_start_nudge(monkeypatch, tmp_path):
    calls = []

    def fake_spec_update(agent):
        calls.append(("spec", agent))
        return "1.0"

    def fake_hv_nudge(event, *, session_id="", cwd="", agent="", text=""):
        calls.append(("nudge", event, session_id, cwd, agent, text))
        if event == "session-start":
            return "[hive] startup digest"
        return ""

    def fake_hv_search_json(query):
        calls.append(("search", query))
        return [{"content": "project fact", "confidence": 0.9, "source_agent": "alice", "tags": "observation"}]

    def fake_hv(*args, **kwargs):
        calls.append(("hv", args))
        if args and args[0] == "stats":
            return True, "stats-ok"
        return True, ""

    monkeypatch.setattr(hermes_plugin, "_spec_update_check", fake_spec_update)
    monkeypatch.setattr(hermes_plugin, "_hv_nudge", fake_hv_nudge)
    monkeypatch.setattr(hermes_plugin, "_hv_search_json", fake_hv_search_json)
    monkeypatch.setattr(hermes_plugin, "_hv", fake_hv)
    monkeypatch.setattr(hermes_plugin.HiveMindMemoryProvider, "is_available", lambda self: True)
    monkeypatch.chdir(tmp_path)

    provider = hermes_plugin.HiveMindMemoryProvider()
    provider.initialize("sess-123", agent_identity="default")

    block = provider.system_prompt_block()

    assert "## Hive Mind — Session Start" in block
    assert "[hive] startup digest" in block
    assert any(c[0] == "nudge" and c[1] == "session-start" for c in calls)
    assert any(c[0] == "search" for c in calls)
    assert any(c[0] == "hv" and c[1] == ("stats",) for c in calls)


# ── #76: tool parity with MCP, the single background writer, the decide environment ─────────────────

import ast        # noqa: E402
import json       # noqa: E402
import os         # noqa: E402
import threading  # noqa: E402
import time       # noqa: E402

_MCP = Path(__file__).resolve().parent.parent / "integrations" / "mcp" / "hive_mcp.py"
_TOOLS = ("hive_search", "hive_remember", "hive_decide", "hive_propose")


def _provider(monkeypatch, fake_hv):
    monkeypatch.setattr(hermes_plugin, "_hv", fake_hv)
    monkeypatch.setattr(hermes_plugin, "_hv_telemetry", lambda *a, **k: None)
    p = hermes_plugin.HiveMindMemoryProvider()
    p.initialize("abcdef1234567890", agent_context="primary", agent_identity="coder")
    return p


def test_tool_names_and_parameters_match_the_mcp_server():
    mcp = {f.name: [a.arg for a in f.args.args] for f in ast.parse(_MCP.read_text()).body
           if isinstance(f, ast.FunctionDef) and f.name in _TOOLS}
    schemas = {t["name"]: t for t in hermes_plugin.HiveMindMemoryProvider().get_tool_schemas()}
    assert set(schemas) == set(_TOOLS) == set(mcp)
    for name in _TOOLS:
        assert set(schemas[name]["parameters"]["properties"]) == set(mcp[name]), name


def test_write_tools_build_the_same_argv_as_the_cli(monkeypatch):
    calls = []
    p = _provider(monkeypatch, lambda *a, **k: (calls.append((a, k.get("env"))), (True, "ok"))[1])
    src = p._source_id
    out = json.loads(p.handle_tool_call("hive_remember", {"content": "ci is green", "tags": "proj",
                                                          "supports": "h:0123456789"}))
    assert out == {"ok": True, "output": "ok"}
    assert calls[-1][0] == ("remember", "ci is green", "--source", src, "--tags", "proj,observation",
                            "--supports", "h:0123456789")
    p.handle_tool_call("hive_remember", {"content": "it worked", "outcome_of": "h:aaaaaaaaaa", "polarity": -1,
                                         "channel": "introspect"})
    assert calls[-1][0][-6:] == ("--outcome-of", "h:aaaaaaaaaa", "--polarity", "-1", "--channel", "introspect")
    p.handle_tool_call("hive_decide", {"content": "cache it", "rationale": "r", "informed_by": "h:1111111111, h:2222222222"})
    assert calls[-1][0] == ("decide", "cache it", "--rationale", "r", "--informed", "h:1111111111", "h:2222222222")
    assert calls[-1][1] == {"HERMES_AGENT": src}                  # decide has no --source: it reads the env
    p.handle_tool_call("hive_propose", {"content": "maybe the cache", "tags": "proj"})
    assert calls[-1][0] == ("propose", "maybe the cache", "--source", src, "--tags", "proj")


def test_two_relationships_are_refused_before_any_hv_call(monkeypatch):
    calls = []
    p = _provider(monkeypatch, lambda *a, **k: (calls.append(a), (True, ""))[1])
    out = json.loads(p.handle_tool_call("hive_remember", {"content": "x", "supports": "h:0123456789",
                                                          "resolves": "h:9876543210"}))
    assert out["ok"] is False and "one relationship per write" in out["output"]
    assert calls == []


def test_search_passes_kind_and_filters_min_confidence(monkeypatch):
    seen = []

    def fake(*a, **k):
        seen.append(a)
        return True, json.dumps([{"content": "low", "confidence": 0.1}, {"content": "high", "confidence": 0.8}])
    p = _provider(monkeypatch, fake)
    out = json.loads(p.handle_tool_call("hive_search", {"query": "cache", "kind": "idea", "min_confidence": 0.5}))
    assert seen[-1] == ("search", "cache", "--format", "json", "--kind", "idea")
    assert [f["content"] for f in out["facts"]] == ["high"]


def test_decide_env_extends_the_environment(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw)

        class R:
            returncode, stdout, stderr = 0, "ok", ""
        return R()
    monkeypatch.setenv("HIVE_HOME", "/tmp/some-hive")
    monkeypatch.setattr(hermes_plugin.subprocess, "run", fake_run)
    hermes_plugin._hv("decide", "x", env={"HERMES_AGENT": "hermes:primary/coder/abcdef12"})
    env = captured["env"]
    assert env["HERMES_AGENT"] == "hermes:primary/coder/abcdef12"
    assert env["HIVE_HOME"] == "/tmp/some-hive" and "PATH" in env      # extended, not replaced


def test_the_mirror_returns_before_the_write_and_shutdown_drains_it(monkeypatch):
    gate, done = threading.Event(), []

    def slow_hv(*a, **k):
        if a and a[0] == "remember":
            gate.wait(5)
            done.append(a)
        return True, ""
    p = _provider(monkeypatch, slow_hv)
    monkeypatch.setattr(hermes_plugin, "_hv_nudge", lambda *a, **k: "")
    monkeypatch.setattr(hermes_plugin, "_hv_audit", lambda *a, **k: "")
    monkeypatch.setattr(p, "is_available", lambda: True)
    t = time.monotonic()
    p.on_memory_write("add", "memory", "the deploy script needs bash 5")
    assert time.monotonic() - t < 1.0 and done == []                   # returned before the write ran
    assert "the deploy script needs bash 5" in p._written_this_session  # marked at enqueue
    gate.set()
    t = time.monotonic()
    p.shutdown()
    assert done and time.monotonic() - t < 5.0                          # drained inside the budget


def test_the_queue_is_bounded(monkeypatch):
    gate, started = threading.Event(), threading.Event()

    def blocked_hv(*a, **k):
        started.set()                                  # the worker has taken a job and is now stuck in it
        gate.wait(10)
        return True, ""
    monkeypatch.setattr(hermes_plugin._HiveWriter, "MAXSIZE", 2)
    p = _provider(monkeypatch, blocked_hv)
    assert p._writer.submit(["remember", "f0"]) is True
    assert started.wait(5)                             # deterministic: f0 is off the queue, in the worker
    assert [p._writer.submit(["remember", f"f{i}"]) for i in (1, 2)] == [True, True]    # fills the queue
    assert p._writer.submit(["remember", "f3"]) is False                                # dropped, not piled up
    busy = p._writer.submit(["remember", "tool"], wait=True)
    assert busy[0] is False and "busy" in busy[1]
    gate.set()
    assert p._writer.drain(5.0) == 0


def test_a_session_switch_drains_the_old_sessions_writes_before_its_audit(monkeypatch):
    gate, order = threading.Event(), []

    def slow_hv(*a, **k):
        if a and a[0] == "remember":
            gate.wait(5)
            order.append("write")
        return True, ""
    p = _provider(monkeypatch, slow_hv)
    monkeypatch.setattr(p, "is_available", lambda: True)
    monkeypatch.setattr(hermes_plugin, "_hv_nudge", lambda *a, **k: "")
    monkeypatch.setattr(hermes_plugin, "_hv_audit", lambda *a, **k: order.append("audit") or "")
    p.on_memory_write("add", "memory", "the old session's last fact")
    threading.Timer(0.2, gate.set).start()                 # the write lands while the switch is draining
    p.on_session_switch("new-session-0000")
    assert order == ["write", "audit"]                        # the audit ran after the write landed


def test_tools_on_an_uninitialized_provider_return_an_error_envelope():
    p = hermes_plugin.HiveMindMemoryProvider()
    out = json.loads(p.handle_tool_call("hive_remember", {"content": "x"}))
    assert out == {"ok": False, "output": "the hive-mind provider is not initialized"}
