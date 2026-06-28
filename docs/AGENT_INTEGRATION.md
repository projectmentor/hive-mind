# HiveMind Agent Integration Spec

`Contract-Version: 1.15`  *(SemVer `MAJOR.MINOR`; authoritative value: `hv version`)*

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
   tagging explicitly is still good practice. **Reconcile, do not just append.** When what you
   write *contravenes* a live fact — an issue you closed, a status that flipped, a claim now shown
   wrong — reconcile it rather than leaving both. The clean way is one command:
   `hv remember "<correction>" --resolves <id>`, which writes the correction, links it on the new
   row, and soft-retracts the old fact (deliberate negative evidence, reversible). If you instead
   name the target in prose (`resolves #N`, `supersedes #N`, `obsoletes #N`, `CORRECTION to #N`)
   without `--resolves`, the audit's **CONTRAVENED** check flags it while the target is still live,
   so a missed reconcile surfaces at the next session start. Prefer `--resolves`: a prose `#N` is a
   **local id** that drifts across rebuilds and nodes (so a flagged target id is best-effort), while
   `--resolves` records the resolved fact's `(node_id, seq)` journal identity, which never drifts. A
   "resolved" fact written while the "open" fact stays live asserts both at once, which is worse than
   either alone. `--resolves` and a bare `hv retract` are soft and reversible; a decisive `--owner`
   forget stays the owner's call (§4).
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
  don't hand-wire it: `hv` owns the shim spec, the installer/update wire it via `hv doctor wire-agent`,
  and `hv doctor --fix` (the 15-min self-heal timer) re-asserts/migrates it. Build the same
  shim-then-reconcile pattern into any foreign-config adapter rather than wiring once and hoping it
  stays; an adapter that *is* plugin code (below) already has its wiring in source and needs no shim.
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

Per-node tuning lives in `nudge.env` (copy from `config/nudge.env.example`; mirrors the `.peers.json`
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
  to the generalized `_wire_agent` (the old `hv doctor wire-agent` is now a hidden deprecated alias
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
  definition to drift. This applies to foreign-config integrations only (Claude Code today, Codex
  later via the same dispatcher); plugin agents (Hermes/OpenClaw/MCP) carry their wiring in our signed
  code and need no shim. Additive, no wire change — adapters unaffected, but a foreign-config adapter
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

*One brain (`hv`), one spec (this file), one reference (`scripts/common/nudge_hook.sh`). Everything else an
agent writes for itself, and keeps current by re-reading this spec when its version changes.*
