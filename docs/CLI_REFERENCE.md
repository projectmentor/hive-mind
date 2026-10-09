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
| `hv audit` | Surface redundant, obsolete, stale or missing facts (used by agent hooks) |
| `hv config` | Device identity (`identity`); governed parameters are set with `hive-mind config set` (owner) |
| `hv dash` | Open the read-only web dashboard (served by the sync daemon) |
| `hv decide` | Record a decision |
| `hv discover` | Find hives on your tailnet |
| `hv doctor` | Check that your device is healthy; `--fix` self-heals (orphan daemons + Claude Code hooks/skill); subcommands `merkle`, `rebuild` |
| `hv entity` | Track named things (people, projects, concepts); `join`/`unjoin` a module's entity to a shared one *(2.1)* |
| `hv feed` | Read-only journal feed for modules and operators: entries past a per-node cursor, as JSON *(2.1)* |
| `hv group` | The membership roster (`list`); the lifecycle (admit/revoke/deny/change/purge) is `hive-mind group …` (owner) |
| `hv wire` | Self-wire a tool/agent from a cell or comb; `--list`/`--show`/`--add` manage cell definitions |
| `hv capsule` | Seal a secret to the authorized device set: `put`/`get`/`ls`/`rm`/`rotate` |
| `hv join` | Request admission to a hive you've synced |
| `hv nudge` | Emit a save/audit hint or the session-start digest (used by agent hooks) |
| `hv owner` | Show the owner (`show`, `elections`) and run the device-signed election (`propose-election --pub`, `vote`); the owner's own acts are `hive-mind owner …` |
| `hv peers` | Hive members, reachability, staleness, trust drift |
| `hv propose` | Record an idea (a hypothesis) |
| `hv remember` | Store a fact |
| `hv retract` | Correct a fact you got wrong |
| `hv search` | Search facts, decisions and ideas; `--id` looks one up by `sid` or `ref` |
| `hv stats` | See a summary of your memory |
| `hive-mind unforget` | Owner only: reverse an owner forget (`hive-mind retract --owner`) |
| `hv sync` | Sync with peer nodes; `auth` sets the read-auth mode, `auth --outbound` what a peer must prove before this node pushes to it |
| `hv telemetry` | Local-only session observability (never synced) |
| `hv verify` | Check that this install is the official, signed release |
| `hv version` | Print the agent contract version |
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

### `hv config` and `hive-mind config set` — Device identity + governed parameters

`hv config identity` manages **this device's** Ed25519 key (`hv key` until 2.0):

```
hv config identity announce            # publish this device's key as a signed journal entry
hv config identity show                # this device's device_id + pubkey
hv config identity init [--force]      # mint a device key (fresh install)
```

`hive-mind config set` tunes the owner-signed, journaled knobs (identical on every
device, which is what keeps confidence converging). It runs on the owner machine:

```
hive-mind config set same_device_lambda 0.5              # weight of EACH extra agent on one device (default 0.5)
hive-mind config set cap_self 0.70                       # ceiling when all corroboration is one principal (default 0.70)
hive-mind config set introspect_support_weight 0.0       # weight of an `introspect`-channel fact assertion (1.21) or
                                                         # supports/contradicts/resolves link (1.19), default 0
hive-mind config set trust_long_days 180                 # 1.19: trust-velocity long window (days)
hive-mind config set trust_short_days 14                 # 1.19: trust-velocity short window (days)
hive-mind config set trust_drift_threshold -0.3          # 1.19: `doctor trust-drift` warns when short − long falls below this
# 1.19 PR6 — salience (importance / utility) and per-class half-lives, all governed the same way:
hive-mind config set importance_self_cap 0.3             # the most a writer's own `--importance` can claim (default 0.3)
hive-mind config set w_links 0.6                         # weight of other-identity link in-degree in importance (default 0.6)
hive-mind config set w_volatile 0.1                      # extra importance of a `volatile` fact while fresh (default 0.1)
hive-mind config set halflife_fact 180                   # confidence/importance/utility half-life of a fact, days (default 180)
hive-mind config set halflife_idea 90                    # … of an idea (default 90)
hive-mind config set halflife_volatile 14                # … of a `volatile`-tagged fact (default 14)
```

> **Importance is learned (1.19 PR6).** `--importance` is a *hint*: the projection starts a fact at
> `min(hint, importance_self_cap)` and raises it only through links from **other** identities
> (`_recompute_importance`); `utility` measures how much recorded decision-making relied on the entry,
> weighted by how those decisions turned out. Both decay at query time under the entry's class half-life.
> See `docs/INTERNALS.md` → *Importance and utility*.

> **Links (1.19).** The journal has a generic `link` entry type (`supports`, `contradicts`,
> `supersedes`, `resolves`, `entity`, `informed`, `outcome-of`, and `extends` since 1.22; unknown
> kinds are ignored).
> *(PR2b)* `--supersedes`, `--resolves` and `entity link` now each write **one** such link instead
> of their legacy field/entry (never both); on the owner machine the link is owner-signed and
> therefore *hard* everywhere, elsewhere it is hard only where this device authored the target.
> `hv doctor` reports downgraded `supersedes`/`resolves` links under `link-authz`, and peers that
> cannot yet honour links under `fleet-contract`. See `docs/INTERNALS.md` → *Links*.

The quorum-election knobs (the dead-owner recovery path; see the owner section) are set the same way. All default to elections **off**:

```
hive-mind config set quorum_m 2             # admitted devices that must agree to elect (0 = OFF, default)
hive-mind config set quorum_by device       # count quorum by `device` or by `principal` (default device)
hive-mind config set dead_man_days 30       # owner silence required before an election can install (default 30)
```

> *(2.0)* One command sets every governed key. The three older `set` forms under `hv config`
> (`confidence set`, `quorum set` and plain `set`) name `hive-mind config set` and exit 2, acting on nothing.

`capsule_putters` and `cell_writers` are the two write policies: who may publish capsules, and who
may publish cells and combs. Each is `owner` (the default) or `fertile` (any admitted device). Set
them like any other governed key:

```
hive-mind config set capsule_putters fertile   # any admitted device may seal and rotate capsules
hive-mind config set cell_writers owner        # only the owner may publish cells/combs (default)
```

*(1.25, #122)* `forget_writers` decides which owner forgets count. `legacy` (the default) also honours an
unsigned owner forget dated before the genesis owner, which an admitted device could have backdated. `owner`
honours only a forget signed by the owner as of its position:

```
hive-mind config set forget_writers owner   # close the pre-genesis grandfather (#122)
hive-mind doctor --fix                      # the same close, re-signing what depends on it first (2.0)
```

`config set` refuses, writing nothing, while closing would bring a fact back: it lists each fact kept forgotten
only by such a forget, with the two ways to decide it, `hive-mind retract <sid> --owner` (keep it forgotten,
now signed) or `hive-mind unforget <sid> --reason …` (let it back). Run it again once each is decided.

*(2.0)* The default stays `legacy`, so nothing a hive forgot comes back when it upgrades. But `hv doctor` fails
`forget-authz` on an owned hive whose policy is still open, and `hive-mind update` ends with an ACTION
REQUIRED note until it is closed. `hive-mind doctor --fix` is the one command. With nothing depending on the
grandfather it closes at once. Otherwise it lists each dependent fact by `h:` id with its text and asks, at a
terminal, whether to re-sign those forgets so the facts stay hidden (default no). `y` re-signs each one
owner-signed and then closes; `n`, or no terminal, writes nothing. Read the list first: an admitted device
could have written one of those forgets, and `hive-mind unforget` lets such a fact back instead.

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
hv decide <content> [--rationale TEXT] [--tags a,b,c] [--source SOURCE] [--supersedes DECISION] [--informed REF ...]
hv decide [content] --revoke DECISION --rationale TEXT [--tags a,b,c] [--source SOURCE] [--informed REF ...]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The decision, stated clearly. Required, except with `--revoke`. |
| `--rationale` | Why this decision was made. Optional but strongly recommended — future you will thank you. |
| `--tags` | Comma-separated tags, just like `hv remember`. Tag a decision with its project (e.g. `--tags hive-mind`) so it shows up in `hv search` scoped to that project — the reliable way to find a decision later. Decision numbers (`#N`) are node-local and shift on rebuild; to cite a decision, use its `sid` (`h:…`, shown by `hv search`). |
| `--source` | *(1.23)* Who is deciding, as on `hv remember` (e.g. `claude-code`). An agent should always pass it: omitted, it is `manual`, a person (2.0 reads no `$HERMES_AGENT`, #119). Through `hv` a write's links are device-signed; through `hive-mind`, a `manual` write's links are owner-signed (#114, decision `h:34cc1dbcd3`). |
| `--supersedes` | A previous decision this replaces: its **`sid`** (`h:…`, shown by `hv search`; preferred), its `ref` (`node_id:seq`). A bare local id (`17` / `d17`) is refused (2.0): it exits 1 with `'118' is a local id, which 2.0 no longer accepts`, having written nothing (see [Stable ids](#stable-ids-sid-vs-local-id)). Resolved and kind-checked **before** anything is written; a bad reference aborts. The old decision stays on record; this one is linked to it. *(1.19 PR2b)* Journaled as one `supersedes` **link**. It is owner-signed (hard everywhere) only when this device holds the owner key **and** the source is `manual` *(1.23, #114)*; otherwise it is hard only where this device authored the old decision (see `hv doctor` `link-authz`). The confirmation says `(owner-signed: source manual)` or `(device-signed: source …)`. |
| `--revoke` | *(1.24, #45)* Withdraw a decision that was **wrong**, with no replacement. Takes the decision's `sid` (preferred), `ref` or a bare local id, resolved and kind-checked like `--supersedes` (a fact aborts). Needs `--rationale`; cannot be combined with `--supersedes`. It writes one decision tagged `revocation`, whose content is `Revoke <sid>: <the target's first line>` unless you give `content`, and one `supersedes` link to the target. The target is superseded **only when that link is hard**: written by a person on the owner device (`manual`), or by the device that wrote the decision. Otherwise the revoke is recorded as evidence and is **not in effect**. The confirmation says which: `Revoked decision h:… (…)` or `Revoke of decision h:… recorded …; NOT in effect until the owner, or the device that wrote the decision, re-runs hv decide --revoke h:…`. Until then `hv search` does not flag the target as superseded, and `hv doctor` lists the link under `link-authz`. A revocation cannot itself be undone; a new decision restates what still holds. |
| `--informed` | *(1.19)* One or more references to the facts/decisions this decision **relied on**. The stable forms are the **`sid`** shown by `hv search` (`h:3f9a1c0b2d` — the form to type) and the `ref` (`node_id:seq`, e.g. `k1:597b3e0f5fb92d37:401`) — both identical on every node, never change. A bare local id — `118` (fact), `d17` (decision), `i5` (idea) — is a rowid that shifts on every rebuild, and 2.0 refuses it. Each ref is resolved **and kind-checked** at write time, and any failure aborts the whole command before anything is written. The refs are journaled on the decision (`informed_by`) and one `informed` link is written per ref; this is the input to the utility projection (what knowledge proved useful once outcomes are recorded). |

**Examples:**
```bash
# Minimal — just the decision
./hv decide "Use Tailscale SSH for inter-node access"

# With rationale
./hv decide "Use Tailscale SSH for inter-node access" \
    --rationale "Zero-config, auth handled by the tailnet, nothing to maintain"

# Project-scoped so `hv search` finds it later
./hv decide "Android uses the runit supervisor (no systemd on Termux)" \
    --rationale "Termux has no systemd; runit/termux-services is the native supervisor" \
    --tags hive-mind,android

# A decision that was wrong: withdraw it, no replacement (1.24)
./hv decide --revoke h:3f9a1c0b2d --rationale "Friday deploys broke prod twice; no deploy-day rule for now"

# Replacing a previous decision
./hv decide "Install Tailscale inside WSL — each device gets its own IP" \
    --rationale "Cleaner than portproxy, WSL appears as its own tailnet machine" \
    --supersedes h:5d0c1a9e42

# All options together
./hv decide "peers.json must use WSL Tailscale IPs, not Windows host IPs" \
    --rationale "WSL gets its own 100.x — Windows host IP is irrelevant to WSL daemon" \
    --supersedes h:0b7e21f3c8 \
    --informed h:3f9a1c0b2d h:7c01d2e9aa
```

---

### `hv propose` — Record an idea (a hypothesis) *(1.20)*

An **idea** is not a fact. It is a hypothesis — "perhaps X relates to Y" — that the hive can
support or contradict over time. It starts at confidence **0.00** and earns confidence **only**
from `supports`/`contradicts` links written by *other* identities on the `sense` channel (an
observation). Restating it, or a second agent proposing the same text, creates a second idea; it
never corroborates the first. An LLM proposes; it cannot promote.

To weigh in on an idea, write what you observed with `hv remember "<observation>" --supports <sid>`
(or `--contradicts`). Its author's own support doesn't count.

```
hv propose <content> [--tags a,b,c] [--source SOURCE] [--channel sense|act|introspect]
```

| Argument | What it does |
|---|---|
| `content` | The hypothesis, stated so it can be supported or contradicted. Required. |
| `--tags` | Comma-separated tags, like `hv remember`. |
| `--source` | Who is proposing. Defaults to `manual`. |
| `--channel` | The experience signal of the proposal itself; defaults to `introspect` (a hypothesis is internal state). Does not affect how the idea earns confidence. |

Ideas get **attention, not announcement**: the session-start digest lists up to three *open*
ideas (effective confidence below the local `OPEN_IDEA_THRESHOLD` knob in `nudge.env`, default
0.3, newest first) as a standing invitation to weigh in, and when a peer's idea arrives by sync
an `idea-arrived` line is appended to the local bus log `$HIVE_HOME/.bus/introspect.log`. Find
ideas with `hv search --kind idea`; cite one by its `sid` (`h:…`) or `ref`, e.g. `hv decide "…"
--informed h:3f9a1c0b2d` (the `i<N>` local-id form still works but is deprecated like every bare id).

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
- **owner-copies** — owner-key files found by name (`hive-owner-*.key`, and the stash's
  `.owner-key`) in `$HIVE_HOME`, the current directory, the home directory and the identity stash. It
  never opens or deletes them, so an export it lists may be sealed or plaintext
- **owner** — whether this device can sign governance (holds the owner key), plus any
  open succession nominations or owner-key sprawl
- **device-keys** — whether every admitted device has a usable capsule key, and whether any
  device carried a public key that disagrees with its identity-derived one (a tamper signal)
- **hygiene** — whether duplicate or obsolete facts have built up
- **agent-hooks** — whether the Claude Code dispatch shim and the `hive-memory` skill are
  wired (skipped silently on a node with no Claude Code)
- **sync-daemon** — whether the background sync service is running
- **sync-bind** — whether the running daemon is bound where it should be now: it warns when the
  daemon sits on `127.0.0.1` while this node has a tailnet address (it started before Tailscale), or
  on an old tailnet IP; `--fix` restarts it. A `HIVE_BIND` or `.peers.json` bind is left alone, and it
  never asks the daemon to move to loopback
- **daemon-code** — whether the running daemon still matches the code in its own checkout. A bare
  `git pull` moves the files but leaves the daemon on the code it started with; this check compares
  the source digest the daemon loaded (from its loopback-only `/api/daemon`) with the same digest of
  its checkout now, and warns when they differ. It uses `hv verify`'s digest, so a docs-only commit
  counts and a re-sign alone does not. A hive daemon from before this check (no `/api/daemon`) warns
  too; another program on the port gets no verdict. `--fix` restarts the managed daemon, at most once
  per run whichever check asks, and the 15-minute `hive-doctor.timer` therefore heals a bare pull.
  `hive-mind update` avoids the state altogether ([#112](https://github.com/projectmentor/hive-mind/issues/112))
- **modules:\<name\>** *(2.1)* — one result per installed module, none on a node with no modules: manifest and
  signature against the pinned publisher key (a failure **fails**, because a module runs code), then unit, device,
  config, listener and quota (**warn**). **modules-contract** warns about a node still on contract 2.0 in a hive
  that has modules and `quorum_m > 0`. `--fix` re-renders a unit and starts one that failed or is not enabled, and
  leaves a unit the operator stopped on purpose alone ([`MODULE_API.md`](MODULE_API.md))
- **ts-bounds**, **future-dated**, **flood** *(2.1, #217)* — advisory: entries the projection skips for a timestamp
  outside the bounds, devices whose entries arrived stamped over 10 minutes ahead of this node's clock, and devices that
  stamped more than `flood_per_minute` (120) or `flood_per_day` (2000) entries (`hv audit` reports the last two too)
- **journal-lock** *(2.2, #253)* — probes the lock a local write takes while it mints a sequence number. **Fails** when the
  filesystem refuses it (an NFS mount without lock support, `ENOLCK`), with the same one-line message a write prints;
  move `HIVE_HOME` to a local filesystem. A write that cannot take the lock stops with that message and exit 1, and
  journals nothing.
- **peers** — whether your peer nodes are reachable and in sync
- **peer-address** — for a peer whose stored address did not answer: whether its device has since
  reached this node from another address with a verified signed request, or answered one of this
  node's signed hellos there. If not, and the entry names a known device, doctor asks up to 8 candidate
  hosts (devices of the same name in `tailscale status`, the device's other recorded addresses, its
  join-request address; entry's port, 3 s each) for a signed `/hive/info`, and counts only an answer that
  proves exactly that admitted device at exactly that address (#107). If either finds it, `--fix` changes
  that entry's URL host in `.peers.json` and nothing else. An address that answers is never rewritten.
  Otherwise it only reports, listing the same-name devices as unverified hints (none where there is no
  `tailscale` CLI, as on Android)
- **peer-identity** — what each answering peer's signed `/hive/info` proves (verified, addr-unproven,
  unadmitted, purged, unsigned or invalid, and whether another device than the entry's `id` answered),
  which peers are still below sync protocol 3, and whether every peer is ready for `hv sync auth enforce`
  (protocol 2 or higher) and for `hv sync auth --outbound enforce` (verified). Advisory; it warns on an
  answer that is not merely from an older peer, and on older peers while outbound enforce is holding
  their push. `--fix` never acts on it

A few checks appear only when there is something to report: **crypto-modules** (a bundled
cryptography module failed to load, so signature checking is degraded), **journal-integrity**
(garbled or truncated journal lines were skipped on read), **capsule-conflicts** (two
devices sealed the same capsule version before syncing, so one value lost a deterministic tie),
**capsule-authz** (a `capsule` entry the projection declines because its signer was not authorized
under `capsule_putters`), **cell-authz** (the same for cells and combs under `cell_writers`),
**link-authz** (a `supersedes`/`resolves` link was downgraded to evidence — re-issue it owner-signed),
**forget-authz** (#122: an owner forget dated before the genesis owner needs no signature while
`forget_writers` is open, so an admitted device could backdate one. In 2.0 the check FAILS on an owned
hive whose policy is still open, listing by `h:` id each fact kept forgotten only by such a forget, and
names `hive-mind doctor --fix`, which closes it; once closed the check reports it closed),
**trust-drift** (a device's recent reliability fell well below its baseline; advisory, see `hv peers`)
and *(1.19 PR2b)* **fleet-contract** (an admitted peer advertises an agent contract below 1.19, or
is unreachable so it cannot be verified — such a peer lands but does not honour the `link` entries
that `--supersedes`, `--resolves` and `entity link` now write; upgrade it with `git pull`, or
`hive-mind group purge` a dead device; `--fix` never touches this).

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
unit. It also **announces this device's signing key** (one `announce` journal entry,
see `hv config identity announce`) when the device has never authored a signed entry — so a
directly-admitted device becomes capsule-addressable without anyone touching it.
It also **re-asserts the agent integration**: Claude Code is wired with a single
stable *dispatch shim* (`hive_dispatch.sh <event>`, one per lifecycle event) rather
than a hook per behavior — what runs for each event is decided inside that script, in
source control, so new behaviors arrive by update, not by editing `~/.claude`. If the
shim or the `hive-memory` skill is missing, or an older node still carries the previous
inline hooks, `--fix` wires the shim, migrates the old hooks away (so nothing fires
twice), and relinks the skill — leaving your own hooks untouched and taking a
`.bak.doctor` backup first. It also **repoints a peer that moved**: when the
`peer-address` check finds a peer's device verified at a new address, `--fix` updates
that one URL in `.peers.json` (see [SYNC_API.md](SYNC_API.md#configuration-peersjson)); that includes a
peer found by the signed `/hive/info` probe, which never contacted this node.
Because the 15-minute `hive-doctor.timer` runs `hv doctor
--fix`, a node that drifts heals itself with no one re-running the installer. Without
`--fix`, doctor only reports — it never kills or writes anything, so it stays safe to
run from cron. `--fix --dry-run` sits in between: it prints every action `--fix` would
take — which key files it would re-tighten, which orphan daemons it would kill,
whether it would rewrite the Claude Code config, and which peer URLs it would repoint —
without performing any of them.

Each check is marked healthy (✓), advisory (•), or failed (✗). The command exits
non-zero only when a check actually fails, so you can wire it into a cron job or a
monitoring probe and get alerted on real breakage, not on a peer being briefly
offline. Right after an upgrade the signed manifest can lag: the release bot
re-signs a few minutes after each merge to `main`. While your checkout is clean, sits
exactly on its upstream commit, and that commit is under 30 minutes old,
`authenticity` reports this as advisory (•) with a "re-sign pending" hint; run
`hive-mind update` again in a few minutes. Outside that window a mismatch is a
failure (✗), because an unsigned change to `main` is exactly what the check exists
to catch.

`hive-mind update` applies the same test before it replaces anything: the commit it would switch to must verify
under the release key this install already pins, or the update is refused and nothing changes. A commit whose
re-sign has not landed yet is waited for, then refused. `hive-mind update --allow-unsigned` overrides this for a
fork you trust.

`hive-mind update` also refuses a commit that is not a descendant of the one installed: an older tree (a rollback to
an earlier commit, whose signature is still valid) or a rewritten history. It prints both shas and changes nothing.
`hive-mind update --allow-rewind` installs it, for a history rewrite you made on purpose; the signature check still
applies. The normal fast-forward is unchanged. This is not complete rollback protection: the manifest carries no
monotonic counter, so an older signed tree committed as a descendant of the installed one still installs (see
THREAT_MODEL, "Update rollback").

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

> `hv merkle`, the 1.x alias, was removed in 2.0: it names this command and exits 2.

---

### Moving a hostname-era node to a device key (removed in 2.0)

`hv doctor migrate-identity`, the one-time step that re-stamped a journal from hostname `node_id`s to
device ids (1.3, #130), was removed in 2.0. A node whose journal is still under its hostname runs it on a
1.x release (1.28 is the last), then upgrades. In 2.0, `hv doctor migrate-identity` and
`hv migrate-device-identity` say so and exit 2.

---

### `hv doctor rebuild` — Fix the local database

If your local database looks wrong or out of date, `rebuild` resets it from
scratch. It's safe to run any time — your data won't be lost.

Also useful after pulling in entries from a peer node, or if HiveMind exited
unexpectedly.

```
hv doctor rebuild
```

(`hv rebuild`, the 1.x alias, was removed in 2.0: it names this command and exits 2,
having rebuilt nothing.)

---

### `hv wire claude` — Wire Claude Code

*`hv doctor wire-agent`, deprecated since contract 1.13, was removed in 2.0: it writes nothing, names
`hv wire claude` and exits 2.*

Wires the Claude Code dispatch shim and the `hive-memory` skill into `~/.claude`,
migrating any older inline hooks to the shim as it goes. It's idempotent — your own
hooks are never touched — and it's the same logic `hv doctor --fix` and the
installer/update all use, so there's one definition that can't drift. You rarely run
this by hand; the installer wires it, update re-asserts it, and the self-heal timer
keeps it in place. Reach for it only to wire a node immediately rather than waiting
for the next self-heal tick.

```
hv wire claude
```

Honors `CLAUDE_CONFIG_DIR` (same as Claude Code). On a node with no Claude Code it's
a quiet no-op.

Only foreign config files (like Claude Code's `settings.json`) need this shim. Agents
integrated as our own plugin code — Hermes and the MCP server — carry their wiring
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
falling back to the `--env-file` dotenv for smooth migration. `hv wire <claude-agent-cell>` replaced
`hv doctor wire-agent`, which 2.0 removed.

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
hv entity {add,list,show,link,join,unjoin} [options]
```

**Sub-commands:**

| Sub-command | What it does |
|---|---|
| `add` | Create a new entity. Needs `--name` and `--type` (e.g. `person`, `project`, `concept`). Optionally add metadata with `--attr` as a JSON object. |
| `list` | List all entities. |
| `show` | Show an entity and all facts linked to it. Needs `--name`. *(2.1)* Also lists each module entity (`x-<module>:…`) a core `same-as` joins to it, with its facts; `--no-joins` prints the core entity only. |
| `link` | Attach a fact to an entity. Needs `--name` and `--fact-id` (the fact's `sid` `h:…` or `ref`; a bare local id is refused). *(1.23)* `--source` names the writer, as on `hv remember`; omitted it is `manual`, and a `manual` link is owner-signed only through `hive-mind` (#114). Optionally set `--confidence` to indicate how strongly the fact relates. *(1.19 PR2b)* Journaled as one `entity` **link** (evidence-class: it never hides anything, so it needs no authority). |
| `join` | *(2.1, #208)* Join a module's entity to a shared one: `--name x-<module>:<n> --to <n>` writes a core `same-as` link. `--name` must be a module-prefixed entity and `--to` an unprefixed one. A module cannot write this itself. |
| `unjoin` | *(2.1)* Withdraw a join: the same flags. Plain `unjoin` withdraws **only the joins this device wrote** (one `retract` per join; another device's join stays live) and exits 1 if this device wrote none. `--owner` is the owner's withdrawal of every live join of the pair, and runs as `hive-mind entity unjoin --owner`. |

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
./hv entity link --name "HiveMind" --fact-id h:3f9a1c0b2d

# link — with confidence score
./hv entity link --name "HiveMind" --fact-id h:3f9a1c0b2d --confidence 0.9
```

---

### `hv feed` — The journal feed for modules *(2.1)*

```
hv feed [--after node_id:seq,node_id:seq] [--limit N] [--format json]
```

Read-only. For each node, the journal entries whose `seq` is past your cursor for that node, as JSON, in
`(node_id, seq)` order: `{"entries": [...], "cursor": "...", "more": false}`. Each entry has `node_id`, `seq`, `ref`
(`node_id:seq`), `sid` (`h:…`), `type`, `timestamp`, `payload`, and `sig` and `pub` when it has them. `--after` is a
comma-separated cursor (an empty one starts at the beginning, a malformed pair is an error); send the returned
`cursor` next. `--limit` defaults to 200 and `more` is true when it cut the page short. An entry whose signature
fails is absent; one with no `sig` is attributed, not authenticated.

The core computes two fields so a consumer never reimplements the forget rule: `forgotten` (true or false) on every
`fact` entry, and `affects` (a list of refs) on every owner `retract`. Every other type omits `forgotten`: a missing
flag means the type cannot be forgotten. The same result is served to a module at `GET /v1/feed`
([`MODULE_API.md`](MODULE_API.md), and the consumer's obligations in [`CONTRACT.md`](CONTRACT.md)).

---

### `hv peers` — Hive members, reachability, staleness, trust drift

Lists every admitted device with its principal, address, last-seen date, reachability, and
*(1.19)* a **DRIFT** column: the device's short-window reliability minus its long-window
reliability (design §8). Reliability is `1 − contradicted/asserted` over the facts that device
wrote in the window, where "contradicted" means retracted or `contradicts`-linked by a
**different** device (self-correction never counts). `—` means nothing to measure yet. The
signal is the *change*, not the level: a long-reliable device that suddenly starts being
contradicted is worth a look; one that was always mediocre is not news. Windows and the
warning threshold are the governed knobs `trust_long_days`, `trust_short_days` and
`trust_drift_threshold` (see `hv config`). `hv doctor` reports devices below the threshold under
**`trust-drift`**, split by channel (`sense` observations vs `introspect` reasoning) and also on
the mean outcome score of the device's recent decisions. **Advisory only:** by decision, drift
changes nothing — not admission, not purge, not link authority.

```
hv peers
```

### `hv group` and `hive-mind group` — Membership lifecycle

By default any device can corroborate. Once you have an owner, only **admitted**
devices count toward confidence — so someone can't mint a pile of keys and fake a
crowd (a Sybil attack). `hv group` shows the roster; the lifecycle is the owner's, on `hive-mind group`:

```
hv group                                       # roster (admitted/pending/denied/purged)
hv group list                                  # same as above
hive-mind group admit                                 # list devices awaiting admission
hive-mind group admit k1:597b3e0f5fb92d37 --principal david
hive-mind group admit k1:… --principal david --module hwatch   # a module's own device (2.1): it never votes
hive-mind group revoke k1:…                           # un-admit (reversible) → device goes STERILE
hive-mind group deny k1:…                             # reject a pending join-request (admit overrides)
hive-mind group change k1:… --principal newname       # re-tag a device's principal (admission unchanged)
hive-mind group purge k1:…                            # tombstone: permanent; its entries stop counting
hive-mind group retire k1:… [--through SEQ] [--confirm]   # tombstone an honest device (*2.3:* or a revoked former member, with --confirm); its chain through SEQ (default: highest held) keeps counting
```

With no device_id, `hive-mind group admit` lists the pending join-requests. (Those also surface
in your session-start digest, so your agent can prompt you.) `--principal` tags who
owns the device; when every device behind a fact belongs to the same principal, its
confidence is capped. Admitting a device also **seeds a reciprocal peer** from the URL its
join-request advertised, so the owner syncs *to* the member too — connectivity is seeded by
admission but stays editable in `.peers.json`. `--module NAME` *(2.1)* marks the device as module NAME's: it is admitted like any other
(give it the operator's `--principal`, so `cap_self` still bounds it), but it neither proposes nor votes in a quorum
election, and no peer is seeded for it (a module has no sync address). A later `admit` without `--module` clears
the mark. `--module` requires `--principal`. A 2.0 node ignores the mark and still counts that device's vote. A 2.0 node lists a module device as unreachable under `fleet-contract` for as long as it is admitted. A 2.1 or newer node does not, and `modules-contract` warns about a 2.0 node when the hive has modules and `quorum_m > 0`. Admission grants only write/fertility, never
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

> *(2.0)* The lifecycle moved to the control plane. Run on `hv`, `group admit`, `revoke`, `deny`, `change`
> and `purge`, and the older top-level `admit`, name the `hive-mind group` form and exit 2. `hv group` and
> `hv group list` stay.

---

### `hv join` — Request admission to a hive

After your device has synced a hive (its peer is in `.peers.json`), `hv join` asks
that hive's owner to admit you.

```
hv join --principal carol
```

This is **non-blocking**: you are already reading the hive. Once an owner exists,
a computer that is not admitted cannot add facts. The owner stores those facts
only after `hive-mind group admit`. The request shows up for the owner to approve.
You do not wait. It prints your `device_id` so you can pass it along.

On a brand-new device that has no key yet, `hv join` **mints the device key for you**
first (you need one to be admitted and to open capsules), so there's no separate
`hv config identity init` step. Running `hv join` again while a request is already pending is a
no-op — it won't pile up duplicate requests.

---

### `hv config identity` — This device's device identity

Each device is identified by an Ed25519 **device key**, not its hostname. The key
proves which device wrote an entry, so a peer cannot impersonate your device to
inflate confidence. Your `node_id` is the key's fingerprint, like
`k1:2a2110f3d8963a9e`. Through 1.x `hv key` was a silent alias of `hv config identity`;
2.0 removed it, so `hv key …` names the `hv config identity …` form and exits 2.

```
hv config identity announce # publish this device's key as a signed journal entry
hv config identity show     # show this device's device_id and public key
hv config identity init     # mint a device key (fresh install only)
```

A fresh install mints a key automatically. `hv config identity init` refuses to run on a node
that already has history under its hostname, because minting a key there would
split its identity; such a node re-stamps its journal on a 1.x release first (see above).
The private seed lives in the key directory outside the checkout, as `device-key`
(`hv whoami` prints where; `$HIVE_KEY_DIR` overrides) — keep it secret, never commit
or sync it. Share your `device_id` and public key with peers (they go in
`.peers.json`).

`hv config identity announce` publishes the key as a signed journal entry (an authority-less
`announce` act, kind `key`). Capsules can only be sealed to a device whose public
key peers can *harvest from an entry that device signed* — a payload-carried key is
never trusted. A device that ever wrote anything (even its `hv join` request) is
already harvestable, and `announce` says so and does nothing. It exists for the one
device that never wrote: admitted directly by the owner, silent since. You rarely
need to run it — `hv doctor --fix` (the 15-minute timer) emits it automatically on
any device of an owned hive that needs it.

---

### `hv owner` and `hive-mind owner` — Governance owner identity

Confidence weighs *who* corroborates a fact. A few of those rules need a shared,
trusted source: which devices count, who owns each one, and a couple of tunable
numbers. That trust is rooted in an **owner key** — a separate Ed25519 key (apart
from your device keys) that signs governance decisions into the journal, so every
node agrees on them.

**Two planes (2.0).** The reads and the device-signed election verbs stay on `hv`, because an admitted member
running only `hv` must be able to propose and vote when the owner has gone dark:

```
hv owner elections                                   # list open owner-election proposals and their tallies
hv owner propose-election --pub B64                  # (admitted device) propose electing a new owner key
hv owner show                                        # the established owner + admitted devices + config
hv owner vote <proposal_id>                          # (admitted device) vote for an open election proposal
```

Everything that owner-signs, or reads or writes the owner key, runs on the owner's control plane. Run on `hv`,
each of these names the `hive-mind` form and exits 2, acting on nothing:

```
hive-mind owner claim [--mint] [--force]             # (successor side) claim ownership against a nomination
hive-mind owner escrow                               # store the key (passphrase-encrypted) IN the hive
hive-mind owner export [--out FILE] [--plaintext]   # back up the owner key to an off-device file (passphrase-encrypted; --plaintext is the opt-in unencrypted form)
hive-mind owner freeze-timestamps                    # arm the bounded entry timestamps (2.1, #217)
hive-mind owner heartbeat                            # refresh owner liveness (resets the dead-man timer)
hive-mind owner import FILE [--force]                # restore it from a file on another device
hive-mind owner init                                 # mint the owner key and claim ownership (once)
hive-mind owner mint [--force]                       # mint owner-key material to propose in an election
hive-mind owner nominate <successor_pub>             # nominate a NEW owner key as successor
hive-mind owner pin [--set | --fingerprint FP] [--force]  # show, or pin, the genesis this node accepts
hive-mind owner restore                              # recover the key from the hive's escrow
hive-mind owner revoke <node_id:seq|all>             # tombstone an escrowed key so `restore` skips it
hive-mind owner seal                                 # seal a plaintext owner key at rest under a passphrase
hive-mind owner standby <device_id> [--off]          # declare an advisory standby key holder
hive-mind owner transfer <new_owner_pub>             # immediate handoff to a key the target already holds
hive-mind owner unnominate <successor_pub>           # withdraw a pending nomination
```

You run `hive-mind owner init` once, on whichever machine you want to hold the owner key.
It mints a **`hive_id`** (a public identifier that keeps your hive separate from
any other hive on the same tailnet) and writes an owner declaration into the
journal that the other nodes pick up on sync. Until you do this, the governance
rules below are simply off (every device counts, nothing is capped) — so it's opt-in.

**The genesis pin.** One `owner` declaration establishes the hive, and the node records
which one it accepted in `$HIVE_HOME/.genesis-pin` — a private, per-node file (mode
`0600`) that is never synced, never written into the journal, and never part of the signed
source manifest. The pin is what the node resolves genesis through, at ingest and in the
projection, so another `owner` declaration arriving later cannot replace the owner no
matter what timestamp it carries. Without it the rule would be "the earliest valid
self-signed declaration wins", and an entry's timestamp is written by whoever made the
entry.

A **joining** device pins from the invite instead, before it has pulled anything:
`hive-mind owner pin --fingerprint h1:…/o1:…/0a1b2c3d` (the installer does this for you when the
pasted invite carries a fingerprint). Only a hash *prefix* travels in an invite, which is
enough — a squatter can copy a `hive_id` and an `owner_id`, but not the genesis entry's
hash. The pin then names the exact entry as soon as the declaration itself syncs.

`hive-mind owner init` pins the declaration it writes, so a new hive is pinned from the start.
An existing hive pins on the operator's word: `hive-mind owner pin --set` takes the journal's
single self-signed declaration. If the journal holds **two**, it refuses and lists them —
there is deliberately no timestamp tie-break, because the timestamp is the part an
attacker controls. Resolve it by re-joining from the device you trust, comparing the
**genesis fingerprint** (`<hive_id>/<owner_id>/<hash8>`) that `hv owner show` prints; the
hash prefix is the part a squatter cannot reproduce — and the prefix is short enough to grind on its
own, so what actually stops them is that the pin binds the **declared owner**: a genesis candidate must be
self-signed by the owner it declares, so a rival needs the victim's owner key. `hv doctor genesis` reports an
unpinned node, a pin whose declaration has not synced yet, and any journal holding more
than one declaration.

A node with no pin keeps the older behaviour, so a fleet mid-upgrade keeps syncing. **The Merkle consequence, plainly.** A rival that a peer already stored is never removed — the journal is append-only. So a node that refused it and a node that holds it differ in their Merkle root from then on, and that difference does not heal. What converges is the *projection*: once every node pins the same genesis, they all agree on the owner. A node that has not pinned keeps the old rule and can project a **different owner** than a pinned peer — visibly, not silently, and `hv doctor genesis` names it.
`hive-mind owner init --force` re-pins **this** node and is a deliberate fork: peers that keep
the old pin keep the old owner, and the old declaration stays in the journal, because
nothing is ever removed from it.

**The owner key is sealed at rest (2.0).** Every new owner key is written only as `owner-key.sealed` in the
key directory: the passphrase envelope `owner escrow` uses (scrypt, then ChaCha20-Poly1305). `hive-mind`
asks for the passphrase once per command that owner-signs, however many signatures that command makes, and
`$HIVE_OWNER_KEY_PASSPHRASE` supplies it for automation. A cancelled or wrong passphrase signs nothing. A
key from before 2.0 is still plaintext: `hive-mind owner seal` converts it in place (same owner, no journal
change), and `hv doctor` fails until you do. `hv` never opens the key in either form.

**The owner key is a single point of failure — back it up.** `hive-mind owner init`
auto-stashes a copy to `~/.config/hive-mind/identity/.owner-key.sealed` (survives uninstall; sealed since 2.0),
and `hive-mind owner export` writes a portable copy you can store off-device (use
`$HIVE_OWNER_PASSPHRASE` or the prompt for its passphrase; the export is sealed by default and `--plaintext` is the opt-in for an unencrypted file; the old `--passphrase` flag is still accepted and changes nothing, since sealed is already the default; an exported key is total hive authority, so treat it
like an SSH private key). If the owner device dies, `hive-mind owner import` installs the
key on a new device and governance resumes under the **same** owner identity — no
journal change. `import` refuses a key that doesn't match the journal's established
owner unless you pass `--force`.

`hive-mind owner escrow` stores the owner key **inside the hive itself**, passphrase-encrypted,
so it syncs to every node and any synced device can recover it with `hive-mind owner restore`
(no file to move). Two cautions, because the journal is shared and append-only: the
encrypted blob is readable by **every** device that syncs the hive (including read-only
members) and **can't be un-published**, so the passphrase is effectively your hive master
key — make it strong (a 12-char minimum is enforced). Escrow and the off-device file cover
different threats: escrow handles "I lost a device," the file handles "I don't fully trust
the shared store." Use the one that fits, or both.

`hive-mind owner standby <device_id>` records an advisory note that a device is sanctioned
to also hold the owner key and act while the primary owner is offline (it must
actually hold a copy via export/import — the key is the authority; the declaration
is for visibility in `hv owner show`/`hv whoami`).

**Changing who the owner is — succession.** Backup/restore recovers the *same* owner
identity. To hand off to a *new* key (a fresh device, or to rotate away from a leaked
one), use succession. On the successor device, `hive-mind owner claim --mint` mints a fresh
owner key and prints its public key. Give that pubkey to the current owner, who runs
`hive-mind owner nominate <pub>`; once that syncs, the successor re-runs `hive-mind owner claim` to
take ownership. From then on the old owner key can no longer sign governance — its
post-handoff acts are simply ignored by every node. `hive-mind owner transfer <pub>` is the
immediate variant (no claim round-trip), but the target device must *already hold* that
key or governance becomes unsignable, so prefer nominate+claim. A live owner can never
be unseated: only the current owner's own nominate/transfer, or a nominee's claim against
an open nomination, advances ownership.

`hive-mind owner revoke <node_id:seq|all>` logically tombstones an escrowed key so
`hive-mind owner restore` skips it (`node_id:seq` from `hv owner show` / the doctor owner check,
or `all`). The ciphertext stays in the append-only journal forever — a tombstone is not
deletion — so if an escrow *passphrase* leaked, the real fix is to **rotate the owner key**
via succession/transfer, which makes the old escrow blob unlock a key that is no longer
the owner.

**When the owner is gone with no backup — quorum election.** Backup/restore and succession
both need someone to act *before* the owner is lost. If the owner device dies with no escrow,
no exported file, and no nominated successor, the admitted devices can still elect a new owner —
but only by agreement, and only once the old owner has genuinely gone dark. The owner first
turns this on (it is off by default): `hive-mind config set quorum_m <N>` sets how many admitted
devices must agree, and `hive-mind config set dead_man_days <D>` sets how long the owner must be
silent first (default 30). Then, if the owner goes dark, any admitted device runs
`hv owner propose-election --pub <base64>` with a key it holds: `hive-mind owner mint` mints a fresh one
and prints it, or name a key held elsewhere. The others run `hv owner vote <proposal_id>`. Once `quorum_m` devices have
endorsed one proposal **and** no owner-signed act has appeared for `dead_man_days`, that proposal
installs its key as the new owner on every node. `hv owner elections` lists open proposals and
their tallies. The dead-man switch is what keeps this safe: **a live owner can never be unseated**,
because any owner-signed act — including an explicit `hive-mind owner heartbeat` — refreshes the owner's
last-activity and re-shuts the window. Votes and proposals are device-signed by admitted members,
so an outsider cannot stuff the ballot. `quorum_m=0` (the default) disables elections entirely.

---

### `hv remember` — Store a fact

Save anything worth keeping: observations, constraints, gotchas, status updates.
Facts are searchable, tagged, and automatically gain credibility when multiple
independent sources agree on the same thing.

```
hv remember <content> [--tags TAGS] [--source SOURCE] [--importance N] [--gate] [--resolves ID]
            [--outcome-of DECISION | --resolves FACT | --supports REF | --contradicts REF | --extends REF]
            [--polarity -1|0|1] [--channel sense|act|introspect]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The text of the fact. Put it in quotes. Required. |
| `--tags` | Comma-separated labels to help you find it later, e.g. `--tags infrastructure,todo`. No spaces. |
| `--source` | Who or what is asserting this fact. Helps HiveMind tell independent sources apart. Defaults to `manual`. See [Source identity](#source-identity) below. |
| `--importance` | A numeric **hint** (0–1, default 0.5) for how significant you think this fact is. *(1.19 PR6)* It is journaled as asserted, but the projected `facts.importance` starts at `min(hint, importance_self_cap)` (default cap 0.3) and rises only when **other** identities link to the fact — importance is learned, never self-declared. `hv search --sort importance` ranks by it. |
| `--gate` | Filter this write through the **admission gate** (salience layer 2) — a content-neutral structural check that silently drops writes that are not knowledge-shaped (trivially short, a bare question, a greeting). It never judges topic or importance; that judgment stays with the agent (layer 1). Useful when an agent is writing many facts at once and you want to keep your memory clean. |
| `--outcome-of DECISION` | *(1.19)* This fact is the **outcome** of a decision. `DECISION` is the decision's `sid` (`h:…`, preferred) or `ref` (`node_id:seq`), both shown by `hv search`, or a local decision id (`17` / `d17`, *deprecated*), resolved and kind-checked **before** anything is written — a bad reference aborts with no fact and no link. Emits the fact plus an `outcome-of` link carrying the polarity. Outcomes feed the decision's `outcome_score` (a *vindication* axis — decisions have no confidence, by design) and the utility of the facts that informed it. |
| `--polarity` | With `--outcome-of`: `1` it worked out (default), `-1` it did not, `0` observed and neutral. Deliberately ternary: magnitude comes from how many independent identities report an outcome, not from one agent's claimed intensity. |
| `--channel` | Which experience signal this write is: `sense` (an observation of the world — the default when absent), `act` (an action taken), `introspect` (the agent's own reasoning or plan). An `introspect` outcome is recorded but **never counted** toward `outcome_score`. An `introspect` fact, and an `introspect` `supports`/`contradicts` link, weigh `introspect_support_weight` (default 0) toward confidence *(facts since 1.21)*: the fact is recorded and searchable but corroborates nothing until an observation backs it. A channel outside these three (possible only in a raw journal entry) counts as `introspect`. |
| `--resolves FACT` | Mark this write as the correction of an earlier fact, named by its **`sid`** (`h:…`, preferred), or its `ref` (`node_id:seq`); a bare local id is refused and aborts the write. It journals one `resolves` **link** *(1.19 PR2b — no separate `retract` entry any more)* from the new fact to the resolved fact's journal identity (stable across rebuilds and nodes); the projection folds it as negative evidence that **soft-retracts** the old fact (so it stops surfacing as canonical) and, when the link is hard, records the chain on the new row — keeping the corpus from asserting the old and corrected claim at once. Reversible; a decisive forget is still `hive-mind retract <sid> --owner`. A reference that names something that is not a fact aborts; one that resolves to nothing is a warning (the fact is still written, without a link). The audit's **CONTRAVENED** check separately flags a correction that names a fact in *prose* (`resolves h:3f9a1c0b2d`, `supersedes #N`) but never reconciled it — a prose `h:…` is matched **exactly**, while a prose `#N` is a **local id** that drifts across rebuilds and nodes, so that target is best-effort. |
| `--supports REF` | *(1.21)* This observation **supports** a fact or an idea, named by its `sid` (`h:…`, preferred) or its `ref`; a bare local id (`118`, `i5`) is refused. Writes the fact plus one `supports` link, resolved and kind-checked **before** anything is written (a decision aborts with a pointer to `--outcome-of`). The target is re-scored immediately. It counts as this identity's evidence on the write's `--channel` (`introspect` weighs `introspect_support_weight`, default 0). An idea's author can't support their own idea. |
| `--contradicts REF` | *(1.21)* The same, as negative evidence: one `contradicts` link. An idea's author may contradict their own idea, which is how to withdraw it. |
| `--extends REF` | *(1.22)* This entry **builds on** (extends, comments on) a fact, an idea or a decision (same forms as `--supports`; `d17` for a decision). Writes the fact plus one `extends` link, resolved and kind-checked **before** anything is written. A relationship, not evidence: it never moves the target's confidence, on any channel. `hv search` shows `extends h:…` on the row. The confirmation line reads `↗ extends <kind> <sid>`. |

**What you get back:**

- `Remembered as h:3f9a1c0b2d (fact #N, confidence 0.45, 1 identity)`, then `ref: <node_id:seq>` —
  the write is always journaled. If identical content already exists, the new entry corroborates
  that same fact (same `sid`), and its confidence rises only when the identity behind it is new.
- `Skipped (admission gate): …` — only with `--gate`, when the text is not statement-shaped.

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

# With the admission gate — non-statements are silently dropped
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
hv retract <fact> [--reason TEXT] [--source SOURCE]            # peer negative evidence
hive-mind retract <fact> --owner [--reason TEXT] [--source SOURCE]  # owner forget (2.0: on hive-mind)
```

**Arguments:**

| Argument | What it does |
|---|---|
| `fact` | The fact to retract, named by its **`sid`** (`h:3f9a1c0b2d`, shown by `hv search` — the same on every node, never changes) or its `ref` (`node_id:seq`). A bare local id (`4`) is refused (2.0): it exits 1 with `'118' is a local id, which 2.0 no longer accepts`, having written nothing: it is this node's rowid, reassigned on every rebuild (after every write and every sync), so an id read from `hv search` could name a *different* fact by the time you retract it. The target is kind-checked — a decision id is an error, never a silent re-target. Required. |
| `--reason` | Why you're retracting it. Saved for reference. |
| `--source` | Who is doing the retracting. Defaults to `manual`. |
| `--owner` | Mark this as an authoritative retraction. Use when the fact is definitively wrong, not just uncertain. Immediately drives confidence to the floor. Once you have an owner (see the owner section), this requires the **owner key** and is cryptographically signed, so a forget can't be forged. In 2.0 it runs as `hive-mind retract … --owner` on the owner machine; run on `hv`, `retract --owner` names that form and exits 2. |

**Examples:**
```bash
# Minimal — by short id (from `hv search`)
./hv retract h:3f9a1c0b2d

# With a reason
./hv retract h:3f9a1c0b2d --reason "Was a test probe, not a real observation"

# With reason and source
./hv retract h:7c01d2e9aa \
    --reason "Portproxy is no longer used — architecture changed" \
    --source "hermes:primary/claude-sonnet/abc12345"

# Owner retraction — authoritative, immediately floors confidence
hive-mind retract h:9be4410f37 --reason "Definitively wrong" --owner

# All options together, on the owner machine (a raw journal ref works too)
hive-mind retract k1:597b3e0f5fb92d37:15 \
    --reason "Superseded by Tailscale-in-WSL architecture" \
    --source "claude-code" \
    --owner
```

---

### `hive-mind unforget` — Reverse an owner forget *(1.24, #46)*

An owner forget (`hive-mind retract --owner`) floors a fact on every node. `hive-mind unforget`
reverses it: the fact comes back, and its confidence re-derives from the evidence
that still stands. **Owner only**: it needs the current owner's key, so run it on
the owner machine. It is not exposed over MCP.

```
hive-mind unforget <fact> --reason TEXT
```

| Argument | What it does |
|---|---|
| `fact` | The forgotten fact, named by its **`sid`** (`h:…`, preferred), or its `ref` (`node_id:seq`); a bare local id is refused. Kind-checked: a decision is an error. Like a forget, it acts on the fact's **text**, so every row with the same text comes back. |
| `--reason` | Why the forget is reversed. Required; journaled. |

It refuses, writing nothing, when the fact is not forgotten, when this device has no
owner key, or when the key is not the hive's current owner's. The latest act the
owner signed wins, so `retract --owner` → `unforget` → `retract --owner` ends
forgotten. A node older than 1.24 skips the unforget and keeps the fact forgotten
until it upgrades.

```bash
hive-mind unforget h:9be4410f37 --reason "Forgotten by mistake on 2026-08-18; the fact was right"
```

---

### `hv search` — Search facts, decisions and ideas

Find facts by keyword. Results are ranked by confidence — the most corroborated
facts come first. Matching **decisions** are listed too, under a `Decisions:`
section (newest first, superseded ones flagged), matched on their content,
rationale, or tags — so `hv search hive-mind` surfaces that project's decisions
alongside its facts. Decisions have no confidence, so `--min-confidence` does not
filter them in text mode.


> *(1.19)* Every result — text and `--format json` — carries two stable identities: **`sid`**, the short id
> (`h:3f9a1c0b2d` — what you type into other commands) and **`ref`**, the raw journal identity `node_id:seq`.
> Use either wherever you cite an entry to another command (`--informed`, `--outcome-of`, `--supersedes`,
> `--resolves`, `hv retract`, `hv entity link`); the `· N` / `#N` beside it is this node's rowid and shifts
> on rebuild (see [Stable ids](#stable-ids-sid-vs-local-id)). Decisions also show `informed by:`
> (the sids they relied on, with this node's current local ids in brackets) and, once any outcome has been
> recorded, `outcome ±0.xx` — the decision's outcome score with its evidence decayed under the fact half-life
> (JSON: `outcome_score`, `effective_outcome_score`, `last_outcome_at`; `null` = no outcome yet). Outcome score
> is **not** confidence: `--min-confidence` still never filters decisions.
```
hv search <query> [--format {text,json}] [--min-confidence N] [--kind {all,fact,decision,idea}] [--sort {confidence,importance,utility,recency}]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `query` | What to search for. Multiple words all have to match. Use `OR` between words for either/or. Use `"quoted phrases"` for exact matches. Matches facts (content/tags) and decisions (content/rationale/tags). |
| `--format` | `text` (default) for readable output. `json` for machine-readable output you can pipe to other tools. JSON is a flat list; each row carries a `kind` field (`fact`, `decision` or `idea`). `tags` is a JSON list *(2.0, [#77](https://github.com/projectmentor/hive-mind/issues/77))*, and `tag_list` *(1.21)* is the same list. |
| `--min-confidence` | Only show facts at or above this confidence level (0.0–1.0). Good for filtering out unverified claims. (In JSON, decisions carry no confidence, so a `min_confidence > 0` consumer drops them.) |
| `--kind` | *(1.20)* What to search: `all` (default), `fact`, `decision`, `idea`. Under `all` an **idea** appears only once it has earned confidence above 0 — a raw hypothesis is not knowledge yet; `--kind idea` lists every idea. JSON rows carry `kind: idea` with `confidence`, `effective_confidence` and `ref`. |
| `--sort` | *(1.19 PR6)* How to rank facts and ideas: `confidence` (default — effective confidence, unchanged behaviour), `importance` (learned salience: capped self-hint + other-identity link attention), `utility` (how much recorded decisions relied on it, weighted by their outcomes), or `recency`. Decisions always list newest-first. Text rows show `Imp:` / `Util:`; JSON rows carry `importance`, `effective_importance`, `utility`, `effective_utility`, `last_link_at`. Both learned values are stored undecayed and decayed at query time under the entry's **class half-life** (`halflife_fact` / `halflife_idea` / `halflife_volatile`), which now also governs confidence decay. |
| `--id SID\|REF` | *(2.2, [#158](https://github.com/projectmentor/hive-mind/issues/158))* Look up **one** entry by its `sid` (`h:…`) or `ref` (`node_id:seq`) instead of searching; `query` is then omitted. It covers facts, decisions and ideas, reads the local store (no sync daemon needed) and answers the same on every node and after a rebuild. Text shows kind, `sid`, `ref`, content, tags, source, confidence (a decision shows `current`/`SUPERSEDED` and its outcome) and contested/forgotten state; `--format json` prints `{kind, id, ref, sid, item}`. **Exits 1 with nothing on stdout when the id names nothing** (an invented id such as `h:e6f0b4c1a2`), so a script or agent can verify an id before citing it: `hv search --id h:… >/dev/null`. `hv search` with neither a query nor `--id` exits 2. |
| `--chain SID\|REF` | *(after v2.3.0, [#157](https://github.com/projectmentor/hive-mind/issues/157))* Follow **one decision's** supersede chain instead of searching: one hop per line, each naming the superseding entry by `sid` and the link's authority, to the live entry. A `supersedes` link recorded only as evidence is listed as **not in effect** and the walk stops there. `--format json` prints `{chain: [{sid, ref, content, superseded_by_sid, superseded_by_ref, authority, supersede_evidence}], live}`. Exits 1 when the id names no decision. Reads the local store. The contract number is still 2.3 until the next contract note. |

**Who superseded a decision** *(after v2.3.0, #157)*. A superseded decision now names its superseder by stable id on every surface, as additive fields beside the unchanged node-local `superseded_by` rowid: text `⚠ SUPERSEDED by h:… (hard)`; JSON in `hv search`, `hv search --id` and the daemon's `/api/item` and `/api/search`: `superseded_by_sid`, `superseded_by_ref`, `superseded_by_authority` (`hard`, the only authority that supersedes; `null` when not superseded) and `supersede_evidence` (a list of `{sid, ref, authority: "evidence"}` for `supersedes` links that are recorded but **not in effect**, which text shows as `(supersede by h:… is evidence only, not in effect)`). The dashboard's `SUPERSEDED` pill links to the superseding entry. The MCP `hive_search` and `hive_lookup` return the same fields.

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

### Stable ids: `sid` vs local id

*(1.19 PR3b)* Every fact, decision and idea has three names:

| Name | Example | Stable? | Use it for |
|---|---|---|---|
| **`sid`** — short id | `h:3f9a1c0b2d` | **Yes** — identical on every node, never changes | Typing into any command that takes an id: `--informed`, `--outcome-of`, `--supersedes`, `--resolves`, `hv retract`, `hv entity link --fact-id`; prose references (`resolves h:3f9a1c0b2d`); dashboard deep links (`/#h:3f9a1c0b2d`). |
| **`ref`** — journal identity | `k1:597b3e0f5fb92d37:401` | **Yes** — the identity `sid` is derived from | Machine use; accepted everywhere `sid` is. |
| local id / rowid | `118`, `d17`, `i5` | **No** — a store.db rowid, reassigned on every rebuild (after every write and every sync) and different on every node | Nothing durable. **Refused as an input since 2.0** (#59, #64): pass the `sid` or the `ref`. |

`sid` is `h:` + the first 10 hex characters of `sha256("node_id:seq")` — derived purely from the
journal identity, never stored in an entry, rebuilt with everything else into the indexed column
`journal_index.sid`. Resolution is an **exact** lookup (never a prefix match). A fact corroborated by
several entries has one canonical `sid` (its first asserting entry, the same on every node), but each
corroborating entry's own `sid` resolves to the same row. Rowids are *not* removed from `store.db` —
they remain its internal keys and join columns; they just stop leaking out as names.

---

### `hv dash` — Open the web dashboard

A read-only dashboard for browsing your hive in a browser. Four tabs: **Overview**
(counts, convergence, contested, the live audit summary), **Corpus** (facts and
decisions — search, filter by tag, sort by confidence, importance, utility or recency,
filter by status: contested, forgotten by the owner, or volatile; paginated), **Hive**
(peers + health, with live sync status), and **Telemetry** (this node's sessions,
tokens, and cost over time). It's served by the **sync daemon** itself (no extra
process) at `http://127.0.0.1:9876/`, and it answers only on the device it runs on:
open it on any device that runs HiveMind, including an Android phone running it in
Termux. A browser on a device without its own node, pointed at another node's tailnet
address, is refused (403) since the July 2026 read-auth fix (PR #42). To see another device's data, pick it
in the dashboard: your own daemon fetches it with a signed request. Nothing is
exposed to the public internet.

`hv dash` prints the URL and tries to open your browser. On a headless box (or
Android/Termux, or WSL) where it can't, the printed URL is the point — open it
yourself.

```
hv dash [--print]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `--print` | Print the dashboard URL only; don't try to open a browser. Handy in scripts. |

**Examples:**
```bash
# Open the dashboard in your browser
./hv dash

# Just show me the URL
./hv dash --print
```

The dashboard is **read-only** — it never writes to the corpus. Governed actions
(admitting devices, retracting, deciding) stay on the CLI by design.

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
| `auth [off\|permissive\|enforce] [--outbound off\|permissive\|enforce]` | With no argument, show both modes. The positional mode sets this node's sync read-auth mode (default `permissive`; restart the daemon to apply): `enforce` requires every remote sync read to be signed by an admitted device — switch once all peers report protocol version 2. See [SYNC_API.md](SYNC_API.md#access-control). `--outbound` sets what a peer must prove before this node pushes to it (default `permissive`, which only flags; applies from the next sync round): under `enforce` a peer whose signed hello does not verify is only pulled from, or skipped when the signature is invalid or the device purged — switch once `hv doctor` (`peer-identity`) says every peer is ready. See [SYNC_API.md](SYNC_API.md#responder-signatures-hello_sig). |

**`.peers.json` format** (the installer writes it):
```json
{
  "self": "k1:10f6b761dd1c2a90",
  "port": 9876,
  "peers": [
    { "id": "100-64-0-2", "url": "http://100.64.0.2:9876" }
  ]
}
```

- `self` — this device's id.
- `peers[].url` — a peer's address. `hive-mind invite`, run on that peer, prints the address to use;
  admitting a device with `hive-mind group admit` also adds it from its join request.
- `peers[].id` — a label for logs. Optional.
- `port` — the port to listen on (default 9876).
- Optional: `bind` (override the listen address — by default the daemon binds this device's Tailscale
  IP, else `127.0.0.1`, never all interfaces; a legacy `0.0.0.0` counts as automatic),
  `sync_auth` (the read-auth mode, set by `hv sync auth`) and `sync_auth_outbound` (the outbound mode,
  set by `hv sync auth --outbound`).

This file is not synced to git — it's specific to each machine.

**Examples:**
```bash
# Sync now — one-shot manual sync
./hv sync now

# Run the daemon manually (the background service does this automatically)
./hv sync daemon

# Show both auth modes; push only to peers whose signed hello verifies
./hv sync auth
./hv sync auth --outbound enforce

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

It prints this device's `device_id`, the `hive_id` and `owner`, your `principal`, and your **status**.
The two ids are labelled `(public id)`: they are fingerprints, safe to share. A `keys:` line says which
directory holds the private keys, and never prints them (2.0). The id stays the second word of its line.

- **OWNER** — you hold the owner key (admit devices, set config, forget facts).
- **FERTILE** — admitted; your writes land in the shared journal and count toward confidence.
- **STERILE** — read-only; you read the whole hive, but your content won't land until the owner admits you (`hv join` to request it).
- **UNAFFILIATED** — no hive yet (`hive-mind owner init` to start one, or sync one and `hv join`).

If you're sterile, your session-start digest says so too, so your agent isn't left guessing.

---

### `hv verify` — Check that this is the official release

```
hv verify
```

Tells you whether this install is the official HiveMind from ProjectMentor. Three independent
checks, strongest last:

1. **Integrity** — every source file matches the bundled manifest (`verify.json`). Catches local
   edits and accidental drift.
2. **Signature** — the manifest carries a valid Ed25519 signature (`verify.json.sig`) under the
   bundled public key (`hivemind.pub`). A fork that changed code cannot forge it.
3. **Key anchor** — the bundled public key matches the one published at
   `https://hivemind.projectmentor.org/.well-known/hivemind.pub`, a different origin from the code
   host. Catches a fork that ships its own key and a self-signed manifest.

A healthy install prints `✓ Official HiveMind v2.3 from ProjectMentor — verified.` If you edited
files yourself it says the install was modified locally.

**Exit codes** (a script or CI step can rely on them):

| Code | Meaning |
|---|---|
| `0` | Verified: integrity, signature and key anchor all confirmed |
| `1` | Failed: the install is modified, the signature is invalid, the signing key is not the published one (a fork), or there is no manifest. Do not trust it. (`2` is not used: argparse usage errors and the 2.0 pointers exit 2) |
| `3` | Could not fully check, nothing found wrong: the key anchor was unreachable (offline), or this build predates signed releases. A network outage does not look like tampering |

A fresh checkout still waiting for its release re-sign exits `1`. `hv doctor` is the advisory view of
that state and never reads these codes. `hv doctor` runs the same check as
**authenticity**, and a peer's result is readable at `/api/verify`. Right after an update the signed
manifest can lag for a few minutes (the release bot re-signs after each merge to `main`): `hive-mind
update` waits for the re-sign and pulls it, and `hv doctor` marks a fresh, clean checkout advisory
rather than failed while it is pending (see `hv doctor` above).

---

### `hv version` — Agent contract version

```
hv version        # → hv contract-version 2.3
```

The version of the agent contract (`docs/AGENT_INTEGRATION.md`). Adapters compare it with the
version they last integrated against: a MINOR bump is additive, a MAJOR bump means re-integrate.

---

### Agent-hook verbs: `hv nudge`, `hv audit`, `hv telemetry`

These are called by agent integrations (the Claude Code hooks, the Hermes plugin, the MCP server)
rather than typed by hand. `docs/AGENT_INTEGRATION.md` is the full reference.

```
hv nudge --event session-start|user-prompt|precompact|sessionend [--session ID] [--cwd DIR] [--agent NAME]
hv audit [--depth light|normal|deep] [--session ID] [--format text|json]
hv telemetry record|report|list …
```

- **`hv nudge`** reads recent text on stdin and prints a short hint to stdout, or nothing. With
  `--event session-start` it prints the session-start digest: project context, what the last
  session left to re-check, pending join requests, and up to three open ideas. Tuned per node in
  `nudge.env` (see `config/nudge.env.example`).
- **`hv audit`** lists facts to reconcile: redundant, obsolete, due for a re-check (`volatile` facts
  past their freshness window), missing (with `--session`), and CONTRAVENED (a correction that names
  a still-live fact in prose but never resolved it).
- **`hv telemetry`** records and reports session observability (duration, tokens, cost) in a
  local-only store that never enters the journal and never syncs.

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
| `update` | Pull the latest code and restart the sync daemon. **Auto-heals** after a force-push / history rewrite: when a fast-forward isn't possible and the tree is clean, it hard-resets to the upstream instead of aborting. It then refreshes the supervisor units, restarts the daemon and waits for it to answer, rebuilds the database, and re-wires the Claude Code hooks, in that order. A rebuild that finds the store busy is skipped with a warning rather than aborting the update; the daemon catches the store up on its next cycle. If the pull landed before the release bot's re-sign, it waits up to 3 minutes (`HIVE_RESIGN_WAIT_S`) for it, pulls it, or says it is still pending. |
| `reset` | Recover a **wedged** install in one command: force-align the code to `origin` (even after a rewrite, even with local edits), rebuild the DB from the journal, refresh the supervisor units + Claude Code hooks, restart the daemon, and verify authenticity. **Your Hive (journal, keys, device identity) is preserved** — this is not `uninstall`. Use it when `hv doctor`/`hv verify` is unhappy after a breaking change. `-y` skips the prompt. |
| `status` | Show device health and peer sync state. |
| `invite` | Print the one-line address to paste on a new device so it can join this hive. |
| `module` | *(2.1, Linux)* Install and manage modules: `add <name> --from <repo>`, `remove`, `update`, `list`, `config` *(2.3)*, `quota` (see below). |
| `uninstall` | Remove HiveMind from this device (see flags below). |

### `hive-mind module` — Install and manage modules *(2.1, Linux only)*

```
hive-mind module add <name> --from <repo> [--publisher KEY] [--principal P]
hive-mind module add --abort <name>          # (2.3)
hive-mind module remove <name>
hive-mind module update <name> [--from <repo>] [--publisher KEY]
hive-mind module list
hive-mind module config <name> set <key> <value> | unset <key> | list
hive-mind module quota <name> [--per-hour N] [--per-day N] [--entry-bytes N] [--lifetime N]
```

Owner and operator; there is no `hv module`. `quota` needs the owner key; the other verbs also run on a device without it (below) *(2.3)*. A module is a git repository whose `module.json` is signed by its
publisher key. `add` shows the key and pins it once you confirm (or pass `--publisher` to pin it without a prompt), mints the module's
own device key, has the owner admit it under your principal, and installs its files, config and unit outside the
checkout. `remove` revokes the device and keeps the module's journal entries. `update` re-verifies and swaps
atomically, and refuses a different publisher key. `quota` is the only way a limit goes above the default. Where the
platform has no module backend, `add` says so and exits non-zero before it touches anything. The manifest, the unit and
the limits are in [`MODULE_API.md`](MODULE_API.md). `hive-mind update` re-renders every module's unit afterwards.

**On a device without the owner key** *(2.3)* (the owner key lives on another machine). `add` fetches, verifies, stages and mints
the module's key, then prints the owner's line, `hive-mind group admit <device> --module <name> --principal <p>`, with
the publisher fingerprint and the commit it verified, and **installs nothing**: no unit, no `.modules.json` row, no
admit. The owner checks that commit and runs the line. When the admit has synced to this device, run the same
`add <name> --from <repo>` again: it installs the staged tree, re-checked against the signed manifest and the digest
recorded at the first run. `add --abort <name>` removes the staging and the key; `hv doctor --fix` never completes a pending add. `update` works without
the key (same tree check, same pinned publisher; it admits, revokes and re-limits nothing). `remove` stops the unit and
deletes the tree and the key, then prints the owner's `hive-mind group revoke <device>` line: **the device id stays
admitted until the owner runs it.** `quota` stays owner-only.

`config` *(2.3)* edits the module's per-node config file: `set` and `unset` change one key, `list` prints `key=value`, sorted. It is local to this
node and never journaled, and needs no owner key. An `update` keeps what you set. A manifest's `service.stop_grace` *(2.3)* gives a long
one-shot that many seconds to drain on stop; a stop, disable or restart waits for it, and `remove` deletes nothing while the unit is still draining.

`hv doctor` reports each installed module as `modules:<name>` (a missing, tampered or wrongly signed manifest **fails**;
an absent or stopped unit, a revoked device, missing config, a down listener and quota past 80% **warn**) and
`modules-contract` (a node still on contract 2.0 in a hive that has modules and `quorum_m > 0`). `hive-mind doctor --fix`
re-renders a unit that differs from its manifest and starts one that failed or is not enabled; a unit you stopped on
purpose is reported and left alone.

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

### Help on the installer subcommands

`hive-mind install`, `update`, `status`, `invite`, `reset` and `uninstall` each print their usage and exit 0 on
`-h` or `--help` without doing anything (no pull, no restart, no write). `install`, `update`, `status` and `invite`
take no arguments and refuse any other one with exit 2, before they do anything. Before this fix (#224),
`hive-mind update --help` ran the update.

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
| `HIVE_NODE_ID` | The device-key fingerprint, else the hostname | Overrides this device's identity. Normally a node identifies by its Ed25519 device key (see `hv config identity`); set this only to force an identity, e.g. to run two separate hive instances on one machine. |
| `HIVE_NODE_LABEL` | Your machine's hostname | A human-friendly display label shown next to the `device_id` in `hv stats` and sync logs. Cosmetic; does not affect identity. |
| `HIVE_NOW` | System clock | For testing only — pins the clock to a fixed time so results are predictable. |
| `HIVE_BIND` | automatic | Overrides the sync daemon's listen address (default: this device's Tailscale IP, else `127.0.0.1`). `0.0.0.0` listens on all interfaces and is warned about. |
| `HIVE_SYNC_AUTH` | from `.peers.json`, else `permissive` | Overrides the sync read-auth mode (`off`, `permissive`, `enforce`). |
| `HIVE_SYNC_AUTH_OUTBOUND` | from `.peers.json`, else `permissive` | Overrides the outbound mode, what a peer must prove before this node pushes to it (`off`, `permissive`, `enforce`). |

Advanced, rarely needed:

| Variable | Default | Description |
|---|---|---|
| `HIVE_SYNC_AUTH_WINDOW` | `300` | Seconds of clock skew tolerated on a signed sync request |
| `HIVE_SYNC_PULL_PAGE` / `HIVE_SYNC_PUSH_PAGE` | `25` | Entries per sync request when pulling / pushing |
| `HIVE_SYNC_MAXSEG` | `1000` | TCP segment-size clamp for sync connections, for tailnet paths with MTU below 1280 (`0` disables) |
| `HIVE_OWNER_PASSPHRASE` | — | Supplies the passphrase that encrypts an owner-key export or escrow, non-interactively (automation and tests) |
| `HIVE_OWNER_KEY_PASSPHRASE` | — | Unlocks the owner key sealed at rest (2.0), non-interactively; empty cancels. Anything that can read this variable can owner-sign, so set it only for the command that needs it |
| `HIVE_KEY_DIR` | the path in `HIVE_HOME/.key-dir`, else `~/.hive/keys/<id>` | Where the private keys live, outside the checkout (2.0) |
| `HIVE_IDENTITY_STASH` | `~/.config/hive-mind/identity` | Where the owner key and `uninstall --keep-identity` stash identity files |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Where `hv wire claude` looks for Claude Code's config, honoured exactly as Claude Code does |

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
two. An identity is the device plus `app` and `instance`; agents on the same device
are discounted (`same_device_lambda`).

`context_class` sets how much one identity's write weighs: `primary` 1.0, `subagent` 0.5,
`cron` 0.3. A source with no class (like plain `claude-code` or `manual`) weighs 1.0, and so does
a class HiveMind doesn't recognise (for example a project name in that slot). The label is
self-declared, so it can only lower an agent's own weight — which is the point: it lets background
jobs count for less.

**Examples:**
```
hermes:primary/claude-sonnet/abc12345    # Hermes, main session
hermes:subagent/claude-haiku/xyz99999    # Hermes running a subagent
claude-code                              # Claude Code
manual                                   # You, typing directly
```
