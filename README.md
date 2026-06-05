# Hive Mind

Institutional memory as observable middleware for multi-agent AI systems.
A shared, append-only knowledge corpus that any agent (Hermes, Claude Code, Codex, etc.)
can read and write — synced peer-to-peer over Tailscale.

---

## Install

Open a WSL terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/projectmentor/hive-mind/main/scripts/installer/install.sh | bash
hive-mind install
```

That's it. The installer will:

1. Install Tailscale inside WSL (if not present) and authenticate it
2. Install Python dependencies
3. Clone this repo to `~/projects/hive-mind`
4. Ask for one input: your peer node's Tailscale IP (run `tailscale ip` on the peer).
   First node with no peers yet? Just press Enter — add peers later by editing `.peers.json`.
5. Initialise the database
6. Install and start the sync daemon as a systemd service
7. Wire the Hermes memory plugin (if Hermes is installed)

### Requirements

- WSL2 on Windows 11 with systemd enabled
- Tailscale installed on the Windows host
- Internet access (for Tailscale install + git clone)

#### Enable systemd in WSL (if not already)

```bash
sudo bash -c 'echo -e "[boot]\nsystemd=true" >> /etc/wsl.conf'
# Then from a Windows terminal:
wsl --shutdown
# Reopen WSL and proceed
```

### Multi-node setup

Run the installer on each node. When prompted for peer IPs, enter the
**WSL Tailscale IP** of the other node — get it by running `tailscale ip`
in WSL on that machine.

Each WSL instance appears as its own machine on the tailnet (e.g. `desktop-egmbl5a-1`).
Use that IP, not the Windows host IP.

After both nodes are up:

```bash
cd ~/projects/hive-mind
./hv sync now        # pull from all peers
./hv stats           # confirm journal entries converged
```

---

## Usage

```bash
./hv remember "fact to store" --tags tag1,tag2
./hv search "query"
./hv decide "decision" --rationale "why"
./hv stats
./hv sync now        # manual sync to all peers
```

Full command reference: [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md)
Sync API reference:    [docs/SYNC_API.md](docs/SYNC_API.md)

---

## How it works

- **Journal** (`journal/YYYY-MM-DD.jsonl`) — append-only event log, source of truth
- **SQLite** (`store.db`) — derived index, rebuilt from journal on any node
- **Merkle tree** — efficient delta sync: only missing entries are transferred
- **Sync daemon** — HTTP server on `:9876`, syncs to peers every 5 minutes
- **Confidence model** — corroboration across independent agents raises fact confidence

Architecture: [docs/P2P_DESIGN.md](docs/P2P_DESIGN.md)

---

## Hermes integration

If [Hermes Agent](https://hermes-agent.nousresearch.com) is installed,
the installer wires the memory plugin automatically. Every `memory()` call
Hermes makes is mirrored to the hive corpus and synced to all peers.

Manual setup:

```bash
hermes config set memory.provider hive-mind
```

---

## Subcommands (hive-mind CLI)

| Command | Description |
|---|---|
| `hive-mind install` | Full node setup from scratch |
| `hive-mind update` | Pull latest + restart daemon |
| `hive-mind status` | Show node health and peer sync state |

---

## License

HiveMind is licensed under the **GNU Affero General Public License v3.0** (see
[LICENSE](LICENSE)). In short: you're free to use, modify, and self-host it, and if you
run a modified version as a network service, you must make your source available under
the same terms. The copyright holder (Certified Project Management, LLC, d/b/a
ProjectMentor) also reserves the right to offer a separate commercial license.

## Contributing

The source is open to read, audit, fork, and self-host. Contributions are welcome on a
best-effort, solo-maintainer basis. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
first. Code contributions require a one-time [CLA](CLA.md), which is automated on your
pull request.

Want to get involved? Start a
[Discussion](https://github.com/projectmentor/hive-mind/discussions) or open a focused
pull request.

## Support the project

HiveMind is free and open source. If it helps you, you can help keep it alive:

- ❤️ [GitHub Sponsors](https://github.com/sponsors/projectmentor)
- ☕ [Ko-fi](https://ko-fi.com/projectmentor)
- 🥤 [Buy Me a Coffee](https://www.buymeacoffee.com/projectmentor)

## Project

Built by [ProjectMentor](https://github.com/projectmentor). HiveMind by ProjectMentor,
a project of Certified Project Management, LLC.
