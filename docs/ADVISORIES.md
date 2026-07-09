# Security Advisories

Hive-Mind ships a small, **offline-first** advisory feed at `advisories.json` in the repo root. It
lets the project warn every node about a known-bad dependency (a broken crypto primitive, a
compromised tool referenced by a cell) without any node having to phone home.

## How it works today

`advisories.json` is **source-controlled and signed**: it is hashed by `verify.json` (so `hv verify`
detects tampering) and distributed exactly like the rest of the code — over `git pull` /
`hive-mind update`. There is no network fetch and no separate feed; the file in the repo *is* the
feed.

Schema:

```json
{
  "version": 1,
  "updated": "2026-06-25",
  "crypto": [
    {"severity": "high", "summary": "one-line description an operator can act on", "id": "optional-slug"}
  ],
  "tools": []
}
```

- `crypto[]` — advisories about the bundled crypto layer (ed25519/x25519/chacha20-poly1305).
- `tools[]` — advisories about external tools/services a cell may wire up.
- `severity` — `low` | `medium` | `high`.

### Where it surfaces

`hv doctor` reads `advisories.json` on every health pass (the 15-minute `hive-doctor` timer). After
the crypto self-test (KATs) passes, it raises a **warn** for each `severity: "high"` entry in
`crypto[]`:

```
• crypto warn: KATs pass; 1 HIGH crypto advisory(ies): <summary>
```

Today only **high-severity crypto** advisories are surfaced automatically; `tools[]` and lower
severities are documentation that an operator (or an agent reading this file) can consult. The read
path lives in `hv` (the `doctor` crypto check, around the `advisories.json` load).

## Published advisories

### GHSA-242f-7fxg-f7wm — unauthenticated remote journal disclosure (High, CWE-306)

**Affected:** sync daemon (`hive_sync_daemon.py`) through protocol version 1.
**Fixed in:** protocol version 2 (read-authentication + bind hardening).

Before the fix, the sync daemon authenticated **writes** (per-entry Ed25519
signatures on `POST /sync/ingest`) but served **reads** to any host that could
reach the port. Any device on the tailnet — including a single compromised or
misbehaving peer — could pull the entire journal off `GET /sync/chunk` /
`GET /sync/hello` with no credential. The default bind was `0.0.0.0`, widening
the exposure to every interface, not just the tailnet.

The fix:

- **Read authentication.** Remote reads of journal content (`/sync/hello`,
  `/sync/chunk`, and `POST /sync/ingest`) now require a signed-request envelope
  (`Hive-Auth-*` headers) proving the caller is an **admitted device**, with a
  freshness window and nonce replay guard. Loopback (`127.0.0.1`) — the local
  operator, `hv`, and the dashboard — stays unauthenticated. Discovery
  metadata (`/hive/info`, `/sync/merkle-root`, `/api/verify`) stays open but
  never returns journal content. The dashboard `/api/*` data layer is
  loopback-only. See `docs/SYNC_API.md`.
- **Bind hardening.** The daemon now binds its own locally-bindable Tailscale IP
  (`100.64.0.0/10`), else `127.0.0.1` — never `0.0.0.0` by default. It also binds
  loopback for the local CLI/dashboard. `HIVE_BIND` remains a deliberate escape
  hatch; a legacy `0.0.0.0` in `.peers.json` is auto-upgraded on restart. The
  installer no longer hardcodes the bind.
- **Phased rollout.** Enforcement is per-node (`off` | `permissive` | `enforce`,
  default `permissive`), so a mixed-version fleet upgrades without breakage:
  land the fix → every node reaches protocol 2 in `permissive` (clients sign,
  nothing breaks) → confirm all peers report `protocol_version` 2 → flip each
  node to `enforce` with `hv sync auth enforce`. See the rollout runbook in
  `docs/SYNC_API.md`.

This advisory concerns the **application layer**; it is not a `crypto[]` or
`tools[]` dependency advisory, so it is not surfaced by `hv doctor` — it is
recorded here and in the threat model (`docs/THREAT_MODEL.md` §3) as the
authoritative account of the fix.

## How to publish a new advisory

It is a normal, signed code change — no special command:

1. Edit `advisories.json`: append an entry to `crypto[]` or `tools[]` with `severity` + a one-line
   `summary` (and an optional `id`).
2. Bump the top-level `updated` date.
3. Regenerate and re-sign the source manifest so `hv verify` stays green. On a push to `main` CI does
   this automatically (`.github/workflows/sign.yml` runs `scripts/common/gen_verify.py` then
   `scripts/common/sign_release.py`). To do it locally you need the release key:
   `python3 scripts/common/gen_verify.py && HIVE_SIGNING_KEY=… python3 scripts/common/sign_release.py`.
4. Commit and push. Every node picks it up on its next `git pull` / `hive-mind update`, and the next
   `hv doctor` pass surfaces any high-severity crypto entry.

## Planned extensions (not yet implemented)

The `_comment` in `advisories.json` records the roadmap:

- **Owner-signed advisories over the journal** — push an advisory as an owner-signed governance
  entry so it propagates with normal P2P sync, no `git pull` required.
- **Optional Ed25519-verified online feed** — fetch advisories from a URL and verify the signature
  against the bundled public key, for nodes that opt in to online updates.

Until those land, the bundled file is the single source of truth.
