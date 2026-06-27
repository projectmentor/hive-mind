# `hv` architecture — why one big file, and how we'd split it

`hv` is a single ~5,400-line executable Python script with ~25 subcommands. New contributors
reasonably ask: shouldn't this be a package? This note records the deliberate decision to **keep it
monolithic for now**, the trade-offs, and the path we'd take if/when we split it.

## Why it is one file

- **It is a signed artifact.** `verify.json` pins the sha256 of every source file and `hv verify`
  proves an install is official. One entrypoint file is the simplest thing to reason about as a
  root-of-trust unit. (Crypto primitives are already separate modules — `ed25519.py`, `x25519.py`,
  `chacha20poly1305.py`, `merkle.py` — and are pinned too; the manifest already spans multiple files,
  so signing is not itself a reason to stay monolithic.)
- **Trivial distribution.** Nodes update with `git pull` / `hive-mind update`; `hv` is symlinked onto
  `PATH`. No packaging, no install step, no import-path setup, no virtualenv. It runs on the Python 3
  stdlib alone.
- **One dispatch surface.** `argparse` subparsers + a single dispatch chain at the bottom. Everything
  a reader needs is `grep`-able in one place.
- **Pervasive shared state.** A global SQLite connection (`get_conn()`), the journal/DB path
  constants, and the `_governance_state()` projection are touched by almost every command. That
  coupling makes a clean split non-trivial (see below), so the cost/benefit hasn't yet favored it.

## Cons of staying monolithic

- Hard to navigate; commands that are conceptually unrelated live next to each other.
- Harder to unit-test a single concern in isolation (tests drive the whole CLI via subprocess).
- Merge-conflict prone when several changes land at once.
- A new reader has to load the whole file to find a seam.

## If we split it (its own PR, not this one)

Extract the **lowest-coupling** concerns first, keeping `hv` as the entrypoint that imports them, and
keep every extracted module inside the signed manifest. Rough order of safety:

1. **Confidence / corroboration scoring** (`_confidence_for`, `_identity_weight`,
   `_content_confidence`, …) — ~200 lines, mostly pure functions over parsed entries.
2. **Advisories** read/format — small and self-contained (see [ADVISORIES.md](ADVISORIES.md)).
3. **Journal I/O** (`append_journal`, `append_foreign_entries`, `init_db`, `persist_fact`) — cohesive,
   but this is where the global-connection coupling bites.
4. **Governance & membership** (`owner`/`group`/`config` commands + `_governance_state`) — the largest
   and most interdependent block; do it last.

**Blockers to resolve first:** the global `get_conn()` connection and the on-demand
`_governance_state()` projection are implicit dependencies of most commands. Before any large split,
thread the DB connection explicitly (or wrap it in a small context object) and make the governance
projection an explicit input rather than a recomputed global. Until that refactor is done, splitting
mostly moves the coupling around rather than removing it.

**Bottom line:** the monolith is a conscious trade-off favoring a simple, signed, dependency-free
distribution. Revisit when the navigation/testing cost clearly outweighs that simplicity — and when
it does, start with the pure-function seams above, not the governance core.
