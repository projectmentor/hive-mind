"""What moved to the control plane, in one table (2.0, public #136).

The split gives `hv` (the agent data plane) and `hive-mind` (the owner/operator control plane) a shared
problem: `hv` must tell an operator where a command went, and `hive-mind` must actually provide it. If
those two lists live in two places they drift, and the failure is a pointer naming a command that does
not exist — the operator is then stuck with no correct next step, which is worse than the command simply
being gone.

So this is the single source of truth, imported by both. A test asserts every entry is reachable in the
control plane and pointed at from `hv`.

Deliberately importable by `hv`: it holds no secret, reads no configuration, and imports nothing. It is
a table. The module `hv` must NOT import is `ownerkey`, and nothing here reaches it.

Each entry maps the OLD `hv` invocation to the NEW `hive-mind` one. `hv` prints the new form and exits
**2** — never 0, which would let a script believe the old command had worked, and never acts. That is a
deliberate amendment to contract §7, which promises the previous major keeps working as deprecated
shims: for a command whose whole purpose is being removed from the data plane, a working shim would keep
an owner-signing path in `hv` and undo S2. The window preserves discoverability, not behaviour.
"""

# (old hv argv prefix) -> (new hive-mind argv prefix, one-line why)
#
# Every target here must resolve to a real command on the control plane, and a test walks the library's
# actual subparsers to prove it. `owner mint` is deliberately ABSENT until the change that implements it:
# advertising a command that does not exist is the failure this table exists to prevent, and it is worse
# than the command being missing, because the operator is told to type something that cannot work.
MOVED = {
    # Owner identity and governance — every one of these produces an owner signature.
    ("owner", "init"):              ("owner init",              "mints and claims the owner key"),
    ("owner", "export"):            ("owner export",            "reads the owner key"),
    ("owner", "import"):            ("owner import",            "installs an owner key"),
    ("owner", "standby"):           ("owner standby",           "owner-signed"),
    ("owner", "escrow"):            ("owner escrow",            "reads and seals the owner key"),
    ("owner", "restore"):           ("owner restore",           "installs an owner key"),
    ("owner", "nominate"):          ("owner nominate",          "owner-signed"),
    ("owner", "unnominate"):        ("owner unnominate",        "owner-signed"),
    ("owner", "claim"):             ("owner claim",             "installs an owner key"),
    ("owner", "transfer"):          ("owner transfer",          "owner-signed"),
    ("owner", "revoke-escrow"):     ("owner revoke",            "owner-signed; S5 renames it here"),
    ("owner", "heartbeat"):         ("owner heartbeat",         "owner-signed"),
    ("owner", "pin"):               ("owner pin",               "operator state: the genesis pin"),

    # Membership. `hv admit` was already an alias; it resolves to the group form on the control plane.
    ("admit",):                     ("group admit",             "owner-signed"),
    ("group", "admit"):             ("group admit",             "owner-signed"),
    ("group", "revoke"):            ("group revoke",            "owner-signed"),
    ("group", "deny"):              ("group deny",              "owner-signed"),
    ("group", "change"):            ("group change",            "owner-signed"),
    ("group", "purge"):             ("group purge",             "owner-signed"),

    # Governed configuration. The three ways to set a value collapse to one here.
    ("config", "set"):              ("config set",              "owner-signed"),
    ("config", "confidence", "set"): ("config set",             "owner-signed; collapsed into `config set`"),
    ("config", "quorum", "set"):    ("config set",              "owner-signed; collapsed into `config set`"),

    # Owner acts on content. `hv retract` without --owner stays: that is peer evidence, not governance.
    ("unforget",):                  ("unforget",                "owner-signed"),
}

# Reads and device-signed acts that deliberately STAY on `hv`, with the reason. Not a pointer table —
# a statement of intent, so a later refactor does not "tidy" one of these into the control plane and
# take a capability with it.
STAYS = {
    ("owner", "show"):              "a read",
    ("owner", "elections"):         "a read",
    ("owner", "propose-election"):  "DEVICE-signed: an admitted member must be able to propose when the "
                                    "owner has gone dark, on a node that has only hv",
    ("owner", "vote"):              "DEVICE-signed: same reason. NOTE the CLI verb is `vote`; "
                                    "`vote-election` is the JOURNAL ACTION name, and using it here made "
                                    "the dead-man guard vacuous",
    ("group", "list"):              "a read",
    ("retract",):                   "peer negative evidence; only --owner is governance",
    ("doctor",):                    "read-only checks, and the peer-address repoint is data-plane self-heal",
}

# `hv retract --owner` and `hv capsule put` under an owner policy are flag-conditional rather than whole
# commands, so they are pointed at from their own code paths rather than from this table.
FLAG_CONDITIONAL = {
    "retract --owner": ("retract --owner", "owner-signed; plain `hv retract` stays"),
    "capsule put (capsule_putters=owner)": ("capsule put", "owner-signed under that policy; reads and "
                                                          "fertile puts stay on hv"),
    "wire --add (cell_writers=owner)": ("wire --add", "owner-signed under that policy"),
    "owner propose-election --mint": ("owner mint", "mints owner-key material; use --pub on hv"),
    "doctor --fix (key perms, genesis pin)": ("doctor --fix", "operator state; the peer-address repoint "
                                                             "stays on hv"),
}

POINTER_EXIT = 2


def pointer_text(old_argv, new_form, why=""):
    """The message `hv` prints for a moved command. Names the exact replacement, because an operator who
    reads this is mid-task and the next thing they need is something to type."""
    old = "hv " + " ".join(old_argv)
    lines = [f"`{old}` moved to the control plane in 2.0.",
             f"  Run: hive-mind {new_form}"]
    if why:
        lines.append(f"  ({why}.)")
    lines.append("  `hv` is the agent data plane and cannot owner-sign; see docs/HV_ARCHITECTURE.md.")
    return "\n".join(lines)


def lookup(argv):
    """The longest matching MOVED prefix for an argv, or None. Longest-first so `config confidence set`
    is not shadowed by `config set`."""
    for n in (3, 2, 1):
        key = tuple(argv[:n])
        if key in MOVED:
            return key, MOVED[key]
    return None
