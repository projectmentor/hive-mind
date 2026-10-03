"""The owner-signed half of the library: what `hive-mind` can do and `hv` cannot (2.0 PR 2b, public #136).

Requirement S2 is that `hv`, the agent data plane, cannot owner-sign by any route. This file is where every
owner-signing code path now lives: reading the owner seed, producing an `owner_sig`, writing owner-key
material, and the commands that exist only to do those things. `hv` never loads it. Two static tests hold
that true: `hv`'s import graph never reaches `ownerkey` (nor this file), and no `owner_sig` is produced
anywhere `hv` can reach, including by an inline signer that imports nothing.

**How it runs.** `hivemind_ctl.install(lib)` executes this file INTO the library's own namespace, in the
`hive-mind` process only. So the bodies below are the previous ones verbatim — same names, same globals,
same bytes on the wire — and each definition here replaces the data-plane placeholder of the same name in
`hv`, which refuses with the `hive-mind` form to run. A command's structure is written once, in `hv`; this
file supplies only the owner steps (S1, no duplicated logic). Nothing at module level here reads a
library name, so importing this file on its own is harmless, but calling anything in it outside
`install` is not supported.
"""

import base64          # noqa: F401  (the moved bodies use the library's names; these are this file's own)
import os              # noqa: F401

import ownerkey        # the one place the seed is read and an owner signature is produced


# ---- owner-key material: seed, passphrase, stash, signature ------------------------------------------

class OwnerKeyLocked(Exception):
    """The owner key is sealed and was not unlocked: the passphrase was cancelled or wrong, or none was
    given for a new key (2.0 PR 3b). `hivemind_ctl.main` prints it and exits 1. It is raised before
    anything is signed or written, so the journal and the key files are as they were."""


# The owner seed once unlocked, for the rest of THIS process: one `hive-mind` command asks for the
# passphrase at most once, however many signatures it makes (unlock per command, h:af5ecf48c5). Never
# written anywhere; it ends with the process.
_UNLOCKED_SEED = None
_KEY_PASSPHRASE_ENV = "HIVE_OWNER_KEY_PASSPHRASE"


def _key_passphrase(prompt, confirm=False):
    """The at-rest passphrase: $HIVE_OWNER_KEY_PASSPHRASE if set (automation and tests; empty = cancel),
    else the tty. Distinct from $HIVE_OWNER_PASSPHRASE, which encrypts an export or an escrow."""
    return ownerkey.read_passphrase(prompt, confirm, env_var=_KEY_PASSPHRASE_ENV)


def _is_file(p):
    try:
        return p.is_file()
    except OSError:
        return False


def _owner_key_exists():
    """Whether this device keeps an owner key in either form, from the file names alone."""
    return _is_file(OWNER_SEALED_PATH) or _is_file(OWNER_KEY_PATH)


def _owner_seed():
    """The 32-byte owner seed if this device holds the owner key, else None. Never creates.

    Control plane only (2.0 S2). A sealed key (2.0 PR 3b) is unlocked with the at-rest passphrase, once
    per process: three tries at a tty, one when the passphrase comes from the environment. A cancel or a
    wrong passphrase raises `OwnerKeyLocked` rather than returning None, since "this device does not hold
    the owner key" would then be false. A plaintext key still loads; doctor fails until it is sealed.

    Also records the key's PUBLIC half in `OWNER_PUB_PATH`, so `hv` can say whether this device holds the
    established owner's key without ever opening the key file — the sidecar is public material, and a
    missing or stale one only costs `hv` its certainty, never authority, since authority is a signature
    that `hv` cannot produce."""
    global _UNLOCKED_SEED
    if _UNLOCKED_SEED is not None:
        return _UNLOCKED_SEED
    if _is_file(OWNER_SEALED_PATH):
        tries = 1 if os.environ.get(_KEY_PASSPHRASE_ENV) is not None else 3
        seed = None
        for attempt in range(1, tries + 1):
            pw = _key_passphrase("Owner key passphrase (blank line / Ctrl-C to cancel): ")
            if pw is None:
                raise OwnerKeyLocked("Cancelled: the owner key stays sealed, and nothing was signed.")
            try:
                seed = ownerkey.load_sealed(OWNER_SEALED_PATH, pw, _owner_unseal)
                break
            except (ValueError, KeyError) as e:
                if attempt == tries:
                    raise OwnerKeyLocked(f"Could not unlock the owner key ({e}). Nothing was signed.")
                print("Wrong passphrase, try again.", file=sys.stderr)
    else:
        seed = ownerkey.load_seed(OWNER_KEY_PATH)
    if seed is not None:
        _UNLOCKED_SEED = seed
        if _ed25519 is not None:
            _record_owner_pub(_ed25519.pub_from_seed(seed))
    return seed


def _record_owner_pub(pub):
    """Write the owner key's public half into the key directory (0644), if it is missing or different, and
    drop a legacy copy in the working tree. Public material: `hv` reads it for its presence check.
    Best-effort; never raises."""
    global OWNER_PUB_PATH
    try:
        b64 = base64.b64encode(pub).decode()
        target = KEY_DIR / "owner-pub"
        cur = target.read_text().strip() if target.exists() else None
        if cur != b64:
            _ensure_key_dir()
            target.write_text(b64 + "\n")
            try:
                os.chmod(target, 0o644)
            except Exception:
                pass
        if LEGACY_OWNER_PUB_PATH.exists():
            LEGACY_OWNER_PUB_PATH.unlink()
        OWNER_PUB_PATH = target
    except Exception:
        pass


def _write_owner_key(seed, passphrase=None):
    """Write the owner seed SEALED into the key directory (0600, outside the working tree: 2.0 PR 3a and
    3b, private #27), then drop every plaintext copy on this device (the key directory's and the legacy
    root's), so there is never a readable second one, and record its public half.

    Asks for a new at-rest passphrase, with confirmation, unless one is given. A cancel raises
    `OwnerKeyLocked` before anything is written, and every caller writes the key before it signs, so a
    cancelled `owner init` leaves no key and no journal entry. The new seed counts as unlocked for the
    rest of this command."""
    global OWNER_KEY_PATH, _UNLOCKED_SEED
    if passphrase is None:
        passphrase = _key_passphrase("New passphrase for the owner key (asked once per `hive-mind` "
                                     "command; blank line / Ctrl-C to cancel): ", confirm=True)
    if passphrase is None:
        raise OwnerKeyLocked("Cancelled: no owner key was written.")
    _ensure_key_dir()
    ownerkey.write_sealed(OWNER_SEALED_PATH, _owner_seal(seed, passphrase))
    # Open what was written before any plaintext copy goes: a seal that does not round-trip must never
    # cost the operator the only readable copy of the key.
    if ownerkey.load_sealed(OWNER_SEALED_PATH, passphrase, _owner_unseal) != seed:
        raise OwnerKeyLocked(f"The sealed key at {OWNER_SEALED_PATH} did not open to the same seed; "
                             "no plaintext copy was removed.")
    for plain in (KEY_DIR / "owner-key", LEGACY_OWNER_KEY_PATH):
        try:
            plain.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    OWNER_KEY_PATH = KEY_DIR / "owner-key"
    _UNLOCKED_SEED = seed
    if _ed25519 is not None:
        _record_owner_pub(_ed25519.pub_from_seed(seed))
    return OWNER_SEALED_PATH


def _read_passphrase(prompt, confirm=False):
    """Read a passphrase from $HIVE_OWNER_PASSPHRASE if set (automation/tests; empty = cancel), else
    prompt on the tty. Returns the passphrase, or None to CANCEL. Body in `ownerkey.read_passphrase`."""
    return ownerkey.read_passphrase(prompt, confirm)


def _stash_owner_key(seed):
    """Best-effort backup of the owner key to the stable identity stash (survives uninstall; the same
    location as the installer's keep_identity_save). Returns the path written, or None.

    Since 2.0 PR 3b the stash holds the SEALED key, copied from `OWNER_SEALED_PATH`, and a plaintext
    `.owner-key` there is removed (`ownerkey.stash_sealed`). Only a device still on a plaintext key (not
    yet `owner seal`ed) stashes the plaintext form, as before.

    `$HIVE_IDENTITY_STASH` is resolved HERE so that variable keeps exactly one reader; `ownerkey` takes
    the directory as an argument."""
    if _ed25519 is not None:
        _record_owner_pub(_ed25519.pub_from_seed(seed))     # every key write ends here; hv reads the pub
    stash_dir = os.environ.get("HIVE_IDENTITY_STASH", Path.home() / ".config" / "hive-mind" / "identity")
    if _is_file(OWNER_SEALED_PATH):
        return ownerkey.stash_sealed(OWNER_SEALED_PATH, stash_dir)
    return ownerkey.stash(seed, stash_dir)

def _sign_governance_payload(payload, owner_seed, owner_pub):
    """Attach `owner_pub` then `owner_sig` (the owner's signature over the payload sans owner_sig).
    This authorizes the governance ACTION independently of which device appends the entry.

    Body in `ownerkey.sign_governance` — the one place a governance signature is produced, and the
    function the split makes unreachable from `hv`. `_verify_governance` below deliberately does NOT
    move: the projection must keep verifying owner signatures on every node, including agent nodes that
    can never produce one."""
    return ownerkey.sign_governance(payload, owner_seed, owner_pub)


# ---- the owner steps of data-plane commands -------------------------------------------------------------

# `_data_plane_link_payload` is `hv`'s own builder (device-signed links only), saved by `install`.


# The link flags of the verbs whose links this plane owner-signs (decision h:34cc1dbcd3); `entity` links
# with `entity link`.
_LINK_FLAGS = {"remember": ("resolves", "outcome_of", "supports", "contradicts", "extends"),
               "decide": ("supersedes", "revoke", "informed")}


def _will_owner_sign_links(args):
    """Whether this `hive-mind remember|decide|entity` will owner-sign a link: at least one link to write,
    and the write's source is `manual`, by `hv`'s own `_write_source`."""
    cmd = getattr(args, "command", None)
    if cmd == "entity":
        wants = getattr(args, "action", None) == "link"
    else:
        wants = any(getattr(args, f, None) for f in _LINK_FLAGS.get(cmd, ()))
    return bool(wants) and _write_source(args) == "manual"


def _unlock_before_writing(args):
    """Unlock the sealed owner key BEFORE a link verb writes anything (Fable on #167). `remember`, `decide`
    and `entity link` append their fact or decision first and build the link payloads after, and it is
    `_link_payload` that owner-signs on this plane. So a locked key used to raise only once the primary
    entry was journaled: written, device-signed, without the link it asked for. Called by
    `hivemind_ctl.main` ahead of dispatch: a cancel or a wrong passphrase raises `OwnerKeyLocked` with the
    journal untouched, and a success leaves the seed unlocked for the links. A no-op when no owner key is
    on this device, or when no owner-signed link is coming."""
    if _will_owner_sign_links(args) and _owner_key_exists():
        _owner_seed()


def _link_payload(kind, from_ref, to_ref, source, data=None, channel=None):
    """Every link a `hive-mind remember|decide|entity` writes (#114, 2.0 PR 2b, decision h:34cc1dbcd3).
    The payload is built by `hv`'s own builder and then owner-signed, exactly as before the split, when
    this machine holds the owner key AND the write's source is `manual`: a person acting as owner. Through
    `hv` the same verbs are device-signed only — using owner authority is an explicit act, typing
    `hive-mind`."""
    lp = _data_plane_link_payload(kind, from_ref, to_ref, source, data=data, channel=channel)
    seed = _owner_seed() if source == "manual" else None
    if seed is not None and _ed25519 is not None:
        lp = _sign_governance_payload(lp, seed, _ed25519.pub_from_seed(seed))
    return lp


def _owner_forget(ref, reason):
    """The owner step of `hive-mind retract --owner`: build, sign and append the owner-forget. Returns the
    entry, or None when an owner is established and this device cannot sign (the message is printed).
    The body is `retract`'s former owner branch, verbatim."""
    # Owner-forget. Once an owner is established it must be cryptographically authorized:
    # require the owner key and sign the retract, so a bare `--owner` source tag can no longer
    # forge governance. Before any owner exists, the legacy unsigned forget still works.
    gov0 = _governance_state(merkle.read_all_entries(JOURNAL_DIR))
    owner_seed = _owner_seed()
    if gov0["owner_id"] and owner_seed is None:
        print("Owner-forget now requires the owner key (an owner is established). "
              "Run this on the owner machine.")
        return None
    payload = {"retracts_ref": ref, "reason": reason or "", "source": "owner:owner/owner"}
    if owner_seed is not None:
        payload = _sign_governance_payload(payload, owner_seed, _ed25519.pub_from_seed(owner_seed))
    return append_journal("retract", payload)


def _holds_owner_key(what):
    """Whether this device can owner-sign `what` (a capsule or cell write under an owner policy). On the
    control plane that is a real answer: the seed is readable here. `hv`'s placeholder refuses instead."""
    return _owner_seed() is not None


def unforget(args):
    """Owner only (1.24, #46): reverse an owner-forget. Journals an owner-signed `retract` carrying
    `unretracts_ref` (no `retracts_ref`, so a pre-1.24 node skips it: projection skew, never divergence).
    It acts on the fact's CONTENT, like forget: every row with the same text comes back. The latest act
    the owner signed wins (`_content_evidence`), so forget → unforget → forget ends forgotten. The
    fact's confidence re-derives from its surviving evidence. Kept off MCP with the other owner verbs."""
    conn = get_conn()
    try:
        ref = _resolve_id_arg(conn, args.fact_id, "fact", "unforget")
    except ValueError as e:
        conn.close()
        print(f"unforget: {e}", file=sys.stderr)
        sys.exit(1)
    rid = _index_row(conn, ref)
    row = conn.execute("SELECT id, content FROM facts WHERE id = ?", (rid[1],)).fetchone() if rid else None
    sid = _sid_of(conn, "fact", rid[1]) if rid else None
    conn.close()
    if row is None:
        print(f"unforget: no fact {args.fact_id}", file=sys.stderr)
        sys.exit(1)
    content = row["content"]
    entries0 = merkle.read_all_entries(JOURNAL_DIR)
    gov0 = _governance_state(entries0)
    owner_seed = _owner_seed()
    if not gov0["owner_id"] or owner_seed is None or _ed25519 is None:
        print("unforget: needs the owner key of an established owner. Run it on the owner machine.",
              file=sys.stderr)
        sys.exit(1)
    if _owner_id_for_pub(_ed25519.pub_from_seed(owner_seed)) != gov0["owner_id"]:
        print("unforget: the owner key on this device is not the hive's current owner; the projection "
              "would ignore its signature.", file=sys.stderr)
        sys.exit(1)
    if not _content_evidence(entries0, gov0).get(content, {}).get("forget"):
        print(f"unforget: fact {sid} is not forgotten; nothing written.", file=sys.stderr)
        sys.exit(1)
    payload = {"unretracts_ref": ref, "reason": args.reason, "source": "owner:owner/owner"}
    payload = _sign_governance_payload(payload, owner_seed, _ed25519.pub_from_seed(owner_seed))
    uentry = append_journal("retract", payload)
    try:
        conn = get_conn()
        all_entries = merkle.read_all_entries(JOURNAL_DIR)
        gov = _governance_state(all_entries)
        ev = _content_evidence(all_entries, gov).get(
            content, {"pos": {}, "neg": {}, "forget": False, "last_ts": ""})
        conf = _content_confidence(ev, gov)
        _apply_content_confidence(conn, content, ev, gov)
        _mark_projected(conn, all_entries, [uentry])
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as e:                # after the append: never report failure (#70)
        _store_deferred(uentry, e, "Unforget")
    print(f"Fact {sid} (#{row['id']}) UNFORGOTTEN (owner) → confidence {conf:.2f}")


def _authorize_content_payload(payload):
    """If this device holds the owner key, attach an owner signature so the projection can authorize an
    authority-bearing content write (capsule, cell, comb) under the owner policy — the writer PROVES
    owner-key possession, exactly like a governance act (the entry is otherwise only device-signed). A
    no-op without the owner key: a fertile-policy write is authorized by device admission and needs no
    owner_sig. For capsules the owner_sig is NOT part of the AAD (`alg|hive_id|name|version`), so it
    never affects sealing/opening. Mutates and returns `payload`."""
    seed = _owner_seed()
    if seed is not None and _ed25519 is not None:
        return _sign_governance_payload(payload, seed, _ed25519.pub_from_seed(seed))
    return payload


# ---- doctor --fix: the operator-state repairs -------------------------------------------------------

def _fixable_key_files():
    """`hive-mind doctor --fix` re-tightens the owner key as well as the device key (operator state).
    `hv doctor --fix` keeps the device key only, so an agent-only node's 15-minute timer still fixes it."""
    return _secret_key_files()


def _heal_genesis_pin(entries, dry):
    """`hive-mind doctor --fix`: pin the genesis when there is exactly one candidate. Moved off `hv` with
    the rest of the operator state (5848736351); `hv doctor --fix` only says to run this."""
    if _load_genesis_pin() is None and len(_self_signed_owner_acts(entries)) == 1:
        if dry:
            print("\n  would pin this hive's single genesis declaration "
                  f"({GENESIS_PIN_PATH.name}, 0600, never synced).")
        else:
            _p = _auto_pin_genesis(entries)
            if _p:
                print(f"\n--fix: pinned genesis {_p['genesis_ref']} (owner {_p['owner_id']}).")


# ---- governance: every owner-signed act -----------------------------------------------------------------

def _append_governance(action_payload):
    """Owner-sign and append a governance entry, then rebuild so confidence reflects it. Requires
    the owner key on this device. Returns the entry, or None if this device isn't the owner."""
    seed = _owner_seed()
    if seed is None:
        print("This device does not hold the owner key — run governance commands on the owner machine.")
        return None
    pub = _ed25519.pub_from_seed(seed)
    # $HIVE_NOW (when set) dates the entry, so owner-liveness vs. an election basis_ts is drivable
    # deterministically in tests; in production it is unset and the wall clock is used.
    entry = append_journal("governance", _sign_governance_payload(action_payload, seed, pub),
                           timestamp=os.environ.get("HIVE_NOW") or None)
    rebuild_db()
    return entry


_GENESIS_REISSUE_REASON = "re-issued owner-signed at genesis (#135): it was honoured only by the pre-genesis grandfather"


def _close_grandfather_at_genesis(owner_seed, owner_pub, reason=_GENESIS_REISSUE_REASON):
    """#135 part (1), contract 1.28: a hive born by `hive-mind owner init` starts CLOSED, so it never depends on
    the pre-genesis grandfather. Called at the end of `owner init` with the genesis `owner` act already
    appended and pinned: re-issue every forget that is in effect ONLY because it precedes genesis as an
    ordinary owner-signed `retract` — the shape `hive-mind retract <fact> --owner` writes — and only THEN set
    `forget_writers=owner`.

    2.0 (4c, decision h:696638b9b7): `hive-mind doctor --fix` runs the same routine, with its own `reason`,
    on an established hive that never closed, after the owner answers `y` to the list. It mints no owner and
    pins nothing; the re-issues are positioned now, after genesis, and signed by the current owner.

    THE ORDER IS LOAD-BEARING. `_config_set`'s #122 guard refuses to close while `_forgets_grandfathered`'s
    `hides` is non-empty, because closing would silently bring those facts back. Re-issuing first empties
    `hides`, so the flip has nothing left to decide — and the flip still goes through that guard, which is
    what makes "closed AND a fact silently back" unreachable rather than merely unlikely.

    `hides` is taken from `_forgets_grandfathered` UNCHANGED and is not filtered further: it is already
    limited to OWNER-SOURCE forgets, so a member device's negative-evidence retract is never promoted into
    an irreversible owner act. Its second list (`dangling` — a grandfathered forget whose target is not a
    fact in this journal) is NOT re-issued: it hides nothing, and minting owner authority over a target
    that does not exist would be a new claim rather than a restatement. `hv doctor` (`forget-authz`) keeps
    counting those. The old unsigned entries stay in the append-only journal and, once the policy is
    `owner`, count for nothing.

    FAILURE. Every retract is prepared AND SIGNED before any of them is appended.
      • preparation fails  → nothing is appended and the config is not set; the journal is as it was.
      • an append fails    → the config is still not set, and what landed is NOT removed. An append-only
        journal cannot be rolled back, and pretending otherwise would be the worse bug; those entries are
        ordinary post-genesis owner forgets, so nothing is resurrected by their presence.
    Either way both lists are reported, with the same two remedies the #122 guard prints. The one state
    that must be impossible is `forget_writers=owner` together with a fact silently back."""
    entries = merkle.read_all_entries(JOURNAL_DIR)
    gov = _governance_state(entries)
    hides, _dangling = _forgets_grandfathered(entries, gov)
    # Phase 1 — prepare and sign EVERY re-issue before appending any of them.
    prepared = []                                  # [(sid, signed payload)], nothing written yet
    resolved = []                                  # the sids we got as far as naming, for the failure report
    try:
        resolved = [(f["sid"], f["ref"]) for f in _grandfather_facts(entries, hides)]
        for sid, ref in resolved:
            prepared.append((sid, _sign_governance_payload(
                {"retracts_ref": ref, "source": "owner:owner/owner", "reason": reason},
                owner_seed, owner_pub)))
    except Exception as e:
        print(f"  COULD NOT re-issue {len(hides)} pre-genesis forget(s): {e}")
        print("    NOTHING was written: forget_writers stays `legacy`, the grandfather is intact and no")
        print("    fact came back. Decide each of these, then close it yourself:")
        _grandfather_remedy([sid for sid, _ in resolved] or hides)
        return
    # Phase 2 — append them. Only now can the journal change.
    done = []
    for i, (sid, payload) in enumerate(prepared):
        try:
            # $HIVE_NOW (when set) dates the entry, exactly as it dates the genesis act just above, so the
            # re-issue is deterministically positioned AFTER genesis (same node_id, higher seq).
            append_journal("retract", payload, timestamp=os.environ.get("HIVE_NOW") or None)
        except Exception as e:
            left = [s for s, _ in prepared[i:]]
            rebuild_db()
            print(f"  PARTLY re-issued the pre-genesis forget(s) — an append failed: {e}")
            print(f"    re-issued owner-signed: {', '.join(done) or '(none)'}")
            print(f"    NOT re-issued: {', '.join(left)}")
            print("    forget_writers stays `legacy`, so nothing came back. The journal is append-only: the")
            print("    re-issued acts stay as ordinary post-genesis owner forgets and are NOT removed.")
            _grandfather_remedy(left)
            return
        done.append(sid)
    if done:
        rebuild_db()
        print(f"  re-issued {len(done)} pre-genesis forget(s) owner-signed, so no fact here depends on the "
              f"grandfather: {', '.join(done)}")
    # Phase 3 — the flip, through the #122 guard (`hides` is empty now, so it has nothing to decide).
    _config_set("forget_writers", "owner")
    if (_governance_state(merkle.read_all_entries(JOURNAL_DIR)).get("config") or {}).get(
            "forget_writers") == "owner":
        print("  forget_writers=owner: only a forget signed by the owner as of its position hides a fact in")
        print("    this hive (#135). Pre-genesis unsigned forgets count for nothing.")


_FIX_REISSUE_REASON = ("re-issued owner-signed by `hive-mind doctor --fix` (2.0, #135): it was honoured only by "
                       "the pre-genesis grandfather")


def _confirmed(question):
    """A y/N answer from a terminal, default N. No terminal on stdin means no: this is the only interactive
    read on the control plane, and an answer piped in or a closed stdin must never re-sign anything."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        return input(question).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt, OSError):
        return False


def _heal_forget_authz(entries, dry):
    """`hive-mind doctor --fix` closes the pre-genesis forget grandfather (4c, decision h:696638b9b7).

    Nothing depends on it: close at once, through `_config_set`'s #122 guard. The forgotten set cannot change,
    and the open policy is the remaining hole. Facts depend on it: list each by `h:` id with its text, then
    ask y/N (default N) BEFORE unlocking the owner key. `y` runs `_close_grandfather_at_genesis`: every
    dependent forget re-issued owner-signed first, the close last. A forget a member device planted (#122)
    would be re-signed too, which is why the text is shown and the default is N. `N`, or no terminal,
    writes nothing and asks for no passphrase. No owner key here: write nothing, say where to run it."""
    gov = _governance_state(entries)
    c = _forget_authz_check(entries, gov)
    if c is None or not c.get("open"):
        return
    facts = c["facts"]
    if dry:
        print(f"\n  would {'ask to re-sign ' + str(len(facts)) + ' pre-genesis forget(s), then ' if facts else ''}"
              f"close the forget grandfather (forget_writers=owner)")
        return
    if not _owner_key_exists():
        print("\n--fix: the forget grandfather is open, and this device does not hold the owner key. Run "
              "`hive-mind doctor --fix` on the owner machine.")
        return
    if not facts:
        print("\n--fix: closing the forget grandfather; no fact depends on it (#122, #135).")
        _config_set("forget_writers", "owner")
        return
    print(f"\n--fix: {len(facts)} fact(s) are kept forgotten only by an unsigned pre-genesis owner forget. An "
          f"admitted device could have written one (#122), so read each:")
    for f in facts:
        text = " ".join(f["content"].split())
        print(f"  {f['sid']}  {text[:100]}{'…' if len(text) > 100 else ''}")
    if not _confirmed(f"Re-sign these {len(facts)} forget(s) so the facts stay hidden, then close? [y/N] "):
        print("  Nothing was written. To let a fact back instead, or to decide them one by one:")
        _grandfather_remedy([f["sid"] for f in facts])
        return
    seed = _owner_seed()                           # unlocked once, after the answer, for the whole routine
    if seed is None:
        print("  This device does not hold the owner key: nothing was written.")
        return
    _close_grandfather_at_genesis(seed, _ed25519.pub_from_seed(seed), reason=_FIX_REISSUE_REASON)


# `_data_plane_owner_cmd` is `hv`'s own `owner_cmd` (show, elections, propose-election --pub, vote),
# saved by `install` before this file replaces the name.

# The `owner` actions this plane implements. Every other action is `hv`'s and runs there unchanged.
_OWNER_ACTIONS_HERE = {"pin", "export", "import", "standby", "escrow", "restore", "nominate", "unnominate",
                       "claim", "transfer", "revoke-escrow", "heartbeat", "init", "mint", "seal"}


def owner_cmd(args):
    """`hive-mind owner …`: the owner-key and owner-signed actions. The read-only and device-signed
    actions (show, elections, propose-election --pub, vote) are `hv`'s and are handed back to it."""
    action = getattr(args, "owner_action", None) or "show"
    if action not in _OWNER_ACTIONS_HERE:
        return _data_plane_owner_cmd(args)
    entries = merkle.read_all_entries(JOURNAL_DIR)
    gov = _governance_state(entries)
    if action == "mint":
        # 2.0 PR 2b: the minting half of the former `hv owner propose-election --mint`. It writes owner-key
        # material, so it cannot stay on `hv`; proposing the minted key is device-signed and stays there.
        # Unlike the old flag it will not overwrite a key already on this device without --force.
        if _ed25519 is None:
            print("ed25519 unavailable; cannot mint an owner key.")
            return
        if _owner_key_exists() and not getattr(args, "force", False):
            print(f"An owner key already exists in {KEY_DIR}. Use --force to replace it "
                  "(if it is the live owner key, replacing it loses the owner).")
            return
        seed = os.urandom(32)
        HIVE_HOME.mkdir(parents=True, exist_ok=True)
        _write_owner_key(seed)                     # into the key directory, outside the tree (2.0 PR 3a)
        _stash_owner_key(seed)
        pub = _ed25519.pub_from_seed(seed)
        _record_owner_pub(pub)
        new_pub = _canon_pub(base64.b64encode(pub).decode())
        print(f"Minted a prospective owner key on this device ({_owner_id_for_pub(pub)}).")
        print(f"  pub: {new_pub}")
        print(f"  Propose it for election with:  hv owner propose-election --pub {new_pub}")
        return
    if action == "seal":
        # 2.0 PR 3b (private #27): convert a plaintext owner key to the sealed form, in place. The owner
        # id, the journal and every peer are unchanged; only how this device stores the seed changes.
        plain = [q for q in (KEY_DIR / "owner-key", LEGACY_OWNER_KEY_PATH) if _is_file(q)]
        if not plain:
            if _is_file(OWNER_SEALED_PATH):
                print(f"The owner key is already sealed ({OWNER_SEALED_PATH}).")
            else:
                print("No owner key on this device, so there is nothing to seal.")
            return
        seeds = {ownerkey.load_seed(q) for q in plain} - {None}
        if not seeds:
            print(f"{', '.join(map(str, plain))}: not a valid owner key, so there is nothing to seal. "
                  "Nothing was changed.")
            return
        if len(seeds) > 1:
            print(f"Two different plaintext owner keys are on this device ({', '.join(map(str, plain))}). "
                  "Keep the one whose id `hv owner show` names, move the other away, then re-run. "
                  "Nothing was changed.")
            return
        seed = seeds.pop()
        oid = _owner_id_for_pub(_ed25519.pub_from_seed(seed))
        if _is_file(OWNER_SEALED_PATH):
            # Sealed and plaintext side by side: drop the plaintext only if it is the same key.
            if _owner_seed() != seed:
                print(f"A sealed owner key already exists at {OWNER_SEALED_PATH}, and it is a DIFFERENT key "
                      f"from the plaintext {oid}. Nothing was changed; decide which to keep.")
                return
            for q in plain:
                q.unlink()
            print(f"Removed the plaintext copy of owner key {oid}; the sealed one stays at {OWNER_SEALED_PATH}.")
        else:
            _write_owner_key(seed)                 # asks for a new passphrase; verifies; then drops plaintext
            print(f"Sealed owner key {oid} at {OWNER_SEALED_PATH} (0600). The plaintext copy is gone.")
        stashed = _stash_owner_key(seed)
        if stashed:
            print(f"  backup: {stashed} (the stash holds the sealed form now)")
        print("  `hive-mind` asks for this passphrase once per command that owner-signs. Losing it loses")
        print("  this copy of the key: keep an escrow or an export (`hive-mind owner escrow`).")
        return
    if action == "pin":
        pin = _load_genesis_pin()
        cands = _self_signed_owner_acts(entries)
        fp = (getattr(args, "fingerprint", "") or "").strip()
        if fp:
            # A joiner pins from the invite BEFORE its first pull, so it never trusts a journal it has
            # not vetted. The invite carries only a hash PREFIX, which is enough: a squatter can copy a
            # hive_id and an owner_id, but not the genesis entry's hash.
            parts = [x for x in fp.replace("\\", "/").split("/") if x]
            if len(parts) < 3:
                print("A genesis fingerprint looks like <hive_id>/<owner_id>/<hash8>, as `hv owner show`")
                print("  prints it (an invite line may carry the address in front of it).")
                return
            hive_id, owner_id, h8 = parts[-3], parts[-2], parts[-1].lower()
            if not (hive_id.startswith(("h1:", "h2:")) and owner_id.startswith("o1:")
                    and len(h8) >= 8 and all(c in "0123456789abcdef" for c in h8)):
                print(f"That does not parse as a genesis fingerprint: {fp!r}")
                print("  Expected <hive_id>/<owner_id>/<hash8>, e.g. h1:abc.../o1:def.../0a1b2c3d")
                return
            if pin is not None and not getattr(args, "force", False):
                print(f"Already pinned to {pin.get('genesis_ref') or pin.get('genesis_hash8')} "
                      f"(owner {pin.get('owner_id')}). Use --force to replace it.")
                return
            new_pin = _write_genesis_pin({"hive_id": hive_id, "owner_id": owner_id, "genesis_hash8": h8})
            print(f"Pinned the genesis fingerprint for hive {hive_id} (owner {owner_id}, {h8}).")
            print("  Nothing else establishes this hive on this device now. The declaration itself")
            print("  arrives on the first sync, and `hv doctor genesis` confirms it matched.")
            if any(_pin_matches(e, new_pin) for e in cands):
                _write_genesis_pin(_genesis_pin_for(next(e for e in cands if _pin_matches(e, new_pin))))
                print("  It is already in this journal — the pin now names the exact entry.")
            return
        if not getattr(args, "set", False):
            if pin is None:
                print("No genesis pinned on this device.")
                print("  Legacy behaviour applies: the EARLIEST self-signed owner declaration in the")
                print("  journal establishes the hive — and an entry's timestamp is written by whoever")
                print("  made the entry. Pin with `hive-mind owner pin --set`.")
            else:
                print(f"genesis pinned: {pin.get('genesis_ref')}")
                print(f"  owner: {pin.get('owner_id')}")
                print(f"  hive_id: {pin.get('hive_id')}")
                print(f"  hash: {pin.get('genesis_hash') or pin.get('genesis_hash8')}")
                print(f"  file: {GENESIS_PIN_PATH} (private to this node, never synced)")
                if not any(_pin_matches(e, pin) for e in cands):
                    print("  MISMATCH: the pinned declaration is not in this journal. Until it syncs,")
                    print("    this node has no owner and refuses foreign content — see `hv doctor genesis`.")
            if len(cands) > 1:
                print(f"  {len(cands)} self-signed owner declaration(s) present — `hv doctor genesis`")
            return
        if pin is not None and not getattr(args, "force", False):
            print(f"Already pinned to {pin.get('genesis_ref')} (owner {pin.get('owner_id')}).")
            print("  A pin changes only by a deliberate act: `hive-mind owner pin --set --force`, or re-joining.")
            return
        if len(cands) != 1:
            print(f"Not pinning: this journal holds {len(cands)} self-signed owner declaration(s).")
            for e in cands:
                pl = e.get("payload") or {}
                print(f"  {e.get('node_id')}:{e.get('seq')}  owner {pl.get('owner_id')}  "
                      f"hive {pl.get('hive_id')}  {e.get('timestamp')}")
            if len(cands) > 1:
                print("  Two or more declarations means one is a rival. There is NO timestamp tie-break:")
                print("  the timestamp is exactly what an attacker controls. Re-join from the device you")
                print("  trust, using the genesis fingerprint from its `hv owner show`.")
            return
        new_pin = _write_genesis_pin(_genesis_pin_for(cands[0]))
        print(f"Pinned genesis {new_pin['genesis_ref']}")
        print(f"  owner: {new_pin['owner_id']}")
        print(f"  hive_id: {new_pin['hive_id']}")
        print(f"  file: {GENESIS_PIN_PATH} (private to this node, never synced)")
        return
    if action == "export":
        seed = _owner_seed()
        if seed is None:
            print("This device does not hold the owner key — nothing to export.")
            return
        oid = _owner_id_for_pub(_ed25519.pub_from_seed(seed))
        out = Path(getattr(args, "out", None) or f"hive-owner-{oid}.key")
        env = {"hive_id": gov.get("hive_id", ""), "owner_id": oid}
        if getattr(args, "passphrase", False):
            pw = _read_passphrase("Passphrase to encrypt the owner key (blank line / Ctrl-C to cancel): ",
                                  confirm=True)
            if pw is None:
                print("Cancelled — nothing exported.")
                return
            env.update(_owner_seal(seed, pw))
        else:
            env["enc"] = "none"
            env["seed"] = base64.b64encode(seed).decode()
        out.write_text(json.dumps(env, indent=2) + "\n")
        try:
            os.chmod(out, 0o600)
        except Exception:
            pass
        print(f"Owner key exported to {out} (chmod 600).")
        if env.get("enc") == "none":
            print("  UNENCRYPTED — treat this like an SSH private key; anyone holding it is the owner.")
            print("  Re-run with --passphrase to encrypt it for transport.")
        return
    if action == "import":
        try:
            env = json.loads(Path(args.file).read_text())
        except Exception as e:
            print(f"Could not read {args.file}: {e}")
            return
        if env.get("enc", "none") == "none":
            seed = base64.b64decode(env.get("seed", ""))
        else:
            pp = _read_passphrase("Passphrase (blank line / Ctrl-C to cancel): ")
            if pp is None:
                print("Cancelled.")
                return
            try:
                seed = _owner_unseal(env, pp)
            except Exception as e:
                print(f"Could not decrypt: {e}")
                return
        if len(seed) != 32:
            print("Invalid owner key (bad length).")
            return
        imported = _owner_id_for_pub(_ed25519.pub_from_seed(seed))
        if gov["owner_id"] and imported != gov["owner_id"] and not getattr(args, "force", False):
            print(f"Refusing: this key is owner {imported}, but the journal's established owner is "
                  f"{gov['owner_id']}. Importing would create a disagreeing owner. Use --force to override.")
            return
        HIVE_HOME.mkdir(parents=True, exist_ok=True)
        _write_owner_key(seed)                     # into the key directory, outside the tree (2.0 PR 3a)
        _stash_owner_key(seed)
        print(f"Owner key installed ({imported}). This device can now sign governance.")
        return
    if action == "standby":
        enabled = not getattr(args, "off", False)
        if _append_governance({"action": "standby", "device_id": args.device_id, "enabled": enabled}):
            if enabled:
                print(f"Declared standby: {args.device_id}")
                print("  It must hold a copy of the owner key to actually act — give it one with "
                      "`hive-mind owner export` then `hive-mind owner import` on that device.")
            else:
                print(f"Removed standby: {args.device_id}")
        return
    if action == "escrow":
        seed = _owner_seed()
        if seed is None:
            print("This device does not hold the owner key — nothing to escrow.")
            return
        print("This stores your owner key, passphrase-ENCRYPTED, INSIDE the hive journal.")
        print("  • It syncs to EVERY device that reads the hive (including read-only members) and is")
        print("    PERMANENT (append-only — it cannot be un-published). The passphrase is then your")
        print("    hive master key: anyone with hive read access can attempt to crack it offline,")
        print("    forever. Use a STRONG passphrase. (Your off-device `hive-mind owner export` file covers")
        print("    the other case — not trusting the shared store.)")
        pw = _read_passphrase("Strong passphrase to encrypt the owner key (blank line / Ctrl-C to cancel): ",
                              confirm=True)
        if pw is None:
            print("Cancelled — the owner key was NOT escrowed.")
            return
        if len(pw) < 12:
            print("Refusing: passphrase too short (need ≥ 12 chars) — it guards a key readable by every node.")
            return
        oid = _owner_id_for_pub(_ed25519.pub_from_seed(seed))
        if _append_governance({"action": "owner-escrow", "owner_id": oid, "envelope": _owner_seal(seed, pw)}):
            print(f"Owner key escrowed to the hive (encrypted; owner {oid}).")
            print("  Recover on any synced device with:  hive-mind owner restore")
        return
    if action == "restore":
        # gov["escrows"] is already owner-signed, revocation-filtered, and sorted (see
        # _governance_state) — a `revoke-escrow` tombstone drops a compromised blob from here.
        if not gov["escrows"]:
            print("No (live) owner-escrow found in the hive. Sync the hive first (`hv sync now`), or "
                  "recover from a file with `hive-mind owner import <file>`.")
            return
        # latest live escrow wins, but only a scheme this build can open (v1 was retired in 1.14).
        openable = [e for e in gov["escrows"] if e[3].get("enc") == _OWNER_SEAL_ALG]
        if not openable:
            print(f"The only live escrow(s) use a retired scheme ({gov['escrows'][-1][3].get('enc')!r}). "
                  "Re-escrow from a device that still holds the owner key (`hive-mind owner escrow`), or recover "
                  "from a file with `hive-mind owner import <file>`.")
            return
        env = openable[-1][3]
        pp = _read_passphrase("Passphrase (blank line / Ctrl-C to cancel): ")
        if pp is None:
            print("Cancelled.")
            return
        try:
            seed = _owner_unseal(env, pp)
        except Exception as e:
            print(f"Could not decrypt: {e}")
            return
        if len(seed) != 32:
            print("Invalid owner key (bad length).")
            return
        oid = _owner_id_for_pub(_ed25519.pub_from_seed(seed))
        if gov["owner_id"] and oid != gov["owner_id"]:
            print(f"Refusing: the escrowed key is owner {oid}, but the established owner is {gov['owner_id']}.")
            return
        HIVE_HOME.mkdir(parents=True, exist_ok=True)
        _write_owner_key(seed)                     # into the key directory, outside the tree (2.0 PR 3a)
        _stash_owner_key(seed)
        print(f"Owner key recovered from the hive ({oid}). This device can now sign governance.")
        return
    if action == "nominate":
        cp = _canon_pub(args.successor_pub)
        if not cp:
            print("Not a valid owner pubkey (need base64 of a 32-byte ed25519 key, e.g. from "
                  "`hive-mind owner claim --mint` on the successor device).")
            return
        sid = _owner_id_for_pub(base64.b64decode(cp))
        if _append_governance({"action": "nominate-successor", "successor_owner_pub": cp}):
            print(f"Nominated successor {sid}.")
            print("  The successor claims it with `hive-mind owner claim` on the device that holds that key.")
        return
    if action == "unnominate":
        cp = _canon_pub(args.successor_pub)
        if not cp:
            print("Not a valid owner pubkey.")
            return
        if _append_governance({"action": "revoke-nomination", "successor_owner_pub": cp}):
            print(f"Revoked the nomination of {_owner_id_for_pub(base64.b64decode(cp))}.")
        return
    if action == "claim":
        if _ed25519 is None:
            print("ed25519 unavailable; cannot claim ownership.")
            return
        seed = _owner_seed()
        mint = getattr(args, "mint", False) or seed is None
        if mint:
            if seed is not None and not getattr(args, "force", False):
                # Holding a key already — minting a fresh one would discard it. Only refuse if it is
                # the live owner key (losing that is the dangerous case); otherwise allow the reuse.
                held_oid = _owner_id_for_pub(_ed25519.pub_from_seed(seed))
                if held_oid == gov["owner_id"]:
                    print("This device already holds the live owner key. `--force` to mint a NEW key anyway.")
                    return
            seed = os.urandom(32)
        pub = _ed25519.pub_from_seed(seed)
        oid = _owner_id_for_pub(pub)
        cp = _canon_pub(base64.b64encode(pub).decode())
        if oid == gov["owner_id"]:
            print(f"This device's key is already the established owner ({oid}). Nothing to claim.")
            return
        # Install the (possibly freshly minted) key locally so a later claim can reuse it.
        HIVE_HOME.mkdir(parents=True, exist_ok=True)
        _write_owner_key(seed)                     # into the key directory, outside the tree (2.0 PR 3a)
        _stash_owner_key(seed)
        if cp not in gov["nominations"]:
            print(f"Prospective owner key ready on this device: {oid}")
            print(f"  pub: {cp}")
            print(f"  Have the current owner run:  hive-mind owner nominate {cp}")
            print( "  then re-run `hive-mind owner claim` here to take ownership.")
            return
        if _append_governance({"action": "claim-succession", "owner_id": oid}):
            print(f"Claimed ownership: {oid} is now the hive owner (term {gov['owner_term'] + 1}).")
            print("  The previous owner key can no longer sign governance.")
        return
    if action == "transfer":
        cp = _canon_pub(args.new_owner_pub)
        if not cp:
            print("Not a valid owner pubkey (base64 of a 32-byte ed25519 key).")
            return
        nid = _owner_id_for_pub(base64.b64decode(cp))
        if nid == gov["owner_id"]:
            print(f"{nid} is already the owner. Nothing to transfer.")
            return
        print(f"Immediate handoff: ownership moves to {nid}.")
        print("  WARNING: that device must already HOLD this key or governance becomes unsignable.")
        print("  (Use `hive-mind owner nominate` + `hive-mind owner claim` for the safer claim-based handoff.)")
        if _append_governance({"action": "transfer", "new_owner_pub": cp}):
            print(f"Transferred ownership to {nid} (term {gov['owner_term'] + 1}).")
        return
    if action == "revoke-escrow":
        ref = args.ref
        if ref != "all":
            ok = any(f"{nid}:{seq}" == ref for _ts, nid, seq, _env in gov["escrows"])
            if not ok:
                print(f"No live escrow {ref!r}. Live escrows (node_id:seq):")
                for _ts, nid, seq, _env in gov["escrows"]:
                    print(f"  {nid}:{seq}")
                if not gov["escrows"]:
                    print("  (none)")
                print("  Use `all` to tombstone every escrow.")
                return
        if _append_governance({"action": "revoke-escrow", "escrow_ref": ref}):
            print(f"Tombstoned escrow {ref} — `hive-mind owner restore` will skip it.")
            print("  NOTE: the ciphertext stays in the append-only journal forever. If the passphrase")
            print("  leaked, ROTATE the owner key (`hive-mind owner nominate`+`claim`, or `transfer`) so the")
            print("  old escrow blob unlocks a key that is no longer the owner.")
        return
    if action == "heartbeat":
        if _owner_seed() is None:
            print("This device does not hold the owner key — heartbeat must run on a key holder.")
            return
        if _append_governance({"action": "heartbeat"}):
            print("Heartbeat recorded — owner liveness refreshed (resets the dead-man timer).")
        return
    if action == "init":
        if _ed25519 is None:
            print("ed25519 unavailable; cannot create an owner key.")
            return
        if _owner_key_exists() and not args.force:
            print(f"Owner key already exists in {KEY_DIR}. Use --force to replace it.")
            return
        if gov["owner_id"] and not args.force:
            print(f"An owner is already established ({gov['owner_id']}). Use --force to override "
                  "(risks governance disagreement across nodes).")
            return
        seed = os.urandom(32)
        HIVE_HOME.mkdir(parents=True, exist_ok=True)
        _write_owner_key(seed)                     # into the key directory, outside the tree (2.0 PR 3a)
        pub = _ed25519.pub_from_seed(seed)
        oid = _owner_id_for_pub(pub)
        # The genesis owner declaration also mints the hive_id: a public, owner-attested identifier
        # that scopes sync, so two hives on one tailnet never merge their journals. Random (no key).
        hive_id = "h1:" + os.urandom(8).hex()
        genesis = append_journal("governance", _sign_governance_payload(
            {"action": "owner", "owner_id": oid, "hive_id": hive_id}, seed, pub),
            timestamp=os.environ.get("HIVE_NOW") or None)
        # Pin the declaration we just made. Without this the node would accept a rival `owner` act
        # with an earlier timestamp and project IT as the owner (hive-mind-private #14). --force is a
        # deliberate FORK: it re-pins here, and peers that keep the old pin keep the old owner.
        forked = _load_genesis_pin() is not None
        _write_genesis_pin(_genesis_pin_for(genesis))
        rebuild_db()
        stashed = _stash_owner_key(seed)
        print(f"Owner established: {oid}")
        print(f"  hive_id: {hive_id}")
        print(f"  genesis: {genesis['node_id']}:{genesis['seq']}  pinned ({GENESIS_PIN_PATH.name})")
        if forked:
            print("  FORKED: this device re-pinned its genesis. Peers that keep the old pin keep the")
            print("    OLD owner, and the old declaration stays in the journal — it is never removed.")
            print("    Every other device must re-join against this genesis to follow this owner.")
        print(f"  key: {OWNER_SEALED_PATH} (sealed at rest; `hive-mind` asks for its passphrase once per "
              "command. Never commit or sync it)")
        if stashed:
            print(f"  backup: {stashed} (auto-stash; `hive-mind owner export` for a portable copy)")
        # #135 part (1), 1.28: leave this hive closed. Re-issue every forget kept in effect only by the
        # pre-genesis grandfather as an owner-signed one, THEN set forget_writers=owner. --force (a
        # deliberate re-genesis) gets the same treatment: skipping it would silently reopen those facts.
        _close_grandfather_at_genesis(seed, pub)
        print("  Admit your devices next: `hive-mind group admit <device_id> --principal <name>`")


def admit_cmd(args):
    """Admit a device into the corroboration set and optionally tag its principal (owner-only).
    With no device_id, LIST the devices awaiting admission (their pending join-requests).
    On admit, the owner also SEEDS a reciprocal peer from the join-request's advertised URL, so
    sync is symmetric (the owner syncs to the member, not only the member to the owner)."""
    if not getattr(args, "device_id", None):
        entries = merkle.read_all_entries(JOURNAL_DIR)
        pend = _pending_admissions(entries, _governance_state(entries))
        if not pend:
            print("No devices awaiting admission.")
            return
        print(f"Devices awaiting admission ({len(pend)}):")
        for r in pend:
            print(f"  {r['device_id']}  ({r['label'] or '?'})  →  hive-mind group admit {r['device_id']} --principal <name>")
        return
    if args.device_id in _governance_state(merkle.read_all_entries(JOURNAL_DIR))["purged"]:
        print(f"{args.device_id} is purged (tombstoned) — purge is permanent; it cannot be re-admitted.")
        return
    module = getattr(args, "module", None)
    if module is not None and not vocabulary.valid_module_name(module):
        print(f"{module!r} is not a valid module name ({vocabulary.valid_module_name.__doc__.strip()})")
        return
    if module and not args.principal:
        print("--module needs --principal: give the module's device the operator's principal, so `cap_self` bounds it "
              "and it is not its own voting unit under quorum_by=principal.")
        return
    payload = {"action": "admit", "device_id": args.device_id}
    if args.principal:
        payload["principal"] = args.principal
    if module:
        payload["module"] = module
    if _append_governance(payload):
        print(f"Admitted {args.device_id}" + (f" (principal: {args.principal})" if args.principal else "")
              + (f" (module: {module}; it does not vote)" if module else ""))
        if module:
            return          # a module has no join-request and no sync address: no reciprocal peer
        if _add_peer(_join_request_url(args.device_id), args.principal or args.device_id):
            print("  + reciprocal peer added from its join-request URL — the owner now syncs to "
                  "this device too (daemon picks it up next cycle, or `hv sync now`).")


def mint_module_key(name):
    """Mint module `name`'s own device key (2.1, plan PR 3) at `<key dir>/modules/<name>/device-key` (0600, in a
    0700 directory) and return `(device_id, pubkey_b64)`. It is a device key, not owner material: the module
    signs its own entries with it, and the owner then admits it with `admit --module <name>`. Refuses a bad
    name, and refuses to replace a key already there (a module's identity is its key; re-minting one would
    orphan every entry it signed)."""
    if not vocabulary.valid_module_name(name):
        raise ValueError(f"{name!r} is not a valid module name")
    if _ed25519 is None:
        raise RuntimeError("ed25519 module unavailable; cannot create a module key")
    d = _ensure_key_dir() / "modules" / name
    path = d / "device-key"
    if path.exists():
        raise FileExistsError(f"module {name!r} already has a key at {path}")
    d.parent.mkdir(exist_ok=True)
    d.mkdir(exist_ok=True)
    for p in (d.parent, d):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    seed = os.urandom(32)
    _write_private(path, base64.b64encode(seed).decode() + "\n")
    pub = _ed25519.pub_from_seed(seed)
    return _device_id_for_pub(pub), base64.b64encode(pub).decode()


def _group_change(action, device_id, principal=None):
    """Append an owner-signed membership action (revoke/deny/change/purge) + confirm. Owner-only;
    `_append_governance` signs and rebuilds so confidence re-derives over the new governance."""
    payload = {"action": action, "device_id": device_id}
    if principal:
        payload["principal"] = principal
    if not _append_governance(payload):
        return
    msg = {
        "revoke": f"Revoked {device_id} — returned to STERILE (re-admit to restore).",
        "deny":   f"Denied {device_id} — dropped from pending join-requests (a later admit overrides).",
        "change": f"Re-tagged {device_id} → principal: {principal}.",
        "purge":  f"Purged {device_id} (tombstoned) — its entries stay in the journal but no longer "
                  f"count; permanent (cannot be re-admitted).",
    }.get(action, f"{action} {device_id}")
    print(msg)


def _config_set(key, value):
    """Set a journaled, owner-signed tunable parameter (same on every node). Owner-only. Covers every
    governed key, whichever `hv config` sub-namespace it is set from: the confidence knobs
    (same_device_lambda, cap_self, introspect_support_weight), the salience/decay knobs
    (importance_self_cap, w_links, w_volatile, halflife_fact/idea/volatile), the trust-velocity knobs
    (trust_long_days, trust_short_days, trust_drift_threshold), the quorum-election knobs (quorum_m,
    quorum_by, dead_man_days) and the write policies (capsule_putters, cell_writers, forget_writers). A key
    `x-<module>:<key>` is a module's fleet-wide config (2.1): a string, stored and never interpreted."""
    if vocabulary.split_module_name(key):      # 2.1 M3: a module's fleet-wide config, `x-<module>:<key>`, a string
        if not isinstance(value, str) or len(value) > vocabulary.MODULE_VALUE_MAX:
            print(f"bad value for {key}: a string of at most {vocabulary.MODULE_VALUE_MAX} characters")
            return
        if _append_governance({"action": "set-config", "key": key, "value": value}):
            print(f"set {key} = {value}")
        return
    coercers = {"same_device_lambda": float, "cap_self": float,
                "quorum_m": int, "quorum_by": str, "dead_man_days": float,
                "capsule_putters": str, "cell_writers": str, "forget_writers": str,
                "introspect_support_weight": float,
                "trust_long_days": float, "trust_short_days": float, "trust_drift_threshold": float,
                **{k: float for k in _PR6_KNOB_DEFAULTS}}
    if key not in coercers:
        print(f"unknown config key {key!r} (known: {', '.join(sorted(coercers))}, or a module's x-<module>:<key>)")
        return
    try:
        val = coercers[key](value)
    except (TypeError, ValueError):
        print(f"bad value for {key}: {value!r}")
        return
    if key == "quorum_by" and val not in ("device", "principal"):
        print("quorum_by must be 'device' or 'principal'")
        return
    if key in ("capsule_putters", "cell_writers") and val not in ("owner", "fertile"):
        print(f"{key} must be 'owner' (default) or 'fertile' (any admitted device)")
        return
    if key == "forget_writers":
        if val not in ("legacy", "owner"):
            print("forget_writers must be 'legacy' (default: an unsigned owner forget dated before the genesis "
                  "owner still counts) or 'owner' (only forgets signed by the owner as of their position count)")
            return
        # #122 step 2 guard, no flag: closing must not silently bring a fact back. Every fact still kept
        # forgotten only by an unsigned pre-genesis forget needs an explicit owner decision first.
        entries = merkle.read_all_entries(JOURNAL_DIR)
        gov = _governance_state(entries)
        hides, _dangling = _forgets_grandfathered(entries, gov)
        if val == "owner" and hides:
            print(f"Not set: closing the grandfather would bring back {len(hides)} fact(s) kept forgotten only by "
                  f"an unsigned pre-genesis forget (#122). Decide each first, then run this again:")
            for f in _grandfather_facts(entries, hides):
                sid = f["sid"]
                print(f"  {sid}   keep it forgotten: hive-mind retract {sid} --owner   |   let it back: hive-mind unforget {sid} --reason …")
            return
        if val == "legacy" and hides and (gov.get("config") or {}).get("forget_writers", "legacy") == "owner":
            print(f"Note: reopening the grandfather hides {len(hides)} fact(s) again (listed by `hv doctor`, "
                  f"forget-authz).")
    if key == "quorum_m" and val < 0:
        print("quorum_m must be >= 0 (0 disables elections)")
        return
    if key == "introspect_support_weight" and not (0.0 <= val <= 1.0):
        print("introspect_support_weight must be in [0, 1] (default 0: reasoning never moves confidence)")
        return
    if key in ("trust_long_days", "trust_short_days") and val <= 0:
        print(f"{key} must be > 0 days")
        return
    if key == "trust_drift_threshold" and not (-1.0 <= val <= 0.0):
        print("trust_drift_threshold must be in [-1, 0] (a negative delta = reliability falling)")
        return
    if key.startswith("halflife_") and not (val > 0):
        print(f"{key} must be > 0 days (defaults: fact {HALFLIFE_DAYS:g}, idea {HALFLIFE_IDEA_DAYS:g}, "
              f"volatile {HALFLIFE_VOLATILE_DAYS:g})")
        return
    if key in ("importance_self_cap", "w_links", "w_volatile") and not (0.0 <= val <= 1.0):
        print(f"{key} must be in [0, 1]")
        return
    if _append_governance({"action": "set-config", "key": key, "value": val}):
        print(f"set {key} = {val}")
