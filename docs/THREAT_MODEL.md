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
   other interfaces. Beyond that, the read surface is gated by an application-layer signed-request
   envelope (`Hive-Auth-*`: an Ed25519 device signature over the request, verified against the
   governance admitted-set, with a freshness window + nonce replay guard). A *remote* reader must be
   an admitted device: `/sync/chunk` and `/sync/hello` require a valid signature; `/api/*` (the
   corpus/telemetry surface) and the dashboard are loopback-only (local operator) or a signed peer
   proxy; only minimal discovery (`/hive/info`, `/sync/merkle-root`, `/api/verify` — no journal
   content) stays open so an unadmitted joiner can still find and verify the hive. Enforcement is a
   per-node mode (`hv sync auth off|permissive|enforce`, default `permissive`) so a live fleet flips
   to `enforce` independently once all peers speak protocol 2 — no lockstep cutover. Tailscale ACLs
   remain the boundary for *who can connect*; the signed envelope is the boundary for *who can read*.
   (Closes GHSA-242f-7fxg-f7wm. WRITES were already per-entry authenticated on ingest.)

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
  silently sealed to. The X25519 ECDH rejects the all-zero (low-order) shared secret.
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
- **Forged clock to trip succession early.** A quorum election's dead-man timer is anchored on the
  proposal's `basis_ts`. A proposal whose `basis_ts` leads its OWN entry timestamp by more than a
  small skew allowance is rejected — a deterministic, entry-time-only check (no wall-clock), so the
  journal stays convergent. See "Known limitations" for the residual.
- **Local write races.** Journal appends take an exclusive `flock`, so a daemon and a CLI writing
  the same daily file cannot interleave mid-line and corrupt a record.
- **Cross-hive contamination.** Sync refuses to merge journals whose `hive_id` differs.

## Known limitations (in scope to document, NOT yet closed)

- **Bootstrap / TOFU window.** Before an owner is established, the hive accepts entries permissively
  so the genesis owner declaration can propagate, and owner establishment is trust-on-first-use: the
  first valid self-signed `owner` declaration wins. An attacker who injects an `owner` declaration
  before the legitimate one can front-run ownership. *Mitigation:* establish the owner before
  exposing the daemon, and verify the genesis `hive_id`/`owner_id` out-of-band when joining. Closing
  this in code is consensus-critical and deferred to its own change with multi-node convergence tests.
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

## Cryptographic posture

- Primitives are **pure-Python** (Ed25519, X25519, Ed25519↔Curve25519, ChaCha20-Poly1305 per
  RFC 8439) so the tool is dependency-free and offline-resilient. They are **not constant-time**.
- **Timing side-channels:** extracting a key via timing requires an attacker who can measure the
  victim *process's* execution finely — i.e. local code execution or precise co-resident timing.
  Under the trust model (0600 local keys; a local-code attacker has already won by reading the key
  file directly), this is **out of scope**. Do not run HiveMind crypto as a remote oracle that
  signs/decrypts attacker-chosen inputs and returns fine-grained timing.
- A bundled crypto module that fails to import is now a hard `doctor` failure (`crypto-modules`),
  because `_verify_entry` falls through to accepting entries unverified when Ed25519 is absent — that
  degradation must be loud, not silent. Known-answer self-tests run on every `doctor` pass.

## Operational / scaling characteristics

- **Journal growth.** The journal is append-only and grows without bound; projections
  (`_governance_state`, confidence) historically full-scanned it. The governance projection is now
  memoized on a content signature of just the governance entries (the heavy Ed25519 verification +
  election walk is skipped when governance is unchanged — the common case, since ordinary `fact`
  writes don't touch it). Very large histories still benefit from periodic operational review;
  streaming/indexed projection is a long-term item.
- **Best-effort recovery.** A truncated/garbled journal line (e.g. a crash mid-write) is skipped on
  read; `hv doctor` now surfaces the count so silent data loss is visible (`journal-integrity`).
- **`hv doctor --fix` blast radius.** `--fix` kills orphan daemons (by argv match), restarts the
  managed daemon (systemd unit on Linux/WSL, launchd agent on macOS), and rewrites the foreign
  Claude Code config (with a backup). Run
  `hv doctor --fix --dry-run` to preview every mutating action before letting it run.
