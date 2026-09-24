"""Hive-mind memory provider plugin for Hermes.

Implements the capture-resistant confidence model described in docs/INTERNALS.md (Confidence
model, Salience layers) and docs/design/hivemind_continual_learning_design.md §7.1.

Key behaviours
--------------
WRITES (on_memory_write + the hive_remember / hive_decide / hive_propose tools)
  - One writer: every `hv` write runs on a single background thread, in order (_HiveWriter). The
    memory() mirror never blocks a turn; the explicit tools wait for their result. The queue is
    bounded, and shutdown() drains it (at most 5 s) before the session-end nudge and audit.
  - The mirror stamps NO channel (so `sense`) whatever the epistemic tag: at memory() time the
    adapter can't tell an observation from reasoning, and since contract 1.21 an `introspect` fact no
    longer corroborates, so stamping it would drop most mirrors from confidence. Agents that know
    they are recording reasoning pass channel="introspect" to hive_remember explicitly.
  - Source identity: hermes/<agent_identity>/<session_id[:8]> — granular enough
    for source-class weighting (primary 1.0 / subagent 0.5 / cron 0.3) and, with the
    entry's `channel` (sense/act/introspect), for telling observation from reasoning.
  - Epistemic status tag: writer records speculation|observation|confirmed,
    NOT a self-declared trust number (confidence stays a derived projection).
  - Novelty gate: suppress re-ingestion of content recalled from hive this
    session (anti-echo at the write boundary; part of salience L1, the agent-side rubric).
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
  - hive_search tool: explicit search (kind: all|fact|decision|idea) for deliberate deeper digs,
    including listing open ideas to weigh in on with hive_remember(supports=/contradicts=).

Activate in config.yaml:
    memory:
      provider: hive-mind

No external dependencies — subprocess + stdlib only.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
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

# Epistemic status tags (written as hv tags, NEVER as a self-declared trust score). They are a
# READER hint only: the confidence projection (`_identity_weight`) never reads tags; source class and
# channel are what weigh. The confidence number stays a pure derived projection.
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


WRITE_TIMEOUT = 20      # an `hv` write takes well under a second since v1.20.1; 20 s is ample headroom


def _hv(*args: str, timeout: int = 10, stdin_text: str = "", env: Optional[Dict[str, str]] = None) -> tuple[bool, str]:
    """Run the hv CLI. Returns (success, stdout|stderr). `env` EXTENDS the environment (HIVE_HOME and
    PATH must survive); it never replaces it."""
    try:
        result = subprocess.run(
            [str(HV_PATH), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **env} if env else None,
        )
        return result.returncode == 0, (result.stdout.strip() if result.returncode == 0 else result.stderr.strip())
    except Exception as e:
        return False, str(e)


def _hv_nudge(event: str, *, session_id: str = "", cwd: str = "", agent: str = "", text: str = "") -> str:
    """Best-effort wrapper around `hv nudge`; empty string means no hint."""
    args = ["nudge", "--event", event]
    if session_id:
        args += ["--session", session_id]
    if cwd:
        args += ["--cwd", cwd]
    if agent:
        args += ["--agent", agent]
    ok, out = _hv(*args, timeout=8, stdin_text=text)
    return out if ok else ""


def _spec_update_check(agent: str) -> str:
    """Best-effort §0 check: record the current contract version marker.

    Returns the version string that was recorded, or "" on failure. A major bump
    logs a warning but never raises.
    """
    try:
        ok, out = _hv("version", timeout=8)
        version = out.strip().split()[-1] if ok and out.strip() else ""
        if not version:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent or "agent")[:60] or "agent"
        marker = Path(os.environ.get("HIVE_HOME", Path.home() / "projects" / "hive-mind")) / ".nudge_state" / f"{safe}.spec"
        marker.parent.mkdir(parents=True, exist_ok=True)
        prev = marker.read_text().strip() if marker.exists() else ""
        if prev and prev.split(".", 1)[0] != version.split(".", 1)[0]:
            logger.warning(
                "hive-mind: contract major changed %s -> %s for %s; re-integration required",
                prev, version, agent,
            )
        marker.write_text(version)
        return version
    except Exception:
        return ""


def _hv_audit(*, session_id: str = "", depth: str = "", fmt: str = "text") -> str:
    """Best-effort wrapper around `hv audit`; empty string means no surfaced audit."""
    args = ["audit"]
    if depth:
        args += ["--depth", depth]
    if session_id:
        args += ["--session", session_id]
    if fmt:
        args += ["--format", fmt]
    ok, out = _hv(*args, timeout=12)
    return out if ok else ""


def _hv_telemetry(event: str, *, session_id: str = "", identity: str = "", cwd: str = "") -> None:
    """Best-effort: record a session start/end into the LOCAL telemetry lane (never the corpus).
    agent is always 'hermes'; `identity` is the stable instance discriminator (agent_identity) so
    two hermes on the same node stay distinct. Failure is silent."""
    args = ["telemetry", "record", "--event", event, "--agent", "hermes"]
    if identity:
        args += ["--identity", identity]
    if session_id:
        args += ["--session", session_id]
    if cwd:
        args += ["--cwd", cwd]
    _hv(*args, timeout=10)


def _load_nudge_cfg() -> dict[str, str]:
    """Read nudge.env + env overrides for the cheap save-nudge gate."""
    cfg = {
        "SAVE_EVERY": "0",
        "MIN_GAP": "6",
        "SAVE_ON_PHRASE": "1",
        "HIVE_NUDGE_PHRASES": "we need,we should,i want,idea,wait,before we,keep,remember,decision,the issue,lesson,wrong",
    }
    repo_root = Path(__file__).resolve().parent.parent.parent  # integrations/hermes/ → repo root
    candidates = [
        Path(os.environ.get("HIVE_HOME", repo_root)) / "nudge.env",
        repo_root / "nudge.env",
    ]
    for p in candidates:
        try:
            if not p.exists():
                continue
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
            break
        except Exception:
            pass
    for k in list(cfg):
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


def _cfg_int(cfg: dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(cfg.get(key, default)).strip())
    except Exception:
        return default


def _cfg_phrases(cfg: dict[str, str]) -> list[str]:
    return [p.strip().lower() for p in str(cfg.get("HIVE_NUDGE_PHRASES", "")).split(",") if p.strip()]


def _hv_search_json(query: str, kind: str = "all") -> list[dict]:
    """Search hive and return parsed JSON results list. `kind`: all | fact | decision | idea."""
    args = ["search", query, "--format", "json"]
    if kind and kind != "all":
        args += ["--kind", kind]
    ok, out = _hv(*args)
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
        trust = f.get("confidence", f.get("trust_score", 0.0))  # Phase A: confidence is primary
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


class _HiveWriter:
    """The adapter's single writer (#76). Every `hv` write, from the memory() mirror and from the explicit
    tools alike, runs on one daemon thread in submission order, so the adapter never runs two writes
    against one store at once, and a mirror followed by a tool write that refers to it keeps its order.
    The queue is bounded: if `hv` wedges (up to WRITE_TIMEOUT per call), new mirror writes are dropped
    and logged rather than piling up; an explicit tool write gets an immediate error instead."""

    MAXSIZE = 64

    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue(maxsize=self.MAXSIZE)
        self._pending = 0                      # queued + running; our own count, not Queue internals
        self._idle = threading.Condition()
        threading.Thread(target=self._run, name="hive-mind-writer", daemon=True).start()

    def _run(self) -> None:
        while True:
            args, env, fut = self._q.get()
            try:
                res = _hv(*args, timeout=WRITE_TIMEOUT, env=env)
            except Exception as e:           # never let one write kill the writer
                res = (False, str(e))
            if fut is not None:
                fut.set_result(res)
            elif not res[0]:
                logger.warning("hive-mind: background write failed: %s", res[1])
            with self._idle:
                self._pending -= 1
                self._idle.notify_all()

    def submit(self, args: List[str], env: Optional[Dict[str, str]] = None, wait: bool = False):
        """Queue one `hv` write. wait=False (the mirror): returns True if queued, False if dropped.
        wait=True (a tool): blocks for (ok, output)."""
        fut = concurrent.futures.Future() if wait else None
        with self._idle:
            self._pending += 1                 # counted before the put, so the worker can never go below zero
        try:
            self._q.put_nowait((list(args), env, fut))
        except queue.Full:
            with self._idle:
                self._pending -= 1
                self._idle.notify_all()
            logger.warning("hive-mind: write queue full (%d); dropped hv %s", self.MAXSIZE, args[0] if args else "")
            return (False, "the hive-mind writer is busy (queue full); try again shortly") if wait else False
        if not wait:
            return True
        try:
            return fut.result(timeout=WRITE_TIMEOUT * 3)
        except concurrent.futures.TimeoutError:
            return (False, "still queued behind other writes; it will be written, so do not retry")

    def drain(self, timeout: float) -> int:
        """Wait up to `timeout` s for every queued write to finish. Returns how many were still pending."""
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return self._pending
                self._idle.wait(left)
        return 0


_RELATIONSHIPS = ("outcome_of", "resolves", "supports", "contradicts")


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
        self._agent_label = f"hermes:{self._agent_identity}"
        self._platform = kwargs.get("platform", "cli")

        # Map agent_context to source namespace context_class
        # agent_context values from Hermes: "primary", "subagent", "cron", "flush"
        ctx_map = {"primary": CONTEXT_PRIMARY, "subagent": CONTEXT_SUBAGENT, "cron": CONTEXT_CRON}
        self._context_class = ctx_map.get(self._agent_context, CONTEXT_PRIMARY)

        self._source_id = _build_source_id(self._agent_identity, session_id, self._context_class)

        # Track content recalled from hive this session (anti-re-ingest gate; salience L1, agent-side)
        self._recall_set: Set[str] = set()

        # Track content written this session (anti-self-amplification on read side). Marked when a write
        # is QUEUED, not when it lands: with writes off the turn, the next prefetch could otherwise
        # surface the agent's own fact without the caveat. Over-flagging is harmless (a caveat, not a filter).
        self._written_this_session: Set[str] = set()
        self._writer = _HiveWriter()
        self._last_audited_session: str = ""
        self._prefetch_turns: int = 0
        self._last_save_turn: int = 0

        if not self.is_available():
            logger.warning("hive-mind: hv CLI not found at %s", HV_PATH)
        else:
            logger.info("hive-mind: initialized source_id=%s", self._source_id)
            # Telemetry start (local-only observability; never the corpus).
            _hv_telemetry("start", session_id=session_id,
                          identity=self._agent_identity, cwd=str(Path.cwd()))

    def system_prompt_block(self) -> str:
        """Inject project-context facts at session start.

        Runs the §0 self-update check, then the session-start nudge, then
        project search + stats. Best-effort throughout: any failure returns
        a smaller block, never an error.
        """
        if not self.is_available():
            return ""

        # §0 self-update protocol: best-effort version check + marker write.
        _spec_update_check(getattr(self, "_agent_label", "hermes"))

        cwd = Path.cwd()
        session_start_hint = _hv_nudge(
            "session-start",
            session_id=getattr(self, "_session_id", ""),
            cwd=str(cwd),
            agent=getattr(self, "_agent_label", "hermes"),
        )

        # Discover project identity
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
            "CONCLUSIONS/PLANS: only if worth finding later, and pass channel=\"introspect\" to hive_remember; "
            "they count 0 until an observation supports them.\n"
            "TAGS: speculation|observation|confirmed help readers; "
            "they never change how much a fact counts as evidence.\n"
        )

        if session_start_hint:
            block += f"\n## Hive Mind — Session Start\n{session_start_hint}\n"

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

        session_id = session_id or getattr(self, "_session_id", "")
        cfg = _load_nudge_cfg()
        self._prefetch_turns = getattr(self, "_prefetch_turns", 0) + 1
        nudge_hint = ""
        save_every = _cfg_int(cfg, "SAVE_EVERY", 0)
        min_gap = _cfg_int(cfg, "MIN_GAP", 6)
        on_phrase = str(cfg.get("SAVE_ON_PHRASE", "1")).strip().lower() in ("1", "true", "yes")
        low = query.lower()
        phrase_hit = on_phrase and any(p in low for p in _cfg_phrases(cfg))
        cadence_hit = save_every > 0 and (self._prefetch_turns % save_every == 0)
        if phrase_hit or cadence_hit:
            if not self._last_save_turn or (self._prefetch_turns - self._last_save_turn) >= min_gap:
                nudge_hint = _hv_nudge(
                    "user-prompt",
                    session_id=session_id,
                    cwd=str(Path.cwd()),
                    agent=getattr(self, "_agent_label", "hermes"),
                    text=query,
                )
                if nudge_hint:
                    self._last_save_turn = self._prefetch_turns

        # Evaluate read-side triggers for recall, independently of save nudges.
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

        if not any([is_error, is_uncertain, is_knowledge, entity_trigger]) and not nudge_hint:
            return ""

        # Search with the most specific signal first
        search_query = query[:200]  # cap for FTS5
        facts = _hv_search_json(search_query)

        if not facts and not nudge_hint:
            return ""

        # Filter to top 5 by trust, but preserve diversity (don't just top-3)
        facts = facts[:5]

        # Track recalled content (anti-re-ingest gate)
        for f in facts:
            self._recall_set.add(f.get("content", ""))

        # Detect conflicts — surface tension intact
        conflicts = _detect_conflicts(facts)

        lines = []
        if nudge_hint:
            lines.append("## Hive Mind — Save Nudge")
            lines.append(nudge_hint)
            lines.append("")
        lines.append("## Hive Mind — Recalled Context")

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
                    f"(conf={a.get('confidence', a.get('trust_score', 0)):.2f}, source={a.get('source_agent')})\n"
                    f"    B: \"{b.get('content')}\" "
                    f"(conf={b.get('confidence', b.get('trust_score', 0)):.2f}, source={b.get('source_agent')})\n"
                    f"  → Both sides injected. Do not collapse to consensus."
                )
            lines.append("")

        if facts:
            lines.append("Facts (with provenance — treat as signals, not truth):")
            formatted = _format_facts_with_provenance(facts, self._session_id)
            lines.append(formatted)

        return "\n".join(lines).strip()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """The hive tools, at parity with the MCP server (integrations/mcp/hive_mcp.py): the same names,
        the same parameters and the same rubric, so every adapter teaches agents the same capture loop."""
        s = {"type": "string"}
        return [
            {
                "name": "hive_search",
                "description": (
                    "Search the shared hive-mind corpus for facts, decisions, ideas and known gotchas across all "
                    "nodes and agents. Returns rows with confidence and provenance; each row's `sid` (h:…) is the "
                    "stable id to pass to the other tools. Results are signals, not truth: if they conflict, "
                    "surface both and hold the tension. kind='idea' lists every open hypothesis, which you can "
                    "weigh in on with hive_remember(supports=/contradicts=)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {**s, "description": "Search terms. FTS5 syntax supported (AND is default, OR explicit, \"exact phrase\")."},
                        "min_confidence": {"type": "number", "default": 0.0,
                                           "description": "Minimum confidence (0.0-1.0). Default 0.0 (return all)."},
                        "kind": {**s, "enum": ["all", "fact", "decision", "idea"], "default": "all",
                                 "description": "all (facts, decisions, and ideas that have earned confidence), or one kind; "
                                                "'idea' lists every hypothesis, including those still at 0.0."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "hive_remember",
                "description": (
                    "Record a durable, checkable fact: an outcome, correction, constraint or discovery. Search "
                    "first; never write back something you just read, your chain-of-thought, or a restatement. "
                    "Confidence is derived from independent corroboration; never state your own. Pass at most ONE "
                    "of outcome_of, resolves, supports, contradicts: one relationship per write."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {**s, "description": "The fact, in one or two self-contained sentences."},
                        "tags": {**s, "description": "Comma-separated tags (e.g. the project)."},
                        "epistemic_status": {**s, "enum": ["observation", "confirmed", "speculation"], "default": "observation",
                                             "description": "Folded into the tags so readers can weigh the claim."},
                        "outcome_of": {**s, "description": "The sid of a decision this fact is the OUTCOME of."},
                        "polarity": {"type": "integer", "enum": [-1, 0, 1], "default": 1,
                                     "description": "With outcome_of: 1 it worked out, -1 it did not, 0 neutral."},
                        "channel": {**s, "enum": ["sense", "act", "introspect"],
                                    "description": "Leave empty for an observation (counted). 'introspect' for your own "
                                                   "reasoning: recorded, never counted as corroboration or toward an outcome."},
                        "resolves": {**s, "description": "The sid of a wrong fact this corrects; it is soft-retracted (reversible)."},
                        "supports": {**s, "description": "The sid of a fact or idea this OBSERVATION supports. Your own support "
                                                         "of your own idea doesn't count."},
                        "contradicts": {**s, "description": "The sid of a fact or idea this observation contradicts "
                                                            "(the author may contradict their own idea to withdraw it)."},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "hive_decide",
                "description": (
                    "Record a DECISION with its rationale, for choices that shape future work. Search first. Name "
                    "the facts and decisions you retrieved and relied on in informed_by, so the hive learns which "
                    "knowledge matters once the decision's outcomes are recorded."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {**s, "description": "The decision."},
                        "rationale": {**s, "description": "Why."},
                        "tags": {**s, "description": "Comma-separated tags (e.g. the project)."},
                        "informed_by": {**s, "description": "Comma-separated sids (h:…) of what you relied on. An "
                                                            "unresolvable reference aborts the whole write."},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "hive_propose",
                "description": (
                    "Record an IDEA: a hypothesis the hive can support or contradict. It starts at 0.0 and earns "
                    "confidence only from other identities' observations (hive_remember with supports=). Use it "
                    "for 'perhaps X relates to Y', never for something observed. Search first (kind='idea')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {**s, "description": "The hypothesis."},
                        "tags": {**s, "description": "Comma-separated tags."},
                    },
                    "required": ["content"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch the hive tools. hive_search returns {facts, count, conflicts, note}; the write tools return
        {ok, output}, where output is hv's stdout, or on failure its stderr (which names the bad reference or
        the refused combination)."""
        if not hasattr(self, "_writer"):
            return json.dumps({"ok": False, "output": "the hive-mind provider is not initialized"})
        if tool_name == "hive_search":
            return self._tool_search(args)
        if tool_name in ("hive_remember", "hive_decide", "hive_propose"):
            if not str(args.get("content", "")).strip():
                return json.dumps({"ok": False, "output": "content is required"})
            argv, env = getattr(self, f"_argv_{tool_name[5:]}")(args)
            if argv is None:
                return json.dumps({"ok": False, "output": env})
            if tool_name != "hive_decide":
                self._written_this_session.add(str(args["content"]))
            ok, out = self._writer.submit(argv, env=env, wait=True)
            return json.dumps({"ok": bool(ok), "output": out})
        raise NotImplementedError(f"Unknown tool: {tool_name}")

    @staticmethod
    def _tags(args: Dict[str, Any], extra: str = "") -> List[str]:
        tags = [t.strip() for t in str(args.get("tags", "") or "").split(",") if t.strip()]
        if extra and extra not in tags:
            tags.append(extra)
        return ["--tags", ",".join(tags)] if tags else []

    def _argv_remember(self, args: Dict[str, Any]):
        rels = [r for r in _RELATIONSHIPS if str(args.get(r, "") or "").strip()]
        if len(rels) > 1:
            return None, f"pass at most one of {', '.join(_RELATIONSHIPS)} (got {', '.join(rels)}): one relationship per write"
        argv = ["remember", str(args["content"]), "--source", self._source_id]
        argv += self._tags(args, str(args.get("epistemic_status", "observation") or ""))
        if rels == ["outcome_of"]:
            argv += ["--outcome-of", str(args["outcome_of"]).strip(), "--polarity", str(int(args.get("polarity", 1)))]
        elif rels:
            argv += [f"--{rels[0]}", str(args[rels[0]]).strip()]
        if str(args.get("channel", "") or "").strip():
            argv += ["--channel", str(args["channel"]).strip()]
        return argv, None

    def _argv_decide(self, args: Dict[str, Any]):
        argv = ["decide", str(args["content"])]
        if str(args.get("rationale", "") or "").strip():
            argv += ["--rationale", str(args["rationale"])]
        argv += self._tags(args)
        refs = [r.strip() for r in str(args.get("informed_by", "") or "").split(",") if r.strip()]
        if refs:
            argv += ["--informed", *refs]
        # `hv decide` has no --source; it reads HERMES_AGENT (the env EXTENDS os.environ, see _hv).
        return argv, {"HERMES_AGENT": self._source_id}

    def _argv_propose(self, args: Dict[str, Any]):
        return ["propose", str(args["content"]), "--source", self._source_id] + self._tags(args), None

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})
        facts = _hv_search_json(query, str(args.get("kind", "all") or "all"))
        try:
            floor = float(args.get("min_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            floor = 0.0
        if floor > 0.0:
            facts = [f for f in facts if (f.get("confidence") or 0.0) >= floor]

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
                    "conf_a": a.get("confidence", a.get("trust_score", 0)),
                    "source_a": a.get("source_agent"),
                    "fact_b": b.get("content"),
                    "conf_b": b.get("confidence", b.get("trust_score", 0)),
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

        SALIENCE LAYERS (hv core vocabulary; see the L2 header above `_is_admissible` in `hv`):
        - L1  agent rubric — THIS adapter's job: `_salience_gate()` below (stub, passes all) +
              the novelty gate. Judges the structure of the SITUATION, never topic.
        - L2  `hv remember --gate` — the hive's content-neutral structural gate on the TEXT.
        - L3  importance — a learned projection over the journal (since 1.19 PR6; earned from links by
              other identities, never self-asserted). Surprise/consequence weighting and
              earn-your-keep promotion live HERE, not in the adapter.
        MVP stays: mirror every explicit memory() call + novelty gate.
        """
        if self._agent_context not in ("primary", ""):
            return
        if action == "remove":
            return
        if not self.is_available():
            return

        # Novelty gate (salience L1, agent-side): don't re-ingest what we recalled
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

        # Off the turn (#76): queue it on the single writer and return. Marked as this session's own at
        # enqueue, so a prefetch before the write lands still carries the "self-reported" caveat.
        self._written_this_session.add(content)
        if self._writer.submit(["remember", content, "--tags", tags, "--source", source]):
            logger.debug("hive-mind: mirror queued (epistemic=%s source=%s)", epistemic, source)

    def on_session_switch(self, new_session_id: str, *, reset: bool = False, **kwargs) -> None:
        """Reset per-session state on session switch."""
        old_session_id = getattr(self, "_session_id", "")
        if old_session_id and old_session_id != new_session_id and old_session_id != getattr(self, "_last_audited_session", ""):
            self._drain_writes("session switch")    # the old session's audit must see its own queued writes
            cwd = str(Path.cwd())
            nudge_hint = _hv_nudge(
                "sessionend",
                session_id=old_session_id,
                cwd=cwd,
                agent=getattr(self, "_agent_label", "hermes"),
            )
            if nudge_hint:
                logger.info("hive-mind: audit hint for %s: %s", old_session_id, nudge_hint)
            audit_hint = _hv_audit(session_id=old_session_id)
            if audit_hint:
                logger.info("hive-mind: audit for %s: %s", old_session_id, audit_hint)
            _hv_telemetry("end", session_id=old_session_id,
                          identity=getattr(self, "_agent_identity", ""), cwd=cwd)
            self._last_audited_session = old_session_id

        self._session_id = new_session_id
        self._source_id = _build_source_id(self._agent_identity, new_session_id, self._context_class)
        self._prefetch_turns = 0
        self._last_save_turn = 0
        if reset:
            self._recall_set = set()
            self._written_this_session = set()

    def _salience_gate(self, content: str, metadata: Optional[Dict[str, Any]]) -> bool:
        """Salience L1 — the agent rubric: "should I say this at all?"

        STUB — always returns True (pass). Explicit memory() calls are already intentional.

        The rubric, when implemented, judges the structure of the SITUATION (never the topic):
        - PASS high-reuse structural forms: decisions+rationale, corrections (esp. owner
          corrections), outcomes/results, constraints/preferences/commitments, new
          entities/relationships, first-hand tool results.
        - REJECT: intermediate reasoning, restatements of known facts, speculation/opinion,
          pleasantries, anything already in the corpus (the novelty gate handles the last).

        L1 is owned by the agent author and runs before any `hv` call. L2 (`hv remember --gate`,
        `_is_admissible`) is the hive's content-neutral check on the TEXT and is shared by every
        agent. L3 (importance) is a projection over the journal and is not the adapter's concern.

        Do NOT implement a learned importance classifier here — that reintroduces the
        capturable authority the confidence model is designed to prevent.
        """
        return True  # MVP: pass everything (explicit memory() calls are already intentional)

    def _drain_writes(self, why: str) -> None:
        """Let queued writes land (at most 5 s) before a session-end nudge and audit, so they see them."""
        writer = getattr(self, "_writer", None)
        if writer is not None:
            pending = writer.drain(5.0)
            if pending:
                logger.warning("hive-mind: %s with %d write(s) still pending after 5 s", why, pending)

    def shutdown(self) -> None:
        self._drain_writes("shutdown")
        session_id = getattr(self, "_session_id", "")
        if session_id and session_id != getattr(self, "_last_audited_session", ""):
            cwd = str(Path.cwd())
            nudge_hint = _hv_nudge(
                "sessionend",
                session_id=session_id,
                cwd=cwd,
                agent=getattr(self, "_agent_label", "hermes"),
            )
            if nudge_hint:
                logger.info("hive-mind: shutdown audit hint for %s: %s", session_id, nudge_hint)
            audit_hint = _hv_audit(session_id=session_id)
            if audit_hint:
                logger.info("hive-mind: shutdown audit for %s: %s", session_id, audit_hint)
            _hv_telemetry("end", session_id=session_id,
                          identity=getattr(self, "_agent_identity", ""), cwd=str(Path.cwd()))
            self._last_audited_session = session_id
