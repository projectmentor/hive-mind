# Changelog

HiveMind is versioned by its **agent contract** (`hv version`), `MAJOR.MINOR`. A MINOR is additive
for the adapter surface — the verbs, flags and outputs an agent calls keep working — while a MAJOR
means adapters must re-integrate (see [`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md) §7,
which also records what each version means for an adapter).

Git tags are `vMAJOR.MINOR.PATCH`. `vX.Y.0` marks the **final** commit of contract `X.Y` on `main`,
so a tag contains everything that version shipped; a later fix that changes no contract is tagged
`vX.Y.1`, `vX.Y.2`, … Dates are when each contract version was introduced.

## Unreleased

- **Contract 1.21: the grounding rule covers fact assertions.** A fact written with
  `--channel introspect` no longer counts as corroboration (it weighs `introspect_support_weight`,
  default 0, like an `introspect` link); it is still recorded and can earn confidence from
  observations (#73).
- **Unrecognised channel labels fail closed:** a `channel` outside `sense`/`act`/`introspect` counts
  as `introspect` everywhere; an unrecognised source class keeps full weight, like an absent one,
  since a class can only claim a discount (#72).
- **MCP:** `hive_remember` gains `resolves`, for parity with `hv remember --resolves` (#75).
- **Search JSON:** rows gain `tag_list` (the tags as a list); `tags` stays a JSON-encoded string
  until 2.0, when it becomes the list (#77).
- **Ideas can earn confidence:** `hv remember "<observation>" --supports <sid>` / `--contradicts <sid>`
  (and MCP `hive_remember(supports=…, contradicts=…)`) write the evidence links ideas and facts
  need. An idea's author can't support their own idea but can contradict it. One relationship per
  write: `--resolves`, `--outcome-of`, `--supports` and `--contradicts` are mutually exclusive, so
  `--resolves` with `--outcome-of`, previously accepted, is refused (#71).
- **The sync daemon's bind heals itself (`v1.20.1`, #47):** a daemon that starts before `tailscaled`
  waits up to 30 s for the tailnet. If it still lands on loopback, it rebinds within 15 s of the
  tailnet appearing, and on a tailnet IP change within one sync round, by exiting 75 for its service
  manager to restart it. It never moves toward loopback. A new `sync-bind` doctor check, fixed by
  `--fix`, catches a daemon still on the wrong address.
- **Performance fix (`v1.20.1`, #70):** `ed25519.py` is rewritten for speed with an identical accept set
  and byte-identical signatures (roughly 60–180× faster), and each signature is verified once per
  process. On a hive of about 860 entries `hv search` drops from about 15 s to 0.3 s and `hv remember`
  from about a minute to 0.7 s, back inside the agent adapters' timeouts. SQLite waits up to 10 s for
  a busy store; a write that can't reach it after its journal append reports success (journaled) instead
  of failing, so callers don't retry into duplicates, and the next command catches the store up.
- Documentation brought up to date with contract 1.20: a new `SECURITY.md`, `docs/SYNC_API.md`
  rewritten (read authentication, current fields, the `/api/*` surface), `docs/P2P_DESIGN.md` marked
  historical, and README, CLI reference, agent-integration spec, internals, threat model and skills
  updated (#78). This changelog and the version tags were added.
- `hv dash` prints only the local URL; the dashboard is refused to other devices (see 1.17).
- Security advisory [GHSA-242f-7fxg-f7wm](https://github.com/projectmentor/hive-mind/security/advisories/GHSA-242f-7fxg-f7wm)
  published (fixed in 1.17).

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
