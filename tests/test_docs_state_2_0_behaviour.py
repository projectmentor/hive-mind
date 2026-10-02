"""The docs state what 2.0 does, not what 1.x promised it would do (#136; Grok and Fable on #136).

4a (#171) refuses a bare local id on every reference, makes `tags` a JSON list, and drops the `$HERMES_AGENT`
source fallback. Text written during 1.x described each of those as a future change ("still accepted",
"removed at the next MAJOR", "`tags` becomes the list"). Read on a 2.0 node that is wrong. This test fails when
a scanned document still says so. It uses the same exclusions as `test_docs_name_moved_commands.py`: history
(including the per-version entries of `AGENT_INTEGRATION.md` §7, which describe 1.x truthfully), the CHANGELOG,
and `README.md` and `SECURITY.md`, which #141 reconciles.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_docs_name_moved_commands import (EXCLUDED_DIRS, EXCLUDED_FILES, SECTION7_FILE,  # noqa: E402
                                            _history_lines, _tracked_docs)

STALE = [
    r"local ids? [^.|]{0,80}still accepted",
    r"bare (local )?ids? [^.|]{0,80}still accepted",
    r"still accepted[^.|]{0,80}local id",
    r"(removed|dropped) at the next MAJOR",
    r"stops? being accepted at the next MAJOR",
    r"`tags` becomes (the|a) list",
    r"becomes the list at\s+2\.0",
    r"omitted,? it is `\$HERMES_AGENT`",
]
_RX = [re.compile(p, re.I) for p in STALE]


def stale_statements(docs):
    hits = []
    for path, text in sorted(docs.items()):
        if path in EXCLUDED_FILES or path.startswith(EXCLUDED_DIRS):
            continue
        skip = _history_lines(text) if path == SECTION7_FILE else set()
        lines = text.split("\n")
        for n, line in enumerate(lines, 1):
            if n in skip:
                continue
            joined = line + " " + (lines[n].strip() if n < len(lines) else "")
            if any(rx.search(line) or (rx.search(joined) and not rx.search(lines[n] if n < len(lines) else ""))
                   for rx in _RX):
                hits.append((path, n, line.strip()))
    return hits


def test_no_document_describes_2_0_behaviour_as_future():
    hits = stale_statements(_tracked_docs())
    assert not hits, ("these describe as future what 2.0 already does (local ids refused, `tags` a list, no "
                      "$HERMES_AGENT fallback):\n  " + "\n  ".join(f"{p}:{n}: {t}" for p, n, t in hits))


@pytest.mark.parametrize("text", [
    "A bare local id is still accepted but deprecated.",
    "A bare id is still accepted, with a warning.",
    "it is removed at the next MAJOR.",
    "deprecated, warned, and dropped at the next MAJOR",
    "it stops being accepted at the next MAJOR contract bump",
    "at 2.0 `tags` becomes the list",
    "omitted, it is `$HERMES_AGENT`, else `manual`",
    "Bare local ids are still\naccepted by this verb",
])
def test_mutant_each_stale_phrase_is_caught(text):
    assert stale_statements({"docs/X.md": text + "\n"}), text


@pytest.mark.parametrize("text", [
    "A bare local id is refused (2.0).",
    "`tags` is a JSON list, and `tag_list` is the same list.",
    "omitted, it is `manual`",
])
def test_the_running_behaviour_is_not_flagged(text):
    assert not stale_statements({"docs/X.md": text + "\n"}), text


def test_mutant_in_the_cli_reference_is_caught():
    docs = _tracked_docs()
    docs["docs/CLI_REFERENCE.md"] += "\nA bare local id is still accepted, with a warning.\n"
    assert any(p == "docs/CLI_REFERENCE.md" for p, _n, _t in stale_statements(docs))


def test_a_section7_entry_describing_1x_is_not_flagged():
    docs = _tracked_docs()
    text = docs[SECTION7_FILE]
    lines = text.split("\n")
    lines.insert(min(_history_lines(text)) + 1, "- `1.19` — bare local ids are still accepted, and removed at the next MAJOR.")
    docs[SECTION7_FILE] = "\n".join(lines)
    assert not [h for h in stale_statements(docs) if h[0] == SECTION7_FILE]
