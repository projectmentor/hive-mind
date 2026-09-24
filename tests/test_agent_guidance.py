"""The write guidance agents read at the moment they write must keep the grounding rule (#99).

Since 1.21 (#73) an `introspect` fact weighs `introspect_support_weight` (default 0), but only if the
writer labels it: an absent channel is `sense`. So the three texts an agent reads while writing (the
Claude Code skill, the MCP server instructions, the Hermes context block) must route conclusions,
analysis and plans to `introspect`, and must not let tags pass for the channel. This pins both, so a
rewrite of any of them can't silently drop the rule. It reads the files statically, like
test_mcp_parity, so CI needs neither `mcp` nor a Hermes runtime.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _surfaces():
    skill = (ROOT / "integrations" / "claude-code" / "hive-memory" / "SKILL.md").read_text()
    mcp = re.search(r'INSTRUCTIONS = """(.*?)"""', (ROOT / "integrations" / "mcp" / "hive_mcp.py").read_text(), re.S)
    hermes = re.search(r"## Hive Mind — Shared Corpus(.*?)if session_start_hint",
                       (ROOT / "integrations" / "hermes" / "__init__.py").read_text(), re.S)
    assert mcp and hermes, "a guidance block moved: update this test to find it"
    return {"skill": skill, "mcp": mcp.group(1), "hermes": hermes.group(1)}


SURFACES = _surfaces()


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_conclusions_analysis_and_plans_go_to_the_introspect_channel(name):
    text = SURFACES[name]
    near = [text[max(0, m.start() - 160):m.end() + 160] for m in re.finditer("introspect", text)]
    assert any(re.search(r"(?i)\b(conclusions?|analysis|plans?)\b", w) for w in near), (
        f"{name}: nothing routes conclusions/analysis/plans to channel introspect")


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_tags_are_not_passed_off_as_the_channel(name):
    assert re.search(r"never change how much (it|a fact) counts as evidence", SURFACES[name]), (
        f"{name}: must say tags never change how much a fact counts as evidence")
