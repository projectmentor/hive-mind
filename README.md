# HiveMind

**Peer-to-peer shared memory for AI agents.** When one agent learns something, every other
agent on every machine can use it too. Local-first, no cloud, no central server.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3.0-E07A00.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12-3776AB.svg)
![Status](https://img.shields.io/badge/status-alpha-yellow.svg)

Website: **[hivemind.projectmentor.org](https://hivemind.projectmentor.org)** ·
Docs: [`docs/`](docs/) · For developers: [hivemind.projectmentor.org/dev](https://hivemind.projectmentor.org/dev/)

<p align="center">
  <video src="https://hivemind.projectmentor.org/assets/hivemind-video-splash.mp4"
         poster="https://hivemind.projectmentor.org/assets/og-image.png"
         controls muted loop playsinline width="680"></video>
</p>

> If the video does not play inline, watch it at
> **[hivemind.projectmentor.org](https://hivemind.projectmentor.org)**.

HiveMind is a shared, append-only memory that your AI agents read and write as they work.
Facts, decisions, and outcomes accumulate over time and earn trust through **independent
corroboration**, not by an agent asserting it. Each machine holds the full memory and syncs
directly with its peers over your private Tailscale network. There is no server to operate and
nothing leaves your hardware.

It is not a vector database or a RAG framework. It is the memory-and-trust layer your agents
share so they work together, instead of each starting from a blank slate. That is what sets it
apart from Pinecone, Weaviate, LlamaIndex, LangGraph, and friends.

---

## What you get

- **Syncs automatically** — when one agent learns something, every other agent on every machine
  gets it, over Tailscale, with only the differences transferred (a Merkle delta).
- **Trust earned, not assumed** — confidence in a fact rises only when *distinct, independent*
  agents agree. A single agent cannot inflate its own credibility, agents on one machine count
  for less than agents on separate machines, and only devices you have admitted count at all, so
  no one can mint a crowd of keys to fake agreement. Conflicts are surfaced, not silently overwritten.
- **Coordinate without a coordinator** — no leader, no Raft, no lock server. A conflict-free
  set (G-Set CRDT) over an append-only journal, so every device is equal and converges.
- **Works offline** — agents keep working with no connection; entries merge cleanly on reconnect.
- **Fast local search** — full-text search runs on your machine in milliseconds, ranked by
  corroboration. No round trips, no data leaving your network.
- **Nothing to operate** — no servers to provision, no database to manage, no cloud accounts.
  Any single device's journal is the complete memory; a backup is just files.
- **Auditable** — every fact records who wrote it and when; nothing is silently overwritten.
- **Works with your agents today** — Claude Code, Hermes, Claude Desktop (via MCP), and any
  agent that can run a shell command.

---

## Install

On Linux (or a WSL terminal on Windows 11), run:

```bash
curl -fsSL https://raw.githubusercontent.com/projectmentor/hive-mind/main/scripts/installer/install.sh | bash
hive-mind install
```

The installer will:

1. Install Tailscale (used for syncing between machines) and authenticate it
2. Install Python dependencies
3. Clone this repo to `~/projects/hive-mind`
4. Ask for one input: your peer's Tailscale IP. **First machine with no peers yet? Just press
   Enter** — you can add peers later by editing `.peers.json`.
5. Initialise the local database
6. Install and start the sync daemon as a systemd service
7. Wire the Hermes memory plugin (if Hermes is installed)

### Requirements

- **Linux** (native) or **Windows 11 with WSL2**, with systemd enabled (Mac and Android
  support are in progress)
- **Internet access** (for the install and git clone)
- **Tailscale** *only if you want multiple machines to sync.* The installer sets it up for
  you (inside WSL on Windows; it is **not** needed on the Windows host). A single-machine
  setup needs no Tailscale at all — there are no peers to reach.

<details>
<summary>Enable systemd in WSL (if it isn't already)</summary>

```bash
sudo bash -c 'echo -e "[boot]\nsystemd=true" >> /etc/wsl.conf'
# Then, from a Windows terminal:
wsl --shutdown
# Reopen WSL and proceed
```
</details>

---

## Usage

```bash
./hv remember "The payments API rate-limits at 100 req/s" --tags api,payments
./hv search "payments"
./hv decide "Use AGPL for the core" --rationale "keeps the dual-license option open"
./hv stats
./hv doctor          # health check: integrity, DB, sync, hygiene, agent hooks (--fix self-heals)
./hv sync now        # manual sync to all peers
```

Most of the time your agents call `hv` for you. Full reference:
[`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md).

### The `hive-mind` command

| Command | Description |
|---|---|
| `hive-mind install` | Full device setup from scratch |
| `hive-mind status`  | Show device health and peer sync state |
| `hive-mind uninstall` | Remove HiveMind from this device (`--keep-hive` preserves your journal + keys; `--yes` skips the confirmation prompt) |
| `hive-mind update`  | Pull latest and restart the daemon |

---

## How it works

- **Journal** (`journal/YYYY-MM-DD.jsonl`) — an append-only event log. **This is the source of
  truth.** Each entry carries a device id, per-node sequence, type, payload, a hash chain, and a
  signature. A node identifies by an Ed25519 **device key** (not a hostname), and entries are
  signed and verified on sync, so a peer cannot forge another node's identity to inflate confidence.
- **SQLite** (`store.db`) — a derived index (WAL + FTS5) rebuilt from the journal on any node.
- **Merkle index** — per-node chunk hashes for efficient delta sync: only missing entries move.
- **Sync daemon** — a stdlib HTTP server on `:9876` that syncs with peers (every 5 minutes, or
  on demand). No leader, no central broker. The tailnet is the trust perimeter, but the daemon also
  caps request bodies, times out slow reads, bounds concurrent requests, and rate-limits per peer,
  so a single misbehaving peer can't exhaust a node.
- **Capsules** — share secrets (API keys, tokens) as encrypt-to-device *capsules*: a secret sealed
  so only the devices you've admitted can open it. Each recipient's key is **derived from that
  device's signed identity**, so a capsule can never be sealed to a key a device hasn't proven it
  holds. See `hv capsule`.
- **Confidence model** — a fact's confidence is *derived* from independent corroboration, never
  declared by the agent that wrote it. It's a governed projection: agreement across distinct
  **devices** counts most, multiple agents on one device are discounted, only owner-**admitted**
  devices count, and agreement among one principal's own machines is capped. Governance (owner,
  admitted devices, tunable parameters) lives in owner-signed journal entries, so every device
  computes the same confidence. The owner runs the membership lifecycle with `hv group`
  (admit, revoke, deny, change, purge, list). The owner key is recoverable, not a dead end:
  back it up off-device or escrow it in the hive (`hv owner export`/`escrow`), and hand it off
  to a new key by nomination or transfer (`hv owner nominate`/`claim`/`transfer`). If it is lost
  outright with no backup, admitted devices can elect a successor by quorum once the owner goes
  dark (`hv config quorum set`, then `hv owner propose-election`/`vote`); a live owner is never
  unseated, since any owner act (including `hv owner heartbeat`) resets the dead-man timer. See
  `hv owner` and `docs/INTERNALS.md`.

Deeper reading: [`docs/INTERNALS.md`](docs/INTERNALS.md),
[`docs/P2P_DESIGN.md`](docs/P2P_DESIGN.md), [`docs/SYNC_API.md`](docs/SYNC_API.md),
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — what HiveMind defends against, and what it assumes.

**Integrating an agent?** See [`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md) — a
runtime-agnostic spec each agent uses to wire *itself* to the hive (and keep itself current). One
brain (`hv`), one spec, no hand-maintained per-agent adapters.

---

## Works with your agents

- **Claude Code** — a skill (`integrations/claude-code/`) lets Claude Code read and write the
  hive directly.
- **Hermes** — if [Hermes Agent](https://hermes-agent.nousresearch.com) is installed, the
  installer wires the memory plugin so every `memory()` call is mirrored to the hive and synced.
  Manual setup: `hermes config set memory.provider hive-mind`.
- **Claude Desktop (MCP)** — a local stdio MCP server (`integrations/mcp/`) exposes the hive to
  Claude Desktop, so it reads and writes your shared memory with no copy-paste.
- **Any CLI agent** — if it can run a shell command, it can use `hv`.

---

## Multi-node sync

Run the installer on each machine. When prompted for peer IPs, enter the **WSL Tailscale IP** of
the other machine (run `tailscale ip -4` in WSL on that machine). Each WSL instance is its own
machine on the tailnet, so use that IP, not the Windows host IP.

```bash
cd ~/projects/hive-mind
./hv sync now    # pull from all peers
./hv stats       # confirm the journals converged
```

Sync runs over HTTP on port `9876` and needs no SSH. If you also want to administer
one node from another (run `git`, `systemctl`, or `hv` on a peer), that uses
Tailscale SSH, which needs an `accept` rule in your tailnet SSH ACL. See
[Remote administration](docs/INTERNALS.md#remote-administration-tailscale-ssh) for
the policy and the common failure modes.

---

## Roadmap

- **Federated hives (a colony)** — separate hives (personal, team, project) that selectively share
  what matters, so groups pool knowledge without merging into one pool. Multiple hives form a colony.
- **More platforms** — Mac and Android.
- **Hosted relay** — an optional managed tier for nodes that can't reach each other directly.

---

## Contributing

The source is open to read, audit, fork, and self-host. Contributions are welcome on a
best-effort, solo-maintainer basis. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first. Code
contributions require a one-time [CLA](CLA.md), automated on your pull request.

Questions or ideas? Start a
[Discussion](https://github.com/projectmentor/hive-mind/discussions).

## License

HiveMind is licensed under the **GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)).
You are free to use, modify, and self-host it; if you run a modified version as a network service,
you must make your source available under the same terms. The copyright holder (Certified Project
Management, LLC, d/b/a ProjectMentor) also reserves the right to offer a separate commercial
license.

## Support the project

HiveMind is free and open source. If it helps you, you can help keep it alive:

- ❤️ [GitHub Sponsors](https://github.com/sponsors/projectmentor)
- ☕ [Ko-fi](https://ko-fi.com/projectmentor)
- 🥤 [Buy Me a Coffee](https://www.buymeacoffee.com/projectmentor)

## Project

Built by [ProjectMentor](https://projectmentor.org). HiveMind by ProjectMentor, a project of
Certified Project Management, LLC. Need custom development or consulting on multi-agent systems?
Reach out at [hello@projectmentor.org](mailto:hello@projectmentor.org).
