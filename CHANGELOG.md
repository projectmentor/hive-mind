# Changelog

HiveMind is versioned by its **agent contract** (`hv version`), `MAJOR.MINOR`. A MINOR is additive
for the adapter surface — the verbs, flags and outputs an agent calls keep working — while a MAJOR
means adapters must re-integrate (see [`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md) §7,
which also records what each version means for an adapter).

Git tags are `vMAJOR.MINOR.PATCH`. `vX.Y.0` marks the commit on `main` that completed contract
`X.Y`, so the tag contains everything that version shipped. Fixes and documentation that follow
without changing the contract are tagged `vX.Y.1`, `vX.Y.2`, … Dates are when each version was
introduced (a contract) or tagged (a patch).

## 2.0.3 — unreleased

No contract change.

- **The legacy `supersedes_ref`, `supersedes` and `resolves_ref` payload fields now go through the link authority**
  (#204, security fix). They projected for any admitted device, so one device could mark another's decision
  superseded on every node. They now command only when the owner key signed the entry or its signer wrote the
  target, as a `link` entry does; `resolves_ref` must name a fact. Signed self-authored and owner-signed history
  projects as before. An unsigned legacy field commands only while the journal has no owner; once an owner exists
  it is evidence, and a matching `node_id` is not a signer. `hv doctor` `link-authz` lists a legacy field that no
  longer commands. A `link` entry is the only way to supersede or resolve (`AGENT_INTEGRATION.md` §7).
  **Upgrade effect:** a device-signed legacy supersede or resolve of another author's entry stops commanding on
  upgrade, and the target stands again on every node until the owner re-ratifies it with an owner-signed `link`.
  On the live hive this is the four `decide --revoke` entries of 2026-08-19; `hv doctor` `link-authz` lists them.
  An unsigned self-authored legacy field likewise stops commanding once any owner exists. The live hive has none:
  its legacy fields are device-signed, and its unsigned entries carry no legacy field.
- **`rebuild_db` is atomic** (#206). It committed the deletes before replaying the journal and never closed its
  connection on error, so a failure left the node's projection empty, search returning nothing, and a write lock
  held. It now runs in one transaction (`BEGIN IMMEDIATE` to the final insert, no commit between), rolls back on any
  exception so the previous projection stays queryable, and closes the connection in a `finally`. `_init_fts` runs
  its statements one at a time, because `executescript` commits.

## 2.0.2 — 2026-10-03 · `v2.0.2`

No contract change.

- **`rebuild_db` no longer raises on an `entity_fact`, `supersedes`, `resolves_ref` or `link` whose ref or local id is
  the wrong kind, absent or malformed** (#196, security fix). An `entity_fact` whose `fact_ref` named an entity,
  whose `entity_ref` named a fact, or whose legacy `entity_id` / `fact_id` named no row made the rebuild fail with
  `IntegrityError`. A ref that is not `[str, int]`, or an integer outside SQLite's 64-bit range (a ref's seq, a
  legacy `entity_id` / `fact_id` / `supersedes`), made it fail with `OverflowError` or `ProgrammingError`. Content of
  these types is accepted from any admitted device and the journal is permanent, so one such entry stopped every
  node's rebuild. The entry now lands and projects to nothing, as a dangling link does. A `supersedes` is held to
  the same rule: a `supersedes_ref` to a non-decision, or a `supersedes` that is not a decision row, marks nothing.
  The link-evidence passes skip a malformed ref instead of raising `TypeError`. Entries that resolve are projected
  as before.

## 2.0.1 — 2026-10-02 · `v2.0.1`

No contract change.

- **`hv verify` now exits non-zero when the install is not verified** (#190). `0` verified; `1` modified,
  signature invalid, fork key or no manifest; `3` could not fully check (anchor unreachable, unsigned build).
  Before, it exited 0 whatever it printed. `hv doctor` is unchanged and advisory. The printed lines are unchanged, and the MCP
  `hive_verify` tool still returns the verdict text for every exit code.

## 2.0 — 2026-10-02 · `v2.0.0`

**Upgrading from 1.x.** Run `hive-mind update` on each node. There is no journal or wire change, so a mixed
1.x and 2.0 fleet converges while you do.
- **Agents re-integrate** (the §0 check fires on the major): local ids are refused, `tags` is a JSON list,
  and every write passes `--source`. `docs/AGENT_INTEGRATION.md` §7 has the details.
- **Owner and operator commands run as `hive-mind`.** Run on `hv`, each names its `hive-mind` form and exits
  2, acting on nothing. So do the removed aliases, which name their `hv` replacements.
- **Keys leave the checkout.** `hv doctor --fix` moves the device key, and `hive-mind doctor --fix` the owner
  key. A plaintext owner key still works; `hive-mind owner seal` seals it, and `hv doctor` fails until then.
- **An owned hive whose `forget_writers` is still open fails `forget-authz`.** The 15-minute timer's
  `hv doctor` alerts, and `hive-mind update` ends with ACTION REQUIRED, until the owner runs
  `hive-mind doctor --fix` once. Nothing a hive forgot comes back.
- **`fleet-contract` lists every 1.x peer as behind** on a 2.0 node. That is intended: upgrade it.
- **A build without its bundled crypto refuses to run.**

- **PR 2b, the split: `hv` cannot owner-sign.** `hv` is the agent data plane and `hive-mind` the
  owner/operator control plane. Every code path that reads the owner seed, writes owner-key material or
  produces an owner signature now lives on the control plane (`hivemind_ctl.py`, `hivemind_owner.py`), and
  `hv` reaches none of it. Two static tests hold that true: `hv`'s import graph never reaches `ownerkey` or
  the control plane, and no `owner_sig` is produced anywhere `hv` can reach. The second one catches an
  inline signer, which imports nothing. Each check is shown failing on mutants in the suite.
- **Moved commands point, exit 2, and act on nothing.** These moved: `hv owner` (init, export, import,
  standby, escrow, restore, nominate, unnominate, claim, transfer, revoke-escrow, heartbeat, pin), `hv admit`,
  `hv group admit|revoke|deny|change|purge`, `hv config set` (with its `confidence set` and `quorum set` forms),
  `hv unforget` and `hv retract --owner`. Each now prints the exact `hive-mind` command to run, this
  invocation's arguments included, and exits 2 (contract §7's 2.0 amendment). `revoke-escrow` becomes
  `hive-mind owner revoke` (S5), and the three `config … set` forms collapse into `hive-mind config set`.
- **What stays on `hv`:**
  - the reads, and the device-signed dead-man recovery: `owner show`, `owner elections`,
    `owner propose-election --pub` and `owner vote`. A member node that has only `hv` can still elect a
    new owner.
  - `hv owner propose-election --mint` points at the new `hive-mind owner mint`, which no longer
    overwrites a key already on the device without `--force`.
- **Owner-signed links come from the control plane** (decision `h:34cc1dbcd3`). Through `hv`, every link is
  device-signed, including source `manual` on the owner device. The same verb through `hive-mind remember`,
  `decide` or `entity` owner-signs its links, when the source is `manual` (#114). Using owner authority is an
  explicit act: typing `hive-mind`.
- **Owner-policy content writes.** Under `capsule_putters=owner` or `cell_writers=owner`, `hv capsule
  put|rotate|rm` and `hv wire --add` point at `hive-mind`. Under `fertile` they run on `hv` as before,
  device-signed.
- **`doctor --fix` is split by what it touches** (5848736351).
  - `hv doctor --fix`, which the 15-minute timer runs, keeps every data-plane repair: the device key's
    permissions, orphan daemons, restarts, the key announce, the skill relink and the peer-address repoint.
  - The owner key's permissions and the genesis pin are operator state, so they move to
    `hive-mind doctor --fix`. `hv doctor --fix` says when either is needed.
  - `hive-mind update` and the installer now pin through the control plane.
- **Key presence without reading the seed.** `hv whoami`, `hv owner show`, `hv doctor` and the admission hint
  decide whether this device holds the owner key from the key file's metadata and a public sidecar,
  `owner-pub`, which the control plane writes. They never read the seed. They can say *unknown*: permission
  denied is not "absent".
- **No journal or wire change** (S7). A mixed 1.x and 2.0 fleet converges.
- **PR 3a: private keys leave the working tree** (private #27). `HIVE_HOME` is the git checkout, which
  every agent CLI runs in and some index, so a 0600 seed there was one `cat` away from any tool running
  as the user.
  - The seeds now live in a key directory (0700): `$HIVE_KEY_DIR`, else the path recorded in
    `$HIVE_HOME/.key-dir`, else `~/.hive/keys/<sha256 of the checkout's path, 16 hex>`. The files are
    `device-key`, `owner-key` (0600) and `owner-pub` (0644). The pointer keeps a renamed checkout on its
    keys.
  - Keys at the old root paths keep working, with a `keyperm` warning. Each plane's `doctor --fix` moves
    its own: `hv` the device key, `hive-mind` the owner key and its public half. A move never overwrites
    a key already in the key directory, and finishes one that was interrupted after the copy.
  - `keyperm` fails on a key directory that is not 0700.
  - The installer's identity stash saves from and restores into the key directory, and the uninstaller
    removes only the three key files there.
  - `hv whoami`, the doctor `owner` check and the dashboard label the device and owner ids as public ids
    and say where the private keys are, without printing them.
  - Presence (`_owner_key_state`) now answers *held* only for a regular file of a seed's size, so a
    directory or an empty file in the key's place reads as *present*, not held (#159 review).
  - The signed manifest names its one extensionless file, `hv`. A test fails if another runnable
    extensionless file would ship unsigned.
- **PR 3b: the owner key is sealed at rest** (private #27). A 0600 file outside the checkout is still
  readable by every process running as the owner's user, the agents included.
  - Every new owner key (`owner init`, `mint`, `import`, `restore`, `claim`) is written only as
    `owner-key.sealed`: the envelope `owner escrow` already uses, scrypt then ChaCha20-Poly1305, written at
    0600 atomically. The seal is opened once before any plaintext copy is removed.
  - `hive-mind` unlocks it **once per command** (h:af5ecf48c5), from the tty with three tries, or from
    `$HIVE_OWNER_KEY_PASSPHRASE` with one. A cancel or a wrong passphrase exits 1 having signed nothing.
    That variable is separate from `$HIVE_OWNER_PASSPHRASE`, which still encrypts an export or an escrow.
  - `hive-mind owner seal` converts a plaintext key in place: same owner id, no journal change. It
    refuses when two different keys are on the device. `hv owner seal` points at it.
  - Doctor's new `owner-seal` check **fails** while a plaintext owner key exists (the key directory's or
    the legacy root's), and names the fix.
  - `hv` never opens either form: presence is still a `stat`, of `owner-key.sealed` first. A test asserts
    `hv` calls neither the opener nor the sealed-key reader.
  - The identity stash, the keep-hive backup, restore and uninstall carry `.owner-key.sealed`. A stash
    holding the sealed form drops its plaintext copy.
  - The installer asks for the new passphrase once, at bootstrap, for `owner init` and the self-admit.
- **PR 4a: the agent surface.** The adapter-visible breaks contract §7 promised for the next MAJOR.
  - **Local ids are refused** (#59, #64). A bare or kind-prefixed rowid (`118`, `d17`, `i5`) on any flag that
    takes a reference (`remember --resolves/--outcome-of/--supports/--contradicts/--extends`,
    `decide --supersedes/--revoke/--informed`, `retract`, `entity link --fact-id`, `hive-mind unforget`,
    `hive-mind retract --owner`, and the MCP and Hermes inputs behind them) exits 1 with
    `'118' is a local id, which 2.0 no longer accepts: … Pass the sid (h:…) or ref (node_id:seq) that
    hv search shows.`, having written nothing. `--resolves` aborts too, where an unresolvable target
    is still only a warning. MCP `hive_entity`'s `fact_id` is a `str`. Journal reads are unchanged: the
    legacy resolvers still read every historical entry.
  - **`tags` is a JSON list** in `hv search --format json` (#77), as it already was on every `/api/*` row.
    `tag_list` stays, the same list. The Hermes prefetch joins it.
  - **No `$HERMES_AGENT` source fallback** (#119). A write's source is `--source`, else `manual`. The
    Hermes adapter now passes `--source` on `decide`, as it already did on `remember` and `propose`;
    without it every Hermes decision would have read as `manual`, which means a person.
- **The core vocabulary is registered** (#136, #150). `vocabulary.py` lists every bare name core writes or
  reads, in ten categories: entry types, link kinds, governance actions, `set-config` keys, channels,
  behaviour tags, announce kinds, source apps, source context classes and envelope fields. Each name has a
  status (`written`, `legacy`, `read`, or `reserved` for four envelope fields held for the module API), the
  contract version that introduced it, and one line of meaning.
  - [`docs/NAMESPACES.md`](docs/NAMESPACES.md) is generated from it by `scripts/common/gen_namespaces.py`, and
    states the rule: core reserves every bare name, and a module's names take a prefix, `x-<module>:<name>`
    (decision `h:af137f9421`), which the 2.1 module API enforces. A test fails when the page and the generator
    differ.
  - `tests/test_vocabulary.py` holds the registry to the code both ways, by AST at enumerated writer and
    reader sites. A name written or read there that is not registered fails, and so does a registered name
    that nothing writes or reads. Each category has a mutant in the suite.
  - No behaviour change. `hv`'s `_CHANNELS` and `_LINK_EVIDENCE_KINDS` now come from the registry, with the
    same values. No journal or wire change.
- **PR 4b: removals** (from this list's "2.0 removals and changes"). No journal read path changes.
  - **The `sync_daemon.py` shim is gone.** `hive-mind update` repoints an `@reboot` cron fallback that still
    names it, since nothing rewrote that line before.
  - **`hv doctor migrate-identity`** (the one-time 1.3 re-keying, #130) and its `hv migrate-device-identity`
    alias are gone. Both say to run it on a 1.x release first, and exit 2. **`utilities/migrate_journal.py`**
    goes too, with `scripts/common/deploy_node.sh`, its only caller, which had been broken since the scripts
    reorg.
  - **Internal shims** `_is_salient` and `_wire_claude_hooks` are gone. The `trust_score` column is no longer
    written; a new store no longer has it. The pre-1.12 direct-hook names are no longer patterns of their own.
  - **A build missing its bundled crypto refuses to run** (`ed25519`, `x25519` or `chacha20poly1305`), at
    import, so the CLI, the control plane and the sync daemon all stop; through 1.x only `hv doctor` failed.
  - **`fleet-contract`** warns below the current MAJOR, and never below 1.19 (where `link` entries became
    honoured).
  - `smoke.sh`'s entity check expects a `link` entry, as written since 1.19. `crontab` is stubbed in tests.
  - **The 1.x aliases point, exit 2, and act on nothing** (decision `h:af137f9421`). `hv rebuild`, `hv merkle`,
    `hv key` and `hv doctor wire-agent` each print the `hv` command that replaced them, this invocation's
    arguments included (`hv doctor rebuild`, `hv doctor merkle`, `hv config identity …`, `hv wire claude`), and
    exit 2, the same shape as a moved command. They are kept through 2.x and deleted at 3.0. Their table is
    `commandmap.RENAMED`, beside `MOVED`. `hive-mind` suggests the new name too. The installer, the smokes and CI
    call the new names; a test fails if shipped shell or a workflow calls an old one.
- **4c: the pre-genesis forget grandfather is closed by the owner, not by a default flip** (#135 part 2;
  decisions `h:af137f9421`, `h:696638b9b7`).
  - **Nothing reappears.** An unset `forget_writers` still projects as `legacy`. A default that meant `owner`
    only when nothing depends on the grandfather would project the same forgotten set in every case, because
    the two policies disagree exactly on the facts `_forgets_grandfathered` lists, and it would not close
    #122 either. So the projection does not change, and a mixed 1.x/2.0 fleet agrees by construction.
  - **`hv doctor` fails `forget-authz` on an owned hive whose policy is still open**, whether or not a fact
    depends on it. Each dependent fact is listed by `h:` id, the form both remedies accept. A closed hive
    reports `ok`, and a hive with no owner reports nothing.
  - **`hive-mind doctor --fix` closes it.** With nothing depending, it closes at once through the #122 guard.
    Otherwise it lists each dependent fact with its text and asks y/N at a terminal, default N, before the
    owner key is unlocked. `y` runs 1.28's `owner init` routine with its own reason: every dependent forget
    is re-issued owner-signed, then `forget_writers=owner` is set. `N`, a piped answer or no terminal writes
    nothing. `hv doctor --fix` (the 15-minute timer) cannot owner-sign, so it only points there.
  - **`hive-mind update` ends with ACTION REQUIRED** on an owned open hive, after the restart, the rebuild
    and the re-wire: the dependent facts and the one command. It exits non-zero, and prints the note on every
    update until the hive is closed. A closed or unowned hive ends as before.
- **4d (docs): the architecture page describes the two planes.** `docs/HV_ARCHITECTURE.md` is rewritten for
  the split. It covers what each file holds and on which plane, how `hive-mind` installs its owner steps
  into the library, the pointer tables, what stays on `hv` and why, where the keys live, and the tests that
  hold S2. `docs/P2P_DESIGN.md` moves to `docs/history/`, and `INTERNALS.md` no longer calls it the full
  design. `ownerkey.py`'s docstring no longer says `hv` imports it.
- **#150 finished: what a module may add, and the local files** (decision `h:a1e3e7cd73`). `docs/NAMESPACES.md`
  now says, for each category, whether a module may add names. A module may add a link kind or a config key,
  prefixed. It may never add an entry type, a governance action or a channel. A new *Local files* table lists
  the per-node names under `$HIVE_HOME` and in the key directory, and a `via` table lists the verification
  values of a peer sighting. Both are generated from `vocabulary.py` and kept apart from the journal tables,
  and `tests/test_vocabulary.py` holds them to the path literals in the code, both ways. The ref-bearing
  payload fields stay deferred to the 2.1 module API. No behaviour change.
- **The docs name each moved command on the plane it runs on** (decision `h:02010d37e3`). Every reference that told a
  reader to run a command that moved in 2b as `hv …` now says `hive-mind …`. The files are `CLI_REFERENCE.md`,
  `AGENT_INTEGRATION.md`'s command table, `INTERNALS.md`, `SYNC_API.md`, `THREAT_MODEL.md`, the continual-learning
  design and the MCP adapter's `retract` docstring. `CLI_REFERENCE.md`'s owner, group and config sections say which
  verbs stay on `hv` and which moved. `tests/test_docs_name_moved_commands.py` fails when a document names a
  `commandmap.MOVED` command, or `retract --owner` or `owner propose-election --mint`, on `hv`. It excludes §7's
  per-version history, the CHANGELOG, `docs/history/`, and `README.md` and `SECURITY.md`, which #141 reconciles
  (6 and 2 references).
- **The docs state what 2.0 does.** Pages written during 1.x described 4a's changes as future ones: bare local ids
  "still accepted" and "removed at the next MAJOR", `tags` that "becomes the list", and a `$HERMES_AGENT` source
  fallback. `CLI_REFERENCE.md`, `INTERNALS.md` (whose `_BARE_ID_WARNING` no longer exists), the `hive-memory`
  skill and the MCP `hive_search` docstring now say a local id is refused, `tags` is a JSON list, and an omitted
  source is `manual`. `tests/test_docs_state_2_0_behaviour.py` fails if one of those phrases comes back outside
  the history, using the moved-command sweep's exclusions.

## 1.28 — 2026-09-26 · `v1.28.0`

- **Contract 1.28: `hv owner init` leaves a new hive closed (#135 part 1).** Before a hive has an owner it
  has no key to sign a forget with, so an owner-source forget dated before the genesis owner has always been
  honoured unsigned — the grandfather, kept since 1.4 so that adopting governance never resurrected a
  deliberately forgotten fact. 1.25 let an owner close it (`hv config set forget_writers owner`), but
  `legacy` remained the default, so every hive depended on its owner remembering to run that. Now genesis
  does it: at the end of `hv owner init`, every forget that is in effect **only** because it precedes genesis
  is re-issued as one ordinary owner-signed `retract` — the same act `hv retract <fact> --owner` writes — and
  only **then** is `forget_writers=owner` set. Each fact stays forgotten on an owner signature of its own, so
  a hive born here never depends on the grandfather. `owner init` prints which facts it re-issued. The old
  unsigned entries stay in the append-only journal and count for nothing.
- **The order is the design, not an implementation detail.** `hv config set forget_writers owner` refuses
  while closing would bring a fact back, naming each one with its two remedies (#122). Re-issuing first
  empties that list, so the flip has nothing left to decide — and the flip still goes through the same guard.
  That is what makes "closed **and** a fact silently back" unreachable rather than merely unlikely.
- **Failing does not close the hive.** Every retract is prepared and signed before any of them is appended.
  If preparation fails, nothing is appended and the config is not set: the journal is as it was. If an append
  fails after some retracts have landed, the config is still not set and what landed is **not** removed — an
  append-only journal cannot be rolled back, and those entries are ordinary post-genesis owner forgets, so
  nothing is resurrected by their presence. Either way `owner init` prints which facts were re-issued, which
  were not, and the two remedies for each one still open.
- **What is deliberately left alone.** A grandfathered forget whose target is not a fact in this journal
  (`dangling`) is not re-issued: it hides nothing, and minting owner authority over a target that does not
  exist would be a new claim rather than a restatement. A member device's negative-evidence retract is never
  promoted into an owner act — the list re-issued is already limited to owner-source forgets and is used
  unfiltered. `owner init --force`, a deliberate re-genesis, gets the same treatment, because skipping it
  would let a forced re-genesis silently reopen facts. There is no `--no-close` flag: a hive born here starts
  closed, and an operator who wants `legacy` sets it afterwards.
- **`hv doctor` says `closed` even with nothing left to ignore.** `forget-authz` used to report the closed
  state only when there were unsigned pre-genesis forgets to count, so a hive born closed — which has none —
  reported it by silence. It now states it. Advisory output only: no projection, journal or wire change.
- **Compatibility.** No new command and no new flag; nothing an adapter calls moves. Everything written is
  an owner-signed `retract` plus the `set-config` act 1.25 already defined, so the forgotten set converges
  with any peer that honours an owner-signed retract. A **pre-1.25** peer does not know the `forget_writers`
  key at all: it lands the act and keeps its own default. That is harmless here precisely because the
  re-issued retracts are owner-signed post-genesis acts that do not depend on the key — which is the point of
  doing them first. Part (2) of #135, changing the **default** for hives that already exist, stays a 2.0 item.

## 1.27 — 2026-09-26 · `v1.27.0`

- **Contract 1.27: the genesis declaration is pinned (security).** Which `owner` declaration established
  this hive is now recorded on the node, at `$HIVE_HOME/.genesis-pin` (0600, never synced, never journaled,
  never in the signed manifest), and the pin decides the genesis instead of the entry timestamp. Genesis used
  to be "the earliest valid self-signed `owner` act wins", and a journal timestamp is written by whoever wrote
  the entry — so a declaration that arrived later with an earlier timestamp replaced the owner, and the real
  owner's later acts stopped counting. `hv owner init` pins what it writes; `hv owner pin --set` pins an
  existing hive's single declaration and refuses to guess between two; `hv owner pin --fingerprint` pins from
  an invite before the first pull. Ingest refuses any other `owner` act and the projection resolves genesis
  through the pin, so a rival already in the journal loses — nothing is ever removed from it. A node with no
  pin keeps the old rule, and `hv doctor genesis` says so. **A rival a peer already stored is never
  removed**, so a node that refused it and a node that holds it keep different Merkle roots from then on;
  what converges is the projection, once every node pins the same genesis. An unpinned node can project a
  different owner than a pinned peer — visibly, not silently.
- **Unsigned entries can no longer fork a device's chain.** An entry with no signature could take an admitted
  device's future `(node_id, seq)` and become that device's chain tip, after which the device's own entry was
  dropped as a duplicate — two bodies under one key. Now, on a chain this node holds, an unsigned entry above
  the tip or a different body at a held sequence is refused; on a chain it does not hold, only a verified first
  pull is accepted (one batch reproducing the peer's whole advertised chain, window hash for window hash), so a
  fresh node still receives the fleet's historical unsigned entries while a single crafted high sequence cannot
  land. A push never qualifies.
- **A node that has not pinned serves nothing of its journal remotely.** `/sync/hello`, `/sync/chunk` and
  `/sync/ingest` answer 403 until the genesis is pinned; `/hive/info` and `/sync/merkle-root` stay open, and
  loopback is never gated, so the operator can always pin. The node still reaches the hive through its own
  outbound pull — that is how it gets the declaration it then pins. `hive-mind update` pins on upgrade, and the
  periodic `hv doctor --fix` pins when there is exactly one declaration to pin. A hive that has **no
  owner at all** cannot pin, because there is no declaration to pin — so its nodes stop syncing with each
  other until one of them runs `hv owner init`. That is the bootstrap window closing, and it is the point.
- **The invite carries a genesis fingerprint.** `hive-mind invite` prints `<address>/<hive_id>/<owner_id>/<hash8>`,
  and the joining device pins that before it trusts any journal, so a squatter advertising this hive's id cannot
  capture it. The short hash prefix is safe **because the pin binds the declared owner**: a genesis candidate
  must be self-signed by the owner it declares, so a rival would need the victim's owner key, not a ground
  hash prefix. The fingerprint is also served in `/hive/info` and `/sync/hello` **inside** the 1.26 responder
  signature, so it cannot be swapped in transit. Older installers strip the path and still read just the address.
- **`hv doctor` gains `genesis` and `unsigned`.** `genesis` fails on more than one self-signed declaration
  (naming each) or on a pin whose declaration has not synced, and warns when the node is unpinned. `unsigned`
  counts unsigned entries positioned after genesis. `--fix` pins when there is exactly one candidate.
- **Test isolation (#142 class).** The governance projection now reads the pin as well as the journal, so tests
  that project a temp hive in-process point `HIVE_HOME` at it, and the #109 session guard snapshots
  `.genesis-pin` so a test that writes one into the developer's real hive is caught.

## 1.26 — 2026-09-26 · `v1.26.0`

- **Contract 1.26: a peer proves who answered (#107), sync protocol 3.** A `/sync/hello` or `/hive/info`
  asked for with a signed request now carries `hello_sig`: the responder's device signature over the
  caller's nonce, the path, its node id, hive id, advertised address, protocol and a digest of the whole
  answer. The caller checks it (verified, addr-unproven, unadmitted, purged, unsigned or invalid). A new
  setting, `hv sync auth --outbound off|permissive|enforce` (default `permissive`, which only flags), makes
  a node push only to a verified peer: under `enforce` an unsigned, addr-unproven or unadmitted peer is
  only pulled from, and a purged or invalid one is skipped. It is separate from the inbound `sync_auth`,
  because inbound enforce needs peers at protocol 2 and outbound enforce needs protocol 3. `hv doctor`
  gains `peer-identity`, which shows each peer's outcome and protocol and when the fleet is ready for
  either enforce, and `peer-address` can now find a peer that never contacts this node: it asks the
  peer's candidate addresses for a signed `/hive/info` and repoints only to one that proves the right
  device at that address. An address that answers is never rewritten. One new flag; no journal change;
  a mixed fleet keeps syncing.
- **CI and the pre-sign gate run the test suite in parallel.** `pytest-xdist` (pinned 3.8.0) runs the same full
  suite on every core: the CI test jobs use `-n auto`, and `sign_release.py`'s gate does too whenever xdist is
  installed (a local run without it still runs the whole suite, serially). The re-sign after a merge drops
  from about 4.5 minutes to under 180 seconds, inside the `hive-mind update` wait window. What gets signed
  is unchanged: only a tree whose full suite passed. No contract change.


## 1.25.1 — 2026-09-25 · `v1.25.1`

No contract change.

- **Ingest validates entry timestamps before writing (security fix).** An entry's timestamp names its journal
  file, so ingest now rejects an entry whose timestamp is not an ISO-8601 time, and every writer refuses a
  date prefix that is not `YYYY-MM-DD` or a path outside `journal/`. Both shapes already in journals stay
  valid, and lines on disk are read unchanged. A security advisory accompanies this release.
- **`actions/create-github-app-token` bumped to v3.2.0** (#137), still pinned to a commit SHA.

## 1.25 — 2026-09-25 · `v1.25.0`

Contract 1.25, tagged 2026-09-25 at df0d671.

- **Contract 1.25: owner forgets can require a signature (#122, step 2).** A governed config key closes the
  hole `forget-authz` reports: `hv config set forget_writers owner` makes only forgets signed by the owner as
  of their position count, so a backdated unsigned owner forget from an admitted device erases nothing.
  `legacy` stays the default. The key stores no journal refs (orphan-proof, #130), and a legitimate legacy
  forget is kept by re-issuing it signed. Closing refuses while it would bring a fact back, and lists each
  one with its two remedies. No new command or flag; no wire change (a pre-1.25 node ignores the key).

## 1.24.1 — 2026-09-25 · `v1.24.1`

CI and signing only; no contract change.

- **`sign.yml` pushes the re-signed manifest as a dedicated GitHub App (#131, #132).** A short-lived
  installation token for `hivemind-release-signer` replaces the default token, which is now read-only. The App
  is the only bypass of a ruleset that requires the Ubuntu checks on `main`, so the re-sign lands without
  GH006.
- **Every GitHub Action is pinned to a full commit SHA (#133).**

## 1.24 — 2026-09-25 · `v1.24.0`

Contract 1.24, tagged 2026-09-25 at b2fe903.

- **Contract 1.24: revoke a decision (#45).** `hv decide --revoke <sid> --rationale "<why>"` withdraws a
  decision that was wrong, with no replacement. It writes one decision tagged `revocation` (content
  `Revoke <sid>: <the target's first line>` unless given) and one `supersedes` link to the target. The
  target is resolved and kind-checked, and a missing rationale or a combination with `--supersedes` is
  refused, before anything is written. There is no new projection: the target is superseded only when
  the link is hard (a person on the owner device, or the device that wrote the decision). The
  confirmation is derived from that authority, so an agent's revoke elsewhere prints `NOT in effect`
  until the owner re-runs it, and `hv doctor` lists it under `link-authz`. MCP and Hermes `hive_decide`
  gain `revoke` and `supersedes` (the latter had no path since 1.19), and a parity test now covers
  `hv decide`'s flags. No wire change; a pre-1.24 node sees an ordinary superseding decision.
- **Contract 1.24: `hv unforget` reverses an owner forget (#46), and owner forgets are honoured
  point-in-time.** `hv unforget <fact> --reason "<why>"` is owner-only and off MCP. It journals an
  owner-signed `retract` carrying `unretracts_ref`. Per fact text, owner forgets and unforgets are sorted
  on `(timestamp, node_id, seq)` and the latest honoured act wins. An act counts only when signed by the
  owner as of its own position, which fixes a resurrection bug: a previous owner's forget was dropped
  after a transfer, succession or election. An unforget is never grandfathered; a forget before the
  genesis owner still is (#122). Owner acts no longer move a fact's `last_evidence_at`, so an ignored
  forged forget can't refresh a fact's decay clock and an unforgotten fact re-derives from its evidence.
  A pre-1.24 node skips an unforget and keeps the fact forgotten until it upgrades (projection skew).

- **CI: the macOS legs run only where they can matter (#125).** The macOS test and installer smoke
  moved to `ci-macos.yml`. It runs on every push to `main`, and on a pull request only when the diff
  touches `hv`, the daemon, the installer or launchd scripts, or the tests that stub them. The merge
  rule is "CI green, and macOS green when it ran"; the macOS jobs are not required checks
  (CONTRIBUTING.md). No contract change.
- **`hv doctor` shows forgets that need no signature (#122, step 1).** An owner forget dated before
  the genesis owner is honoured unsigned, and ingest does not check timestamps, so an admitted device
  can erase a fact with a backdated forget. The advisory `forget-authz` check names every fact such a
  forget keeps forgotten (undo it with `hv unforget`) and counts the ones whose target is not in the
  journal. No projection or contract change; step 2 closes the hole.

- **The contract history moved out of `hv` (#106).** The per-version notes were one trailing comment on
  `hv`'s `CONTRACT_VERSION` line (18,544 characters), printed whole by every `grep` that matched it, at a
  real token cost for every agent reading the code. They now live in `docs/CONTRACT_HISTORY.md`, one
  section per version, text unchanged. No code change.

## 1.23 — 2026-09-25 · `v1.23.0`

Contract 1.23, tagged 2026-09-25 at 3f6d569.

- **Contract 1.23: links carry owner authority only when a person writes them (#114).** On a machine
  that holds the owner key, a link is owner-signed only when its source is `manual`; every agent
  source is device-signed, as on a member device, so an agent there can no longer `supersedes` or
  `resolves` another device's entries with owner authority. All eight link kinds go through one
  builder (`outcome-of` and `informed` were built inline). `hv decide` and `hv entity link` gain
  `--source` (the MCP server passes `claude-ai`), so an agent's decision is no longer attributed to a
  person. Write confirmations say `(owner-signed: source manual)` or `(device-signed: source …)`. An
  omitted `--source` still means `manual` (#119). No wire change.
- **`hv doctor` catches a sync daemon left on old code (#112).** A bare `git pull` moves the files but
  not the running daemon. The daemon now records the source digest it loaded and serves it on a
  loopback-only `/api/daemon`; the new `daemon-code` check compares it with the same digest of the
  daemon's own checkout (never the checkout doctor runs from) and `--fix` restarts the managed daemon,
  at most once per run. A hive daemon from before this change also warns; another program on the port
  gets no verdict. The 15-minute doctor timer therefore heals a bare pull.

## 1.22 — 2026-09-24 · `v1.22.0`

Contract 1.22, tagged 2026-09-24 at 5c30c20. It also carries the fixes that landed before the tag.

- **Contract 1.22: an `extends` link kind.** `hv remember "…" --extends <sid>` (and MCP
  `hive_remember(extends=…)`, Hermes `hive_remember`) writes the fact plus one `extends` link to a
  fact, idea or decision: "builds on" without evidence semantics. The projection keeps it as an edge
  row in `links` and never weighs it toward confidence on any channel. One relationship per write, so
  it is mutually exclusive with `--resolves`, `--outcome-of`, `--supports` and `--contradicts`.
  `hv search` text shows `extends h:…` on the row; JSON rows are unchanged. No wire change and no
  skew: a pre-1.22 node lands the link and projects it to nothing (#115).
- **The test suite no longer touches the developer's real account (#109).** Every test runs with a
  throwaway `HOME`, `CLAUDE_CONFIG_DIR` and `HIVE_IDENTITY_STASH`, and with service-manager stubs
  first on `PATH`, so `hv doctor --fix` can no longer relink the live `~/.claude/skills/hive-memory`
  into a worktree or restart the live daemon, and `hv owner init` can no longer overwrite the
  owner-key stash. A session guard checks those real paths are unchanged at the end of every run.

## 1.21 — 2026-09-23 · `v1.21.0`

Contract 1.21, tagged 2026-09-24 at b723b12. It also carries the fixes and documentation that landed before the tag.

- **Contract 1.21: the grounding rule covers fact assertions.** A fact written with
  `--channel introspect` no longer counts as corroboration (it weighs `introspect_support_weight`,
  default 0, like an `introspect` link); it is still recorded and can earn confidence from
  observations (#73).
- **Unrecognised channel labels fail closed:** a `channel` outside `sense`/`act`/`introspect` counts
  as `introspect` everywhere; an unrecognised source class keeps full weight, like an absent one,
  since a class can only claim a discount (#72).
- **MCP:** `hive_remember` gains `resolves`, for parity with `hv remember --resolves` (#75).
- **A peer that moves to a new tailnet IP heals itself (#7).** The daemon records a peer's address
  only from requests signed by that peer's admitted device key (`$HIVE_HOME/.peer_candidates.json`,
  local and never journaled). A new `hv doctor` check, `peer-address`, repoints the `.peers.json`
  entry with `--fix` when the stored address fails and the peer has verified itself elsewhere. A
  working address is never rewritten, and `tailscale status` names are only unverified hints. The
  responder-signed hello that would also cover a peer that never contacts this node is #107.
- **`.gitignore`** ignores the local bus log (`.bus/`) and any `*.log` in the checkout. Like the
  store's side files (#74), they hold hive content and could otherwise be committed to the public repo
  by accident (#103).
- **Search JSON:** rows gain `tag_list` (the tags as a list); `tags` stays a JSON-encoded string
  until 2.0, when it becomes the list (#77).
- **Ideas can earn confidence:** `hv remember "<observation>" --supports <sid>` / `--contradicts <sid>`
  (and MCP `hive_remember(supports=…, contradicts=…)`) write the evidence links ideas and facts
  need. An idea's author can't support their own idea but can contradict it. One relationship per
  write: `--resolves`, `--outcome-of`, `--supports` and `--contradicts` are mutually exclusive, so
  `--resolves` with `--outcome-of`, previously accepted, is refused (#71).
- **Hermes adapter at parity with MCP (#76):** the Hermes plugin gains `hive_remember` (outcomes,
  corrections, `supports`/`contradicts`), `hive_decide` (with `informed_by`) and `hive_propose`, and
  `hive_search` gains `kind`. All writes, including the `memory()` mirror, run on one bounded
  background writer, so a turn never waits on the hive. The unused root `hermes_integration.py` is
  removed.
- **`hive-mind update` no longer aborts on a busy store (#93).** It now refreshes the units, restarts
  the daemon and waits for it, *then* rebuilds (the old daemon could hold the store's lock through a
  minutes-long rebuild, past the 10 s busy timeout, which aborted the update and skipped the restart
  and hooks). A rebuild that still finds the store busy warns and continues; the daemon catches up on
  its next cycle. A pull that lands before the release re-sign waits for it (up to 3 minutes) and
  pulls it, or says it is pending, and `hv doctor`'s `authenticity` check marks a clean checkout on a
  commit under 30 minutes old advisory instead of failed while the re-sign is pending.
- **Agent write guidance teaches the grounding rule (#99):** the Claude Code skill, the MCP server
  instructions and the Hermes context block now tell agents to write their own conclusions, analysis
  and plans with `--channel introspect` (only if worth finding later), and that tags help readers but
  never change how much a fact counts as evidence. A new test (`tests/test_agent_guidance.py`) keeps
  all three from dropping it.

## 1.20.1 — 2026-09-23 · `v1.20.1`

A patch release: no contract change.

- **Performance fix (#70):** `ed25519.py` is rewritten for speed with an identical accept set and
  byte-identical signatures (roughly 60–180× faster), and each signature is verified once per
  process. On a hive of about 860 entries `hv search` drops from about 15 s to 0.3 s and `hv remember`
  from about a minute to 0.7 s, back inside the agent adapters' timeouts (#87). SQLite waits up to 10 s
  for a busy store. A write that can't reach it after its journal append (`remember`, `decide`,
  `propose`, `retract`, `entity`) reports success (journaled) instead of failing, so callers don't
  retry into duplicates, and the next command catches the store up; a busy store never stops a read
  (#95).
- **The sync daemon's bind heals itself (#47):** a daemon that starts before `tailscaled` waits up to
  30 s for the tailnet. If it still lands on loopback, it rebinds within 15 s of the tailnet appearing,
  and on a tailnet IP change within one sync round, by exiting 75 for its service manager to restart
  it. It never moves toward loopback. A new `sync-bind` doctor check, fixed by `--fix`, catches a
  daemon still on the wrong address (#90).
- **`.gitignore`** ignores the store's SQLite side files (`store.db-wal`, `-shm`, `-journal`), which
  hold hive content, so they can't be committed by accident (#74, #80).
- Documentation brought up to date with contract 1.20: a new `SECURITY.md`, `docs/SYNC_API.md`
  rewritten (read authentication, current fields, the `/api/*` surface), `docs/P2P_DESIGN.md` marked
  historical, and README, CLI reference, agent-integration spec, internals, threat model and skills
  updated (#78). This changelog and the version tags were added. The continual-learning design
  record is in `docs/design/` (#68).
- `hv dash` prints only the local URL; the dashboard is refused to other devices (see 1.17).
- Security advisory [GHSA-242f-7fxg-f7wm](https://github.com/projectmentor/hive-mind/security/advisories/GHSA-242f-7fxg-f7wm)
  published (fixed in 1.17).
- CI also runs on Ubuntu 26.04, ahead of the `ubuntu-latest` migration (#89).

## 1.20 — 2026-09-20 · `v1.20.0`

The second half of the continual-learning work.

- **Ideas:** a new `idea` entry type for hypotheses, whose confidence is earned from others' evidence
  and never asserted; `hv propose` and MCP `hive_propose`; `hv search --kind all|fact|decision|idea`;
  open ideas in the session-start digest (#62). *Ideas could not earn confidence in 1.20: nothing
  wrote the links they need (fixed in 1.21, #71).*
- **Trust velocity:** per-device reliability drift, the `trust-drift` doctor advisory and a `DRIFT`
  column in `hv peers`; advisory only (#63).
- **Stable short ids:** every fact, decision and idea has an `h:…` id that is the same on every node,
  shown everywhere and accepted wherever an id is; bare local ids are deprecated (#64).
- **Learned importance and utility:** importance (salience L3) starts at a capped self-hint and rises
  only through other agents' links; utility credits the facts that informed decisions, weighted by
  outcomes; per-class half-lives; `hv search --sort importance|utility|recency` (#65).
- **Write-path switch:** `hv decide --supersedes`, `hv remember --resolves` and `hv entity link` now
  write one `link` entry; the `fleet-contract` doctor advisory lists peers older than 1.19. Upgrade
  always-on nodes first (#66).

## 1.19 — 2026-09-20 · `v1.19.0`

The first half of the continual-learning work.

- **Links:** one generic `link` entry type with an open vocabulary (`supports`, `contradicts`,
  `supersedes`, `resolves`, `entity`, `informed`, `outcome-of`). Links are evidence, not commands:
  only the owner (by signature) or the target's author can supersede or resolve; `link-authz` doctor
  advisory (#57).
- **Informed decisions:** `hv decide --informed` records what a decision relied on (#60).
- **Outcomes:** `hv remember --outcome-of --polarity [--channel]`; decisions gain an outcome score, a
  measure of whether they worked out, never confidence (#61).

## 1.18 — 2026-07-09 · `v1.18.0`

- **`announce`:** a device the owner admitted directly, which never wrote anything, can publish its
  key and so receive capsules (`hv key announce`; `hv doctor --fix` emits it automatically) (#43).
  Upgrade always-on nodes first; older peers show a temporary divergence until they upgrade.
- `_is_salient` renamed `_is_admissible` (the salience L2 admission gate) (#49).

## 1.17 — 2026-06-30 · `v1.17.0`

- **Cell and comb write authorization** under a separate `cell_writers` policy (default: owner only);
  `cell-authz` doctor advisory (#40). *Behaviour change:* `hv wire --add` is owner-only by default.
- **Security fix, 2026-07-08:** sync reads are signed by an admitted device
  (`hv sync auth off|permissive|enforce`, default `permissive`), the dashboard and its data answer only
  local or signed requests, and the daemon binds the Tailscale address instead of all interfaces.
  Fixes [GHSA-242f-7fxg-f7wm](https://github.com/projectmentor/hive-mind/security/advisories/GHSA-242f-7fxg-f7wm)
  (#42).

## 1.16 — 2026-06-30 · `v1.16.0`

- **Capsule write authorization** enforced where capsules are read, not only in the CLI, under
  `capsule_putters`; a compromised admitted device can no longer overwrite or delete a capsule;
  `capsule-authz` doctor advisory (#39).

## 1.15 — 2026-06-28 · `v1.15.0`

- **Decisions become taggable and searchable:** `hv decide --tags`; `hv search` returns matching
  decisions (#33).
- **Web dashboard:** `hv dash`, served by the sync daemon (#34–#38).

## 1.14 — 2026-06-25 · `v1.14.0`

- **Standards-anchored crypto:** ChaCha20-Poly1305 (RFC 8439) for capsules and owner-key sealing,
  with a pinned known-answer test; `keyperm` doctor check. *Breaking* for escrows and passphrase
  exports made before 1.14: re-run `hv owner escrow` from a device that holds the owner key.
- Also: `hive-mind reset`, the `hv peers` roster, the invite and seed-peer join flow (Android), sync
  over sub-1280-MTU tailnet paths, and MCP parity with the CLI.

## 1.13 — 2026-06-25 · `v1.13.0`

- **Cells, combs and capsules** as entry types: `hv wire` for self-wiring tools and agents (replacing
  `hv doctor wire-agent`, now a deprecated alias), `hv capsule` for secrets sealed to your devices, a
  crypto self-test and the bundled advisories feed; release signing requires a green test suite.

## 1.12 — 2026-06-22 · `v1.12.0`

- **Self-healing Claude Code integration:** one stable dispatch shim per lifecycle event in
  `~/.claude`, reconciled by `hv doctor --fix`; `agent-hooks` doctor check.

## 1.11 — 2026-06-22 · `v1.11.0`

- **Reconciliation:** `hv remember --resolves` corrects and soft-retracts a fact; the audit's
  CONTRAVENED check flags corrections that were never reconciled.

## 1.10 — 2026-06-21 · `v1.10.0`

- **Sync fix:** the Merkle index de-duplicates the journal by `(node_id, seq)`, so a repeated line can
  no longer cause a permanent false divergence.

## 1.9 — 2026-06-14 · `v1.9.0`

- **Owner resilience, part 3:** quorum election with a dead-man switch (`hv owner propose-election`,
  `vote`, `heartbeat`; `hv config quorum`). A live owner can never be unseated.

## 1.8 — 2026-06-14 · `v1.8.0`

- `hv rebuild` folded into `hv doctor rebuild` (the old name still works).

## 1.7 — 2026-06-14 · `v1.7.0`

- **Owner resilience, part 2:** nominated succession (`hv owner nominate` / `claim`), immediate
  `transfer`, `revoke-escrow`; `owner` doctor check.

## 1.6 — 2026-06-14 · `v1.6.0`

- **Owner resilience, part 1:** `hv owner export` / `import`, in-hive escrow (`escrow` / `restore`),
  `standby`.

## 1.5 — 2026-06-13 · `v1.5.0`

- **Membership lifecycle:** `hv group` (admit, revoke, deny, change, purge, list); `hv config
  identity` and `hv config confidence`; `hv merkle` and `migrate-device-identity` folded under
  `hv doctor`.

## 1.4 — 2026-06-08 · `v1.4.0`

- **Governed confidence:** journaled, owner-signed governance; the same-device discount; the
  admission gate; `cap_self`. An owner forget requires the owner key.
- Also: read-only membership until admitted, `hv whoami`, installer self-healing and the `hv doctor`
  timer, MCP nudge, audit and telemetry tools.

## 1.3 — 2026-06-08 · `v1.3.0`

- **Cryptographic device identity:** Ed25519 device keys; entries are signed and verified on ingest;
  migration tooling.

## 1.2 — 2026-06-07 · `v1.2.0`

- `hv remember` auto-tags transient status claims `volatile`; `hv doctor`.

## 1.1 — 2026-06-07 · `v1.1.0`

- A local-only telemetry lane (`hv telemetry`) that is never journaled or synced.

## 1.0 — 2026-06-07 · `v1.0.0`

- The first versioned contract: the `hv` CLI over an append-only journal, peer-to-peer Merkle sync,
  confidence from independent corroboration, the nudge-and-audit capture loop, `hv verify`, and the
  installer.
