#!/usr/bin/env python3
"""Generate docs/NAMESPACES.md, the names core reserves, from vocabulary.py (2.0, public #136 and #150).

Deterministic: one table per category in the registry's order, rows sorted by name, no timestamps and nothing
read from git, so any checkout of the same tree generates the same bytes. `tests/test_vocabulary.py` fails
when the committed page differs from this output.

    python3 scripts/common/gen_namespaces.py            # writes docs/NAMESPACES.md
    python3 scripts/common/gen_namespaces.py OUT.md     # writes OUT.md instead
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
import vocabulary  # noqa: E402

INTRO = """\
# Namespaces: the names core reserves

Generated from [`vocabulary.py`](../vocabulary.py) by `scripts/common/gen_namespaces.py`. Do not edit this
page by hand: change the registry and run the generator. A test fails when the two differ, and another fails
when the code writes or reads a name the registry does not list, or the registry lists a name no code uses.

## Why

A journal entry whose type, link kind or governance action a node does not recognise still lands, and
projects to nothing. That is how an older node stays converged with a newer one. The journal is permanent,
so the rule has a cost: if a module wrote a name that a later core feature then reused, every entry the
module had written would be read with the new meaning, on every node, from then on.

## The rule

- **Core reserves every bare name on this page**, in its category.
- **A module's names take a prefix: `x-<module>:<name>`.** The form is pending David's word on #136;
  `x-<module>.<name>` is the other recorded form. No core name starts with `x-`.
- **The 2.1 module API enforces the prefix.** 2.0 reserves the names and documents them.
- **Envelope fields are the entry's structure**, not vocabulary a module picks. A module never writes its
  own: the core builds the envelope around a module's payload.

## Reading the tables

- `written`: core writes the name today.
- `legacy`: core no longer writes it, and still projects the entries that carry it.
- `read`: core reads and honours it, and never writes it itself; agents and people do.
- `reserved`: no core code uses it yet. It is held for the 2.1 module envelope.

*Since* is the contract version that introduced the name. It is a field of the registry, filled in from the
changelog and the contract history as a best effort, and never derived from git."""


def render():
    lines = [INTRO]
    for _key, title, what, table in vocabulary.CATEGORIES:
        lines += ["", f"## {title}", "", what, "", "| Name | Status | Since | Meaning |", "|---|---|---|---|"]
        for name in sorted(table):
            rec = table[name]
            lines.append(f"| `{name}` | {rec['status']} | {rec['since']} | {rec['meaning']} |")
    return "\n".join(lines) + "\n"


def main(argv):
    out = Path(argv[1]) if len(argv) > 1 else ROOT / "docs" / "NAMESPACES.md"
    out.write_bytes(render().encode())
    print(f"wrote {out}: {sum(len(t) for *_, t in vocabulary.CATEGORIES)} names in "
          f"{len(vocabulary.CATEGORIES)} categories")


if __name__ == "__main__":
    main(sys.argv)
