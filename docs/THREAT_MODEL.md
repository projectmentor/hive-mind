# HiveMind threat model

This is the authoritative statement of what HiveMind defends against, what it assumes, and what
is explicitly out of scope. It is meant to be read alongside `INTERNALS.md` (mechanics) and
`P2P_DESIGN.md` (sync). When a security-relevant change lands, update this file.

## Trust assumptions (the security rests on these)

1. **Local key files are 0600 and the host is not compromised.** Device and owner Ed25519 seeds
   live in `HIVE_HOME/.device-key` and `.owner-key` as raw bytes at mode 0600. Anyone who can read
   those files *is* that device/owner. `hv doctor` hard-fails on a group/other-readable key and
   `--fix` re-tightens it (and now loudly reports if the chmod itself fails).
2. **The journal is an append-only, signed G-Set.** Every entry is signed by its device key and
   `node_id = "k1:"+sha256(pub)[:16]`, so an entry cannot be attributed to a device whose key you
   do not hold. Governance acts that bear authority (owner/admit/config) additionally carry an owner
   signature. The journal converges deterministically across nodes (dedup by `(node_id, seq)`), so
   every honest node computes the same governance/confidence projection.
3. **The network perimeter is Tailscale, and sync READS are authenticated (defense in depth).** The
   sync daemon binds the node's own tailnet IP (not `0.0.0.0`) by default, so it is not exposed on
   other interfaces. Without a tailnet address it binds loopback only, and an automatic bind moves
   only from loopback to the tailnet, never the other way (`sync-bind` doctor check). Beyond that, the read surface is gated by an application-layer signed-request
   envelope (`Hive-Auth-*`: an Ed25519 device signature over the request, verified against the
   governance admitted-set, with a freshness window + nonce replay guard). A *remote* reader must be
   an admitted device: `/sync/chunk` and `/sync/hello` require a valid signature; `/api/*` (the
   corpus/telemetry surface) and the dashboard are loopback-only (local operator) or a signed peer
   proxy; only minimal discovery (`/hive/info`, `/sync/merkle-root`, `/api/verify` — no journal
   content) stays open so an unadmitted joiner can still find and verify the hive. Enforcement is a
   per-node mode (`hv sync auth off|permissive|enforce`, default `permissive`) so a live fleet flips
   to `enforce` independently once all peers speak protocol 2 — no lockstep cutover. Tailscale ACLs
   remain the boundary for *who can connect*; the signed envelope is the boundary for *who can read*.
   (Closes GHSA-242f-7fxg-f7wm. WRITES were already per-entry authenticated on ingest.) The other
   direction is authenticated too since protocol 3 (#107): a `/sync/hello` or `/hive/info` answer carries
   the responder's device signature over the caller's nonce, the path, its identity and advertised
   address, and a digest of the answer. A separate per-node mode (`hv sync auth --outbound
   off|permissive|enforce`, default `permissive`, which only flags) makes a node push its journal only to
   a peer that proved itself, admitted, at the address it contacted; the signed answer is the boundary
   for *who this node writes to*.

## Adversary model (what we actively defend against)

Given the perimeter above, the realistic adversary is **a misbehaving or compromised tailnet peer**
(not an arbitrary internet host), plus **local multi-process races** (the daemon and a CLI writing
at once). Concretely:

- **Resource exhaustion from one peer.** The daemon caps request bodies (413 over `MAX_BODY_BYTES`),
  sets a per-connection read timeout (slow-loris), bounds concurrent handlers (503 when saturated),
  and rate-limits per peer-IP (429). A single peer cannot OOM, wedge, or thread-exhaust a node.
- **Forged identity / laundered corroboration.** Entry signatures are verified on ingest; an entry
  whose `node_id` is not the fingerprint of its embedded pubkey, or whose signature fails, is
  rejected. Content from a non-admitted device is dropped once an owner exists.
- **Sealing a secret to an attacker-chosen key.** Capsule recipient keys are derived ONLY from a
  device's own signed Ed25519 identity (X25519 conversion); a `curve_pub` merely *carried* on a
  join/admit payload is never trusted as a key (it is only a tamper tripwire → `suspect`). A
  low-order or malformed recipient key is skipped per-recipient and reported `unsealable`, never
  silently sealed to. The X25519 ECDH rejects the all-zero (low-order) shared secret. The
  `announce` act (1.18) adds NO key source — it only gives a silent device a signed entry to
  harvest from, under the same fingerprint binding; its payload is never trusted, its emission
  guard applies the same binding (so a planted pub on a spoofed unsigned entry cannot suppress a
  device's announcement), and unsigned-announce spam is the same accepted, inert class as
  unsigned join-request spam.
- **Superseding/killing a secret by a compromised admitted device.** A `capsule` is the shared
  latest-wins projection (`_capsule_state`); a later version supersedes an older seal and a
  `tombstone-v1` kills the secret. `capsule_putters` (default `owner`) is the write policy, but it
  was historically enforced only in the CLI `put`/`rotate`/`rm` path — a compromised *admitted*
  device could bypass it by appending a `capsule` entry to the journal directly, silently tombstoning
  or resealing any capsule (a targeted denial-of-secret). The write policy is now enforced at the
  **projection**: under `capsule_putters=owner` a `capsule` entry is honored only if it carries an
  owner signature (the writer proves owner-key possession, exactly as a governance act does) AND that
  owner was the legitimate owner AS OF the entry's journal position (point-in-time, so a prior
  owner's seals survive a transfer/succession/election); under `fertile`, only a currently admitted,
  non-purged device. An unauthorized entry still LANDS in every journal (the G-Set and Merkle are
  untouched, so sync stays convergent) — every node just deterministically folds it away. `hv doctor`
  (`capsule-authz`) surfaces any entry the projection declines. Confidentiality is unaffected
  (recipient keys are identity-derived); this protects availability/integrity of the secret.
- **Redefining an executable `cell`/`comb` by a compromised admitted device.** A `cell` defines what
  an agent or tool runs and a `comb` orders cells, so a malicious redefinition is code-injection-
  equivalent — and `hv wire --add` historically appended these with NO write gate. The same
  projection authorization now applies to `_cell_state`/`_comb_state`, under a SEPARATE `cell_writers`
  policy (default `owner`) so executable-definition authority is tunable independently of capsule
  authority (a hive can keep cells owner-only while running capsules fertile). `hv wire --add` refuses
  an unauthorized write and the projection declines a non-owner-signed cell/comb entry; `hv doctor`
  (`cell-authz`) surfaces any it declines.
- **Erasing knowledge with a forged relationship (contract 1.19 `link`).** A `supersedes` or
  `resolves` link is the new denial-of-knowledge surface: honoured blindly, a compromised admitted
  device could retire any decision or soft-retract any fact by appending one entry. Links are
  therefore **evidence, not commands**: a link COMMANDS (sets `superseded_by`, writes `facts.resolves`)
  only if its payload carries an owner signature valid for the owner AS OF that journal position — the
  same `_is_authorized_writer` proof capsules and cells require; a device key is never the owner key —
  or its verified signer is the target's author (self-correction). From any other admitted device it is
  downgraded to weigh-only: it still moves the target's confidence like a `retract` would, so a real
  correction from a peer is not silenced, but it cannot hide or replace. There is deliberately no
  `link_writers` knob (a fertile hive needs every agent to link). Unknown link kinds project to
  nothing. `hv doctor` (`link-authz`) lists every downgraded link; the owner ratifies by re-issuing it
  owner-signed. **Agents on the owner device are members for link authority (1.23, #114).** A link is
  owner-signed only when the machine holds the owner key AND the write's source is `manual` (a person
  at the CLI). Every agent source (claude-code, hermes, grok, claude-ai …) is device-signed as on any
  member device, so an agent running where the owner key lives cannot borrow owner authority to
  `supersedes`/`resolves` another device's entries. The confirmation line says which. Residual: an
  agent that omits `--source` writes as `manual` (#119). Grounding: a `supports`/`contradicts` link, or (since 1.21) a fact assertion, whose
  `channel` is `introspect` weighs `introspect_support_weight` (default 0), so no agent can talk a
  claim up or down by reasoning alone.
- **Owner forgets are reversible, and only by the owner (contract 1.24, #46).** `hv retract --owner` floors a fact; `hv unforget` reverses it. Both are owner-signed `retract` entries (an unforget carries `unretracts_ref`), and the owner acts on one fact's text form a timeline sorted on `(timestamp, node_id, seq)` where the latest honoured act wins. An act is honoured only when signed by the owner AS OF its own journal position (`_owner_at`, the capsule/cell/link rule), so a previous owner's forget survives a transfer, succession or election (before 1.24 it resurrected), and a successor's signature from before their term does not count. An unsigned or forged unforget is ignored, and an unforget is never grandfathered. Owner acts are governance, not evidence: they do not move a fact's last-evidence time, so an unforgotten fact's confidence re-derives from its surviving evidence. A pre-1.24 node skips an unforget (it has no `retracts_ref`) and keeps the fact forgotten until it upgrades: projection skew, never divergence. **Residual (#122):** a forget positioned before the genesis owner is still grandfathered without a signature, and ingest does not check timestamps, so an admitted device can erase a fact with a backdated unsigned owner forget. After 1.24 the owner can undo it with `hv unforget`, and `hv doctor` (`forget-authz`) names every fact a grandfathered forget keeps
  forgotten, and counts the ones whose target is not in the journal. **Closed by policy (contract 1.25,
  #122):** `hv config set forget_writers owner` makes only forgets signed by the owner as of their position
  count, so a backdated unsigned forget erases nothing, including one appended after the close. The policy
  is a set-config act honoured only from the owner at its position, and it stores no journal refs, so no
  future re-keying can orphan it (#130). A legitimate legacy forget is kept by re-issuing it signed. Once set,
  the 1.19 property "a compromised admitted device can never erase, only weigh" holds for fact forgets
  too. Residual: a hive still on `legacy` (the default), and nodes before 1.25, which ignore the key and
  keep honouring the grandfather until they upgrade (projection skew).
- **Version skew during the link write-path switch (contract 1.19 PR2b).** `hv decide --supersedes`,
  `hv remember --resolves` and `hv entity link` now write only a `link` entry. A node older than 1.19
  lands that entry (journals stay converged) but does not honour it, so on that node a superseded
  decision still reads as current and a resolved fact as live until it upgrades. `hv doctor`
  (`fleet-contract`) lists admitted peers that advertise a contract below 1.19, or that are
  unreachable and so cannot be verified; upgrade always-on nodes first. Advisory; `--fix` never
  touches it.
- **Self-promotion through the learning projections (contracts 1.19–1.20).** An outcome counts toward
  a decision's outcome score only on the `sense` channel, and `cap_self` applies, so a device cannot
  vindicate its own decisions by reflection or by itself. A writer's `--importance` is only a hint,
  capped at `importance_self_cap` (default 0.3); only links from *other* principals raise it. An idea
  starts at zero, its own channel defaults to `introspect`, and it can gain confidence only from
  `sense`-channel links by *other principals*: since 1.21 the author's own `supports` weighs 0
  (before an owner exists, only the same device is excluded). Restating it never counts.
- **Detecting a device that has gone bad (contract 1.19 trust velocity).** Per-signer reliability is a
  derived view of the same evidence: how often a device's recent facts get retracted or contradicted by
  *other* devices, and how its recent decisions' outcomes score, each compared against the device's own
  long-window baseline. A sharply negative velocity is a security **signal** — possible compromise,
  prompt injection, corrupted state, a model or tool change — surfaced by `hv doctor` (`trust-drift`)
  and `hv peers`. It is deliberately **not an enforcement input**: it does not change admission, purge,
  corroboration weight or link authority, and no agent-trust *level* is exposed as a number to rank
  agents by (information is trusted, not agents). Any automatic response (a CAUTION/RESTRICTED state)
  is a later decision once real drift has been observed for a release.
- **Forged clock to trip succession early.** A quorum election's dead-man timer is anchored on the
  proposal's `basis_ts`. A proposal whose `basis_ts` leads its OWN entry timestamp by more than a
  small skew allowance is rejected — a deterministic, entry-time-only check (no wall-clock), so the
  journal stays convergent. See "Known limitations" for the residual.
- **Local write races.** Journal appends take an exclusive `flock`, so a daemon and a CLI writing
  the same daily file cannot interleave mid-line and corrupt a record.
- **Cross-hive contamination.** Sync refuses to merge journals whose `hive_id` differs — but only when
  BOTH sides present one: the check compares the `hive_id` in the request body, and a push that simply
  omits it is not caught by this guard. That is deliberate (an empty id on either side is the pre-owner
  bootstrap case, which has to be allowed so a genesis can propagate), and it is why the guard is not
  what keeps a rival `owner` declaration out. **Genesis pinning** is (below).
- **Genesis pinning.** Which `owner` declaration established this hive is recorded on the node, at
  `$HIVE_HOME/.genesis-pin` (0600, never synced, never journaled, never in the signed manifest), and the
  pin — not the entry timestamp — decides. This matters because genesis used to be "the earliest valid
  self-signed `owner` act wins" and a journal timestamp is written by whoever wrote the entry, so a
  declaration arriving later with an earlier timestamp replaced the established owner, after which the
  real owner's own acts stopped counting (only the current owner's acts are honoured). The pin binds the
  whole entry by hash, so a declaration that copies the `hive_id` and the `owner_id` still fails to match.
  Ingest refuses any other `owner` act, and the projection resolves genesis through the pin, so a rival
  already on disk loses — an append-only journal never removes it. Honest nodes sharing one pin drop the
  same acts, so journals stay convergent, exactly as they already do for forged acts. A node that has not
  pinned serves no journal to a remote caller and accepts no push (403 on `/sync/hello`, `/sync/chunk`
  and `/sync/ingest`; `/hive/info` and `/sync/merkle-root` stay open; loopback is never gated), which
  closes the window in which a joiner could be captured and then relay a rival onward.
- **Unsigned entries cannot fork a chain.** `_verify_entry` accepts an entry with no signature at all
  (historical entries predate device keys), so an unsigned entry could take an admitted device's future
  `(node_id, seq)`, become that device's chain tip, and make the device's own later entry drop as a
  duplicate — two different bodies under one key, and a permanent Merkle divergence. On a chain this node
  holds, an unsigned entry above the tip, or a different body at a sequence already held, is now refused.
  On a chain it does not hold, only a verified first pull is accepted: one batch reproducing that peer's
  whole advertised chain, window hash for window hash from sequence 1, which only the node's own outbound
  pull can present. A push never qualifies.
- **An impostor at a peer's address.** Tailnet IPs get reassigned, and whatever listens at a peer's
  stale address used to receive that peer's pushes, since a sync round trusted the answering
  `/sync/hello`. Its answer is now signed by the responder's device key over the caller's single-use
  nonce (#107), so under outbound `enforce` an impostor gets no push: a forged, replayed or foreign-key
  answer is `invalid` and skipped, and an unsigned one is only pulled from (pulled entries are checked
  one by one on append). A relay that forwards the request to the real peer gets back a signature over
  the peer's real `advertised_addr`, not the address it squats, so it is `addr-unproven` and gets no
  push either. `hv doctor --fix` repoints a peer to a new address only on the same proof: a verified
  answer for exactly that device at exactly that address.

## Known limitations (in scope to document, NOT yet closed)

- **Outbound verification is opt-in.** The outbound mode defaults to `permissive`, which flags and never
  withholds a push, so a node is protected from an impostor at a peer's address only after it runs
  `hv sync auth --outbound enforce`; `hv doctor` (`peer-identity`) says when every peer is ready. A peer on
  a legacy identity (no device key) can never pass `enforce`, and a peer stored under a host name rather
  than its advertised IP is `addr-unproven`, so `enforce` only pulls from it. The merkle-root comparison
  that ends a round for a peer already in sync stays unsigned: an impostor that echoes this node's root
  can stall its sync with that peer (a denial of service, not a disclosure).
- **Anyone can obtain a responder signature over a nonce it chooses.** `/hive/info` is open discovery,
  so any reachable host can make a node sign. The statement holds only public facts (node id, hive id,
  advertised address, protocol) and a digest of the answer that caller was served; the `hive-hello-v1`
  domain tag keeps it from ever passing as a request or entry signature, and the address binding keeps a
  relayed one from proving anything. Signing runs inside the request-slot cap and per-address rate limit.
- **What genesis pinning leaves.** Pinning (above) decides which declaration establishes the hive, but a
  device still has to learn the right fingerprint once. An invite carries it
  (`<address>/<hive_id>/<owner_id>/<hash8>`), and `/hive/info` and `/sync/hello` serve it inside the
  responder signature, so it cannot be swapped in transit. A join by a **bare address**, with no
  fingerprint to compare, remains trust-on-first-use for that first pull: compare `hv owner show` against
  the inviting device out of band. A node that has not pinned keeps the previous genesis rule — it must,
  or a mixed fleet would stop syncing — so an un-upgraded or unpinned node is still exposed until it
  pins, and it can project a different owner than a pinned peer. A rival a peer already stored is never
  removed, so a node that refused it and a node that holds it keep different Merkle roots permanently;
  the projections agree once every node pins the same genesis, the roots do not; `hv doctor genesis` reports exactly that state. And on a first pull of a chain it holds none of,
  an admitted peer can advertise a chain that is not the fleet's: every established node refuses anything
  past its own tip, so such an entry cannot become fleet truth, and under `hv sync auth --outbound enforce`
  the peer's identity is proven first.
- **Join-request replay semantics.** Join-requests are last-write-wins per `device_id`; a denied
  device can re-ask, and clearing a deny makes an older request visible again. This is intended
  (a device may legitimately re-request), but it is not replay-bounded. Documented, not changed.
- **Timestamp forgery by a quorum-holding adversary.** The election skew bound rejects a `basis_ts`
  that leads its own entry timestamp, but an adversary who already controls a voting quorum AND
  forges the carrying entry's timestamp far into the future can still pre-arm the dead-man switch.
  This only lets such an adversary *skip the waiting period* — controlling a quorum already permits
  a legitimate takeover after the wait — and the forged entries sort anomalously late. Residual,
  documented.
- **Backdating by a compromised RETIRED owner key.** Capsule write-authorization under the owner
  policy is point-in-time, judged by the entry's self-asserted timestamp against the owner-succession
  timeline (there is no positional anchor for a content entry, unlike an election `basis_ts`). An
  attacker who compromises a *former* owner key can forge a timestamp placing a malicious
  capsule/tombstone *inside that key's former term*, where the projection still honors it. A
  current-owner-only rule would avoid this but would also silently drop a prior owner's legitimate
  seals after every ownership change — so point-in-time is the deliberate choice (it is required for
  owner-resilience correctness). *Mitigation:* rotate capsules (and the upstream secrets) on an
  ownership change, the same hygiene already advised when a device is removed. Residual, documented.
- **Old ciphertext survives revocation/rotation.** A device removed (or an owner retired) still holds
  any capsule version it already synced; `rotate`/`tombstone` cut it off the *new* version only. To
  truly cut access, rotate the upstream token/secret too. Inherent to encrypt-to-device (you cannot
  un-send ciphertext); the CLI says so at `put`/`rotate` time. Documented.
- **Version currency under partition.** A capsule is a latest-version-wins projection over the local
  journal, so a device that is offline or partitioned when a secret is rotated or tombstoned keeps
  opening the version it already holds until it reconnects and syncs. Rotating the upstream secret
  bounds the damage; the offline device simply fails with the stale value. Inherent to offline-first
  sync. Documented.
- **Channel and source-class labels are self-declared.** An entry's `channel`
  (`sense`/`act`/`introspect`) and its source `context_class` (`primary`/`subagent`/`cron`) are
  asserted by the writer. They let an *honest* agent mark its own reasoning or background jobs so they
  count for less; they do not stop a dishonest admitted device, which can simply label everything
  `sense` and `primary`. What bounds such a device is identity: admission, the same-device discount
  and `cap_self`. Since 1.21 honest labels are applied consistently: an unrecognised `channel`
  counts as `introspect`, so a typo or a newer label never earns observation weight
  ([#72](https://github.com/projectmentor/hive-mind/issues/72)), and an `introspect` *fact*
  assertion no longer counts as corroboration ([#73](https://github.com/projectmentor/hive-mind/issues/73)).
  An unrecognised source class weighs 1.0, like an absent one: a class can only claim a discount,
  so there is nothing to fail open into.

## Cryptographic posture

- Primitives are **pure-Python** (Ed25519, X25519, Ed25519↔Curve25519, ChaCha20-Poly1305 per
  RFC 8439) so the tool is dependency-free and offline-resilient. They are **not constant-time**.
- **Timing side-channels:** extracting a key via timing requires an attacker who can measure the
  victim *process's* execution finely — i.e. local code execution or precise co-resident timing.
  Under the trust model (0600 local keys; a local-code attacker has already won by reading the key
  file directly), this is **out of scope**. Do not run HiveMind crypto as a remote oracle that
  signs/decrypts attacker-chosen inputs and returns fine-grained timing.
- **Ed25519 accept set (v1.20.1).** The faster `ed25519.py` accepts exactly what the reference it
  replaced accepted, including the reference's permissive rules (no `S < l` check, non-canonical `y`,
  cofactorless verification), so upgraded and older nodes agree on every entry. Strict RFC 8032
  checks would be a consensus change and would need their own contract version.
- **Verification memo (v1.20.1).** Each process remembers which signatures it has verified, keyed on
  the exact signed bytes, signature and key, so a changed byte is always checked afresh. Only the
  cryptographic result is cached, never authorization (admission, revocation, owner-as-of, writer
  policies, link authority). It is bounded and in memory only: persisting it would make `store.db`
  part of the trust base.
- A bundled crypto module that fails to import is now a hard `doctor` failure (`crypto-modules`),
  because `_verify_entry` falls through to accepting entries unverified when Ed25519 is absent — that
  degradation must be loud, not silent. Known-answer self-tests run on every `doctor` pass.

## Operational / scaling characteristics

- **Journal growth.** The journal is append-only and grows without bound, and every projection
  scans all of it. Since v1.20.1 ([#70](https://github.com/projectmentor/hive-mind/issues/70)) that is cheap: Ed25519 is roughly 60–180× faster than
  the reference it replaced and each signature is verified once per process, so on about 860
  entries a search takes about a third of a second and a write under a second (they took about 15 s
  and a minute). The cost is still linear in the journal: `tests/test_perf.py` fails if a
  1,000-entry journal pushes the adapter paths past their timeouts, and persisted or incremental
  projections are the next step if it ever does.
- **Best-effort recovery.** A truncated/garbled journal line (e.g. a crash mid-write) is skipped on
  read; `hv doctor` now surfaces the count so silent data loss is visible (`journal-integrity`).
- **`hv doctor --fix` blast radius.** `--fix` kills orphan daemons (by argv match), restarts the
  managed daemon (systemd unit on Linux/WSL, launchd agent on macOS), rewrites the foreign
  Claude Code config (with a backup), and repoints a `.peers.json` peer's URL host when its stored
  address fails and its device has since verified itself from another address with a signed request,
  or answers a signed `/hive/info` probe at a candidate host as exactly that admitted device at exactly
  that address (`peer-address`, [#7](https://github.com/projectmentor/hive-mind/issues/7),
  [#107](https://github.com/projectmentor/hive-mind/issues/107)). It also restarts the
  managed daemon when the source digest it loaded no longer matches its own checkout (`daemon-code`,
  [#112](https://github.com/projectmentor/hive-mind/issues/112)); a foreign listener on the port never
  triggers that. Run
  `hv doctor --fix --dry-run` to preview every mutating action before letting it run.
