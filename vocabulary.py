"""The core vocabulary: every bare name hive-mind core reserves (2.0, public #136 and #150).

A journal entry whose type, link kind or governance action a node does not recognise still lands, and
projects to nothing: that is how an older node stays converged with a newer one. The journal is permanent,
so the same rule has a cost. If a module wrote a name that a later core feature then reused, every entry
the module had written would be read with the new meaning, on every node, forever. So core reserves the
bare names listed here, and a module's names take a prefix, `x-<module>:<name>` (decision h:af137f9421).
The 2.1 module API enforces the prefix. 2.0 reserves and documents.

This file is the registry the code is checked against, not a description of it. `tests/test_vocabulary.py`
reads `hv`, `hivemind_owner.py` and the sync modules with `ast`, at enumerated sites, and fails both ways: a
name written or read at one of those sites that is missing here, and a name here that no site writes
(`written`) or reads (`legacy`, `read`). `docs/NAMESPACES.md` is generated from it by
`scripts/common/gen_namespaces.py`, and a test fails when the committed page differs from the output.

It also lists what a module may add per category (rule 4), and, apart from the journal tables, the local
per-node files and the `via` values of a peer sighting (#150, decision h:a1e3e7cd73). Those are not journal
vocabulary; the same test holds them to the code's path literals, both ways.

Deliberately importable by `hv`: it holds no secret, reads no configuration, and imports nothing, like
`commandmap.py`, so `hv`'s import graph (S2) is unchanged. It is a `.py`, so the signed manifest covers it.

Each entry maps a name to:
  status   `written`  core writes it today, and reads it where it has a meaning;
           `legacy`   core no longer writes it, and still projects the entries that carry it;
           `read`     core reads and honours it, and never writes it itself (agents and people do);
           `reserved` no core site uses it yet; reserved for the 2.1 module envelope (envelope fields only).
  since    the contract version that introduced it, best effort from CHANGELOG.md, docs/CONTRACT_HISTORY.md
           and the code's comments. It is a field here, never derived from git history, so a checkout
           without history generates the same document.
  meaning  one line.

Changing a `written` name's meaning is a contract change, whatever this file says. Adding a name here is
how a new core name is reserved: the tripwire fails until the code and this table agree.
"""

WRITTEN = "written"
LEGACY = "legacy"
READ = "read"
RESERVED = "reserved"
STATUSES = (WRITTEN, LEGACY, READ, RESERVED)


def _v(status, since, meaning, **extra):
    record = {"status": status, "since": since, "meaning": meaning}
    record.update(extra)
    return record


# ── journal entry types: the `type` of a journal entry ───────────────────────────────────────────────
ENTRY_TYPES = {
    "capsule":     _v(WRITTEN, "1.13", "A secret sealed to the hive's devices; the latest version per name "
                                       "wins, and a tombstone ends it."),
    "cell":        _v(WRITTEN, "1.13", "An executable definition (a tool or an agent integration) that "
                                       "`hv wire` runs; the latest version per name wins."),
    "comb":        _v(WRITTEN, "1.13", "A named, ordered collection of cells."),
    "decision":    _v(WRITTEN, "1.0", "A decision and its rationale."),
    "entity":      _v(WRITTEN, "1.0", "A named entity (a person, project or system) and its attributes."),
    "entity_fact": _v(LEGACY, "1.0", "The old entity-to-fact link. Not written since 1.20, when an `entity` "
                                     "link replaced it; still projected."),
    "fact":        _v(WRITTEN, "1.0", "An assertion; its confidence is derived from independent "
                                      "corroboration, never asserted."),
    "governance":  _v(WRITTEN, "1.4", "A governance act; its `action` says which one (see Governance "
                                      "actions)."),
    "idea":        _v(WRITTEN, "1.20", "A hypothesis; its confidence is earned from other identities' "
                                       "links, never asserted."),
    "link":        _v(WRITTEN, "1.19", "A typed edge between two entries; its `kind` says which "
                                       "relationship (see Link kinds)."),
    "retract":     _v(WRITTEN, "1.0", "Negative evidence against a fact. From the owner it is a forget, or, "
                                      "carrying `unretracts_ref`, an unforget."),
}

# ── link kinds: the `kind` of a `link` entry ────────────────────────────────────────────────────────
# `evidence` (pos|neg) is the side of a fact's evidence the kind folds into; `hv`'s `_LINK_EVIDENCE_KINDS`
# is derived from it, unchanged.
LINK_KINDS = {
    "contradicts": _v(WRITTEN, "1.19", "Evidence against a fact or an idea.", evidence="neg"),
    "entity":      _v(WRITTEN, "1.19", "Links an entity to a fact."),
    "extends":     _v(WRITTEN, "1.22", "Builds on a fact, idea or decision. An edge only, never evidence on "
                                       "any channel."),
    "informed":    _v(WRITTEN, "1.19", "A decision relied on this entry; it feeds the entry's utility."),
    "outcome-of":  _v(WRITTEN, "1.19", "This fact is an outcome of a decision; it feeds the decision's "
                                       "outcome score, on the `sense` channel only."),
    "resolves":    _v(WRITTEN, "1.19", "This fact corrects another: negative evidence on the target, and "
                                       "provenance when the link is hard.", evidence="neg"),
    "supersedes":  _v(WRITTEN, "1.19", "This decision replaces another, when the link is hard."),
    "supports":    _v(WRITTEN, "1.19", "Evidence for a fact or an idea.", evidence="pos"),
}

# ── governance actions: the `action` of a `governance` entry ───────────────────────────────────────
GOVERNANCE_ACTIONS = {
    "admit":              _v(WRITTEN, "1.4", "Admit a device to the corroboration set, optionally tagging its "
                                             "principal. Owner-signed."),
    "announce":           _v(WRITTEN, "1.18", "An authority-less, device-signed announcement, discriminated by "
                                              "`kind` (see Announce kinds). The projection ignores it."),
    "change":             _v(WRITTEN, "1.5", "Re-tag an admitted device's principal. Owner-signed."),
    "claim-succession":   _v(WRITTEN, "1.7", "A nominated successor takes ownership, signed by the new owner "
                                             "key."),
    "deny":               _v(WRITTEN, "1.5", "Drop a pending join-request; a later admit overrides it. "
                                             "Owner-signed."),
    "heartbeat":          _v(WRITTEN, "1.9", "Owner liveness: an owner-signed act with no other effect, which "
                                             "keeps the dead-man switch shut."),
    "join-request":       _v(WRITTEN, "1.4", "A device asks to be admitted. Device-signed; it carries no "
                                             "authority."),
    "nominate-successor": _v(WRITTEN, "1.7", "Nominate a successor owner key. Owner-signed."),
    "owner":              _v(WRITTEN, "1.4", "The genesis declaration, which establishes the hive and its "
                                             "owner. Self-signed."),
    "owner-escrow":       _v(WRITTEN, "1.6", "A passphrase-sealed copy of the owner key, kept in the hive. "
                                             "Owner-signed."),
    "propose-election":   _v(WRITTEN, "1.9", "An admitted device proposes a new owner, installed only past "
                                             "`dead_man_days` of owner silence. Device-signed."),
    "purge":              _v(WRITTEN, "1.5", "Tombstone a device, permanently. Owner-signed."),
    "revoke":             _v(WRITTEN, "1.5", "Un-admit a device; a later admit restores it. Owner-signed."),
    "revoke-escrow":      _v(WRITTEN, "1.7", "Tombstone an owner escrow, so a restore skips it. Owner-signed."),
    "revoke-nomination":  _v(WRITTEN, "1.7", "Withdraw an open successor nomination. Owner-signed."),
    "set-config":         _v(WRITTEN, "1.4", "Set a governed parameter (see Config keys), the same on every "
                                             "node. Owner-signed."),
    "standby":            _v(WRITTEN, "1.6", "Advisory: a device the owner sanctions to also hold the owner "
                                             "key. Owner-signed."),
    "transfer":           _v(WRITTEN, "1.7", "Hand ownership to a new key at once. Owner-signed."),
    "vote-election":      _v(WRITTEN, "1.9", "An admitted device endorses an open election. Device-signed."),
}

# ── set-config keys: every key a `set-config` act may set ───────────────────────────────────────────
CONFIG_KEYS = {
    "cap_self":                  _v(WRITTEN, "1.4", "The confidence ceiling when all corroboration traces to "
                                                    "one principal."),
    "capsule_putters":           _v(WRITTEN, "1.13", "Who may write capsules: `owner` (the default) or "
                                                     "`fertile` (any admitted device)."),
    "cell_writers":              _v(WRITTEN, "1.17", "Who may write cells and combs: `owner` (the default) or "
                                                     "`fertile`."),
    "dead_man_days":             _v(WRITTEN, "1.9", "Days of owner silence before a quorum election can "
                                                    "install a new owner."),
    "forget_writers":            _v(WRITTEN, "1.25", "Whether an unsigned owner forget dated before genesis "
                                                     "still counts: `legacy` or `owner`."),
    "halflife_fact":             _v(WRITTEN, "1.20", "Half-life, in days, of a fact's confidence and "
                                                     "importance."),
    "halflife_idea":             _v(WRITTEN, "1.20", "Half-life, in days, of an idea's confidence and "
                                                     "importance."),
    "halflife_volatile":         _v(WRITTEN, "1.20", "Half-life, in days, of a `volatile`-tagged fact."),
    "importance_self_cap":       _v(WRITTEN, "1.20", "The most a writer's own `--importance` can claim."),
    "introspect_support_weight": _v(WRITTEN, "1.19", "The weight of `introspect`-channel evidence; 0, the "
                                                     "default, means reasoning never moves confidence."),
    "quorum_by":                 _v(WRITTEN, "1.9", "What a quorum counts: `device` or `principal`."),
    "quorum_m":                  _v(WRITTEN, "1.9", "Voter units needed to elect a new owner; 0 turns "
                                                    "elections off."),
    "same_device_lambda":        _v(WRITTEN, "1.4", "The weight of each additional identity on the same "
                                                    "device."),
    "trust_drift_threshold":     _v(WRITTEN, "1.20", "The reliability change below which `hv doctor` "
                                                     "reports trust drift."),
    "trust_long_days":           _v(WRITTEN, "1.20", "The long window, in days, of per-device reliability."),
    "trust_short_days":          _v(WRITTEN, "1.20", "The short window, in days, of per-device reliability."),
    "w_links":                   _v(WRITTEN, "1.20", "The weight of other identities' links in importance."),
    "w_volatile":                _v(WRITTEN, "1.20", "The importance a `volatile` fact gains while it is "
                                                     "fresh."),
}

# ── channels: an entry's `channel`. Closed: an unrecognised channel counts as `introspect` ───────────
CHANNELS = {
    "sense":      _v(WRITTEN, "1.19", "An observation. An entry with no channel is `sense`."),
    "act":        _v(WRITTEN, "1.19", "Something the writer did. Weighs like `sense`, except that a "
                                      "decision's outcome counts `sense` only."),
    "introspect": _v(WRITTEN, "1.19", "Reasoning, not observation: it weighs `introspect_support_weight`. An "
                                      "unrecognised channel counts as this."),
}

# ── behaviour tags: tags whose presence core writes or acts on. `*` ends a prefix ───────────────────
BEHAVIOUR_TAGS = {
    "durable":    _v(WRITTEN, "1.2", "Records an opt-out of the `volatile` auto-tag, so the audit's detection "
                                     "honours it."),
    "revocation": _v(WRITTEN, "1.24", "Marks a decision written by `decide --revoke`; no projection reads it."),
    "ttl:*":      _v(READ, "1.2", "`ttl:<n>h` or `ttl:<n>d`: the audit treats the fact as volatile, with this "
                                  "freshness window."),
    "volatile":   _v(WRITTEN, "1.2", "Transient status: the audit re-checks it past its freshness window, and "
                                     "it decays under `halflife_volatile`."),
}

# ── announce kinds: the `kind` of a governance `announce` ───────────────────────────────────────────
ANNOUNCE_KINDS = {
    "key": _v(WRITTEN, "1.18", "Publishes the device's key, so capsules can be sealed to a device that has "
                               "never written anything."),
}

# ── source apps: the `<app>` of a source, `<app>:<context_class>/<instance>/<session8>` ─────────────
SOURCE_APPS = {
    "manual": _v(WRITTEN, "1.0", "A person, typing; the source of a write that names none. Only a `manual` "
                                 "link is owner-signed through `hive-mind`."),
    "owner":  _v(WRITTEN, "1.4", "Owner governance: a retract from this app is a forget, not evidence."),
}

# ── source context classes: the `<context_class>` of a source ───────────────────────────────────────
SOURCE_CONTEXTS = {
    "cron":     _v(READ, "1.0", "A scheduled job; weighs 0.3."),
    "owner":    _v(WRITTEN, "1.4", "Owner governance in the class slot: a retract from it is a forget. Core "
                                   "writes `owner:owner/owner`."),
    "primary":  _v(READ, "1.0", "A main agent session; weighs 1.0, as does a source with no class."),
    "subagent": _v(READ, "1.0", "A delegated task; weighs 0.5."),
}

# ── envelope fields: the structure every entry is made of. A module never writes its own ────────────
ENVELOPE_FIELDS = {
    "action":    _v(WRITTEN, "1.4", "Which act a `governance` payload records."),
    "from":      _v(RESERVED, "2.0", "Reserved for the module envelope (#150): the entry an edge starts from. "
                                     "A link carries `from_ref` today."),
    "id":        _v(RESERVED, "2.0", "Reserved for the module envelope (#150): an entry's identity, today "
                                     "`node_id:seq` or its `h:` short id."),
    "kind":      _v(WRITTEN, "1.13", "The discriminator inside a typed payload: a link's relationship, an "
                                     "announce's kind, a cell's or capsule's kind."),
    "node_id":   _v(WRITTEN, "1.0", "The authoring device; since 1.3, `k1:` and 16 hex of its key's "
                                    "sha256."),
    "payload":   _v(WRITTEN, "1.0", "The type-specific body."),
    "prev_hash": _v(WRITTEN, "1.0", "The hash of the device's previous entry: its hash chain."),
    "pub":       _v(WRITTEN, "1.3", "The signer's Ed25519 public key."),
    "seq":       _v(WRITTEN, "1.0", "The device's sequence number; `(node_id, seq)` identifies an entry."),
    "sig":       _v(WRITTEN, "1.3", "The device's Ed25519 signature over the entry without `sig`."),
    "target":    _v(RESERVED, "2.0", "Reserved for the module envelope (#150): the entry an act is about. "
                                     "Today `retracts_ref` or `to_ref`."),
    "timestamp": _v(WRITTEN, "1.0", "When the entry was written (ISO 8601); it also names the journal's day "
                                    "file."),
    "to":        _v(RESERVED, "2.0", "Reserved for the module envelope (#150): the entry an edge points to. A "
                                     "link carries `to_ref` today."),
    "type":      _v(WRITTEN, "1.0", "The entry type (see Entry types)."),
}

# ── local files: per-node names under $HIVE_HOME and in the key directory. NOT journal vocabulary ────
# They never enter the journal or the wire. They are listed apart from the journal tables so a module never
# claims one, and so a file name is never mistaken for vocabulary (the #146 category error; #150, decision
# h:a1e3e7cd73). `where`: "hive" is $HIVE_HOME, the checkout; "keys" is the 0700 key directory (2.0 PR 3a).
# tests/test_vocabulary.py holds this table to the path literals in the code, both ways.
LOCAL_FILES = {
    ".bus":                  _v(WRITTEN, "1.10", "Directory for the local event log, `introspect.log`; never synced.",
                                where="hive"),
    ".device-id":            _v(WRITTEN, "1.3", "This device's id, `k1:` and 16 hex of its key's sha256.", where="hive"),
    ".device-key":           _v(LEGACY, "1.3", "The device key's pre-2.0 path in the checkout. It still loads, with a "
                                               "`keyperm` warning, and `hv doctor --fix` moves it.", where="hive"),
    ".genesis-pin":          _v(WRITTEN, "1.27", "Which `owner` declaration established this hive (0600, never "
                                                 "synced).", where="hive"),
    ".key-dir":              _v(WRITTEN, "2.0", "The path of this checkout's key directory, so a renamed checkout keeps "
                                                "its keys.", where="hive"),
    ".nudge_state":          _v(WRITTEN, "1.0", "When the save and audit nudges last fired.", where="hive"),
    ".owner-key":            _v(LEGACY, "1.4", "The owner key's pre-2.0 path in the checkout. `hive-mind doctor --fix` "
                                               "moves it.", where="hive"),
    ".owner-pub":            _v(LEGACY, "2.0", "The owner key's public half at its first 2.0 path. `hive-mind doctor "
                                               "--fix` moves it.", where="hive"),
    ".quarantine.jsonl":    _v(WRITTEN, "2.0", "The entries ingest refused for a malformed or hostile payload, verbatim, with the "
                                                "reason, the signer and the time (an entry over 64 KB as its hash, size and "
                                                "first 4 KB); only admitted signers, the newest 200 records within 16 MB, "
                                                "local, never synced (`hv doctor` reports it).", where="hive"),
    ".quarantine.idx":      _v(WRITTEN, "2.0", "One `hash size` line per quarantine record: the dedupe key and the byte "
                                                "count, so a refusal appends without reading the quarantine; local.",
                              where="hive"),
    ".quarantine.lock":     _v(WRITTEN, "2.0", "The lock the quarantine writers take; empty, local.", where="hive"),
    ".peer_candidates.json": _v(WRITTEN, "1.21", "The addresses each admitted device verified itself from; local, never "
                                                 "journaled (see `via` values).", where="hive"),
    ".peers.json":           _v(WRITTEN, "1.0", "This node's sync settings and its peers' addresses.", where="hive"),
    ".telemetry":            _v(WRITTEN, "1.1", "Directory for the local-only session telemetry store; never synced.",
                                where="hive"),
    "journal":               _v(WRITTEN, "1.0", "The journal's day files: the source of truth, and all that sync "
                                                "carries.", where="hive"),
    "nudge.env":             _v(READ, "1.0", "Optional nudge settings (`KEY=value`), read here or at the repository "
                                             "root.", where="hive"),
    "store.db":              _v(WRITTEN, "1.0", "The SQLite index derived from the journal; `hv doctor rebuild` "
                                                "recreates it.", where="hive"),
    "device-key":            _v(WRITTEN, "2.0", "This device's Ed25519 seed (0600). Not sealed: every entry is "
                                                "device-signed unattended.", where="keys"),
    "owner-key":             _v(LEGACY, "2.0", "An unsealed owner seed. It still loads, `hv doctor` fails until "
                                               "`hive-mind owner seal` seals it, and that removes it.", where="keys"),
    "owner-key.sealed":      _v(WRITTEN, "2.0", "The owner seed, sealed at rest: scrypt, then ChaCha20-Poly1305.",
                                where="keys"),
    "owner-pub":             _v(WRITTEN, "2.0", "The owner key's public half (0644), so `hv` can tell whose key is here "
                                                "without opening it.", where="keys"),
}

# ── `via`: how a `.peer_candidates.json` sighting was verified. Local, never journaled ──────────────
# Only the values the code writes or compares are listed. A sighting with no `via` came from a signed inbound
# request to the daemon.
VIA_VALUES = {
    "outbound": _v(WRITTEN, "1.26", "The sync client saw the device answer a signed `/sync/hello` at this address "
                                    "(#107)."),
}

# ── what a module may add, per journal category (#150 rule 4, decision h:a1e3e7cd73) ──────────────────
# Every name a module adds takes the prefix `x-<module>:` (decision h:af137f9421). Rule 4 is which categories
# a module may extend at all. The link-kind rule is rule 1's contract: `_LINK_RESOLVERS` has no resolver for a
# module's kind, so its entries land and project to nothing (tests/test_links.py).
MODULE_RULES = {
    "entry_types":        "No. A new entry type needs a core projection.",
    "link_kinds":         "Yes, prefixed. It lands, and projects to nothing on a node without a resolver for it.",
    "governance_actions": "No. An action needs the core governance projection.",
    "config_keys":        "Yes, prefixed. An older node ignores a key it does not know.",
    "channels":           "No. Closed: an unrecognised channel counts as `introspect`.",
    "behaviour_tags":     "Only prefixed, like every module name.",
    "announce_kinds":     "Only prefixed. A node accepts an unknown kind and ignores it.",
    "source_apps":        "Only prefixed, like every module name.",
    "source_contexts":    "Only prefixed, like every module name. An unrecognised class weighs 1.0.",
    "envelope_fields":    "No. The core builds the envelope around a module's payload.",
}

# The categories, in the order the generated document lists them: (key, title, what the names are, table).
CATEGORIES = (
    ("entry_types", "Entry types", "The `type` of a journal entry.", ENTRY_TYPES),
    ("link_kinds", "Link kinds", "The `kind` of a `link` entry. An unknown kind lands and projects to nothing.",
     LINK_KINDS),
    ("governance_actions", "Governance actions", "The `action` of a `governance` entry.", GOVERNANCE_ACTIONS),
    ("config_keys", "Config keys", "The keys a `set-config` act may set. An older node ignores a key it does "
                                   "not know.", CONFIG_KEYS),
    ("channels", "Channels", "An entry's `channel`. Closed: a module never adds one, because an unrecognised "
                             "channel counts as `introspect`.", CHANNELS),
    ("behaviour_tags", "Behaviour tags", "Tags core writes or acts on. A name ending in `*` is a prefix.",
     BEHAVIOUR_TAGS),
    ("announce_kinds", "Announce kinds", "The `kind` of a governance `announce`. An unknown kind is accepted "
                                         "and ignored.", ANNOUNCE_KINDS),
    ("source_apps", "Source apps", "The `<app>` of a source, `<app>:<context_class>/<instance>/<session8>`.",
     SOURCE_APPS),
    ("source_contexts", "Source context classes", "The `<context_class>` of a source. An unrecognised class "
                                                  "weighs 1.0.", SOURCE_CONTEXTS),
    ("envelope_fields", "Envelope fields", "The fields an entry is made of. A module never writes its own: the "
                                           "core builds the envelope around a module's payload.",
     ENVELOPE_FIELDS),
)

# ── what `hv` derives from the registry (the values it had before, unchanged) ───────────────────────
CHANNEL_NAMES = tuple(CHANNELS)                                  # hv: _CHANNELS
LINK_EVIDENCE_SIDES = {k: r["evidence"] for k, r in LINK_KINDS.items() if "evidence" in r}  # hv: _LINK_EVIDENCE_KINDS
