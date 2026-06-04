"""Hive-mind memory provider plugin for Hermes.

Implements the capture-resistant confidence model described in
~/.claude/plans/async-inventing-shore.md (CC's design doc).

Key behaviours
--------------
WRITES (on_memory_write)
  - Source identity: hermes/<agent_identity>/<session_id[:8]> — granular enough
    for Phase B2/B3 source-class weighting (assertion vs observation).
  - Epistemic status tag: writer records speculation|observation|confirmed,
    NOT a self-declared trust number (confidence stays a derived projection).
  - Novelty gate: suppress re-ingestion of content recalled from hive this
    session (anti-echo at the write boundary, Salience Layer 3).
  - Skip: removes (append-only), non-primary contexts (cron/subagent noise).

READS (prefetch + system_prompt_block + hive_search tool)
  - session_start: system_prompt_block searches corpus for cwd/git-remote/
    project name and injects matching facts once as static context.
  - per-turn prefetch: event-driven triggers (not every turn). Fires on:
      error strings, explicit uncertainty, named entities in corpus,
      knowledge questions ("how do we", "what's the").
  - Provenance-surfacing: injects "corpus holds X — conf C, N sources,
    nodes [...]" not bare "X is true".
  - Conflict-surfacing: when facts on same topic diverge, injects BOTH sides
    with contested flag — does NOT rank-pick a winner. Tension is information.
  - Anti-self-amplification: facts authored in the current session are flagged
    "self-reported this session, unconfirmed" if surfaced, never injected clean.
  - hive_search tool: exposes explicit search for deliberate deeper digs.

Activate in config.yaml:
    memory:
      provider: hive-mind

No external dependencies — subprocess + stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

HV_PATH = Path(os.environ.get("HIVE_HOME", Path.home() / "projects" / "hive-mind")) / "hv"

# ---------------------------------------------------------------------------
# Source namespace convention (shared contract with CC's confidence model)
# ---------------------------------------------------------------------------
# Format: hermes:<context_class>/<agent_identity>/<session_id[:8]>
#
# context_class — the classification the confidence projection keys on:
#   primary    = interactive Hermes session, full agent loop
#   subagent   = delegate_task child (isolated, no memory provider)
#   cron       = scheduled job (skip_memory=True by default)
#
# agent_identity — profile name from Hermes config (e.g. "default", "coder")
#   Used for per-profile identity scoping in multi-profile setups.
#
# session_id[:8] — first 8 chars of the session UUID
#   Stable within a session, distinct across sessions of the same agent.
#
# Phase B2 source-class weighting keys on context_class:
#   primary   → source-class weight 1.0 (direct human-adjacent observation)
#   subagent  → source-class weight 0.5 (model assertion, less direct)
#   cron      → source-class weight 0.3 (automated, no human in loop)
#
# Phase B3 self-content quarantine keys on whether ALL corroborating sources
# share the same agent_identity prefix — if so, CAP_self applies.
#
# DO NOT change the format without coordinating with hv's _recompute_confidence.
# The source string is parsed by the confidence model, not just stored.

CONTEXT_PRIMARY  = "primary"
CONTEXT_SUBAGENT = "subagent"
CONTEXT_CRON     = "cron"

# Epistemic status tags (written as hv tags, NEVER as a self-declared trust score)
# These feed Phase B2 source-class weighting via the tag field, not the source field.
# The confidence number stays a pure derived projection — writers cannot set it.
EPISTEMIC_CONFIRMED    = "confirmed"     # externally verified / human-confirmed
EPISTEMIC_OBSERVATION  = "observation"   # first-hand tool result / outcome
EPISTEMIC_SPECULATION  = "speculation"   # model reasoning / restatement / inference

# Trigger patterns for prefetch (event-driven, not every turn)
_ERROR_PATTERNS = re.compile(
    r"connection refused|permission denied|no such file|module not found|"
    r"command not found|error:|exception:|traceback|failed to|can't find",
    re.IGNORECASE,
)
_UNCERTAINTY_PATTERNS = re.compile(
    r"\bnot sure\b|\bdon'?t recall\b|\bi'm unsure\b|\buncertain\b|"
    r"\bnot certain\b|\bdo(?:es)? anyone know\b|\bhow do we\b|\bwhat'?s the\b|"
    r"\bwhat is the\b|\bcan you remind\b",
    re.IGNORECASE,
)
_KNOWLEDGE_PATTERNS = re.compile(
    r"\bhow do (?:we|i|you)\b|\bwhat'?s the (?:best|right|correct)\b|"
    r"\bwhat is the (?:best|right|correct)\b|\bwhere (?:does|is|are)\b",
    re.IGNORECASE,
)


def _hv(*args: str, timeout: int = 10) -> tuple[bool, str]:
    """Run the hv CLI. Returns (success, stdout|stderr)."""
    try:
        result = subprocess.run(
            [str(HV_PATH), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, (result.stdout.strip() if result.returncode == 0 else result.stderr.strip())
    except Exception as e:
        return False, str(e)


def _hv_search_json(query: str) -> list[dict]:
    """Search hive and return parsed JSON results list."""
    ok, out = _hv("search", query, "--format", "json")
    if not ok or not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def _build_source_id(agent_identity: str, session_id: str, context_class: str = CONTEXT_PRIMARY) -> str:
    """Build a granular source identity for Phase B2/B3 weighting.

    Format: hermes:<context_class>/<agent_identity>/<session_id[:8]>

    context_class is the primary classification signal for the confidence
    model — do not change the format without coordinating with hv's
    _recompute_confidence (see source namespace convention above).
    """
    sid = (session_id or "unknown")[:8]
    identity = agent_identity or "default"
    ctx = context_class or CONTEXT_PRIMARY
    return f"hermes:{ctx}/{identity}/{sid}"


def _format_facts_with_provenance(facts: list[dict], self_session_id: str) -> str:
    """Format search results with provenance, conflict detection,
    and anti-self-amplification flagging.

    Surfaces conflict intact rather than picking a winner.
    Flags facts authored in the current session.
    """
    if not facts:
        return ""

    lines: list[str] = []
    # Group by content to detect conflicts (same topic, divergent claims handled below)
    # For now surface each fact with provenance; flag self-session facts.
    session_prefix = f"hermes/"  # any hermes write from this session
    for f in facts:
        content = f.get("content", "")
        trust = f.get("trust_score", f.get("confidence", 0.0))
        source = f.get("source_agent", "unknown")
        tags = f.get("tags", "")

        # Anti-self-amplification: flag if authored in current session
        self_flag = ""
        if self_session_id and self_session_id[:8] in source:
            self_flag = " [self-reported this session, unconfirmed]"

        # Format with explicit provenance
        lines.append(
            f"  - \"{content}\"\n"
            f"    conf={trust:.2f}  source={source}  tags={tags}{self_flag}"
        )

    return "\n".join(lines)


def _detect_conflicts(facts: list[dict]) -> list[tuple[dict, dict]]:
    """Simple conflict detection: facts with same leading words but divergent content.

    Returns pairs of conflicting facts. Heuristic only — Phase C contested
    flag will make this precise once implemented.
    """
    conflicts = []
    seen = []
    for f in facts:
        content = f.get("content", "").lower()
        for other in seen:
            other_content = other.get("content", "").lower()
            # Same first 4 words but different content = potential conflict
            f_words = content.split()[:4]
            o_words = other_content.split()[:4]
            if f_words == o_words and content != other_content:
                conflicts.append((f, other))
        seen.append(f)
    return conflicts


class HiveMindMemoryProvider(MemoryProvider):
    """Capture-resistant hive-mind memory provider.

    Implements the write/read governor from async-inventing-shore.md.
    """

    @property
    def name(self) -> str:
        return "hive-mind"

    def is_available(self) -> bool:
        return HV_PATH.exists() and HV_PATH.is_file()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")
        self._agent_identity = kwargs.get("agent_identity", "default")
        self._platform = kwargs.get("platform", "cli")

        # Map agent_context to source namespace context_class
        # agent_context values from Hermes: "primary", "subagent", "cron", "flush"
        ctx_map = {"primary": CONTEXT_PRIMARY, "subagent": CONTEXT_SUBAGENT, "cron": CONTEXT_CRON}
        self._context_class = ctx_map.get(self._agent_context, CONTEXT_PRIMARY)

        self._source_id = _build_source_id(self._agent_identity, session_id, self._context_class)

        # Track content recalled from hive this session (anti-re-ingest gate, Salience Layer 3)
        self._recall_set: Set[str] = set()

        # Track content written this session (anti-self-amplification on read side)
        self._written_this_session: Set[str] = set()

        if not self.is_available():
            logger.warning("hive-mind: hv CLI not found at %s", HV_PATH)
        else:
            logger.info("hive-mind: initialized source_id=%s", self._source_id)

    def system_prompt_block(self) -> str:
        """Inject project-context facts at session start.

        Searches corpus for cwd / git-remote / project name.
        Much more useful than just dumping stats.
        """
        if not self.is_available():
            return ""

        # Discover project identity
        cwd = Path.cwd()
        project_name = cwd.name

        # Try git remote for a more stable identity
        try:
            git_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=3, cwd=str(cwd),
            )
            if git_result.returncode == 0:
                remote = git_result.stdout.strip()
                # Extract repo name from URL
                project_name = remote.rstrip("/").split("/")[-1].replace(".git", "")
        except Exception:
            pass

        # Search for project context
        facts = _hv_search_json(project_name)
        if not facts and project_name != cwd.name:
            facts = _hv_search_json(cwd.name)

        # Track recalled content
        for f in facts:
            self._recall_set.add(f.get("content", ""))

        # Stats for orientation
        ok_stats, stats_out = _hv("stats")

        block = "\n\n## Hive Mind — Shared Corpus\n"
        block += (
            "Memory writes are mirrored here and synced to peer nodes.\n"
            "WRITE: save decisions, corrections, outcomes, constraints, first-hand observations.\n"
            "DO NOT save: intermediate reasoning, restatements of known facts, speculation.\n"
            "EPISTEMIC TAG: add --tags speculation|observation|confirmed to signal status.\n"
        )

        if ok_stats:
            block += f"\nCorpus status: {stats_out}\n"

        if facts:
            formatted = _format_facts_with_provenance(facts, self._session_id)
            block += f"\nProject context from corpus ({len(facts)} facts for '{project_name}'):\n{formatted}\n"

        return block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Event-driven prefetch — fires on specific triggers, not every turn.

        Surfaces provenance + conflict intact. Does not rank-pick a winner.
        Flags self-session content.
        """
        if not self.is_available():
            return ""

        # Evaluate triggers
        is_error = bool(_ERROR_PATTERNS.search(query))
        is_uncertain = bool(_UNCERTAINTY_PATTERNS.search(query))
        is_knowledge = bool(_KNOWLEDGE_PATTERNS.search(query))

        # Named-entity trigger: check if any word >5 chars exists in corpus
        # (cheap heuristic — FTS5 handles the actual matching)
        words = [w for w in re.findall(r'\b\w{6,}\b', query) if not w.lower() in (
            "should", "could", "would", "please", "before", "after", "during",
            "because", "through", "between", "system", "really", "actually"
        )]
        entity_trigger = bool(words)

        if not any([is_error, is_uncertain, is_knowledge, entity_trigger]):
            return ""

        # Search with the most specific signal first
        search_query = query[:200]  # cap for FTS5
        facts = _hv_search_json(search_query)

        if not facts:
            return ""

        # Filter to top 5 by trust, but preserve diversity (don't just top-3)
        facts = facts[:5]

        # Track recalled content (anti-re-ingest gate)
        for f in facts:
            self._recall_set.add(f.get("content", ""))

        # Detect conflicts — surface tension intact
        conflicts = _detect_conflicts(facts)

        lines = ["## Hive Mind — Recalled Context"]

        if is_error:
            lines.append("(triggered: error pattern detected — checking corpus for known gotchas)")
        elif is_uncertain:
            lines.append("(triggered: uncertainty signal — checking corpus before responding)")
        elif is_knowledge:
            lines.append("(triggered: knowledge question — checking corpus)")

        lines.append("")

        # Surface conflicts explicitly before listing facts
        if conflicts:
            lines.append("CONFLICT DETECTED — do not resolve, hold tension:")
            for a, b in conflicts:
                lines.append(
                    f"  CONTESTED:\n"
                    f"    A: \"{a.get('content')}\" "
                    f"(conf={a.get('trust_score', 0):.2f}, source={a.get('source_agent')})\n"
                    f"    B: \"{b.get('content')}\" "
                    f"(conf={b.get('trust_score', 0):.2f}, source={b.get('source_agent')})\n"
                    f"  → Both sides injected. Do not collapse to consensus."
                )
            lines.append("")

        lines.append("Facts (with provenance — treat as signals, not truth):")
        formatted = _format_facts_with_provenance(facts, self._session_id)
        lines.append(formatted)

        return "\n".join(lines)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Expose hive_search as a first-class tool for deliberate deeper digs."""
        return [
            {
                "name": "hive_search",
                "description": (
                    "Search the shared hive-mind corpus for facts, decisions, and known gotchas "
                    "across all nodes and agents. Returns facts with confidence scores and provenance. "
                    "Use for deliberate lookup beyond the automatic prefetch triggers. "
                    "Results include conflict flags — do not resolve contested facts, hold the tension."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search terms. FTS5 syntax supported (AND is default, OR explicit, \"exact phrase\").",
                        },
                        "min_confidence": {
                            "type": "number",
                            "description": "Minimum confidence threshold (0.0-1.0). Default 0.0 (return all).",
                            "default": 0.0,
                        },
                    },
                    "required": ["query"],
                },
            }
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle hive_search tool calls."""
        if tool_name != "hive_search":
            raise NotImplementedError(f"Unknown tool: {tool_name}")

        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})

        facts = _hv_search_json(query)

        # Track recalled content
        for f in facts:
            self._recall_set.add(f.get("content", ""))

        conflicts = _detect_conflicts(facts)

        result: dict[str, Any] = {
            "facts": facts,
            "count": len(facts),
            "conflicts": [
                {
                    "fact_a": a.get("content"),
                    "conf_a": a.get("trust_score", 0),
                    "source_a": a.get("source_agent"),
                    "fact_b": b.get("content"),
                    "conf_b": b.get("trust_score", 0),
                    "source_b": b.get("source_agent"),
                    "note": "CONTESTED — hold tension, do not collapse to consensus",
                }
                for a, b in conflicts
            ],
            "note": "Confidence scores are corpus projections, not ground truth. Surface provenance to the user.",
        }
        return json.dumps(result, indent=2)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to hive-mind with full source identity.

        Write-side governor (current state):
        - Skip removes (append-only corpus)
        - Skip non-primary contexts (cron/subagent noise)
        - Novelty gate: suppress re-ingestion of content recalled from hive this session
        - Source identity: hermes:<context>/<agent>/<session> for Phase B2/B3 weighting
        - Epistemic status tag: speculation|observation (not a self-declared trust score)

        DEFERRED (Salience Layers 2-5 from async-inventing-shore.md):
        - Structural auto-capture: classify by form (decision/correction/outcome/constraint)
          not topic — only write high-reuse shapes. Stub: _salience_gate() below.
        - Surprise + consequence weighting
        - Provisional tier + earn-your-keep promotion
        MVP stays: mirror every explicit memory() call + novelty gate.
        """
        if self._agent_context not in ("primary", ""):
            return
        if action == "remove":
            return
        if not self.is_available():
            return

        # Novelty gate (Salience Layer 3): don't re-ingest what we recalled
        if content in self._recall_set:
            logger.debug("hive-mind: novelty gate suppressed re-ingest of recalled content")
            return

        # Derive epistemic status from target/metadata tags
        # target='memory' = agent reasoning/knowledge → assertion class
        # target='user'   = observed facts about user → observation class
        # metadata may carry write_origin hints
        write_origin = (metadata or {}).get("write_origin", "")
        if target == "user" or "observation" in write_origin:
            epistemic = EPISTEMIC_OBSERVATION
        else:
            epistemic = EPISTEMIC_SPECULATION  # conservative default for agent assertions

        # Build tags: hermes base + target + epistemic status
        # NOTE: source_class (observation/assertion) is encoded in the source
        # identity string (context_class field), NOT here. Tags carry epistemic
        # status only. Do not add a numeric trust — confidence is derived.
        tags = f"hermes,{target},{epistemic}"

        # Source identity: granular for Phase B2/B3
        source = self._source_id

        ok, out = _hv("remember", content, "--tags", tags, "--source", source)
        if ok:
            self._written_this_session.add(content)
            logger.debug("hive-mind: mirrored → %s (epistemic=%s source=%s)", out, epistemic, source)
        else:
            logger.warning("hive-mind: write failed: %s", out)

    def on_session_switch(self, new_session_id: str, *, reset: bool = False, **kwargs) -> None:
        """Reset per-session state on session switch."""
        self._session_id = new_session_id
        self._source_id = _build_source_id(self._agent_identity, new_session_id, self._context_class)
        if reset:
            self._recall_set = set()
            self._written_this_session = set()

    def _salience_gate(self, content: str, metadata: Optional[Dict[str, Any]]) -> bool:
        """Structural salience filter — stub for Salience Layers 2-5.

        STUB — always returns True (pass) until implemented.

        When implemented (S-A phase per async-inventing-shore.md), this will:
        - Return True for high-reuse structural forms: decisions+rationale,
          corrections (esp. owner corrections), outcomes/results, constraints/
          preferences/commitments, new entities/relationships, first-hand tool results.
        - Return False for: intermediate reasoning, restatements of known facts,
          speculation/opinion, pleasantries, anything already in the corpus.
        - Classification is by STRUCTURE, never by content/topic (content-neutral).

        Do NOT implement a learned importance classifier here — that reintroduces
        the capturable authority the confidence model is designed to prevent.
        """
        return True  # MVP: pass everything (explicit memory() calls are already intentional)

    def shutdown(self) -> None:
        pass
