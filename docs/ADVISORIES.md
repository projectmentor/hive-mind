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

The bundled file is the single source of truth: there is no online feed and nothing is fetched.

## Published security advisories

Vulnerabilities in HiveMind itself are published as GitHub security advisories. Report a new one
privately; see [`SECURITY.md`](../SECURITY.md).

| Advisory | Severity | What | Affected | Fixed |
|---|---|---|---|---|
| [GHSA-242f-7fxg-f7wm](https://github.com/projectmentor/hive-mind/security/advisories/GHSA-242f-7fxg-f7wm) | High (CVSS 7.5) | Unauthenticated journal disclosure through the sync API: `/sync/chunk` served journal entries to any client that could reach the port, and the daemon bound all interfaces by default. | Installs from before July 8, 2026 (contract 1.17 and earlier, before commit `10dc598`) | PR #42 (`10dc598`): signed sync reads from admitted devices, local-only dashboard data, and a bind to the Tailscale address instead of all interfaces. Every install at contract 1.18 or later includes it; update with `hive-mind update` and confirm with `hv verify`. Reported by EQSTLab and 2REBCat. |
