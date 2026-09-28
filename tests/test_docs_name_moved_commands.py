"""The docs name each moved command by the plane it runs on (2.0, decision h:02010d37e3).

Since 2b, a command that moved to the control plane prints a pointer and exits 2 when typed on `hv`. A doc that
still tells a reader to run `hv owner init` sends them to a pointer, not to a command. This test fails when a
tracked document names a moved command on `hv`. The patterns come from `commandmap.MOVED`, plus the two
flag-conditional forms `retract --owner` and `owner propose-election --mint`, so a command that moves later is
covered without editing this file.

What is deliberately NOT scanned is listed here, not inferred:
- history: `CHANGELOG.md`, `docs/CONTRACT_HISTORY.md`, `docs/history/`, and the per-version entries of
  `docs/AGENT_INTEGRATION.md` §7 (from its **Changelog.** line to the next section). They record what each
  version shipped.
- `README.md` and `SECURITY.md`: the #141 carve-out reconciles those (decision h:02010d37e3).
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import commandmap  # noqa: E402

EXCLUDED_FILES = {"CHANGELOG.md", "docs/CONTRACT_HISTORY.md", "README.md", "SECURITY.md"}
EXCLUDED_DIRS = ("docs/history/",)
SECTION7_FILE = "docs/AGENT_INTEGRATION.md"


def _patterns():
    pats = [re.compile(r"(?<![\w/-])(?:\./)?hv\s+" + r"\s+".join(map(re.escape, key)) + r"(?![\w-])")
            for key in commandmap.MOVED]
    pats.append(re.compile(r"(?<![\w/-])(?:\./)?hv\s+retract\b[^`\n]*--owner\b"))
    pats.append(re.compile(r"(?<![\w/-])(?:\./)?hv\s+owner\s+propose-election\b[^`\n]*--mint\b"))
    return pats


def _history_lines(text):
    """The 1-based line numbers of §7's per-version entries in AGENT_INTEGRATION.md."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines, 1) if l.startswith("**Changelog.**")), None)
    if start is None:
        return set()
    end = next((i for i, l in enumerate(lines, 1) if i > start and l.startswith("## ")), len(lines) + 1)
    return set(range(start, end))


def stale_references(docs):
    """[(path, line, text)] for every moved command named on `hv` in `docs` ({path: text}), outside the
    exclusions. A reference broken across a line is caught by also scanning each pair of lines joined."""
    pats, hits = _patterns(), []
    for path, text in sorted(docs.items()):
        if path in EXCLUDED_FILES or path.startswith(EXCLUDED_DIRS):
            continue
        skip = _history_lines(text) if path == SECTION7_FILE else set()
        lines = text.split("\n")
        for n, line in enumerate(lines, 1):
            if n in skip:
                continue
            here = line.rstrip()
            joined = here + " " + (lines[n].lstrip() if n < len(lines) else "")
            for p in pats:
                m = p.search(joined)
                if not m or m.start() >= len(here):
                    continue
                # A match that runs into the next line counts only when this line ends inside the command
                # itself (`hv owner` / `init`), never when it strings a usage line onto the next one.
                if m.end() > len(here) and not re.fullmatch(r"(?:\./)?hv(?:\s+[a-z-]+)*\s*", here[m.start():]):
                    continue
                hits.append((path, n, line.strip()))
                break
    return hits


def _tracked_docs():
    try:
        out = subprocess.run(["git", "-C", str(PROJECT), "ls-files", "-z", "*.md", "integrations/*.py"],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("not a git checkout")
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    paths = [p for p in out.stdout.decode().split("\0") if p]
    return {p: (PROJECT / p).read_text(errors="replace") for p in paths if (PROJECT / p).is_file()}


def test_no_document_names_a_moved_command_on_hv():
    hits = stale_references(_tracked_docs())
    assert not hits, ("these name a command that moved to `hive-mind` in 2.0 as an `hv` command; a reader who "
                      "runs it gets a pointer:\n  " + "\n  ".join(f"{p}:{n}: {t}" for p, n, t in hits))


def test_the_scan_covers_the_docs_in_scope():
    docs = _tracked_docs()
    for path in ("docs/CLI_REFERENCE.md", "docs/INTERNALS.md", "docs/AGENT_INTEGRATION.md",
                 "integrations/mcp/hive_mcp.py"):
        assert path in docs, path


def test_mutant_a_moved_command_in_the_cli_reference_is_caught():
    docs = _tracked_docs()
    docs["docs/CLI_REFERENCE.md"] += "\nRun `hv owner init` once.\n"
    assert any(p == "docs/CLI_REFERENCE.md" and "hv owner init" in t for p, _n, t in stale_references(docs))


@pytest.mark.parametrize("text", ["`hv group admit k1:…`", "`hv config confidence set cap_self 0.7`",
                                  "`hv retract h:0123456789 --reason x --owner`",
                                  "`hv owner propose-election --mint`", "`./hv unforget h:0123456789`",
                                  "the old `hv owner\n  revoke-escrow all`"])
def test_mutant_each_moved_shape_is_caught(text):
    assert stale_references({"docs/X.md": text + "\n"}), text


@pytest.mark.parametrize("text", ["hv retract <fact> [--source S]   # evidence\nhive-mind retract <fact> --owner",
                                  "`hv owner show`", "`hv owner propose-election --pub B64`", "`hv owner vote 3`",
                                  "`hv group list`", "`hv retract h:0123456789 --reason x`",
                                  "`hive-mind owner init`", "`hv config identity show`", "`hv doctor --fix`"])
def test_what_stays_on_hv_is_not_flagged(text):
    assert not stale_references({"docs/X.md": text + "\n"}), text


def test_mutant_in_section7_history_is_not_flagged():
    docs = _tracked_docs()
    text = docs[SECTION7_FILE]
    lines = text.split("\n")
    at = min(_history_lines(text)) + 1
    lines.insert(at, "- `0.9` — `hv owner init` did something once.")
    docs[SECTION7_FILE] = "\n".join(lines)
    assert not [h for h in stale_references(docs) if h[0] == SECTION7_FILE]


def test_excluded_files_are_not_scanned():
    assert not stale_references({"README.md": "`hv owner init`\n", "docs/history/P2P_DESIGN.md": "`hv admit`\n",
                                 "CHANGELOG.md": "`hv unforget`\n"})
