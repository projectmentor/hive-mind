# Changelog

HiveMind is versioned by its **agent contract** (`hv version`), `MAJOR.MINOR`. A MINOR is additive
for the adapter surface — the verbs, flags and outputs an agent calls keep working — while a MAJOR
means adapters must re-integrate (see [`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md) §7,
which also records what each version means for an adapter).

Git tags are `vMAJOR.MINOR.PATCH`. `vX.Y.0` marks the commit on `main` that completed contract
`X.Y`, so the tag contains everything that version shipped. Fixes and documentation that follow
without changing the contract are tagged `vX.Y.1`, `vX.Y.2`, … Dates are when each version was
introduced (a contract) or tagged (a patch).

## Unreleased — contract 1.24

- **Contract 1.24: revoke a decision (#45).** `hv decide --revoke <sid> --rationale "<why>"` withdraws a
  decision that was wrong, with no replacement. It writes one decision tagged `revocation` (content
  `Revoke <sid>: <the target's first line>` unless given) and one `supersedes` link to the target. The
  target is resolved and kind-checked, and a missing rationale or a combination with `--supersedes` is
  refused, before anything is written. There is no new projection: the target is superseded only when
  the link is hard (a person on the owner device, or the device that wrote the decision). The
  confirmation is derived from that authority, so an agent's revoke elsewhere prints `NOT in effect`
  until the owner re-runs it, and `hv doctor` lists it under `link-authz`. MCP and Hermes `hive_decide`
  gain `revoke` and `supersedes` (the latter had no path since 1.19), and a parity test now covers
  `hv decide`'s flags. No wire change; a pre-1.24 node sees an ordinary superseding decision.

## 1.23 — 2026-09-25 · `v1.23.0`

Contract 1.23, tagged 2026-09-25 at 3f6d569.

- **Contract 1.23: links carry owner authority only when a person writes them (#114).** On a machine
  that holds the owner key, a link is owner-signed only when its source is `manual`; every agent
  source is device-signed, as on a member device, so an agent there can no longer `supersedes` or
  `resolves` another device's entries with owner authority. All eight link kinds go through one
  builder (`outcome-of` and `informed` were built inline). `hv decide` and `hv entity link` gain
  `--source` (the MCP server passes `claude-ai`), so an agent's decision is no longer attributed to a
  person. Write confirmations say `(owner-signed: source manual)` or `(device-signed: source …)`. An
  omitted `--source` still means `manual` (#119). No wire change.
- **`hv doctor` catches a sync daemon left on old code (#112).** A bare `git pull` moves the files but
  not the running daemon. The daemon now records the source digest it loaded and serves it on a
  loopback-only `/api/daemon`; the new `daemon-code` check compares it with the same digest of the
  daemon's own checkout (never the checkout doctor runs from) and `--fix` restarts the managed daemon,
  at most once per run. A hive daemon from before this change also warns; another program on the port
  gets no verdict. The 15-minute doctor timer therefore heals a bare pull.

## 1.22 — 2026-09-24 · `v1.22.0`

Contract 1.22, tagged 2026-09-24 at 5c30c20. It also carries the fixes that landed before the tag.

- **Contract 1.22: an `extends` link kind.** `hv remember "…" --extends <sid>` (and MCP
  `hive_remember(extends=…)`, Hermes `hive_remember`) writes the fact plus one `extends` link to a
  fact, idea or decision: "builds on" without evidence semantics. The projection keeps it as an edge
  row in `links` and never weighs it toward confidence on any channel. One relationship per write, so
  it is mutually exclusive with `--resolves`, `--outcome-of`, `--supports` and `--contradicts`.
  `hv search` text shows `extends h:…` on the row; JSON rows are unchanged. No wire change and no
  skew: a pre-1.22 node lands the link and projects it to nothing (#115).
- **The test suite no longer touches the developer's real account (#109).** Every test runs with a
  throwaway `HOME`, `CLAUDE_CONFIG_DIR` and `HIVE_IDENTITY_STASH`, and with service-manager stubs
  first on `PATH`, so `hv doctor --fix` can no longer relink the live `~/.claude/skills/hive-memory`
  into a worktree or restart the live daemon, and `hv owner init` can no longer overwrite the
  owner-key stash. A session guard checks those real paths are unchanged at the end of every run.

## 1.21 — 2026-09-23 · `v1.21.0`

Contract 1.21, tagged 2026-09-24 at b723b12. It also carries the fixes and documentation that landed before the tag.

- **Contract 1.21: the grounding rule covers fact assertions.** A fact written with
  `--channel introspect` no longer counts as corroboration (it weighs `introspect_support_weight`,
  default 0, like an `introspect` link); it is still recorded and can earn confidence from
  observations (#73).
- **Unrecognised channel labels fail closed:** a `channel` outside `sense`/`act`/`introspect` counts
  as `introspect` everywhere; an unrecognised source class keeps full weight, like an absent one,
  since a class can only claim a discount (#72).
- **MCP:** `hive_remember` gains `resolves`, for parity with `hv remember --resolves` (#75).
- **A peer that moves to a new tailnet IP heals itself (#7).** The daemon records a peer's address
  only from requests signed by that peer's admitted device key (`$HIVE_HOME/.peer_candidates.json`,
  local and never journaled). A new `hv doctor` check, `peer-address`, repoints the `.peers.json`
  entry with `--fix` when the stored address fails and the peer has verified itself elsewhere. A
  working address is never rewritten, and `tailscale status` names are only unverified hints. The
  responder-signed hello that would also cover a peer that never contacts this node is #107.
- **`.gitignore`** ignores the local bus log (`.bus/`) and any `*.log` in the checkout. Like the
  store's side files (#74), they hold hive content and could otherwise be committed to the public repo
  by accident (#103).
- **Search JSON:** rows gain `tag_list` (the tags as a list); `tags` stays a JSON-encoded string
  until 2.0, when it becomes the list (#77).
- **Ideas can earn confidence:** `hv remember "<observation>" --supports <sid>` / `--contradicts <sid>`
  (and MCP `hive_remember(supports=…, contradicts=…)`) write the evidence links ideas and facts
  need. An idea's author can't support their own idea but can contradict it. One relationship per
  write: `--resolves`, `--outcome-of`, `--supports` and `--contradicts` are mutually exclusive, so
  `--resolves` with `--outcome-of`, previously accepted, is refused (#71).
- **Hermes adapter at parity with MCP (#76):** the Hermes plugin gains `hive_remember` (outcomes,
  corrections, `supports`/`contradicts`), `hive_decide` (with `informed_by`) and `hive_propose`, and
  `hive_search` gains `kind`. All writes, including the `memory()` mirror, run on one bounded
  background writer, so a turn never waits on the hive. The unused root `hermes_integration.py` is
  removed.
- **`hive-mind update` no longer aborts on a busy store (#93).** It now refreshes the units, restarts
  the daemon and waits for it, *then* rebuilds (the old daemon could hold the store's lock through a
  minutes-long rebuild, past the 10 s busy timeout, which aborted the update and skipped the restart
  and hooks). A rebuild that still finds the store busy warns and continues; the daemon catches up on
  its next cycle. A pull that lands before the release re-sign waits for it (up to 3 minutes) and
  pulls it, or says it is pending, and `hv doctor`'s `authenticity` check marks a clean checkout on a
  commit under 30 minutes old advisory instead of failed while the re-sign is pending.
- **Agent write guidance teaches the grounding rule (#99):** the Claude Code skill, the MCP server
  instructions and the Hermes context block now tell agents to write their own conclusions, analysis
  and plans with `--channel introspect` (only if worth finding later), and that tags help readers but
  never change how much a fact counts as evidence. A new test (`tests/test_agent_guidance.py`) keeps
  all three from dropping it.

## 1.20.1 — 2026-09-23 · `v1.20.1`

A patch release: no contract change.

- **Performance fix (#70):** `ed25519.py` is rewritten for speed with an identical accept set and
  byte-identical signatures (roughly 60–180× faster), and each signature is verified once per
  process. On a hive of about 860 entries `hv search` drops from about 15 s to 0.3 s and `hv remember`
  from about a minute to 0.7 s, back inside the agent adapters' timeouts (#87). SQLite waits up to 10 s
  for a busy store. A write that can't reach it after its journal append (`remember`, `decide`,
  `propose`, `retract`, `entity`) reports success (journaled) instead of failing, so callers don't
  retry into duplicates, and the next command catches the store up; a busy store never stops a read
  (#95).
- **The sync daemon's bind heals itself (#47):** a daemon that starts before `tailscaled` waits up to
  30 s for the tailnet. If it still lands on loopback, it rebinds within 15 s of the tailnet appearing,
  and on a tailnet IP change within one sync round, by exiting 75 for its service manager to restart
  it. It never moves toward loopback. A new `sync-bind` doctor check, fixed by `--fix`, catches a
  daemon still on the wrong address (#90).
- **`.gitignore`** ignores the store's SQLite side files (`store.db-wal`, `-shm`, `-journal`), which
  hold hive content, so they can't be committed by accident (#74, #80).
- Documentation brought up to date with contract 1.20: a new `SECURITY.md`, `docs/SYNC_API.md`
  rewritten (read authentication, current fields, the `/api/*` surface), `docs/P2P_DESIGN.md` marked
  historical, and README, CLI reference, agent-integration spec, internals, threat model and skills
  updated (#78). This changelog and the version tags were added. The continual-learning design
  record is in `docs/design/` (#68).
- `hv dash` prints only the local URL; the dashboard is refused to other devices (see 1.17).
- Security advisory [GHSA-242f-7fxg-f7wm](https://github.com/projectmentor/hive-mind/security/advisories/GHSA-242f-7fxg-f7wm)
  published (fixed in 1.17).
- CI also runs on Ubuntu 26.04, ahead of the `ubuntu-latest` migration (#89).

## 1.20 — 2026-09-20 · `v1.20.0`

The second half of the continual-learning work.

- **Ideas:** a new `idea` entry type for hypotheses, whose confidence is earned from others' evidence
  and never asserted; `hv propose` and MCP `hive_propose`; `hv search --kind all|fact|decision|idea`;
  open ideas in the session-start digest (#62). *Ideas could not earn confidence in 1.20: nothing
  wrote the links they need (fixed in 1.21, #71).*
- **Trust velocity:** per-device reliability drift, the `trust-drift` doctor advisory and a `DRIFT`
  column in `hv peers`; advisory only (#63).
- **Stable short ids:** every fact, decision and idea has an `h:…` id that is the same on every node,
  shown everywhere and accepted wherever an id is; bare local ids are deprecated (#64).
- **Learned importance and utility:** importance (salience L3) starts at a capped self-hint and rises
  only through other agents' links; utility credits the facts that informed decisions, weighted by
  outcomes; per-class half-lives; `hv search --sort importance|utility|recency` (#65).
- **Write-path switch:** `hv decide --supersedes`, `hv remember --resolves` and `hv entity link` now
  write one `link` entry; the `fleet-contract` doctor advisory lists peers older than 1.19. Upgrade
  always-on nodes first (#66).

## 1.19 — 2026-09-20 · `v1.19.0`

The first half of the continual-learning work.

- **Links:** one generic `link` entry type with an open vocabulary (`supports`, `contradicts`,
  `supersedes`, `resolves`, `entity`, `informed`, `outcome-of`). Links are evidence, not commands:
  only the owner (by signature) or the target's author can supersede or resolve; `link-authz` doctor
  advisory (#57).
- **Informed decisions:** `hv decide --informed` records what a decision relied on (#60).
- **Outcomes:** `hv remember --outcome-of --polarity [--channel]`; decisions gain an outcome score, a
  measure of whether they worked out, never confidence (#61).

## 1.18 — 2026-07-09 · `v1.18.0`

- **`announce`:** a device the owner admitted directly, which never wrote anything, can publish its
  key and so receive capsules (`hv key announce`; `hv doctor --fix` emits it automatically) (#43).
  Upgrade always-on nodes first; older peers show a temporary divergence until they upgrade.
- `_is_salient` renamed `_is_admissible` (the salience L2 admission gate) (#49).

## 1.17 — 2026-06-30 · `v1.17.0`

- **Cell and comb write authorization** under a separate `cell_writers` policy (default: owner only);
  `cell-authz` doctor advisory (#40). *Behaviour change:* `hv wire --add` is owner-only by default.
- **Security fix, 2026-07-08:** sync reads are signed by an admitted device
  (`hv sync auth off|permissive|enforce`, default `permissive`), the dashboard and its data answer only
  local or signed requests, and the daemon binds the Tailscale address instead of all interfaces.
  Fixes [GHSA-242f-7fxg-f7wm](https://github.com/projectmentor/hive-mind/security/advisories/GHSA-242f-7fxg-f7wm)
  (#42).

## 1.16 — 2026-06-30 · `v1.16.0`

- **Capsule write authorization** enforced where capsules are read, not only in the CLI, under
  `capsule_putters`; a compromised admitted device can no longer overwrite or delete a capsule;
  `capsule-authz` doctor advisory (#39).

## 1.15 — 2026-06-28 · `v1.15.0`

- **Decisions become taggable and searchable:** `hv decide --tags`; `hv search` returns matching
  decisions (#33).
- **Web dashboard:** `hv dash`, served by the sync daemon (#34–#38).

## 1.14 — 2026-06-25 · `v1.14.0`

- **Standards-anchored crypto:** ChaCha20-Poly1305 (RFC 8439) for capsules and owner-key sealing,
  with a pinned known-answer test; `keyperm` doctor check. *Breaking* for escrows and passphrase
  exports made before 1.14: re-run `hv owner escrow` from a device that holds the owner key.
- Also: `hive-mind reset`, the `hv peers` roster, the invite and seed-peer join flow (Android), sync
  over sub-1280-MTU tailnet paths, and MCP parity with the CLI.

## 1.13 — 2026-06-25 · `v1.13.0`

- **Cells, combs and capsules** as entry types: `hv wire` for self-wiring tools and agents (replacing
  `hv doctor wire-agent`, now a deprecated alias), `hv capsule` for secrets sealed to your devices, a
  crypto self-test and the bundled advisories feed; release signing requires a green test suite.

## 1.12 — 2026-06-22 · `v1.12.0`

- **Self-healing Claude Code integration:** one stable dispatch shim per lifecycle event in
  `~/.claude`, reconciled by `hv doctor --fix`; `agent-hooks` doctor check.

## 1.11 — 2026-06-22 · `v1.11.0`

- **Reconciliation:** `hv remember --resolves` corrects and soft-retracts a fact; the audit's
  CONTRAVENED check flags corrections that were never reconciled.

## 1.10 — 2026-06-21 · `v1.10.0`

- **Sync fix:** the Merkle index de-duplicates the journal by `(node_id, seq)`, so a repeated line can
  no longer cause a permanent false divergence.

## 1.9 — 2026-06-14 · `v1.9.0`

- **Owner resilience, part 3:** quorum election with a dead-man switch (`hv owner propose-election`,
  `vote`, `heartbeat`; `hv config quorum`). A live owner can never be unseated.

## 1.8 — 2026-06-14 · `v1.8.0`

- `hv rebuild` folded into `hv doctor rebuild` (the old name still works).

## 1.7 — 2026-06-14 · `v1.7.0`

- **Owner resilience, part 2:** nominated succession (`hv owner nominate` / `claim`), immediate
  `transfer`, `revoke-escrow`; `owner` doctor check.

## 1.6 — 2026-06-14 · `v1.6.0`

- **Owner resilience, part 1:** `hv owner export` / `import`, in-hive escrow (`escrow` / `restore`),
  `standby`.

## 1.5 — 2026-06-13 · `v1.5.0`

- **Membership lifecycle:** `hv group` (admit, revoke, deny, change, purge, list); `hv config
  identity` and `hv config confidence`; `hv merkle` and `migrate-device-identity` folded under
  `hv doctor`.

## 1.4 — 2026-06-08 · `v1.4.0`

- **Governed confidence:** journaled, owner-signed governance; the same-device discount; the
  admission gate; `cap_self`. An owner forget requires the owner key.
- Also: read-only membership until admitted, `hv whoami`, installer self-healing and the `hv doctor`
  timer, MCP nudge, audit and telemetry tools.

## 1.3 — 2026-06-08 · `v1.3.0`

- **Cryptographic device identity:** Ed25519 device keys; entries are signed and verified on ingest;
  migration tooling.

## 1.2 — 2026-06-07 · `v1.2.0`

- `hv remember` auto-tags transient status claims `volatile`; `hv doctor`.

## 1.1 — 2026-06-07 · `v1.1.0`

- A local-only telemetry lane (`hv telemetry`) that is never journaled or synced.

## 1.0 — 2026-06-07 · `v1.0.0`

- The first versioned contract: the `hv` CLI over an append-only journal, peer-to-peer Merkle sync,
  confidence from independent corroboration, the nudge-and-audit capture loop, `hv verify`, and the
  installer.
