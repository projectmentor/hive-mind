# --------------------------------------------------------------------------
# Persistence — derive SQLite state from journal entries.
#
# Two phases (used by rebuild and, inline, by the live commands):
#   pass 1: persist_* insert/upsert rows for fact|decision|entity and record
#           (node_id, seq) -> local_id in journal_index.
#   pass 2: resolve_* wire up cross-row links (entity_fact, decision supersedes)
#           by mapping their (node_id, seq) references through journal_index.
# Splitting the passes means a link can reference a row created later in the
# journal (or on another node) without ordering/FK surprises.
# --------------------------------------------------------------------------

def _record_index(conn, entry, kind, local_id):
    conn.execute(
        "INSERT OR REPLACE INTO journal_index(node_id, seq, kind, local_id, sid) VALUES (?,?,?,?,?)",
        (entry.get("node_id"), entry.get("seq"), kind, local_id, _short_id(entry.get("node_id"), entry.get("seq"))),
    )


def _short_id(node_id, seq):
    """1.19 PR3b: the human-stable id of a journal entry — `h:` + sha256("node_id:seq")[:10]. Derived
    purely from journal identity, so it is identical on every node, forever, and never changes on a
    rebuild (unlike the SQLite rowid it is shown beside). 40 bits: a collision inside one hive's
    lifetime is negligible, and resolution is EXACT (an equality lookup on journal_index.sid, never a
    prefix match). Facts, decisions and ideas only — it is never stored in an entry."""
    if node_id is None or seq is None:
        return None
    return "h:" + hashlib.sha256(f"{node_id}:{seq}".encode()).hexdigest()[:10]


def _claim_write(conn, entry, kind):
    """Take the store's write lock, then return the local id this entry already has (or None). A live writer
    appends to the journal, then projects; between the two another process's catch-up (`rebuild_db`) can read
    the journal and project this very entry. The writer must then neither insert it again nor, for a fact,
    take the content check on a read made before that rebuild committed (#215), so the lock comes first and the
    `(node_id, seq)` guard a rebuild has comes with it. Inside a rebuild's own transaction this is a lookup
    that finds nothing, because the rebuild emptied `journal_index`."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT local_id FROM journal_index WHERE node_id = ? AND seq = ? AND kind = ?",
                       (entry.get("node_id"), entry.get("seq"), kind)).fetchone()
    return row[0] if row else None


def persist_fact(conn, entry):
    if not _projectable(entry):
        return None
    held = _claim_write(conn, entry, "fact")
    if held is not None:                       # a catch-up already projected this entry
        return {"id": held, "inserted": _sid_of(conn, "fact", held) == _short_id(entry["node_id"], entry["seq"])}
    payload = entry["payload"]
    ts = entry.get("timestamp", _now_iso())
    cur = conn.cursor()
    content = payload["content"]
    cur.execute("SELECT id FROM facts WHERE content = ?", (content,))
    row = cur.fetchone()
    if row:
        # Known content: this entry is CORROBORATION, not a new fact. Map its
        # journal identity onto the existing row (so distinct sources stay
        # countable) and bump access_count as telemetry only. Confidence is NOT
        # mutated here — it is a pure projection recomputed by
        # _recompute_confidence() over the set of distinct sources, so repetition
        # can never launder confidence upward.
        cur.execute(
            "UPDATE facts SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
            (ts, row["id"]),
        )
        _record_index(conn, entry, "fact", row["id"])
        return {"id": row["id"], "inserted": False}
    cur.execute("""
        INSERT INTO facts (content, tags, importance, source_agent, source_session,
                           created_at, access_count, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        content,
        json.dumps(_payload_field(entry, "tags")),
        _payload_field(entry, "importance"),
        _payload_field(entry, "source"),
        _payload_field(entry, "source_session"),
        _payload_field(entry, "created_at") or ts,
        _payload_field(entry, "access_count"),
        0.0,                                # confidence placeholder — set by _recompute_confidence()
    ))
    fid = cur.lastrowid
    _record_index(conn, entry, "fact", fid)
    return {"id": fid, "inserted": True}


# --- Confidence projection (Phase A + D0 identity) ----------------------------
# Confidence is a PURE, DETERMINISTIC projection over the immutable journal,
# recomputed on every rebuild — never a mutable counter. It is a function of the
# distinct INDEPENDENT IDENTITIES that asserted the exact content, with diminishing
# returns and a hard cap below "certain". Repetition from one identity is
# idempotent, so it cannot launder confidence upward. The G-Set journal converges
# across nodes, so this yields identical values everywhere.
#
# D0: the corroborating identity is (node_id, app, instance) parsed from the source
# string — NOT the raw source string. Structured agents (e.g. Hermes) vary their
# source per session ("hermes:<ctx>/<agent>/<session8>"); counting the raw string
# would treat one agent's two sessions as two corroborators. Session is excluded;
# each identity is weighted by its context-class (primary/subagent/cron).
# Deferred (D0-v2): same-node independence discount; principal + CAP_self.
CONF_BASE = 0.5    # each new independent identity halves the remaining gap to CAP
CONF_CAP = 0.90    # never "certain" without external/human verification
SRC_CLASS_WEIGHT = {"primary": 1.0, "subagent": 0.5, "cron": 0.3}
# D0-v2 defaults (owner-overridable via journaled `set-config`, so they stay identical on every node).
CONF_SAME_DEVICE_LAMBDA = 0.5   # weight of EACH additional identity on the same device (1.0 = no discount)
CONF_CAP_SELF = 0.70            # ceiling when all corroboration traces to ONE principal (self-reinforcement)
CONF_INTROSPECT_SUPPORT_WEIGHT = 0.0   # 1.19: weight of a supports/contradicts/resolves LINK whose channel is
                                       # `introspect` (reasoning, not observation). 0 = never moves confidence.
# Quorum-election defaults (owner-overridable via journaled `set-config`, identical on every node).
# quorum_m=0 keeps elections OFF, so a hive with no election config/entries resolves to exactly the
# chain owner (back-compat). See Stage C in `_governance_state`.
GOV_QUORUM_M = 0                # admitted voter-units needed to elect a new owner (0 = elections OFF)
GOV_QUORUM_BY = "device"       # count quorum by "device" or by "principal"
GOV_DEAD_MAN_DAYS = 30.0
# 1.19 PR7: trust-velocity windows + advisory threshold (governed so every node computes the same drift).
GOV_TRUST_LONG_DAYS = 180.0     # long window for per-signer reliability
GOV_TRUST_SHORT_DAYS = 14.0     # short window; velocity = short − long (CHANGE is the signal, not the level)
GOV_TRUST_DRIFT_THRESHOLD = -0.3   # `doctor trust-drift` warns below this delta (advisory only)       # owner inactivity (no owner-signed act, incl. heartbeat) before an election can install
GOV_ELECTION_SKEW_DAYS = 1.0 / 24.0   # max a proposal's basis_ts may lead its OWN entry timestamp (clock-skew
                               # allowance); a further-future basis_ts is a forged inactivity anchor → rejected


def _parse_source(source):
    """Parse a source identity into (app, context_class, instance, session).
    Structured: '<app>:<context_class>/<instance>/<session8>'.
    Flat/legacy/CLI (e.g. 'manual', 'claude-code'): app=instance=string,
    context_class='primary', session=None."""
    s = source if isinstance(source, str) and source else "manual"      # #205: a non-string source is `manual`
    if ":" in s:
        app, _, rest = s.partition(":")
        parts = rest.split("/")
        ctx = parts[0] if parts and parts[0] else "primary"
        instance = parts[1] if len(parts) > 1 and parts[1] else (app or "manual")
        session = parts[2] if len(parts) > 2 and parts[2] else None
        return (app or "manual", ctx, instance, session)
    return (s, "primary", s, None)


def _identity_weight(node_id, source):
    """The corroborating-identity key (node_id, app, instance) and its source-class
    weight. Session is deliberately excluded so one agent across sessions = one identity.

    An unrecognised context class weighs 1.0, the same as an absent one (#72, decided). The class is
    a self-declared DISCOUNT: `subagent` and `cron` say "count me for less", and no class can claim
    more than 1.0. So an unknown class claims no discount, which is exactly what an absent class
    claims, and there is nothing to fail open into. Honest writers also use the slot as a context
    name (`claude-code:<project>/…`); failing closed would lower their confidence retroactively."""
    app, ctx, instance, _sess = _parse_source(source)
    return (node_id, app, instance), SRC_CLASS_WEIGHT.get(ctx, 1.0)


_CHANNELS = vocabulary.CHANNEL_NAMES      # ("sense", "act", "introspect"): the registry holds the one list


def _channel(payload):
    """An entry's experience channel, normalised the same way by every reader (#72). Absent or empty
    means `sense` (every pre-1.19 entry). A value outside the vocabulary, such as a typo, a newer
    label or a non-string, counts as `introspect`: an unrecognised label never earns observation
    weight. This matches the outcome rule, which already counted only `sense`."""
    ch = (payload or {}).get("channel") or "sense"
    return ch if isinstance(ch, str) and ch in _CHANNELS else "introspect"


OWNER_APPS = {"owner"}    # governance source-class → decisive forget (FORGET_FLOOR)
FORGET_FLOOR = -1.0       # owner-forget sits below the epistemic range [-CAP, +CAP]


def _confidence_for(net):
    """Signed, saturating projection of NET evidence (n_pos - n_neg, weighted floats):
    CAP*(1 - BASE**|net|) with net's sign. +1->0.45 +2->0.675 0->0 -1->-0.45 -2->-0.675;
    bounded [-CAP, +CAP]."""
    if net == 0:
        return 0.0
    mag = round(CONF_CAP * (1 - CONF_BASE ** abs(net)), 6)
    return mag if net > 0 else -mag


HALFLIFE_DAYS = 180.0     # Phase C: confidence half-life when not refreshed by new evidence (= `halflife_fact` default)
# 1.19 PR6 (design §7): per-CLASS half-lives (governed knobs; HALFLIFE_DAYS stays the `fact` default) and
# the salience-L3 / utility weights. All journaled `set-config` knobs — identical on every node.
HALFLIFE_IDEA_DAYS = 90.0          # `halflife_idea`: a hypothesis nobody tests fades faster than an observation
HALFLIFE_VOLATILE_DAYS = 14.0      # `halflife_volatile`: a `volatile`-tagged fact (transient status)
CONF_IMPORTANCE_SELF_CAP = 0.3     # `importance_self_cap`: the most a writer's own `--importance` can claim
CONF_W_LINKS = 0.6                 # `w_links`: weight of squashed other-identity link in-degree in importance
CONF_W_VOLATILE = 0.1              # `w_volatile`: a volatile fact matters a little more while it is fresh
_HALFLIFE_KNOBS = {"fact": ("halflife_fact", HALFLIFE_DAYS), "idea": ("halflife_idea", HALFLIFE_IDEA_DAYS),
                   "volatile": ("halflife_volatile", HALFLIFE_VOLATILE_DAYS)}
_PR6_KNOB_DEFAULTS = {"halflife_fact": HALFLIFE_DAYS, "halflife_idea": HALFLIFE_IDEA_DAYS,
                      "halflife_volatile": HALFLIFE_VOLATILE_DAYS, "importance_self_cap": CONF_IMPORTANCE_SELF_CAP,
                      "w_links": CONF_W_LINKS, "w_volatile": CONF_W_VOLATILE}


def _halflife(gov, cls):
    """Half-life in days for a salience class (`fact` | `idea` | `volatile`) under this hive's governed
    config; unknown class → fact. Decisions have no half-life of their own — their outcome evidence
    decays under `fact`."""
    key, default = _HALFLIFE_KNOBS.get(cls, _HALFLIFE_KNOBS["fact"])
    try:
        v = float(((gov or _DEFAULT_GOV).get("config") or {}).get(key, default))
    except (TypeError, ValueError):
        v = default
    return v if v > 0 else default


def _fact_class(tags):
    """`volatile` when the fact carries the volatile tag (auto- or hand-applied), else `fact`."""
    try:
        tl = [str(t).strip().lower() for t in (json.loads(tags) if isinstance(tags, str) else (tags or []))]
    except Exception:
        tl = []
    return "volatile" if "volatile" in tl else "fact"


def _squash(x):
    """Monotone saturation for evidence sums: 0 at 0, 0.5 at one full-weight unit, → 1. Same family as
    `_confidence_for` (1 − BASE^x) with BASE = ½ and no cap: `1 − 2^(−x)`."""
    return 0.0 if x <= 0 else 1.0 - 2.0 ** (-x)


def _projection_latest(entries, want_type, authz=None):
    """Pure, deterministic projection over content entries of `want_type` → {name: latest payload}.
    Mirrors `_governance_state`'s discipline: read only the journal (which converges via the G-Set),
    skip entries that fail their device signature, and break ties identically on every node, so all
    nodes derive the same view. "Latest" = highest `version`, then (timestamp, node_id, seq) — a total
    order, deterministic across nodes. Admission is already enforced at ingest (`append_foreign_entries`
    Pass 2 rejects content from non-admitted devices), so the projection just folds what landed.

    `authz`, when given, is a pure callable `(signer_node_id, (ts, node_id, seq), payload) -> bool`:
    an entry whose signer is not authorized to WRITE this content type is skipped from the fold
    exactly like a bad-signature entry. The entry still LANDS in every journal (convergence/Merkle
    untouched) — every node just deterministically declines to honor it. The caller attaches `authz`
    only once an owner exists and crypto is available, so an unsigned content entry (no verifiable
    signer) is declined post-owner; pre-owner and crypto-less (stripped) folds stay permissive
    (`authz=None`). See `_is_authorized_writer` and hive decisions #58/#61."""
    rows = []
    entries = _ts_unskipped(entries)        # #217: an entry the timestamp bounds skip is not in the fold
    for e in entries:
        if e.get("type") != want_type:
            continue
        if "sig" in e and not _verify_entry(e):
            continue
        p = e.get("payload", {}) or {}
        name = p.get("name")
        if not name:
            continue
        if authz is not None:
            # The device signer is trustworthy only on a SIGNED entry (`_verify_entry` above bound its
            # node_id to the embedded pub); an unsigned entry has no verifiable device signer. The
            # predicate decides per policy (fertile → device admission; owner → the payload's owner
            # signature), so we hand it both and let it decline.
            signer = e.get("node_id") if "sig" in e else None
            pos = (e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0))
            if not authz(signer, pos, p):
                continue
        rows.append((int(p.get("version", 0) or 0), e.get("timestamp", ""),
                     str(e.get("node_id", "")), e.get("seq", 0), name, p))
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))   # earliest→latest; last write per name wins
    out = {}
    for _v, _ts, _nid, _seq, name, p in rows:
        out[name] = p
    return out


def _content_authz(gov, policy_key):
    """The projection authorization callable for an authority-bearing content type governed by the
    `policy_key` config knob (`capsule_putters` for capsules, `cell_writers` for cells/combs), or None
    when authz is inactive — pre-owner, or a crypto-less (stripped) build, where the fold stays
    permissive (hive #61). Default policy is `owner`."""
    if not gov.get("owner_id") or _ed25519 is None:
        return None
    policy = gov.get("config", {}).get(policy_key, "owner")
    return lambda signer, pos, p: _is_authorized_writer(signer, pos, p, gov, policy)


def _cell_state(entries, gov=None):
    """name → latest `cell` payload. A cell is an executable unit with a `kind` (`tool`|`agent`) — it
    DEFINES what an agent or tool runs, so a malicious redefinition is as dangerous as code injection.
    Writes are authorized at the projection like capsules, but under the SEPARATE `cell_writers` policy
    (default owner — hive decision #60), so a hive can keep cells owner-only while running capsules
    fertile (or vice versa). Pass `gov` to reuse an already-computed governance projection."""
    if gov is None:
        gov = _governance_state(entries)
    return _projection_latest(entries, "cell", authz=_content_authz(gov, "cell_writers"))


def _comb_state(entries, gov=None):
    """name → latest `comb` payload. A comb is a named, ordered collection of cell names. Authorized
    under the same `cell_writers` policy as cells (hive #60). Pass `gov` to reuse it."""
    if gov is None:
        gov = _governance_state(entries)
    return _projection_latest(entries, "comb", authz=_content_authz(gov, "cell_writers"))


def _capsule_state(entries, gov=None):
    """name → latest `capsule` payload (an encrypt-to-device sealed secret). Latest version wins, so
    a rotation supersedes an old seal and a tombstone (alg='tombstone-v1') supersedes the secret.

    Honors only entries whose signer was the AUTHORIZED writer under the hive's `capsule_putters`
    policy — judged POINT-IN-TIME for the owner policy (hive decision #58), so a compromised admitted
    device can no longer supersede or tombstone a capsule by appending a journal entry that bypasses
    the CLI-only `capsule_putters` gate. Pre-owner, or a crypto-less (stripped) build, folds
    permissively (decision #61). Pass `gov` to reuse an already-computed governance projection."""
    if gov is None:
        gov = _governance_state(entries)
    return _projection_latest(entries, "capsule", authz=_content_authz(gov, "capsule_putters"))


def _owner_at(owner_timeline, pos):
    """The owner_id legitimately in authority AS OF journal position `pos` (a (ts, node_id, seq)
    tuple) per the settled owner-succession timeline, or None if `pos` precedes the genesis owner.
    Pure/deterministic: the timeline and `pos` share the journal's single total order, so every node
    resolves the same owner-at-T with no wall-clock."""
    owner = None
    for tts, tnid, tseq, oid in owner_timeline:        # ascending by position
        if (tts, tnid, tseq) <= pos:
            owner = oid
        else:
            break
    return owner


def _member_at(gov, nid, pos):
    """True when device `nid` was a member AT journal position `pos`: the genesis device (always), or admitted by an
    honoured owner act at or before `pos` and not revoked or purged by one at or before it. SECREV A3: an owner
    signature proves the KEY signed, not WHEN; the carrying device being a member at that position is what ties the act
    to the owner's term, since a former owner's key on a device nobody admitted could stamp any position it likes."""
    if nid == gov.get("genesis_device"):
        return True
    for a, b in (gov.get("member_spans") or {}).get(str(nid), ()):
        if a <= pos and (b is None or pos <= b):   # the revoke or purge itself is still carried by the device it ends
            return True
    return False


def _owner_signed_at(oid, pos, gov):
    """`oid` (the verified owner signature on an act) is the owner in authority at `pos`, and the device that carries the
    act (`pos[1]`) was a member there. The one test the owner-signed content paths (forget, link, capsule, cell) share
    with the governance walk."""
    return oid is not None and oid == _owner_at(gov.get("owner_timeline") or [], pos) and _member_at(gov, pos[1], pos)


def _is_authorized_writer(signer, pos, payload, gov, policy):
    """True if this content entry — device-signed by `signer`, carrying `payload`, at journal position
    `pos` — may write an authority-bearing content type under `policy` ('owner'|'fertile').

    OWNER policy: the writer must PROVE owner-key possession via an owner signature on the payload
    (verified by `_verify_governance`, exactly as a governance act proves authority — a capsule is
    device-signed, so the device node_id alone cannot establish owner authorship), AND that owner must
    have been the legitimate owner AS OF `pos`. POINT-IN-TIME (hive #58): a prior owner's seals survive
    a later transfer/succession/election; a position before the genesis owner resolves to None and is
    declined (closing a backdate-before-genesis bypass).

    FERTILE policy: any currently admitted, non-purged device — authority is the device's own
    signature + hive membership (the asymmetric tombstone/seal refinement is deferred until fertile is
    adopted, hive #59). Caller invokes this only once an owner exists and crypto is available."""
    if policy == "fertile":
        return signer is not None and signer in gov.get("admitted", set()) and signer not in gov.get("purged", set())
    return _owner_signed_at(_verify_governance(payload), pos, gov)


def _capsule_version_conflicts(entries):
    """Capsule names where the SAME version was sealed by MORE THAN ONE device — a concurrent
    put/rotate that happened before the writers synced. The projection still resolves deterministically
    by (version, timestamp, node_id, seq), so the hive stays converged, but it means one writer's value
    silently lost the tie. Returns sorted [name] so `hv doctor` can surface it (version is a local
    increment, not owner-signed — this is the visibility the design otherwise lacks)."""
    seen = {}      # (name, version) -> set(node_id)
    for e in entries:
        if e.get("type") != "capsule":
            continue
        p = e.get("payload", {}) or {}
        name, ver = p.get("name"), p.get("version")
        if name is None or ver is None:
            continue
        seen.setdefault((name, ver), set()).add(e.get("node_id"))
    return sorted({name for (name, _ver), nodes in seen.items() if len(nodes) > 1})


def _content_unauthorized(entries, gov, want_type, policy_key):
    """Entries of `want_type` the projection now DECLINES to honor because their signer was not the
    authorized writer (the owner AS OF the write position, or an admitted device under `fertile`) under
    the `policy_key` policy — surfaced by `hv doctor` so an operator sees what an upgrade changes rather
    than silently dropping depended-on state (hive decisions #58/#60/#61). Returns ['name vN by
    node_id', ...]; empty when authz is inactive (pre-owner or a crypto-less build)."""
    if not gov.get("owner_id") or _ed25519 is None:
        return []
    policy = gov.get("config", {}).get(policy_key, "owner")
    out = []
    for e in entries:
        if e.get("type") != want_type:
            continue
        if "sig" in e and not _verify_entry(e):
            continue
        p = e.get("payload", {}) or {}
        if not p.get("name"):
            continue
        signer = e.get("node_id") if "sig" in e else None
        pos = (e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0))
        if not _is_authorized_writer(signer, pos, p, gov, policy):
            out.append(f"{p['name']} v{p.get('version', '?')} by {e.get('node_id', '?')}")
    return out


_GOV_CACHE = {}     # signature(governance entries) -> projected gov dict (master copy, never handed out)


def _genesis_pin_for(entry):
    """The pin a given `owner` act would produce: which hive, which owner, and WHICH ENTRY. The hash
    binds the whole entry, so a rival that copies the hive_id and the owner_id still fails to match."""
    p = entry.get("payload", {}) or {}
    return {"hive_id": p.get("hive_id", ""), "owner_id": p.get("owner_id"),
            "genesis_ref": f"{entry.get('node_id')}:{entry.get('seq')}",
            "genesis_hash": compute_hash(entry)}


def _load_genesis_pin():
    """This node's pin, or None when it has never pinned (legacy). A malformed or half-written file is
    NOT a pin: an unreadable pin must fail to the documented legacy behaviour, never to "any owner"."""
    try:
        pin = json.loads(GENESIS_PIN_PATH.read_text())
    except Exception:
        return None
    if not isinstance(pin, dict) or not pin.get("owner_id"):
        return None
    return pin if (pin.get("genesis_hash") or pin.get("genesis_hash8")) else None


def _write_genesis_pin(pin):
    """Write the pin 0600 and atomically — a torn pin would silently unpin the node on the next read."""
    GENESIS_PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not GENESIS_PIN_PATH.exists():
        GENESIS_PIN_PATH.touch(mode=0o600)
    _atomic_write_text(GENESIS_PIN_PATH, json.dumps(pin, sort_keys=True) + "\n")
    os.chmod(GENESIS_PIN_PATH, 0o600)
    return pin


def _pin_matches(entry, pin):
    """True when this `owner` act IS the pinned genesis. A pin taken from an invite fingerprint carries
    only a hash prefix (`genesis_hash8`), so both forms are honoured; the full hash wins when present.

    The declared owner (and the hive, when the pin names one) is compared FIRST, and that is what makes
    the short prefix safe. A prefix alone is grindable: an attacker varies `seq` and `timestamp` — fields
    the owner signature does not cover — until their own self-signed declaration's hash shares those few
    hex characters, which is ~2^32 sha256 evaluations for eight of them. Binding the owner_id means a
    rival would need the victim's OWNER KEY to be a candidate at all, because a genesis candidate must
    be self-signed by the owner it declares."""
    if not pin:
        return False
    pl = entry.get("payload") or {}
    if pl.get("owner_id") != pin.get("owner_id"):
        return False
    if pin.get("hive_id") and pl.get("hive_id") != pin.get("hive_id"):
        return False
    h = compute_hash(entry)
    if pin.get("genesis_hash"):
        return h == pin["genesis_hash"]
    short = str(pin.get("genesis_hash8") or "")
    return bool(short) and h.split(":", 1)[-1].startswith(short)


def _self_signed_owner_acts(entries):
    """Every genesis CANDIDATE: a governance `owner` act whose payload is signed by the very owner it
    declares. Sorted the way the projection sorts governance, so the legacy pick — candidate 0 — is
    byte-identical to the pre-pin behaviour on every node."""
    out = [e for e in entries
           if e.get("type") == "governance"
           and (e.get("payload") or {}).get("action") == "owner"
           and _verify_governance(e.get("payload") or {}) == (e.get("payload") or {}).get("owner_id")]
    out.sort(key=lambda e: (e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0)))
    return out


def _auto_pin_genesis(entries):
    """Pin on upgrade, but only when there is nothing to guess: EXACTLY ONE candidate. Two means a
    rival is already stored (or `owner init` ran twice), and the earlier timestamp is precisely what an
    attacker controls — so pin nothing, let `hv doctor genesis` fail, and leave the choice to the
    operator (re-join with the winner's fingerprint). Returns the pin, or None."""
    existing = _load_genesis_pin()
    cands = _self_signed_owner_acts(entries)
    if existing is not None:
        # A pin taken from an invite fingerprint holds only a hash prefix. Once the declaration itself
        # has synced, complete the pin so it names the exact entry (and doctor can say so).
        if not existing.get("genesis_hash"):
            match = [e for e in cands if _pin_matches(e, existing)]
            if len(match) == 1:
                return _write_genesis_pin(_genesis_pin_for(match[0]))
        return existing
    return _write_genesis_pin(_genesis_pin_for(cands[0])) if len(cands) == 1 else None


def _restore_lost_genesis_pin():
    """SN-14: a device that is already a member of the hive its own journal describes, but holds no pin, lost
    the pin file (deleted, torn, restored from a backup without it). Without one, ingest falls back to the
    legacy earliest-timestamp rule and a rival `owner` act can be stored. Re-pin from the LOCAL journal, with
    no network trust: only when there is exactly ONE self-signed declaration in it and this device is that
    declaration's genesis device or an admitted member under it. A fresh joiner that pulled a genesis by trust
    on first use is neither (the owner has not admitted it yet), so its pin stays a deliberate act. The pin
    is marked `restored_at` so `hv doctor genesis` keeps saying it was restored. Returns the pin, or None."""
    try:
        entries = merkle.read_all_entries(JOURNAL_DIR)
        cands = _self_signed_owner_acts(entries)
        if len(cands) != 1:
            return None
        gov = _governance_state(entries)
        if NODE_ID != gov.get("genesis_device") and NODE_ID not in (gov.get("admitted") or ()):
            return None
        pin = _genesis_pin_for(cands[0])
        pin["restored_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"hv: the genesis pin was missing; re-established from this journal's genesis {pin['genesis_ref']} "
              "(see `hv doctor genesis`).", file=sys.stderr)
        return _write_genesis_pin(pin)
    except Exception:
        return None


def _genesis_fingerprint(entries, gov=None):
    """This hive's genesis, in a form a human can compare across devices and an invite can carry:
    `<hive_id>/<owner_id>/<hash8>`. The hash prefix is what makes it a FINGERPRINT rather than a
    claim — a squatter can copy the hive_id and the owner_id, but not the genesis entry's hash. Empty
    when no genesis is established on this node."""
    gov = _governance_state(entries) if gov is None else gov
    ref = (gov.get("genesis") or {}).get("ref")
    if not ref:
        return ""
    for e in _self_signed_owner_acts(entries):
        if f"{e.get('node_id')}:{e.get('seq')}" == ref:
            return f"{gov.get('hive_id', '')}/{gov.get('owner_id', '')}/{compute_hash(e).split(':', 1)[-1][:8]}"
    return ""


def _succession_inversions(entries):
    """[(earlier_ref, later_ref)] for succession acts (`transfer`, `claim-succession`, owner-signed) where the act that
    sorts EARLIER in the journal's order reached this node LATER than one that sorts after it: the signature of a
    backdated act. An entry this device wrote itself has no arrival record and counts as arrived first. Local evidence,
    like `future-dated`: it reads this node's arrival record and changes no projection."""
    arrived = _read_arrivals()
    acts = []
    for e in entries:
        p = e.get("payload")
        if (e.get("type") == "governance" and isinstance(p, dict) and p.get("action") in ("transfer", "claim-succession")
                and _verify_governance(p) is not None):
            at = arrived.get((e.get("node_id"), e.get("seq")))
            at = _ts_instant(at) if at is not None else None
            if at is None and e.get("node_id") != NODE_ID:
                continue                        # arrival unknown: no evidence either way
            acts.append(((e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0)), at))
    acts.sort(key=lambda a: a[0])
    out = []
    for i, (pos_a, at_a) in enumerate(acts):
        for pos_b, at_b in acts[i + 1:]:
            if at_a is not None and (at_b is None or at_a > at_b):
                out.append((f"{pos_a[1]}:{pos_a[2]}", f"{pos_b[1]}:{pos_b[2]}"))
    return out


def _governance_state(entries):
    """Memoized wrapper over the (pure, deterministic) governance projection. The projection reads
    ONLY governance entries (but for `kept`, derived per call below), so the cache key is a content signature of just those — a plain `fact`
    write (the common case) does not bust it, and the heavy work (sort + multi-stage election walk +
    per-entry Ed25519 verification) is skipped on the long-lived daemon's repeated calls. The cached
    value is the master; every caller gets a deep COPY so a caller mutating its result can never
    corrupt the cache. Output is byte-identical to the uncached projection for the same journal."""
    govs_raw = [e for e in entries if e.get("type") == "governance"]
    parts = ([json.dumps(_load_genesis_pin(), sort_keys=True, separators=(",", ":"))] +   # the pin decides
             [json.dumps([e.get("node_id"), e.get("seq"), e.get("timestamp"),             # genesis (#14)
                          e.get("sig"), e.get("payload")], sort_keys=True, separators=(",", ":"))
              for e in govs_raw])
    if any((e.get("payload") or {}).get("action") == "freeze-timestamps" for e in govs_raw if isinstance(e.get("payload"), dict)):
        # #217: once a marker exists a governance entry's verdict depends on its device's other timestamps
        parts += [f"{e.get('node_id')}:{e.get('seq')}:{e.get('timestamp')}" for e in entries if e.get("type") != "governance"]
    sig = hashlib.sha256("\x01".join(parts).encode()).hexdigest()
    master = _GOV_CACHE.get(sig)
    if master is None:
        master = _governance_state_bounded(entries)
        _GOV_CACHE.clear()              # journal grows monotonically; only the latest sig is ever reused
        _GOV_CACHE[sig] = master
    gov = copy.deepcopy(master)
    # #260: what the latest honoured retire of each device keeps. It reads the device's own content entries, which the
    # cache key above leaves out, so it is derived per call (from the signed pair the cached walk resolved), never cached.
    gov["kept"] = _purge_kept(entries, gov["keep"])
    return gov


def _governance_state_bounded(entries):
    """The governance projection with the timestamp bounds (#217) applied to governance entries: a governance act
    the bounds skip (a backdated admit, say) is read as absent, so every node resolves the same owner and admits."""
    gov = _governance_state_uncached(entries)
    if not gov["ts_tips"]:
        return gov
    kind = {(e.get("node_id"), e.get("seq")): e.get("type") for e in entries}
    skipped = {k for k in _ts_violations(entries, gov) if kind.get(k) == "governance"}
    if not skipped:
        return gov
    return _governance_state_uncached([e for e in entries if (e.get("node_id"), e.get("seq")) not in skipped])


def _governance_state_uncached(entries):
    """Pure, deterministic projection over owner-signed `governance` entries → the established owner,
    the admitted device set, the principal map, and tunable config. Because it reads only the journal
    (which converges via the G-Set), every node derives the same governance state, so confidence stays
    identical everywhere. No owner declared yet → empty admitted set + default config (gate off)."""
    gov = {"owner_id": None, "owner_pub": None, "owner_ts": "", "hive_id": "", "admitted": set(),
           "denied": set(), "purged": set(), "standbys": set(),
           "nominations": set(), "escrows": [], "elections": [], "owner_term": 0,
           "principals": {}, "modules": {}, "module_config": {},
           "ts_tips": {}, "ts_markers": set(), "first_admit": {}, "revoked": set(),
           "keep": {}, "kept": set(), "uncarried": [], "member_spans": {}, "ended_at": {},
           "config": {"same_device_lambda": CONF_SAME_DEVICE_LAMBDA, "cap_self": CONF_CAP_SELF,
                                        "quorum_m": GOV_QUORUM_M, "quorum_by": GOV_QUORUM_BY,
                                        "dead_man_days": GOV_DEAD_MAN_DAYS,
                                        "introspect_support_weight": CONF_INTROSPECT_SUPPORT_WEIGHT,
                                        "trust_long_days": GOV_TRUST_LONG_DAYS, "trust_short_days": GOV_TRUST_SHORT_DAYS,
                                        "trust_drift_threshold": GOV_TRUST_DRIFT_THRESHOLD,
                                        "flood_per_minute": FLOOD_PER_MINUTE, "flood_per_day": FLOOD_PER_DAY,
                                        **_PR6_KNOB_DEFAULTS}}
    govs = []
    for e in entries:
        if e.get("type") != "governance":
            continue
        p = e.get("payload")
        if not isinstance(p, dict):
            continue   # a null or non-object payload (a line already on disk) carries no act
        action = p.get("action")
        if action in ("propose-election", "vote-election"):
            # DEVICE-signed (not owner-signed): authority is the signer's hive membership, not the
            # owner key. Require a real device signature (these acts are new in 1.9 — nothing legacy
            # to grandfather); admission is enforced later in the walk. `signer=None` marks them.
            if "sig" in e and _verify_entry(e):
                govs.append((e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0), p, None))
            continue
        signer = _verify_governance(p)
        if signer is None:
            continue   # unsigned or forged governance is ignored
        govs.append((e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0), p, signer))
    govs.sort(key=lambda x: (x[0], x[1], x[2]))   # deterministic order across nodes

    # 1) Establish the owner. The PINNED genesis wins (hive-mind-private #14): the pin decides, not
    #    the timestamp, so a rival `owner` act cannot take the hive — including one already on disk,
    #    which an append-only journal never removes. With NO pin this node keeps the legacy rule (the
    #    earliest valid self-signed declaration, TOFU), so it still converges with an un-upgraded
    #    fleet; `hv doctor genesis` is what tells the operator to pin. A pin whose genesis entry has
    #    not synced yet leaves the hive owner-less HERE and sets `mismatch` — ingest fails CLOSED on
    #    that state rather than falling back to pre-owner permissiveness.
    pin = _load_genesis_pin()
    cands = _self_signed_owner_acts(entries)
    chosen = (next((e for e in cands if _pin_matches(e, pin)), None) if pin
              else (cands[0] if cands else None))
    gov["genesis"] = {"pinned": pin is not None, "candidates": len(cands), "ref": None,
                      "mismatch": bool(pin) and chosen is None}
    if chosen is None:
        return gov
    cp = chosen.get("payload") or {}
    gov["owner_id"], gov["owner_pub"] = cp.get("owner_id"), cp.get("owner_pub")
    gov["owner_ts"] = chosen.get("timestamp", "")
    gov["hive_id"] = cp.get("hive_id", "")
    gen_pos = (gov["owner_ts"], str(chosen.get("node_id", "")), chosen.get("seq", 0))
    gov["genesis"]["ref"] = f"{chosen.get('node_id')}:{chosen.get('seq')}"
    gov["genesis_device"] = chosen.get("node_id")   # a member from genesis (`owner init` writes no admit); see `_admitted_content`

    # 2) Single deterministic walk: the OWNER SUCCESSION CHAIN (Stage B) and the QUORUM ELECTION
    #    OVERRIDE (Stage C) interleaved with the membership/config/escrow acts. `current` is the owner
    #    AT THIS POINT in the log — succession advances it, and every other act is honored only when
    #    signed by the current owner. So a handed-off owner's later acts drop, and a live owner is
    #    never unseated (only their own signed nominate/transfer, a nominee's claim against an OPEN
    #    nomination, OR — when the owner has gone dark past dead_man_days — a quorum election advances
    #    it). Because the election installs its winner mid-walk like a transfer, the new owner's later
    #    acts are honored from that point and any heartbeat by a live owner keeps the dead-man shut.
    current = gov["owner_id"]
    owner_pubs = {current: gov["owner_pub"]}
    owner_timeline = [(gen_pos[0], gen_pos[1], gen_pos[2], current)]   # [(ts,node_id,seq,owner_id)] —
    #   each entry marks the journal position from which `owner_id` holds authority; consumed by
    #   `_owner_at` for POINT-IN-TIME content authorization (hive #58). Appended on every succession.
    nominations = set()                       # open successor pubs (canonical b64)
    escrows = []                              # [{ref:(nid,seq), ts, nid, seq, env, revoked}]
    owner_act = {current: gov["owner_ts"]}    # owner_id -> latest ts of an owner-signed act (liveness)
    proposals = {}                            # Stage C: election_id -> {new_pub, new_oid, basis_ts, voters, installed}
    pending_votes = {}                        # election_id -> {voter_units} seen BEFORE the proposal (clock skew)
    def carried(nid):
        """SECREV A3: the device carrying an owner-signed act is a member AT THIS POINT in the walk: admitted, or the
        genesis device. The owner signature alone proves the KEY signed; a former owner's key on a device nobody
        admitted proves nothing about when it signed."""
        return nid in gov["admitted"] or nid == gov["genesis_device"]

    def span_open(nid, pos):
        sp = gov["member_spans"].setdefault(str(nid), [])
        if not sp or sp[-1][1] is not None:
            sp.append([pos, None])

    def span_close(nid, pos):
        sp = gov["member_spans"].get(str(nid))
        if sp and sp[-1][1] is None:
            sp[-1][1] = pos

    for _ts, _nid, _seq, p, signer in govs:
        action = p.get("action")

        # --- Stage C: device-signed quorum election (signer is None — authority is membership) ---
        if signer is None and action in ("propose-election", "vote-election"):
            quorum_m = int(gov["config"].get("quorum_m", 0) or 0)
            if quorum_m <= 0 or _nid not in gov["admitted"] or _nid in gov["purged"]:
                continue                       # elections off, or a non-admitted/purged signer
            if _nid in gov["modules"]:
                continue                       # 2.1: a module device neither proposes nor votes (the owner's `admit` says so)
            pid = p.get("proposal_id")
            if not pid:
                continue
            by_principal = gov["config"].get("quorum_by", GOV_QUORUM_BY) == "principal"
            vunit = ("p", gov["principals"].get(_nid, _nid)) if by_principal else ("d", _nid)
            if action == "propose-election":
                new_pub = _canon_pub(p.get("new_owner_pub", ""))
                basis_ts = p.get("basis_ts", "")
                if not new_pub or not basis_ts or pid != _election_id(new_pub, basis_ts):
                    continue                   # malformed or not content-addressed to its body
                # Deterministic clock-skew bound: basis_ts anchors the owner-inactivity measurement,
                # so a FUTURE-dated basis_ts pre-arms the dead-man switch. It cannot legitimately lead
                # the proposal's OWN entry timestamp (_ts) — both are stamped at propose time — so
                # reject one that does, beyond a small skew allowance. Entry-time vs entry-time only:
                # every node decides identically and the journal stays convergent (no wall-clock).
                if _days_between(_ts, basis_ts) > GOV_ELECTION_SKEW_DAYS:
                    continue
                pr = proposals.get(pid)
                if pr is None:
                    try:
                        new_oid = _owner_id_for_pub(base64.b64decode(new_pub))
                    except Exception:
                        continue
                    pr = proposals[pid] = {"new_pub": new_pub, "new_oid": new_oid,
                                           "basis_ts": basis_ts,
                                           "voters": pending_votes.pop(pid, set()), "installed": False}
            else:                              # vote-election
                pr = proposals.get(pid)
                if pr is None:                 # a vote that sorts before its proposal — hold it
                    pending_votes.setdefault(pid, set()).add(vunit)
                    continue
            pr["voters"].add(vunit)
            # Install the elected owner the instant a proposal reaches quorum AND the dead-man is
            # armed for the owner current HERE — like a transfer at this log position. The earliest
            # such crossing wins: once it installs, `current` becomes the new owner with fresh
            # activity (owner_act below), so a racing election can no longer arm against it.
            if not pr["installed"] and len(pr["voters"]) >= quorum_m:
                la = owner_act.get(current, "")
                dead_man_days = float(gov["config"].get("dead_man_days", GOV_DEAD_MAN_DAYS) or 0)
                if la and pr["basis_ts"] > la and _days_between(la, pr["basis_ts"]) >= dead_man_days:
                    pr["installed"] = True
                    current = pr["new_oid"]
                    owner_pubs[current] = pr["new_pub"]
                    owner_act[current] = _ts
                    owner_timeline.append((_ts, str(_nid), _seq, current))
                    nominations.clear()
                    gov["owner_term"] += 1
            continue

        if not carried(str(_nid)):
            gov["uncarried"].append((str(_nid), _seq, action))
            continue                          # SECREV A3: owner-signed, but carried by a device that was not a member here

        owner_act[signer] = max(owner_act.get(signer, ""), _ts)   # any owner-signed act = liveness

        # --- Stage B: advance the owner chain (evaluated BEFORE the current-owner gate) ---
        if action == "nominate-successor":
            cp = _canon_pub(p.get("successor_owner_pub", "")) if signer == current else None
            if cp:
                nominations.add(cp)
            continue
        if action == "revoke-nomination":
            if signer == current:
                cp = _canon_pub(p.get("successor_owner_pub", ""))
                nominations.discard(cp)
            continue
        if action == "claim-succession":
            # Self-signed by the successor's NEW owner key (like genesis): the embedded owner_pub is
            # what signed it. Valid only against an open matching nomination AND when carried by a member (SECREV A3):
            # the successor's key signs the payload, not the device, so a keyless device could otherwise re-carry it
            # at an earlier stamp and re-route the term start. The claim does NOT admit its carrier, so the successor's
            # device must already be admitted (by the old owner, after the nomination) or be the genesis device.
            cp = _canon_pub(p.get("owner_pub", ""))
            if signer == p.get("owner_id") and cp and cp in nominations:
                current = p["owner_id"]
                owner_pubs[current] = p.get("owner_pub")
                owner_timeline.append((_ts, str(_nid), _seq, current))
                nominations.clear()           # nominations close on succession
                gov["owner_term"] += 1
            continue
        if action == "transfer":
            if signer == current and p.get("new_owner_pub"):
                try:
                    new_oid = _owner_id_for_pub(base64.b64decode(p["new_owner_pub"]))
                except Exception:
                    continue
                current = new_oid
                owner_pubs[new_oid] = p["new_owner_pub"]
                owner_act[new_oid] = _ts          # the handoff is the successor's term-start activity
                owner_timeline.append((_ts, str(_nid), _seq, current))
                nominations.clear()
                gov["owner_term"] += 1
            continue

        # Everything below is authorized only by the owner CURRENT at this log position.
        if signer != current:
            continue
        if action == "owner-escrow" and p.get("envelope"):
            escrows.append({"ref": (str(_nid), _seq), "ts": _ts, "nid": str(_nid), "seq": _seq,
                            "env": p["envelope"], "revoked": False})
        elif action == "revoke-escrow":
            target = p.get("escrow_ref")
            for esc in escrows:                # tombstone matching escrows seen SO FAR (positional)
                if (target == "all"
                        or (isinstance(target, (list, tuple)) and tuple(target) == esc["ref"])
                        or target == f"{esc['nid']}:{esc['seq']}"):
                    esc["revoked"] = True
        elif action == "admit" and p.get("device_id"):
            # A later admit re-admits a revoked device and clears a prior deny. Purge is a final
            # tombstone — admit does NOT clear it (a purged device cannot be resurrected here).
            gov["admitted"].add(p["device_id"])
            span_open(p["device_id"], (_ts, str(_nid), _seq))
            gov["first_admit"].setdefault(p["device_id"], _ts)   # #217 rule 2's floor: a re-admit never moves it
            gov["denied"].discard(p["device_id"])
            if p["device_id"] not in gov["purged"]:
                gov["ended_at"].pop(p["device_id"], None)
            if p.get("principal"):
                gov["principals"][p["device_id"]] = p["principal"]
            # 2.1: an owner `admit` carrying `module: <name>` marks a module's device; a later admit without
            # it clears the mark. A 2.0 node ignores the field, so it still counts that device's vote (§7).
            if vocabulary.valid_module_name(p.get("module")):
                gov["modules"][p["device_id"]] = p["module"]
            else:
                gov["modules"].pop(p["device_id"], None)
        elif action == "revoke" and p.get("device_id"):
            gov["admitted"].discard(p["device_id"])   # reversible: a later admit restores it
            span_close(p["device_id"], (_ts, str(_nid), _seq))
            gov["revoked"].add(p["device_id"])        # honoured: ends the pre-genesis exemption (#230)
            gov["ended_at"].setdefault(p["device_id"], (_ts, str(_nid), _seq))   # where this ending began (SECREV B6)
        elif action == "deny" and p.get("device_id"):
            gov["denied"].add(p["device_id"])          # drop a pending join-request (admit overrides)
        elif action == "change" and p.get("device_id") and p.get("principal"):
            if p["device_id"] in gov["admitted"]:      # re-tag principal without changing admission
                gov["principals"][p["device_id"]] = p["principal"]
        elif action == "purge" and p.get("device_id"):
            gov["admitted"].discard(p["device_id"])    # final tombstone (see _governed_side / ingest)
            span_close(p["device_id"], (_ts, str(_nid), _seq))
            gov["purged"].add(p["device_id"])
            gov["ended_at"].setdefault(p["device_id"], (_ts, str(_nid), _seq))
            # #260: the latest honoured purge decides. A well-formed keep pair makes it a retire (the named prefix
            # still counts); a plain one, or a malformed pair, drops the device's content whole.
            pair = _keep_pair(p)
            if pair is not None:
                gov["keep"][p["device_id"]] = pair
            else:
                gov["keep"].pop(p["device_id"], None)
        elif action == "standby" and p.get("device_id"):
            # Advisory: a device the owner sanctions to also hold the owner key and act while the
            # owner is offline. No authority is granted here (the key is the authority) — this just
            # records intent so `hv whoami`/`owner show`/`doctor` can surface who may act.
            if p.get("enabled", True):
                gov["standbys"].add(p["device_id"])
            else:
                gov["standbys"].discard(p["device_id"])
        elif action == "freeze-timestamps":
            # #217: the owner-signed marker that arms the timestamp bounds. `tips` freezes each device's chain at
            # a seq; an entry at or below it is unchecked. The per-device maximum across markers, so a marker
            # only ever grandfathers more by seq, never by timestamp.
            gov["ts_markers"].add((_nid, _seq))
            tips = p.get("tips")
            if isinstance(tips, dict):
                for tnid, tseq in tips.items():
                    if isinstance(tnid, str) and isinstance(tseq, int) and not isinstance(tseq, bool) and tseq >= 0:
                        gov["ts_tips"][tnid] = max(gov["ts_tips"].get(tnid, 0), tseq)
        elif action == "set-config":
            key, raw = p.get("key"), p.get("value")
            try:
                if key in ("flood_per_minute", "flood_per_day"):
                    if int(raw) >= 1:
                        gov["config"][key] = int(raw)
                elif key in ("same_device_lambda", "cap_self", "dead_man_days", "introspect_support_weight",
                           "trust_long_days", "trust_short_days", "trust_drift_threshold") \
                        or key in _PR6_KNOB_DEFAULTS:
                    gov["config"][key] = float(raw)
                elif key == "quorum_m":
                    gov["config"][key] = int(raw)
                elif key == "quorum_by" and raw in ("device", "principal"):
                    gov["config"][key] = raw
                elif key in ("capsule_putters", "cell_writers") and raw in ("owner", "fertile"):
                    gov["config"][key] = raw
                elif key == "forget_writers" and raw in ("legacy", "owner"):   # #122 step 2; else ignored
                    gov["config"][key] = raw
                elif vocabulary.split_module_name(key) and isinstance(raw, str) \
                        and len(raw) <= vocabulary.MODULE_VALUE_MAX:
                    gov["module_config"][key] = raw   # 2.1 M3: `x-<module>:<key>`, a string core never interprets
            except (TypeError, ValueError):
                pass

    # The owner the chain resolved to (term-0 owner if no succession/election happened). owner_ts/
    # hive_id stay the term-0 values — they mark when governance BEGAN (grandfathers pre-owner forgets).
    gov["owner_id"], gov["owner_pub"] = current, owner_pubs.get(current)
    gov["owner_timeline"] = sorted(owner_timeline)    # ascending by position for `_owner_at`
    gov["member_spans"] = {n: [(tuple(a), tuple(b) if b else None) for a, b in sp] for n, sp in gov["member_spans"].items()}
    gov["nominations"] = nominations
    gov["escrows"] = sorted(((e["ts"], e["nid"], e["seq"], e["env"]) for e in escrows
                             if not e["revoked"]), key=lambda x: (x[0], x[1], x[2]))
    # Stage C summary for display (owner show / elections / doctor). `armed`/`crossed` are recomputed
    # against the FINAL owner's liveness, so an open proposal shows whether it would install now.
    final_la = owner_act.get(current, gov["owner_ts"])
    qm = int(gov["config"].get("quorum_m", 0) or 0)
    dmd = float(gov["config"].get("dead_man_days", GOV_DEAD_MAN_DAYS) or 0)
    gov["elections"] = sorted(
        ({"proposal_id": pid, "new_owner_id": pr["new_oid"], "basis_ts": pr["basis_ts"],
          "votes": len(pr["voters"]), "quorum_m": qm, "installed": pr["installed"],
          "crossed": qm > 0 and len(pr["voters"]) >= qm,
          "armed": bool(final_la and pr["basis_ts"] > final_la
                        and _days_between(final_la, pr["basis_ts"]) >= dmd)}
         for pid, pr in proposals.items()),
        key=lambda x: x["proposal_id"])
    return gov


def _local_hive_id():
    """This device's hive_id (from the established owner declaration), or '' if no owner yet. Used by
    the sync layer to refuse merging journals across hives — two hives on one tailnet stay separate."""
    return _governance_state(merkle.read_all_entries(JOURNAL_DIR)).get("hive_id", "")


def _owner_declaration(entries):
    """The owner-signed genesis governance payload that established the hive (for /hive/info, so a
    joiner can verify the queen bee). None if no owner."""
    gov = _governance_state(entries)
    if not gov["owner_id"]:
        return None
    for e in entries:
        if e.get("type") == "governance":
            p = e.get("payload", {})
            if (p.get("action") == "owner" and p.get("owner_id") == gov["owner_id"]
                    and _verify_governance(p) == gov["owner_id"]):
                return p
    return None


def _probe_hive(ip, port, timeout=3):
    """GET /hive/info from ip:port and return it only if it's a GENUINE hive — a verifiable signed
    genesis whose owner matches the claimed owner_id. Rejects a non-hive service squatting the port."""
    try:
        import requests
        info = requests.get(f"http://{ip}:{port}/hive/info", timeout=timeout).json()
    except Exception:
        return None
    gen = info.get("genesis")
    if not info.get("hive_id") or not gen:
        return None
    if _verify_governance(gen) != info.get("owner_id"):
        return None
    return info


def _content_evidence(entries, gov=None, links=True):
    """content -> {'pos': {id: weight}, 'neg': {id: weight}, 'forget': bool, 'last_ts': str,
    'evidence': [(ts, sign, identity), ...]}. `evidence` is the ORDERED sequence of every evidence
    event (sign +1/-1), sorted by timestamp — retained since 1.19 as the prerequisite for a future
    per-fact adaptive half-life (design D-2); nothing reads it yet. Facts add positive identity-evidence; `retract` entries add negative (peer) evidence or set
    owner-forget (governance, decisive). last_ts = most-recent fact/retract timestamp (drives
    decay). Pure projection over the journal.

    Owner-forget authority: once an owner is established, a forget is honored only if it is
    OWNER-SIGNED by the owner AS OF its own journal position (`_owner_at`, the capsule/cell/link rule,
    so a previous owner's forget survives a transfer, succession or election; #46 Q1), or its position
    precedes the genesis owner never carries authority: 3.0 removed the pre-genesis grandfather (the
    `forget_writers=legacy` arm of #122), so a forget no owner signed as of its position is ignored whatever
    `forget_writers` says. Before any owner exists, the legacy source-tag forget (`owner:owner/owner`) is
    honored — so existing forgets are never resurrected, and after `hive-mind owner init` a forged unsigned
    forget no longer carries authority.

    Unforget (1.24, #46): an owner-signed `retract` carrying `unretracts_ref` (and no `retracts_ref`, so
    a pre-1.24 node skips it) reverses a forget. The owner acts on one CONTENT form a timeline sorted on
    `(timestamp, node_id, seq)`, the single order `_owner_at` uses (not the journal's (node_id, seq)
    loop order), and the LATEST HONORED act wins: forget → unforget is live again, unforget → forget
    stays forgotten. An unforget is honored only when signed by the owner as of its position; it is
    never grandfathered. Owner acts are governance, not evidence, so they never move `last_ts`: an
    unforgotten fact's confidence re-derives from its surviving evidence alone.

    Grounding rule (design §0.1), applied to assertions as well as links since 1.21: a fact entry whose
    `channel` is `introspect` weighs `introspect_support_weight` (governed, default 0). Reasoning is not
    observation, so two agents restating the same reasoning never corroborate each other. The fact still
    lands in the corpus (its content is a key of the result, and links and retracts can target it); a
    zero-weight assertion adds no evidence and does not move `last_ts`, exactly like a zero-weight link."""
    gov = gov or _DEFAULT_GOV
    entries = _purge_unkept(entries, gov)    # #260: a purged device's content counts only as far as a retire keeps it
    isw = float((gov.get("config") or {}).get("introspect_support_weight", CONF_INTROSPECT_SUPPORT_WEIGHT))
    out = {}
    seq_content = {}                       # (node_id, seq) of a fact entry -> its content
    for e in entries:
        if e.get("type") != "fact":
            continue
        p = e.get("payload", {})
        content = p.get("content")
        if content is None:
            continue
        seq_content[(e.get("node_id"), e.get("seq"))] = content
        ev = out.setdefault(content, {"pos": {}, "neg": {}, "forget": False, "last_ts": "", "evidence": []})
        key, w = _identity_weight(e.get("node_id"), p.get("source", "manual"))
        if _channel(p) == "introspect":
            w = w * isw
        if w <= 0:
            continue
        if w > ev["pos"].get(key, 0.0):
            ev["pos"][key] = w
        ts = e.get("timestamp", "")
        ev["evidence"].append((ts, 1, key))
        if ts > ev["last_ts"]:
            ev["last_ts"] = ts
    owner_acts = {}                        # content -> [(pos, is_forget, honored)], the #46 timeline
    for e in entries:
        if e.get("type") != "retract":
            continue
        p = e.get("payload", {})
        ref = p.get("retracts_ref")
        unforget = ref is None and p.get("unretracts_ref") is not None
        if unforget:
            ref = p.get("unretracts_ref")
        content = seq_content.get(tuple(ref)) if _valid_ref(ref) else None
        if content is None or content not in out:
            continue
        ev = out[content]
        app, ctx, instance, _sess = _parse_source(p.get("source", "manual"))
        if app in OWNER_APPS or ctx == "owner":
            pos = (e.get("timestamp", ""), str(e.get("node_id", "")), e.get("seq", 0))
            if gov["owner_id"]:
                # Owner established: honor only an act signed by the owner AS OF this position. An act
                # positioned before the genesis owner has no such owner, so none is honored (3.0 removed the
                # pre-genesis grandfather). An unsigned/forged owner act is ignored.
                signer = _verify_governance(p) if "owner_sig" in p else None
                honored = _owner_signed_at(signer, pos, gov)
            else:
                honored = not unforget   # pre-governance: legacy source-tag forget honored; no owner, no unforget
            owner_acts.setdefault(content, []).append((pos, not unforget, honored))
            ev.setdefault("acts", {})[(e.get("node_id"), e.get("seq"))] = honored   # `hv feed` reads this (2.1)
            continue                     # governance, not evidence: never moves last_ts
        elif unforget:
            continue                     # only the owner can unforget
        else:
            key = (e.get("node_id"), app, instance)
            w = SRC_CLASS_WEIGHT.get(ctx, 1.0)
            if w > ev["neg"].get(key, 0.0):
                ev["neg"][key] = w
            ev["evidence"].append((e.get("timestamp", ""), -1, key))
        ts = e.get("timestamp", "")
        if ts > ev["last_ts"]:
            ev["last_ts"] = ts
    for content, acts in owner_acts.items():
        state = False
        for _pos, is_forget, honored in sorted(acts):   # (timestamp, node_id, seq): latest honored act wins
            if honored:
                state = is_forget
        out[content]["forget"] = state
    # 1.19: `link` evidence on a FACT target — supports → positive, contradicts → negative, resolves →
    # negative (retract-equivalent) — identity-weighted exactly like an assertion or a retract, so
    # cap_self, the same-device discount and admission apply unchanged. Grounding rule (design §0.1/§6):
    # a link whose `channel` is `introspect` weighs `introspect_support_weight` (governed, default 0):
    # reasoning never corroborates or contradicts an observation; absent channel = `sense`, an
    # unrecognised one counts as `introspect` (`_channel`, #72). The §5
    # hard/evidence verdict does NOT change the weight — a hard `resolves` and a downgraded one are both
    # one identity's negative evidence; authority only decides provenance/commands (see resolve_link).
    known = {(e.get("node_id"), e.get("seq")) for e in entries}     # a dangling from_ref weighs nothing
    for e in entries if links else ():
        if e.get("type") != "link":
            continue
        if "sig" in e and not _verify_entry(e):
            continue
        p = e.get("payload", {}) or {}
        side = _LINK_EVIDENCE_KINDS.get(p.get("kind"))
        ref, frm = p.get("to_ref"), p.get("from_ref")
        content = seq_content.get(tuple(ref)) if _valid_ref(ref) else None
        if side is None or content is None or content not in out:
            continue
        if not (_valid_ref(frm) and tuple(frm) in known):
            continue
        key, w = _identity_weight(e.get("node_id"), p.get("source", "manual"))
        if _channel(p) == "introspect":
            w = w * isw
        if w <= 0:
            continue
        ev = out[content]
        if w > ev[side].get(key, 0.0):
            ev[side][key] = w
        ts = e.get("timestamp", "")
        ev["evidence"].append((ts, 1 if side == "pos" else -1, key))
        if ts > ev["last_ts"]:
            ev["last_ts"] = ts
    for ev in out.values():
        ev["evidence"].sort()
    return out


# 1.19: the link kinds the projection KNOWS. `kind` is a free string on the wire; anything else lands
# in the journal and projects to nothing (the `announce` rule), so nodes never diverge over vocabulary.
# {"supports": "pos", "contradicts": "neg", "resolves": "neg"}, derived from the registry's `evidence` field.
_LINK_EVIDENCE_KINDS = dict(vocabulary.LINK_EVIDENCE_SIDES)


_DEFAULT_GOV = {"owner_id": None, "owner_ts": "", "admitted": set(), "denied": set(), "purged": set(),
                "standbys": set(), "nominations": set(), "escrows": [], "elections": [], "owner_term": 0,
                "principals": {}, "modules": {}, "module_config": {}, "ts_tips": {}, "ts_markers": set(), "first_admit": {}, "revoked": set(),
                "keep": {}, "kept": set(),
                "config": {"same_device_lambda": CONF_SAME_DEVICE_LAMBDA, "cap_self": CONF_CAP_SELF,
                           "quorum_m": GOV_QUORUM_M, "quorum_by": GOV_QUORUM_BY,
                           "dead_man_days": GOV_DEAD_MAN_DAYS,
                           "introspect_support_weight": CONF_INTROSPECT_SUPPORT_WEIGHT,
                                        "trust_long_days": GOV_TRUST_LONG_DAYS, "trust_short_days": GOV_TRUST_SHORT_DAYS,
                                        "trust_drift_threshold": GOV_TRUST_DRIFT_THRESHOLD,
                           "flood_per_minute": FLOOD_PER_MINUTE, "flood_per_day": FLOOD_PER_DAY,
                           **_PR6_KNOB_DEFAULTS}}


def _governed_side(weights, gov):
    """Collapse identity weights {(device_id, app, instance): w} into a single governed total.
    (D0-v2) ADMISSION: once an owner is established, only admitted devices count. SAME-DEVICE
    DISCOUNT: each device contributes its strongest identity in full plus λ·(its others), since
    agents on one machine are correlated, not independent. Returns (total, set_of_device_ids)."""
    has_owner, admitted = gov["owner_id"] is not None, gov["admitted"]
    purged = gov.get("purged", set())
    keep = gov.get("keep") or {}          # a retired device's kept prefix still weighs (#260): the entries are filtered
    lam = gov["config"]["same_device_lambda"]   # by `_purge_unkept` before they reach here
    by_dev = {}
    for (dev, _app, _inst), w in weights.items():
        if has_owner and dev not in keep and (dev not in admitted or dev in purged):
            continue
        by_dev.setdefault(dev, []).append(w)
    total = 0.0
    for ws in by_dev.values():
        ws.sort(reverse=True)
        total += ws[0] + lam * sum(ws[1:])
    return total, set(by_dev.keys())


def _content_confidence(ev, gov=None):
    """Time-INVARIANT base confidence for one content: a SIGNED, GOVERNED projection. forget ->
    FORGET_FLOOR. Else net = governed(pos) - governed(neg) -> _confidence_for, then CAP_self clamps
    positive confidence when every contributing device shares one principal (self-reinforcement).
    Pure over (journal facts + governance) -> identical on every node. Decay applied at query time."""
    if ev.get("forget"):
        return FORGET_FLOOR
    gov = gov or _DEFAULT_GOV
    pos, pos_devs = _governed_side(ev["pos"], gov)
    neg, _ = _governed_side(ev["neg"], gov)
    base = _confidence_for(pos - neg)
    if base > 0 and pos_devs:
        prins = [gov["principals"].get(d) for d in pos_devs]
        if all(p is not None for p in prins) and len(set(prins)) == 1:
            base = min(base, gov["config"]["cap_self"])
    return base


def _apply_content_confidence(conn, content, ev, gov=None):
    """Write base confidence + contested flag + last-evidence ts for one content."""
    contested = 1 if (not ev.get("forget") and ev.get("pos") and ev.get("neg")) else 0
    conn.execute(
        "UPDATE facts SET confidence = ?, contested = ?, last_evidence_at = ? WHERE content = ?",
        (_content_confidence(ev, gov), contested, ev.get("last_ts") or None, content),
    )


def _recompute_confidence(conn, entries):
    """The ONLY writer of facts.confidence/contested/last_evidence_at. Pure SIGNED, GOVERNED
    projection over the journal (corroborators MINUS retractors, weighted, admitted-and-discounted;
    owner-retract forgets). Time-invariant — decay at query time."""
    gov = _governance_state(entries)
    for content, ev in _content_evidence(entries, gov).items():
        _apply_content_confidence(conn, content, ev, gov)


def _decision_evidence(entries, gov=None):
    """decision (node_id, seq) -> {'pos': {id: w}, 'neg': {id: w}, 'forget': False, 'last_ts': str,
    'evidence': [(ts, sign, identity), ...]}. A deliberate clone of `_content_evidence` keyed by the
    DECISION's journal identity instead of a content string (design §4): every `outcome-of` link is one
    identity's report that the decision worked out (polarity > 0 → pos), did not (< 0 → neg) or was
    observed and neutral (0 → recorded on pos with weight 0, so `last_ts` moves without the score).
    Weight = identity weight × |polarity| (ternary at the CLI, numeric in the schema, §4.1).
    SENSE-ONLY (§0.1): a link whose `channel` is not `sense` (absent = sense) is ignored — an agent
    cannot grade its own decision by reflection; the LINK's channel decides, never the fact's.
    Pure over the journal; `_content_confidence` scores it unchanged (governed pos − neg, cap_self when
    every reporter shares one principal — a device cannot vindicate its own decisions)."""
    gov = gov or _DEFAULT_GOV
    entries = _purge_unkept(entries, gov)    # #260: a purged device's content counts only as far as a retire keeps it
    known = {(e.get("node_id"), e.get("seq")) for e in entries}
    out = {}
    for e in entries:
        if e.get("type") != "link":
            continue
        if "sig" in e and not _verify_entry(e):
            continue
        p = e.get("payload", {}) or {}
        if p.get("kind") != "outcome-of":
            continue
        if _channel(p) != "sense":
            continue                                   # no self-grading: only observed outcomes count
        to_ref, frm = p.get("to_ref"), p.get("from_ref")
        if not (_valid_ref(to_ref) and tuple(to_ref) in known):
            continue
        if not (_valid_ref(frm) and tuple(frm) in known):
            continue
        data = p.get("data")
        try:
            pol = float((data if isinstance(data, dict) else {}).get("polarity", 1))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(pol):
            continue
        pol = max(-1.0, min(1.0, pol))
        ev = out.setdefault(tuple(to_ref), {"pos": {}, "neg": {}, "forget": False, "last_ts": "", "evidence": []})
        key, w = _identity_weight(e.get("node_id"), p.get("source", "manual"))
        side = "neg" if pol < 0 else "pos"
        w = w * abs(pol)
        if w > ev[side].get(key, 0.0) or key not in ev[side]:
            ev[side][key] = max(w, ev[side].get(key, 0.0))
        ts = e.get("timestamp", "")
        ev["evidence"].append((ts, -1 if pol < 0 else (1 if pol > 0 else 0), key))
        if ts > ev["last_ts"]:
            ev["last_ts"] = ts
    for ev in out.values():
        ev["evidence"].sort()
    return out


def _recompute_decision_outcome(conn, entries):
    """The ONLY writer of decisions.outcome_score / last_outcome_at (design §4). NOT confidence —
    decisions have none, by design: "is this the decision" is settled by supersede; "did it work out"
    is this separate, learned VINDICATION axis. Stored undecayed; the read path decays the evidence
    under the fact half-life (`_effective_confidence`) while the decision's standing never decays.
    A decision with no `outcome-of` links keeps 0.0 / NULL ("no outcome yet"), distinct from an
    observed-neutral outcome (0.0 with a `last_outcome_at`)."""
    gov = _governance_state(entries)
    conn.execute("UPDATE decisions SET outcome_score = 0.0, last_outcome_at = NULL")
    for ref, ev in _decision_evidence(entries, gov).items():
        row = _index_row(conn, list(ref))
        if row is None or row[0] != "decision":
            continue
        conn.execute("UPDATE decisions SET outcome_score = ?, last_outcome_at = ? WHERE id = ?",
                     (_content_confidence(ev, gov), ev.get("last_ts") or None, row[1]))


def _idea_evidence(entries, gov=None):
    """idea (node_id, seq) -> {'pos', 'neg', 'forget': False, 'last_ts', 'evidence'} — the fact-confidence
    evidence shape, keyed by the idea's JOURNAL IDENTITY (design §6). Only `supports` (+) and
    `contradicts` (−) links move it; the idea's own assertion contributes NOTHING (initial confidence
    0.0) and identical text elsewhere is never corroboration. Grounding rule: a link whose `channel` is
    `introspect` weighs `introspect_support_weight` (governed, default 0) — reasoning cannot promote a
    hypothesis; only `sense`-channel observation can.

    No self-support (1.21, #71): a `supports` link whose signer is the SAME PRINCIPAL as the idea's author
    (`_same_principal`: the same device, or devices admitted under one principal) weighs 0, so an idea
    earns confidence only from others' evidence. The rule is asymmetric on purpose: the author's own
    `contradicts` still counts, since it is the only way to withdraw an idea. Before an owner exists there
    is no principal map, so only the same DEVICE is excluded; an author's second device still counts.
    Admission, the same-device discount and cap_self apply as for facts via `_content_confidence`, so
    cap_self still binds when every supporter shares one OTHER principal (two devices of B on A's idea)."""
    gov = gov or _DEFAULT_GOV
    entries = _purge_unkept(entries, gov)    # #260: a purged device's content counts only as far as a retire keeps it
    isw = float((gov.get("config") or {}).get("introspect_support_weight", CONF_INTROSPECT_SUPPORT_WEIGHT))
    authors = {(e.get("node_id"), e.get("seq")): e.get("node_id") for e in entries if e.get("type") == "idea"}
    ideas = set(authors)
    known = {(e.get("node_id"), e.get("seq")) for e in entries}
    out = {ref: {"pos": {}, "neg": {}, "forget": False, "last_ts": "", "evidence": []} for ref in ideas}
    for e in entries:
        if e.get("type") != "link":
            continue
        if "sig" in e and not _verify_entry(e):
            continue
        p = e.get("payload", {}) or {}
        side = {"supports": "pos", "contradicts": "neg"}.get(p.get("kind"))
        to_ref, frm = p.get("to_ref"), p.get("from_ref")
        if side is None or not (_valid_ref(to_ref) and tuple(to_ref) in ideas):
            continue
        if not (_valid_ref(frm) and tuple(frm) in known):
            continue
        if side == "pos" and _same_principal(gov, e.get("node_id"), authors[tuple(to_ref)]):
            continue                                   # #71: an author cannot support their own idea
        key, w = _identity_weight(e.get("node_id"), p.get("source", "manual"))
        if _channel(p) == "introspect":
            w = w * isw
        if w <= 0:
            continue
        ev = out[tuple(to_ref)]
        if w > ev[side].get(key, 0.0):
            ev[side][key] = w
        ts = e.get("timestamp", "")
        ev["evidence"].append((ts, 1 if side == "pos" else -1, key))
        if ts > ev["last_ts"]:
            ev["last_ts"] = ts
    for ev in out.values():
        ev["evidence"].sort()
    return out


def _recompute_idea_confidence(conn, entries):
    """The ONLY writer of ideas.confidence/contested/last_evidence_at (1.20). Pure projection."""
    gov = _governance_state(entries)
    for ref, ev in _idea_evidence(entries, gov).items():
        row = _index_row(conn, list(ref))
        if row is None or row[0] != "idea":
            continue
        contested = 1 if (ev["pos"] and ev["neg"]) else 0
        conn.execute("UPDATE ideas SET confidence = ?, contested = ?, last_evidence_at = ? WHERE id = ?",
                     (_content_confidence(ev, gov), contested, ev.get("last_ts") or None, row[1]))


