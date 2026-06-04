---
name: hive-memory
description: Read from and write to the shared Hive Mind corpus (the `hv` CLI) — append-only institutional memory shared across agents (Hermes, Claude Code, …) and machines. Use to record decisions/outcomes/constraints and to check what the hive already knows before acting — especially on errors, uncertainty, or a named project/person/system.
when_to_use: A decision or outcome is reached; you hit an error or gotcha; you're unsure or about to assert something checkable; the user references a project/person/system that may be in the corpus; or the user says to use hive-mind / log to the hive.
---

# Hive Mind memory

Hive Mind is a shared, append-only memory corpus used by multiple AI agents across machines,
synced peer-to-peer. You interact with it through the `hv` CLI. **Confidence in a fact is a
derived projection** over the corpus — you never set it; it rises only when *distinct
independent sources* assert the same thing. So repetition is worthless; independent
corroboration and checkable outcomes are what matter.

## The CLI
`hv` lives at `~/projects/hive-mind/hv` — an **absolute path that works from any directory**
(do not `cd`). Run it via the shell:
- Search:   `~/projects/hive-mind/hv search "<query>"`   (add `--format json` for structured results)
- Remember: `~/projects/hive-mind/hv remember "<fact>" --tags <t1,t2> --source claude-code`
- Decide:   `~/projects/hive-mind/hv decide "<decision>" --rationale "<why>"`
- Sync:     `~/projects/hive-mind/hv sync now`

**ALWAYS pass `--source claude-code`** on `remember` so the hive can distinguish your writes
from other agents' — this is what makes corroboration and provenance work.

## When to WRITE (be disciplined — the corpus is shared and permanent)
Write durable, checkable, reusable knowledge:
- **Decisions** (`hv decide`) with rationale — architectural / process choices.
- **Outcomes / results** of actions ("did X → got Y") — checkable ground truth.
- **Corrections** — something was wrong and is now right.
- **Constraints / preferences / commitments** that shape future work.
- **New entities / relationships** worth remembering.

Do **NOT** write: your chain-of-thought, restatements of things already in the corpus, or
speculation/opinion. Mark epistemic status with `--tags` (e.g. `observation`, `confirmed`,
`speculation`) so readers can weigh it — but **never assert your own confidence/trust number**;
confidence is derived, not declared.

**Search before you write** (`hv search`). If the fact is already there, don't rewrite it — its
confidence rises from *independent* corroboration, not from you repeating it. **Never write back
something you just read from the hive this session** (that's an echo, not evidence).

## When to READ
Search the hive — `hv search "<terms>"` — when:
- you start work on a project/person/system (search its name),
- you hit an **error or gotcha** (search the error text — someone may have left the fix),
- you're **uncertain** or about to assert something checkable,
- the user mentions a named entity that might be in the corpus.

Treat results as **signals with provenance**, not truth — note the confidence, the number of
sources, and which node/agent said it. If the corpus holds **conflicting** facts on a topic,
surface **both** with their provenance and hold the tension; do not silently pick a winner.
Prefer observed outcomes over interpretations, and verify when you can.

## Why the discipline
A shared memory that records everything — guesses, repetition, self-talk — becomes a confident
echo chamber. Writing only checkable decisions/outcomes, checking novelty first, and surfacing
conflict instead of resolving it are what keep the hive sane.
