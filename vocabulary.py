"""The core vocabulary: every bare name hive-mind core reserves (2.0, public #136 and #150).

A journal entry whose type, link kind or governance action a node does not recognise still lands, and
projects to nothing: that is how an older node stays converged with a newer one. The journal is permanent,
so the same rule has a cost. If a module wrote a name that a later core feature then reused, every entry
the module had written would be read with the new meaning, on every node, forever. So core reserves the
bare names listed here, and a module's names take a prefix, `x-<module>:<name>` (decision h:af137f9421).
The 2.1 module API enforces the prefix (`check_module_name`, below). 2.0 reserves and documents.

This file is the registry the code is checked against, not a description of it. `tests/test_vocabulary.py`
reads `hv`, `hivemind_owner.py` and the sync modules with `ast`, at enumerated sites, and fails both ways: a
name written or read at one of those sites that is missing here, and a name here that no site writes
(`written`) or reads (`legacy`, `read`). `docs/NAMESPACES.md` is generated from it by
`scripts/common/gen_namespaces.py`, and a test fails when the committed page differs from the output.

It also lists what a module may add per category (rule 4), and, apart from the journal tables, the local
per-node files and the `via` values of a peer sighting (#150, decision h:a1e3e7cd73). Those are not journal
vocabulary; the same test holds them to the code's path literals, both ways.

Deliberately importable by `hv`: it holds no secret, reads no configuration, and imports nothing (the
validators below use no `re`), like
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
    "same-as":     _v(WRITTEN, "2.1", "Core only. Joins a module's `x-<module>:` entity (`from_ref`) to an "
                                       "unprefixed entity (`to_ref`); `entity show` lists the joined entity's "
                                       "facts. A module's is refused, and a `retract` naming it withdraws it."),
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
    "from":      _v(RESERVED, "2.0", "Reserved for the module envelope: the ref an edge starts from. A module's own link kind "
                                     "carries it as a pair or an `h:` short id, shape-checked as an `h:` id and resolved when a projection first reads a module link kind; a core kind carries `from_ref`."),
    "id":        _v(RESERVED, "2.0", "Reserved for the module envelope: an entry's identity, `node_id:seq` or its "
                                     "`h:` short id. It names the entry; it is not a field a module writes."),
    "kind":      _v(WRITTEN, "1.13", "The discriminator inside a typed payload: a link's relationship, an "
                                     "announce's kind, a cell's or capsule's kind."),
    "node_id":   _v(WRITTEN, "1.0", "The authoring device; since 1.3, `k1:` and 16 hex of its key's "
                                    "sha256."),
    "payload":   _v(WRITTEN, "1.0", "The type-specific body."),
    "prev_hash": _v(WRITTEN, "1.0", "The hash of the device's previous entry: its hash chain."),
    "pub":       _v(WRITTEN, "1.3", "The signer's Ed25519 public key."),
    "seq":       _v(WRITTEN, "1.0", "The device's sequence number; `(node_id, seq)` identifies an entry."),
    "sig":       _v(WRITTEN, "1.3", "The device's Ed25519 signature over the entry without `sig`."),
    "target":    _v(RESERVED, "2.0", "Reserved for the module envelope: the ref an act is about, resolved like "
                                     "`retracts_ref` or `to_ref`."),
    "timestamp": _v(WRITTEN, "1.0", "When the entry was written (ISO 8601); it also names the journal's day "
                                    "file."),
    "to":        _v(RESERVED, "2.0", "Reserved for the module envelope: the ref an edge points to. A module's own link kind "
                                     "carries it as a pair or an `h:` short id, shape-checked as an `h:` id and resolved when a projection first reads a module link kind; a core kind carries `to_ref`."),
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
    ".module-quota.json":    _v(WRITTEN, "2.1", "Each module device's hourly and daily write times, for the module API's rate "
                                                "limits (0600, never journaled). The lifetime count is the journal's, not this "
                                                "file's.", where="hive"),
    ".modules.json":         _v(WRITTEN, "2.1", "The modules installed on this node: version, pinned publisher key, device id, source and "
                                                "quota limits (0600, never journaled). Written by `hive-mind module`.", where="hive"),
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
    "config_keys":        "Yes, prefixed. `x-<module>:<key>` holds a string the core stores and never interprets. An "
                          "older node ignores a key it does not know.",
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


# ── the module prefix (2.1, plan PR 2; decisions h:af137f9421, h:2294779aaf) ──────────────────────────
# A module adds a name only as `x-<module>:<name>`; a bare name that is not in the registry is refused, so core
# can adopt it later without reinterpreting a module's old entries. Pure functions over the tables above,
# importing nothing: the module API (plan PR 4, 5) and `hv doctor` call them, and `hv` reads a config key's
# prefix in the `set-config` projection.
MODULE_PREFIX = "x-"
MODULE_NAME_MAX = 32
MODULE_KEY_MAX = 64
MODULE_VALUE_MAX = 4096          # the longest value `set-config` stores under a module's config key

_LOWER, _DIGIT = "abcdefghijklmnopqrstuvwxyz", "0123456789"


def valid_module_name(module):
    """A module's name: 1 to 32 characters of `a-z`, `0-9` and `-`, starting with a letter, not ending in `-`."""
    return (isinstance(module, str) and 0 < len(module) <= MODULE_NAME_MAX and module[0] in _LOWER
            and module[-1] != "-" and all(c in _LOWER + _DIGIT + "-" for c in module))


def _valid_local_name(name):
    """The part after `x-<module>:`: 1 to 64 characters of letters, digits, `_`, `.` and `-`, starting alphanumeric."""
    return (isinstance(name, str) and 0 < len(name) <= MODULE_KEY_MAX and name[0].isascii() and name[0].isalnum()
            and all(c.isascii() and (c.isalnum() or c in "_.-") for c in name))


def module_prefix(module):
    return f"{MODULE_PREFIX}{module}:"


def split_module_name(name):
    """`x-<module>:<name>` -> (module, name), or None if `name` is not a well-formed prefixed name."""
    if not isinstance(name, str) or not name.startswith(MODULE_PREFIX) or ":" not in name:
        return None
    module, _, local = name[len(MODULE_PREFIX):].partition(":")
    return (module, local) if valid_module_name(module) and _valid_local_name(local) else None


def _core_has(table, name):
    return name in table or any(k.endswith("*") and name.startswith(k[:-1]) for k in table)


# category -> (core names a module may USE bare, or None when it may use none). A module may use a core link
# kind, tag or source context (they carry no new meaning); it may never write `manual` or `owner` as its source
# app, nor the `owner` context (a retract from either is a forget). `source_apps` is checked on its own below.
# Any other category a module may extend (announce kinds, config keys) takes a prefixed name only.
_BARE_OK = {"link_kinds": LINK_KINDS, "behaviour_tags": BEHAVIOUR_TAGS,
            "source_contexts": {k: v for k, v in SOURCE_CONTEXTS.items() if k != "owner"}}
_NEVER = ("entry_types", "governance_actions", "channels", "envelope_fields")


def check_module_name(module, category, name):
    """None if `module` may introduce `name` in `category` (a key of `CATEGORIES`), else the reason, one line.
    Core names are allowed only where `_BARE_OK` says so; anything else must be `x-<module>:<name>` for this
    module (a module cannot write `x-other:` names). A source app is `x-<module>` with no colon part."""
    if not valid_module_name(module):
        return f"{module!r} is not a valid module name"
    if not isinstance(name, str):
        return f"a name must be a string, not {type(name).__name__}"
    if category in _NEVER:
        return f"a module may not add {category.replace('_', ' ')}: {MODULE_RULES[category]}"
    if category not in MODULE_RULES:
        return f"unknown category {category!r}"
    if category == "source_apps":
        if name == f"{MODULE_PREFIX}{module}":
            return None
        return (f"source app {name!r} must be `{MODULE_PREFIX}{module}`" if name not in SOURCE_APPS else
                f"source app {name!r} is core's: a module's source is `{MODULE_PREFIX}{module}`")
    if category in _BARE_OK and _core_has(_BARE_OK[category], name):
        return None
    parts = split_module_name(name)
    if parts is None:
        return (f"{name!r} is not a core {category.replace('_', ' ')[:-1]} a module may use; a module's own name is "
                f"`{module_prefix(module)}<name>`")
    if parts[0] != module:
        return f"{name!r} belongs to module {parts[0]!r}, not {module!r}"
    return None


# ── ref-bearing payload fields (2.1, plan PR 2; the list #150 deferred) ───────────────────────────────
# Every payload field the code reads as a reference to another entry. `shape`: `pair` is `[node_id, seq]`,
# `pairs` a list of them, `ref` a pair or its `node_id:seq` or `h:` string form, `local_id` a pre-Phase-2 local row id. A ref-walking check (a rebuild, a doctor pass, the module
# API) covers this table, and `tests/test_ref_fields.py` fails when the code reads a ref field that is not listed,
# or a row nothing reads.
# `legacy` rows are read for old journals and never written by core any more. The module envelope's
# `from`, `to` and `target` (see ENVELOPE_FIELDS) are `reserved`: the module API (`POST /v1/entries`) reads and
# shape-checks `from` and `to` on a module's own link kind; `target` has no writer, because a module may not
# retract. They say `ref`: a pair or an `h:` short id, shape-checked as an `h:` id and
# resolved when a projection first reads a module link kind.
REF_FIELDS = {
    "retracts_ref":   {"types": ("retract",), "shape": "pair", "status": WRITTEN,
                       "meaning": "The fact a retract acts on (a forget, when from an owner)."},
    "unretracts_ref": {"types": ("retract",), "shape": "pair", "status": WRITTEN,
                       "meaning": "The fact an owner unforget reverses; a retract carries this or `retracts_ref`."},
    "from_ref":       {"types": ("link",), "shape": "pair", "status": WRITTEN,
                       "meaning": "The entry a link starts from."},
    "to_ref":         {"types": ("link",), "shape": "pair", "status": WRITTEN,
                       "meaning": "The entry a link points to."},
    "informed_by":    {"types": ("decision",), "shape": "pairs", "status": WRITTEN,
                       "meaning": "The entries a decision relied on; a record, not evidence."},
    "revokes":        {"types": ("decision",), "shape": "pair", "status": WRITTEN,
                       "meaning": "The decision a revocation withdraws (the `supersedes` link carries the effect)."},
    "supersedes_ref": {"types": ("decision",), "shape": "pair", "status": LEGACY,
                       "meaning": "The decision a pre-1.19 decision replaced; a `supersedes` link does it now."},
    "resolves_ref":   {"types": ("fact",), "shape": "pair", "status": LEGACY,
                       "meaning": "The fact a pre-1.19 fact resolved; a `resolves` link does it now."},
    "entity_ref":     {"types": ("entity_fact",), "shape": "pair", "status": LEGACY,
                       "meaning": "The entity a pre-1.19 `entity_fact` entry attaches a fact to."},
    "fact_ref":       {"types": ("entity_fact",), "shape": "pair", "status": LEGACY,
                       "meaning": "The fact a pre-1.19 `entity_fact` entry attaches."},
    "entity_id":      {"types": ("entity_fact",), "shape": "local_id", "status": LEGACY,
                       "meaning": "A pre-Phase-2 `entity_fact` entry's entity, as a local id; read when `entity_ref` is absent."},
    "fact_id":        {"types": ("entity_fact",), "shape": "local_id", "status": LEGACY,
                       "meaning": "A pre-Phase-2 `entity_fact` entry's fact, as a local id; read when `fact_ref` is absent."},
    "supersedes":     {"types": ("decision",), "shape": "local_id", "status": LEGACY,
                       "meaning": "A pre-Phase-2 decision's replaced decision, as a local id; read when `supersedes_ref` is absent."},
    "escrow_ref":     {"types": ("governance",), "shape": "ref", "status": WRITTEN,
                       "meaning": "The escrow a `revoke-escrow` act tombstones: `all`, a pair or `node_id:seq`."},
    "from":           {"types": ("link",), "shape": "ref", "status": RESERVED,
                       "meaning": "A module link's start: a pair or an `h:` short id."},
    "to":             {"types": ("link",), "shape": "ref", "status": RESERVED,
                       "meaning": "A module link's end: a pair or an `h:` short id."},
    "target":         {"types": ("retract",), "shape": "ref", "status": RESERVED,
                       "meaning": "What a module act is about: a pair or an `h:` short id."},
}
