# Modules: developer guide

Module contract **1** (manifest field `contract: 1`, core 3.1), as of the **4.0** release. This is how you write, sign, and install a HiveMind module today. The
route-by-route API is [`MODULE_API.md`](MODULE_API.md). The names a module may introduce are
[`NAMESPACES.md`](NAMESPACES.md). The verbs are [`CLI_REFERENCE.md`](CLI_REFERENCE.md#hive-mind-module--install-and-manage-modules-21-linux-only).

Modules run on Linux with a user systemd manager. Documented here is what 4.0 ships; what it does not do is under
[Not in 4.0](#not-in-40).

## What a module is

A **module** is a signed, sandboxed add-on. It is a separate process on the same machine as the
sync daemon. It has its own Ed25519 device key and its own write quota. The owner admits that
device as a module under the operator's principal. The module reaches the hive only through the
loopback `/v1` API, never through the owner key. It reads the hive, and writes `fact`, `decision`,
`idea`, `entity` and `link` entries that it signs itself; the core verifies, gates and appends them.

Install with `hive-mind module install <name> --from <repo>`: one idempotent command that installs, updates or
leaves alone, with fixed exit codes and `--json` (see [`CLI_REFERENCE.md`](CLI_REFERENCE.md#hive-mind-module--install-and-manage-modules-21-linux-only)).
`hive-mind module add` is its older, non-idempotent form. There is no `hv module`.

## Trust model

A module runs code on the machine, so nothing is installed until the signed tree checks out.

1. **Publisher key, pinned on first use.** The module's `module.json` is signed by its publisher
   key in `module.json.sig` (Ed25519 over the exact bytes). The HiveMind release key
   `hivemind.pub` never signs a module. `add` shows the publisher key and pins it when you confirm
   (or when you pass `--publisher`). An `update` signed by a different key is refused. The
   fingerprint you compare is the first 16 hex of the sha256 of the public key.
2. **Owner admit.** The module's device is admitted with the module marker
   (`hive-mind group admit <device> --module <name> --principal <p>`). `hive-mind module add` on
   the owner machine does this itself. A module device does not vote in owner elections, and no
   sync peer is seeded for it.
3. **Two-step install without the owner key (C6, 2.3).** On a device that does not hold the owner
   key, `add` fetches, verifies, stages and mints the module's key, prints the owner's admit line
   (with the publisher fingerprint and the commit it verified), and **installs nothing**: no unit,
   no `.modules.json` row, no admit. The owner checks that commit and runs the line. When the
   admit has synced to this device, the same `add <name> --from <repo>` installs the staged tree,
   re-checked against the signed manifest and the digest recorded at the first run.
   `hive-mind module add --abort <name>` drops the staging and the key. `hv doctor --fix` never
   completes a pending add.

## The manifest

A module is a git repository whose `module.json` names every file with its sha256. The fetched
tree must hold exactly those files and no others. A field this node does not know is refused, not
ignored.

| Field | |
|---|---|
| `name` | 1 to 32 characters of `a-z`, `0-9` and `-`, starting with a letter, not ending in `-`. It prefixes `x-<name>:`. Required. |
| `version` | A string, up to 64 characters. Required. |
| `min_core` | The oldest core contract it runs on, a dotted version; refused on an older node. Default `0`. |
| `publisher` | The publisher's Ed25519 public key, base64. Required. |
| `files` | `{relative path: sha256 hex}` for every file, 1 to 500 of them, each at most 8 MiB. Paths must be relative, with no `..`, and may not be `module.json`, `module.json.sig` or `config`. Links are refused. Required. |
| `service` | Optional. `{"command": [argv…], "restart": "no" \| "on-failure" \| "always", "interval": seconds ≥ 60, "stop_grace": seconds 1–3600}`. `interval` makes it a systemd timer. `stop_grace` *(2.3)* renders `TimeoutStopSec` and `KillMode=mixed`. |
| `hooks` | Optional list of Claude Code events: `session-start`, `user-prompt`, `precompact`, `sessionend`, `notification`, `stop`. Each needs a `hooks/<event>` file in `files`. |
| `quota` | Optional `{per_hour, per_day, entry_bytes, lifetime}`. A manifest may ask for less than the defaults (60 / 500 / 16384 / 50000), never more. Only the owner raises a limit. |
| `config` | Optional `{key: default string}`, per-node defaults. Keys follow the `x-<name>:<key>` rule; values are at most 4096 characters. |

### Manifest contract 1 *(3.1)*

A manifest that declares `"contract": 1` may use the fields below. A manifest **without** `contract` is a 2.1/3.0
manifest: it installs exactly as before, and naming any of these fields without `contract` is refused. A node on 3.0 or
earlier refuses a contract-1 manifest (it names fields that node does not know), so a module that must stay installable
there keeps a separate tag. The core knows contract `1` and refuses any other value.

| Field | |
|---|---|
| `contract` | `1`. Requires `min_core` of `3.1` or newer, and replaces `config` (a manifest with both is refused). |
| `cli` | `{"name": "<cli>", "command": [argv…]}`. The name is 2 to 32 characters of `a-z`, `0-9`, `-`, starting with a letter, and is not one the core uses (`hv`, `hive-mind`, anything starting `hive-`). The command has `service.command`'s rules. |
| `runtimes` | Up to 16 distinct program **names** (`claude`, `grok`), never paths: no slash. |
| `config_schema` | `{key: entry}`, up to 128 keys. A key is a config key name where a whole dot-segment `{word}` stands for any one segment (`seat.{role}.runtime`). An entry has `type` (`string`, `int`, `bool`, `enum`), `description` (required), `scope` (`node` (default), `fleet`, `fleet-node`), and optionally `default` (a string that fits the type), `max` (string length, or int bound), `min` (int), `values` (enum), `required_at_install`, `secret`, `identifying`, and `help` *(4.1)*, a plain-language explanation for the settings page (at most 400 characters, text only; a module that ships `help` sets `min_core` 4.1). A `secret` key is node-scoped. An `identifying` or `secret` key may not ship a `default`: a signed manifest carries no one's user, host, account or repository name. |
| `status` | argv that prints the status envelope (below) as one JSON object. |
| `doctor` | argv that prints the module's own doctor answer (below). |
| `reads` | Distinct names from `overview`, `tags`, `related`, `audit`, `peers`, `verify`, `node`: the read routes the module declares it will use. `module add` and `update` list them (marking `peers`/`node`, which carry tailnet addresses, and `overview`/`peers`/`verify`, which are node-local) and record them in `.modules.json`; the module API serves `/v1/<name>` only for a recorded name and answers `403` otherwise. A manifest without `reads` is served none. |
| `trust` | `trusted` or `confined`; absent means `trusted`, so every existing manifest keeps its meaning. It is the tier the publisher signs. `module add` and `update` print it (`trusted` loudly) before you confirm, and `.modules.json` records a `confined` tier. A node may **lower** a module's tier with `hive-mind module trust <name> confined` (node-local, no key needed); clearing that lowering, or an update whose manifest signs a higher tier than the node last held, is a raise and needs the owner key, and nothing raises a module above what its manifest signs. `module list` shows the effective tier. A unit whose effective tier is `confined` gets `ProtectSystem=strict`, `ProtectHome=tmpfs`, `PrivateTmp=yes`, `NoNewPrivileges=yes` and an empty `CapabilityBoundingSet=`; it can write only its key directory (`HIVE_MODULE_KEY_DIR`) and reads its own directory and its runtimes' `PATH` entries; a unit with a `service` also gets its own runtime directory for sockets, `HIVE_MODULE_RUNTIME_DIR` (`$XDG_RUNTIME_DIR/hive-mind-mod-<name>`, 0700, never the shared `hive-mind` one), and reads `hive_module_client.py`, `ed25519.py`, `merkle.py` and `sync_common.py` from the checkout. Lowering a tier re-renders and restarts the unit; a `trusted` unit is unchanged. |
| `commands` | *(4.2)* An object keyed by operator verb (1 to 4 words of `a-z`, `0-9`, `-`; at most 64): `{"mutates": bool, "reversible": "yes"\|"no"\|"n/a", "undo": [argv…], "dry_run": bool, "fleet": bool}`. `mutates` is required; a mutating verb names `reversible` and takes `--dry-run` (`dry_run` must be `true`); `undo` is an argv template (placeholders `{name}`, one whole word each) whose leading words name another declared mutating verb, and is required for `reversible: "yes"` and only then; `fleet` is reserved and refused on a `reversible: "no"` verb. Needs `contract: 1`, a `cli` and `min_core` `4.2` or newer. A manifest without it is unchanged; `module check` warns that its callers cannot tell which verbs change state. `module add` and `update` print the table, and `hive-mind module commands <name> [--json]` lists it from the verified tree. It is the publisher's claim, not a sandbox. |
| `ui` | The module's page for the dashboard: an object of at most 64 KiB, defined by [`ui_block.schema.json`](ui_block.schema.json). Unknown top-level keys are refused; unknown widgets, slots and extra fields inside an item are ignored by the dashboard. |

`status`, `doctor` and `cli.command` are argv lists run without a shell, with `service.command`'s rules: strings, no
control characters, and a relative first word may not leave the module's directory.

**The status envelope** is `{"schema": 1, "state": "ok"|"degraded"|"down"|"unknown", "version": string,
"problems": [string], "units": [{"name", "state"}] (optional), "detail": {…} (optional, the module's own)}`, exit 0.

**The doctor answer** is `{"schema": 1, "results": [{"level": "ok"|"warn"|"fail", "check", "message",
"fix_hint"?}]}` with at most 100 results and 400 characters a string, exit 0 unless a result is `fail`.

*In 3.1 the core validates and signs for all of these fields; the behaviour that acts on each lands one slot at a time,
each documented here when it does. The config schema, the runtimes, the CLI launcher, the doctor and the read routes
act now, below.*

#### The doctor slot

A contract-1 module that declares `doctor` has the core run it. It is an argv with no shell, run in the module's
directory with the module's PATH and an environment built from nothing: `HIVE_MODULE_NAME`, `HIVE_MODULE_DIR`,
`HIVE_MODULE_KEY_DIR`, `HIVE_DOCTOR_MODE` (`manual`, `install`, `update`, `boot` or `timer`, so a module can do a deeper
check at install and a cheap one on a timer), `PATH`, `HOME` and `LANG`. The owner key, its passphrase, the identity stash
and every other `HIVE_*` variable are not passed. Do not write into `HIVE_MODULE_DIR`: a file the manifest does not list
makes the tree fail its check.

The core reads the answer as text and caps it: 20 seconds, 64 KiB, 100 results, 400 characters a string, control
and bidi characters removed. A module that times out, crashes, prints something else or exits non-zero without a `fail`
result gets one **warn** naming the problem; the core's own checks never wait on it. A `fail` it reports is a `fail`.

It runs in `hv doctor` (its results join the `modules:<name>` line; a `fail` makes that line fail), at the end of
`module add` and `module update` (printed; the install is never rolled back, and `--require-healthy` makes a reported
`fail` set exit 1), and from the 15-minute `hive-doctor` timer with `HIVE_DOCTOR_MODE=timer`. It runs only for a module
whose manifest verifies and is signed by the pinned publisher. The latest results are cached in
`$HIVE_HOME/.module-doctor/<name>.json` (0600, local, never journaled) with the time they were taken, so a dashboard
shows them and their age without starting module code. The fix step of `hv doctor --fix` never runs module code (its check pass does run the doctor, which is how the timer gets `timer` mode) and never treats a
module's output as an action; `fix_hint` is text. A runtime the manifest declares that is not on the module's PATH is
a core **warn** (`hive-mind module runtime <name> <program> <dir>`).

#### Runtimes, the unit PATH and the CLI launcher

`runtimes` names programs; the core finds them, and the module never names a directory. At `add`, `update`,
`reapply` (which `hive-mind update` runs) and `module runtime`, each program is looked for in the invoking `PATH`, then
`~/.local/bin`, `~/.npm-global/bin`, `~/.cargo/bin`, `~/.bun/bin`, `/opt/homebrew/bin` and `/usr/local/bin`, then in the
directory the operator gave with `hive-mind module runtime <name> <program> <dir>` (`--unset` forgets it; use it when the
install ran under a narrower PATH than your login shell, as a dashboard-started one does). A directory counts only if it
is absolute, exists, is owned by you or root, and is writable by neither group nor world; anything else is skipped. The
resolved directories are recorded in `.modules.json` (`runtime_dirs`) and the unit's `PATH` becomes
`<runtime dirs>:<bin dir>:/usr/local/bin:/usr/bin:/bin`, where the bin dir is the one that holds `hive-mind`
(`$HIVE_BIN_DIR` overrides it). A program that is not found is a warning at install (the install completes) with the fix
named: `claude not found; install it or run hive-mind module runtime hwatch claude <dir>`. A manifest without `contract`
gets no `PATH` line in its unit, as before.

`cli` gives the module a command on your PATH. `add` and `update` write one small `sh` file named `cli.name` into the
bin dir, rendered by the core: it unsets the owner's secrets (`HIVE_OWNER_PASSPHRASE`, `HIVE_OWNER_KEY_PASSPHRASE`,
`HIVE_OWNER_SEED`, `HIVE_KEY_DIR`), exports `HIVE_MODULE_NAME`, `HIVE_MODULE_DIR`, `HIVE_MODULE_KEY_DIR` and the same `PATH`
as the unit, and `exec`s the module's `cli.command` with `"$@"`. The module supplies only a name and an argv; every word is
single-quoted, so no module text is ever shell. The file's second line is a marker with the module name and a digest of
the rest, which is how the core knows a file is its own. An install is refused, before anything is written, when the name
is one the core uses, another module's, an existing file that is not this module's unedited launcher, or when the bin dir
is writable by group or world. `remove` deletes the launcher (only if it is still the module's own and unedited); `update`
renames or drops it with the manifest, and a failed update puts the old one back.

`hive-mind module run <name> [--] <args…>` does the same without the launcher (flags after the name go to the module's
command, each word unchanged, and core reads none of them), and returns the command's exit status.
`hive-mind module commands <name> [--json]` (4.2) prints the verbs the module's signed manifest declares: which are
read-only, which change state, which can be undone and how. It loads the installed tree through the same signature and
digest check as `doctor` and needs the pinned publisher's signature, and exits 1 when that fails or the module is not
installed or disabled. A manifest without `commands` prints that it declares none; treat every verb of such a module as one that
cannot be undone. A verb the table does not list is undeclared. `hive-mind module status <name> [--json]` runs the manifest's `status`
argv (no shell, in the module's directory, with `PATH`, `HOME`, `LANG` and the three `HIVE_MODULE_*` variables only; 20 s and
64 KiB at most) and prints the envelope. A status that does not run, parse or fit the envelope is reported as `unknown`
with the reason, never as a crash. Exit 0 when the state is `ok`, 1 otherwise.

#### Configuration under `config_schema`

A module with a `config_schema` has typed, scoped keys. A value lives in one of three layers, and a key is read only
from the layer its `scope` names, then its schema `default`:

| Scope | Where it lives | Set by |
|---|---|---|
| `node` (default) | `<module dir>/config` on this node: 0600, never journaled | the node's operator, no owner key |
| `fleet` | `x-<module>:<key>`: an owner-signed `set-config`, on every node | the owner, `--fleet` |
| `fleet-node` | `x-<module>:node.<16 hex of the device>.<key>`: the same entry, keyed by device | the owner, `--fleet-node <device>` |

`fleet-node` is a naming convention over the existing `set-config`: nothing in the journal, the entry or the
projection differs. A template key (`seat.{role}.enabled`) matches any one segment; an exact name wins over a template.
Defaults are not copied into the node file, so a changed default reaches every node that did not set the key. A
module reads its own explicit values from the node file and the rest through `GET /v1/` (fleet) and its own shipped
defaults; `config get` and `config list` show the merged answer and where each value came from (`node`, `fleet-node`,
`fleet`, `default`).

```
hive-mind module config <name> list [--json] [--merged]
hive-mind module config <name> get <key> [--json]
hive-mind module config <name> set <key> [<value>] [--fleet | --fleet-node <device>] [--stdin]
hive-mind module config <name> unset <key>
hive-mind module config <name> validate [--json]
```

- `set` checks the key (an unknown key is refused when there is a schema), the type, the bounds and the enum, and that
  the layer is the key's scope. `set` to the value the key already has changes and signs nothing, so a re-run prompts
  for no passphrase. `unset` removes a node value; a fleet value cannot be removed, only set again.
- `validate` checks the node file and the fleet values against the schema and reports a bad value, a missing
  `required_at_install` key (an error, exit 1), and a key the schema does not know or whose scope says the layer is
  ignored (a warning). `hv doctor` shows the same findings as warnings under `modules:<name>`. They name a key and a
  rule, never a value.
- A **`secret`** key is node-scoped, is shown as `********` in `list`, `get`, `--json`, `validate` and the doctor, and is
  never accepted on a command line (shell history, `ps`): `set` asks on a terminal, reads one line with `--stdin`, and
  install takes it with `--set-env key=VARIABLE`. A key the schema no longer names is masked too (it may have been a secret). It is written to the 0600 node file only. The node file is plaintext
  at rest; anyone who is the operator can read it (B9).
- An **`identifying`** fleet value is journaled and visible to every admitted device for good; the CLI says so when
  it is set.
- A module without `contract` keeps the free-form `set`/`unset`/`list` of 2.3, unchanged; `get`, `validate` and
  `list --merged` also work on it.

**At install.** `module add <name> --from <repo>` (and `update`) takes `--set key=value` (node scope),
`--set-fleet key=value` (fleet scope; owner key needed, signed after the module is installed), `--set-env
key=VARIABLE` (node scope, for a secret) and `--non-interactive`. Every `required_at_install` key that has no value and
no `default` must be given: on a terminal the command asks (hidden for a secret), otherwise it refuses, naming the
keys, **before anything is fetched into place, minted or signed**. `required_at_install` is asked for concrete `node`
and `fleet` keys; a template key or a `fleet-node` key is set after the install. A device without the owner key is not
asked for a fleet key (the owner sets it; `config validate` reports it). `update` asks only for keys the new schema
added. The install of a module that has no `config_schema` takes none of these flags.

#### Declared commands *(4.2)*

`commands` (the manifest table above) is how a module says which of its operator verbs change state and how to take
that back. A verb is words, not a string: core runs `cli.command`, then the verb's words, then the caller's remaining
arguments, with no shell, and joins nothing. Core reads none of them (`--dry-run`, `--yes`, `--`, `--json` and a word
with spaces reach the module as one word each). The verb core matches is the leading words that do not start with `-`,
longest key first; `hwatch --json status` matches none, so that verb is undeclared and runs as before. A verb absent from the map is undeclared: treat it as one
that cannot be undone. The rules below are the module's to keep; core checks the shape and `--help`, never the truth.

**Every declared verb answers `--help` with exit 0 and no state change.** `module check --run` and `hv doctor` run it.

**`--dry-run` (every verb that `mutates`).** Prints exactly what the verb would change, item by item, in the same shape
the real run prints, and changes nothing: no file, snapshot, ledger, unit, request or journal write.

- It never prompts and never needs `--yes`, including when stdin is not a terminal and including a `reversible: "no"`
  verb. It prints the plan and exits 0 when the verb would act once confirmed. It uses the real run's refusal (same
  message, same exit code) only for a reason other than the missing confirmation: a superseded restore, an unknown id,
  a missing file. `--dry-run` together with `--yes` is still a dry run.
- Text output starts with the line `dry run: nothing is changed`. A machine format keeps its format and does not
  prefix that line; every record it prints carries `"dry_run": true` instead (a real run omits the field).

**`reversible: "yes"`.**

- *Keep what you remove.* Before the verb removes, moves or overwrites a thing, it writes the thing as it was to a
  bounded archive of the module's own: a local 0600 file or snapshot section, never journaled, never synced. The bound
  is a count and an age, stated in the module's README and `--help`. On each archive write, drop entries older than the
  age, then drop the oldest until the count fits. One entry per change, with an undo id (the proposal id when the change
  came from a proposal, else a fresh 16-hex id), the time, who ran it (the verb and its arguments, as data) and the
  restore point.
- *Print the way back.* For each change the real run prints one line `undo: <cli> <undo verb> <id>`, filled in: on
  stdout for text output, on stderr when stdout is a machine format. A `--json` run carries `"undo": [argv…]` per
  change. A value that contains spaces, or starts with `-`, stays one element.
- *The undo verb* takes the undo id (or `--since <time>`: every archived change at or after that time, each handled on
  its own, oldest first; a superseded id is reported and skipped; exit 1 if any selected id was refused, 0 only when
  every one restored) and restores the thing as it was. It refuses, naming the reason and the id, exit 1, when the
  restore point is superseded: the entry is gone (pruned) or past the age (expired, even when no write has pruned it);
  a newer state exists for the same subject; or the subject no longer exists. There is no force flag. Undo is itself a
  declared verb, takes `--dry-run`, never needs `--yes`, and archives what it replaces.
- Nothing is read from the archive or a ledger as an instruction: the undo verb restores a stored object after
  re-checking the current state.

**`reversible: "no"`.**

- The help text contains the sentence `This cannot be undone.` and names `--yes` and `--dry-run`.
- The output of a real run starts with `this cannot be undone: <what>` and ends with what was done.
- A run is non-interactive when stdin is not a terminal (`os.isatty(0)` is false). Then, without `--yes`, the verb
  prints `refused: <verb> cannot be undone; add --yes to confirm, or --dry-run to see what it would do`, changes
  nothing and exits 1. At a terminal the real run asks once (`This cannot be undone. Type yes to continue:`); only the
  exact word `yes` proceeds. `--yes` is accepted, and ignored, on any declared mutating verb. No environment variable
  stands in for `--yes`: the word must be in the argv a permission rule sees.
- Such a verb is never `fleet: true`.

**`reversible: "n/a"`.** Takes `--dry-run`, needs no `--yes`, prints what it did. A verb that deletes, closes, posts,
moves or overwrites anything is never `n/a`. `module check` lists every `n/a` verb in one warning for the publisher to
reread, because core cannot tell `n/a` from a lie.

**Exit codes.** 0: done, or a dry run that would act. 1: refused (no `--yes` without a terminal, a superseded restore,
an unknown id) or any selected id of a `--since` refused. The module's own usage errors keep its own code.

**What an agent can rely on** is exactly as good as the publisher's honesty: the map is the signed publisher's claim,
not a sandbox. Read the table at install and `hive-mind module commands <name>` after an update, and give the agent's
settings a deny rule for `--yes`.

#### `hive-mind module check <dir> [--run] [--deny TERM] [--json]`

For publishers and CI. It validates the directory's `module.json` with the code `module add` uses, verifies the
signature when there is one, checks the file set and digests (the git-tracked files, in a checkout), and lints the
manifest and shipped files for text that looks like a person, host, account or repository (an email address, a
`/home/<user>`, an `@handle` outside code files, a `github.com/<org>/<repo>`; `--deny` adds terms). Lint results are
**warnings**: a publisher aid, not a gate and not a claim that nothing identifying is shipped. With `--run` it also
runs the module's `status` and `doctor` in a throwaway directory, with a scrubbed environment (`PATH`, a temporary
`HOME`, `HIVE_MODULE_NAME/DIR/KEY_DIR` and `HIVE_DOCTOR_MODE=manual`; no owner or identity variable), a 20-second
limit and a 64 KiB output cap, and checks the shapes above. For a module that declares `commands` it also runs
`<cli.command> <verb> --help` for each declared verb (5 s each, 60 s in all, the last probe getting only the time that remains; then it stops and warns naming the verbs it did not
run) and warns when one exits non-zero, when a mutating verb's help does not name `--dry-run`, or when a `reversible: "no"`
verb's help lacks `This cannot be undone.`, `--yes` or `--dry-run`. Nothing in `check` runs a verb without `--help`. `--run` runs the module's own code as you, which is why it
is a flag. A contract-1 manifest with a `cli` and no `commands` gets a warning (4.2), and the verbs a `commands` map
declares `reversible: "n/a"` are listed in one warning so the publisher rereads them. Exit 0 when there is no error, 1 otherwise. `module add` does not require `check` to have been run: it
re-validates everything itself.

`module.json.sig` is Ed25519 over the exact bytes of `module.json`, verified under `publisher`.
`hive-mind module add` copies the listed files into a staging directory and hashes them again
there: what is installed is what was hashed.

## Names: `x-<module>:<key>`

Core reserves every bare name in [`NAMESPACES.md`](NAMESPACES.md). A module's own names take the
prefix `x-<module>:`, for example `x-hwatch:heartbeat`. No core name starts with `x-`.

A module may add a **link kind** or a **config key**, prefixed. It may never add an entry type, a
governance action or a channel: each of those needs a core projection, and an unrecognised
channel counts as `introspect`. The 2.1 module API enforces the prefix.

Fleet-wide config is an owner-signed `set-config` of `x-<module>:<key>` as a string. Every node
projects it; a node that does not know the key ignores it; core never interprets the value. The
module reads those keys on `GET /v1/` and cannot write governed config.

## The module API

The listener is `127.0.0.1:9886` (`HIVE_MODULE_PORT` overrides), loopback only, in the sync
daemon's process. Every request is signed with the Hive-Auth envelope by the module's admitted
device. Unsigned requests are never served.

### One hive per machine

A fleet whose devices run under different OS accounts works: membership is by device key and
principal, and the hive home, `~/.hive/modules`, the launcher directory and the runtime
directory are all per account. Each machine's hcns bus is private to its one user, and events
reach other devices through the hive, not the socket.

Two OS users running **separate hives on the same machine** is not supported and not planned. Both hives'
sync daemons default to port `9876` and both module APIs to `9886`. The sync port can be moved
(`port` in `.peers.json`), but a module's unit gets no `HIVE_MODULE_PORT`, so the second hive's
modules would still call the first hive's module API, where their device keys are not
admitted. Several hives under **one** user (a test hive beside the real one) share
`~/.hive/modules` the same way, which is what the doctor's "not managed by this hive" warning
reports.

| Route | Verb | Does |
|---|---|---|
| `/v1/` | GET | Version, hive, caller, quota, fleet config; since 2.3 also `node_id` and `node_devices` |
| `/v1/feed` | GET | Journal past a per-node cursor |
| `/v1/search` | GET | Search over the projection |
| `/v1/item` | GET | One item by `h:` id or `node_id:seq` |
| `/v1/tip` | GET | The caller's own chain tip |
| `/v1/entries` | POST | Append one entry the module signed |
| `/v1/entity` | GET | An entity and the module entities a core `same-as` joins to it |
| `/v1/overview`, `tags`, `related`, `audit`, `peers`, `verify`, `node` | GET | *(3.1)* The dashboard's reads; served only to a module whose manifest names the route in `reads` (below) |

HiveMind ships a reference signer and client, [`hive_module_client.py`](../hive_module_client.py),
readable top to bottom and importing nothing from `hv`. A module in another language reimplements
its few lines. See [Signing](MODULE_API.md#signing) in the API reference.

A module's `payload.source` is required and is `x-<module>` of this module. `manual` and `owner`
are refused.

## Per-node config and fleet config

Two stores, both live in 2.3.

**Per-node** (this machine only). `hive-mind module config <name> set <key> <value>`, `unset`,
`list`. The file is `$HIVE_MODULES_DIR/<name>/config` (default `~/.hive/modules/<name>/config`):
local, never journaled, mode 0600, no owner key. An `update` keeps what you set. Keys follow the
manifest's rule; values are strings of at most 4096 characters (a value starting with `-` needs
`--` first). The manifest's `config` object supplies defaults for keys the file does not yet have.

**Fleet** (every node). The owner runs `hive-mind config set x-<module>:<key> <value>`. That is a
signed `set-config` in the journal. The module reads the projected keys on `GET /v1/` as
`config`. It cannot write them.

## Install, update, remove

Platform check first. Modules are supported on **Linux with a user systemd manager** only.
Elsewhere `add` says `module management is not supported on this platform yet (2.1 ships on Linux
only)` and exits non-zero before it fetches, mints or installs anything. Termux is refused too.

A module is installed under `$HIVE_MODULES_DIR` (default `~/.hive/modules/<name>/`), **outside the
checkout**, so `hive-mind reset` and `hv verify` never touch it. Its key lives in the key
directory under `modules/<name>/device-key` (base64 of a 32-byte seed, mode 0600). The unit
`hive-module-<name>.service` (and `.timer` when `interval` is set) runs as the operator's user,
in the module's directory, with `HIVE_MODULE_NAME`, `HIVE_MODULE_DIR` and `HIVE_MODULE_KEY_DIR`
in its environment and **no owner-key path**.

```
hive-mind module install <name> [--from <repo>] [--publisher KEY] [--principal P] [--json] [--require-healthy]   # (3.1)
hive-mind module add <name> --from <repo> [--publisher KEY] [--principal P]
hive-mind module add --abort <name>
hive-mind module update <name> [--from <repo>]
hive-mind module remove <name>
hive-mind module list [--json]                                     # (4.0) --json
hive-mind module check <dir> [--run] [--deny TERM] [--json]       # (3.1)
hive-mind module config <name> set <key> <value> | unset <key> | list | get <key> | validate | schema
hive-mind module commands <name> [--json]                         # (4.2)
hive-mind module run <name> [--] <args>... | status <name> [--json] | runtime <name> <program> <dir> | trust <name> [confined|trusted]
hive-mind module quota <name> [--per-hour N] [--per-day N] [--entry-bytes N] [--lifetime N]
```

On the **owner machine**, `add` fetches, verifies, pins the publisher, mints the device key,
admits it, writes config and the unit, and records the module in `$HIVE_HOME/.modules.json`. A
failed `add` leaves nothing behind.

On a **member machine** (no owner key), `add` is the two-step above. `update` needs no owner key
(same tree check, same pinned publisher; it changes no membership or quota). `remove` stops the
unit and deletes the tree and the key, then prints the owner's `hive-mind group revoke <device>`
line: the device id stays admitted until the owner runs it. `quota` stays owner-only.

A `confined` service's persistent state directory (`HIVE_MODULE_STATE_DIR`, `~/.local/state/hive-mind-mod-<name>`) is **left in place** by `remove`; `remove --purge` deletes it too.

`hive-mind update` then runs the internal `hive-mind module refresh` *(3.1)* for every installed module, after the core
files are in place and the daemon is restarted:

1. It clones the module's **recorded source**, runs the same tree check as `update` (signature, digests, `min_core`,
   known `contract`) and requires the **pinned publisher**. A different signer is refused. A version that is not strictly
   newer than the installed one (compared as `major[.minor[.patch]]` integers) is refused too, and so is one the two
   strings cannot be ordered by (a suffix, a leading `v`, a fourth component); the same version string republished is
   kept. An automatic update only goes forward; `module update` by hand takes any tree the pinned publisher signed. The newest version is the source's default-branch head.
   A manifest without `contract` is compatible with every 3.x core.
2. A newer compatible version replaces the module in one step, its files and its units together, so the new core's
   unit text never restarts a module that is still on old code (decision h:98d0d79694). Running seat sessions are not
   killed: the core touches only the module's own units. One status line per module.
3. If the update fails but the installed version still admits this core, it stays, its units are re-rendered as before,
   and a loud warning names the module and the reason.
4. If it fails and the installed version does not admit this core (it no longer verifies, or the core refuses it), the
   module is **disabled**: units stopped and disabled (not deleted), launcher removed, `disabled` recorded in
   `.modules.json`. A boxed message names the module, why, and the one command that brings it back. Its hooks do not
   run, `module run` and `status` refuse, and `hv doctor` reports `modules:<name>` as `disabled-incompatible` (a fail).
5. `hive-mind module update <name>`, or the next `hive-mind update`, installs a compatible version and re-enables it.

The core update succeeds in every case, and a second run with nothing new changes nothing. `hive-mind module refresh
--check` prints the plan and changes nothing. A new required config key with no default cannot be asked for here (no
prompt): that update fails as in step 3, and `hive-mind module update <name> --set ...` finishes it.

`hv doctor` reports each installed module as `modules:<name>`. A missing, tampered or wrongly
signed manifest **fails**. An absent or stopped unit, a revoked device, missing config, a down
listener and quota past 80% **warn**.

## The four modules

hwatch and hcns are public overviews: their repositories are private, so there are no links here and access is on
request. hconfig and hdash are public.

### hwatch

**hwatch** is the seat watcher. It watches GitHub and the hive, routes addressed posts and holds to agent seats, and
launches those seat sessions, so a team of agents picks up the work addressed to it without anyone polling. It runs in
our own fleet. Its manifest is contract 1 (`min_core` 3.1, a `config_schema`, `cli: hwatch`, `runtimes` for the agent
programs). Install it as any other module: `hive-mind module install hwatch --from <repo>`. Its fleet keys are
`x-hwatch:…`, set by the owner; its per-node keys are `hive-mind module config hwatch …`.

Session telemetry is hwatch's (`hwatch telemetry record|report|list`). The session hook calls `hive-mind module run
hwatch -- telemetry record …`; it keeps the store core used to write, `$HIVE_HOME/.telemetry/telemetry.db`, so nothing
was copied or migrated. A node without hwatch records nothing and the hooks stay silent.

### hcns

**hcns** is an ephemeral event bus. Processes on one node, and admitted nodes over the tailnet, publish and subscribe to
short events that say where to look, so a seat or tool learns of a new hive entry within a second instead of on its
next poll. An event is a reference and a few structured fields; nothing is journaled, and the consumer always reads the
durable source, so the bus can only make a wake sooner, never replace polling. It is contract 1, `trust: confined`,
declares `reads` of `overview` and `peers`, and writes nothing to the hive. Its CLI is `hcns` (`serve`, `publish`,
`wait`, `status`, `doctor`). hwatch uses it as a trigger when it is running and behaves as before when it is not.

### hconfig

**hconfig** is the configuration module ([`projectmentor/hive-mind-config`](https://github.com/projectmentor/hive-mind-config), MIT).
It is a `cli` module (`hconfig`) with no service, no hooks, no runtimes, `reads: []` and `trust: confined`: one registry of every
setting a node has (`node:` keys, a module's `x-<module>:` keys, and the governed `core:` keys), each with its schema, merged value,
source, and who may change it and how. Every verb takes `--json`; the protocol is
[`docs/CLI.md`](https://github.com/projectmentor/hive-mind-config/blob/main/docs/CLI.md) in that repository, and hdash calls it as a subprocess.
It reads and writes only through this core's own verbs (the [4.0 config verbs](#config-verbs-added-in-40)) and holds no key: a
signed write is the core's, through the owner-unlock agent, or it exits 5 and hconfig reports `owner-locked`. It needs a 4.0 core.
hdash needs hconfig; install hconfig first with `hive-mind module install hconfig --from https://github.com/projectmentor/hive-mind-config`.

### hdash

**hdash** is the read-only dashboard ([`projectmentor/hive-mind-dash`](https://github.com/projectmentor/hive-mind-dash)),
extracted unchanged from core in 3.1. It is a `service` module (a loopback listener, config key `port`, default 9896)
with a `cli` (`hdash dash`), and `reads` `overview`, `tags`, `related`, `audit`, `peers` and `verify`. Its `/api/*`
calls go to the core's signed `/v1` routes, or, for `status`, `telemetry`, `entities` and a peer's `?node=` view, to the
sync daemon's loopback `/api/*`. The sync daemon no longer serves the page. Install it with
`hive-mind module install hdash --from https://github.com/projectmentor/hive-mind-dash`.

## Config verbs added in 4.0

These are the core verbs hconfig and hdash are built on; any module or script may use them.

```
hv config schema [--json]                         # the governed (set-config) and node-local key tables: type, default, range
hv config show [--json]                           # each governed key's value, source, and set_by (device_id, ts, sid)
hv config node list|get <key>|set <key> <value> [--json]   # node-local keys of .peers.json; no owner key
hive-mind module list --json
hive-mind module config <name> schema             # the installed manifest's config_schema as JSON
hive-mind config set ... --non-interactive
hive-mind module config <name> set <key> <value> --fleet|--fleet-node <device> --non-interactive
```

- `hv config schema` and `show` are read-only. `show` adds an operator-only `governance` block.
- `config node` reads and writes `port`, `bind`, `sync_auth`, `sync_auth_outbound`, `dashboard_share`,
  `dashboard_viewers` and `dashboard_viewer_port`. They are never a `set-config` and never journaled; an environment
  override shows as `source: env`, masked. The daemon re-reads them at restart.
- `--non-interactive` never asks for the owner passphrase and never reads stdin to unlock. When the unlock agent
  is absent, locked or refuses the act, nothing is signed and the exit status is **5**.
- Every refusal of `hive-mind config set` exits 1 (it exited 0 before 4.0).

## `hv dash` and `hv telemetry` are removed in 4.0

Both exit 2 with one line naming the module command that replaced them, whatever arguments follow, and run nothing:

| Command | Use instead |
|---|---|
| `hv dash` was removed | `hdash dash` ([hdash](#hdash)) |
| `hv telemetry` was removed | `hwatch telemetry` ([hwatch](#hwatch)) |

## Not in 4.0

- Supervisor backends other than a Linux user systemd manager (launchd, Windows). On other platforms `module add` and
  `install` say so and change nothing.
- Raising a module's `trust` above what its publisher signed. A node can only lower a tier.

## A minimal worked example

A contract-1 timer module named `pulse` that writes one volatile fact a minute, with one typed config key, a
`status` and a `doctor`. Copy these files into a git repository, sign the manifest, and `hive-mind module install pulse --from <that repo>`.
It is `trusted` (the default tier) because it imports the reference client from the HiveMind checkout; a module that
carries its own copy of the client can sign `"trust": "confined"`.

### `pulse.py`

The unit sets `HIVE_MODULE_KEY_DIR`. The reference client lives in the HiveMind checkout
(`hive_module_client.py`); the service adds that checkout to `sys.path`. `payload.source` is
`x-pulse`.

```python
#!/usr/bin/env python3
import base64, json, os, sys
from pathlib import Path

if sys.argv[1:] == ["status"]:                     # the manifest's `status` argv: print the envelope, exit 0
    print(json.dumps({"schema": 1, "state": "ok", "version": "0.1.0", "problems": []}))
    sys.exit(0)
if sys.argv[1:] == ["doctor"]:                     # the manifest's `doctor` argv: print the answer, exit 0 unless a fail
    print(json.dumps({"schema": 1, "results": [{"level": "ok", "check": "pulse", "message": "nothing to check"}]}))
    sys.exit(0)

sys.path.insert(0, os.environ.get("HIVE_HOME", str(Path.home() / "projects" / "hive-mind")))
import hive_module_client as client

seed = base64.b64decode((Path(os.environ["HIVE_MODULE_KEY_DIR"]) / "device-key").read_text().strip())
status, body = client.ModuleClient(seed).write(
    "fact",
    {"content": "pulse", "source": "x-pulse", "channel": "act", "tags": ["volatile"]},
)
sys.exit(0 if status == 200 else 1)
```

### Sign `module.json`

Mint a 32-byte publisher seed and keep it off the module tree (`python3 -c 'import os; open("publisher-seed","wb").write(os.urandom(32))'`).
The signature is Ed25519 over the exact bytes of `module.json`.

```python
#!/usr/bin/env python3
import base64, hashlib, json, os, sys
from pathlib import Path

sys.path.insert(0, os.environ.get("HIVE_HOME", str(Path.home() / "projects" / "hive-mind")))
import ed25519

seed = Path("publisher-seed").read_bytes()          # 32 bytes; not part of the module
body = Path("pulse.py").read_bytes()
files = {"pulse.py": hashlib.sha256(body).hexdigest()}
manifest = {
    "name": "pulse",
    "version": "0.1.0",
    "contract": 1,
    "min_core": "3.1",
    "publisher": base64.b64encode(ed25519.pub_from_seed(seed)).decode(),
    "files": files,
    "service": {"command": ["python3", "-I", "pulse.py"], "restart": "on-failure", "interval": 60},
    "status": ["python3", "-I", "pulse.py", "status"],
    "doctor": ["python3", "-I", "pulse.py", "doctor"],
    "config_schema": {"note": {"type": "string", "description": "Free text for the operator", "default": "pulse"}},
    "quota": {"per_hour": 60, "per_day": 200},
}
raw = json.dumps(manifest, indent=1, sort_keys=True).encode()
Path("module.json").write_bytes(raw)
Path("module.json.sig").write_text(base64.b64encode(ed25519.sign(raw, seed)).decode())
```

Commit `pulse.py`, `module.json` and `module.json.sig`. Then:

```bash
hive-mind module check /path/to/pulse --run
hive-mind module install pulse --from /path/to/pulse --publisher '<the base64 publisher key>'
hive-mind module status pulse
hive-mind module config pulse list --merged
```

`install` is idempotent: run it again and it says `nothing to do`. On a member node the first `install` prints the
owner's `hive-mind group admit … --module pulse` line and exits 3. Run that on the owner machine, wait until the admit
has reached the member, and run the same `install` again.

A Python module that cannot see the HiveMind checkout copies `hive_module_client.py` plus
`ed25519.py`, `merkle.py` and `sync_common.py` into its own tree and lists them in `files`. A
module in another language reimplements the few lines in `hive_module_client.py`.
