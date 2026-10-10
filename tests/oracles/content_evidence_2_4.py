"""FROZEN: `_content_evidence` exactly as it stood in hive-mind 2.4 (`hv` at `53a3eeb`, contract 2.4). Never edit.

3.0 removes `forget_writers=legacy` and the pre-genesis honouring arm from the live function (release 3.0
plan, 3.5). This copy keeps that arm, so a differential test can say what 3.0 changed and what it did not.
The function text between the two markers is byte-for-byte the 2.4 text; `tests/test_forget_oracle.py` pins
its sha256 and, where the commit is in the checkout, compares it with `git show 53a3eeb:hv`.

Only the function is frozen. The helpers it calls (`_purge_unkept`, `_identity_weight`, `_owner_at`,
`_owner_signed_at`, `_verify_governance`, ...) are the LIVE ones, supplied by `bind(hv)`: the oracle answers
"what would the 2.4 forget rule say over today's governance and signing code", which is the question the
migration gate asks. A later PR that changes one of those helpers changes both sides alike.
"""
# ruff: noqa
# flake8: noqa
# --- begin frozen 2.4 text ---
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
    precedes the genesis owner (grandfathered, signed or not) while the governed `forget_writers` policy is
    `legacy`, the default; `forget_writers=owner` closes that grandfather (#122 step 2). Before any owner
    exists, the legacy source-tag forget (`owner:owner/owner`) is honored — so existing forgets are never
    resurrected, and after `hive-mind owner init` a forged unsigned forget no longer carries authority.

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
    timeline = gov.get("owner_timeline") or []
    # #122 step 2 (1.25): `forget_writers=owner` closes the pre-genesis grandfather; `legacy` (default) keeps it.
    grandfather = (gov.get("config") or {}).get("forget_writers", "legacy") != "owner"
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
                # Owner established: honor only an act signed by the owner AS OF this position. A forget
                # positioned before the genesis owner is grandfathered unless `forget_writers=owner`
                # (#122); an unforget never is. An unsigned/forged owner act is ignored.
                signer = _verify_governance(p) if "owner_sig" in p else None
                at = _owner_at(timeline, pos)
                honored = _owner_signed_at(signer, pos, gov) or (at is None and not unforget and grandfather)
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
# --- end frozen 2.4 text ---


def bind(hv):
    """The frozen function, with `hv`'s module namespace as its globals (a snapshot taken now)."""
    import types
    return types.FunctionType(_content_evidence.__code__, dict(vars(hv)), "_content_evidence_2_4",
                              _content_evidence.__defaults__)
