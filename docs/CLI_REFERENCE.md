# HiveMind CLI Reference

`hv` is the HiveMind command-line interface. All agent integrations shell out
to it. It reads and writes the local journal and SQLite store, and coordinates
with peer nodes via the sync daemon.

## Global

```
./hv <command> [options]
```

The `hv` binary reads `HIVE_HOME` (env) to locate the journal and store. If
unset it defaults to the directory containing `hv` itself.

`NODE_ID` is derived from `socket.gethostname()` unless overridden by the
`HIVE_NODE_ID` environment variable. Two instances on the same host that need
distinct identities must set `HIVE_NODE_ID`.

---

## Commands

### `hv remember`

Store a fact in the corpus.

```
hv remember <content> [--tags TAGS] [--source SOURCE] [--importance N] [--gate]
```

| Argument | Description |
|---|---|
| `content` | The fact text (required) |
| `--tags` | Comma-separated tags, e.g. `infrastructure,todo` |
| `--source` | Source identity string. Convention: `hermes:primary/<agent>/<session8>` or `claude-code`. Default: `manual` |
| `--importance` | Numeric importance hint (telemetry only, not used in confidence projection) |
| `--gate` | Apply the content-neutral salience gate; writes that fail the gate are silently skipped |

**Output:** `Remembered as fact #N (confidence C, K identity)` or `Already known (fact #N, confidence C)` on exact-content dedup.

**Confidence:** Derived from distinct sources asserting identical content: `confidence(n) = 0.90 * (1 - 0.5^n)`. A single-source write lands at 0.45. The same content from a second distinct source raises it to 0.675. Self-repeating (same source, same content) is idempotent — confidence does not move.

**Examples:**
```bash
./hv remember "SSH DefaultShell on gregorius must use C:\ssh-wsl.bat" \
    --tags infrastructure,gotcha --source "hermes:primary/claude-sonnet/abc12345"

./hv remember "EntitlementService is the single source of truth for plan resolution" \
    --tags architecture,constraint --source "claude-code"
```

---

### `hv search`

Full-text search the corpus.

```
hv search <query> [--format {text,json}] [--min-confidence N]
```

| Argument | Description |
|---|---|
| `query` | FTS5 search query. AND is default; use OR explicitly; `"exact phrase"` for phrases |
| `--format` | Output format: `text` (default, human-readable) or `json` (machine-readable) |
| `--min-confidence` | Filter out facts below this confidence (0.0–1.0). Default: 0.0 (show all) |

**JSON output fields per fact:** `id`, `content`, `tags`, `confidence`, `source_agent`, `created_at`

**Examples:**
```bash
./hv search "openssh"
./hv search "phase a" --format json
./hv search "portproxy" --min-confidence 0.5
```

---

### `hv decide`

Record a decision with optional rationale and supersession chain.

```
hv decide <content> [--rationale TEXT] [--supersedes ID]
```

| Argument | Description |
|---|---|
| `content` | The decision statement (required) |
| `--rationale` | Why this decision was made |
| `--supersedes` | ID of a prior decision this replaces. Links are journal-identity refs so they survive cross-node merge |

**Examples:**
```bash
./hv decide "Use stdlib http.server for sync daemon, not FastAPI" \
    --rationale "No pip, zero-dep MVP on a WireGuard Tailnet"

./hv decide "Use netsh portproxy for WSL/Tailscale bridging" \
    --rationale "WSL is in NAT mode; Tailscale is on Windows host" \
    --supersedes 3
```

---

### `hv retract`

Record negative evidence against a fact (soft-forget). Append-only — does not
delete the journal entry, appends a `retract` event that the confidence
projection excludes.

```
hv retract <fact_id> [--reason TEXT] [--source SOURCE] [--owner]
```

| Argument | Description |
|---|---|
| `fact_id` | Local fact ID to retract |
| `--reason` | Why this fact is being retracted |
| `--source` | Retractor identity |
| `--owner` | Owner/governance retraction — carries higher authority, drives to the confidence floor |

**Examples:**
```bash
./hv retract 4 --reason "Test probe, not a real fact" --source "hermes:primary/claude-sonnet/abc12345"
./hv retract 7 --reason "Superseded by EntitlementService decision" --owner
```

---

### `hv entity`

Manage named entities and their fact links.

```
hv entity {add,list,show,link} [options]
```

| Sub-command | Description |
|---|---|
| `add` | Create a new entity (`--name`, `--type`, `--attr` JSON) |
| `list` | List all entities |
| `show` | Show an entity and its linked facts (`--name`) |
| `link` | Link a fact to an entity (`--name`, `--fact-id`, `--confidence`) |

Entity types: `person`, `project`, `concept` (or any string).

**Examples:**
```bash
./hv entity add --name "EntitlementService" --type concept \
    --attr '{"project":"realsparkz"}'
./hv entity link --name "EntitlementService" --fact-id 36 --confidence 0.9
./hv entity show --name "EntitlementService"
```

---

### `hv stats`

Show corpus statistics for this node.

```
hv stats
```

**Output includes:** node ID, fact count, avg confidence, decision count, entity count, journal entry count (by node), top tag groups.

---

### `hv rebuild`

Rebuild `store.db` from the journal. Safe to run at any time — the journal is
the source of truth; SQLite is a derived cache.

```
hv rebuild
```

Run after: pulling foreign journal entries, a crash, or a schema migration.
Also recomputes the confidence projection (`_recompute_confidence`) so all
facts reflect the current distinct-source set.

---

### `hv merkle`

Show the Merkle index over the journal. Used to diagnose sync state.

```
hv merkle
```

**Output:** global root hash + per-node chunk hashes. If two nodes show the
same root hash, their journals are byte-identical.

---

### `hv sync`

Bidirectional P2P sync with configured peers. Reads `.peers.json`.

```
hv sync now
hv sync daemon [--interval SECONDS]
```

| Sub-command | Description |
|---|---|
| `now` | One-shot sync round with all peers. Exits when complete. |
| `daemon` | Serve the sync HTTP endpoints on `:9876` AND run an outbound `sync now` every `interval` seconds (default 300). Blocks. |

**`.peers.json` format:**
```json
{
  "peers": [
    {"url": "http://100.114.200.119:9876", "node_id": "gregorius"}
  ],
  "bind": "0.0.0.0",
  "port": 9876
}
```

**Examples:**
```bash
./hv sync now
./hv sync daemon
HIVE_NODE_ID=node-b ./hv sync daemon   # override node identity
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HIVE_HOME` | Directory of `hv` binary | Path to journal/ and store.db |
| `HIVE_NODE_ID` | `socket.gethostname()` | Override node identity (required when two instances share a hostname) |
| `HIVE_NOW` | wall clock | Pin clock for deterministic decay/confidence in tests. ISO8601 or Unix timestamp. |

---

## Source Identity Convention

The `--source` argument is the shared contract between agent integrations and
the confidence model. Format:

```
<app>:<context_class>/<instance>/<session8>
```

| Field | Values | Weight in Phase B2 |
|---|---|---|
| `context_class` | `primary` | 1.0 |
| | `subagent` | 0.5 |
| | `cron` | 0.3 |
| `instance` | agent profile name | used for Phase B3 self-quarantine |
| `session8` | first 8 chars of session ID | distinguishes sessions, NOT counted separately |

**Critical:** `_recompute_confidence` counts distinct `(node_id, app, instance)` — NOT the full
source string. The same agent across two sessions is one corroborating identity. Do not change
this format without coordinating with `_recompute_confidence` in `hv`.

**Examples:**
```
hermes:primary/claude-sonnet/abc12345
hermes:subagent/claude-haiku/xyz99999
hermes:cron/default/cron0001
claude-code                              # v0 flat — valid, counts as one identity
claude-code:primary/opus/abc12345       # v1 structured (D0 upgrade)
manual                                   # human direct write
```
