# Module API reference (`/v1`)

The route reference for the module API, contract **2.1**. What each route and field *promises* (stable, provisional
or internal), the deprecation window and the limits of the guarantees are in [`CONTRACT.md`](CONTRACT.md); this file
is what the routes do. The manifest format and the `hive-mind module` verbs are at the end.

A **module** is a separate process on the same machine as the sync daemon. It has its own Ed25519 device key, the
owner admits it as a module device under the operator's principal, and it reaches the hive only through this API,
never through the owner key. A module reads the hive, and writes `fact`, `decision`, `idea`, `entity` and `link`
entries that it **signs itself**; the core verifies, gates and appends them.

Hive-mind ships a reference signer and client, `hive_module_client.py`, readable top to bottom and importing nothing
from `hv`. A module in another language reimplements its few lines (see [Signing](#signing)).

## The listener

| | |
|---|---|
| Address | `127.0.0.1`, port `9886` (`HIVE_MODULE_PORT` overrides). Loopback only, whatever `.peers.json` says; a peer that is not loopback is `403`. There is no remote opt-in in 2.1. |
| Process | The sync daemon's process, a listener of its own. A taken port is reported and the daemon runs without it; sync never waits on it. |
| Concurrency | At most 8 requests in flight on this listener (excess is `503`), apart from the sync daemon's own cap. |
| Verbs | `GET` and `POST` only. Any other verb is a JSON `405` with `Allow: GET, POST`, before authentication. |
| Bodies | JSON, always, including every error. |

## Authentication

Every request is signed with the Hive-Auth envelope the sync daemon uses between peers: an Ed25519 signature over
the algorithm, method, path, canonical query, body hash, timestamp and nonce, sent in the `Hive-Auth-*` headers
(`Alg`, `Device`, `Pub`, `Ts`, `Nonce`, `Sig`). The signature, the timestamp window, the nonce (a replay is
refused) and the device's admission are checked the way a peer's are. On top of that the device must be a **module
device**: one the owner admitted with the module marker (`admit --module`, which `hive-mind module add` does).

Unlike the sync daemon, this listener **ignores `sync_auth_mode`**: an unsigned request is always `401`.

| Answer | When |
|---|---|
| `401` | Unsigned, stale, replayed, wrong path or wrong body. `detail` names why. Authentication runs before the route lookup, so an unsigned request to *any* path, an unknown one included, is `401`. |
| `403` | Signed by a device that is not admitted (`not-admitted`), is admitted but not a module device (`not-a-module-device`), or the hive has no owner yet (`no-owner`). |

## Routes

The route table is exactly this set. A route not here does not exist, and no route reads or writes governance,
owner, escrow, capsule, cell or comb state.

| Route | Verb | Does |
|---|---|---|
| `/v1/` | GET | The version, the hive, the caller, its quota and its fleet config |
| `/v1/feed` | GET | The journal past a per-node cursor, the same result as `hv feed` |
| `/v1/search` | GET | Search over the projection |
| `/v1/item` | GET | One item by `h:` id or `node_id:seq` |
| `/v1/entity` | GET | An entity, its facts and the module entities a core `same-as` joins to it |
| `/v1/tip` | GET | The caller's own chain tip, to build the next entry |
| `/v1/entries` | POST | Append one entry the module signed |

A signed `GET` on a `POST` route, or the reverse, is `405` with `Allow`. An unknown path is `404 {"error": "not found"}`.
A path under a version this node does not serve (`/v2/…`) is `404` naming the versions it does:
`{"error": "unsupported API version 'v2'", "supported": ["v1"]}`.

### `GET /v1/`

```json
{"api": ["v1"], "contract": "2.1", "hive_id": "…", "device_id": "k1:…", "module": "hwatch",
 "quota": {"limits": {…}, "used": {…}, "remaining": {…}}, "config": {"x-hwatch:poll": "30"}}
```

`api` lists the path versions served. `module` is the caller's module name. `config` is the caller's own
`x-<module>:` fleet config, the keys the owner set with a signed `set-config`, as strings. It is a convenience, not
a confidentiality boundary: the journal is shared by design and `/v1/feed` carries all of it. `quota` is the same
object [Quotas](#quotas-and-rate-limits) describes.

### `GET /v1/feed?after=<node_id:seq,…>&limit=<n>`

`hv feed`: for each node, the journal entries whose `seq` is past the caller's cursor for that node. The cursor is
a comma-separated set of `node_id:seq` pairs, which is the journal's own identity; a node id holds a `:`, so each
pair splits at the last one. An empty `after` starts at the beginning. `limit` is 1 to 1000, default 200. A
malformed cursor is `400`, never a silent restart from the start.

```json
{"entries": [{"node_id": "k1:…", "seq": 41, "ref": "k1:…:41", "sid": "h:…", "type": "fact",
              "timestamp": "…", "payload": {…}, "sig": "…", "pub": "…", "forgotten": false}],
 "cursor": "k1:…:41,k1:…:7", "more": false}
```

- Entries are ordered by `(node_id, seq)`. `cursor` is the cursor to send next; `more` is true when `limit` cut the
  page short.
- It reads the journal files, not `store.db`. An entry whose signature fails to verify is absent.
- `sig` and `pub` pass through. An entry with **no** `sig` is *attributed, not authenticated*: its presence is not a
  device's signed claim.
- **`forgotten`** is on every `fact` entry, `true` or `false`, computed by the core: last act wins, at content
  level, so two facts with the same text are both `true` once one is forgotten. **Every other type omits it**: a
  missing flag means "this type cannot be forgotten", not "the owner did not forget it".
- **`affects`** is on every owner `retract` entry (and the entry that undoes one): the refs of the held `fact`
  entries that share the act's target content, when the act is honoured. It is empty when the act is not honoured
  or its target is not a held fact. It does not depend on later acts: a forget followed by an unforget lists the
  same refs on both. A retract that is not an owner's carries no `affects`.
- `forgotten` and `affects` are **authoritative**. A consumer must not compute its own forget state; it hides or
  withdraws any ref with `forgotten: true`, handles a retract arriving before or after its target (the feed is
  ordered by `(node_id, seq)` and sync can deliver either first), and never republishes forgotten text.

### `GET /v1/search`

`q`, `tag`, `kind` (`all`, `fact`, `decision`, `idea`), `min_confidence` (a finite number ≥ 0), `limit` (1–200,
default 50), `offset`, `sort` (`confidence`, `recency`, `importance`, `utility`), `status` (`all`, `contested`,
`volatile`). The result is `hv search --format json`'s. Anything the owner forgot is withheld in every branch:
`status=forgotten` is refused with `400`. A bad enumerated value is `400` naming the allowed ones.

### `GET /v1/item?id=<h:… or node_id:seq>`

`api_item`'s answer. `404` for anything that does not resolve (`h:nope`, a local id), and for a forgotten fact.
A missing `id` is `400`.

### `GET /v1/entity?name=<entity name>`

The view `hv entity show --name` prints: the entity, its linked facts (forgotten ones left out) and, under `joined`,
each `x-<module>:` entity a core `same-as` joins to it, with the join's `ref`, its `signer` and that entity's own
facts. Only a live join is listed (see [Joins](#entity-joins)). Local row ids are removed from the answer, because
they change on every rebuild. `404` when there is no such entity, `400` without `name`.

### `GET /v1/tip`

`{"node_id": "k1:…", "seq": 12, "hash": "sha256:…"}`: the caller's own tip, from the journal. Before the module's
first write it is `seq` 0 and `"sha256:genesis"`. The next entry is `seq + 1` with `prev_hash` set to `hash`. The
read holds no lock.

### `POST /v1/entries`

The body is one **signed entry**, exactly these fields and no others:

```json
{"node_id": "k1:…", "seq": 13, "type": "fact", "timestamp": "2026-10-05T12:00:00.000+00:00",
 "payload": {"content": "…", "source": "x-hwatch"}, "prev_hash": "sha256:…", "pub": "<b64>", "sig": "<b64>"}
```

The gate, in order. Every refusal is a 4xx and writes nothing.

| Check | Refusal |
|---|---|
| Body larger than the module's `entry_bytes` | `413` |
| Not JSON; not an object; a field missing or unknown; a malformed `seq`, `payload` or string field; `timestamp` not ISO 8601; the signature does not verify | `400` |
| `node_id` is not the device that sent it | `403` |
| `type` outside `fact`, `decision`, `idea`, `entity`, `link` (so never `governance`, `capsule`, `cell`, `comb` or `retract`) | `403` |
| `payload.source` absent, not `x-<module>` of this module (`manual` and `owner` are refused), or a name the module may not introduce | `400` or `403` |
| `payload.channel` other than `sense`, `act`, `introspect` | `400` (refused, not coerced) |
| A tag, a link `kind`, a source app or class that is neither a core name nor `x-<this module>:…` | `403` |
| A legacy reference field (`supersedes_ref`, `supersedes`, `resolves_ref`, `entity_ref`, `fact_ref`, `entity_id`, `fact_id`); a bound field of the wrong type | `400` |
| A `fact`, `decision` or `idea` with no non-empty string `payload.content`; an `entity` with no `payload.name` | `400` |
| A reference field that is not the shape its row in `REF_FIELDS` says | `400` |
| The core's own projection raises on the payload (a dry run, rolled back) | `400` |
| The chain: a different entry already held at this `seq`; a `seq` or `prev_hash` that does not extend the tip | `409`, with the tip |
| Over quota | `429`, with `Retry-After` when waiting helps |
| The core would not append it (admission, signature, timestamp) | `403` |

An accepted write answers `200`:

```json
{"accepted": 1, "duplicate": false, "ref": "k1:…:13", "sid": "h:…", "tip": {"node_id": "k1:…", "seq": 13, "hash": "sha256:…"}}
```

**Idempotent and conflict-safe.** The check and the append happen inside one lock section. The *same* bytes at a
held `seq` (a retry) are `200` with `{"accepted": 0, "duplicate": true, …}` and no second write. A *different* body at
a held `seq`, or an entry on a stale tip, is `409` carrying the current tip, so the client rebuilds on the tip and
tries again. A retry built on a fresh tip is a new entry and lands as a second copy.

**Names.** A module may *use* any core link kind, tag or channel, and may only *define* a new name under its own
`x-<module>:` prefix. A bare name that is not in the registry ([`NAMESPACES.md`](NAMESPACES.md)) is refused, so core
can adopt it later without reinterpreting a module's old entries.

**Entities and links (2.1).**
- A module writes only entities named `x-<its module>:<name>` (`403 entity-prefix-required`), and updates one only
  if its own device created it (`403 not-entity-owner`).
- A module never links to a shared entity, with any link kind (`403 shared-entity-link`). It links its own
  `x-<module>:` entities, and the core joins them to a shared one (see [Joins](#entity-joins)).
- A module's `same-as` is refused (`403 same-as-core-only`).
- A core link kind carries `from_ref` and `to_ref`, each a `[node_id, seq]` pair. A module's own kind carries the
  envelope's `from` and `to`, each a pair or an `h:` short id. An end that resolves to nothing yet is allowed: the
  target may sync later, and a projection skips a dangling edge the same way on every node.

**Timestamps.** The entry's timestamp is signed, so the module chooses it, inside the bounds the projection holds
every device to once the owner has armed them (see [Bounded timestamps](#bounded-timestamps)). A module reads the
clock fresh, never earlier than its previous entry.

#### Signing

An entry is a JSON object. Both signatures and the hash use the same canonical form.

```
canonical(obj)  = JSON with sorted keys and separators "," and ":", non-ASCII escaped as \uXXXX (Python's
                  json.dumps default), as bytes: merkle._canonical
device id       = "k1:" + the first 16 hex of sha256(raw public key)
signature       = Ed25519 over canonical(entry without "sig")      (so "pub" is covered)
entry hash      = "sha256:" + hex sha256(canonical(entry))          ("sig" included)
prev_hash       = the previous entry's entry hash, or "sha256:genesis" for seq 1
```

A request is signed the same way over `sync_common.sync_signing_bytes` (algorithm, method, path, canonical query,
body hash, timestamp, nonce); `hive_module_client.py` is the reference for both.

## Quotas and rate limits

Journal entries are permanent, so the budget is on entries and bytes, not only requests.

| Limit | Default | Counted from |
|---|---|---|
| `per_hour` | 60 | `$HIVE_HOME/.module-quota.json` (local, 0600, never journaled) |
| `per_day` | 500 | the same file |
| `entry_bytes` | 16 KiB (16384) | the request body |
| `lifetime` | 50,000 entries | the **journal**, filtered by the module's node id |

Only `per_hour` and `per_day` live in the side file. A deleted file, a rebuild or a daemon restart can loosen a
window of at most a day, and can never reset `lifetime`, which is counted from the permanent journal. Two modules
never share a bucket.

A module's manifest may ask for less than a default and never more. Only the owner raises or lowers a limit, with
`hive-mind module quota <name> --per-hour N …`. Over quota is `429` and nothing is written:

```json
{"error": "over quota: per_hour", "limit": "per_hour", "quota": {…}, "retry_after": 1200}
```

`Retry-After` is sent when waiting lifts the limit; a ceiling no wait lifts (`lifetime`, or a limit of 0) has none.
`GET /v1/` carries the current `limits`, `used` and `remaining`.

**What a quota is not.** It stops a buggy or chatty module, not a hostile one. A module is a process under the
operator's OS user holding its own device key, so it could append a signed line to the journal without this API,
and no other node can know a quota. A hostile module is **revoked** (`hive-mind group revoke`, or
`hive-mind module remove`), which is why each module has a key of its own.

## Entity joins

A module's entity (`x-<module>:<name>`) and a shared one are different entities until a **core** `same-as` joins them.

- `hv entity join --name x-<module>:<n> --to <n>` writes a `same-as` link from a non-module device.
- `hv entity unjoin --name x-<module>:<n> --to <n>` withdraws **only the joins this device wrote**, one `retract` per
  join entry, so each join is judged on its own. Another device's join stays live. It exits 1 when this device wrote
  no live join of that pair.
- `hive-mind entity unjoin … --owner` is the owner's withdrawal of **every** live join of the pair, owner-signed.
- A join is live from its entry until a later honoured withdrawal *of that entry*; a later `same-as` joins again.
  Joins and withdrawals are judged together in the journal's `(timestamp, node_id, seq)` order, the same on every
  node. A withdrawal is honoured when it is signed by the join entry's own device, or by the owner as of its
  position.
- `hv entity show --name <n>` lists the joined entities and their facts; `--no-joins` prints the core entity only.
  `GET /v1/entity` returns the same view.

## Bounded timestamps

Journal order is `(timestamp, node_id, seq)` and the writer picks the timestamp, so two rules bound it per device.
They apply at ingest and in the projection, so every node agrees:

1. **No earlier than the device's own chain.** A timestamp is no earlier than the latest timestamp of the device's
   lower `seq` entries, less the **5-minute tolerance**. The tolerance is one budget measured against the chain's
   latest timestamp, so a run of small steps cannot walk back without a limit.
2. **No earlier than admission.** A timestamp is no earlier than the device's first honoured `admit`, less the same
   5 minutes. A `join-request` and an `announce`, which a device writes before any admit, are exempt.

A checked entry must also carry the one canonical shape, `YYYY-MM-DDTHH:MM:SS.mmm+00:00`.

**Nothing is checked until the owner arms it** with `hive-mind owner freeze-timestamps`, which signs a marker that
freezes each device's chain at its current `seq`. An entry at or below its device's frozen `seq` is unchecked, by
chain position and never by timestamp, so a backdated entry cannot claim the exemption. Until then a node keeps the
projection it had. An entry outside the bounds is skipped by the projection on every node, and `hv doctor`'s
`ts-bounds` check names it.

**The own-clock clamp.** A device that writes through `hv` stamps each automatic entry `max(now, the latest timestamp
it holds)`, so a clock that stepped back never writes an entry the bounds would skip. A step past the tolerance is
named on stderr. A timestamp the caller passes explicitly is used as given. A module signs its own entry, so it
applies the same rule itself: **never stamp earlier than your last entry**.

**Detection only.** Two further checks never refuse anything. `future-dated` flags a device whose entries arrived
here stamped more than 10 minutes ahead of this node's clock (from a local arrival record that is never synced).
`flood` flags a device that stamped more entries into a minute or a day than the owner-signed `flood_per_minute`
(default 120) and `flood_per_day` (default 2000). Both are advisories in `hv doctor` and `hv audit`.

## The module manifest

A module is a git repository with a signed manifest. `hive-mind module add` checks it before anything is installed.

`module.json`, signed by the module's **publisher key** in `module.json.sig` (Ed25519 over the exact bytes of
`module.json`):

| Field | |
|---|---|
| `name` | The module name (a valid module name; it prefixes `x-<name>:`). Required. |
| `version` | A string, up to 64 characters. Required. |
| `min_core` | The oldest core contract it runs on, a dotted version; refused on an older node. Default `0`. |
| `publisher` | The publisher's Ed25519 public key, base64. Required. |
| `files` | `{relative path: sha256 hex}` for every file, 1 to 500 of them. The fetched tree must hold exactly these files and no others. Required. |
| `service` | Optional. `{"command": [argv…], "restart": "no" \| "on-failure" \| "always", "interval": seconds ≥ 60}`. `interval` makes it a timer. |
| `hooks` | Optional list of events the module hooks. **Provisional; see below.** |
| `config` | Optional `{key: default string}`, per-node defaults under the module's own prefix. |
| `quota` | Optional `{per_hour, per_day, entry_bytes, lifetime}`: at most the defaults. |

A field this node does not know is refused, not ignored.

### `hive-mind module`

Control plane, owner and operator only; there is no `hv module`.

| Verb | Does |
|---|---|
| `add <name> --from <repo> [--publisher KEY] [--principal P]` | Platform check first, then fetch, verify, show the publisher key and pin it on the owner's confirmation (trust on first use), stage and hash again, mint the module's device key, sign the `admit` (principal: the operator's, with the module marker), write config and unit, record it in `$HIVE_HOME/.modules.json`. A failed `add` leaves nothing behind. |
| `remove <name>` | Revoke the device, stop and remove the unit, delete the module's files and key, drop the record. The module's entries **stay** in the journal. |
| `update <name> [--from <repo>]` | Re-verify and swap atomically. A different publisher key than the pin is refused. The device, key, pin and per-node config survive. |
| `list` | Name, version, publisher fingerprint, signature state, service state, device state, quota use. |
| `quota <name> [--per-hour N] [--per-day N] [--entry-bytes N] [--lifetime N]` | The owner's limits for one module. |

A module is installed under `$HIVE_MODULES_DIR` (default `~/.hive/modules/<name>/`), **outside the checkout**, so
`hive-mind reset` and `hv verify` never touch it, and its key lives in the key directory under `modules/<name>/`.
Per-node config is `$HIVE_MODULES_DIR/<name>/config`: local, never journaled, and an update keeps its values.

**Platform.** 2.1 supports modules on **Linux with a user systemd manager** only. Elsewhere `add` says
"module management is not supported on this platform yet (2.1 ships on Linux only)" and exits non-zero before it
fetches, mints or installs anything.

**Publisher key.** The release key `hivemind.pub` never signs a module. The publisher key is the module's, pinned
on first use. The operator compares its fingerprint (the first 16 hex of the sha256 of the key) or passes
`--publisher` to pin it without a prompt.

### The unit

A manifest with a `service` block becomes `hive-module-<name>.service`, and `hive-module-<name>.timer` when
`interval` is set, in the operator's systemd user directory. It runs as the operator's user, in the module's own
directory, with the module's own key directory and **no owner-key path** in its environment. It does not use the sync
unit's `pkill -f` pattern. `add` installs it, `update` re-renders it, `remove` stops and deletes it.

**After a core update** (`hive-mind update`), the internal `hive-mind module reapply` runs: it renders every
installed module's units again, writes only the files that changed, reloads systemd once and restarts only the units
it rewrote. It never fails the update; a module it cannot re-apply is a warning that names it.

### Doctor

`hv doctor` reports one `modules:<name>` result per module and none on a node with no modules, so a node without
modules sees nothing new. A manifest that is missing, tampered with or signed by anything but the pinned publisher
is a **fail**, because the module runs code. A unit that is absent, differs from its rendering or is not running, a
revoked device, missing config, a listener that is down and quota past 80% of a limit are **warns**. A stray
directory under `$HIVE_MODULES_DIR` that this hive does not manage is reported without being called an error, since
the directory is shared between hives on a machine. `modules-contract` warns, on a hive that has modules and
`quorum_m > 0`, about any node still on contract 2.0.

`hive-mind doctor --fix` re-renders a unit that differs from its manifest and starts one that is `failed` or not
enabled. **A unit that is enabled but `inactive` was stopped on purpose, and `--fix` leaves it alone**: the doctor
reports it, with the `systemctl --user start` line that resumes it, and the 15-minute timer never restarts it. `--fix`
acts only on a module whose manifest verifies, never re-admits a revoked device and never touches governance.

### Hooks and events

> **PENDING: plan PR 8, then 9b's hooks check.** This section is a placeholder and is filled after PR 8 merges. The
> `hooks` manifest field is *provisional* until then. See [`CONTRACT.md`](CONTRACT.md#pending-until-plan-pr-8-merges).

## Fleet config

The owner sets `x-<module>:<key>` as a string with a signed `set-config`; every node projects it, a node that does
not know the key ignores it, and core never interprets the value. A module reads it through `GET /v1/`, and cannot
write governed config. A bare key core does not know is still refused, and a key set by anyone but the owner does
not project.
