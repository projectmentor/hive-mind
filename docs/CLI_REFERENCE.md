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
| `hv remember` | Store a fact |
| `hv search` | Search stored facts |
| `hv decide` | Record a decision |
| `hv retract` | Correct a fact you got wrong |
| `hv entity` | Track named things (people, projects, concepts) |
| `hv stats` | See a summary of your memory |
| `hv doctor` | Check that your node is healthy |
| `hv key` | Show or create this node's device identity |
| `hv rebuild` | Fix the local database if something looks wrong |
| `hv merkle` | Diagnose sync state between nodes |
| `hv sync` | Sync with peer nodes |
| `hv migrate-device-identity` | One-time: re-stamp the journal to device identities |

---

## Running `hv`

```bash
cd ~/projects/hive-mind
./hv <command> [options]
```

---

## Commands

---

### `hv remember` — Store a fact

Save anything worth keeping: observations, constraints, gotchas, status updates.
Facts are searchable, tagged, and automatically gain credibility when multiple
independent sources agree on the same thing.

```
hv remember <content> [--tags TAGS] [--source SOURCE] [--importance N] [--gate]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The text of the fact. Put it in quotes. Required. |
| `--tags` | Comma-separated labels to help you find it later, e.g. `--tags infrastructure,todo`. No spaces. |
| `--source` | Who or what is asserting this fact. Helps HiveMind tell independent sources apart. Defaults to `manual`. See [Source identity](#source-identity) below. |
| `--importance` | A numeric hint for how significant this fact is. Recorded for future use but not currently applied to search ranking. |
| `--gate` | Filter this write through the **salience gate** — a quality check that silently drops low-value entries (too short, no meaningful content, likely noise). Useful when an agent is writing many facts at once and you want to keep your memory clean. |

**What you get back:**

- `Remembered as fact #N` — stored successfully
- `Already known (fact #N)` — identical content already exists; nothing written

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

# With salience gate — low-quality entries are silently dropped
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

### `hv search` — Search stored facts

Find facts by keyword. Results are ranked by confidence — the most corroborated
facts come first.

```
hv search <query> [--format {text,json}] [--min-confidence N]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `query` | What to search for. Multiple words all have to match. Use `OR` between words for either/or. Use `"quoted phrases"` for exact matches. |
| `--format` | `text` (default) for readable output. `json` for machine-readable output you can pipe to other tools. |
| `--min-confidence` | Only show facts at or above this confidence level (0.0–1.0). Good for filtering out unverified claims. |

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

### `hv decide` — Record a decision

Decisions are for choices you've made — not just facts, but the *why* behind
them. You can link a new decision to an older one it replaces, so you always
have a clear trail of what changed and why.

```
hv decide <content> [--rationale TEXT] [--supersedes ID]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The decision, stated clearly. Required. |
| `--rationale` | Why this decision was made. Optional but strongly recommended — future you will thank you. |
| `--supersedes` | The ID of a previous decision this replaces. The old decision stays on record; this one is linked to it. |

**Examples:**
```bash
# Minimal — just the decision
./hv decide "Use Tailscale SSH for inter-node access"

# With rationale
./hv decide "Use Tailscale SSH for inter-node access" \
    --rationale "Zero-config, auth handled by the tailnet, nothing to maintain"

# Replacing a previous decision
./hv decide "Install Tailscale inside WSL — each node gets its own IP" \
    --rationale "Cleaner than portproxy, WSL appears as its own tailnet machine" \
    --supersedes 5

# All options together
./hv decide "peers.json must use WSL Tailscale IPs, not Windows host IPs" \
    --rationale "WSL gets its own 100.x — Windows host IP is irrelevant to WSL daemon" \
    --supersedes 3
```

---

### `hv retract` — Correct a fact you got wrong

When a stored fact turns out to be wrong, use `retract` to say so. The fact
isn't deleted — HiveMind keeps a record of everything — but it's marked as
retracted and won't show up in normal search results. Think of it as
"this was wrong" rather than "this never happened."

```
hv retract <fact_id> [--reason TEXT] [--source SOURCE] [--owner]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `fact_id` | The ID of the fact to retract. Get it from `hv search` output. Required. |
| `--reason` | Why you're retracting it. Saved for reference. |
| `--source` | Who is doing the retracting. Defaults to `manual`. |
| `--owner` | Mark this as an authoritative retraction. Use when the fact is definitively wrong, not just uncertain. Immediately drives confidence to the floor. |

**Examples:**
```bash
# Minimal — fact ID only
./hv retract 4

# With a reason
./hv retract 4 --reason "Was a test probe, not a real observation"

# With reason and source
./hv retract 7 \
    --reason "Portproxy is no longer used — architecture changed" \
    --source "hermes:primary/claude-sonnet/abc12345"

# Owner retraction — authoritative, immediately floors confidence
./hv retract 12 --reason "Definitively wrong" --owner

# All options together
./hv retract 15 \
    --reason "Superseded by Tailscale-in-WSL architecture" \
    --source "claude-code" \
    --owner
```

---

### `hv entity` — Track named things

Entities are named anchors — a person, a project, a concept — that you can
attach facts to. Instead of hunting through search results, you can ask
"what do we know about X?" and get everything linked to it in one place.

```
hv entity {add,list,show,link} [options]
```

**Sub-commands:**

| Sub-command | What it does |
|---|---|
| `add` | Create a new entity. Needs `--name` and `--type` (e.g. `person`, `project`, `concept`). Optionally add metadata with `--attr` as a JSON object. |
| `list` | List all entities. |
| `show` | Show an entity and all facts linked to it. Needs `--name`. |
| `link` | Attach a fact to an entity. Needs `--name` and `--fact-id`. Optionally set `--confidence` to indicate how strongly the fact relates. |

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
./hv entity link --name "HiveMind" --fact-id 42

# link — with confidence score
./hv entity link --name "HiveMind" --fact-id 42 --confidence 0.9
```

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

### `hv doctor` — Check that your node is healthy

Runs a single set of checks over your local node and tells you whether anything
needs attention. It looks at six things:

- **authenticity** — your copy of HiveMind matches the signed official release
- **journal** — your node's history is intact and unbroken from the start
- **database** — the local lookup index is in step with that history
- **hygiene** — whether duplicate or obsolete facts have built up
- **sync-daemon** — whether the background sync service is running
- **peers** — whether your peer nodes are reachable and in sync

```
hv doctor
hv doctor --format json    # machine-readable, for scripts and monitoring
hv doctor --fix            # take the one safe remedial action (clear orphan daemons)
```

The `sync-daemon` check also flags **orphan daemons** — a stale `hv sync daemon`
left running outside systemd (for example, the unit died but an old process still
holds the port and serves stale code, so the port answers while nothing is
actually managed). `hv doctor --fix` kills those orphans and restarts the managed
unit. Without `--fix`, doctor only reports — it never kills anything, so it stays
safe to run from cron.

Each check is marked healthy (✓), advisory (•), or failed (✗). The command exits
non-zero only when a check actually fails, so you can wire it into a cron job or a
monitoring probe and get alerted on real breakage, not on a peer being briefly
offline. A failed `authenticity` check right after an upgrade usually just means
the signed manifest has not caught up yet; pull the latest and re-run.

---

### `hv key` — This node's device identity

Each node is identified by an Ed25519 **device key**, not its hostname. The key
proves which device wrote an entry, so a peer cannot impersonate your node to
inflate confidence. Your `node_id` is the key's fingerprint, like
`k1:2a2110f3d8963a9e`.

```
hv key show          # show this node's device_id and public key
hv key init          # mint a device key (fresh install only)
```

A fresh install mints a key automatically. `hv key init` refuses to run on a node
that already has history under its hostname, because minting a key there would
split its identity; use `hv migrate-device-identity` for an existing node instead.
The private seed lives at `HIVE_HOME/.device-key` — keep it secret, never commit
or sync it. Share your `device_id` and public key with peers (they go in
`.peers.json`).

---

### `hv migrate-device-identity` — Move an existing node to a device key

A one-time, coordinated step that re-stamps an existing journal from hostname
`node_id`s to cryptographic `device_id`s.

```
hv migrate-device-identity --map map.json --dry-run   # preview
hv migrate-device-identity --map map.json             # apply
```

`map.json` is `{"hostname": "k1:device_id", ...}` covering every node, identical
on each. Because the re-stamp is deterministic, running it on every peer with the
same map produces byte-identical journals, so your nodes stay in sync with no
re-transfer. The runbook, per node: `hv key init --force` to mint the key, share
the resulting `device_id`, build the shared map, stop the sync daemons, run this
on each node, confirm `hv merkle` roots match, then restart. Your old journal is
backed up to `journal.bak.device-id.<timestamp>/`.

---

### `hv rebuild` — Fix the local database

If your local database looks wrong or out of date, `rebuild` resets it from
scratch. It's safe to run any time — your data won't be lost.

Also useful after pulling in entries from a peer node, or if HiveMind exited
unexpectedly.

```
hv rebuild
```

---

### `hv merkle` — Diagnose sync problems

Shows a fingerprint of your current data. If two nodes show the same
fingerprint, they're in sync. If they differ, `hv sync now` will sort it out.

You don't normally need to run this — `hv sync` handles it automatically. It's
here for when you're troubleshooting and want to see exactly where two nodes
diverge.

```
hv merkle
```

---

### `hv sync` — Sync with peer nodes

Keeps your node up to date with peers — pulling in any facts they have that you
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

**`.peers.json` format:**
```json
{
  "bind": "0.0.0.0",
  "port": 9876,
  "peers": [
    {
      "url": "http://100.64.0.2:9876",
      "node_id": "node-b"
    }
  ]
}
```

- `peers[].url` — your peer's address. Use the WSL Tailscale IP (run `tailscale ip` on the peer to get it).
- `peers[].node_id` — a label for logs. Optional but helpful.
- `bind` and `port` — what address and port to listen on. Defaults are fine for most setups.

This file is not synced to git — it's specific to each machine.

**Examples:**
```bash
# Sync now — one-shot manual sync
./hv sync now

# Run the daemon manually (the background service does this automatically)
./hv sync daemon

# Check background service status
systemctl --user status hive-sync

# Watch sync logs live
journalctl --user -u hive-sync -f

# Restart the background service (e.g. after editing peers.json)
systemctl --user restart hive-sync
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `HIVE_HOME` | The folder containing `hv` | Where your data lives. Override to point `hv` at a different location. |
| `HIVE_NODE_ID` | The device-key fingerprint, else the hostname | Overrides this node's identity. Normally a node identifies by its Ed25519 device key (see `hv key`); set this only to force an identity, e.g. to run two separate hive instances on one machine. |
| `HIVE_NODE_LABEL` | Your machine's hostname | A human-friendly display label shown next to the `device_id` in `hv stats` and sync logs. Cosmetic; does not affect identity. |
| `HIVE_NOW` | System clock | For testing only — pins the clock to a fixed time so results are predictable. |

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
two.

**Examples:**
```
hermes:primary/claude-sonnet/abc12345    # Hermes, main session
hermes:subagent/claude-haiku/xyz99999    # Hermes running a subagent
claude-code                              # Claude Code
manual                                   # You, typing directly
```
