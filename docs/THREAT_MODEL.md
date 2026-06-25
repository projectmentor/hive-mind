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
3. **The network perimeter is Tailscale.** The sync daemon (`:9876`) is reachable by any device on
   the tailnet. There is no application-layer authentication beyond network reachability + the
   per-entry signatures above. Tailscale ACLs are the access-control boundary for *who can connect*.

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
  managed systemd unit, and rewrites the foreign Claude Code config (with a backup). Run
  `hv doctor --fix --dry-run` to preview every mutating action before letting it run.
