# HiveMind threat model

This is the authoritative statement of what HiveMind defends against, what it assumes, and what
is explicitly out of scope. It is meant to be read alongside `INTERNALS.md` (mechanics),
`SYNC_API.md` (sync) and `HV_ARCHITECTURE.md` (the two planes). When a security-relevant change lands,
update this file.

## Trust assumptions (the security rests on these)

1. **Local key files are 0600 and the host is not compromised.** Device and owner Ed25519 seeds
   live in a key directory (0700) outside the working tree, as `device-key` and `owner-key` at mode
   0600 (2.0; before it, `HIVE_HOME/.device-key` and `.owner-key`, which still work, with a warning).
   Anyone who can read those files *is* that device/owner. `hv doctor` hard-fails on a
   group/other-readable key or an open key directory, and `--fix` re-tightens it (and now loudly
   reports if the chmod itself fails). Moving the seeds out of `HIVE_HOME` keeps them out of the
   directory agent CLIs run in, index and pass to tools; it is not a boundary against code running as
   the same user, which can still read a 0600 file. That is why the **owner** key is also sealed at rest
   (2.0): `owner-key.sealed`, scrypt then ChaCha20-Poly1305 under a passphrase the operator types, and
   unlocked once per `hive-mind` command. A process running as the same user can read the ciphertext but
   cannot sign governance without the passphrase, unless it can also read the passphrase where it is
   typed or supplied (a keylogger, a `HIVE_OWNER_KEY_PASSPHRASE` left in an agent's environment); that
   stays out of scope. The device key is not sealed: every entry is device-signed, unattended, including
   by the sync daemon, and a device key's authority is its admission, which the owner can revoke.
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
- **Owner forgets are reversible, and only by the owner (contract 1.24, #46).** `hive-mind retract --owner` floors a fact; `hive-mind unforget` reverses it. Both are owner-signed `retract` entries (an unforget carries `unretracts_ref`), and the owner acts on one fact's text form a timeline sorted on `(timestamp, node_id, seq)` where the latest honoured act wins. An act is honoured only when signed by the owner AS OF its own journal position (`_owner_at`, the capsule/cell/link rule), so a previous owner's forget survives a transfer, succession or election (before 1.24 it resurrected), and a successor's signature from before their term does not count. An unsigned or forged unforget is ignored, and an unforget is never grandfathered. Owner acts are governance, not evidence: they do not move a fact's last-evidence time, so an unforgotten fact's confidence re-derives from its surviving evidence. A pre-1.24 node skips an unforget (it has no `retracts_ref`) and keeps the fact forgotten until it upgrades: projection skew, never divergence. **Closed in 3.0 (#122).** 3.0 deleted the pre-genesis grandfather. A forget positioned before the genesis owner is
  honoured only if the owner signed it as of its position, which a pre-genesis act never is, so a backdated unsigned
  forget from an admitted device erases nothing, whatever `forget_writers` says. The key stays a recognised governed
  key whose value the projection no longer reads (a 2.x `set-config` entry is not "unknown"): `legacy` is refused with
  a sentence, `owner` is accepted and records the close for 2.x peers. The pre-owner path is untouched: before any owner
  exists the `owner:owner/owner` source-tag forget is honoured and is never grandfathered. This is a tightening, so it
  adds no attack surface, and the admitted-device backdating hole of #122 is removed for good.
  What 3.0 still has to do is keep the facts the last 2.x hid. The **migration set** is the facts a 2.x projection
  hides by an unsigned pre-genesis forget alone and 3.0 would show again (on a hive whose key is `owner` it is empty by
  construction: 2.x already ignored those forgets). `hv doctor` (`forget-authz`) fails while the set is not empty, naming
  each fact by `h:` id, and passes on an empty set whatever the key says, stating it as a list (`closed: 0 facts depend
  on a pre-genesis unsigned forget`, with `dangling: N` when forgets target nothing). `hive-mind migrate forget` (and
  `--check`, which never writes) lists the set with each fact's text and, on the owner's `y`, re-signs those forgets and
  records `forget_writers=owner` so a 2.x peer closes its own grandfather too. The list is shown because a planted forget
  would be re-signed too; `hive-mind unforget <h:id>` lets a fact back instead and settles it the same way. Nothing is
  re-keyed (#130): the routine only appends `retract` entries naming the fact's current ref.
  Residuals, all accepted for 3.x because each needs an admitted device (David, 2026-10-09), each with its rule where one
  helps: **(a) a dangling late arrival**: a pre-genesis forget whose target is not in the journal is not re-issued (that
  would be a new owner claim), so a fact that reaches a 3.0 node after it shows; `--check` prints the dangling list, and
  the rule is to sync the fleet before running `migrate forget`. **(b) a pre-genesis forget planted on a closed hive**: a
  forget an admitted device wrote before genesis on a hive that already closed is ignored, not re-signed, and is not
  flagged; the rule is to review pre-genesis owner-source forgets before running `owner init`. **(c)** the update-gate
  residuals below (the manual-pull window, the same-user reflog collision, and no rollback when the reflog is disabled).
  **(d)** nodes still on 2.x keep honouring the grandfather until they upgrade (projection skew); `hv doctor`
  (`fleet-contract`) lists them. **(e) a revoked writer's pre-genesis forget in the migration set**: the set is computed over the
  journal's entries after the purge filter, not over the projection's entries, so a pre-genesis forget by a device that was later
  revoked (and, in shapes not probed, one the timestamp bounds or the module write policy skip) can list a fact the 2.x display
  already showed. The error only adds to the list. `migrate forget` shows each fact's text and defaults to N, and `hive-mind unforget
  <h:id>` settles one; the rule is to read the list before answering `y`, and to answer N for a fact whose forget came from a
  device the owner has since revoked.
- **The update gate (3.0, step 0 of `hive-mind update`).** The new `_update.sh` runs `migrate forget` after the
  re-exec, before any unit, restart, rebuild or wiring. A non-empty migration set is migrated on the owner's `y`, and
  otherwise the update exits 1: if this update's own switch is identified (an exported marker, or an exact
  `merge <sha>: Fast-forward` / `reset: moving to <sha>` reflog row inside a few seconds of the start) the tree
  returns to the pre-switch HEAD with `reset --keep`, so local edits stay; else HEAD is left where it is. There is no
  skip flag. Residuals, all accepted for 3.x: between a 2.4 updater's merge and step 0 (seconds) another `hv` process
  runs the new code on an un-migrated journal; a manual `git pull` bypasses the updater and is covered by detection
  (`hv doctor` `forget-authz`, the session-start nudge, the read-verb notice) rather than prevention; a same-user git
  command that writes an exact script subject in those seconds is indistinguishable from the updater's switch (a
  local-operator collision, not a journal act), and with the reflog disabled no switch is identified and nothing is
  rolled back. Rule: after a refused update, check `git log` and `git status`. The re-exec'd process runs step 0
  before its own fetch too (the parent already switched the tree, and a failed fetch or a refused step below would
  otherwise leave that switch in place unchecked), and runs it again after any switch of its own. The owner machine
  updates first; every other machine updates after the owner's re-issues have synced.
- **Entity rewrite by name (SECREV B3).** An `entity` write by a device that is neither the name's creator nor
  owner-signed is weigh-only: the projection skips it, as a link from such a device is held to evidence. The
  creator is the name's first applied `entity` entry in replay order `(node_id, seq)`, the rule #208 uses for
  modules. `hv doctor` (`entity-rewrite`) counts the writes this stops. Residual: that order is not time, so an
  admitted device whose `node_id` sorts first can claim a name nobody has yet written on this node; the owner's
  signed write still commands it. Nodes before this rule still let the last write win until they upgrade
  (projection skew). Making the creator the earliest by a signed field would be a signing-format change.
- **Entity identity (3.0, PR 8).** An `entity` entry now says which entity it writes: `new_identity: true` creates
  one, `identity_ref` updates the entity whose creating entry that ref names, and an entry with neither (every one a
  2.x node wrote) is read by name as before. The creator rule above is keyed by identity: only the identity's creator,
  or an owner-signed write, rewrites it; any other admitted device's `identity_ref` write is weigh-only. An update
  that names no entity, an entity of another name, another update, or carries both fields is skipped like any entry
  whose body does not match its type. Nothing on the wire, in signing or in the manifest changes, so a party that is
  not admitted can do nothing it could not do before. Residuals, all admitted-device and accepted for 3.x: (1) a create
  needs no authority, so an admitted device can create a second entity of a name another device already uses
  (impersonation by label; it replaces nothing and shows up as a repeated name). Rule: address an entity by its `h:` id,
  not its name, wherever the difference matters; `hv entity list` shows the id of every repeated name. (2) Two devices
  that each `entity add --name X` before they have synced make two entities; `add` writes a create when it finds none.
  Rule: sync before adding a name another device may already have. (3) A legacy name group's identity is still the first
  applied entry in replay order, which is not time (a lower `node_id` arriving later moves it); only an entity a 3.0 node
  creates is pinned. (4) An update is read against the journal as a whole, so one whose target has not synced yet is
  skipped until it does. (5) Replay order `(node_id, seq)` also decides which of two writes to one entity wins: an owner
  update from a lower `node_id` is overwritten by the creator's later-replayed create. (6) A 2.x node reads
  every entry by name and shows one entity per name; journals converge and views diverge. Rule: update every node
  before creating a second entity of a name.
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
- **Loopback is trusted by source address, `Host` and, on a POST, a token.** A request from a loopback
  address skips the signed-request gate, so the daemon no longer trusts the address alone: the `Host`
  header must name this listener (`localhost`, a loopback address or the bound address, on its port) or
  the answer is `421`, which stops a DNS-rebinding page from reading the dashboard or the module API.
  `/sync/ingest` refuses a request the browser labels cross-site (`Sec-Fetch-Site`, or an `Origin` that is
  not this listener), and a POST that arrives on loopback must carry the `Hive-CSRF` token from
  `$HIVE_HOME/.csrf-token` (0600, never synced) or a valid signed envelope. A local process running as the
  same user can still read the token file; that boundary is the OS account, as before. The dashboard is
  the `hdash` module's since 3.1 (below); it is served with `Content-Security-Policy: default-src 'self'` and
  builds its page from text nodes, so a journal value cannot run as script.
- **The dashboard is a module, `hdash` (3.1).** Moving it out of the sync daemon changes who serves the page, not what
  an unadmitted or remote party can reach. The daemon serves no page now (`/` is 404) and keeps `/api/*` exactly as
  it was: loopback or a signed peer, `Host`-checked. `hdash serve` is a second loopback listener (`127.0.0.1`, config
  `port`, default 9896): GET and HEAD only, a request whose `Host` is not that loopback address is 403 (the
  DNS-rebinding guard the daemon has), no `Origin`-based write because it has no write. It reads the hive two ways:
  signed `/v1` calls as an admitted module device (`overview`, `tags`, `related`, `audit`, `peers`, `verify`, declared
  in its `reads`, plus `search` and `item`), and the daemon's loopback `/api/*` for `status`, `telemetry`, `entities`
  and a peer's `?node=` view (the daemon still resolves that address itself and signs the peer request). Residuals,
  all needing a local process or an admitted device and so accepted for 3.x: any local process can `GET` the hdash
  port and read what the page shows, as it could read the daemon's `/api/*` before (the OS account is the boundary;
  `reads` is a declaration, not a sandbox here); hdash's `/v1` reads carry tailnet addresses (`peers`) and the local
  integrity verdict (`verify`) to an admitted module device that declares them; and `search` and `item` through `/v1`
  leave out owner-forgotten facts that the old page showed. Operator rule: remove hdash on a node where other local
  OS accounts must not see the hive (`hive-mind module remove hdash`); the daemon's `/api/*` stays loopback-open
  either way, as before.
- **Session telemetry is a module, `hwatch` (3.1).** `hv telemetry` forwards to `hwatch telemetry` through
  `hive-mind module run` (a subprocess; `hv` imports no module code and adds no control-plane edge). The forwarder passes
  its arguments through unchanged and runs no shell. The store is the same local file as before, outside the journal,
  and core now only opens it read-only for the loopback `/api/telemetry`. Residuals, all needing a local process or an
  admitted device and so accepted for 3.x: `hwatch telemetry record` writes whatever its caller passes (no signature,
  as before), so a local process can forge or hide a session's tokens and cost; and a module installed under the name
  `hwatch` is run with the operator's own account when a hook fires (the module is signed under its pinned publisher,
  as every module is). Operator rule: treat `hv telemetry report` as a report, not a control.
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

- **A module's own doctor runs module code (3.1, #294 PR 4).** `hv doctor`, `module add`, `module update` and the 15-minute
  `hive-doctor` timer run the `doctor` argv of a contract-1 module as the operator's user, in the module's directory,
  with no shell, a 20 s kill timer on its process group, a 64 KiB output cap and an environment built from nothing
  (`PATH`, `HOME`, `LANG`, `HIVE_MODULE_NAME/DIR/KEY_DIR`, `HIVE_DOCTOR_MODE`): the owner passphrase, the owner seed and
  the identity stash are not passed. It runs only for a module whose installed tree verifies against a manifest signed
  by the publisher key pinned at install, so an unadmitted or unauthenticated party cannot make the core run code that
  the pinned publisher did not sign. Its answer is data: capped, control and bidi characters removed, shown as text,
  cached in `$HIVE_HOME/.module-doctor/<name>.json` (0600, a 0700 directory of the operator's, never through a link or a
  shared directory), and read back through the same cleaning. The fix step of `hv doctor --fix` never runs module code (its check pass does run the doctor, which is how the timer gets `timer` mode) and never reads
  the answer as an action. Accepted residual (admitted device or the operator's own account): module code already runs
  as the operator through its unit and `module run`, so the doctor adds no privilege; a process of the same user can read
  the owner agent socket or the key files whether or not the environment is scrubbed. The operator rule is the existing
  one: pin only publishers you trust, and treat a module's `doctor` as that module's code. A hung doctor delays
  `hv doctor` by up to 20 s per module, one module after another (a wall-clock deadline on the read; a child that holds the pipe open or leaves the process group cannot extend it, though one that left the group may outlive the kill).
- **The owner-unlock agent (3.1, #295).** `hive-mind owner unlock [--for D] [--module m]...` asks for the passphrase once and
  hands the unsealed owner seed to a per-user agent process, which signs only these governance acts: `admit` of a
  **module** device for a module named by `--module`, `revoke` of a module device that same agent session admitted, and
  `set-config` under `x-<module>:`. Everything else (`transfer`, `rotate`, a voting device, forgets, owner-signed links,
  an unknown action) is refused and the command falls back to its own passphrase prompt. The agent signs only bytes that
  re-canonicalise to themselves and name its own key; it has no op that unlocks, returns the seed, or signs arbitrary
  bytes. The socket is mode 0600 in a 0700 directory owned by the user, and a connection from any other uid is closed
  unread. The seed lives only in the agent's memory (never a file, argv, env or log); `--for` is an idle timeout (1m
  to 8h), 12h is a hard cap, and expiry, `owner lock`, a suspend/resume jump or a vanished socket end the process.
  Residuals, none changing a journal, signature or sync byte: (1) *admitted-device class (accepted for 3.x):* while it
  is unlocked, any process running as the operator can obtain signatures for the acts above, so it can set config under
  an installed module's prefix or admit a module device for a module named at unlock; it can never get the seed or a
  voting device. (2) *Swap and hibernation:* the agent is `PR_SET_DUMPABLE=0`, has no core dump and `mlock`s the seed
  page, but the pure-Python signer builds big integers on the ordinary heap and keeps an immutable copy of the seed, which
  CPython can neither zero nor lock; process exit clears them, swap or a hibernation image may not. (3) Root and a kernel
  bug can read the agent. **Operator rule:** lock when you leave a machine you share with untrusted local code; use the
  shortest `--for` that fits; unlock with `--module <name>` for an install; keep swap encrypted; lock before
  hibernating; do not unlock on a machine that runs code you do not trust. `HIVE_OWNER_KEY_PASSPHRASE` is ignored by
  `owner unlock`. Windows has no agent yet.

- **`hv`'s code is in several files (3.1).** The body of `hv` runs from `hivemind/parts/*.py`, chosen by one list,
  `PARTS`, in `hv` itself (no glob, no config, nothing under `HIVE_HOME`), read as bytes and run into `hv`'s own
  namespace. A part has exactly the authority `hv` has, no more. Parts are tracked `.py` files, so the signed source
  manifest hashes them as it hashes `hv`, and `hv verify` and the update gate see a changed or added part as they see a
  changed `hv`. A file in `hivemind/parts/` that `PARTS` does not name never runs. Residual: whoever can write the
  install directory can replace a part as they can replace `hv`, which is the same position as before the split (an
  operator or admitted-device compromise, accepted in 3.x). Operator rule: the install directory is writable only by the
  operator. The S2 guards that scan `hv` read the parts too (`tests/_source.py`).

- **The S2 exec ban is a static name list, with documented limits (#382).** The guard in `tests/test_hv_parts.py`
  (`file_runner_violations`) reads the source of `hv` and the parts and reports banned imports, names, attributes and
  exact strings. It is a tripwire against an accidental or careless file-running call, not a sandbox. A name list
  cannot be complete, because Python reaches the same effect through names the list does not hold. These are not
  caught, and each stays out of the list for the reason given:
  - *Process spawn through `os`* (`os.execv`, `os.posix_spawn` and kin). `hv` already imports `os` and
    `subprocess` for its own work, so banning them would ban `hv`; the guard cannot tell a spawn of a fixed argv
    from a spawn of a planted one.
  - *`pickle` and `shelve` loads.* Loading runs code chosen by the data. The modules have legitimate uses and the
    data is not visible in the source, so a name match says nothing about what is loaded.
  - *`logging.config` factory lookup.* A config names a callable by a dotted string that the logging module
    resolves at run time. The string is data, and any dotted name is a valid value.
  - *A `sys.path` edit followed by a plain import.* The import names an ordinary module, and the file it loads
    depends on a path the source builds. Nothing in the import statement is wrong.
  - *Split or computed strings* (`"ex" + "ec"`, a join, a decode). A scan of literals sees the pieces, never the
    result, and the names reach the call through `getattr` or `sys.modules` lookups on computed keys.
  - *The string `code`.* It is exempt on purpose, because it is an ordinary word in `hv` (error codes, exit
    codes). With `import code` banned, the one leftover is a `code` module that something else already loaded and
    reached through `sys.modules`.

  Residual: only someone who can write signed core (`hv` or a part) can plant any of these, which is the position of
  the parts bullet above: an operator or admitted-device compromise, accepted in 3.x. A change to signed core is
  seen by `hv verify` and the update gate as a changed file, whatever it calls, so the guard is a second line, not
  the first. Operator rule: review every change to `hv` and `hivemind/parts/` as code, and do not read a green S2
  guard as proof that a change runs no file. The guard is not extended to close these; widening a name list
  closes one spelling and leaves the next.

- **Outbound verification is opt-in.** The outbound mode defaults to `permissive`, which flags and never
  withholds a push, so a node is protected from an impostor at a peer's address only after it runs
  `hv sync auth --outbound enforce`; `hv doctor` (`peer-identity`) says when every peer is ready. A peer on
  a legacy identity (no device key) can never pass `enforce`, and a peer stored under a host name rather
  than its advertised IP is `addr-unproven`, so `enforce` only pulls from it. The merkle-root comparison
  that ends a round for a peer already in sync stays unsigned: an impostor that echoes this node's root
  can stall its sync with that peer (a denial of service, not a disclosure). Under `enforce` the pull is
  unsigned until the hello is `verified`: an `unsigned`, `unadmitted` or `addr-unproven` peer is sent no
  `Hive-Auth-*` headers (a legacy peer that does not check signatures still answers; an enforce-mode peer that
  has not proved its address is not pulled until it has, and `hv doctor --fix` repoints a proved address).
- **Update rollback protection needs one update to start, and trusts the signer's count.** `hive-mind update`
  refuses a fetched commit that is not a descendant of the installed one unless `--allow-rewind` is passed, and
  refuses a signed manifest whose `sequence` (the integer `git rev-list --count HEAD` of the commit sign.yml
  signs, inside the signed bytes of `verify.json`) is lower than the installed manifest's. Git ancestry is the
  origin's to write, so an older signed tree re-wrapped as a descendant commit is a fast-forward; its signed
  `sequence` is still the old, lower number and it is refused. This closes the rollback for a node that has run
  one update with this script: an installed manifest without the field counts as 0, and old nodes ignore the
  field. Residual: a node that has not yet updated is not protected, `--allow-rewind` is the operator's
  override, and the `sequence` is only as monotonic as the history the signer counts, so a history rewrite that
  shortens `main` below an installed node's number needs `--allow-rewind` there. Whoever holds the release key
  can still sign any number.
- **A signed read is not bound to an audience** (SECREV B6). The `Hive-Auth-*` envelope signs the method, path,
  query, body hash, timestamp and nonce, but not the node it is sent to, and each node's nonce cache is its
  own. An admitted, unrevoked peer that this node signs reads to can replay them to a third node inside the
  freshness window (`HIVE_SYNC_AUTH_WINDOW`, default 300 s; the nonce is remembered for twice that) and be
  served as this node, for as long as the third node still admits this one. Under outbound `enforce` the
  address is a verified one: a pull is unsigned until the hello is `verified`, and the dashboard's signed
  telemetry read (`GET /api/telemetry`) and the daemon's per-node proxy (`GET /api/overview`, `/api/search`,
  `/api/status`, `/api/telemetry` with `?node=`) are not sent to an address whose `/hive/info` is not
  `verified` for that device; those reads follow no redirect. The hello request, the `/hive/info` probe that
  gates these reads, and `/sync/merkle-root` stay signed open-discovery reads (a replay of one does not disclose
  the journal). Closed node-locally, with no
  format change: a device the owner revokes or purges is dropped from `.peers.json` when the revoke or purge
  projects, and `hv sync now`, the sync loop and the doctor probes send it nothing. An entry is matched by
  its device `id` (`k1:…`); an entry labelled otherwise (`group admit --principal` seeds the principal) is
  matched by the URL of the ended device's last join-request at or before the revoke or purge that ended it (one this node received at or before the act counts whatever stamp it carries; a
  join-request it writes afterwards leaves that URL where it was, and so does one this node received after the
  act's stamp, by its own arrival record, whatever stamp it carries), else by the one device verified at its host in
  `.peer_candidates.json`. A match is dropped when an admitted device was verified at, or advertised, the same
  host, so a reinstall at the old address keeps its entry once the new device is admitted; a join-request
  from a device that is not admitted is only a claim and keeps nothing. An entry set by hand with no device
  `id`, at an address that matches neither, keeps getting signed reads, so list peers by their `k1:…` id.
  Accepted for 3.x (an admitted device only): clock skew between this node's receipt clock and the act's stamp, and
  entries that arrived before arrival records existed (they keep the stamp rule); that device can append a join-request whose URL differs from
  the one `group admit` stored (the last one wins), after which the principal-labelled row matches nothing,
  and a second admitted device whose join-request names the host keeps the row; list those peers by `k1:…`
  id. What remains: a peer you still list can do this to a third node, so
  list only devices you trust to hold your reads, and revoke a device before you stop trusting it. A daemon
  restart empties the nonce cache, so a request captured within the window is replayable once. Binding the
  responder into the signed bytes is a wire-format change and waits on the maintainer's decision.
- **Anyone can obtain a responder signature over a nonce it chooses.** `/hive/info` is open discovery,
  so any reachable host can make a node sign. The statement holds only public facts (node id, hive id,
  advertised address, protocol) and a digest of the answer that caller was served; the `hive-hello-v1`
  domain tag keeps it from ever passing as a request or entry signature, and the address binding keeps a
  relayed one from proving anything. The only caller-chosen input is the nonce, which must be 32 to 64 hex
  characters, and the signer refuses any path but `/sync/hello` and `/hive/info`. An unhandled error answers
  a generic 500 body; the exception text stays in the daemon's own log. Signing runs inside the request-slot cap and per-address rate limit.
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
- **Backdating by a compromised RETIRED owner key.** Owner authority is point-in-time: an act is judged by its
  self-asserted position against the owner-succession timeline (there is no positional anchor for an entry, unlike an
  election `basis_ts`), because a current-owner-only rule would silently drop a prior owner's legitimate acts after every
  ownership change. The owner signature proves the KEY signed, not WHEN, so a *former* owner key could stamp an act
  *inside its own former term*, and the projection would honour it. That covers `transfer` (which re-routes
  succession), `admit`, `purge`, `set-config`, `freeze-timestamps`, owner forgets, hard links, capsules and cells.
  *Closed (SECREV A3):* the projection honours an owner-signed act only when the device carrying it is the genesis device
  or was admitted, at or before that position, by an honoured owner act and not since revoked or purged. A device nobody
  admitted proves nothing about when the retired key signed. A device that claims a nominated succession is NOT admitted by the claim (the successor key signs the payload, not the device, so a keyless device could re-carry it): the successor's device must be admitted before the claim, and a claim carried by a non-member is uncarried and moves nothing.
  `hv doctor` (`succession`) reports, without changing which act is honoured, when a succession act reached this node
  after one that sorts later, and warns when an owner-signed act is dropped for want of a member carrying it.
  *Residual:* the carrier check closes an unadmitted device; an admitted device can still place a public `claim-succession`, and can place a different `transfer` when it holds the retired key, because the signature names no position. The follow-up is a design issue for that signed position, waiting on David's word. Owner acts must be made from an admitted device or the genesis device; a new device is admitted from one of those. *Mitigation:* rotate the owner key after any succession and treat the retired key as a live secret; rotate capsules (and the upstream secrets) on an ownership change, as when a device is removed.
  Operator rule: after nominating a successor, sign no other owner act until the succession lands; after any succession or transfer, treat the retired owner key as a live secret: rotate away from it and keep no plaintext copy.
- **The owner signature does not cover the entry type.** The owner signs the payload minus `owner_sig`; the entry `type` is outside those bytes, so an admitted device could re-append an owner-signed capsule body as a `cell` (or the reverse) and the projection would honour both: a capsule that will not open, a cell whose command is empty. It moves an existing owner-signed body; it cannot author one.
  *Closed (SECREV B1):* the signed bytes are unchanged (no `type` and no version in the payload; D-7 minimal-first). `merkle.envelope_problem` gives each owner-authored content type a shape only that type carries (capsule: `alg` and `wraps`; cell: a `kind` of `tool` or `agent`; comb: a `cells` list), at ingest, on a local write and in the reader, so a body under another type is refused, or skipped and counted (`skipped_lines`) when already on disk.
  *Residual:* the shapes are disjoint for every body `hv` writes, not for every body an owner could sign. A hand-written, owner-signed body that carries the fields of two types (a cell `kind` plus a `cells` list, say) fits both rows and can still be re-appended under the other. Only a `type` inside the signed bytes closes that, a signing-format change waiting on David's word. *Mitigation:* publish cells and combs only through `hv wire --add`, which writes one type's fields, and do not sign a body that mixes them. A cell published without a `kind` is now refused (it could never be wired).
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

- **Manifest contract 1 widens what a signed manifest can say (3.1).** The new fields (`cli`, `runtimes`, `status`,
  `doctor`, `reads`, `config_schema`, `ui`) sit inside the signed `module.json`, so the publisher key and the file
  digests cover them and `validate_manifest` refuses a field it does not know, a malformed one, or a contract-1 field
  on a manifest without `contract`. They are declarations the operator reads, not a sandbox: a module is unconfined as
  the operator (B9), so `reads` limits `/v1` and nothing else (any local process can GET the loopback `/api/*` routes),
  `runtimes` names programs and never directories, and `status`/`doctor`/`cli.command` run as the operator like
  `service.command`. The validator refuses what would put a person's data or a shell in the signed text: a default on
  an `identifying` or `secret` key, a secret outside node scope, a `ui` action whose parameters are not drawn from an
  enum or a data-source path, and a `ui` entity panel that names another module's prefix. `module check` lints shipped
  files for identifying text and warns; it cannot prove absence, so a publisher still reads the diff. `module check
  --run` runs the module's own `status` and `doctor` as the user who typed it, in a scrubbed environment and a
  temporary directory, with a time and size limit; a module that spawns a detached child can outlive it. Operator rule:
  run `--run` only on a module you would install. Only an admitted publisher key signs a manifest, so a remaining gap
  that needs one is an admitted-device residual (accepted for 3.x).
- **Module config secrets sit in a plaintext node file (3.1).** A `secret` key in a `config_schema` is node-scoped by
  the validator, so it is never journaled or synced, and the CLI never takes it from the command line (shell history,
  `ps`), masks it in `list`, `get`, `--json`, `validate` and the doctor, and refuses it from `--fleet`. It is stored in
  `<module dir>/config` (0600, written through a 0600 temp file renamed in place), unencrypted: the operator, any
  process running as the operator, and the module itself can read it, which is B9 (modules are unconfined as the
  operator). `--set-env` and `--stdin` move a secret through the environment or a pipe, which other processes of the
  same user can observe; the operator rule is to prefer the terminal prompt. An `identifying` fleet value is journaled
  and visible to every admitted device for good, and the CLI says so when it is set; the validator refuses a shipped
  default on such a key, but a publisher's *files* can still carry identifying text (`module check` only warns). A
  `fleet` or `fleet-node` value is signed by the owner key; a `fleet-node` key names a device by a convention, so an
  owner can set another device's key, and a module must treat the layers as the owner's word, never as input from the
  device itself. Only an admitted device or the operator can reach any of these, so a gap here is an admitted-device
  residual (accepted for 3.x).
- **Runtimes, the unit PATH and the CLI launcher run as the operator (3.1).** A contract-1 module names programs
  (`runtimes`), never directories; the core puts a directory on the unit's and the launcher's `PATH` only if it is
  absolute, owned by the operator or root, and writable by neither group nor world, so another local user cannot plant a
  program there and a module cannot add a directory of its own. The check is of the directory itself, not of its parents or
  of the program, which the operator owns: a program that is a link into a directory someone else can write is the
  operator's to avoid. The launcher is rendered by the core from the manifest's name and argv, every word single-quoted,
  never from module text; it unsets the owner's passphrase, seed and key-directory variables before it runs the module.
  Those are the variables core reads, not a sandbox: the module runs as the operator and can read anything the operator
  can (B9). A launcher is written only into a bin directory only the operator (or root) can write, never over a file that
  is not the module's own unedited launcher, and a `module run` or launcher invocation inherits the rest of the caller's
  environment. `module status` runs the module's own code as the operator with a narrow environment, 20 seconds and 64 KiB,
  and a module that spawns a detached child can outlive it. A module's `cli.name` is a command on the operator's PATH for
  as long as it is installed, so the operator is trusting that name to the publisher: read it at install. Only an
  admitted publisher key signs a manifest, so a gap that needs one is an admitted-device residual (accepted for 3.x).
- **Read routes (3.1) hand an admitted module the hive's view of itself.** `GET /v1/overview`, `tags`, `related`,
  `audit`, `peers`, `verify` and `node` are served only to a module whose signed manifest names the route in `reads`,
  which the install summary (`module add` and `module update`) lists; they take no owner-key material, no capsule body and no governance state the feed
  does not already carry, and `related` and `audit` leave owner-forgotten facts out (`tags` and the `overview` counts do not: a module that declares them, which only an admitted device with a publisher-signed manifest can, sees the names and counts of forgotten facts too, an accepted residual). Three carry node-local data
  the feed does not: `overview` (this node's address and role), `peers` (a live probe of each peer, with tailnet
  addresses and join-request labels) and `verify` (the local integrity verdict). `peers` and `node` therefore give a
  declaring module the tailnet map. That is an admitted-device residual, accepted for 3.x: only a device the owner
  admitted as that module's, running a manifest the publisher signed, can read them. `reads` gates `/v1` and nothing
  else: any local process can still `GET` the loopback `/api/*` routes, which answer every loopback caller (B9), so
  `reads` is a declaration the operator sees, not a sandbox. The read bucket (a burst of 60, then 5 a second, per
  device, in memory) bounds how much work a module can ask of the node (`overview` and `audit` read the whole
  journal); it resets when the daemon restarts, and it is not a defence against the operator's own processes.
  Operator rule: install a module that declares `peers` or `node` only if you would give it the tailnet addresses.
- **`module install` never prompts for the owner key under `--non-interactive` or `--json` (3.1).** It opens the key only when
  it is already open, unsealed, its passphrase is in `HIVE_OWNER_KEY_PASSPHRASE`, or the unlock agent covers the acts it will
  sign; otherwise it exits 5 before it fetches or writes anything. It adds no signing path: the acts are `module add`'s
  (`admit`, `revoke` on a failed add, `set-config` for `--set-fleet`), made through the same gate, and a re-run of an installed
  module signs a fleet value only if the journal does not already hold it. `--json` prints the messages the steps printed;
  those name keys and never values, and a secret is never echoed. Anything that runs as the operator can set the passphrase
  variable or reach the agent, as before: an admitted-device residual (accepted for 3.x).
- **`hv doctor` `owner-copies` is a name-only check.** It looks one level deep in four directories
  (`$HIVE_HOME`, the current directory, the home directory, the identity stash) for `hive-owner-*.key` and
  the stash's `.owner-key`, and never opens a file. The default export and `--plaintext` share a basename,
  so it cannot tell a sealed export from a plaintext one. A `--plaintext --out` under another name, or a
  copy outside those directories, is invisible to it. Operator rule: delete exports after moving them
  off-device.
- **The release key is a repository-level secret.** `sign.yml` runs the test suite in a job with no secrets
  and signs in a separate job (`needs: test`, `environment: signing`, deployable from `main` only), so a pull
  request's code never runs with the key in scope, and `tests/test_workflow_secrets.py` fails if a job that runs
  pytest references `secrets.`. `HIVE_SIGNING_KEY` itself stays a repository secret (its seed cannot be
  recovered to move it into the Environment), so a workflow pushed to another branch by someone with write
  access could still reference it. Only an admitted party (a repository writer) can do that; accepted for
  3.x. `SIGN_APP_ID` and `SIGN_APP_KEY` are read from the `signing` Environment once added there, and from
  the repository until then. Operator rule: keep write access to the repository to the people who may sign.
- **Credentials handed to a cell run in the operator's uid.** A tool cell's secrets travel in the child
  environment, not the command line. Any process with the operator's uid can read `/proc/<pid>/environ`,
  ptrace the shell or run `hv capsule get`; that is the design. A secret is inherited by every grandchild,
  crash dump and sourced profile, and a step that puts `$NAME` on a tool's argv reopens the process-list
  leak. A cell author runs arbitrary shell as the operator, so credential-name validation removes
  footguns, not that authority. A capsule putter under `capsule_putters=fertile` chooses the value; it is
  inert unless a step evals it. Per-OS environment protection is unverified outside Linux and WSL.
  Operator rules: never `eval` a credential, never place `$NAME` on argv, and keep `cell_writers` and
  `capsule_putters` at `owner` unless every admitted device is trusted. The same holds for a module's
  per-node config: a secret key is read from stdin, a file or a prompt, never argv.

### Boot pass (`hv doctor --boot`, 3.1, #344)

`hive-doctor-boot.service` runs the same `hv doctor --fix` code as the timer, once per boot, as the user, with no new
privilege. Its fixes are the existing ones (restart the managed unit, re-render a module unit its verified manifest
renders, re-wire hooks); it adds a bounded wait and a report, and writes no file outside the hive. The report is a
fact tagged `boot`, source `doctor`; its text is built from check names and details this node computed, cut to 900
characters. It runs `claude --version` and `grok --version` (the first of each on the user's `PATH`) with a 10-second
bound, so a replaced binary on that `PATH` runs at boot as it would at any shell. That needs write access to the
user's own `PATH`, so it is an admitted-device residual (accepted for 3.x; the operator rule is to keep `PATH`
directories user-owned). The Windows logon task is written by the installer or `hive-mind update`, not by doctor; its
distro and user names are refused unless they are plain `[A-Za-z0-9._-]`, so nothing reaches the Windows command
line quoted. Doctor only checks that the task exists; it does not check what the task runs.

### Module trust tier (3.1, B9)

A signed manifest may carry `trust`: `trusted` or `confined`, absent meaning `trusted`. In this change the field is only
validated, shown at `module add` / `update` / `list` and recorded; **no unit is hardened yet**, so a `confined` module
runs exactly as a `trusted` one until the security pass adds the confined unit. Do not rely on `confined` for isolation
before that lands. The tier can go down on a node (`module trust <name> confined`, node-local) and up only with the
owner key (clearing that lowering, or an update that signs a higher tier than the node last held), never above what the
publisher signed. Residual, needing an admitted device or local access and so accepted for 3.x: a process running as the
operator's own user can edit `.modules.json` and so change the node-local tier; the OS account is the boundary. A
publisher key holder can sign `trusted`; the pinned key and the operator's look at the printed tier are the control.

## Seat steering (agent seats driven by GitHub and the hive)

A seat is an agent session that the runner wakes on a GitHub event or a hive entry. Text a seat reads can steer
it, so the sources are gated:

- **The GitHub channel gate** (`channel.<name>.authors` and `channel.<name>.author_classes`). `authors` lists
  logins; `author_classes` lists the classes (`owner`, `member`, `collaborator`, ...) mapped from GitHub's
  `author_association`. Both unset admits nobody. On this fleet they are fleet keys, set by the owner with a
  signed `hive-mind config set`: `authors` is the owner's login and `author_classes` is
  `owner,member,collaborator`. The shipped dev workflow's addressed rules also require one of those three
  classes. An event is admitted when its author's login is in `authors` or its association maps to a class in
  `author_classes`. Text from anyone whose login is not listed and whose class is not listed is dropped before it
  is classified, so it never reaches a seat or wakes one.
  The gate therefore admits the owner's account plus anyone the owner makes a collaborator on a watched repo (or
  an org member, if the repos move to an org). Today the only collaborator on all three repos is the owner.
- **Hive events are not author-gated.** Any admitted device's entry reaches the hive channel.
- **Fork heads are not followed.** When `head.repo` is not the base repository, hwatch takes no later head move
  and reads none of that head's checks. The first opened event from an admitted author is still yielded and
  carries that head sha. hwatch itself checks no head out.
- **Untrusted bodies are framed as data.** Whatever text a seat does read from GitHub or the hive reaches it
  marked as data to read, not as instructions.

**Residual (accepted for 3.x).** Seats run as the operator's OS user with the operator's `gh` login. A seat
steered through the owner's own account, or through an entry signed by an admitted device, therefore acts with the
owner's GitHub scope. A narrow seat token and a separate OS user for seats would only limit the damage after one
of those two sources is already compromised, so they are not built. Both sources are inside the trust boundary
this model already draws: an admitted device is a member of the hive, and the owner account is the owner.

Operator rules:

- Keep `channel.github.authors` to the owner's login.
- Add no collaborator (or org member) to a watched repo while `author_classes` includes that class.
- Run seats only on nodes the owner controls.
- Admit no device the owner does not control while seats run as the operator.

**Reopen if** any of these arrives: federation, hosted relays, an admitted device the owner does not control, or a
second login or collaborator admitted by the gate. Tracked in hive-mind-private #44.

## Cryptographic posture

- Primitives are **pure-Python** (Ed25519, X25519, Ed25519↔Curve25519, ChaCha20-Poly1305 per
  RFC 8439) so the tool is dependency-free and offline-resilient. They are **not constant-time**.
- **Timing side-channels:** extracting a key via timing requires an attacker who can measure the
  victim *process's* execution finely — i.e. local code execution or precise co-resident timing.
  Under the trust model (0600 local keys; a local-code attacker has already won by reading the key
  file directly), this is **out of scope**. Do not run HiveMind crypto as a remote oracle that
  signs/decrypts attacker-chosen inputs and returns fine-grained timing.
- **Ed25519 accept set (v2.0.3, #213).** `ed25519.py` accepts the reference's accept set minus the
  encodings RFC 8032 forbids: `S >= l` (§5.1.7), `y >= q` in R or A, and `x = 0` with the sign bit set
  (§5.1.3). Up to v2.0.2 it kept the reference's permissive rules for parity, so anyone could re-encode a
  signed entry's `sig` as `S + k*l` without the key; a node fed that copy first kept different bytes from
  the fleet for good, since ingest keeps the first copy of a `(node_id, seq)` and `sig` is inside the entry
  hash. No forgery was possible. The cofactorless equation stays, since RFC 8032 permits it. A node that
  has not updated to v2.0.3 still accepts malleated copies, so update every node.
- **Signer seam (3.1).** Every signature core makes (journal entries, owner governance, Hive-Auth envelopes,
  hello proofs) goes through `hivemind/crypto/signer.py`, and `tests/test_signer_seam.py` fails on a call to
  `ed25519.sign` anywhere else in core. Callers hold `sign(bytes)`, never the seed. With the one backend
  (the key file) nothing changes: the same bytes are signed and the signatures are identical, and a process that
  has unlocked a key still holds its seed in memory, as before. The seam is what lets a later backend sign with a
  key held elsewhere (hardware, TPM, gateway); verification stays fixed in core, so a bad backend can fail to sign
  but cannot make a forgery verify. Out of scope: module signing (the module's own key) and capsule decryption
  (X25519 secret material).
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
- **3.1: `hive-mind update` fetches and installs module code (#390).** Updating the core now also clones each installed module's recorded source and installs a newer version with no prompt, so a compromised source repository or a network path to it is in the update path. What bounds it: the same checks as `module update` (the manifest signature under the **publisher key pinned at the first install**, every file's digest, no unlisted file), so a source that cannot sign as that publisher gets nothing installed and the module is warned about or, if its installed copy no longer verifies, disabled; and an automatic update installs only a version strictly newer than the installed one (whole-string order over `major[.minor[.patch]]` integers; a suffix, leading non-digit or extra component is unorderable and refused) or a republish of the identical version string, so a source cannot roll a module back to an older signed release; a republish under the same version string is the one case kept, and it is whatever the publisher signed. Residual: the holder of the publisher key (or a source serving the publisher's own newest release) is trusted to run code as the operator on every node at the next `hive-mind update`, as at `module update`, but without the operator naming the module that time; `reads` a new version adds are printed but not confirmed. Operator rule: pin only publishers you trust to ship unattended, and run `hive-mind module refresh --check` first to see the plan. A disabled module's units, launcher and hooks are off, but its device stays admitted until `group revoke` (an admitted-device residual, accepted for 3.x).
