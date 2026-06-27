#!/usr/bin/env python3
"""Infer the user's real "salience" trigger phrases from past Claude Code sessions.

Mines human messages out of the local transcripts and ranks the phrases the user
actually leans on around important moments. Prints a suggested HIVE_NUDGE_PHRASES=
line you can paste into nudge.env. Read-only; touches nothing but stdout.

Usage:
  scripts/infer-phrases.py [--projects DIR] [--top N]
Default DIR: ~/.claude/projects
"""
import argparse
import collections
import glob
import json
import os
import re

# Broad candidate net of salience signals; only those that ACTUALLY appear are reported.
CANDIDATES = [
    "important", "wrong", "idea", "remember", "don't forget", "make sure", "note that",
    "turns out", "the issue", "the problem", "the fix", "we need", "we should", "i want",
    "i like", "i prefer", "decided", "decision", "gotcha", "careful", "watch out", "heads up",
    "for the record", "actually", "confirm", "verify", "verified", "never", "always",
    "that works", "works great", "didn't work", "broken", "bug", "leak", "private", "secret",
    "before we", "last thing", "one more", "lesson", "genuinely", "real", "actual", "keep",
    "save", "wait", "concern", "we definitely", "rule", "prefer",
]
# Filler to never suggest even if frequent.
STOP = {"ok", "also", "good", "great", "nice", "actually", "yeah", "yes", "no", "sure"}


def human_messages(projects_dir):
    out = []
    for f in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        if os.sep + "subagents" + os.sep in f:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "user":
                        continue
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                    if not isinstance(c, str):
                        continue
                    c = c.strip()
                    # genuine human prompts only: skip tags/tool output/huge pastes
                    if not c or c.startswith("<") or c.startswith("[Request") or len(c) > 2000:
                        continue
                    out.append(c)
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--top", type=int, default=14)
    args = ap.parse_args()

    msgs = human_messages(args.projects)
    if not msgs:
        print(f"# no human messages found under {args.projects}")
        return
    blob = "\n".join(msgs).lower()

    hits = [(p, blob.count(p)) for p in CANDIDATES]
    hits = [(p, n) for p, n in hits if n > 0 and p not in STOP]
    hits.sort(key=lambda x: -x[1])

    print(f"# inferred from {len(msgs)} human messages across {args.projects}")
    print("# candidate salience phrases (count):")
    for p, n in hits:
        print(f"#   {n:4}  {p}")

    # Suggest the strongest IMPLICIT signals (explicit save commands excluded — the agent
    # should just save on those). Keep multi-word + clearly-salient single words.
    explicit = {"save", "keep"}  # treated as commands, not nudge triggers
    suggest = [p for p, n in hits if n >= 2 and p not in explicit][: args.top]
    print("\nHIVE_NUDGE_PHRASES=" + ",".join(suggest))


if __name__ == "__main__":
    main()
