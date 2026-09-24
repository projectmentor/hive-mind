# HiveMind Sync API Reference

The sync daemon (`hive_sync_daemon.py`) is a Python-stdlib HTTP server on port `9876`. It serves
three things from one port:

- the **sync protocol** peers use to converge their journals (`/sync/*`);
- **discovery**, so a new device can find and verify a hive before it is admitted (`/hive/info`);
- the **dashboard** (`hv dash`) and the JSON data it reads (`/api/*`).

Sync wire-protocol version: **2** (the `protocol_version` field; 2 = understands signed read requests).
The agent contract version is reported separately as `contract` (see `hv version`).

Start it:

```bash
./hv sync daemon              # serve + outbound sync every 5 minutes (what the background service runs)
python3 hive_sync_daemon.py   # same, direct
```

---

## Binding

The daemon never listens on all interfaces by default. It binds, in order of priority:

1. `HIVE_BIND` (environment) — any value, including `0.0.0.0` if you really want all interfaces (it warns).
2. `bind` in `.peers.json`, if it is a specific address. A legacy `0.0.0.0`, `::` or empty value (what
   older installers wrote) is treated as "automatic".
3. This node's Tailscale IP, if `tailscale ip` finds one.
4. `127.0.0.1` — loopback only (single node, no tailnet, or Android/Termux where there is no `tailscale` CLI).

When it binds a specific non-loopback address (the usual case: the tailnet IP), it **also** listens on
`127.0.0.1`, so the local dashboard and `hv` keep working.

**An automatic bind heals itself** (cases 3–4). At start the daemon waits up to 30 s for a tailnet
address when a `tailscale` CLI is present, so it doesn't lose the race with `tailscaled` at boot. If
it still starts on loopback, it checks for the tailnet every 15 s. Once bound, it re-checks every
sync round. When a better address appears, it restarts itself (exit 75) and the service manager
brings it back on the new address: loopback to the tailnet IP, or an old tailnet IP to a new one. It
never moves toward loopback, so a Tailscale outage doesn't knock it off the tailnet. A `HIVE_BIND` or
a specific `.peers.json` bind never moves. `hv doctor` reports a daemon stuck on the wrong address as
`sync-bind`, and `hv doctor --fix` restarts it.

---

## Access control

Every path belongs to one of three classes. A **loopback** caller is a request that arrives on
`127.0.0.1` / `::1` — that is, from the same device.

| Class | Paths | From this device (loopback) | From another device |
|---|---|---|---|
| **Open discovery** | `/hive/info`, `/sync/merkle-root`, `/api/verify` | allowed | allowed — none of these returns journal content |
| **Remote-auth** | `/sync/hello`, `/sync/chunk`, `/sync/ingest` | allowed | depends on the node's **sync auth mode** (below) |
| **Local only** | the dashboard (`/`, `/index.html`, `/dashboard`, `/dashboard/`, `/logo.svg`, `/favicon.svg`) and every other `/api/*` path | allowed | only a validly **signed** request from an admitted device (the dashboard's per-node view, below); anything else gets **403**, even in `permissive` mode |

**Sync auth mode** is set per node and is not journaled, so each node can switch on its own schedule:

| Mode | Remote-auth paths from another device | Local-only paths from another device |
|---|---|---|
| `off` | allowed | allowed |
| `permissive` *(default)* | allowed; an unsigned or invalid request is logged as one that `enforce` would block | signed requests only (403 otherwise) |
| `enforce` | a valid signature from an admitted device is required (**401** otherwise) | signed requests only (403 otherwise) |

Set it with `hv sync auth off|permissive|enforce` (restart the daemon to apply). The mode is read from
`HIVE_SYNC_AUTH`, then `.peers.json` → `sync_auth`, then defaults to `permissive`. Switch to `enforce`
once every peer reports `protocol_version` 2 or higher.

**What this means for the dashboard.** Open it on a device that runs a HiveMind node:
`http://127.0.0.1:9876/` (or `hv dash`). That includes an Android phone running HiveMind in Termux.
A browser on a device that is *not* running its own node, pointed at another node's tailnet address,
is refused with 403. To look at another node, use the dashboard's node picker — your own daemon fetches
that node's data with a signed request.

---

## Authentication (`Hive-Auth-*` headers)

A signed request carries six headers:

| Header | Value |
|---|---|
| `Hive-Auth-Alg` | `hive-sig-v1` |
| `Hive-Auth-Device` | the signer's device id, `k1:` + first 16 hex of `sha256(pubkey)` |
| `Hive-Auth-Pub` | the signer's Ed25519 public key, base64 (32 bytes) |
| `Hive-Auth-Ts` | Unix time in seconds |
| `Hive-Auth-Nonce` | a random nonce (hex) |
| `Hive-Auth-Sig` | base64 Ed25519 signature over the bytes below |

The signed bytes are these seven lines joined with `\n`:

```
hive-sig-v1
<METHOD>                         GET or POST, upper-case
<path>                           e.g. /sync/chunk
<canonical query>                the query parameters sorted and URL-encoded ("" if none)
sha256:<hex digest of the body>  the digest of an empty body for a GET
<Hive-Auth-Ts>
<Hive-Auth-Nonce>
```

The daemon accepts the request only if all six headers are present, the algorithm matches, the public
key's fingerprint equals `Hive-Auth-Device`, the timestamp is within the freshness window
(`HIVE_SYNC_AUTH_WINDOW`, default 300 seconds either way), the signature verifies, the nonce has not been
seen within twice the window (a per-process replay cache), and — once the hive has an owner — the
device is admitted and not purged. Before an owner exists any valid signature passes, so the genesis
owner declaration can propagate.

`hv sync` signs every request with this device's key. A device that has no key yet sends no headers;
that works only against a node in `off` or `permissive` mode.

---

## Sync endpoints

### `GET /hive/info` — open discovery

Minimal hive metadata plus the signed genesis, for a device deciding whether to join. Never returns
journal entries.

```json
{
  "node_id": "k1:10f6b761dd1c2a90",
  "hive_id": "h1:cf5b2e8adbe05936",
  "owner_id": "o1:3afa9410be4d1e04",
  "label": "gregorius",
  "node_count": 3,
  "protocol_version": 2,
  "contract": "1.22",
  "advertised_addr": "100.84.84.100:9876",
  "genesis": { "node_id": "k1:…", "seq": 1, "type": "governance", "payload": { "action": "owner", "…": "…" } }
}
```

| Field | Description |
|---|---|
| `node_id` | This device's id (`k1:…`, the fingerprint of its Ed25519 device key) |
| `hive_id` | The hive's id: `h1:` + 8 random bytes, minted by `hv owner init` and carried in the signed genesis. Empty before an owner exists. Nodes refuse to merge journals across different hive ids. |
| `owner_id` | The current owner's fingerprint (`o1:` + first 16 hex of `sha256(owner pubkey)`), from governance |
| `label` | This node's display label (`HIVE_NODE_LABEL`, default the hostname) |
| `node_count` | Number of admitted devices (the count of distinct authoring devices if nothing is admitted yet) |
| `protocol_version` | Sync wire-protocol version (2) |
| `contract` | The agent contract version (`hv version`); `hv doctor` reads it for the `fleet-contract` check |
| `advertised_addr` | The `address:port` this daemon actually bound |
| `genesis` | The signed owner declaration, so a joiner can verify the hive's origin |

### `GET /sync/merkle-root` — open discovery

```json
{ "root_hash": "sha256:4bff2b51c9e3a2f0…" }
```

The root of the Merkle tree over the whole journal, computed over the journal **as a set**: entries are
de-duplicated by `(node_id, seq)` before hashing, so two nodes holding the same entries always produce
the same root. Equal roots mean nothing to sync.

### `GET /sync/hello` — remote-auth

The handshake: per-node sequence maxima and per-node chunk hashes, used to find which windows differ.

```json
{
  "node_id": "k1:10f6b761dd1c2a90",
  "hive_id": "h1:cf5b2e8adbe05936",
  "protocol_version": 2,
  "contract": "1.22",
  "advertised_addr": "100.84.84.100:9876",
  "journal_summary": { "total": 802, "by_node": { "k1:10f6b761dd1c2a90": 335, "k1:597b3e0f5fb92d37": 464 } },
  "chunks": { "k1:10f6b761dd1c2a90": ["sha256:d46957a7…", "sha256:4bff2b51…"], "k1:597b3e0f5fb92d37": ["…"] }
}
```

| Field | Description |
|---|---|
| `journal_summary.by_node` | Highest `seq` held for each authoring device |
| `chunks` | Per device, the hashes of consecutive 100-entry windows (seq 1–100, 101–200, …) |

### `GET /sync/chunk?node=<device_id>&start=<seq>&end=<seq>` — remote-auth

The journal entries authored by `node` with `start ≤ seq ≤ end` (inclusive, 1-based).

```json
{ "entries": [ { "node_id": "k1:…", "seq": 1, "type": "fact", "…": "…" } ], "hash": "sha256:…" }
```

`hash` is the hash of the returned entries, for comparison against the window's hash from `/sync/hello`.

### `POST /sync/ingest` — remote-auth

Push entries this node is missing. Body:

```json
{ "hive_id": "h1:cf5b2e8adbe05936", "entries": [ { "node_id": "k1:…", "seq": 12, "…": "…" } ] }
```

- `Content-Length` is required (400 if missing or malformed) and must not exceed 32 MiB (413).
- The signature check covers the exact body bytes.
- If both sides have a hive id and they differ, the push is refused with **409**:
  `{"error": "different hive", "hive_id": "<local>", "accepted": 0}`. An empty hive id on either side is
  allowed, so the genesis can propagate during bootstrap.
- Each entry is checked on ingest (see *Journal entries*); entries are de-duplicated by `(node_id, seq)`.
- Ingests are serialized; the local index is rebuilt after any accepted entry.

Response: `{"accepted": 3, "duplicates": 0}`.

---

## A sync round

`hv sync now`, and the daemon every 300 seconds, run one round with each peer in `.peers.json`:

```
Client                                   Peer
  |-- GET /sync/merkle-root ----------->  |   equal to ours → done
  |-- GET /sync/hello ----------------->  |   refuse if the hive ids differ
  |   compare per-device chunk hashes      |
  |-- GET /sync/chunk?node=…&start=…&end=… |   PULL each differing window, in pages of
  |      (repeated)                        |   HIVE_SYNC_PULL_PAGE entries (default 25)
  |   append (de-dup) + rebuild            |
  |-- POST /sync/ingest ---------------->  |   PUSH what the peer lacks, in pages of
  |      (repeated)                        |   HIVE_SYNC_PUSH_PAGE entries (default 25)
```

Both directions happen in one round; there is no leader or coordinator. Where the platform allows it
(not macOS), sync connections clamp the TCP segment size (`HIVE_SYNC_MAXSEG`, default 1000) so transfers
fit tailnet paths with an MTU below 1280.

---

## Dashboard data (`/api/*`) — local only

These endpoints feed the dashboard. They are listed here so the daemon's surface is fully documented;
they are the dashboard's internal data layer, not a stable public API.

| Path | Parameters | Returns |
|---|---|---|
| `/api/overview` | — | Counts, convergence, contested items, the audit summary |
| `/api/search` | `q`, `tag`, `kind` = `all`\|`fact`\|`decision`\|`idea`, `min_confidence`, `limit` (1–200, default 50), `offset`, `sort` = `confidence` (default; `salience` is a legacy alias) \| `importance` \| `utility` \| `recency`, `status` = `all` \| `contested` \| `forgotten` (owner-forgotten) \| `volatile` | Paginated facts and decisions, like `hv search` |
| `/api/tags` | — | Tag counts |
| `/api/related` | `kind` = `fact`\|`decision`, `id` | Entries related to one item |
| `/api/item` | `sid` (an `h:…` short id or a `node_id:seq` ref) | One item, for dashboard deep links (`/#h:…`) |
| `/api/audit` | — | The `hv audit` result |
| `/api/status` | optional `cmd` = `whoami`\|`stats`\|`doctor` | The text output of those `hv` commands |
| `/api/verify` | — | The `hv verify` result (**open discovery**, not local only) |
| `/api/daemon` | — | The code this daemon process loaded: `root` (its checkout), `digest` (`hv verify`'s source digest at startup, `null` if it could not be computed), `contract`, `pid`, `started_at`. Read by `hv doctor`'s `daemon-code` check ([#112](https://github.com/projectmentor/hive-mind/issues/112)); local only, never on `/hive/info` |
| `/api/telemetry` | `limit` (1–2000, default 25), `offset`, `sort`, `fproject`, `fagent`, `fnode`, `fday`, `fmodel`, `scope` = `self`\|`hive` | This node's session telemetry, or combined across reachable nodes with `scope=hive` |
| `/api/peers` | `probe` = `1` (default) \| `0` | Admitted devices with reachability |

**Per-node view.** `/api/overview`, `/api/search`, `/api/status` and `/api/telemetry` accept
`node=<device_id>`. For another node, the daemon looks up that admitted device's address itself (never
from the request, so it cannot be pointed at an arbitrary host), fetches the same path from it with a
signed request, and returns the result. An unreachable node returns
`{"available": false, "reachable": false, "node_id": "…", "error": "…"}` with status 200.

---

## Journal entries

The journal is the source of truth; `store.db` is an index rebuilt from it. Every entry has these
top-level fields:

```json
{
  "node_id": "k1:2a2110f3d8963a9e",
  "seq": 42,
  "type": "fact",
  "timestamp": "2026-09-23T12:00:00.000+00:00",
  "payload": { },
  "prev_hash": "sha256:…",
  "pub": "<base64 Ed25519 public key>",
  "sig": "<base64 signature over the entry without sig>"
}
```

| Field | Description |
|---|---|
| `node_id` | The authoring device: `k1:` + first 16 hex of `sha256(pub)` |
| `seq` | Per-device sequence number. `(node_id, seq)` is an entry's global identity; links between entries use it, never local ids. |
| `type` | See below |
| `timestamp` | ISO 8601, UTC |
| `payload` | Type-specific (see below) |
| `prev_hash` | Hash of this device's previous entry (a per-device hash chain) |
| `pub`, `sig` | The signer's public key and Ed25519 signature |

On ingest a signed entry is rejected if its `node_id` is not the fingerprint of its `pub` or its
signature fails. Unsigned entries (pre-migration history) are accepted as-is. Once the hive has an
owner, content from devices that are not admitted is not accepted.

### Entry types

| `type` | Since | Payload fields |
|---|---|---|
| `fact` | 1.0 | `content`, `tags`, `source`, `importance` (a hint, capped by the projection), `created_at`, optional `channel` (`sense`\|`act`\|`introspect`; absent = `sense`). Older entries may also carry `resolves_ref` (written by `--resolves` before contract 1.19 PR2b) and the legacy fields `source_session`, `trust_score`, `access_count`. |
| `decision` | 1.0 | `content`, `rationale`, `source`, `created_at`, `tags` (1.15), `informed_by` (1.19: list of `[node_id, seq]` refs it relied on). Older entries may carry `supersedes_ref` (written by `--supersedes` before PR2b). |
| `entity` | 1.0 | `name`, `type`, `attributes`, `created_at` |
| `retract` | 1.0 | `retracts_ref` (`[node_id, seq]` of the fact), `reason`, `source`; an owner forget also carries `owner_pub` and `owner_sig` |
| `governance` | 1.4 | `action` plus action-specific fields; authority-bearing actions carry `owner_pub` / `owner_sig` (see below) |
| `capsule` | 1.13 | A sealed secret: `name`, `capsule_id`, `version`, `kind`, `alg`, `nonce`, `ct`, `tag`, `wraps` (per-recipient key wraps), `signer`, `hive_id`, and `owner_pub` / `owner_sig` when owner-signed |
| `cell`, `comb` | 1.13 | Self-wiring definitions (`hv wire`); a comb is an ordered list of cells |
| `link` | 1.19 | `kind`, `from_ref`, `to_ref` (both `[node_id, seq]`), `data`, `source`, optional `channel`; `owner_pub` / `owner_sig` when written on the owner's machine |
| `idea` | 1.20 | `content`, `tags`, `source`, `channel` (defaults to `introspect`) |
| `entity_fact` | 1.0 | *Legacy:* `entity_ref`, `fact_ref`, `confidence`. Since PR2b `hv entity link` writes a `link` of kind `entity` instead; existing entries are still read. |

`link` kinds the projection understands: `supports`, `contradicts`, `supersedes`, `resolves`, `entity`,
`informed`, `outcome-of`. An unknown kind is stored and ignored, so nodes on different versions never
diverge over it. See `docs/INTERNALS.md` → *Links*.

**Governance actions** (`payload.action`): `owner` (the genesis declaration), `admit`, `revoke`,
`deny`, `change`, `purge`, `join-request`, `announce`, `set-config`, `standby`, `owner-escrow`,
`revoke-escrow`, `nominate-successor`, `revoke-nomination`, `claim-succession`, `transfer`,
`heartbeat`, `propose-election`, `vote-election`.

---

## Configuration: `.peers.json`

Written by the installer; per node and git-ignored.

```json
{
  "self": "k1:10f6b761dd1c2a90",
  "port": 9876,
  "peers": [
    { "id": "100-123-162-114", "url": "http://100.123.162.114:9876" }
  ]
}
```

| Field | Default | Description |
|---|---|---|
| `self` | — | This device's id |
| `port` | `9876` | Port to listen on |
| `peers[].url` | required | Base URL of a peer's daemon |
| `peers[].id` | optional | A label for logs. A device id (`k1:…`) here also tells `hv doctor` which device the entry is (below) |
| `bind` | automatic | Optional override of the bind address (see *Binding*); `0.0.0.0` is treated as automatic |
| `sync_auth` | `permissive` | Optional sync auth mode (`hv sync auth` sets it) |

Admitting a device with `hv group admit` also adds a peer entry from the address in its join request.
To add a device, run `hive-mind invite` on a device already in the hive and paste the line into
`hive-mind install` on the new one.

**When a peer's address changes** (same device key, new tailnet IP). A request that passes signature
verification (see *Authentication*) proves which admitted device sent it, and the daemon sees the address
it came from. The daemon records that pair in `$HIVE_HOME/.peer_candidates.json`. This
includes the signed `/sync/merkle-root` call that a peer already in sync makes each round: the daemon
checks that signature only to learn the address, and the path stays open. Unsigned requests, failed
signatures and loopback record nothing. The file is local: it is never journaled or synced, and it is
git-ignored. `hv doctor` reads it in the `peer-address` check. When a peer's stored address does not
answer and its device has verified itself from another address since `.peers.json` last changed,
`hv doctor --fix` (which the 15-minute doctor timer runs) replaces that entry's URL host. The scheme,
port and every other key stay as they are. An address that answers is never rewritten. An entry is
matched to its device by a device id in `id`, or else by the one device verified at its stored address. Host
names are never used, because they are self-reported and can collide. When no verified address exists,
doctor only reports, and lists devices with the same name in `tailscale status` as unverified hints. A
peer that never contacts this node is not learned this way; that needs responder-signed `/sync/hello`
replies, tracked in [#107](https://github.com/projectmentor/hive-mind/issues/107).

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `HIVE_BIND` | — | Bind address override (see *Binding*) |
| `HIVE_SYNC_AUTH` | — | Sync auth mode override (`off`\|`permissive`\|`enforce`) |
| `HIVE_SYNC_AUTH_WINDOW` | `300` | Signed-request freshness window, seconds |
| `HIVE_SYNC_PULL_PAGE` | `25` | Entries per `/sync/chunk` request |
| `HIVE_SYNC_PUSH_PAGE` | `25` | Entries per `/sync/ingest` request |
| `HIVE_SYNC_MAXSEG` | `1000` | TCP segment-size clamp for sync connections (`0` disables it) |

### Platform notes

- **WSL2:** each WSL instance is its own machine on the tailnet; the daemon binds that instance's
  Tailscale IP. Use `tailscale ip -4` inside WSL, not the Windows host's address.
- **macOS:** Tailscale runs as the app or `brew services`; the daemon runs as the launchd agent
  `com.projectmentor.hive-sync` and logs to `~/Library/Logs/hive-mind/`.
- **Android (Termux):** there is no `tailscale` CLI, so the daemon binds `127.0.0.1`. The phone cannot
  accept inbound sync, but it converges by syncing outbound every 5 minutes, and its own dashboard is
  at `http://127.0.0.1:9876/`.

---

## Limits and errors

Errors are JSON: `{"error": "description"}`.

| Status | When |
|---|---|
| `200` | Success (also for an unreachable node in the per-node view, with `available: false`) |
| `400` | `/sync/ingest` without a valid `Content-Length` |
| `401` | A remote-auth path without a valid signature, in `enforce` mode |
| `403` | A local-only path from another device without a valid signature (unless the mode is `off`) |
| `404` | Unknown path |
| `409` | `/sync/ingest` from a different hive |
| `413` | `/sync/ingest` body over 32 MiB |
| `429` | Too many requests from one address (a per-address token bucket: burst 256, 64 per second) |
| `500` | An internal error; the handler never crashes the server |
| `503` | More than 32 requests in flight |

Each connection has a 30-second read timeout.
