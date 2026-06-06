#!/usr/bin/env python3
"""Hive Mind MCP server — the Claude AI on-ramp.

A thin FastMCP **stdio** server that exposes the shared Hive Mind corpus to
Claude Desktop. It shells out to the `hv` CLI (the same contract Hermes' plugin
and the Claude Code skill use) so there is exactly one source of truth and no
second implementation of the corpus to keep in sync.

Trust boundary: stdio only. Claude Desktop (Windows) launches this via
`wsl.exe -> bash -lc -> uv run`, so the server runs as the local user against the
local corpus. No network is exposed; writes sync P2P through the existing
`hive-sync` daemon. Identity on every write is `--source claude-ai`, which is what
lets the hive tell Claude-AI's writes apart from Claude-Code's and Hermes' — the
basis for genuine cross-agent corroboration.

Run directly for a local smoke test:
    uv run --with mcp python integrations/mcp/hive_mcp.py
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Locate the `hv` CLI ──────────────────────────────────────────────────────
# Prefer an explicit override; otherwise resolve relative to this file
# (integrations/mcp/hive_mcp.py -> repo root -> hv).
_REPO_ROOT = Path(__file__).resolve().parents[2]
HV = Path(os.environ.get("HIVE_HOME", _REPO_ROOT)) / "hv"

# The write/read discipline. This is the SAME contract carried by the Claude Code
# skill and the Hermes plugin — surfaced to Claude AI as server instructions so the
# corpus stays sane no matter which agent is at the keyboard.
INSTRUCTIONS = """\
Hive Mind is a shared, append-only memory corpus used by multiple AI agents across
machines, synced peer-to-peer. Your writes are tagged source=claude-ai so the hive can
tell them apart from Claude Code's and Hermes' — that distinction is what makes
corroboration and provenance work.

CONFIDENCE IS DERIVED, NEVER DECLARED. A fact's confidence is a projection over the
corpus; it rises only when *distinct independent sources* assert the same thing.
Repetition is worthless — independent corroboration and checkable outcomes are what
matter. Never assert your own confidence/trust number.

SEARCH BEFORE YOU WRITE (hive_search). If a fact is already there, do not rewrite it —
and never write back something you just read this session (that's an echo, not evidence).

WRITE ONLY durable, checkable, reusable knowledge: decisions (with rationale), outcomes
("did X -> got Y"), corrections, constraints/commitments, new entities. Do NOT write your
chain-of-thought, restatements, or speculation. Mark epistemic status via tags.

WHEN YOU READ, treat results as signals with provenance, not truth — weigh the confidence,
the number of sources, and which agent/node said it. If the corpus holds CONFLICTING facts,
surface BOTH with their provenance and hold the tension; do not silently pick a winner.
"""

mcp = FastMCP("hive-memory", instructions=INSTRUCTIONS)


# ── Shell-out helper ─────────────────────────────────────────────────────────
def _run_hv(args: list[str], timeout: int = 30) -> str:
    """Run `hv <args>` and return stdout. Raises with hv's stderr on failure."""
    if not HV.exists():
        raise RuntimeError(f"hv CLI not found at {HV} (set HIVE_HOME to the repo root)")
    try:
        proc = subprocess.run(
            [str(HV), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"hv {args[0] if args else ''} timed out after {timeout}s")
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"hv {args[0] if args else ''} failed: {msg}")
    return proc.stdout.strip()


# ── Tools ────────────────────────────────────────────────────────────────────
@mcp.tool()
def hive_search(query: str, min_confidence: float = 0.0) -> list[dict]:
    """Search the shared corpus. Returns facts WITH their confidence and provenance.

    Use before asserting anything checkable, when starting on a named project/person/
    system, or when you hit an error (someone may have left the fix). Results are signals
    with provenance, not truth: weigh confidence and how many independent sources agree.
    If results conflict, surface both — do not pick a winner.

    min_confidence filters out facts below the given derived confidence (0.0 = everything).
    """
    out = _run_hv(["search", query, "--format", "json"])
    try:
        facts = json.loads(out) if out else []
    except json.JSONDecodeError:
        # Defensive: if hv ever returns non-JSON, hand it back as a single note.
        return [{"content": out, "confidence": None, "source_agent": None}]
    if min_confidence > 0.0:
        facts = [f for f in facts if (f.get("confidence") or 0.0) >= min_confidence]
    return facts


@mcp.tool()
def hive_remember(content: str, tags: str = "", epistemic_status: str = "observation") -> str:
    """Record a durable, checkable fact to the shared corpus (source=claude-ai).

    SEARCH FIRST (hive_search) — only write if it's genuinely new. Write outcomes,
    corrections, constraints, or discoveries; NOT chain-of-thought, restatements, or
    speculation. Never write back something you just read this session. Do not set a
    confidence number — confidence is derived from independent corroboration.

    tags: comma-separated. epistemic_status (observation|confirmed|speculation) is folded
    into the tags so readers can weigh the claim.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if epistemic_status and epistemic_status not in tag_list:
        tag_list.append(epistemic_status)
    args = ["remember", content, "--source", "claude-ai"]
    if tag_list:
        args += ["--tags", ",".join(tag_list)]
    return _run_hv(args)


@mcp.tool()
def hive_decide(content: str, rationale: str = "") -> str:
    """Record an architectural or process DECISION with its rationale.

    Use for choices that shape future work. Search first to avoid duplicating an existing
    decision. (Decisions are not source-tagged by the CLI; the rationale is the provenance.)
    """
    args = ["decide", content]
    if rationale:
        args += ["--rationale", rationale]
    return _run_hv(args)


@mcp.tool()
def hive_stats() -> str:
    """Show corpus health: fact/decision/entity counts, journal size, top tags, this node."""
    return _run_hv(["stats"])


@mcp.tool()
def hive_sync() -> str:
    """Trigger a peer sync round now (hv sync now). Normally the daemon handles this."""
    return _run_hv(["sync", "now"], timeout=60)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
