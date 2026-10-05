"""Ring 1 doc-sync floor: the docs must not drift from the code's CLI surface.

Two deterministic guarantees, run offline in CI (no network, no temp HIVE_HOME needed):

  1. Flag coverage — every flag that `hv` (argparse) and the `hive-mind` dispatcher accept
     must be documented in docs/CLI_REFERENCE.md or docs/AGENT_INTEGRATION.md. This is the
     check that would have caught `hive-mind uninstall --yes` being undocumented.
  2. Contract-version consistency — CONTRACT_VERSION (in `hv`) must equal the
     `Contract-Version:` header in AGENT_INTEGRATION.md, and that version must have a
     changelog entry. This is the drift that left the header at 1.2 while hv reported 1.4.

These are the high-precision corroborator under the hive doc-debt loop: what is mechanically
checkable in-repo, CI enforces; prose/external/cross-repo doc-debt the hive carries.
"""

import argparse
import importlib.util
import re
from importlib.machinery import SourceFileLoader
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DOCS = PROJECT / "docs"

# Intentionally undocumented in the user/agent docs: local-only `hv telemetry report|list`
# observability flags, surfaced via `hv telemetry ... --help` only. They never touch the
# corpus and are not part of the agent contract. Document one → remove it from here.
ALLOWLIST = {"--since", "--by", "--limit", "--transcript"}


def _load_hv():
    """Import the extensionless `hv` script as a module (it is __main__-guarded, no side effects)."""
    loader = SourceFileLoader("hv_cli", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hv_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _hv_flags():
    """Every option string across the whole hv parser tree (minus auto-added -h/--help)."""
    flags = set()

    def walk(parser):
        for action in parser._actions:
            flags.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    walk(sub)

    walk(_load_hv().build_parser())
    return flags - {"-h", "--help"}


def _dispatcher_flags():
    """Flags the `hive-mind` dispatcher subcommands parse, read from their case arms.

    Matches lines like `--keep-hive)` or `-y|--yes)` at the start of a line; ignores command
    invocations in script bodies (e.g. `python3 --version`) which are never line-leading arms.
    """
    flags = set()
    arm = re.compile(r"^\s*((?:-[a-z]\|)?--[a-z][a-z-]+|-[a-z])\)")
    for sh in sorted((PROJECT / "scripts" / "installer").glob("*.sh")):
        for line in sh.read_text().splitlines():
            m = arm.match(line)
            if m:
                flags.update(m.group(1).split("|"))
    return flags - {"-h", "--help"}


def test_every_cli_flag_is_documented():
    docs = (DOCS / "CLI_REFERENCE.md").read_text() + "\n" + (DOCS / "AGENT_INTEGRATION.md").read_text()
    flags = (_hv_flags() | _dispatcher_flags()) - ALLOWLIST
    missing = sorted(f for f in flags if f not in docs)
    assert not missing, (
        "Undocumented CLI flag(s): " + ", ".join(missing) + ". "
        "Document each in docs/CLI_REFERENCE.md or docs/AGENT_INTEGRATION.md, "
        "or add it to ALLOWLIST in this test with a justification."
    )


def test_contract_version_matches_spec():
    hv = _load_hv()
    spec = (DOCS / "AGENT_INTEGRATION.md").read_text()

    m = re.search(r"`Contract-Version:\s*([0-9]+\.[0-9]+)`", spec)
    assert m, "Contract-Version header not found in docs/AGENT_INTEGRATION.md"
    header = m.group(1)
    assert header == hv.CONTRACT_VERSION, (
        f"Contract-Version drift: hv CONTRACT_VERSION={hv.CONTRACT_VERSION} but "
        f"AGENT_INTEGRATION.md header={header}. Bump both together."
    )

    assert re.search(rf"^- `{re.escape(hv.CONTRACT_VERSION)}`", spec, re.M), (
        f"No changelog entry for `{hv.CONTRACT_VERSION}` in AGENT_INTEGRATION.md §7."
    )


def _history_head(text):
    """The newest version in docs/CONTRACT_HISTORY.md: its first `## X.Y` heading."""
    m = re.search(r"^## ([0-9]+\.[0-9]+)\s*$", text, re.M)
    return m.group(1) if m else None


def test_contract_version_matches_the_newest_history_entry():
    """`CONTRACT_VERSION` and docs/CONTRACT_HISTORY.md move together: the history's newest entry is the version
    `hv version` reports. A bump of one without the other fails here (2.0: Fable's ask on #136)."""
    hv = _load_hv()
    assert _history_head((DOCS / "CONTRACT_HISTORY.md").read_text()) == hv.CONTRACT_VERSION


def test_mutant_a_bump_of_only_one_side_is_caught():
    hv = _load_hv()
    history = (DOCS / "CONTRACT_HISTORY.md").read_text()
    # the history bumped, the version line not
    assert _history_head("## 9.9\n\nx\n\n" + history) != hv.CONTRACT_VERSION
    # the version line bumped, the history not
    assert _history_head(history) != "9.9"


# The five places the docs state the contract version in prose (#141, set to 2.1 by the bump in 2.1 PR 10).
_VERSION_STATEMENTS = [
    ("README.md", "Current agent contract: **{v}**"),
    ("CONTRIBUTING.md", "contract is {v}."),
    ("docs/WORKING_TOGETHER.md", "Today that set is number {v}."),
    ("docs/CLI_REFERENCE.md", "Official HiveMind v{v} from ProjectMentor"),
    ("docs/CLI_REFERENCE.md", "hv contract-version {v}"),
]


def test_docs_that_state_the_contract_version_name_contract_version():
    """Each prose statement of the contract version matches `CONTRACT_VERSION`; a bump that leaves one behind fails."""
    hv = _load_hv()
    for rel, template in _VERSION_STATEMENTS:
        text = (PROJECT / rel).read_text()
        assert template.format(v=hv.CONTRACT_VERSION) in text, (
            f"{rel} does not say {template.format(v=hv.CONTRACT_VERSION)!r} (CONTRACT_VERSION={hv.CONTRACT_VERSION})"
        )


def test_mutant_a_stale_version_statement_is_caught():
    hv = _load_hv()
    rel, template = _VERSION_STATEMENTS[0]
    stale = (PROJECT / rel).read_text().replace(template.format(v=hv.CONTRACT_VERSION), template.format(v="1.25"))
    assert template.format(v=hv.CONTRACT_VERSION) not in stale


def _doc_routes(text):
    """The `| `/v1/…` | GET |` rows of a route table: {path: verb}."""
    return dict(re.findall(r"^\| `(/v1/[a-z/]*)` \| (GET|POST) \|", text, re.M))


def test_module_api_doc_route_table_equals_the_handler():
    """docs/MODULE_API.md lists exactly the routes `hive_module_api` serves, each with its verb (A5: the doc
    cannot claim a route the code lacks, or omit one it has)."""
    import hive_module_api as api
    want = {path: ("POST" if path in api.WRITE_ROUTES else "GET") for path in api.ROUTES}
    assert _doc_routes((DOCS / "MODULE_API.md").read_text()) == want


def test_mutant_a_route_missing_from_the_doc_is_caught():
    import hive_module_api as api
    doc = (DOCS / "MODULE_API.md").read_text().replace("| `/v1/tip` | GET |", "| `/v1/tap` | GET |")
    assert _doc_routes(doc) != {path: ("POST" if path in api.WRITE_ROUTES else "GET") for path in api.ROUTES}


def test_contract_names_every_route_and_the_quota_defaults():
    import hive_module_api as api
    text = (DOCS / "CONTRACT.md").read_text()
    for path in api.ROUTES:
        assert path in text, f"docs/CONTRACT.md does not mention {path}"
    module_api = (DOCS / "MODULE_API.md").read_text()
    for key, default in api.QUOTA_DEFAULTS.items():
        assert f"`{key}`" in module_api, f"docs/MODULE_API.md does not name the `{key}` quota"
    assert "50,000" in module_api and "16 KiB" in module_api and "| 60 |" in module_api and "| 500 |" in module_api
