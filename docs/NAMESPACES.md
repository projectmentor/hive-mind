# Namespaces: the names core reserves

Generated from [`vocabulary.py`](../vocabulary.py) by `scripts/common/gen_namespaces.py`. Do not edit this
page by hand: change the registry and run the generator. A test fails when the two differ, and another fails
when the code writes or reads a name the registry does not list, or the registry lists a name no code uses.

## Why

A journal entry whose type, link kind or governance action a node does not recognise still lands, and
projects to nothing. That is how an older node stays converged with a newer one. The journal is permanent,
so the rule has a cost: if a module wrote a name that a later core feature then reused, every entry the
module had written would be read with the new meaning, on every node, from then on.

## The rule

- **Core reserves every bare name on this page**, in its category.
- **A module's names take a prefix: `x-<module>:<name>`**, for example `x-hwatch:heartbeat` (decision
  `h:af137f9421`). No core name starts with `x-`.
- **What a module may add** (decision `h:a1e3e7cd73`): a link kind or a config key, prefixed. Never an entry
  type, a governance action or a channel: each needs a core projection, and channels are closed. Each table
  below says which, under *Modules*.
- **The 2.1 module API enforces the prefix.** 2.0 reserves the names and documents them.
- **Envelope fields are the entry's structure**, not vocabulary a module picks. A module never writes its
  own: the core builds the envelope around a module's payload.

## Reading the tables

- `written`: core writes the name today.
- `legacy`: core no longer writes it, and still projects the entries that carry it.
- `read`: core reads and honours it, and never writes it itself; agents and people do.
- `reserved`: no core code uses it yet. It is held for the 2.1 module envelope.

*Since* is the contract version that introduced the name. It is a field of the registry, filled in from the
changelog and the contract history as a best effort, and never derived from git.

## Entry types

The `type` of a journal entry.

*Modules:* No. A new entry type needs a core projection.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `capsule` | written | 1.13 | A secret sealed to the hive's devices; the latest version per name wins, and a tombstone ends it. |
| `cell` | written | 1.13 | An executable definition (a tool or an agent integration) that `hv wire` runs; the latest version per name wins. |
| `comb` | written | 1.13 | A named, ordered collection of cells. |
| `decision` | written | 1.0 | A decision and its rationale. |
| `entity` | written | 1.0 | A named entity (a person, project or system) and its attributes. |
| `entity_fact` | legacy | 1.0 | The old entity-to-fact link. Not written since 1.20, when an `entity` link replaced it; still projected. |
| `fact` | written | 1.0 | An assertion; its confidence is derived from independent corroboration, never asserted. |
| `governance` | written | 1.4 | A governance act; its `action` says which one (see Governance actions). |
| `idea` | written | 1.20 | A hypothesis; its confidence is earned from other identities' links, never asserted. |
| `link` | written | 1.19 | A typed edge between two entries; its `kind` says which relationship (see Link kinds). |
| `retract` | written | 1.0 | Negative evidence against a fact. From the owner it is a forget, or, carrying `unretracts_ref`, an unforget. |

## Link kinds

The `kind` of a `link` entry. An unknown kind lands and projects to nothing.

*Modules:* Yes, prefixed. It lands, and projects to nothing on a node without a resolver for it.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `contradicts` | written | 1.19 | Evidence against a fact or an idea. |
| `entity` | written | 1.19 | Links an entity to a fact. |
| `extends` | written | 1.22 | Builds on a fact, idea or decision. An edge only, never evidence on any channel. |
| `informed` | written | 1.19 | A decision relied on this entry; it feeds the entry's utility. |
| `outcome-of` | written | 1.19 | This fact is an outcome of a decision; it feeds the decision's outcome score, on the `sense` channel only. |
| `resolves` | written | 1.19 | This fact corrects another: negative evidence on the target, and provenance when the link is hard. |
| `same-as` | written | 2.1 | Core only. Joins a module's `x-<module>:` entity (`from_ref`) to an unprefixed entity (`to_ref`); `entity show` lists the joined entity's facts. A module's is refused, and a `retract` naming it withdraws it. |
| `supersedes` | written | 1.19 | This decision replaces another, when the link is hard. |
| `supports` | written | 1.19 | Evidence for a fact or an idea. |

## Governance actions

The `action` of a `governance` entry.

*Modules:* No. An action needs the core governance projection.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `admit` | written | 1.4 | Admit a device to the corroboration set, optionally tagging its principal. Owner-signed. |
| `announce` | written | 1.18 | An authority-less, device-signed announcement, discriminated by `kind` (see Announce kinds). The projection ignores it. |
| `change` | written | 1.5 | Re-tag an admitted device's principal. Owner-signed. |
| `claim-succession` | written | 1.7 | A nominated successor takes ownership, signed by the new owner key. |
| `deny` | written | 1.5 | Drop a pending join-request; a later admit overrides it. Owner-signed. |
| `freeze-timestamps` | written | 2.1 | Arms the bounded entry timestamps: `tips` maps each device to the seq its chain is frozen at, and only later entries are checked. Owner-signed. |
| `heartbeat` | written | 1.9 | Owner liveness: an owner-signed act with no other effect, which keeps the dead-man switch shut. |
| `join-request` | written | 1.4 | A device asks to be admitted. Device-signed; it carries no authority. |
| `nominate-successor` | written | 1.7 | Nominate a successor owner key. Owner-signed. |
| `owner` | written | 1.4 | The genesis declaration, which establishes the hive and its owner. Self-signed. |
| `owner-escrow` | written | 1.6 | A passphrase-sealed copy of the owner key, kept in the hive. Owner-signed. |
| `propose-election` | written | 1.9 | An admitted device proposes a new owner, installed only past `dead_man_days` of owner silence. Device-signed. |
| `purge` | written | 1.5 | Tombstone a device, permanently. Owner-signed. |
| `revoke` | written | 1.5 | Un-admit a device; a later admit restores it. Owner-signed. |
| `revoke-escrow` | written | 1.7 | Tombstone an owner escrow, so a restore skips it. Owner-signed. |
| `revoke-nomination` | written | 1.7 | Withdraw an open successor nomination. Owner-signed. |
| `set-config` | written | 1.4 | Set a governed parameter (see Config keys), the same on every node. Owner-signed. |
| `standby` | written | 1.6 | Advisory: a device the owner sanctions to also hold the owner key. Owner-signed. |
| `transfer` | written | 1.7 | Hand ownership to a new key at once. Owner-signed. |
| `vote-election` | written | 1.9 | An admitted device endorses an open election. Device-signed. |

## Config keys

The keys a `set-config` act may set. An older node ignores a key it does not know.

*Modules:* Yes, prefixed. `x-<module>:<key>` holds a string the core stores and never interprets. An older node ignores a key it does not know.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `cap_self` | written | 1.4 | The confidence ceiling when all corroboration traces to one principal. |
| `capsule_putters` | written | 1.13 | Who may write capsules: `owner` (the default) or `fertile` (any admitted device). |
| `cell_writers` | written | 1.17 | Who may write cells and combs: `owner` (the default) or `fertile`. |
| `dead_man_days` | written | 1.9 | Days of owner silence before a quorum election can install a new owner. |
| `flood_per_day` | written | 2.1 | Entries one device may stamp into a day before `hv doctor` reports a flood; 2000 by default. Detection only. |
| `flood_per_minute` | written | 2.1 | Entries one device may stamp into a minute before `hv doctor` reports a flood; 120 by default. Detection only. |
| `forget_writers` | written | 1.25 | Whether an unsigned owner forget dated before genesis still counts: `legacy` or `owner`. |
| `halflife_fact` | written | 1.20 | Half-life, in days, of a fact's confidence and importance. |
| `halflife_idea` | written | 1.20 | Half-life, in days, of an idea's confidence and importance. |
| `halflife_volatile` | written | 1.20 | Half-life, in days, of a `volatile`-tagged fact. |
| `importance_self_cap` | written | 1.20 | The most a writer's own `--importance` can claim. |
| `introspect_support_weight` | written | 1.19 | The weight of `introspect`-channel evidence; 0, the default, means reasoning never moves confidence. |
| `quorum_by` | written | 1.9 | What a quorum counts: `device` or `principal`. |
| `quorum_m` | written | 1.9 | Voter units needed to elect a new owner; 0 turns elections off. |
| `same_device_lambda` | written | 1.4 | The weight of each additional identity on the same device. |
| `trust_drift_threshold` | written | 1.20 | The reliability change below which `hv doctor` reports trust drift. |
| `trust_long_days` | written | 1.20 | The long window, in days, of per-device reliability. |
| `trust_short_days` | written | 1.20 | The short window, in days, of per-device reliability. |
| `w_links` | written | 1.20 | The weight of other identities' links in importance. |
| `w_volatile` | written | 1.20 | The importance a `volatile` fact gains while it is fresh. |

## Channels

An entry's `channel`. Closed: a module never adds one, because an unrecognised channel counts as `introspect`.

*Modules:* No. Closed: an unrecognised channel counts as `introspect`.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `act` | written | 1.19 | Something the writer did. Weighs like `sense`, except that a decision's outcome counts `sense` only. |
| `introspect` | written | 1.19 | Reasoning, not observation: it weighs `introspect_support_weight`. An unrecognised channel counts as this. |
| `sense` | written | 1.19 | An observation. An entry with no channel is `sense`. |

## Behaviour tags

Tags core writes or acts on. A name ending in `*` is a prefix.

*Modules:* Only prefixed, like every module name.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `durable` | written | 1.2 | Records an opt-out of the `volatile` auto-tag, so the audit's detection honours it. |
| `revocation` | written | 1.24 | Marks a decision written by `decide --revoke`; no projection reads it. |
| `ttl:*` | read | 1.2 | `ttl:<n>h` or `ttl:<n>d`: the audit treats the fact as volatile, with this freshness window. |
| `volatile` | written | 1.2 | Transient status: the audit re-checks it past its freshness window, and it decays under `halflife_volatile`. |

## Announce kinds

The `kind` of a governance `announce`. An unknown kind is accepted and ignored.

*Modules:* Only prefixed. A node accepts an unknown kind and ignores it.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `key` | written | 1.18 | Publishes the device's key, so capsules can be sealed to a device that has never written anything. |

## Source apps

The `<app>` of a source, `<app>:<context_class>/<instance>/<session8>`.

*Modules:* Only prefixed, like every module name.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `manual` | written | 1.0 | A person, typing; the source of a write that names none. Only a `manual` link is owner-signed through `hive-mind`. |
| `owner` | written | 1.4 | Owner governance: a retract from this app is a forget, not evidence. |

## Source context classes

The `<context_class>` of a source. An unrecognised class weighs 1.0.

*Modules:* Only prefixed, like every module name. An unrecognised class weighs 1.0.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `cron` | read | 1.0 | A scheduled job; weighs 0.3. |
| `owner` | written | 1.4 | Owner governance in the class slot: a retract from it is a forget. Core writes `owner:owner/owner`. |
| `primary` | read | 1.0 | A main agent session; weighs 1.0, as does a source with no class. |
| `subagent` | read | 1.0 | A delegated task; weighs 0.5. |

## Envelope fields

The fields an entry is made of. A module never writes its own: the core builds the envelope around a module's payload.

*Modules:* No. The core builds the envelope around a module's payload.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `action` | written | 1.4 | Which act a `governance` payload records. |
| `from` | reserved | 2.0 | Reserved for the module envelope: the ref an edge starts from. A module's own link kind carries it as a pair or an `h:` short id, shape-checked as an `h:` id and resolved when a projection first reads a module link kind; a core kind carries `from_ref`. |
| `id` | reserved | 2.0 | Reserved for the module envelope: an entry's identity, `node_id:seq` or its `h:` short id. It names the entry; it is not a field a module writes. |
| `kind` | written | 1.13 | The discriminator inside a typed payload: a link's relationship, an announce's kind, a cell's or capsule's kind. |
| `node_id` | written | 1.0 | The authoring device; since 1.3, `k1:` and 16 hex of its key's sha256. |
| `payload` | written | 1.0 | The type-specific body. |
| `prev_hash` | written | 1.0 | The hash of the device's previous entry: its hash chain. |
| `pub` | written | 1.3 | The signer's Ed25519 public key. |
| `seq` | written | 1.0 | The device's sequence number; `(node_id, seq)` identifies an entry. |
| `sig` | written | 1.3 | The device's Ed25519 signature over the entry without `sig`. |
| `target` | reserved | 2.0 | Reserved for the module envelope: the ref an act is about, resolved like `retracts_ref` or `to_ref`. |
| `timestamp` | written | 1.0 | When the entry was written (ISO 8601); it also names the journal's day file. |
| `to` | reserved | 2.0 | Reserved for the module envelope: the ref an edge points to. A module's own link kind carries it as a pair or an `h:` short id, shape-checked as an `h:` id and resolved when a projection first reads a module link kind; a core kind carries `to_ref`. |
| `type` | written | 1.0 | The entry type (see Entry types). |

## Local files

Not journal vocabulary. These are the per-node files and directories core keeps under `$HIVE_HOME` (the
checkout) and in the key directory (`$HIVE_KEY_DIR`, else the path in `$HIVE_HOME/.key-dir`, else
`~/.hive/keys/<16 hex>`). None of them enters the journal or the wire. A module never claims one of these
names. They are listed apart from the tables above so that a file name is never mistaken for vocabulary.
Here `legacy` means core no longer writes the file at that path and still reads it.

| Name | Where | Status | Since | Meaning |
|---|---|---|---|---|
| `.arrivals.jsonl` | `$HIVE_HOME` | written | 2.1 | When this node first received each foreign entry, for the `future-dated` check; never synced, never read by the projection. |
| `.bus` | `$HIVE_HOME` | written | 1.10 | Directory for the local event log, `introspect.log`; never synced. |
| `.csrf-token` | `$HIVE_HOME` | written | 2.0 | The secret a local client sends as `Hive-CSRF` on a POST to this node's loopback daemon (0600, never synced). |
| `.device-id` | `$HIVE_HOME` | written | 1.3 | This device's id, `k1:` and 16 hex of its key's sha256. |
| `.device-key` | `$HIVE_HOME` | legacy | 1.3 | The device key's pre-2.0 path in the checkout. It still loads, with a `keyperm` warning, and `hv doctor --fix` moves it. |
| `.genesis-pin` | `$HIVE_HOME` | written | 1.27 | Which `owner` declaration established this hive (0600, never synced). |
| `.journal.lock` | `$HIVE_HOME` | written | 2.2 | The lock a local write holds from reading this device's tip to appending the entry built on it, so two processes never mint one seq; empty, local. |
| `.key-dir` | `$HIVE_HOME` | written | 2.0 | The path of this checkout's key directory, so a renamed checkout keeps its keys. |
| `.module-quota.json` | `$HIVE_HOME` | written | 2.1 | Each module device's hourly and daily write times, for the module API's rate limits (0600, never journaled). The lifetime count is the journal's, not this file's. |
| `.modules.json` | `$HIVE_HOME` | written | 2.1 | The modules installed on this node: version, pinned publisher key, device id, source and quota limits (0600, never journaled). Written by `hive-mind module`. |
| `.nudge_state` | `$HIVE_HOME` | written | 1.0 | When the save and audit nudges last fired. |
| `.owner-key` | `$HIVE_HOME` | legacy | 1.4 | The owner key's pre-2.0 path in the checkout. `hive-mind doctor --fix` moves it. |
| `.owner-pub` | `$HIVE_HOME` | legacy | 2.0 | The owner key's public half at its first 2.0 path. `hive-mind doctor --fix` moves it. |
| `.peer_candidates.json` | `$HIVE_HOME` | written | 1.21 | The addresses each admitted device verified itself from; local, never journaled (see `via` values). |
| `.peers.json` | `$HIVE_HOME` | written | 1.0 | This node's sync settings and its peers' addresses. |
| `.quarantine.idx` | `$HIVE_HOME` | written | 2.0 | One `hash size` line per quarantine record: the dedupe key and the byte count, so a refusal appends without reading the quarantine; local. |
| `.quarantine.jsonl` | `$HIVE_HOME` | written | 2.0 | The entries ingest refused for a malformed or hostile payload, verbatim, with the reason, the signer and the time (an entry over 64 KB as its hash, size and first 4 KB); only admitted signers, the newest 200 records within 16 MB, local, never synced (`hv doctor` reports it). |
| `.quarantine.lock` | `$HIVE_HOME` | written | 2.0 | The lock the quarantine writers take; empty, local. |
| `.succession-pin` | `$HIVE_HOME` | written | 2.3 | The owner-succession acts this node accepted, pinned the way the genesis is (0600, never synced). |
| `.telemetry` | `$HIVE_HOME` | written | 1.1 | Directory for the local-only session telemetry store; never synced. |
| `journal` | `$HIVE_HOME` | written | 1.0 | The journal's day files: the source of truth, and all that sync carries. |
| `nudge.env` | `$HIVE_HOME` | read | 1.0 | Optional nudge settings (`KEY=value`), read here or at the repository root. |
| `store.db` | `$HIVE_HOME` | written | 1.0 | The SQLite index derived from the journal; `hv doctor rebuild` recreates it. |
| `device-key` | key directory | written | 2.0 | This device's Ed25519 seed (0600). Not sealed: every entry is device-signed unattended. |
| `owner-key` | key directory | legacy | 2.0 | An unsealed owner seed. It still loads, `hv doctor` fails until `hive-mind owner seal` seals it, and that removes it. |
| `owner-key.sealed` | key directory | written | 2.0 | The owner seed, sealed at rest: scrypt, then ChaCha20-Poly1305. |
| `owner-pub` | key directory | written | 2.0 | The owner key's public half (0644), so `hv` can tell whose key is here without opening it. |

### `via` values

How a `.peer_candidates.json` sighting was verified. Local, never journaled. A sighting with no `via` came
from a signed inbound request to the daemon.

| Name | Status | Since | Meaning |
|---|---|---|---|
| `outbound` | written | 1.26 | The sync client saw the device answer a signed `/sync/hello` at this address (#107). |
