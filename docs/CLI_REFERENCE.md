# HiveMind CLI Reference

`hv` is the HiveMind command-line tool. It's how you (and AI agents) read and
write the shared knowledge corpus — storing facts, recording decisions, searching
memory, and syncing with peer nodes.

Everything `hv` writes goes to the local **journal** first (a plain JSONL file),
and the **SQLite store** is rebuilt from it. This means the journal is the real
database — SQLite is just a fast index. You can always recover by running
`hv rebuild`.

---

## Quick reference

| Command | What it does |
|---|---|
| `hv remember` | Store a fact |
| `hv search` | Search stored facts |
| `hv decide` | Record a decision |
| `hv retract` | Walk back a fact you got wrong |
| `hv entity` | Track named things (people, projects, concepts) |
| `hv stats` | See corpus health at a glance |
| `hv rebuild` | Rebuild the database from the journal |
| `hv merkle` | Check sync state between nodes |
| `hv sync` | Sync facts with peer nodes |

---

## Running `hv`

```bash
cd ~/projects/hive-mind
./hv <command> [options]
```

`hv` figures out where your data lives using the `HIVE_HOME` environment
variable. If you don't set it, it defaults to the folder containing `hv` itself
— so running it from `~/projects/hive-mind` just works.

Your **node identity** is your hostname (`socket.gethostname()`). On a fresh
machine this is set automatically. If you ever need to run two hive instances
on the same machine with separate identities, set `HIVE_NODE_ID` to tell them
apart.

---

## Commands

---

### `hv remember` — Store a fact

Use this to save anything worth keeping: observations, decisions, constraints,
gotchas, status updates. Facts are searchable, tagged, and given a confidence
score that rises when multiple independent sources agree on the same content.

```
hv remember <content> [--tags TAGS] [--source SOURCE] [--importance N] [--gate]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The text of the fact you want to store. Put it in quotes. Required. |
| `--tags` | Comma-separated labels to help you find it later, e.g. `--tags infrastructure,todo`. No spaces. |
| `--source` | Who or what is asserting this fact. Used by the confidence model to detect independent corroboration. Defaults to `manual` if not set. See Source Identity below. |
| `--importance` | A numeric hint (e.g. `0.8`). Stored as telemetry but not currently used in ranking. |
| `--gate` | Run the salience gate before writing. Facts that don't pass the structural check are silently dropped. Useful for high-volume agent writes where you want to filter noise. |

**What you get back:**

- `Remembered as fact #N (confidence C, K identity)` — written successfully
- `Already known (fact #N, confidence C)` — exact same content already exists; no duplicate created

**How confidence works:**

A brand-new fact from one source starts at **0.45**. If a second completely
independent source (different node, different agent, different app) stores the
exact same content, confidence rises to **0.675**. A third pushes it to **0.79**.
The ceiling is **0.90**. Writing the same fact from the same source repeatedly
doesn't move confidence — it's idempotent.

The formula is: `confidence(n) = 0.90 × (1 − 0.5ⁿ)` where n = number of
distinct sources.

**Examples:**
```bash
# Simple fact with tags
./hv remember "Tailscale SSH replaces Win OpenSSH on both nodes" \
    --tags infrastructure,architecture

# Agent write with source identity
./hv remember "EntitlementService is the single source of truth for plan resolution" \
    --tags architecture,constraint --source "claude-code"

# Hermes agent write (structured source identity)
./hv remember "Sync daemon binds 0.0.0.0:9876, no portproxy needed" \
    --tags infrastructure,confirmed \
    --source "hermes:primary/claude-sonnet/abc12345"
```

---

### `hv search` — Search stored facts

Full-text search across everything in the corpus. Returns facts ranked by
confidence (highest first). Supports the same boolean syntax as SQLite FTS5.

```
hv search <query> [--format {text,json}] [--min-confidence N] [--limit N]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `query` | What to search for. Multiple words default to AND (all must match). Use `OR` explicitly for either/or. Use `"quoted phrases"` for exact matches. |
| `--format` | How to display results. `text` (default) is human-readable. `json` gives you machine-readable output for piping to other tools. |
| `--min-confidence` | Only show facts at or above this confidence level (0.0–1.0). Useful for filtering out low-confidence noise. Default: 0.0 (show everything). |
| `--limit` | Maximum number of results to return. |

**JSON output fields per fact:** `id`, `content`, `tags`, `confidence`,
`source_agent`, `created_at`

**Examples:**
```bash
# Find anything about tailscale
./hv search "tailscale"

# Find facts about sync that are well-corroborated
./hv search "sync daemon" --min-confidence 0.5

# Pipe to jq for scripting
./hv search "portproxy" --format json | python3 -m json.tool

# Exact phrase
./hv search '"address already in use"'
```

---

### `hv decide` — Record a decision

Decisions are different from facts — they represent a choice made, with a
rationale. They show up in `hv stats` and can be linked to each other in a
supersession chain so you have a clear history of "we used to do X, then we
decided to do Y instead."

```
hv decide <content> [--rationale TEXT] [--supersedes ID]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `content` | The decision, stated clearly. Required. |
| `--rationale` | Why this decision was made. Optional but strongly recommended — future you will thank you. |
| `--supersedes` | The ID of a previous decision this replaces. Creates a linked chain in the journal so the history survives sync across nodes. |

**Examples:**
```bash
# New decision
./hv decide "Use Tailscale SSH instead of Win OpenSSH for inter-node access" \
    --rationale "Win OpenSSH requires portproxy, authorized_keys setup, and breaks on reboot. Tailscale SSH is zero-config and auth is handled by the tailnet."

# Decision that replaces a previous one
./hv decide "Install Tailscale inside WSL — each WSL gets its own 100.x IP" \
    --rationale "WSL Tailscale appears as its own tailnet machine, no portproxy needed" \
    --supersedes 5
```

---

### `hv retract` — Walk back a fact

When you find out a stored fact was wrong, use `retract` to record that. It
doesn't delete anything — the journal is append-only — but it appends a
retraction event that the confidence model uses to lower or floor the fact's
confidence, and excludes it from normal search results.

Think of it as "I was wrong about this" rather than "this never existed."

```
hv retract <fact_id> [--reason TEXT] [--source SOURCE] [--owner]
```

**Arguments:**

| Argument | What it does |
|---|---|
| `fact_id` | The numeric ID of the fact to retract. Get it from `hv search` output. Required. |
| `--reason` | Why you're retracting it. Stored in the journal for future reference. |
| `--source` | Who is doing the retracting. Defaults to `manual`. |
| `--owner` | Flag this as a governance/owner retraction — carries higher authority and drives the confidence to the floor immediately. Use for facts that are definitively wrong, not just questionable. |

**Examples:**
```bash
# Retract a test fact
./hv retract 4 --reason "Was a test probe, not a real observation"

# Owner retraction — authoritative, floors confidence immediately
./hv retract 12 --reason "Portproxy is no longer used — architecture changed" --owner
```

---

### `hv entity` — Track named things

Entities let you create named anchors (a person, a project, a concept) and link
facts to them. Useful when you want to ask "what do we know about X?" without
relying on text search alone.

```
hv entity {add,list,show,link} [options]
```

**Sub-commands:**

| Sub-command | What it does |
|---|---|
| `add` | Create a new entity. Needs `--name` (required), `--type` (e.g. `person`, `project`, `concept`), and optionally `--attr` (a JSON object of extra metadata). |
| `list` | Show all entities in the corpus. |
| `show` | Show a specific entity and all facts linked to it. Needs `--name`. |
| `link` | Connect a fact to an entity. Needs `--name`, `--fact-id`, and optionally `--confidence` (how strongly this fact relates to the entity). |

**Examples:**
```bash
# Create an entity for a key service
./hv entity add --name "HiveMind" --type project \
    --attr '{"repo":"projectmentor/hive-mind","status":"active"}'

# Link a fact to it
./hv entity link --name "HiveMind" --fact-id 42 --confidence 0.9

# See everything we know about it
./hv entity show --name "HiveMind"

# List all entities
./hv entity list
```

---

### `hv stats` — Corpus health at a glance

Shows a summary of everything in the corpus on this node.

```
hv stats
```

**Output includes:**
- Node ID and data directory
- Total facts and average confidence
- Number of active decisions
- Number of entities and their fact-link counts
- Total journal entries broken down by which node wrote them
- Top tag groups by frequency

Run this after a sync to confirm entries came across, or just to get oriented
at the start of a session.

---

### `hv rebuild` — Rebuild the database from the journal

Throws away `store.db` and rebuilds it from scratch using the journal files.
Safe to run any time. Use it after:

- Pulling journal entries from a peer (sync does this automatically)
- A crash or unexpected exit
- A schema migration
- Anything that makes you wonder if SQLite is out of sync with the journal

Also recomputes all confidence scores from scratch, so facts that gained new
corroborating sources since the last write will reflect updated confidence.

```
hv rebuild
```

No arguments. Just run it.

---

### `hv merkle` — Check sync state

Shows the Merkle tree hash of your journal. Used to diagnose sync problems.

```
hv merkle
```

**Output:** global root hash + per-node chunk hashes (each chunk = 100 journal
entries).

If two nodes show the **same root hash**, their journals are identical. If the
root hashes differ, `hv sync now` will figure out which chunks differ and pull
only the missing entries.

You don't need to run this manually in normal use — `hv sync now` calls it
internally. It's here for debugging when sync isn't behaving.

---

### `hv sync` — Sync with peer nodes

Pulls new journal entries from peers and pushes yours. Uses the Merkle tree to
figure out exactly which entries are missing on each side — only the delta is
transferred, not the whole corpus.

Peer addresses are configured in `.peers.json` in your `HIVE_HOME` directory.

```
hv sync now
hv sync daemon [--interval SECONDS]
```

**Sub-commands:**

| Sub-command | What it does |
|---|---|
| `now` | Do one sync round with all configured peers right now, then exit. Prints status per peer. |
| `daemon` | Start the sync server on `:9876` AND run a `sync now` automatically every N seconds (default: 300 = 5 minutes). Runs forever. The systemd service uses this. |

**`.peers.json` format:**
```json
{
  "bind": "0.0.0.0",
  "port": 9876,
  "peers": [
    {
      "url": "http://100.84.84.100:9876",
      "node_id": "gregorius"
    }
  ]
}
```

- `bind` — which interface the daemon listens on. `0.0.0.0` means all interfaces.
- `port` — which port. Default 9876.
- `peers[].url` — the sync daemon URL of a peer node. Use the WSL Tailscale IP (get it with `tailscale ip` on the peer).
- `peers[].node_id` — a human label for logs. Optional but helpful.

`.peers.json` is gitignored — it contains your Tailscale IPs and is per-machine.
Copy from `.peers.json.example` when setting up a new node.

**Examples:**
```bash
# One-shot sync (check if peers are up, pull any new entries)
./hv sync now

# Run the daemon manually (normally managed by systemd)
./hv sync daemon

# Check daemon status
systemctl --user status hive-sync

# Watch daemon logs live
journalctl --user -u hive-sync -f
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `HIVE_HOME` | Directory containing `hv` | Where to find `journal/` and `store.db`. Override to point `hv` at a different data directory. |
| `HIVE_NODE_ID` | `socket.gethostname()` | Override your node identity. Only needed if you're running two hive instances on the same machine that need distinct identities. |
| `HIVE_NOW` | System clock | Pin the clock to a fixed time. ISO8601 or Unix timestamp. Used in tests for deterministic confidence decay. Not needed in normal use. |

---

## Source identity convention

The `--source` argument tells the confidence model *who* is asserting a fact.
The model counts **distinct sources** — not word count, not repetition — to
determine how trustworthy a fact is. A fact that three independent agents all
independently assert is much more reliable than one agent saying the same thing
three times.

**Format:**
```
<app>:<context_class>/<instance>/<session8>
```

| Field | What it is | Examples |
|---|---|---|
| `app` | The tool or agent writing the fact | `hermes`, `claude-code`, `manual` |
| `context_class` | The authority level of this write | `primary` (full agent), `subagent` (delegated), `cron` (scheduled) |
| `instance` | The specific agent profile or instance name | `claude-sonnet`, `default`, `opus` |
| `session8` | First 8 chars of the session ID | `abc12345` — used to tell sessions apart in logs, NOT counted as a separate source |

**Important:** the confidence model counts distinct `(node_id, app, instance)`
tuples — not full source strings. The same Hermes agent across two sessions is
still one corroborating identity. Don't change this format without updating
`_recompute_confidence` in `hv`.

**Examples:**
```
hermes:primary/claude-sonnet/abc12345    # Hermes on EGMBL5A, primary session
hermes:subagent/claude-haiku/xyz99999    # Hermes spawning a subagent
hermes:cron/default/cron0001             # Hermes scheduled job
claude-code                              # Claude Code (flat format, still valid)
manual                                   # You, typing directly
```
