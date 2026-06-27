"""MCP <-> hv feature-parity guard.

The MCP server (integrations/mcp/hive_mcp.py) shells out to the `hv` CLI. It is easy for it to drift
behind hv as new commands land. This test asserts that every NON-SENSITIVE top-level hv subcommand is
exposed by an MCP tool, and that the sensitive owner/secret commands are NOT — without importing the
server (CI has no `mcp` dependency), so it parses both files statically.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HV_SRC = (ROOT / "hv").read_text()
MCP_SRC = (ROOT / "integrations" / "mcp" / "hive_mcp.py").read_text()

# Top-level hv subcommands only — the main parser's var is exactly `subparsers` (a \b before it
# excludes nested `sync_subparsers`/`tel_sub`/`config_sub`/... sub-subparsers).
HV_SUBCOMMANDS = set(re.findall(r'\bsubparsers\.add_parser\(\s*"([a-z][a-z-]*)"', HV_SRC))

# Commands intentionally kept OFF the MCP surface: owner/membership governance, encrypt-to-device
# secrets, device-key material, and local maintenance/wiring. These are owner/CLI-level by design.
CLI_ONLY = {"owner", "admit", "join", "config", "capsule", "key", "doctor", "wire"}
# `group` is exposed read-only (list) but its mutating actions are owner-only — handled separately.
PARTIAL = {"group"}

# hv subcommands the MCP server invokes. Tools build either `_run_hv(["cmd", ...])` inline or
# `args = ["cmd", ...]; _run_hv(args)`, so scrape the first string of EVERY list literal and keep
# only those that are real hv subcommands.
_LIST_HEADS = set(re.findall(r'\[\s*"([a-z][a-z-]*)"', MCP_SRC))
MCP_INVOKED = _LIST_HEADS & HV_SUBCOMMANDS


def test_every_non_sensitive_hv_command_is_exposed():
    expected = HV_SUBCOMMANDS - CLI_ONLY - PARTIAL
    missing = expected - MCP_INVOKED
    assert not missing, (
        f"MCP server is missing tools for non-sensitive hv commands: {sorted(missing)}. "
        f"Add an @mcp.tool() in integrations/mcp/hive_mcp.py, or (if intentionally CLI-only) "
        f"add it to CLI_ONLY in this test."
    )


def test_sensitive_commands_are_not_exposed():
    leaked = CLI_ONLY & MCP_INVOKED
    assert not leaked, (
        f"MCP server exposes owner/secret/maintenance commands that must stay CLI-only: {sorted(leaked)}."
    )


def test_group_is_read_only_over_mcp():
    # Only `group list` may be wrapped; admit/revoke/deny/change/purge must not appear.
    group_calls = re.findall(r'_run_hv\(\[\s*"group",\s*"([a-z]+)"', MCP_SRC)
    assert set(group_calls) <= {"list"}, f"Only `group list` may be exposed over MCP; found: {group_calls}"
