# HiveMind Agent Integration Spec

`Contract-Version: 1.28`  *(SemVer `MAJOR.MINOR`; authoritative value: `hv version`)*

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
   A pull alone leaves the sync daemon on its old code: when it moved `HEAD`, run `hive-mind update`
   (or leave it to the 15-minute doctor timer, whose `daemon-code` check restarts a stale daemon, #112).
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
| `hv search "<q>" [--format json] [--min-confidence N] [--kind all\|fact\|decision\|idea] [--sort confidence\|importance\|utility\|recency]` | Read the corpus (ranked by effective confidence by default) |
| `hv remember "<fact>" --tags a,b --source <you>` | Write a fact (confidence is DERIVED, never set by you) |
| `hv remember "<outcome>" --source <you> --outcome-of <decision sid> --polarity 1|0|-1` | Record what happened after acting on a decision *(1.19)* |
| `hv decide "<decision>" --rationale "<why>" --informed <sid>… --source <you>` | Record a decision, naming the entries it relied on by `sid` (`h:…`) *(1.19)* |
| `hv propose "<hypothesis>" --tags a,b --source <you>` | Record an idea — it can earn confidence only from others' evidence, never from you *(1.20)* |
| `hv retract <sid> [--owner]` | Negative evidence / owner-forget (`--owner` is decisive and, once an owner exists, requires + applies the owner signature) |
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
   project digest, up to three open ideas, **and** a short audit-on-boot line (what the previous session left to re-check,
   stale, or duplicate). Audit-on-boot is the reliable floor of the whole loop, because session
   start fires on every runtime even when shutdown and per-turn hooks do not. Surface it so the new
   session can reconcile.
2. **Capture.** Search first, and never write back something you just read this session (no
   echoes). Then route by what happened:
   - a **decision** → `hv decide "<decision>" --rationale "<why>" --informed <sid> [<sid>…] --source <you>`,
     naming the entries you searched and relied on (their `sid`, `h:…`, from `hv search`);
   - the **result of acting on a recorded decision** → `hv remember "<what happened>"
     --outcome-of <decision sid> --polarity 1|0|-1`. Only observed results count toward the
     decision's outcome score; if it is your own assessment rather than an observation, add
     `--channel introspect` (recorded, never counted);
   - a **hypothesis** worth testing → `hv propose "<hypothesis>"`, an idea, never a fact;
   - an **observation that bears on an idea or a fact** (for example one listed as an open idea in the
     digest) → `hv remember "<what you observed>" --supports <sid>` or `--contradicts <sid>`. It is
     evidence only from an identity other than the idea's author, and only on the `sense` channel;
   - something that **builds on or comments on another entry** (a fact, idea or decision) without
     being evidence for or against it → `hv remember "..." --extends <sid>` *(1.22)*. It records the
     relationship and is never weighed toward confidence, on any channel;
   - a correction, constraint, observation or new entity → `hv remember "..." --tags ...
     --source <you>`.

   Label your own reasoning or plans `--channel introspect` if you record them at all: reasoning is
   not observation, so an `introspect` fact is recorded but never corroborates. **Tag time-varying/operational facts `volatile`**
   (optionally `ttl:<n>h|d`), for example "service running" or "host reachable", so the audit flags
   them for re-verification instead of trusting them indefinitely. Since 1.2 `hv remember`
   **auto-tags** transient-status claims `volatile` (high-precision, content-neutral; pass
   `--no-volatile` to opt out, or an explicit `ttl:` to set the window) — you can rely on it, but
   tagging explicitly is still good practice. **Reconcile, do not just append.** When what you
   write *contravenes* a live fact — an issue you closed, a status that flipped, a claim now shown
   wrong — reconcile it rather than leaving both. The clean way is one command:
   `hv remember "<correction>" --resolves <sid>`, which writes the correction plus one `resolves`
   link and soft-retracts the old fact (deliberate negative evidence, reversible). If you instead
   name the target in prose (`resolves h:3f9a1c0b2d`, `supersedes …`, `obsoletes …`,
   `CORRECTION to …`) without `--resolves`, the audit's **CONTRAVENED** check flags it while the
   target is still live, so a missed reconcile surfaces at the next session start. Cite targets by
   `sid`: the audit matches a prose `h:…` exactly, while a prose `#N` is a **local id** that drifts
   across rebuilds and nodes (best-effort). A "resolved" fact written while the "open" fact stays
   live asserts both at once, which is worse than either alone. `--resolves` and a bare
   `hv retract` are soft and reversible; a decisive `--owner` forget stays the owner's call (§4).
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

- **Claude Code (reference):** `~/.claude/settings.json` registers ONE stable shim per lifecycle
  event (`SessionStart`, `UserPromptSubmit`, `PreCompact`, `SessionEnd`) → `command` runs
  `scripts/common/hive_dispatch.sh <event>`. The shim consumes the hook's stdin once, replays it to each
  behavior for that event (session telemetry, then the nudge/digest in `scripts/common/nudge_hook.sh`), and
  passes their stdout through so context injection still works. Because the foreign config holds only
  the shim, **what** runs per event is a source-controlled decision, not a `~/.claude` edit — you
  don't hand-wire it: `hv` owns the shim spec, the installer/update wire it via `hv wire claude`,
  and `hv doctor --fix` (the 15-min self-heal timer) re-asserts/migrates it. Build the same
  shim-then-reconcile pattern into any foreign-config adapter rather than wiring once and hoping it
  stays; an adapter that *is* plugin code (below) already has its wiring in source and needs no shim.
- **Hermes:** plugin lifecycle (`initialize`, `system_prompt_block`, `prefetch`,
  `on_session_switch`, `shutdown`). No per-turn pre-prompt hook exists → wire the save-nudge into
  `prefetch`, reorient into `system_prompt_block` (it already searches the hive), and the
  audit-nudge into `on_session_switch`/`shutdown`. Tools: `hive_search` (with `kind`), `hive_remember`,
  `hive_decide`, `hive_propose`, the same names and parameters as the MCP server (a test pins the
  parity). Every write, the `memory()` mirror and the tools alike, runs on one bounded background
  writer in order: the mirror returns at once, the tools wait for their result, and `shutdown` drains
  the queue (at most 5 s) before the session-end nudge and audit. The mirror stamps no channel (so
  `sense`): at `memory()` time the adapter can't tell an observation from reasoning.
- **OpenClaw** (no adapter ships; guidance for writing one): plugin SDK hooks —
  `before_prompt_build` (inject digest/nudge via `prependContext`), `gateway_stop` (audit).
  Push-capable; maps cleanly.
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
  Writes and searches take well under a second since v1.20.1 ([#70](https://github.com/projectmentor/hive-mind/issues/70)), but still run writes in
  the background and never block a turn on one. If the local store is busy, a write reports that it
  was journaled and exits 0: do not retry it.
- **Hint, never act.** Nudges and audits only *prompt*. You never auto-write and never
  auto-delete. **You remain the salience judge** (salience layer 1 — the agent rubric; `--gate` is
  the hive's content-neutral layer 2); erasing/forgetting is the **owner's** decision.
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

Only record the new `Contract-Version` to your marker file if all pass:

1. `hv stats` and `hv search test` succeed.
2. Your session-start path prints a project digest (or nothing, quietly) and the session is unharmed.
3. A user-turn with a watched phrase (see `nudge.env`) produces a one-line nudge, and the hook `exit 0`s.
4. Malformed/empty input to your hook → still `exit 0`, session unaffected.
5. Temporarily make `hv` unavailable → your session still starts and runs normally (best-effort proven).

---

## 6. Config

Per-node tuning lives in `nudge.env` (copy from `config/nudge.env.example`; mirrors the `.peers.json`
convention): `SAVE_EVERY`, `MIN_GAP`, `SAVE_ON_PHRASE`, `AUDIT_ON`, `AUDIT_DEPTH`,
`VOLATILE_TTL_HOURS` (freshness window for `volatile` facts), `OPEN_IDEA_THRESHOLD` (1.20: ideas
below this effective confidence are listed as "open" in the digest), `HIVE_NUDGE_PHRASES`. `hv` reads it for you — your adapter does not need to parse it. Honor it by
simply routing text through `hv nudge`/`hv audit`.

---

## 7. Versioning & backward compatibility

The contract is **SemVer (`MAJOR.MINOR`)**, reported by `hv version`:

- **MINOR** — additive **for the adapter surface**: new verbs, new *optional* flags, new output
  fields. The verbs, flags and outputs an adapter calls keep working within a major, so an adapter
  written for any `N.x` keeps working on every later `N.y`; no re-integration required. A MINOR may
  still change how the hive itself governs, encrypts or authorizes (1.14 retired an old escrow
  format; 1.17 made publishing cells owner-only); such changes are called out in the changelog below
  and never require an adapter change.
- **MAJOR** — breaking (a verb/flag removed or repurposed, an output format changed). Bumped only
  when unavoidable. On a major bump: `hv` keeps the **previous major working as deprecated shims**
  through a migration window; §0 fires a loud re-integrate nudge; and — because every adapter call
  is best-effort and `exit 0` (§4) — an un-migrated adapter **degrades gracefully** (its nudges
  silently no-op) rather than crashing. Migrate to the new major within the window.

This is what makes future breaking changes safe: additive-within-major keeps old adapters running,
the deprecation window + graceful degradation prevent hard breakage, and §0 tells each agent exactly
when it must re-wire.

**Changelog.** The full per-version record is [`CONTRACT_HISTORY.md`](CONTRACT_HISTORY.md).
- `1.28` — **a new hive starts closed** (#135 part 1). Nothing an adapter calls changes: no verb, flag or
  output format moves. `hv owner init` now re-issues every forget that was in effect only because it predates
  the genesis owner as an ordinary owner-signed `retract`, then sets `forget_writers=owner`, so a hive born
  here never depends on the pre-genesis grandfather; it prints which facts it re-issued. If any of that fails
  the policy is left at `legacy` and the message says what is still open, so a hive is never left closed with a
  fact silently back. `hv doctor`'s advisory `forget-authz` now reports `closed` even when there is nothing
  left to ignore. No journal or wire change: everything written is an owner-signed `retract` plus the 1.25
  `set-config` act, and a pre-1.25 peer simply keeps its own default for that key.
- `1.27` — **the genesis declaration is pinned** (security). Nothing an adapter calls changes: no verb, flag or
  output format moves. Two effects worth knowing. A node that has not pinned its genesis answers `403` to a
  remote `/sync/hello`, `/sync/chunk` and `/sync/ingest` until the operator pins (`hv owner pin --set`, or
  `hive-mind update`, or the periodic `hv doctor --fix`) — loopback, `/hive/info` and `/sync/merkle-root` are
  never gated, so an adapter on the node itself is unaffected. And an entry with no signature is now refused
  when it would extend or rewrite a device's chain, so any adapter that writes through `hv` on a keyed node
  (every supported one) is unaffected, while a hand-built unsigned entry is not accepted.
- `1.26` — **a peer proves who answered** (#107): `/sync/hello` and `/hive/info` carry a responder
  signature (`hello_sig`) over the caller's nonce, and sync `protocol_version` is 3. `hv sync auth --outbound
  off|permissive|enforce` (default `permissive`) decides whether this node pushes only to verified peers;
  `hv doctor` gains the advisory `peer-identity`. Operator-only; adapters are unaffected. A foreign sync client
  may ignore the field. No journal change.
- `1.25` — **owner forgets can require a signature** (#122): a governed config key, `hv config set
  forget_writers owner`, closes the pre-genesis grandfather. Owner-only, no new command or flag; adapters
  are unaffected. No wire change.
- `1.24` — **revoke a decision** (#45, additive flag). `hv decide --revoke <sid> --rationale "<why>"` (MCP and Hermes
  `hive_decide(revoke=…)`) withdraws a decision that was wrong, with no replacement: one decision tagged
  `revocation` plus one `supersedes` link. `content` is optional only with `--revoke`, and a rationale is
  required. Like any `supersedes`, it takes effect only when the link is hard: a person on the owner
  device, or the device that wrote the decision. **Read the confirmation**: an agent's revoke is usually
  `recorded …; NOT in effect` until the owner re-runs it. `hive_decide` also gains `supersedes`, which
  had no MCP or Hermes path before. `hv unforget` (#46) reverses an owner forget; it is owner-only and
  not on MCP, so adapters are unaffected. No wire change.
- `1.23` — **links carry owner authority only when a person writes them** (#114, additive flag).
  `hv decide` and `hv entity link` gain `--source` (the MCP server passes `claude-ai`; Hermes keeps
  `HERMES_AGENT`, still honoured). **Pass `--source <you>` on every write verb**: an omitted source is
  `manual`, a person. On a machine holding the owner key, only `manual` writes are owner-signed; every
  agent's `supersedes`/`resolves`/`supports`/… link is device-signed and, against another device's
  entry, weighs as evidence instead of commanding (the member-device rule). All eight link kinds now go
  through one builder (`outcome-of` and `informed` included). Write confirmations add `(owner-signed:
  source manual)` or `(device-signed: source <src>)` to each link line. No wire change; a 1.22 node
  projects the same journal identically.
- `1.22` — **`extends` link kind** (#115, additive). `hv remember "…" --extends <sid>` and MCP
  `hive_remember(extends=…)` write the fact plus one `extends` link to a fact, idea or decision
  (resolved and kind-checked before anything is written; a decision target is allowed). It says
  "builds on" without evidence semantics: the projection keeps it as an edge row in `links` and never
  weighs it toward confidence on any channel. Mutually exclusive with `--resolves`, `--outcome-of`,
  `--supports` and `--contradicts` (one relationship per write). `hv search` text shows
  `extends h:…` on the row; JSON rows are unchanged. No wire change, no version skew: a pre-1.22 node
  lands the link and projects it to nothing.
- `1.21` — **the grounding rule covers fact assertions** (#73). A `fact` written with
  `--channel introspect` now weighs `introspect_support_weight` (default 0) toward confidence, the same
  as an `introspect` link: two agents restating the same reasoning no longer corroborate each other.
  The fact still lands and can be searched, linked and supported; it sits at confidence 0 until an
  observation (a `sense` assertion or `supports` link from another identity) backs it. `hv remember`
  prints a note when it writes one. No verb, flag or output-schema change for this; adapters keep
  working.
  **Unrecognised labels fail closed** (#72): a `channel` outside `sense`/`act`/`introspect`
  (reachable only through raw journal entries) counts as `introspect` everywhere, so a typo or a newer
  label never earns observation weight; an unrecognised source class keeps weight 1.0, like an absent
  one, because a class can only claim a discount. **MCP parity** (#75, additive): `hive_remember`
  gains `resolves`, the MCP form of `hv remember --resolves` (write a correction and soft-retract the
  wrong fact in one step); `tests/test_mcp_parity.py` now guards every agent-facing `hv remember`
  flag. **Evidence writers** (#71): `hv remember "<observation>" --supports <sid>` / `--contradicts
  <sid>` and MCP `hive_remember(supports=…, contradicts=…)` write the fact plus one `supports` or
  `contradicts` link to a fact or idea (kind-checked before anything is written; a decision points
  you to `--outcome-of`). The target is re-scored immediately. An idea's author can't support their own
  idea (the same principal weighs 0) but can contradict it, the only way to withdraw one. One
  relationship per write: `--resolves`, `--outcome-of`, `--supports` and `--contradicts` are mutually
  exclusive (argparse), so `--resolves` together with `--outcome-of`, previously accepted, is now
  refused. **Search JSON** (#77, additive): every `hv search --format json` row gains `tag_list`, the
  tags as a list; `tags` stays the JSON-encoded string for existing consumers and **becomes the list at
  2.0** (read `tag_list` now). Version skew: a 1.20 node keeps counting introspect facts, unknown channels and an idea author's self-support until it
  upgrades (confidence differs across the fleet; the journal and Merkle root do not).
- `1.20` — **`idea` journal type.** A hypothesis whose confidence is *earned*, never asserted:
  `hv propose` / `hive_propose` journal an `idea` (channel defaults to `introspect`); it starts at
  confidence 0.0 in a new `ideas` table and moves only via `supports`/`contradicts` links from other
  identities on the `sense` channel (`introspect` links weigh `introspect_support_weight`, default 0).
  Identity is the journal entry, not the text — a restated idea is a second idea, never
  corroboration, and ideas never corroborate facts. `hv search --kind {all,fact,decision,idea}` /
  `hive_search(kind=…)`: under `all` an idea surfaces only once it has earned confidence > 0. The
  session-start digest lists up to three open ideas; a peer's idea arriving by sync appends an
  `idea-arrived` line to the local bus log. **No wire change, no version skew** (older nodes land an
  `idea` and ignore it). Rubric: a hypothesis worth testing is an idea, not a fact. In 1.20 nothing
  could write the `supports`/`contradicts` links an idea needs; 1.21 adds them (#71).
- `1.19` — **generic `link` journal type (read side).** One content type with an open `kind`
  vocabulary for relationships between entries: `{kind, from_ref, to_ref, data, source, channel?}`
  with journal-identity refs. The projection knows `supports`, `contradicts`, `supersedes`,
  `resolves`, `entity`, `informed`, `outcome-of`; an unknown kind lands and projects to nothing.
  Links are **evidence, not commands**: `supersedes`/`resolves` command only when owner-signed for
  the owner-at-that-position or written by the target's author; otherwise they weigh-only
  (`hv doctor link-authz` lists them). `supports`/`contradicts`/`resolves` fold into confidence as
  identity-weighted evidence; a link with `channel: introspect` weighs the new governed knob
  `introspect_support_weight` (default 0). New `links` table in store.db. **No wire change, no
  version skew** (a 1.18 node lands a `link` and ignores it); **no write-path change** — no verb
  emits links yet, the first writers arrive with `--informed` and `--outcome-of`. Adapters
  unaffected.
  - *PR3 (same contract):* **`informed_by` on decisions.** `hv decide --informed <ref>…` /
    `hive_decide(informed_by="ref,ref")` records what a decision relied on: an additive payload
    list of journal refs plus one `informed` link per ref. `hv search` (text + JSON) and
    `hive_search` now return **`ref`** (`node_id:seq`) on every fact and decision — the stable
    identity agents should carry; bare local ids are accepted but kind-checked and discouraged.
    Rubric: when you call `hive_decide`, pass the `sid`s (PR3b) or refs of the facts you retrieved
    and relied on.
  - *PR4 (same contract):* **outcomes → `decisions.outcome_score`.** `hv remember --outcome-of
    <decision ref> [--polarity 1|0|-1] [--channel …]` / `hive_remember(outcome_of=…, polarity=…)`
    records what happened after a decision was acted on: an ordinary fact plus an `outcome-of`
    link carrying a ternary polarity. `_decision_evidence` (a clone of the fact-confidence
    projection keyed by decision) scores it into `decisions.outcome_score` / `last_outcome_at` —
    a *vindication* axis; decisions still carry **no confidence** and `--min-confidence` still
    excludes them. Only `sense`-channel outcomes count (absent = sense); an `introspect` outcome
    is recorded, never counted. Both evidence projections now retain an ordered evidence sequence.
  - *PR3b (1.19 feature set; landed under contract 1.20):* **stable short ids.** Every fact/decision/idea now carries **`sid`** —
    `h:` + `sha256("node_id:seq")[:10]` (e.g. `h:3f9a1c0b2d`), the human form of `ref`: identical on
    every node, never changes on rebuild, resolved **exactly** through the projection-written column
    `journal_index.sid`. It is shown wherever a rowid was shown (`hv search` text + JSON, write
    confirmations, `hv audit`, the open-ideas digest, `hive_search` rows, the dashboard — detail
    views are addressable as `/#h:…`) and accepted wherever an id is accepted (`--informed`,
    `--outcome-of`, `--supersedes`, `--resolves`, `hv retract`, `hv entity link`; MCP `informed_by`,
    `outcome_of`, `fact_id`). **Deprecation:** bare local ids (`118`, `d17`, `i5`) are rowids that
    drift across rebuilds and nodes; they remain accepted (kind-checked) but print a one-line
    warning and **stop being accepted at the next MAJOR contract bump** — adapters must pass `sid`
    (or `ref`). `hive_retract(fact_id)` and `hive_entity(fact_id)` now take the `sid` string. The
    audit's CONTRAVENED check parses a prose `h:…` exactly (a prose `#N` stays best-effort).
    Closes the `hv retract` wrong-target hazard (hive #58). No wire change; nothing new is journaled.
  - *PR6 (1.19 feature set; landed under contract 1.20):* **importance (salience L3) + utility are learned projections.**
    `facts.importance` is now written only by `_recompute_importance`: it starts at
    `min(--importance hint, importance_self_cap)` (default cap **0.3**) and rises only through links
    from **other** identities; `--importance` is therefore a hint, never a claim — adapters should
    stop treating it as a ranking lever. New `utility` (facts + ideas): how much recorded
    decision-making (`informed` links) relied on the entry, weighted by those decisions' outcomes.
    `hv search --sort importance|utility` / `api_search(sort=…)` rank by them (default ranking
    unchanged; the dashboard's `salience` sort is now an alias of `confidence`). Per-class half-lives
    (`halflife_fact` 180, `halflife_idea` 90, `halflife_volatile` 14 days) are governed knobs and now
    drive confidence decay too. JSON search rows gain `importance`, `effective_importance`,
    `utility`, `effective_utility`, `last_link_at`. No wire change; nothing new is journaled.
  - *PR2b (1.19 feature set; landed under contract 1.20):* **the write-path switch.** `hv decide --supersedes`, `hv remember
    --resolves` and `hv entity link` now emit **one `link`** (`supersedes` / `resolves` / `entity`)
    instead of their legacy field or entry (`supersedes_ref`; `resolves_ref` + `retract`;
    `entity_fact`) — never both. On the owner machine the link is owner-signed (hard everywhere);
    elsewhere it is hard only where the device authored the target, evidence otherwise (§5). The
    CLI/MCP surface is unchanged; only the journal output of those three verbs changed. **Version
    skew:** a pre-1.19 peer lands such a link but does not honour it, so `hv doctor` gains the
    advisory `fleet-contract` check (peers below 1.19, or unreachable and unverifiable; read from
    `/hive/info.contract`, which the daemon now advertises alongside `/sync/hello`, or from an older
    peer's `/api/verify.version`). Upgrade every always-on node
    before relying on these verbs across the fleet. Legacy entries keep projecting forever.
    Rubric: when you act on a decision and observe the result, record it with `--outcome-of`.
  - *PR7 (1.19 feature set; landed under contract 1.20):* **trust velocity.** Per-signer reliability (facts contradicted by *other*
    devices, decisions' outcome mean) over governed short/long windows (`trust_short_days`,
    `trust_long_days`); the delta is a **signal** surfaced by `hv doctor` (`trust-drift`, threshold
    `trust_drift_threshold`) and a `DRIFT` column on `hv peers`. Advisory only — no effect on
    admission, purge, corroboration weight or link authority. Adapters unaffected.
- `1.18` — **capsule-addressability for silent devices (`announce`)**. New authority-less,
  device-signed, kind-discriminated governance act `announce` — fixed envelope `{action, kind}`,
  kind-specific fields under `data`; first kind: `key`. Its only purpose is to *exist* as a signed
  entry carrying the device's pub, so a device the owner admitted directly (no join-request, zero
  writes) becomes a capsule recipient; previously it could never receive capsules and `doctor`
  warned forever. Ingest allowlists `announce` beside `join-request`; the projection ignores it
  entirely (grants nothing); the trust model is unchanged — a payload-carried key is still never a
  key source. Unknown announce *kinds* are accepted and ignored, so future kinds ride through 1.18
  nodes with no skew window. `hv key announce` (alias `hv config identity announce`) emits it,
  guarded to once-ever per device; `hv doctor --fix` auto-emits on an owned hive, so the 15-minute
  doctor timer self-heals the fleet. Version skew: a pre-1.18 peer rejects an announce on ingest
  and shows an advisory `diverged` until *it* upgrades — nothing is lost (stateless Merkle-diff
  re-pull); upgrade always-on nodes first, the emitting device last, for a zero-noise rollout.
- `1.17` — **cell/comb write-authorization at the projection**. `_cell_state`/`_comb_state` now apply
  the same projection authorization as capsules (1.16), but under a **separate `cell_writers` policy**
  (default `owner`; `fertile` = any admitted device) — so a hive can keep executable definitions
  owner-only while running capsules fertile, or the reverse. A `cell` defines what an agent or tool
  runs, so a malicious redefinition is code-injection-equivalent. **Behavior change:** `hv wire --add`
  was previously ungated (any admitted device could publish a cell/comb); under the default
  `cell_writers=owner` only the owner may now publish, the CLI refuses a non-owner write, and the
  projection declines a non-owner-signed cell/comb entry (it still lands in the journal — every node
  folds it away, convergence-safe). New advisory `cell-authz` doctor check surfaces any now-unhonored
  definition. Set the policy with `hv config set cell_writers owner|fertile`.
  **Security fix, 2026-07-08, still under contract 1.17** (GHSA-242f-7fxg-f7wm, PR #42): remote sync
  reads are signed by an admitted device (`Hive-Auth-*` headers), the dashboard's `/api/*` data
  answers only local or signed requests, and the daemon binds the Tailscale address instead of all
  interfaces; sync `protocol_version` is 2. This changes the daemon's wire behavior, not the adapter
  contract: no verb or flag changed. Only a foreign sync client that reads a peer must adopt the
  signed-request envelope (the bundled `hv` and daemon already do). See `docs/SYNC_API.md`.
- `1.16` — **capsule write-authorization at the projection**. `_capsule_state` now declines to honor a
  `capsule` entry whose signer was not the authorized writer — under the default `capsule_putters=owner`
  the entry must carry an owner signature from the owner who was legitimate *as of that entry's journal
  position* (point-in-time, so a prior owner's seals survive a transfer/succession/election); under
  `fertile`, any currently admitted, non-purged device. This closes a hole where a compromised
  *admitted* device could supersede or tombstone any capsule by appending a journal entry directly,
  bypassing the CLI-only `capsule_putters` gate (a targeted denial-of-secret). Convergence-safe: the
  entry still lands in every journal and the Merkle is unchanged; every node deterministically folds it
  away — no ingest/sync/admission change. Unsigned capsule entries are declined once an owner exists; a
  crypto-less (stripped) build keeps honoring rather than silently emptying capsules. New advisory
  `capsule-authz` doctor check surfaces any now-unhonored entry. `cell`/`comb` write-authorization (a
  separate `cell_writers` policy) lands in a follow-up.
- `1.15` — **taggable, searchable decisions**. `hv decide --tags <a,b>` records project/topic tags on
  a decision (journaled additively in the decision payload; projected to a new `decisions.tags`
  column — pre-1.15 decisions read back untagged). `hv search` now **also returns matching decisions**
  (a `LIKE` over content, rationale and tags, newest-first, superseded ones flagged) alongside facts,
  so a project decision is findable by tag or text instead of by its node-local, rebuild-unstable
  `#id` (the local id is a store.db rowid — it differs between nodes and shifts on rebuild, so it was
  never a stable cross-node reference). `hv search --format json` stays a flat list but each row gains
  a `kind` discriminator (`fact` | `decision`); a `min_confidence > 0` consumer naturally drops
  decisions (they carry no confidence). The MCP `hive_decide` gains a `tags` argument for parity.
  Additive journal field, wire-compatible — no sync, admission, or CLI-removal change.
- `1.14` — **standards-anchored crypto**. The bundled symmetric layer moved off a hand-rolled
  SHA-256-CTR keystream + HMAC encrypt-then-MAC onto pure-Python **ChaCha20-Poly1305 (RFC 8439,
  `chacha20poly1305.py`)** — so the crypto self-test pins the RFC's *published* known-answer vector
  instead of a self-minted byte string, and a human auditor reviews one analyzed AEAD rather than a
  bespoke generic composition. `_kdf_keystream` is **deleted**. Capsule `alg` →
  `x25519-hkdf-chacha20poly1305-v1` (the HKDF-SHA256 over the X25519 ECDH is unchanged; only the
  cipher moved). Owner-seal `enc` → `scrypt-chacha20poly1305-v2` (scrypt cost 2¹⁵→2¹⁶; the scrypt
  params are now bound as AEAD associated data); **the v1 read path is removed** — breaking for any
  pre-1.14 escrow or passphrase-export, so `hv owner restore` now *skips* a retired-scheme escrow with
  a re-`escrow` hint rather than failing. New hard-fail **`keyperm`** doctor check: the raw device and
  owner seeds at rest must be `0600` (the whole threat model rests on it); `hv doctor --fix`
  re-tightens. The crypto self-test gains the RFC 8439 AEAD KAT — the symmetric layer previously had
  *no* pinned vector at all. Owner-seal/capsule formats changed: a node holding a pre-1.14 escrow
  re-mints it with `hv owner escrow` from a device that still holds the owner key.
- `1.13` — **first-class `cell`/`comb`/`capsule` primitives + unified `hv wire`**. Three new
  `kind`-discriminated journal types ride the existing G-Set/sync/admission machinery unchanged: a
  **cell** is an executable unit with a spec (`kind:tool` = a self-wiring recipe; `kind:agent` = a
  foreign-config wiring like the Claude hooks), a **comb** is an ordered collection of cells, and a
  **capsule** is a payload **sealed to the authorized device set** (pure-Python X25519 ECDH wrapping
  an authenticated-cipher payload key, recipients = `admitted − purged`; see 1.14 for the cipher).
  They project deterministically
  (`_cell_state`/`_comb_state`/`_capsule_state`) rather than indexing into `store.db`. Wiring is
  unified under **`hv wire <name>|--comb <name>|--list|--show <name>|--add`**: `kind:agent` dispatches
  to the generalized `_wire_agent` (the old `hv doctor wire-agent` is now a deprecated alias
  with byte-identical output), `kind:tool` runs the platform-aware executor (credentials from a
  capsule, falling back to `--env-file`). New **`hv capsule put|get|ls|rm|rotate`** seals secrets with
  **secure ingestion only** (`--env-file`/`--file`/`--stdin`/interactive `getpass` — never via chat or
  argv). Owner-signed **`capsule_putters`** config gates who may publish. `hv doctor` gains a hard-fail
  **`crypto`** check (RFC 7748/8032 + ed↔curve + sealed-capsule KATs on the self-heal timer) and
  `scripts/common/sign_release.py` refuses to sign `verify.json` on a red suite. Additive to the wire format —
  existing adapters are unaffected — but agents gain `hv wire`/`hv capsule` as the self-provisioning
  surface.
- `1.12` — **self-healing agent integration via a stable shim**. The Claude Code hooks live in a
  foreign config (`~/.claude/settings.json`) the daemon self-heal never owned, so a node updated from
  before a hook existed could pass every health check yet silently miss half its hooks. The fix is to
  register only a **stable dispatch shim** there — `scripts/common/hive_dispatch.sh <event>`, one per
  lifecycle event — and decide *which* behaviors run for each event inside that script, which lives in
  source control (and so under `hv verify`'s signature). New behaviors then ship by update, never by
  re-wiring `~/.claude`; only a new event TYPE changes the registered set. `hv` holds the one shim
  spec; `hv doctor` adds an `agent-hooks` check and `hv doctor --fix` (the 15-min self-heal timer)
  reconciles — adds the shim, **migrates** any older inline hooks to it (so behaviors never
  double-fire), relinks the `hive-memory` skill — surgically (your own hooks untouched, `.bak.doctor`
  backup first). `hv doctor wire-agent` exposes it; the installer/update delegate to it, no second
  definition to drift. This applies to foreign-config integrations only (Claude Code today; any other
  foreign-config agent can reuse the same dispatcher); plugin agents (Hermes, the MCP server) carry
  their wiring in our signed code and need no shim. Additive, no wire change — adapters unaffected, but a foreign-config adapter
  should adopt the same shim + reconcile pattern.
- `1.11` — reconciliation verbs. `hv remember --resolves <id>` writes a correction, links it on the
  new fact row (`facts.resolves`), and **soft-retracts** the prior fact (deliberate negative evidence,
  reversible — never an owner-forget). A new `hv audit` category **CONTRAVENED** parses prose
  references (`resolves/supersedes/obsoletes/corrects #N`) and flags any whose target is still live,
  surfaced in the session-start boot summary. Closes the gap where a "resolved" fact landed while the
  stale one stayed live. Additive, no wire-format change — adapters need no edit.
- `1.10` — sync robustness: the **Merkle index now de-dups the journal by `(node_id, seq)`** before
  hashing (`merkle.read_all_entries`). A `(node_id, seq)` names one logical entry, so a
  physically-repeated journal line (a file re-imported or concatenated during recovery) can no longer
  shift a chunk hash and fake a **permanent peer divergence** that ordinary sync cannot heal (ingest
  also de-dups by that key, so a peer's correct copy is rejected as a duplicate and the stray row is
  never removed). A deterministic lexicographic tie-break on the canonical encoding keeps the chosen
  representative identical across nodes. No wire-format or CLI change — purely internal correctness;
  adapters need no edit.
- `1.9` — owner resilience (pt.3): **quorum election + dead-man switch**. When the owner is lost with
  no backup, no escrow, and no nominee, admitted devices can elect a new owner: `hv owner
  propose-election [--mint | --pub B64]` + `hv owner vote <proposal_id>` (both **device-signed** —
  authority is hive membership, not the owner key), tallied in `_governance_state` (Stage C). A
  proposal installs only when `quorum_m` admitted devices agree **and** the owner has been silent for
  `dead_man_days` — a **live owner is never unseated** because any owner-signed act, including the new
  `hv owner heartbeat`, refreshes liveness. Owner-signed config: `hv config quorum set
  quorum_m|quorum_by|dead_man_days` (`quorum_m=0` = OFF = exact 1.8 behavior, back-compat). `hv owner
  elections` lists tallies. Additive, governance-only — adapters never act as owner, so they are
  unaffected.
- `1.8` — footprint trim. `hv rebuild` is folded into **`hv doctor rebuild`** (joining `hv doctor
  merkle`/`migrate-identity`); the top-level `hv rebuild` keeps working as a hidden deprecated alias,
  so installer/update scripts and existing automation are unaffected. No new capability — purely a
  surface change; adapters that call `hv rebuild` need no edit.
- `1.7` — owner resilience (pt.2): **succession**. `hv owner nominate <pub>` + the successor's
  `hv owner claim [--mint]` hand ownership to a NEW key; `hv owner transfer <pub>` is the immediate
  variant. `_governance_state` now resolves an owner *chain* (term-0 TOFU → owner-signed nominate/
  transfer + nominee claims), so post-handoff old-owner acts are ignored and a live owner is never
  unseated. `hv owner revoke-escrow <ref|all>` logically tombstones a compromised escrow (`restore`
  skips it). `hv doctor` gains an `owner` check (key custody / nominations / sprawl). Additive,
  governance-only — adapters never act as owner, so they are unaffected. Quorum recovery lands in 1.9.
- `1.6` — owner resilience (pt.1). `hv owner export`/`import` back up and restore the owner key to
  an off-device file (optionally passphrase-encrypted) so a lost owner device can resume the SAME
  owner identity; `hv owner escrow`/`restore` store the key passphrase-encrypted IN the hive (it
  syncs to every device, so any synced device can recover it with the passphrase); `hv owner standby
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

## 8. Cost and inference

The checks use no model, and they should stay that way: the save-nudge gate, the audit (FTS plus
identity for redundancy, confidence plus TTL for obsolete and recheck), and the version check are
all deterministic code, which is cheaper than any model. Gate cheaply before you spend anything: a
quiet turn should cost a counter and a string scan, not a process.

If you add model-based help to your own adapter (for example, reading a transcript to find
decisions that were made but never written, or semantic dedup), keep it optional and local-first,
run it on a cheap model separate from the in-session one, and make sure the deterministic path works
with no model and no API key. Treat its output as proposals that a real agent or the owner confirms:
a background model never self-certifies a fact into being trusted.

---

*One brain (`hv`), one spec (this file), one reference (`scripts/common/nudge_hook.sh`). Everything else an
agent writes for itself, and keeps current by re-reading this spec when its version changes.*
