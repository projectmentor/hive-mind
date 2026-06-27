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
