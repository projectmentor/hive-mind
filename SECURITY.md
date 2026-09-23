# Security

HiveMind holds your agents' memory and can distribute secrets between your devices, so security
questions deserve a clear starting point. This page is that starting point: how to report a
problem, and where each part of HiveMind's security is documented.

## Reporting a vulnerability

**Please report privately. Don't open a public issue.** Use GitHub's private vulnerability
reporting: **Security → Report a vulnerability** on this repository, or go directly to
<https://github.com/projectmentor/hive-mind/security/advisories/new>. If you can't use GitHub, email
<netadmin@projectmentor.org>.

Include what you found, how to reproduce it, the version (`hv version`, and `hv verify` output), and
the impact you expect. HiveMind is maintained by one person, so responses are best-effort with no
guaranteed timeline, but security reports are handled first. Fixes are published as GitHub security
advisories, and reporters are credited unless they prefer not to be.

**Supported versions:** only the current `main` receives fixes. Update with `hive-mind update`, then
confirm with `hv verify`.

## Published advisories

See [`docs/ADVISORIES.md`](docs/ADVISORIES.md) for every published advisory and the bundled
`advisories.json` feed that `hv doctor` checks on every health pass.

## How HiveMind is secured

| Topic | What it covers | Where |
|---|---|---|
| **Threat model** | What HiveMind defends against, what it assumes, known limitations, cryptographic posture | [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) |
| **Is this the official release?** | `hv verify`: source integrity, the release signature, and the signing key published at `hivemind.projectmentor.org` | [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) → `hv verify` |
| **Device and owner keys** | Ed25519 device identity; the owner key that signs governance; backup, escrow, succession and quorum recovery | [`docs/INTERNALS.md`](docs/INTERNALS.md) → *Device identity*, *Owner resolution + succession*; `hv owner` in the CLI reference |
| **Sync network access** | What the daemon binds to; which paths are open, signed or local-only; the `off` / `permissive` / `enforce` modes; the signed-request format | [`docs/SYNC_API.md`](docs/SYNC_API.md) → *Binding*, *Access control*, *Authentication* |
| **Who counts toward confidence** | Admission, principals, the same-device discount and `cap_self` | [`docs/INTERNALS.md`](docs/INTERNALS.md) → *Confidence model*; `hv group` in the CLI reference |
| **Relationships can't erase knowledge** | Links are evidence, not commands: only the owner (by signature) or an entry's author can supersede or resolve it | [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md); [`docs/INTERNALS.md`](docs/INTERNALS.md) → *Links* |
| **Secrets** | Capsules: sealed to your authorized devices, never passed through chat or the command line; the limits of revocation | `hv capsule` in [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md); the capsule limitations in [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) |
| **Executable definitions** | Who may publish cells and combs (`cell_writers`) and capsules (`capsule_putters`) | `hv config` and `hv wire` in [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) |
| **Health checks** | `hv doctor` security checks: `authenticity`, `crypto`, `crypto-modules`, `keyperm`, `device-keys`, `capsule-authz`, `cell-authz`, `link-authz`, `trust-drift`, `fleet-contract` | `hv doctor` in [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) |
| **Agent integrations** | What an adapter may and may not do (hint, never act; the owner decides what is forgotten) | [`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md) §4 |

## Hardening checklist for operators

- **Establish the owner first,** before exposing the daemon to other devices, and verify the
  `hive_id` and owner id out of band when a new device joins (see the bootstrap limitation in the
  threat model).
- **Back up the owner key**: an off-device export, an in-hive escrow with a strong passphrase, or
  both (`hv owner export`, `hv owner escrow`).
- **Admit only devices you control** (`hv group admit`); `revoke` or `purge` a lost device, then
  `hv capsule rotate` **and** rotate the upstream secret, since removed devices keep old ciphertext.
- **Keep key files private:** `hv doctor` fails if `.device-key` or `.owner-key` is readable by
  anyone but you, and `hv doctor --fix` re-tightens them.
- **Switch sync to `enforce`** (`hv sync auth enforce`) once every peer reports protocol version 2,
  and don't set `HIVE_BIND=0.0.0.0`.
- **Restrict who can connect** with Tailscale ACLs; HiveMind's signed reads decide who can *read*.
- **Stay current:** `hive-mind update`, then `hv verify`. The self-heal timer runs `hv doctor --fix`
  every 15 minutes; read its advisories.

## Scope

In scope: everything in this repository — `hv`, the sync daemon, the installer and the
integrations under `integrations/`. Out of scope: Tailscale itself, the host operating system,
and third-party agents or models, though reports about how HiveMind uses them are welcome.
