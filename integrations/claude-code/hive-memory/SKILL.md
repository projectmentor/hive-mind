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
- Remember: `~/projects/hive-mind/hv remember "<fact>" --tags <t1,t2> --source claude-code [--channel introspect]`
- Decide:   `~/projects/hive-mind/hv decide "<decision>" --rationale "<why>" --informed <ref> [<ref>…]`
  (`<ref>` = the `sid` field — `h:` + 10 hex, e.g. `h:3f9a1c0b2d` — of each fact/decision you
  retrieved and relied on; the raw `ref` `node_id:seq` also works. Both are stable across nodes and
  rebuilds. Bare local ids like `118`/`d17` are rowids that drift on every rebuild: deprecated, warned,
  and dropped at the next MAJOR — never pass one)
- Propose:  `~/projects/hive-mind/hv propose "<hypothesis>" --tags <t1,t2> --source claude-code`
  (an IDEA, not a fact: starts at 0.00 and can earn confidence only from other identities' sense-channel
  evidence — use it for "perhaps X relates to Y", never for something observed)
- Weigh in: `~/projects/hive-mind/hv remember "<what you observed>" --supports <sid>` (or `--contradicts <sid>`)
  when an observation bears on an idea or fact, e.g. one the digest lists as open. If it comes from
  your reasoning rather than an observation, add `--channel introspect`. One relationship per
  write; your own support of your own idea doesn't count
- Sync:     `~/projects/hive-mind/hv sync now`

**ALWAYS pass `--source claude-code`** on `remember` so the hive can distinguish your writes
from other agents' — this is what makes corroboration and provenance work.

## When to WRITE (be disciplined — the corpus is shared and permanent)
Write durable, checkable, reusable knowledge:
- **Decisions** (`hv decide`) with rationale — architectural / process choices. **Pass `--informed`
  with the refs of the facts you searched and relied on**: that is how the hive later learns which
  knowledge proved useful once the decision's outcomes are recorded.
- **Outcomes / results** of actions ("did X → got Y") — checkable ground truth. When the action
  carried out a recorded decision, write the outcome with `--outcome-of <decision sid> --polarity 1|0|-1`
  so the decision earns an outcome score and the facts it relied on earn utility. Only observed
  outcomes count; if it is your own assessment rather than something observed, add `--channel introspect`.
- **Corrections** — something was wrong and is now right. Replace the old fact rather than leaving
  both live: `hv remember "<correction>" --resolves <sid of the wrong fact>` (soft-retracts it).
- **Constraints / preferences / commitments** that shape future work.
- **New entities / relationships** worth remembering.
- **Conclusions, analysis, plans**: only if worth finding later, and always with
  `--channel introspect`. They are recorded, searchable and linkable, but count 0 toward
  confidence until someone's observation supports them. Two agents that read the same code and
  reach the same conclusion are one line of reasoning twice, not two observations.

Do **NOT** write your chain-of-thought (the journal is permanent, syncs to every device and is
searched on every turn), restatements of things already in the corpus, or untestable speculation.
A hypothesis worth testing goes through `hv propose` as an **idea** (it earns confidence from
evidence; it cannot borrow it from you). Tags such as `observation` or `confirmed` help readers
weigh a fact but never change how much it counts as evidence: **the channel is what counts**. Leave
it off for what you observed; pass `--channel introspect` for what you concluded. Never assert your
own confidence/trust number; confidence is derived, not declared.

**Search before you write** (`hv search`). If the fact is already there, don't rewrite it — its
confidence rises from *independent* corroboration, not from you repeating it. **Never write back
something you just read from the hive this session** (that's an echo, not evidence).

You are the salience judge — apply the rubric above. As a mechanical floor you may add `--gate` to
`remember` (skips trivially short / bare-question / greeting writes); it never overrides your judgment.

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
