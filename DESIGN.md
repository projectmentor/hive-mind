# Hive Mind Design Document

## Overview

Institutional memory as observable middleware for heterogeneous AI agents.

## Core Architecture

```
~/.hermes/hive-mind/
├── store.db          # SQLite main store
├── journal/          # Append-only event log (YYYY-MM-DD.jsonl)
├── DESIGN.md         # This file
├── hv                # CLI executable
└── skills/           # Per-agent integration skills
    ├── hermes/
    ├── claude-code/
    └── codex/
```

## Phase 1 Scope (MVP)

### 1. Event Journal (Write Path)

Every memory operation appends to a daily journal file before processing:

```json
{"ts": "2026-06-02T10:30:00Z", "agent": "hermes", "op": "remember", "data": {...}}
{"ts": "2026-06-02T10:30:01Z", "agent": "claude-code", "op": "decide", "data": {...}}
```

Benefits:
- Concurrent writes safe (append-only)
- Full audit trail
- Crash recovery (replay journal)
- Observable event stream

### 2. SQLite Store (Read Path)

```sql
-- Core tables
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    tags TEXT, -- JSON array
    importance REAL DEFAULT 0.5,
    source_agent TEXT NOT NULL,
    source_session TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trust_score REAL DEFAULT 1.0,
    last_accessed TIMESTAMP,
    access_count INTEGER DEFAULT 0
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    rationale TEXT,
    superseded_by INTEGER REFERENCES decisions(id),
    source_agent TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT, -- person, project, concept
    attributes TEXT, -- JSON
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP
);

CREATE TABLE entity_facts (
    entity_id INTEGER REFERENCES entities(id),
    fact_id INTEGER REFERENCES facts(id),
    confidence REAL DEFAULT 1.0,
    PRIMARY KEY (entity_id, fact_id)
);

-- Indexes for common queries
CREATE INDEX idx_facts_tags ON facts(tags);
CREATE INDEX idx_facts_source ON facts(source_agent, created_at);
CREATE INDEX idx_facts_trust ON facts(trust_score);
CREATE INDEX idx_decisions_active ON decisions(id) WHERE superseded_by IS NULL;
```

### 3. CLI Interface

```bash
# Write operations (append to journal)
hv remember "David prefers parallel delegation over sequential" --tags workflow,preference
hv decide "Use RealSparkz for flexible leads platform" --supersedes 142
hv entity "David Faith" --type person --attr '{"role": "real estate agent"}'

# Read operations (query store)
hv search "parallel delegation"
hv facts --agent hermes --recent 10
hv decisions --active
hv entity "David Faith"

# Maintenance
hv compact              # Process journal into store
hv trust decay --days 30   # Decay old facts
hv stats                # Memory usage stats
```

### 4. Hermes Integration Skill

```python
# ~/.hermes/hive-mind/skills/hermes/hive_memory.py
from hermes_tools import subprocess_run
import json

class HiveMemoryProvider:
    """Memory provider that delegates to hive-mind CLI"""
    
    def add(self, content: str, target: str = "memory") -> dict:
        # Translate Hermes memory() call to hv CLI
        result = subprocess_run(
            ["hv", "remember", content, "--source", "hermes", "--tags", target],
            capture_output=True
        )
        return {"success": result.returncode == 0}
    
    def search(self, query: str) -> list:
        result = subprocess_run(
            ["hv", "search", query, "--format", "json"],
            capture_output=True
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        return []
```

## Design Decisions

### Why SQLite + Journal?

- **SQLite**: Fast reads, complex queries, FTS5 search
- **Journal**: Concurrent writes, crash recovery, event sourcing
- **Together**: Write to journal (fast), async compact to SQLite

### Why CLI First?

- Works with any agent that can shell out
- No dependencies on agent internals
- Easy to test/debug
- Observable (just `tail -f journal/2026-06-02.jsonl`)

### Why Not Start with MCP?

- MCP requires server management
- Skills are simpler to prototype
- We can add MCP server in Phase 5 once protocol stabilizes

## Phase 1 Deliverables

1. **hv CLI** - Basic CRUD operations
2. **SQLite schema** - Facts, decisions, entities
3. **Journal format** - Append-only JSONL
4. **Hermes skill** - Memory provider integration
5. **Basic TUI** - Browse facts/decisions (using textual)

## Success Criteria

- [ ] Multiple agents can write concurrently (via journal)
- [ ] Facts are tagged and searchable
- [ ] Decisions have supersedence chains
- [ ] Trust scores decay over time
- [ ] Hermes memory() calls flow through hive
- [ ] Human can browse memory via TUI

## Future Phases

- Phase 2: Skills for Claude Code, Codex
- Phase 3: Consolidation engine (dreaming)
- Phase 4: Web dashboard
- Phase 5: MCP server

## Open Questions

1. Should we version facts (keep history) or just track current state?
2. How to handle conflicting facts from different agents?
3. Should entities be first-class (separate table) or just tags?
4. Trust decay formula - linear, exponential, or power law?

## References

- nexus9888/hermes-memory-skills - Dreaming consolidation pattern
- kiranklabs/hermes-memory-wiki - Index + supersedence pattern
- Letta - Agent autonomy patterns
- Event sourcing - Journal/compact pattern