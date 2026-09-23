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

WRITE ONLY durable, checkable, reusable knowledge, and route it by what it is:
- a DECISION -> hive_decide(content, rationale, informed_by="<sid>,<sid>") naming the facts
  you searched and relied on (their `sid`, `h:…`, from hive_search);
- the RESULT of acting on a recorded decision -> hive_remember(content, outcome_of="<decision
  sid>", polarity=1|0|-1). Only observed results count; for your own assessment rather than
  an observation, pass channel="introspect" (recorded, never counted);
- a HYPOTHESIS worth testing -> hive_propose(content): an idea, never a fact;
- an OBSERVATION that bears on an idea or a fact -> hive_remember(content, supports="<sid>") or
  contradicts="<sid>" (your own support of your own idea doesn't count);
- a CORRECTION of a fact that is wrong -> hive_remember(content, resolves="<wrong fact's sid>"):
  writes the correction and soft-retracts the old fact in one step (reversible);
- constraints/commitments, observations, new entities -> hive_remember.
Do NOT write your chain-of-thought or restatements. Mark epistemic status via tags.

WHEN YOU READ, treat results as signals with provenance, not truth — weigh the confidence,
the number of sources, and which agent/node said it. If the corpus holds CONFLICTING facts,
surface BOTH with their provenance and hold the tension; do not silently pick a winner.

LIFECYCLE (you are a pull-only host — there are no per-turn hooks, so you call these yourself):
At the START of a conversation, call hive_nudge(event="session-start") and act on the digest plus
the audit-on-boot line it returns. When wrapping up, or on any audit hint, call hive_audit and
reconcile what it surfaces — but you remain the salience judge: audits only prompt, they never
auto-write or auto-delete, and forgetting is the owner's call.
"""

mcp = FastMCP("hive-memory", instructions=INSTRUCTIONS)


# ── Shell-out helper ─────────────────────────────────────────────────────────
def _run_hv(args: list[str], timeout: int = 30, stdin_text: str | None = None) -> str:
    """Run `hv <args>` and return stdout. Raises with hv's stderr on failure.

    stdin_text, if given, is piped to the process (e.g. the user's turn text for
    `hv nudge --event=user-prompt`, which reads recent text on stdin).
    """
    if not HV.exists():
        raise RuntimeError(f"hv CLI not found at {HV} (set HIVE_HOME to the repo root)")
    try:
        proc = subprocess.run(
            [str(HV), *args],
            input=stdin_text,
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
def hive_search(query: str, min_confidence: float = 0.0, kind: str = "all") -> list[dict]:
    """Search the shared corpus. Returns facts WITH their confidence and provenance.

    kind: "all" (default: facts + decisions + ideas that have EARNED confidence > 0), or
    "fact" | "decision" | "idea" ("idea" lists every hypothesis, including raw ones at 0.0).

    Use before asserting anything checkable, when starting on a named project/person/
    system, or when you hit an error (someone may have left the fix). Results are signals
    with provenance, not truth: weigh confidence and how many independent sources agree.
    If results conflict, surface both — do not pick a winner.

    Every row carries two STABLE identities of that entry: `sid` (the short id, `h:` + 10 hex —
    the form to pass to other tools: hive_decide informed_by, hive_remember outcome_of, hive_retract,
    hive_entity fact_id) and `ref` (`node_id:seq`, the raw journal identity). Both are identical on
    every node and never change. The numeric `id` is this node's rebuild-unstable rowid; it is
    DEPRECATED as an input and stops being accepted at the next MAJOR contract bump — never carry it
    across a sync or a session.

    min_confidence filters out facts below the given derived confidence (0.0 = everything).
    """
    args = ["search", query, "--format", "json"]
    if kind and kind != "all":
        args += ["--kind", kind]
    out = _run_hv(args)
    try:
        facts = json.loads(out) if out else []
    except json.JSONDecodeError:
        # Defensive: if hv ever returns non-JSON, hand it back as a single note.
        return [{"content": out, "confidence": None, "source_agent": None}]
    if min_confidence > 0.0:
        facts = [f for f in facts if (f.get("confidence") or 0.0) >= min_confidence]
    return facts


@mcp.tool()
def hive_remember(content: str, tags: str = "", epistemic_status: str = "observation",
                  outcome_of: str = "", polarity: int = 1, channel: str = "", resolves: str = "",
                  supports: str = "", contradicts: str = "") -> str:
    """Record a durable, checkable fact to the shared corpus (source=claude-ai).

    SEARCH FIRST (hive_search) — only write if it's genuinely new. Write outcomes,
    corrections, constraints, or discoveries; NOT chain-of-thought, restatements, or
    speculation. Never write back something you just read this session. Do not set a
    confidence number — confidence is derived from independent corroboration.

    tags: comma-separated. epistemic_status (observation|confirmed|speculation) is folded
    into the tags so readers can weigh the claim.

    outcome_of: when this fact is the OUTCOME of a decision you acted on, pass that decision's
    `sid` (from hive_search, `h:…`; its `ref` `node_id:seq` also works). polarity: +1 it worked out (default), -1 it did
    not, 0 observed and neutral — ternary on purpose. channel: leave empty for an observation
    of the world (the default, counted); pass "introspect" if this is your own reasoning rather
    than something observed — it is recorded but never counted as corroboration of the fact or
    toward the decision's outcome.

    resolves: when this fact CORRECTS an earlier fact that is wrong, pass the wrong fact's `sid`
    (from hive_search, `h:…`; its `ref` also works). The correction is written with one `resolves`
    link and the old fact is soft-retracted: reversible negative evidence, never a deletion. The
    target is kind-checked; a bad reference aborts before anything is written.

    supports / contradicts: when this OBSERVATION is evidence for or against a fact or an idea (for
    example an open idea from the session-start digest), pass its `sid`. Writes the fact plus one
    `supports` or `contradicts` link; the target is re-scored immediately. Your own support of your own
    idea doesn't count; contradicting it does (that is how to withdraw it).

    Pass at most ONE of outcome_of, resolves, supports, contradicts: one relationship per write.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if epistemic_status and epistemic_status not in tag_list:
        tag_list.append(epistemic_status)
    args = ["remember", content, "--source", "claude-ai"]
    if tag_list:
        args += ["--tags", ",".join(tag_list)]
    if outcome_of:
        args += ["--outcome-of", outcome_of.strip(), "--polarity", str(int(polarity))]
    if channel:
        args += ["--channel", channel]
    if resolves:
        args += ["--resolves", resolves.strip()]
    if supports:
        args += ["--supports", supports.strip()]
    if contradicts:
        args += ["--contradicts", contradicts.strip()]
    return _run_hv(args)


@mcp.tool()
def hive_decide(content: str, rationale: str = "", tags: str = "", informed_by: str = "") -> str:
    """Record an architectural or process DECISION with its rationale.

    Use for choices that shape future work. Search first to avoid duplicating an existing
    decision. (Decisions are not source-tagged by the CLI; the rationale is the provenance.)

    tags: comma-separated (e.g. the project) so the decision is findable by hive_search /
    `hv search` by tag/text instead of by an unstable, node-local decision id.

    informed_by: comma-separated references to the facts/decisions you retrieved and RELIED ON
    for this decision — pass the `sid` values from hive_search results (`h:…`; `ref` `node_id:seq`
    also works — both stable
    across nodes and rebuilds). Bare local ids (`118`, `d17`) are accepted but drift; prefer
    the `sid`. An unresolvable reference aborts the whole write. This is what lets the hive learn
    which knowledge turns out to matter once the decision's outcomes are recorded.
    """
    args = ["decide", content]
    if rationale:
        args += ["--rationale", rationale]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if tag_list:
        args += ["--tags", ",".join(tag_list)]
    refs = [r.strip() for r in informed_by.split(",") if r.strip()]
    if refs:
        args += ["--informed", *refs]
    return _run_hv(args)


@mcp.tool()
def hive_propose(content: str, tags: str = "") -> str:
    """Record an IDEA — a hypothesis the hive can support or contradict (contract 1.20).

    An idea is not a fact: it starts at confidence 0.0 and can EARN confidence only from
    sense-channel `supports`/`contradicts` links written by other identities against
    observations: hive_remember(observation, supports="<idea sid>"). Your own support of your own
    idea doesn't count. Restating it, or another agent proposing the same text, is a second
    hypothesis, never corroboration. Use it for "perhaps X relates to Y" — things you want the
    hive to test over time, not things you observed. Search first (hive_search kind="idea").
    """
    args = ["propose", content, "--source", "claude-ai"]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if tag_list:
        args += ["--tags", ",".join(tag_list)]
    return _run_hv(args)


@mcp.tool()
def hive_stats() -> str:
    """Show corpus health: fact/decision/entity counts, journal size, top tags, this node."""
    return _run_hv(["stats"])


@mcp.tool()
def hive_retract(fact_id: str, reason: str = "") -> str:
    """Record SOFT negative evidence against a fact (a reversible soft-forget), source=claude-ai.

    fact_id: the fact's `sid` from hive_search (`h:…`, stable on every node) — or its `ref`
    (`node_id:seq`). A bare numeric id is still accepted but DEPRECATED: it is this node's rowid,
    reassigned on every rebuild (after every write and every sync), so it can silently name a
    different fact by the time you use it. The target is kind-checked (a decision aborts).

    Use when you find a fact is wrong or stale and want to down-weight it without destroying it.
    This is deliberate, reversible negative evidence — NOT a deletion. The decisive owner-forget
    (`hv retract --owner`) is intentionally NOT exposed here; it stays a CLI/owner action.

    To REPLACE a fact with a correction, don't use this tool: call
    hive_remember("<correction>", resolves="<sid>"), which writes the correction and soft-retracts
    the old fact in one step. A prose "resolves <sid>" in the correction's text only gets flagged
    later by the audit; it does not retract anything.
    """
    args = ["retract", str(fact_id).strip(), "--source", "claude-ai"]
    if reason:
        args += ["--reason", reason]
    return _run_hv(args)


@mcp.tool()
def hive_entity(action: str, name: str = "", type: str = "", attr: str = "",
                fact_id: str | int | None = None, confidence: float | None = None) -> str:
    """Manage entities (people, projects, concepts) and link facts to them.

    action:
    - "list"  — list known entities.
    - "show"  — show one entity and its linked facts (pass name).
    - "add"   — create/upsert an entity (pass name, optional type, optional attr as a JSON string).
    - "link"  — attach a fact to an entity (pass name + fact_id — the fact's `sid` from hive_search,
      `h:…`, or its `ref`; a bare numeric rowid is deprecated — optional confidence).

    Entities are the corpus's nouns; linking facts to them makes recall by subject reliable.
    """
    args = ["entity", action]
    if name:
        args += ["--name", name]
    if type:
        args += ["--type", type]
    if attr:
        args += ["--attr", attr]
    if fact_id is not None and str(fact_id).strip():
        args += ["--fact-id", str(fact_id).strip()]
    if confidence is not None:
        args += ["--confidence", str(confidence)]
    return _run_hv(args)


@mcp.tool()
def hive_whoami() -> str:
    """Show THIS device's identity and membership status (sterile / fertile / owner).

    Read-only. Tells you whether this node can write to a governed hive yet, who the owner is, and
    this device's fingerprint — useful when a write is rejected or sync isn't converging.
    """
    return _run_hv(["whoami"])


@mcp.tool()
def hive_members() -> str:
    """List the hive's admitted devices (read-only view of `hv group list`).

    The mutating membership/governance verbs (admit/revoke/deny/change/purge) and owner/key/capsule
    operations are owner-level and intentionally CLI-only — they are not exposed over MCP.
    """
    return _run_hv(["group", "list"])


@mcp.tool()
def hive_peers() -> str:
    """List hive members by device + principal + address + reachability (read-only).

    Authoritative roster from the governance journal, with each device's advertised address and a
    live reachable/in-sync/diverged/unreachable probe — useful when sync isn't converging and you
    need to see which device (not just which principal) is unreachable.
    """
    return _run_hv(["peers"], timeout=60)


@mcp.tool()
def hive_discover(format: str = "text") -> str:
    """Scan the tailnet for reachable hives (Tailscale + /hive/info probe). Read-only.

    format: text|json. Use to find peers before joining/configuring sync.
    """
    return _run_hv(["discover", "--format", format], timeout=60)


@mcp.tool()
def hive_verify() -> str:
    """Verify this is the official, untampered HiveMind (checks the signed source manifest). Read-only."""
    return _run_hv(["verify"])


@mcp.tool()
def hive_version() -> str:
    """Print the hv contract version (SemVer). Read-only — handy to confirm feature parity."""
    return _run_hv(["version"])


@mcp.tool()
def hive_telemetry_report(view: str = "report", since: str = "") -> str:
    """Read the LOCAL-ONLY session telemetry (observability, never corpus/sync data). Read-only.

    view="report" aggregates usage (optionally windowed by `since`, e.g. "7d", "24h");
    view="list" shows recent raw session records. This is pure observability — it is NOT knowledge.
    """
    if view == "list":
        return _run_hv(["telemetry", "list"])
    args = ["telemetry", "report"]
    if since:
        args += ["--since", since]
    return _run_hv(args)


@mcp.tool()
def hive_sync() -> str:
    """Trigger a peer sync round now (hv sync now). Normally the daemon handles this.

    (The long-running `hv sync daemon` is intentionally not exposed — it blocks forever and is the
    installed service's job, not an MCP call.)
    """
    return _run_hv(["sync", "now"], timeout=60)


@mcp.tool()
def hive_nudge(event: str, session_id: str = "", cwd: str = "", text: str = "") -> str:
    """Get a startup digest or a save/audit hint from the hive (the pull-only lifecycle hook).

    An MCP host cannot run per-turn hooks, so YOU call this at your checkpoints:

    - event="session-start": call once at the START of a conversation, passing cwd. Returns the
      project digest PLUS a short audit-on-boot line (what the previous session left to re-check,
      dedup, or reconcile) — inject it and act on it. It also emits a re-integrate nudge if the
      contract had a MAJOR bump since you last integrated.
    - event="user-prompt": optional; pass the user's message as `text` (piped on stdin). Returns a
      terse save-nudge or nothing. `hv` owns the debounce — a quiet turn returns empty.
    - event="precompact" / "sessionend": optional; before context is lost or at the end, get an
      audit hint, then call hive_audit and reconcile.

    Returns hv's hint, or "" when there is nothing to surface.
    """
    args = ["nudge", "--event", event, "--agent", "claude-ai"]
    if session_id:
        args += ["--session", session_id]
    if cwd:
        args += ["--cwd", cwd]
    # Only the per-turn path consumes stdin; for other events there is nothing to pipe.
    return _run_hv(args, stdin_text=text if event == "user-prompt" else None)


@mcp.tool()
def hive_audit(depth: str = "normal", session_id: str = "", format: str = "text") -> str:
    """Surface facts to reconcile: redundant, obsolete, recheck (volatile past freshness), missing.

    Call after a hive_nudge audit hint, or when wrapping up a session. You remain the salience
    judge — audit only PROMPTS; it never auto-writes or auto-deletes. Reconcile by writing
    corrections or (owner-only) retracting.

    depth: light|normal|deep. format: text|json. Pass session_id to enable the MISSING proxy.
    """
    args = ["audit", "--depth", depth, "--format", format]
    if session_id:
        args += ["--session", session_id]
    return _run_hv(args)


@mcp.tool()
def hive_telemetry(event: str, session_id: str = "", cwd: str = "") -> str:
    """Record session observability (start/end) to the LOCAL-ONLY telemetry lane.

    This is pure observability — duration/usage. It NEVER enters the corpus, the journal, or sync,
    and has no effect on knowledge or confidence. It is not a fact; never use hive_remember for it.

    event="start" at the beginning, event="end" when wrapping up. Pull-only caveat: an MCP host
    cannot guarantee the end call fires, so event="end" is best-effort.
    """
    args = ["telemetry", "record", "--event", event, "--agent", "claude-ai"]
    if session_id:
        args += ["--session", session_id]
    if cwd:
        args += ["--cwd", cwd]
    return _run_hv(args)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
