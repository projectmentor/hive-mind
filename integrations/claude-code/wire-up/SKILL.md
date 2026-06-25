---
name: wire-up
description: Self-provision a tool or agent from its Hive-Mind "cell" via the `hv wire` CLI — look up the recipe + credential, run the platform-correct setup, and verify. Use when you (or the user) need to wire this device up to a tool/service (Cloudflare, Vultr, chrome-devtools-mcp, the Lofty API, …) hands-off, or to list/inspect/publish wiring recipes.
when_to_use: You need a tool/service wired up on this device and a cell may already describe it; the user says "wire up X" / "set up X" / "connect to X"; you want to see what wiring recipes exist; or you're authoring a new recipe.
---

# wire-up — self-provision tools & agents from cells

Hive Mind models each wireable thing as a **cell** (a `kind:tool` recipe or a `kind:agent`
foreign-config integration). `hv wire` resolves a cell and provisions it: for tools it ensures the
required credential, runs the platform-correct steps, and runs the cell's `verify`; for agents it
reconciles the foreign config (e.g. Claude Code hooks). A **comb** is a named collection of cells.

## Commands
```
hv wire <name>                 # provision a cell (auto-dispatch by its kind)
hv wire --comb <name>          # provision every cell in a comb
hv wire --list [--kind tool|agent]   # list cells (and combs)
hv wire --show <name>          # print a cell/comb definition (recipe + gotchas + verify)
hv wire --add <file.json>      # publish a cell/comb to the hive (syncs to your other devices)
hv wire <name> --env-file PATH # read tool credentials from a dotenv file (default ~/.claude/.env)
```

## How to use it
1. **See what exists / read the recipe first:** `hv wire --list`, then `hv wire --show <name>` to
   read its `obtain` instructions, `gotchas`, and what credential it `requires`.
2. **Make the credential available — never via chat.** If a tool `requires` a secret (e.g.
   `CLOUDFLARE_API_TOKEN`, `LOFTY_API_TOKEN`), the user adds it to `~/.claude/.env` themselves
   (e.g. tell them to run `! echo 'NAME=...' >> ~/.claude/.env`). Do **not** ask them to paste a
   secret into the conversation. Then run `hv wire <name>` — the value flows file→tool, never the
   transcript. (Phase 2 will seal these into encrypted capsules instead of `.env`.)
3. **Provision + verify:** `hv wire <name>`. It is idempotent — if `verify` already passes it does
   nothing. A clear message tells you what was missing, what ran, and whether verify passed.
4. **Author a recipe:** write a cell JSON (`{name, kind:"tool", requires:[…], spec:{obtain,
   platforms:{linux|wsl|darwin:{steps:[…]}}, gotchas}, verify:{http|cmd, headers?, expect}}`) and
   `hv wire --add it.json`. Built-in cells (like the `claude` agent) resolve before journaled ones.

## Notes
- `kind:agent` cells reconcile foreign config (Claude Code hooks today); `hv wire claude` is the
  modern form of the old `hv doctor wire-agent` (kept as a hidden alias).
- If a tool's `requires` credential is missing, `hv wire` tells you exactly which one and where to
  put it — surface that to the user rather than guessing.
- Canonical cells live in the repo's `cells/` dir; check there and via `hv wire --list`.
