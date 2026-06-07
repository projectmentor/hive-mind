# HiveMind Agent Integration Spec

`Contract-Version: 1.0`  *(SemVer `MAJOR.MINOR`; authoritative value: `hv version`)*

> **Audience: any AI agent** (Claude Code, Hermes, OpenClaw, an MCP host, any CLI agent).
> You are reading this because you are joining a HiveMind — a shared, local-first memory.
> This spec tells you how to wire **your** runtime to it. **You write your own adapter from
> this spec.** There is no hand-maintained, per-agent integration code: there is one brain
> (`hv`), this one spec, and one reference implementation. You generate the rest for your
> runtime, once, and keep yourself current by re-reading this spec when its version changes.

When the contract changes, the `Contract-Version` and the relevant section move. A **MINOR** bump
is additive and backward-compatible — your existing adapter keeps working, no re-integration needed.
A **MAJOR** bump is breaking and triggers re-integration (see §0 and §7). Maintainers bump MINOR for
additions, MAJOR only for breaking changes.

---

## 0. Self-update protocol — run on EVERY boot

The session-start integration you build in §2 **must itself perform this check** (so once
installed, it keeps you current with no human in the loop). All steps are best-effort — never
block or break a session:

1. Make your local hive current. Your install's update path keeps the repo fresh (a periodic
   `hive-mind update` / git pull); a best-effort `git -C "$HIVE_HOME" pull --ff-only` is fine on a
   deployed checkout, but **skip auto-pull on a dev checkout** with local changes. Offline is fine.
2. Read the current `Contract-Version` — authoritatively from `hv version`, else this spec's header.
3. Compare it to the version you last integrated against (marker `"$HIVE_HOME/.nudge_state/<agent>.spec"`).
4. **Same** → you are up to date; do nothing (the fast path, every normal boot).
5. **MINOR bump** (same MAJOR) → additive and backward-compatible; you keep working — record the new
   version, optionally skim the changes. **MAJOR bump, or first run** → re-read this spec, (re)wire per
   §2–§3, run the §5 self-verify, and on success record the new version. (The reference
   `hv nudge --event=session-start` already emits the re-integrate nudge on a MAJOR bump.)

This is the one mechanism that makes integration self-updating: you are not regenerated every
session (that would be unstable — see §4), but you never silently fall behind the spec either.

`$HIVE_HOME` defaults to `~/projects/hive-mind`.

---

## 1. The contract — `hv`, the one brain

All logic lives in the `hv` CLI (`$HIVE_HOME/hv`); your adapter only *calls* it. Stable verbs:

| Call | Purpose |
|---|---|
| `hv search "<q>" [--format json] [--min-confidence N]` | Read the corpus (ranked by effective confidence) |
| `hv remember "<fact>" --tags a,b --source <you>` | Write a fact (confidence is DERIVED, never set by you) |
| `hv decide "<decision>" --rationale "<why>"` | Record a decision |
| `hv retract <id> [--owner]` | Negative evidence / owner-forget |
| `hv nudge --event=<E> [--session=<id>] [--cwd=<dir>]` | Emit a save/audit hint or a startup digest (reads recent text on **stdin**, prints a terse hint to **stdout**, or nothing) |
| `hv audit [--depth light\|normal\|deep] [--format json] [--session=<id>]` | Surface redundant / obsolete / missing facts |

**Contract invariants you can rely on:** `hv nudge`/`hv audit` are best-effort and print to
stdout (empty = no nudge); `--source` is your stable identity; confidence rises only from
*independent* corroboration (never from one agent repeating itself).

---

## 2. The four behaviors to wire

Map each behavior to the matching point in **your** runtime's lifecycle (§3). For each, the `hv`
call is fixed; you provide the plumbing (where the text comes from, where the output goes).

1. **Reorient on start.** At session start: run the §0 self-update check, then
   `hv nudge --event=session-start --cwd="<cwd>"` and inject its stdout into your context so you
   begin oriented to the project. (Equivalently, `hv search "<project>"`.)
2. **Capture.** When a decision, outcome, correction, or constraint occurs, write it:
   `hv remember "..." --tags ... --source <you>`. Search first; never write back something you
   just read this session (no echoes).
3. **Save-nudge.** On each user turn, pipe the user's message to
   `hv nudge --event=user-prompt --session="<id>"` and inject any stdout. It is debounced and
   phrase/cadence-driven inside `hv`; you just feed it text.
4. **Audit-nudge.** Before context is lost (pre-compaction) and at session end, run
   `hv nudge --event=precompact` / `--event=sessionend` and inject any stdout; act on it by
   running `hv audit` and reconciling what it surfaces.

---

## 3. Map to your runtime (guidance, not code)

Find your runtime's lifecycle hooks — **session-start, per-turn pre-prompt, pre-compaction,
session-end** — and bind the §2 behaviors to them. The reference implementation for Claude Code
is real, working code in this repo — **study it, then write the equivalent for your runtime:**

- **Claude Code (reference):** `~/.claude/settings.json` hooks (`SessionStart`,
  `UserPromptSubmit`, `PreCompact`, `SessionEnd`) → `command` runs
  `scripts/nudge_hook.sh <event>`. See `scripts/nudge_hook.sh` (best-effort stdin parsing → `hv`).
- **Hermes:** plugin lifecycle (`initialize`, `system_prompt_block`, `prefetch`,
  `on_session_switch`, `shutdown`). No per-turn pre-prompt hook exists → wire the save-nudge into
  `prefetch`, reorient into `system_prompt_block` (it already searches the hive), and the
  audit-nudge into `on_session_switch`/`shutdown`.
- **OpenClaw:** plugin SDK hooks — `before_prompt_build` (inject digest/nudge via
  `prependContext`), `gateway_stop` (audit). Push-capable; maps cleanly.
- **MCP host:** pull-only — expose `hive_nudge`/`hive_audit` as tools and call them at your
  checkpoints (you cannot push per-turn).
- **Any CLI agent:** a thin wrapper that calls `hv` at the points you control.

If your runtime lacks one of the four points, wire what you can and skip the rest — partial is fine.

---

## 4. Safety invariants — MANDATORY

Your adapter **must**:

- **Be best-effort.** Never block, never error, never slow a session. Wrap everything; the hook
  process must `exit 0` even on failure. A missing/broken `hv` must be a no-op, not a crash.
- **Hint, never act.** Nudges and audits only *prompt*. You never auto-write and never
  auto-delete. **You remain the salience judge**; erasing/forgetting is the **owner's** decision.
- **Install once, persist.** Wire your adapter a single time and keep it. Do **not** regenerate
  the live hook every session — re-wire only when §0 detects a `Spec-Version` bump. (Stability +
  safety: a per-turn hook that must never break the session cannot be a fresh guess each boot.)
- **Use a stable, distinct `--source`.** So independent corroboration across agents works
  (e.g. `claude-code`, `claude-ai`, `hermes:...`). Two agents agreeing must look like two sources.

---

## 5. Self-verify checklist — run after (re)wiring

Only record the new `Spec-Version` to your marker file if all pass:

1. `hv stats` and `hv search test` succeed.
2. Your session-start path prints a project digest (or nothing, quietly) and the session is unharmed.
3. A user-turn with a watched phrase (see `nudge.env`) produces a one-line nudge, and the hook `exit 0`s.
4. Malformed/empty input to your hook → still `exit 0`, session unaffected.
5. Temporarily make `hv` unavailable → your session still starts and runs normally (best-effort proven).

---

## 6. Config

Per-node tuning lives in `nudge.env` (copy from `nudge.env.example`; mirrors the `.peers.json`
convention): `SAVE_EVERY`, `MIN_GAP`, `SAVE_ON_PHRASE`, `AUDIT_ON`, `AUDIT_DEPTH`,
`HIVE_NUDGE_PHRASES`. `hv` reads it for you — your adapter does not need to parse it. Honor it by
simply routing text through `hv nudge`/`hv audit`.

---

---

## 7. Versioning & backward compatibility

The contract is **SemVer (`MAJOR.MINOR`)**, reported by `hv version`:

- **MINOR** — additive only: new verbs, new *optional* flags, new output fields. Existing behavior
  never changes within a major, so an adapter written for any `N.x` keeps working on every later
  `N.y`. No re-integration required.
- **MAJOR** — breaking (a verb/flag removed or repurposed, an output format changed). Bumped only
  when unavoidable. On a major bump: `hv` keeps the **previous major working as deprecated shims**
  through a migration window; §0 fires a loud re-integrate nudge; and — because every adapter call
  is best-effort and `exit 0` (§4) — an un-migrated adapter **degrades gracefully** (its nudges
  silently no-op) rather than crashing. Migrate to the new major within the window.

This is what makes future breaking changes safe: additive-within-major keeps old adapters running,
the deprecation window + graceful degradation prevent hard breakage, and §0 tells each agent exactly
when it must re-wire.

---

*One brain (`hv`), one spec (this file), one reference (`scripts/nudge_hook.sh`). Everything else
an agent writes for itself — and keeps current by re-reading this spec when its version changes.*
