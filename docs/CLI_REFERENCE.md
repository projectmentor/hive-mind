# HiveMind CLI Reference

`hv` is your command-line interface to HiveMind — a place where you and all
kinds of AI agents can collaborate, share what they know, and build on each
other's work. In practice you'll rarely need to type these commands yourself.
Your agents (Hermes, Claude Code, and others) use `hv` automatically to store
and retrieve knowledge as they work. This reference is here for when you want
to look something up directly, correct a fact, or just see what's in your
memory.

---

## Quick reference

| Command | What it does |
|---|---|
| `hv config` | Device identity (`identity`) + owner-signed confidence (`confidence`) / quorum (`quorum`) params |
| `hv decide` | Record a decision |
| `hv discover` | Find hives on your tailnet |
| `hv doctor` | Check that your device is healthy; `--fix` self-heals (orphan daemons + Claude Code hooks/skill); subcommands `merkle` + `migrate-identity` + `rebuild` + `wire-agent` |
| `hv entity` | Track named things (people, projects, concepts) |
| `hv group` | Membership lifecycle (owner-only): admit/revoke/deny/change/purge/list |
| `hv wire` | Self-wire a tool/agent from a cell or comb; `--list`/`--show`/`--add` manage cell definitions |
| `hv capsule` | Seal a secret to the authorized device set: `put`/`get`/`ls`/`rm`/`rotate` |
| `hv join` | Request admission to a hive you've synced |
| `hv owner` | Show or create the governance owner identity |
| `hv remember` | Store a fact |
| `hv retract` | Correct a fact you got wrong |
| `hv search` | Search stored facts |
| `hv stats` | See a summary of your memory |
| `hv sync` | Sync with peer nodes |
| `hv whoami` | Show this device's identity and membership status (sterile/fertile/owner) |

---

## Running `hv`

```bash
cd ~/projects/hive-mind
./hv <command> [options]
```

---

## Commands

---

### `hv config` — Device identity + confidence/quorum parameters

`hv config identity` manages **this device's** Ed25519 key (the same as the legacy
`hv key`, kept as an alias):

```
hv config identity show                # this device's device_id + pubkey
hv config identity init [--force]      # mint a device key (fresh install)
```

`hv config confidence set` tunes the two owner-signed, journaled knobs (identical on every
device, which is what keeps confidence converging):

```
hv config confidence set same_device_lambda 0.5   # weight of EACH extra agent on one device (default 0.5)
hv config confidence set cap_self 0.70            # ceiling when all corroboration is one principal (default 0.70)
```

`hv config quorum set` tunes the owner-signed quorum-election knobs (the dead-owner recovery
path; see `hv owner`). All default to elections **off**:

```
hv config quorum set quorum_m 2          # admitted devices that must agree to elect (0 = OFF, default)
hv config quorum set quorum_by device    # count quorum by `device` or by `principal` (default device)
hv config quorum set dead_man_days 30    # owner silence required before an election can install (default 30)
```

> `hv config set <key> <value>` is kept as a silent alias for `hv config confidence set …`.

`same_device_lambda` is why two agents on one machine count for less than two on
separate machines: the device contributes its strongest agent in full plus this
fraction of the rest. `0` means a device is one voice no matter how many agents run
on it; `1` removes the discount.

---

### `hv decide` — Record a decision

Decisions are for choices you've made — not just facts, but the *why* behind
them. You can link a new decision to an older one it replaces, so you always
have a clear trail of what changed and why.

```
hv decide <content> [--rationale TEXT] [--supersedes ID]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The decision, stated clearly. Required. |
| `--rationale` | Why this decision was made. Optional but strongly recommended — future you will thank you. |
| `--supersedes` | The ID of a previous decision this replaces. The old decision stays on record; this one is linked to it. |

**Examples:**
```bash
# Minimal — just the decision
./hv decide "Use Tailscale SSH for inter-node access"

# With rationale
./hv decide "Use Tailscale SSH for inter-node access" \
    --rationale "Zero-config, auth handled by the tailnet, nothing to maintain"

# Replacing a previous decision
./hv decide "Install Tailscale inside WSL — each device gets its own IP" \
    --rationale "Cleaner than portproxy, WSL appears as its own tailnet machine" \
    --supersedes 5

# All options together
./hv decide "peers.json must use WSL Tailscale IPs, not Windows host IPs" \
    --rationale "WSL gets its own 100.x — Windows host IP is irrelevant to WSL daemon" \
    --supersedes 3
```

---

### `hv discover` — Find hives on your tailnet

Lists every device on your Tailscale network that's running a hive, with its
queen bee (owner) and node count — so a new machine can find a hive to join
without knowing any IP.

```
hv discover
```

It reads `tailscale status` and probes each device. A random service squatting
the sync port is not mistaken for a hive (the probe verifies a signed genesis).

---

### `hv doctor` — Check that your device is healthy

Runs a single set of checks over your local device and tells you whether anything
needs attention. It looks at:

- **authenticity** — your copy of HiveMind matches the signed official release
- **crypto** — the bundled cryptography passes its known-answer self-tests
- **keyperm** — your private key files are `0600` (not group/other-readable)
- **journal** — your device's history is intact and unbroken from the start
- **database** — the local lookup index is in step with that history
- **owner** — whether this device can sign governance (holds the owner key), plus any
  open succession nominations or owner-key sprawl
- **device-keys** — whether every admitted device has a usable capsule key, and whether any
  device carried a public key that disagrees with its identity-derived one (a tamper signal)
- **hygiene** — whether duplicate or obsolete facts have built up
- **agent-hooks** — whether the Claude Code dispatch shim and the `hive-memory` skill are
  wired (skipped silently on a node with no Claude Code)
- **sync-daemon** — whether the background sync service is running
- **peers** — whether your peer nodes are reachable and in sync

A few checks appear only when there is something to report: **crypto-modules** (a bundled
cryptography module failed to load, so signature checking is degraded), **journal-integrity**
(garbled or truncated journal lines were skipped on read), and **capsule-conflicts** (two
devices sealed the same capsule version before syncing, so one value lost a deterministic tie).

```
hv doctor
hv doctor --format json      # machine-readable, for scripts and monitoring
hv doctor --fix              # take the safe remedial actions (see below)
hv doctor --fix --dry-run    # preview every remedial action without making any change
```

The `sync-daemon` check also flags **orphan daemons** — a stale `hv sync daemon`
left running outside systemd (for example, the unit died but an old process still
holds the port and serves stale code, so the port answers while nothing is
actually managed). `hv doctor --fix` kills those orphans and restarts the managed
unit. It also **re-asserts the agent integration**: Claude Code is wired with a single
stable *dispatch shim* (`hive_dispatch.sh <event>`, one per lifecycle event) rather
than a hook per behavior — what runs for each event is decided inside that script, in
source control, so new behaviors arrive by update, not by editing `~/.claude`. If the
shim or the `hive-memory` skill is missing, or an older node still carries the previous
inline hooks, `--fix` wires the shim, migrates the old hooks away (so nothing fires
twice), and relinks the skill — leaving your own hooks untouched and taking a
`.bak.doctor` backup first. Because the 15-minute `hive-doctor.timer` runs `hv doctor
--fix`, a node that drifts heals itself with no one re-running the installer. Without
`--fix`, doctor only reports — it never kills or writes anything, so it stays safe to
run from cron. `--fix --dry-run` sits in between: it prints every action `--fix` would
take — which key files it would re-tighten, which orphan daemons it would kill, and
whether it would rewrite the Claude Code config — without performing any of them.

Each check is marked healthy (✓), advisory (•), or failed (✗). The command exits
non-zero only when a check actually fails, so you can wire it into a cron job or a
monitoring probe and get alerted on real breakage, not on a peer being briefly
offline. A failed `authenticity` check right after an upgrade usually just means
the signed manifest has not caught up yet; pull the latest and re-run.

---

### `hv doctor merkle` — Diagnose sync problems

Shows a fingerprint of your current data. If two nodes show the same
fingerprint, they're in sync. If they differ, `hv sync now` will sort it out.

You don't normally need to run this — `hv sync` handles it automatically. It's
here for when you're troubleshooting and want to see exactly where two nodes
diverge.

```
hv doctor merkle
```

> `hv merkle` is kept as a silent alias.

---

### `hv doctor migrate-identity` — Move an existing node to a device key

A one-time, coordinated step that re-stamps an existing journal from hostname
`node_id`s to cryptographic `device_id`s.

```
hv doctor migrate-identity --map map.json --dry-run   # preview
hv doctor migrate-identity --map map.json             # apply
```

`map.json` is `{"hostname": "k1:device_id", ...}` covering every device, identical
on each. Because the re-stamp is deterministic, running it on every peer with the
same map produces byte-identical journals, so your devices stay in sync with no
re-transfer. The runbook, per node: `hv config identity init --force` to mint the key,
share the resulting `device_id`, build the shared map, stop the sync daemons, run this
on each device, confirm `hv doctor merkle` roots match, then restart. Your old journal is
backed up to `journal.bak.device-id.<timestamp>/`.

> `hv migrate-device-identity` is kept as a silent alias.

---

### `hv doctor rebuild` — Fix the local database

If your local database looks wrong or out of date, `rebuild` resets it from
scratch. It's safe to run any time — your data won't be lost.

Also useful after pulling in entries from a peer node, or if HiveMind exited
unexpectedly.

```
hv doctor rebuild
```

(`hv rebuild` still works as a deprecated alias, kept for the installer/update
scripts; new use should prefer `hv doctor rebuild`.)

---

### `hv doctor wire-agent` — (Re)wire the Claude Code integration

Wires the Claude Code dispatch shim and the `hive-memory` skill into `~/.claude`,
migrating any older inline hooks to the shim as it goes. It's idempotent — your own
hooks are never touched — and it's the same logic `hv doctor --fix` and the
installer/update all use, so there's one definition that can't drift. You rarely run
this by hand; the installer wires it, update re-asserts it, and the self-heal timer
keeps it in place. Reach for it only to wire a node immediately rather than waiting
for the next self-heal tick.

```
hv doctor wire-agent
```

Honors `CLAUDE_CONFIG_DIR` (same as Claude Code). On a node with no Claude Code it's
a quiet no-op.

Only foreign config files (like Claude Code's `settings.json`) need this shim. Agents
integrated as our own plugin code — Hermes, OpenClaw, the MCP host — carry their wiring
in source already, so they have nothing to drift and nothing to re-assert.

---

### `hv wire` — Self-wire a tool or agent from a cell

A **cell** is an executable unit recorded in the journal (or shipped built-in): a `kind:tool` cell is
a platform-aware self-wiring recipe (obtain steps + a `verify` check), a `kind:agent` cell wires a
foreign config like the Claude Code hooks. A **comb** is an ordered collection of cells. `hv wire`
resolves a cell (built-ins first, then the journal projection) and dispatches by kind — agents wire
their config, tools run their steps and then the verify check (idempotent: a re-run is a no-op when
verify already passes).

```
hv wire <name>                 # wire a single cell by name
hv wire --comb <name>          # wire every cell in a comb
hv wire --list [--kind tool|agent]   # list cells (and combs); optionally filter by kind
hv wire --show <name>          # print a cell/comb definition
hv wire --add <file>           # publish a cell/comb from a JSON file (admission-gated)
hv wire <name> --env-file <path>     # tool credentials source (default ~/.claude/.env)
```

For `kind:tool` cells, required credentials are read from an opened **capsule** when one exists,
falling back to the `--env-file` dotenv for smooth migration. `hv wire <claude-agent-cell>` replaces
the deprecated `hv doctor wire-agent` (still available as a hidden alias, byte-identical output).

---

### `hv capsule` — Seal a secret to the authorized device set

A **capsule** encrypts a secret so that **only the hive's currently-authorized devices**
(`admitted − purged`) can open it — a random content key seals the payload with ChaCha20-Poly1305
(RFC 8439), and that key is wrapped to each device via an ephemeral X25519 ECDH. Secrets are ingested
**securely only**: from a dotenv file, a raw
file, stdin, or an interactive prompt — **never** through chat or the command line (`argv`), so the
value never lands in a transcript or process list. Who may publish is gated by the owner-signed
`capsule_putters` config (`owner` default, or `fertile`).

```
hv capsule put <name> --env-file <path> [--name VAR]   # seal from a dotenv var (default ~/.claude/.env)
hv capsule put <name> --file <path>                    # seal the raw contents of a file
hv capsule put <name> --stdin                          # seal a value piped on stdin
hv capsule put <name>                                  # no source flag → secure interactive getpass
hv capsule get <name> [--raw]                          # open on this device (--raw = no trailing newline)
hv capsule ls                                          # list capsules + whether this device can open each
hv capsule rm <name>                                   # tombstone (logical delete; ciphertext stays in the journal)
hv capsule rotate <name>                               # re-seal to the current device set after admit/revoke/purge
```

Each recipient's encryption key is **derived from that device's own signed identity**, so a
capsule can only ever be sealed to a key a device has cryptographically proven it holds — a public
key merely *carried* on a join-request or admit is never trusted as a recipient key. `put` and
`rotate` tell you who they could and couldn't seal to: **missing** (an admitted device with no
synced key yet), **skipped/unusable** (a recipient key that failed validation — that one device is
left out rather than failing the whole seal), and a **security** warning for any carried key that
disagrees with the identity-derived one. On a fresh device with no key, `hv capsule put` mints the
device key for you before sealing.

**Rotation caveat:** a revoked device still holds the *old* ciphertext, so `rotate` cuts it off the
new version only — to truly revoke access, rotate the upstream token/secret too.

---

### `hv entity` — Track named things

Entities are named anchors — a person, a project, a concept — that you can
attach facts to. Instead of hunting through search results, you can ask
"what do we know about X?" and get everything linked to it in one place.

```
hv entity {add,list,show,link} [options]
```

**Sub-commands:**

| Sub-command | What it does |
|---|---|
| `add` | Create a new entity. Needs `--name` and `--type` (e.g. `person`, `project`, `concept`). Optionally add metadata with `--attr` as a JSON object. |
| `list` | List all entities. |
| `show` | Show an entity and all facts linked to it. Needs `--name`. |
| `link` | Attach a fact to an entity. Needs `--name` and `--fact-id`. Optionally set `--confidence` to indicate how strongly the fact relates. |

**Examples:**
```bash
# add — minimal
./hv entity add --name "HiveMind" --type project

# add — with attributes
./hv entity add --name "HiveMind" --type project \
    --attr '{"repo":"projectmentor/hive-mind","status":"active"}'

# list — show all entities
./hv entity list

# show — everything linked to a named entity
./hv entity show --name "HiveMind"

# link — attach a fact to an entity (minimal)
./hv entity link --name "HiveMind" --fact-id 42

# link — with confidence score
./hv entity link --name "HiveMind" --fact-id 42 --confidence 0.9
```

---

### `hv group` — Membership lifecycle (owner-only)

By default any device can corroborate. Once you have an owner, only **admitted**
devices count toward confidence — so someone can't mint a pile of keys and fake a
crowd (a Sybil attack). `hv group` is the owner's roster + lifecycle:

```
hv group                                       # roster (admitted/pending/denied/purged)
hv group list                                  # same as above
hv group admit                                 # list devices awaiting admission
hv group admit k1:597b3e0f5fb92d37 --principal david
hv group revoke k1:…                           # un-admit (reversible) → device goes STERILE
hv group deny k1:…                             # reject a pending join-request (admit overrides)
hv group change k1:… --principal newname       # re-tag a device's principal (admission unchanged)
hv group purge k1:…                            # tombstone: permanent; its entries stop counting
```

With no device_id, `hv group admit` lists the pending join-requests. (Those also surface
in your session-start digest, so your agent can prompt you.) `--principal` tags who
owns the device; when every device behind a fact belongs to the same principal, its
confidence is capped. Admitting a device also **seeds a reciprocal peer** from the URL its
join-request advertised, so the owner syncs *to* the member too — connectivity is seeded by
admission but stays editable in `.peers.json`. Admission grants only write/fertility, never
governance. Get a device's id with `hv config identity show` on it. A device that isn't
admitted is a **read-only ("sterile") member**: it reads the whole hive, but its content writes
are **not accepted** until you admit it. Run `hv whoami` on any device to see sterile/fertile/owner.

**revoke vs purge.** `revoke` is reversible — the device returns to STERILE and a later
`admit` restores it. `purge` is a **final tombstone**: the device's entries stay in the
append-only journal but are permanently excluded from corroboration and ingest, and it
**cannot be re-admitted**. `change` re-tags the principal without touching admission; `deny`
drops a pending join-request (a later `admit` overrides it).

`hv group list` also calls out two key-coverage conditions when they apply: **missing keys**
(an admitted device that hasn't synced its signed identity yet, so it can't receive capsules
until it does) and a **security** line for any device that carried a public key disagreeing
with its identity-derived one — that carried key is ignored, never trusted, and the mismatch is
surfaced as a tamper signal. The same two conditions appear in `hv doctor` as the `device-keys`
check.

> `hv admit …` is kept as a silent alias for `hv group admit …`.

---

### `hv join` — Request admission to a hive

After your device has synced a hive (its peer is in `.peers.json`), `hv join` asks
that hive's owner to admit you.

```
hv join --principal carol
```

This is **non-blocking**: you're already reading the hive, and you can write, but
your writes don't count toward confidence until you're admitted. The request shows
up for the owner to approve; you don't wait. It prints your `device_id` so you can
pass it along.

On a brand-new device that has no key yet, `hv join` **mints the device key for you**
first (you need one to be admitted and to open capsules), so there's no separate
`hv key init` step. Running `hv join` again while a request is already pending is a
no-op — it won't pile up duplicate requests.

---

### `hv key` — This device's device identity _(alias of `hv config identity`)_

Each device is identified by an Ed25519 **device key**, not its hostname. The key
proves which device wrote an entry, so a peer cannot impersonate your device to
inflate confidence. Your `node_id` is the key's fingerprint, like
`k1:2a2110f3d8963a9e`. The canonical form lives under `hv config identity`; `hv key`
is kept as a silent alias.

```
hv config identity show     # show this device's device_id and public key
hv config identity init     # mint a device key (fresh install only)
hv key show                 # alias of the above
```

A fresh install mints a key automatically. `hv key init` refuses to run on a node
that already has history under its hostname, because minting a key there would
split its identity; use `hv migrate-device-identity` for an existing node instead.
The private seed lives at `HIVE_HOME/.device-key` — keep it secret, never commit
or sync it. Share your `device_id` and public key with peers (they go in
`.peers.json`).

---

### `hv owner` — Governance owner identity

Confidence weighs *who* corroborates a fact. A few of those rules need a shared,
trusted source: which devices count, who owns each one, and a couple of tunable
numbers. That trust is rooted in an **owner key** — a separate Ed25519 key (apart
from your device keys) that signs governance decisions into the journal, so every
node agrees on them.

```
hv owner claim [--mint] [--force]             # (successor side) claim ownership against a nomination
hv owner elections                             # list open owner-election proposals and their tallies
hv owner escrow                                # store the key (passphrase-encrypted) IN the hive
hv owner export [--out FILE] [--passphrase]   # back up the owner key to an off-device file
hv owner heartbeat                             # refresh owner liveness (resets the dead-man timer)
hv owner import FILE [--force]                 # restore it from a file on another device
hv owner init                                 # mint the owner key and claim ownership (once)
hv owner nominate <successor_pub>             # nominate a NEW owner key as successor
hv owner propose-election [--mint | --pub B64] # (admitted device) propose electing a new owner
hv owner restore                               # recover the key from the hive's escrow
hv owner revoke-escrow <node_id:seq|all>     # tombstone an escrowed key so `restore` skips it
hv owner show                                 # the established owner + admitted devices + config
hv owner standby <device_id> [--off]          # declare an advisory standby key holder
hv owner transfer <new_owner_pub>            # immediate handoff to a key the target already holds
hv owner unnominate <successor_pub>           # withdraw a pending nomination
hv owner vote <proposal_id>                    # (admitted device) vote for an open election proposal
```

You run `hv owner init` once, on whichever machine you want to hold the owner key.
It mints a **`hive_id`** (a public identifier that keeps your hive separate from
any other hive on the same tailnet) and writes an owner declaration into the
journal that the other nodes pick up on sync. Until you do this, the governance
rules below are simply off (every device counts, nothing is capped) — so it's opt-in.

**The owner key is a single point of failure — back it up.** `hv owner init`
auto-stashes a copy to `~/.config/hive-mind/identity/.owner-key` (survives uninstall),
and `hv owner export` writes a portable copy you can store off-device (use
`--passphrase` to encrypt it; an exported key is total hive authority, so treat it
like an SSH private key). If the owner device dies, `hv owner import` installs the
key on a new device and governance resumes under the **same** owner identity — no
journal change. `import` refuses a key that doesn't match the journal's established
owner unless you pass `--force`.

`hv owner escrow` stores the owner key **inside the hive itself**, passphrase-encrypted,
so it syncs to every node and any synced device can recover it with `hv owner restore`
(no file to move). Two cautions, because the journal is shared and append-only: the
encrypted blob is readable by **every** device that syncs the hive (including read-only
members) and **can't be un-published**, so the passphrase is effectively your hive master
key — make it strong (a 12-char minimum is enforced). Escrow and the off-device file cover
different threats: escrow handles "I lost a device," the file handles "I don't fully trust
the shared store." Use the one that fits, or both.

`hv owner standby <device_id>` records an advisory note that a device is sanctioned
to also hold the owner key and act while the primary owner is offline (it must
actually hold a copy via export/import — the key is the authority; the declaration
is for visibility in `hv owner show`/`hv whoami`).

**Changing who the owner is — succession.** Backup/restore recovers the *same* owner
identity. To hand off to a *new* key (a fresh device, or to rotate away from a leaked
one), use succession. On the successor device, `hv owner claim --mint` mints a fresh
owner key and prints its public key. Give that pubkey to the current owner, who runs
`hv owner nominate <pub>`; once that syncs, the successor re-runs `hv owner claim` to
take ownership. From then on the old owner key can no longer sign governance — its
post-handoff acts are simply ignored by every node. `hv owner transfer <pub>` is the
immediate variant (no claim round-trip), but the target device must *already hold* that
key or governance becomes unsignable, so prefer nominate+claim. A live owner can never
be unseated: only the current owner's own nominate/transfer, or a nominee's claim against
an open nomination, advances ownership.

`hv owner revoke-escrow <node_id:seq|all>` logically tombstones an escrowed key so
`hv owner restore` skips it (`node_id:seq` from `hv owner show` / the doctor owner check,
or `all`). The ciphertext stays in the append-only journal forever — a tombstone is not
deletion — so if an escrow *passphrase* leaked, the real fix is to **rotate the owner key**
via succession/transfer, which makes the old escrow blob unlock a key that is no longer
the owner.

**When the owner is gone with no backup — quorum election.** Backup/restore and succession
both need someone to act *before* the owner is lost. If the owner device dies with no escrow,
no exported file, and no nominated successor, the admitted devices can still elect a new owner —
but only by agreement, and only once the old owner has genuinely gone dark. The owner first
turns this on (it is off by default): `hv config quorum set quorum_m <N>` sets how many admitted
devices must agree, and `hv config quorum set dead_man_days <D>` sets how long the owner must be
silent first (default 30). Then, if the owner goes dark, any admitted device runs
`hv owner propose-election --mint` (mint a fresh owner key here) or `--pub <base64>` (propose a
key held elsewhere); the others run `hv owner vote <proposal_id>`. Once `quorum_m` devices have
endorsed one proposal **and** no owner-signed act has appeared for `dead_man_days`, that proposal
installs its key as the new owner on every node. `hv owner elections` lists open proposals and
their tallies. The dead-man switch is what keeps this safe: **a live owner can never be unseated**,
because any owner-signed act — including an explicit `hv owner heartbeat` — refreshes the owner's
last-activity and re-shuts the window. Votes and proposals are device-signed by admitted members,
so an outsider cannot stuff the ballot. `quorum_m=0` (the default) disables elections entirely.

---

### `hv remember` — Store a fact

Save anything worth keeping: observations, constraints, gotchas, status updates.
Facts are searchable, tagged, and automatically gain credibility when multiple
independent sources agree on the same thing.

```
hv remember <content> [--tags TAGS] [--source SOURCE] [--importance N] [--gate] [--resolves ID]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The text of the fact. Put it in quotes. Required. |
| `--tags` | Comma-separated labels to help you find it later, e.g. `--tags infrastructure,todo`. No spaces. |
| `--source` | Who or what is asserting this fact. Helps HiveMind tell independent sources apart. Defaults to `manual`. See [Source identity](#source-identity) below. |
| `--importance` | A numeric hint for how significant this fact is. Recorded for future use but not currently applied to search ranking. |
| `--gate` | Filter this write through the **salience gate** — a quality check that silently drops low-value entries (too short, no meaningful content, likely noise). Useful when an agent is writing many facts at once and you want to keep your memory clean. |
| `--resolves ID` | Mark this write as the correction of an earlier fact. It records a **durable link** (the resolved fact's `(node_id, seq)` journal identity, stable across rebuilds and nodes) and **soft-retracts** fact `ID` (registers negative evidence so it stops surfacing as canonical), keeping the corpus from asserting the old and corrected claim at once. Reversible; a decisive forget is still `hv retract ID --owner`. The audit's **CONTRAVENED** check separately flags a correction that names a fact in *prose* (`resolves #N`, `supersedes #N`) but never reconciled it — but a prose `#N` is a **local id** that drifts across rebuilds and nodes, so treat the flagged target id as best-effort and reconcile with `--resolves` (which never drifts). |

**What you get back:**

- `Remembered as fact #N` — stored successfully
- `Already known (fact #N)` — identical content already exists; nothing written

**How confidence works:**

A new fact starts with low confidence — it's one source making one claim. When
a second completely independent source stores the exact same content, confidence
rises. Each additional independent voice adds more weight, but with diminishing
returns. You can't inflate confidence by repeating the same fact from the same
source — self-repetition does nothing.

**Examples:**
```bash
# Minimal — just the fact
./hv remember "Tailscale SSH replaces Win OpenSSH on both nodes"

# With tags
./hv remember "Tailscale SSH replaces Win OpenSSH on both nodes" \
    --tags infrastructure,architecture

# With tags and source
./hv remember "Payments must go through the billing service — no direct DB writes" \
    --tags architecture,constraint \
    --source "claude-code"

# With importance hint
./hv remember "Sync daemon must be restarted after peers.json changes" \
    --tags ops \
    --importance 0.9

# With salience gate — low-quality entries are silently dropped
./hv remember "daemon binds on 9876" \
    --tags infrastructure \
    --source "hermes:primary/claude-sonnet/abc12345" \
    --gate

# All options together
./hv remember "WSL Tailscale IP changes after re-install — update peers.json" \
    --tags infrastructure,gotcha \
    --source "hermes:primary/claude-sonnet/abc12345" \
    --importance 0.8 \
    --gate
```

---

### `hv retract` — Correct a fact you got wrong

When a stored fact turns out to be wrong, use `retract` to say so. The fact
isn't deleted — HiveMind keeps a record of everything — but it's marked as
retracted and won't show up in normal search results. Think of it as
"this was wrong" rather than "this never happened."

```
hv retract <fact_id> [--reason TEXT] [--source SOURCE] [--owner]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `fact_id` | The ID of the fact to retract. Get it from `hv search` output. Required. |
| `--reason` | Why you're retracting it. Saved for reference. |
| `--source` | Who is doing the retracting. Defaults to `manual`. |
| `--owner` | Mark this as an authoritative retraction. Use when the fact is definitively wrong, not just uncertain. Immediately drives confidence to the floor. Once you have an owner (see `hv owner`), this requires the **owner key** and is cryptographically signed, so a forget can't be forged; run it on the owner machine. |

**Examples:**
```bash
# Minimal — fact ID only
./hv retract 4

# With a reason
./hv retract 4 --reason "Was a test probe, not a real observation"

# With reason and source
./hv retract 7 \
    --reason "Portproxy is no longer used — architecture changed" \
    --source "hermes:primary/claude-sonnet/abc12345"

# Owner retraction — authoritative, immediately floors confidence
./hv retract 12 --reason "Definitively wrong" --owner

# All options together
./hv retract 15 \
    --reason "Superseded by Tailscale-in-WSL architecture" \
    --source "claude-code" \
    --owner
```

---

### `hv search` — Search stored facts

Find facts by keyword. Results are ranked by confidence — the most corroborated
facts come first.

```
hv search <query> [--format {text,json}] [--min-confidence N]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `query` | What to search for. Multiple words all have to match. Use `OR` between words for either/or. Use `"quoted phrases"` for exact matches. |
| `--format` | `text` (default) for readable output. `json` for machine-readable output you can pipe to other tools. |
| `--min-confidence` | Only show facts at or above this confidence level (0.0–1.0). Good for filtering out unverified claims. |

**Examples:**
```bash
# Minimal — keyword search
./hv search "tailscale"

# Multiple keywords (all must match)
./hv search "sync daemon"

# Either/or
./hv search "tailscale OR portproxy"

# Exact phrase
./hv search '"address already in use"'

# Filter by confidence
./hv search "billing" --min-confidence 0.6

# Machine-readable output
./hv search "sync" --format json

# All options together
./hv search "infrastructure" --format json --min-confidence 0.5
```

---

### `hv stats` — Memory summary

Shows a snapshot of everything in your local memory: how many facts, decisions,
and entities you have, where the entries came from, and which tags are most used.

```
hv stats
```

Run this at the start of a session to get oriented, or after a sync to confirm
entries came across from a peer.

---

### `hv sync` — Sync with peer nodes

Keeps your device up to date with peers — pulling in any facts they have that you
don't, and pushing yours to them. Only the differences are transferred, not
everything.

Peers are configured in `.peers.json` in your hive-mind directory. The installer
sets this up for you.

```
hv sync now
hv sync daemon
```

**Sub-commands:**

| Sub-command | What it does |
|---|---|
| `now` | Sync with all peers right now and exit. Good for a manual check. |
| `daemon` | Run continuously — sync automatically every 5 minutes. This is what the background service runs. |

**`.peers.json` format:**
```json
{
  "bind": "0.0.0.0",
  "port": 9876,
  "peers": [
    {
      "url": "http://100.64.0.2:9876",
      "node_id": "node-b"
    }
  ]
}
```

- `peers[].url` — your peer's address. Use the WSL Tailscale IP (run `tailscale ip` on the peer to get it).
- `peers[].node_id` — a label for logs. Optional but helpful.
- `bind` and `port` — what address and port to listen on. Defaults are fine for most setups.

This file is not synced to git — it's specific to each machine.

**Examples:**
```bash
# Sync now — one-shot manual sync
./hv sync now

# Run the daemon manually (the background service does this automatically)
./hv sync daemon

# Check background service status
sv status hive-sync                                        # Android (Termux)
systemctl --user status hive-sync                          # Linux / WSL
launchctl print gui/$(id -u)/com.projectmentor.hive-sync   # macOS

# Watch sync logs live
tail -f ~/.hive-mind/logs/hive-sync.log                    # Android (Termux)
journalctl --user -u hive-sync -f                          # Linux / WSL
tail -f ~/Library/Logs/hive-mind/hive-sync.log             # macOS

# Restart the background service (e.g. after editing peers.json)
sv restart hive-sync                                                 # Android (Termux)
systemctl --user restart hive-sync                                   # Linux / WSL
launchctl kickstart -k gui/$(id -u)/com.projectmentor.hive-sync      # macOS
```

---

### `hv whoami` — Your device's identity and membership status

Answers "who am I, and what can I do here?" — read-only, no side effects:

```
hv whoami
```

It prints this device's `device_id`, the `hive_id` and `owner`, your `principal`, and your **status**:

- **OWNER** — you hold the owner key (admit devices, set config, forget facts).
- **FERTILE** — admitted; your writes land in the shared journal and count toward confidence.
- **STERILE** — read-only; you read the whole hive, but your content won't land until the owner admits you (`hv join` to request it).
- **UNAFFILIATED** — no hive yet (`hv owner init` to start one, or sync one and `hv join`).

If you're sterile, your session-start digest says so too, so your agent isn't left guessing.

---

## The `hive-mind` command — device management

`hv` works with the memory corpus; the separate `hive-mind` command manages the HiveMind
install on this device (set up, update, health, removal). It is a thin dispatcher
(`scripts/installer/dispatcher.sh`) symlinked onto your PATH, so a plain `git pull` keeps its
subcommands current.

```bash
hive-mind <subcommand> [options]
```

| Subcommand | What it does |
|---|---|
| `install` | Set up this device from scratch (discovery-driven bootstrap or join). |
| `update` | Pull the latest code and restart the sync daemon. **Auto-heals** after a force-push / history rewrite: when a fast-forward isn't possible and the tree is clean, it hard-resets to the upstream instead of aborting. |
| `reset` | Recover a **wedged** install in one command: force-align the code to `origin` (even after a rewrite, even with local edits), rebuild the DB from the journal, refresh the supervisor units + Claude Code hooks, restart the daemon, and verify authenticity. **Your Hive (journal, keys, device identity) is preserved** — this is not `uninstall`. Use it when `hv doctor`/`hv verify` is unhappy after a breaking change. `-y` skips the prompt. |
| `status` | Show device health and peer sync state. |
| `invite` | Print the one-line address to paste on a new device so it can join this hive. |
| `uninstall` | Remove HiveMind from this device (see flags below). |

### `hive-mind invite` — add another device

```bash
hive-mind invite
```

Run it on any device that's **already in the hive**; its only output is this node's Tailscale
address (e.g. `100.84.84.100`). On the **new** device, run `hive-mind install` and paste that
line when it asks for a hive address — no need to know what an IP is or where to find it.

`hive-mind install` auto-discovers hives on platforms with the `tailscale` CLI (Linux, macOS,
WSL). On **Android (Termux)** there is no `tailscale` CLI — Tailscale is the phone's VPN app —
so discovery can't enumerate the tailnet; the installer asks you to paste an address instead.
You only ever need **one** node of the hive to join; the rest syncs from there. (A fresh
`hive-mind install` on the owner device prints the invite line automatically once the hive is
created.)

### `hive-mind uninstall`

```bash
hive-mind uninstall [--keep-hive] [--keep-identity] [--yes]
```

| Flag | Effect |
|---|---|
| `--keep-hive` | Preserve your **full Hive** (journal, keys, `peers.json`) to a timestamped backup folder `~/hive-mind-keep-<UTC-timestamp>`, **and** keep this device's identity (see `--keep-identity`) so a reinstall resumes the same `device_id`. Without this, all Hive data is removed. |
| `--keep-identity` | Delete the Hive data but preserve only this device's **identity** (its keys) to `~/.config/hive-mind/identity/` — a stable location that survives uninstall. A reinstall then offers to resume the same `device_id`, so the owner's prior **admission still applies (no re-admit)**. Lighter than `--keep-hive`. |
| `-y`, `--yes` | Skip the `Continue? [y/N]` confirmation prompt and proceed — for unattended or scripted removal. |
| `-h`, `--help` | Print usage and exit. |

On the next `hive-mind install`, if a preserved identity is found it offers to **resume** it (so a sterile/fertile device keeps its standing); decline to mint a fresh one. A plain `uninstall` (no flag) is a clean slate — identity is gone and a reinstall mints a new `device_id`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `HIVE_HOME` | The folder containing `hv` | Where your data lives. Override to point `hv` at a different location. |
| `HIVE_NODE_ID` | The device-key fingerprint, else the hostname | Overrides this device's identity. Normally a node identifies by its Ed25519 device key (see `hv key`); set this only to force an identity, e.g. to run two separate hive instances on one machine. |
| `HIVE_NODE_LABEL` | Your machine's hostname | A human-friendly display label shown next to the `device_id` in `hv stats` and sync logs. Cosmetic; does not affect identity. |
| `HIVE_NOW` | System clock | For testing only — pins the clock to a fixed time so results are predictable. |

---

## Source identity

When an AI agent calls `hv remember`, it passes a `--source` string that
identifies who made the claim. HiveMind uses this to detect when multiple
independent agents agree on the same fact — which raises that fact's confidence.

The format is:

```
<app>:<context_class>/<instance>/<session8>
```

| Field | What it is | Examples |
|---|---|---|
| `app` | The tool making the write | `hermes`, `claude-code`, `manual` |
| `context_class` | The type of agent session | `primary` (main agent), `subagent` (delegated task), `cron` (scheduled job) |
| `instance` | The specific agent profile | `claude-sonnet`, `default` |
| `session8` | First 8 chars of the session ID | Tells sessions apart in logs — not counted as a separate source |

The same agent writing the same fact across multiple sessions still counts as
one source. Two different agents independently writing the same fact counts as
two.

**Examples:**
```
hermes:primary/claude-sonnet/abc12345    # Hermes, main session
hermes:subagent/claude-haiku/xyz99999    # Hermes running a subagent
claude-code                              # Claude Code
manual                                   # You, typing directly
```
