def _unsigned_refused(e, pin, tips, held_hash, door=()):
    """Whether an UNSIGNED arriving entry is refused (hive-mind-private #14, finding 3; the rule Grok
    4.7 adjudicated). `_verify_entry` returns True for an entry with no `sig` at all, so before this an
    unsigned entry naming an admitted device counted as that device's evidence — and one naming a
    FUTURE `(node_id, seq)` became that device's chain tip, so the device's own later entry was dropped
    as a duplicate: two different bodies under one key, a permanent merkle fork.

    A blanket "reject every unsigned entry once pinned" cannot be the rule: this hive holds 225
    historical unsigned entries under real `k1:` device ids, so nothing about an entry's shape
    distinguishes them from an attack, and a FRESH node must still accept them on its first pull or
    diverge from the fleet for good. So the rule is positional:

      * a chain this node already holds — refuse an entry ABOVE that chain's tip (the squat), and
        refuse a DIFFERENT body at a seq already held;
      * a chain this node does not hold — refuse it unless `door` says this batch IS that chain: one
        contiguous batch from seq 1 whose every window hash matches what an authenticated peer
        advertised. A single pushed high seq is never that batch.

    Returns (refused, why) — `why` is for the operator-facing rejection summary, never the wire."""
    if pin is None or "sig" in e:
        return False, ""
    if _pin_matches(e, pin):
        # The PINNED genesis is never refused for being unsigned. The pin names that exact entry by
        # hash, which is stronger evidence than a device signature — and a hive whose genesis predates
        # device identity has an unsigned one, so without this a pinned joiner could never accept the
        # very declaration it pinned, and would sit failing closed forever.
        return False, ""
    nid, seq = e.get("node_id"), e.get("seq")
    if not isinstance(seq, int):
        return True, "unsigned with no sequence number"
    if nid not in tips:
        if nid in door:
            return False, ""                    # the first pull of a chain we do not hold yet
        return True, "unsigned on a chain this node does not hold (not a verified first pull)"
    if seq > tips[nid]:
        return True, "unsigned above the chain tip this node holds (sequence squat)"
    known = held_hash(nid, seq)
    if known is not None and known != compute_hash(e):
        return True, "unsigned, and a different body is already held at that sequence"
    return False, ""


def _first_pull_door(entries, tips, advertised_chunks):
    """Which not-yet-held chains this batch may bring in UNSIGNED (hive-mind-private #14).

    A fresh node must be able to accept a peer's historical unsigned entries — this hive holds 225 of
    them — without opening the door to a single crafted high-seq entry. So the door opens per node_id
    only when the batch reproduces that node's WHOLE chain exactly as the peer advertised it in
    `/sync/hello`: every window hash equal, from window 0 (seq 1) through the last. Reusing
    `merkle.node_chunk_hashes` means the comparison is the same function both sides already agree on,
    and it is strictly stronger than one batch hash — a missing, extra or altered entry anywhere
    changes its window.

    `advertised_chunks` is passed ONLY by the outbound pull path, which fetched those hashes itself. A
    PUSH to /sync/ingest never carries it, so a pushed chain is never doored. The residual is that an
    admitted peer can advertise a false chain to a node that holds none of it; every established node
    refuses anything past its own tip, so such an entry cannot become fleet truth — and under outbound
    `enforce` the peer's identity is proven first (#107)."""
    if not advertised_chunks:
        return set()
    by_node = {}
    for e in entries:
        nid, seq = e.get("node_id"), e.get("seq")
        if nid is not None and nid not in tips and isinstance(seq, int):
            by_node.setdefault(nid, []).append(e)
    door = set()
    for nid, ents in by_node.items():
        advertised = advertised_chunks.get(nid)
        if not advertised:
            continue
        try:
            if merkle.node_chunk_hashes(list(ents)).get(nid) == list(advertised):
                door.add(nid)
        except Exception:
            continue
    return door


# Authority-less governance (`join-request`, `announce`) lands on a device signature alone, so anyone who can
# push can make this node store it forever and, for a join-request, have the owner later write its fields into
# .peers.json. These are the bounds on what such a payload may carry; ingest refuses what breaks them, and
# `_add_peer` and the join-request readers re-check, so an entry already in a journal cannot reach .peers.json.
AUTHORITYLESS_PAYLOAD_MAX = 2048          # bytes of canonical JSON
_PEER_LABEL_MAX, _PEER_URL_MAX, _PEER_ID_MAX, _PEER_SCALAR_MAX = 80, 256, 64, 256
_PEER_ID_RE = re.compile(r"[A-Za-z0-9:._\-]{1,%d}" % _PEER_ID_MAX)
_PEER_URL_RE = re.compile(r"https?://[A-Za-z0-9._\-\[\]:]{1,200}/?")


def _plain_text(v, limit):
    """A str of 1..limit characters with no control character (it is printed, logged and written to JSON)."""
    return isinstance(v, str) and 0 < len(v) <= limit and v.isprintable()


def _peer_url_ok(v):
    return isinstance(v, str) and len(v) <= _PEER_URL_MAX and bool(_PEER_URL_RE.fullmatch(v))


def _peer_label_ok(v):
    return _plain_text(v, _PEER_LABEL_MAX)


def _wire_label(label):
    """NODE_LABEL as this version writes it into a join-request or announce: printable, at most
    _PEER_LABEL_MAX characters, so the entry passes the bounds `_authorityless_shape_ok` applies to it."""
    clean = "".join(c for c in str(label) if c.isprintable()).strip()[:_PEER_LABEL_MAX].strip()
    return clean or str(NODE_ID)[:_PEER_LABEL_MAX]


def _authorityless_shape_ok(e):
    """Whether a `join-request` or `announce` entry has the size and shape such a payload ever has. A field
    this version does not know is allowed if it is a short scalar, so a later version's field still rides
    through; the whole payload is capped either way."""
    p = e.get("payload")
    if not isinstance(p, dict):
        return False
    try:
        if len(json.dumps(p, sort_keys=True).encode()) > AUTHORITYLESS_PAYLOAD_MAX:
            return False
    except (TypeError, ValueError):
        return False
    action = p.get("action")
    known = {"action"}
    if action == "join-request":
        did = p.get("device_id")
        if not (isinstance(did, str) and _PEER_ID_RE.fullmatch(did) and did == e.get("node_id")):
            return False
        known |= {"device_id", "label", "url", "requested_principal"}
        if "label" in p and not _peer_label_ok(p["label"]):
            return False
        if "url" in p and not _peer_url_ok(p["url"]):
            return False
        if "requested_principal" in p and not _plain_text(p["requested_principal"], _PEER_ID_MAX):
            return False
    elif action == "announce":
        known |= {"kind", "data"}
        if "kind" in p and not _plain_text(p["kind"], 32):
            return False
        data = p.get("data")
        if "data" in p and not (isinstance(data, dict) and len(data) <= 8 and all(
                isinstance(k, str) and len(k) <= 32 and isinstance(v, str) and len(v) <= _PEER_SCALAR_MAX
                for k, v in data.items())):
            return False
    else:
        return False
    return all(k in known or (isinstance(k, str) and len(k) <= 32 and isinstance(v, (bool, int, str))
                              and (not isinstance(v, str) or len(v) <= _PEER_SCALAR_MAX))
               for k, v in p.items())


def append_foreign_entries(entries, advertised_chunks=None):
    """Append FOREIGN journal entries verbatim (preserving node_id/seq) with G-Set dedup by
    (node_id, seq). Two membership gates keep the shared journal sound (D0-v2 two-tier membership):

      • GOVERNANCE: an authority-BEARING act (owner / admit / set-config) must carry a valid OWNER
        signature. An authority-LESS `join-request` is accepted on its device signature alone — it
        grants nothing (`_governance_state` ignores any governance lacking an owner sig), it is only
        a pending ask. Accepting it is what lets the owner's session-start tickle surface the joiner.
      • CONTENT: once an owner is established, content (fact/decision/entity/entity_fact) is accepted
        only from ADMITTED devices. A synced-but-unadmitted device is a READ-ONLY member — it reads
        the whole hive and may tickle to join, but its content does not land in (pollute) the shared
        journal until the owner admits it. A pre-owner (bootstrap) hive accepts everything so the
        genesis owner declaration can propagate.

    The batch is swept in (device, seq) order, so a device's entries are judged against its chain in order
    whatever their type. Content waits for an admit that arrives later in the same batch, and is refused only
    when no sweep can admit it. A device's own revoke in the same sweep is read before that
    device's later entries are judged, so those facts are not stored (#284). A revoke carried on another device's chain is read only if
    that chain is swept first; otherwise the facts are stored, and they do not project. Does NOT persist to SQLite — the caller runs rebuild() once afterwards
    if accepted > 0. Returns (accepted, duplicates)."""
    existing = journal_keys()
    accepted = duplicates = rejected = 0
    arrived_ideas = []
    pin = _load_genesis_pin() or _restore_lost_genesis_pin()   # the one `owner` act this node accepted (hive-mind-private #14)
    tips = {}                        # node_id -> highest seq this node already holds
    for _nid, _seq in existing:
        if isinstance(_seq, int) and _seq > tips.get(_nid, 0):
            tips[_nid] = _seq
    # The envelope first, before any signature is checked or any field is read (SECREV A1): an entry whose
    # node_id, seq, type, prev_hash or payload container has the wrong shape would break every later read.
    # A device's seq may lead the tip this node holds by SEQ_JUMP_MAX plus what the batch itself carries of it.
    in_batch = {}
    for x in entries:
        if isinstance(x, dict) and isinstance(x.get("node_id"), str):
            in_batch[x["node_id"]] = in_batch.get(x["node_id"], 0) + 1
    sound = []
    for x in entries:
        nid = x.get("node_id") if isinstance(x, dict) else None
        reach = tips.get(nid, 0) + in_batch.get(nid, 0) if isinstance(nid, str) else None
        if merkle.envelope_problem(x, reach) is None:
            sound.append(x)
        else:
            rejected += 1
    entries = sound
    _held = {}

    def held_hash(nid, seq):
        """The hash of the entry already held at (nid, seq), or None. Read lazily: only an unsigned
        entry landing on a seq we already hold needs it, which is a re-push, not the common case."""
        if not _held:
            for h in merkle.read_all_entries(JOURNAL_DIR):
                if h.get("node_id") is not None and h.get("seq") is not None:
                    _held[(h["node_id"], h["seq"])] = compute_hash(h)
            _held.setdefault(("", -1), None)          # mark as loaded even for an empty journal
        return _held.get((nid, seq))

    door = _first_pull_door(entries, tips, advertised_chunks)
    unsigned_why = []
    arrived = []                     # (node_id, seq) of each entry that landed, for the local arrival record
    refused_payload = []             # (entry, reason): signed, verified, refused for its payload (#205)
    gov = None
    has_owner = False
    admitted = set()
    gov_dirty = False

    def refresh():
        """The governance state of what this node holds now."""
        nonlocal gov, has_owner, admitted, gov_dirty
        gov = _governance_state(merkle.read_all_entries(JOURNAL_DIR))
        # FAIL CLOSED on a pin whose genesis has not synced yet (a joiner that pinned from an invite
        # fingerprint before its first pull). The projection reports no owner there, and the pre-owner rule
        # accepts ANY device's content — so a pinned-but-not-yet-synced node must be gated, not open.
        has_owner = gov["owner_id"] is not None or bool((gov.get("genesis") or {}).get("mismatch"))
        admitted = gov["admitted"]
        gov_dirty = False

    if entries:
        refresh()
        # Ingest keeps no verdict on the timestamp bounds (#230, #250; David's ruling of 2026-10-06): an entry the
        # bounds would skip is stored all the same and the projection alone skips it (`_ts_violations`), so two
        # nodes holding the same entries project the same set and their journals end equal in every order.
        # The device that signs a marker goes first, so a marker arms the rest of its batch as it would had it
        # arrived alone first.
        # A device carrying an owner-chain act goes first too, so nothing in a batch that carries the genesis is
        # judged as pre-owner.
        markers = {x.get("node_id") for x in entries if x.get("type") == "governance"
                   and isinstance(x.get("payload"), dict)
                   and x["payload"].get("action") in ("freeze-timestamps", "owner", "transfer", "claim-succession")}
        entries = sorted(entries, key=lambda x: (x.get("node_id") not in markers, str(x.get("node_id")),
                                                 x.get("seq") if isinstance(x.get("seq"), int) else 0))

    def _landed(e):
        arrived.append((e["node_id"], e["seq"]))

    def _persist(e):
        _append_line(_journal_path_for(e["timestamp"]), json.dumps(e))   # validated above in both passes

    _DONE, _LANDED, _DEFER = 0, 1, 2

    def judge_governance(e):
        """Governance: owner-signed acts + authority-less join-requests. Never deferred."""
        nonlocal rejected, accepted, duplicates, gov_dirty
        nid, seq = e.get("node_id"), e.get("seq")
        if not isinstance(e.get("payload"), dict):
            rejected += 1               # a payload that is not an object is one bad entry, never an exception
            return _DONE
        refused, why = _unsigned_refused(e, pin, tips, held_hash, door)
        if refused:
            rejected += 1
            unsigned_why.append(why)
            return _DONE
        if (nid, seq) in existing:
            duplicates += 1
            return _DONE
        if not _valid_timestamp(e.get("timestamp")):
            rejected += 1               # the timestamp names the journal file: never persist one that isn't a time
            return _DONE
        if e["payload"].get("action") in ("join-request", "announce") and not _authorityless_shape_ok(e):
            rejected += 1               # checked before the signature: an oversized or misshapen ask is not worth verifying
            return _DONE
        if not _verify_entry(e):
            rejected += 1               # forged/corrupt device signature
            return _DONE
        action = (e.get("payload") or {}).get("action")
        if pin is not None and action == "owner" and not _pin_matches(e, pin):
            rejected += 1
            # A RIVAL genesis. Its owner signature is perfectly valid — it is signed by the attacker's
            # OWN owner key — so only the pin can tell it from ours. Dropping it is deterministic, so
            # every pinned node drops the same act and the journals stay converged.
            return _DONE
        if ((e.get("payload") or {}).get("action") not in ("join-request", "propose-election", "vote-election",
                                                           "announce")
                and _verify_governance(e.get("payload") or {}) is None):
            # An authority-bearing governance act with no valid owner signature is forged. Rejection
            # is deterministic, so every honest node drops the same ones and journals stay converged.
            # `join-request` and election proposals/votes are authority-LESS device-signed acts (already
            # verified above by _verify_entry); their weight is decided in `_governance_state`.
            # `announce` (kind-discriminated, e.g. kind:key) is likewise authority-less and grants
            # NOTHING — it is deliberately projection-invisible. Its whole value is to EXIST as a
            # device-signed entry carrying `pub`, so _recipient_pubkeys can harvest an identity-bound
            # capsule key for a device that never authored anything (direct owner admit). The security
            # boundary is the pub↔device_id fingerprint binding at harvest, not this ingest gate; the
            # payload of an announce is never trusted. Unknown announce kinds are accepted and ignored,
            # so future kinds ride through this contract's nodes with no version-skew rejection window.
            rejected += 1
            return _DONE
        try:
            _persist(e)
        except ValueError:              # _journal_path_for refused it: never let one entry abort the batch
            rejected += 1
            return _DONE
        existing.add((nid, seq))
        accepted += 1
        _landed(e)
        if action in ("freeze-timestamps", "owner", "transfer", "claim-succession"):
            refresh()                   # arms the bounds, or changes who may sign them: the rest of the batch sees it
        elif action not in ("join-request", "announce"):
            gov_dirty = True            # an admit and the like: read at the next sweep
        return _LANDED

    def judge_content(e):
        """Content, admission-gated once an owner exists. Deferred, not refused, while its device is not yet
        admitted: an admit later in the batch may still land."""
        nonlocal rejected, accepted, duplicates
        nid, seq = e.get("node_id"), e.get("seq")
        refused, why = _unsigned_refused(e, pin, tips, held_hash, door)
        if refused:
            rejected += 1
            unsigned_why.append(why)
            return _DONE
        if (nid, seq) in existing:
            duplicates += 1
            return _DONE
        if not _valid_timestamp(e.get("timestamp")):
            rejected += 1
            return _DONE
        retired = False
        if has_owner and nid in gov["purged"]:
            # A purged device is tombstoned: its new writes are rejected even if later re-admitted, except the ones a
            # retire (#260) keeps: seq <= the signed `keep_through`, whatever order the act and the entry arrive in.
            # Whether such an entry projects is the projection's call (it must be on the chain the head names).
            keep = gov.get("keep", {}).get(nid)
            if keep is None or not isinstance(seq, int) or seq > keep[0]:
                rejected += 1
                return _DONE
            retired = True
        if has_owner and not retired and nid not in admitted:
            # Read-only member: its content does not land until the owner admits the device.
            # Checked before the payload, so a stranger's hostile entry is refused and never quarantined, and
            # before the signature, so an unadmitted device costs this node no Ed25519 verification.
            return _DEFER
        if not _verify_entry(e):
            rejected += 1
            return _DONE
        problem = _hostile_problem(e) or _payload_problem(e)
        if problem is not None:
            rejected += 1               # #205: a payload the projection cannot bind safely never lands
            refused_payload.append((e, problem))
            return _DONE
        try:
            _persist(e)
        except ValueError:              # _journal_path_for refused it: never let one entry abort the batch
            rejected += 1
            return _DONE
        existing.add((nid, seq))
        accepted += 1
        _landed(e)
        if e.get("type") == "idea" and nid != NODE_ID:
            arrived_ideas.append(e)
        return _LANDED

    # Sweep the batch in (device, seq) order. An entry whose device is not admitted yet waits; it does not hold back
    # its device's later entries, because each is judged on its own against the admitted set of what is held,
    # so the batch's order cannot change a verdict. A sweep that lands nothing first lets the waiting governance run
    # (an admit may be what the waiting content needs); only when none is waiting are the deferred entries settled as
    # refused (their device was never admitted). Every sweep lands or refuses at least one entry, so the loop is
    # finite.
    unresolved = [e for e in entries if e.get("node_id") is not None and e.get("seq") is not None]
    while unresolved:
        landed_any, waiting = False, []
        for e in unresolved:
            if gov_dirty:
                refresh()               # a revoke or admit that landed above is read before the next entry is judged (#284)
            r = judge_governance(e) if e.get("type") == "governance" else judge_content(e)
            if r == _DEFER:
                waiting.append(e)
            elif r == _LANDED:
                landed_any = True
        if gov_dirty:
            refresh()
        if not landed_any:
            if any(e.get("type") == "governance" for e in waiting):
                still = []
                for e in waiting:
                    if e.get("type") == "governance":
                        judge_governance(e)          # never deferred: lands or is refused, either way it is done
                    else:
                        still.append(e)
                if gov_dirty:
                    refresh()
                waiting = still
            else:
                rejected += len(waiting)
                break
        unresolved = waiting

    # 1.20: attention, not announcement — a peer's idea landing here is an `introspect`-channel event
    # on the local bus (internal state arriving, not an observation). Non-journaled, best-effort, one
    # line per idea in $HIVE_HOME/.bus/introspect.log for whatever consumer is wired; the session-start
    # digest independently surfaces open ideas from store.db, so no consumer is required.
    if arrived_ideas:
        try:
            bus = HIVE_HOME / ".bus"
            bus.mkdir(exist_ok=True)
            with open(bus / "introspect.log", "a") as f:
                for e in arrived_ideas:
                    f.write(f"{_now_iso()} idea-arrived {e.get('node_id')}:{e.get('seq')} "
                            f"{(e.get('payload') or {}).get('content', '')[:120]!r}\n")
        except Exception:
            pass

    if refused_payload:
        _quarantine(refused_payload, gov)        # node-local evidence for `hv doctor`; never synced
    _record_arrivals([k for k in arrived if k[0] != NODE_ID])
    if rejected:
        reasons = "bad signature, malformed envelope, timestamp or payload, unauthorized governance, or content from an " \
                  "unadmitted device"
        if unsigned_why:
            reasons += "; " + "; ".join(sorted(set(unsigned_why)))
        print(f"append_foreign_entries: rejected {rejected} entr"
              f"{'y' if rejected == 1 else 'ies'} ({reasons})", file=sys.stderr)
    return accepted, duplicates


