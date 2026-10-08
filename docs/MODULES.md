# Modules: developer guide

Contract **2.3**. This is how you write, sign, and install a HiveMind module today. The
route-by-route API is [`MODULE_API.md`](MODULE_API.md). The names a module may introduce are
[`NAMESPACES.md`](NAMESPACES.md). The verbs are [`CLI_REFERENCE.md`](CLI_REFERENCE.md#hive-mind-module--install-and-manage-modules-21-linux-only).

A one-command, platform-independent install, extra supervisor backends, and dashboard UX are
[planned](#planned). Documented here is what 2.3 ships.

## What a module is

A **module** is a signed, sandboxed add-on. It is a separate process on the same machine as the
sync daemon. It has its own Ed25519 device key and its own write quota. The owner admits that
device as a module under the operator's principal. The module reaches the hive only through the
loopback `/v1` API, never through the owner key. It reads the hive, and writes `fact`, `decision`,
`idea`, `entity` and `link` entries that it signs itself; the core verifies, gates and appends them.

Install with `hive-mind module add`. There is no `hv module`.

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

| Route | Verb | Does |
|---|---|---|
| `/v1/` | GET | Version, hive, caller, quota, fleet config; since 2.3 also `node_id` and `node_devices` |
| `/v1/feed` | GET | Journal past a per-node cursor |
| `/v1/search` | GET | Search over the projection |
| `/v1/item` | GET | One item by `h:` id or `node_id:seq` |
| `/v1/tip` | GET | The caller's own chain tip |
| `/v1/entries` | POST | Append one entry the module signed |
| `/v1/entity` | GET | An entity and the module entities a core `same-as` joins to it |

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

Platform check first. 2.3 supports modules on **Linux with a user systemd manager** only.
Elsewhere `add` says `module management is not supported on this platform yet (2.1 ships on Linux
only)` and exits non-zero before it fetches, mints or installs anything. Termux is refused too.

A module is installed under `$HIVE_MODULES_DIR` (default `~/.hive/modules/<name>/`), **outside the
checkout**, so `hive-mind reset` and `hv verify` never touch it. Its key lives in the key
directory under `modules/<name>/device-key` (base64 of a 32-byte seed, mode 0600). The unit
`hive-module-<name>.service` (and `.timer` when `interval` is set) runs as the operator's user,
in the module's directory, with `HIVE_MODULE_NAME`, `HIVE_MODULE_DIR` and `HIVE_MODULE_KEY_DIR`
in its environment and **no owner-key path**.

```
hive-mind module add <name> --from <repo> [--publisher KEY] [--principal P]
hive-mind module add --abort <name>
hive-mind module update <name> [--from <repo>]
hive-mind module remove <name>
hive-mind module list
hive-mind module config <name> set <key> <value> | unset <key> | list
hive-mind module quota <name> [--per-hour N] [--per-day N] [--entry-bytes N] [--lifetime N]
```

On the **owner machine**, `add` fetches, verifies, pins the publisher, mints the device key,
admits it, writes config and the unit, and records the module in `$HIVE_HOME/.modules.json`. A
failed `add` leaves nothing behind.

On a **member machine** (no owner key), `add` is the two-step above. `update` needs no owner key
(same tree check, same pinned publisher; it changes no membership or quota). `remove` stops the
unit and deletes the tree and the key, then prints the owner's `hive-mind group revoke <device>`
line: the device id stays admitted until the owner runs it. `quota` stays owner-only.

`hive-mind update` then runs the internal `hive-mind module reapply`: it re-renders units that
changed and never fails the core update.

`hv doctor` reports each installed module as `modules:<name>`. A missing, tampered or wrongly
signed manifest **fails**. An absent or stopped unit, a revoked device, missing config, a down
listener and quota past 80% **warn**.

## hwatch

**hwatch** is the first module. It watches GitHub and the hive, routes addressed posts and holds
to agent seats, and launches those seat sessions. This is a public overview; the module's own
repository is private.

Install it the same way as any other module, with `hive-mind module add hwatch --from <repo>`.
Its fleet keys are `x-hwatch:…`, set by the owner. Its per-node keys are
`hive-mind module config hwatch …`.

## Planned

These are **not** in 2.3:

- One idempotent, platform-independent install command, extra supervisor backends (launchd,
  Windows), and dashboard UX for install / admit / configure:
  [Standard module install + configuration (#294)](https://github.com/projectmentor/hive-mind/issues/294).
- One owner-key unlock per session, and batch-signed config so an install does not prompt for
  the passphrase on every command:
  [Owner key: one unlock per session, batch-signed config (#295)](https://github.com/projectmentor/hive-mind/issues/295).

Until those land, the verbs and the two-step C6 path above are the contract.

## A minimal worked example

A timer module named `pulse` that writes one volatile fact a minute. Copy these files into a git
repository, sign the manifest, and `hive-mind module add pulse --from <that repo>`.

### `pulse.py`

The unit sets `HIVE_MODULE_KEY_DIR`. The reference client lives in the HiveMind checkout
(`hive_module_client.py`); the service adds that checkout to `sys.path`. `payload.source` is
`x-pulse`.

```python
#!/usr/bin/env python3
import base64, os, sys
from pathlib import Path

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
    "min_core": "2.3",
    "publisher": base64.b64encode(ed25519.pub_from_seed(seed)).decode(),
    "files": files,
    "service": {"command": ["python3", "-I", "pulse.py"], "restart": "on-failure", "interval": 60},
    "config": {"note": "pulse"},
    "quota": {"per_hour": 60, "per_day": 200},
}
raw = json.dumps(manifest, indent=1, sort_keys=True).encode()
Path("module.json").write_bytes(raw)
Path("module.json.sig").write_text(base64.b64encode(ed25519.sign(raw, seed)).decode())
```

Commit `pulse.py`, `module.json` and `module.json.sig`. Then:

```bash
hive-mind module add pulse --from /path/to/pulse --publisher '<the base64 publisher key>'
hive-mind module list
hive-mind module config pulse list
```

On a member node the first `add` prints the owner's `hive-mind group admit … --module pulse`
line. Run that on the owner machine, wait until the admit has reached the member, and run the
same `add` again.

A Python module that cannot see the HiveMind checkout copies `hive_module_client.py` plus
`ed25519.py`, `merkle.py` and `sync_common.py` into its own tree and lists them in `files`. A
module in another language reimplements the few lines in `hive_module_client.py`.
