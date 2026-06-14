# HiveMind Agent Integration Spec

`Contract-Version: 1.6`  *(SemVer `MAJOR.MINOR`; authoritative value: `hv version`)*

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
| `hv retract <id> [--owner]` | Negative evidence / owner-forget (`--owner` is decisive and, once an owner exists, requires + applies the owner signature) |
| `hv nudge --event=<E> [--session=<id>] [--cwd=<dir>]` | Emit a save/audit hint or a startup digest (reads recent text on **stdin**, prints a terse hint to **stdout**, or nothing) |
| `hv audit [--depth light\|normal\|deep] [--format json] [--session=<id>]` | Surface redundant / obsolete / missing facts |
| `hv telemetry record --event=start\|end --agent=<you> --identity=<instance> --session=<id> [--cwd=<dir>]` | **(optional, since 1.1)** record session observability into the LOCAL telemetry lane |

**Contract invariants you can rely on:** `hv nudge`/`hv audit` are best-effort and print to
stdout (empty = no nudge); `--source` is your stable identity; confidence rises only from
*independent* corroboration (never from one agent repeating itself). Confidence is a **governed
projection** — its derivation parameters are owner-signed and journaled (`hv config`) — but it is
still derived, never declared: you read it, you never set it.

Hive membership and governance are **device/owner-level, outside the per-session adapter loop**:
onboarding (`hv discover` / `join` / `admit`, and the `hive_id` that scopes sync) and confidence
governance (`hv config`) are not behaviors you wire — see `docs/CLI_REFERENCE.md`.

---

## 2. The four behaviors to wire

Map each behavior to the matching point in **your** runtime's lifecycle (§3). For each, the `hv`
call is fixed; you provide the plumbing (where the text comes from, where the output goes).

1. **Reorient on start, and audit-on-boot.** At session start: run the §0 self-update check, then
   `hv nudge --event=session-start --cwd="<cwd>"` and inject its stdout. That output carries the
   project digest **and** a short audit-on-boot line (what the previous session left to re-check,
   stale, or duplicate). Audit-on-boot is the reliable floor of the whole loop, because session
   start fires on every runtime even when shutdown and per-turn hooks do not. Surface it so the new
   session can reconcile.
2. **Capture.** When a decision, outcome, correction, or constraint occurs, write it:
   `hv remember "..." --tags ... --source <you>`. Search first; never write back something you
   just read this session (no echoes). **Tag time-varying/operational facts `volatile`**
   (optionally `ttl:<n>h|d`), for example "service running" or "host reachable", so the audit flags
   them for re-verification instead of trusting them indefinitely. Since 1.2 `hv remember`
   **auto-tags** transient-status claims `volatile` (high-precision, content-neutral; pass
   `--no-volatile` to opt out, or an explicit `ttl:` to set the window) — you can rely on it, but
   tagging explicitly is still good practice.
3. **Save-nudge (best-effort, not a guarantee).** Where your runtime has a per-turn pre-prompt
   hook, pipe the user's message to `hv nudge --event=user-prompt --session="<id>"` and inject any
   stdout. Gate cheaply first: skip the call on turns that are neither a phrase hit nor a cadence
   checkpoint, so a quiet turn costs no process. It is phrase-first by default (turn-cadence is
   opt-in via `SAVE_EVERY`); `hv` owns the precise debounce. If your runtime has no per-turn hook,
   wire this to whatever in-session events you have (§3) and lean on audit-on-boot as the net.
4. **Audit-nudge (bonus).** Before context is lost (pre-compaction) and at session end, run
   `hv nudge --event=precompact` / `--event=sessionend` and inject any stdout; act on it by running
   `hv audit` and reconciling redundant, obsolete, recheck (volatile past freshness), and missing.
   These two hooks are best-effort: a hard kill skips them, so they sit on top of audit-on-boot
   (behavior 1), which is the actual guarantee.
5. **Telemetry (OPTIONAL, since 1.1).** If you want session observability, call
   `hv telemetry record --event=start` at session start and `--event=end` at session end, passing a
   stable `--agent` and `--identity` (your instance discriminator) plus `--session`/`--cwd`. This is
   pure observability — token usage, duration, cost — written to a LOCAL-ONLY lane that **never enters
   the corpus, the journal, or sync**. It has no effect on knowledge or confidence; skip it entirely
   if you do not want it. Never write telemetry as a `hv remember` fact.

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

If your runtime lacks one of these points, wire what you can and skip the rest; partial is fine.
The per-turn save-nudge especially is optional. Where there is no per-turn hook, wire it to whatever
in-session events you have and rely on audit-on-boot. Capture does not depend on the per-turn nudge.

---

## 4. Safety invariants — MANDATORY

Your adapter **must**:

- **Be best-effort.** Never block, never error, never slow a session. Wrap everything; the hook
  process must `exit 0` even on failure. A missing/broken `hv` must be a no-op, not a crash.
- **Hint, never act.** Nudges and audits only *prompt*. You never auto-write and never
  auto-delete. **You remain the salience judge**; erasing/forgetting is the **owner's** decision.
- **Install once, persist.** Wire your adapter a single time and keep it. Do **not** regenerate
  the live hook every session — re-wire only when §0 detects a `Contract-Version` MAJOR bump. (Stability +
  safety: a per-turn hook that must never break the session cannot be a fresh guess each boot.)
- **Use a stable, distinct `--source`.** So independent corroboration across agents works
  (e.g. `claude-code`, `claude-ai`, `hermes:...`). Two agents agreeing must look like two sources.
  `--source` is a human label that distinguishes apps/agents *on one device*; the device itself is
  identified cryptographically (an Ed25519 key — see `hv key` and INTERNALS "Device identity"), and
  entries are signed under it. You set the label; you do not get to assert which device you are.

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

**Changelog.**
- `1.6` — owner resilience (pt.1). `hv owner export`/`import` back up and restore the owner key to
  an off-device file (optionally passphrase-encrypted) so a lost owner device can resume the SAME
  owner identity; `hv owner escrow`/`restore` store the key passphrase-encrypted IN the hive (it
  syncs to every node, so any synced device can recover it with the passphrase); `hv owner standby
  <device_id>` records an advisory standby. Additive, governance-only; adapters are unaffected (they
  never act as owner). Succession + quorum recovery land in later minors.
- `1.5` — membership **lifecycle** + a unified settings surface. New owner-only `hv group`
  verb groups the admission lifecycle: `admit / revoke / deny / change / purge / list`
  (revoke is reversible; purge is a final tombstone — its entries stay in the journal but stop
  counting and it can't be re-admitted). `hv config` gains `identity` (this device's key) and
  `confidence` (the params) sub-namespaces. `hv key`, `hv admit`, and `hv config set …` are kept
  as silent aliases. Additive: adapters still only *read* derived confidence; nothing to re-wire.
- `1.4` — confidence is now a **governed projection**: its derivation parameters (caps, decay,
  same-source discount) are owner-signed and journaled via the new `hv config` verb, and owner-forget
  (`hv retract --owner`) is cryptographically authorized once an owner exists. Additive: adapters
  still only *read* derived confidence and never set it; nothing to re-wire.
- `1.3` — cryptographic **device identity**: `node_id` is an Ed25519 device-key fingerprint
  (`k1:` + `sha256(pub)[:16]`) and entries are signed under it (see §4, `hv key`). Additive: your
  `--source` label is unchanged; the device is identified for you.
- `1.2` — `hv remember` auto-tags transient operational-status claims `volatile` (new optional
  `--no-volatile` flag). Additive: existing calls keep working; you just get smarter freshness tagging.
- `1.1` — added the optional `hv telemetry` verb (behavior 5). Additive and backward-compatible:
  a `1.0` adapter keeps working unchanged; wire telemetry only if you want it.

---

---

## 8. Cost and inference

The checks use no model, and they should stay that way: the save-nudge gate, the audit (FTS plus
identity for redundancy, confidence plus TTL for obsolete and recheck), and the version check are
all deterministic code, which is cheaper than any model. Gate cheaply before you spend anything: a
quiet turn should cost a counter and a string scan, not a process.

The one place a model helps is the optional, deferred inference phase: reading a transcript to find
decisions that were made but never written, semantic dedup, short summaries. When you build it, run
it on a cheap, configurable model (a `BACKGROUND_MODEL` knob, cheap by default, separate from the
in-session model), keep it optional and local-first (the deterministic path must work with no model
and no API key), and treat its output as proposals or low-confidence machine-tagged writes that a
real agent or the owner confirms. A background model never self-certifies a fact into being trusted.

---

*One brain (`hv`), one spec (this file), one reference (`scripts/nudge_hook.sh`). Everything else an
agent writes for itself, and keeps current by re-reading this spec when its version changes.*
