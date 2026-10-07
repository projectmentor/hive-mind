# The module contract (2.1)

What the module API promises, and what it does not. A module author reads this file to know what may be relied on
across releases; [`MODULE_API.md`](MODULE_API.md) is the route reference and
[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md) §7 the adapter-facing version policy. Contract **2.1** is the first to
publish this.

## Stability tiers

Everything a module can reach is marked with one of three tiers.

| Tier | Promise |
|---|---|
| **stable** | Does not change incompatibly inside a contract MAJOR. A change that would is a new path version (`/v2/`), and `/v1/` keeps serving through the deprecation window. Additions are allowed: a new optional field, route or flag is a MINOR. A consumer ignores fields it does not know. |
| **provisional** | Shipped and working, but may change in a MINOR, with a CHANGELOG entry. Build on it knowing it may move. |
| **internal** | Not part of the contract. Do not read it, parse it or depend on its shape. |

### What is stable

- **The path version.** `/v1/…` is versioned apart from the CLI contract. `GET /v1/` lists the versions served in
  `api`. An unserved version is `404` naming the ones that are.
- **The routes and their verbs**: `GET /v1/`, `/v1/feed`, `/v1/search`, `/v1/item`, `/v1/entity`, `/v1/tip` and
  `POST /v1/entries`, with the parameters, the status codes and the fields [`MODULE_API.md`](MODULE_API.md) lists.
- **The authentication**: the Hive-Auth envelope, its signing bytes and the module-device requirement; an unsigned
  request is never served.
- **The entry format a module signs.** Once a module signs its own entry, these must be reproducible in any
  language, so they are stable: the entry's fields (`node_id`, `seq`, `type`, `timestamp`, `payload`, `prev_hash`,
  `pub`, `sig`), the canonical form (`merkle._canonical`), the signing bytes (everything but `sig`, `pub` included),
  the entry hash, the device id (`k1:` and 16 hex of the key's sha256), `prev_hash` and `"sha256:genesis"`.
  `hive_module_client.py` is the reference and imports nothing from `hv`.
- **The feed's fields**: `node_id`, `seq`, `ref`, `sid`, `type`, `timestamp`, `payload`, `sig`, `pub`, the cursor,
  `more`, and the two the core computes, `forgotten` and `affects`.
- **What a module may write**: the types `fact`, `decision`, `idea`, `entity`, `link`; a source of `x-<module>`; the
  channels `sense`, `act`, `introspect`; the naming rule below.
- **The refusals**, by code: `entity-prefix-required`, `not-entity-owner`, `shared-entity-link`,
  `same-as-core-only`, and the status classes of the gate (`400` malformed, `401` unauthenticated, `403` not allowed,
  `409` chain, `413` size, `429` quota).
- **The quota defaults and their meaning**: 60 an hour, 500 a day, 16 KiB an entry, 50,000 for life; a manifest may
  lower and never raise; only the owner raises.
- **Local-only**: the listener binds `127.0.0.1` and refuses any other peer.
- **The hook contract**: the `hooks` manifest field, the six events, `hooks/<event>` with the payload on stdin, output
  discarded, the 3-second per-hook cap and 6-second event budget, and the gates a hook must pass (`MODULE_API.md`).
- **The `x-<module>:` rule** and the **manifest format** (the fields in `MODULE_API.md`; an unknown field is
  refused, so adding one is a MINOR and raises `min_core`).

### What is provisional

- **`GET /v1/` `quota` object's exact keys** beyond `limits`, `used` and `remaining`.
- **The text of `detail` and `error` strings.** The status code and the machine-readable keys (`limit`,
  `retry_after`, `tip`, `supported`, `field`) are stable; the English is not.
- **The doctor check names** a module's operator might alert on, beyond `modules:<name>` and `modules-contract`.

### What is internal

`store.db` and its tables (`journal_index` among them; read the feed, not the projection), `.modules.json`,
`.module-quota.json`, `.peers.json`, local row ids (they change on every rebuild; the API strips them), the sync
daemon's `/sync/*` and `/api/*` routes, the module's key directory layout and the unit file text.

## Deprecation

A **breaking change** to a route needs a new path version (`/v2/`). `/v1/` keeps serving through the contract's own
deprecation window, the one `AGENT_INTEGRATION.md` §7 gives a MAJOR: the previous version keeps working while
adapters migrate, and the CHANGELOG names when it will stop. A route or field marked **provisional** may change without
that window. The module API is an additive 2.x MINOR and a module never bumps the core MAJOR.

A module declares the oldest core it runs on as `min_core` in its manifest. `hive-mind module add` and `update` refuse
a module on a core older than that.

## What a module may and may not write

- **Types.** A module writes `fact`, `decision`, `idea`, `entity` and `link`. It never writes `governance`,
  `capsule`, `cell`, `comb` or `retract`: a retract from a module would be peer evidence in the module's name.
- **Names.** Every bare name core writes or reads (entry types, link kinds, governance actions, config keys,
  channels and the rest) is listed in [`NAMESPACES.md`](NAMESPACES.md). A module may **use** any core link kind, tag
  or channel, and may only **define** a new name under its own `x-<module>:` prefix. A module never adds an entry
  type, a governance action or a channel; it may add a link kind, and a config key only prefixed. A module cannot
  write another module's `x-other:` names. A bare name that is not in the registry is refused, so core can adopt it
  later without reinterpreting a module's old entries.
- **Source.** `x-<module>`. `manual` (a person) and `owner` are refused.
- **Entities (2.1).** Only `x-<its module>:<name>`; it updates one only if it created it. It never links to a shared
  entity, of any link kind. The core joins a module's entity to a shared one with `same-as`, which only a
  non-module device or the owner writes. A device withdraws only the joins it wrote; the owner withdraws any. This
  is a deliberate 2.1 boundary: module entities and shared entities stay apart until the core joins them. A unified
  identity across them is deferred to 3.0 (public #151).
- **No governance.** No route reads or writes governance, owner, escrow, capsule, cell or comb state, and the module
  API imports no owner-key code. A module that signs a `governance` entry and posts it is refused, whatever its
  signature.
- **No votes.** A module device is admitted under the operator's principal, with a module marker on the owner's
  `admit`, and is **excluded from election votes**. Under the operator's principal it falls under `cap_self`, so it
  cannot manufacture independent corroboration of its own claims.

## The guarantees, and their limits

State these as the contract, not more.

1. **Writes go through the core.** A module signs its own entry under its own device key, so it is attributable and
   individually revocable, and the core verifies, gates and appends it. The core never holds a module's seed.
2. **Local-only.** The module API listens on loopback. 2.1 has **no remote opt-in**: a module on another host is
   not supported, and no design for one ships in 2.1.
3. **Signed requests.** Every request is signed and replay-checked; `sync_auth_mode` does not weaken it.
4. **Quotas are a limit on a chatty module, not a hostile one.** A module is a separate process under the operator's
   OS user, holding its own device key. It could append a signed line to the journal directly, and ingest on other
   nodes cannot know a quota. So quotas protect against a buggy or over-eager module, not against one with the
   operator's file access. **A hostile module is revoked** (`hive-mind group revoke` or `hive-mind module remove`);
   its entries stay in the journal and stop counting. A **compromised** module device is `hive-mind group purge`d:
   none of its content counts, in the journal or on any node. An honest one that is being replaced is `hive-mind
   group retire`d: the same tombstone, but the owner signs the seq and hash of its last good entry, and the chain up to
   it keeps counting while later entries do not.
5. **Timestamps are the writer's, within bounds.** Once the owner arms the bounds (`hive-mind owner
   freeze-timestamps`), an entry more than 5 minutes before its device's latest earlier entry, or before the
   device's first admit, is skipped by every node. A module never stamps earlier than its own last entry.
6. **A module's hooks and service run code outside the signed source.** They are verified at install and by
   `hv doctor`, and not at every event.
7. **Linux only in 2.1.** On any other platform `hive-mind module add` refuses and exits non-zero before it
   fetches, mints or installs anything. macOS and Termux follow in a later release.
8. **Election skew on a mixed fleet.** The module marker on `admit` is read by 2.1 nodes. A 2.0 node ignores it and
   still counts a module device's vote. This matters only on a hive with `quorum_m > 0` and `quorum_by=device`
   (the default is `quorum_m = 0`, elections off; under `quorum_by=principal` a module is already in the operator's
   unit on 2.0). `hv doctor` warns (`modules-contract`) about any node still on 2.0 in such a hive.
9. **A module device is not a node.** It has no sync address, so a 2.0 node's `fleet-contract` check lists it as
   unreachable for as long as it is admitted; 2.1 nodes do not. This noise on 2.0 nodes is accepted skew.

## `hv feed`: the consumer's obligations

The feed carries what the core computes. A consumer **must not** compute its own forget state.

- **`forgotten`** is on every `fact` entry. Hide or withdraw every ref with `forgotten: true`. **Every other type
  omits the field: a missing flag means "this type cannot be forgotten", not "the owner did not forget it".**
- **`affects`** is on every owner `retract` (and its undo). Re-read `forgotten` for each ref it lists that you have
  already shown.
- **Order.** Entries come in `(node_id, seq)` order and sync can deliver a retract before its target. Handle both.
- **Never republish forgotten text**, including in a summary, a cache or a downstream write.
- **Raw entries stay.** The feed returns the original entry even when it is forgotten, as sync does for any admitted
  device. `forgotten` and `affects` are the instruction to the consumer, not an erasure.
- **`sig` and `pub` pass through.** An entry with no `sig` is *attributed, not authenticated*: its presence in the
  feed is not a device's signed claim.
- **The cursor** is a set of `(node_id, seq)` pairs, the journal's own identity. Local ids are renumbered and a synced
  entry keeps its author's timestamp, so neither is a safe cursor.

## Provisional

The doctor's `modules:<name>` hooks check (the re-verification of an installed module's `hooks/<event>` files) has
shipped, but its check name and message text are provisional. The `hooks` manifest field, the six events and the
dispatcher's caps and gates (`MODULE_API.md`, *Hooks and events*) are stable.

## Deferred

Out of scope for 2.1, and not designed here: a remote opt-in for the module API; module support on macOS and
Termux; the `AGENTS.md` cell (2.2); module-added lines in the session digest, which land with the module that adds
them; a unified entity identity across module and shared entities (3.0, public #151).

## Versioning

The contract is `MAJOR.MINOR` (`hv version`). 2.1 is a MINOR: additive for the adapter surface. `hv feed` is a new
verb, `hive-mind module` a new control-plane verb, `/v1` a new surface, and the hook events additive. No verb, flag
or output an adapter calls changes. The one change to existing behaviour is the election projection, which §7 of
`AGENT_INTEGRATION.md` already allows a MINOR to make to how the hive itself governs. The release tag `v2.1.0` marks
the commit that completed this contract.
