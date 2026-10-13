def _doctor_status():
    """Run the local health checks (READ-ONLY) and return a list of
    {name, status: 'ok'|'warn'|'fail', detail}. Shared by `hv doctor` text + json output.

    'fail' = something is broken and needs action (tampered source, corrupt journal, empty DB).
    'warn' = degraded or advisory (auto-sync off, a peer offline, hygiene to reconcile). A warn
    does not mean the node is unhealthy, so it does not set a non-zero exit on its own."""
    checks = []
    entries = []

    # 1. Authenticity — integrity + signature. Skip the slow network key-anchor layer so doctor
    #    stays fast and works offline; `hv verify` does the full three-layer check.
    try:
        root = Path(__file__).resolve().parent
        v = _verify_status(root, check_anchor=False)
        headline = v["lines"][0].lstrip("✓⚠ ").strip()
        status = "ok" if v["ok"] else "fail"
        if v["level"] == "modified":                    # #93: a pull inside sign.yml's re-sign window
            hint = _resign_pending_hint(root)
            if hint:
                status, headline = "warn", f"{headline} {hint}"
        checks.append({"name": "authenticity", "status": status, "detail": headline})
    except Exception as e:
        checks.append({"name": "authenticity", "status": "warn", "detail": f"check error: {e}"})

    # 1a-bis. Crypto MODULE bindings — if hv's own _ed25519 import is None, `_verify_entry` falls
    #     through to accepting EVERY entry unverified (the legacy grandfather path), i.e. signature
    #     checking is silently OFF. That must be a loud HARD failure, not a hidden degradation.
    _crypto_missing = [n for n, mod in (("ed25519", _ed25519), ("x25519", _x25519),
                                        ("chacha20poly1305", _chacha)) if mod is None]
    if "ed25519" in _crypto_missing:
        checks.append({"name": "crypto-modules", "status": "fail",
                       "detail": "ed25519 module unavailable — entry/governance signatures are NOT "
                                 "being verified (all entries accepted unsigned). Reinstall/repair."})
    elif _crypto_missing:
        checks.append({"name": "crypto-modules", "status": "fail",
                       "detail": f"crypto module(s) unavailable: {', '.join(_crypto_missing)} — "
                                 f"capsules cannot be sealed/opened here. Reinstall/repair."})

    # 1b. Crypto self-test — known-answer tests for the bundled crypto (x25519/ed25519/ed↔curve +
    #     the capsule key-agreement), run on every doctor pass (and the 15-min `--fix` timer). A
    #     broken primitive is a HARD failure (a corrupt/tampered module would silently weaken every
    #     sealed capsule), so it sets doctor's non-zero exit like authenticity does.
    try:
        import crypto_selftest
        res = crypto_selftest.run_all()
        bad = [k for k, ok in res.items() if not ok]
        if bad:
            checks.append({"name": "crypto", "status": "fail",
                           "detail": f"self-test FAILED: {', '.join(bad)} — `hv verify`/reinstall"})
        else:
            adv = []
            try:
                ap = Path(__file__).resolve().parent / "advisories.json"
                if ap.exists():
                    adv = [a for a in json.loads(ap.read_text()).get("crypto", [])
                           if a.get("severity") == "high"]
            except Exception:
                adv = []
            if adv:
                checks.append({"name": "crypto", "status": "warn",
                               "detail": f"KATs pass; {len(adv)} HIGH crypto advisory(ies): "
                                         + "; ".join(a.get("summary", "") for a in adv)})
            else:
                checks.append({"name": "crypto", "status": "ok",
                               "detail": "x25519/ed25519/ed↔curve/chacha20-poly1305(RFC8439)/sealed-box KATs pass"})
    except Exception as e:
        checks.append({"name": "crypto", "status": "fail", "detail": f"self-test error: {e}"})

    # 1b. Key file permissions — the device/owner seeds are stored RAW at 0600; the whole threat model
    #     ("defense rests on 0600 local keys") collapses if a private key is group/other-readable. So a
    #     leaky key is a HARD failure (`hv doctor --fix` re-tightens it). POSIX-only; n/a on Windows.
    if os.name == "nt":
        checks.append({"name": "keyperm", "status": "ok", "detail": "n/a on Windows (no POSIX mode)"})
    else:
        leaky = _leaky_key_files()
        dir_mode = None
        try:
            if KEY_DIR.exists():
                dir_mode = KEY_DIR.stat().st_mode & 0o777
        except OSError:
            pass
        in_tree = [lbl for lbl, _l, _t in _legacy_key_files() if lbl != "owner-pub"]
        if leaky or (dir_mode is not None and dir_mode & 0o077):
            parts = [f"{lbl} {oct(m)}" for lbl, _p, m in leaky]
            if dir_mode is not None and dir_mode & 0o077:
                parts.append(f"key directory {KEY_DIR} {oct(dir_mode)} (must be 0700)")
            checks.append({"name": "keyperm", "status": "fail",
                           "detail": "private key group/other-accessible: " + "; ".join(parts)
                                     + " — run `hv doctor --fix` for the device key, `hive-mind doctor --fix` "
                                       "for the owner key (or `chmod 600` / `chmod 700`)"})
        elif in_tree:
            checks.append({"name": "keyperm", "status": "warn",
                           "detail": f"{', '.join(in_tree)} still in the working tree ({HIVE_HOME}), which every "
                                     "agent CLI runs in — relocate to the key directory: `hv doctor --fix` moves "
                                     "the device key, `hive-mind doctor --fix` the owner key (private #27)"})
        else:
            checks.append({"name": "keyperm", "status": "ok",
                           "detail": f"key files are 0600 in {KEY_DIR} (0700), outside the working tree "
                                     "(or absent)"})

    # 2. Journal — THIS node's hash chain is contiguous from genesis and each prev_hash links.
    #    A break means local corruption or tampering (foreign nodes may be partial, so only ours).
    try:
        entries = merkle.read_all_entries(JOURNAL_DIR)
        mine = sorted([e for e in entries if e.get("node_id") == NODE_ID], key=lambda e: e["seq"])
        broken, expect_prev, expect_seq = None, "sha256:genesis", 1
        for e in mine:
            if e["seq"] != expect_seq:
                broken = f"seq gap at {e['seq']} (expected {expect_seq})"
                break
            if e.get("prev_hash") != expect_prev:
                broken = f"hash chain breaks at seq {e['seq']}"
                break
            expect_prev, expect_seq = compute_hash(e), expect_seq + 1
        root_hash = merkle.merkle_root(merkle.chunk_hashes(entries))
        if broken:
            checks.append({"name": "journal", "status": "fail",
                           "detail": f"{broken} — local journal corruption or tampering"})
        else:
            checks.append({"name": "journal", "status": "ok",
                           "detail": f"{len(mine)} own entries chained from genesis, {len(entries)} total, root {root_hash[7:23]}"})
        # Surface lines that read_all_entries silently dropped (truncated/garbled .jsonl) instead of
        # letting that data loss pass unnoticed — advisory, since recovery is best-effort by design.
        corrupt, where = merkle.corrupt_lines(JOURNAL_DIR)
        if corrupt:
            checks.append({"name": "journal-integrity", "status": "warn",
                           "detail": f"{corrupt} unreadable/garbled journal line(s) skipped "
                                     f"(in {', '.join(where)}) — possible truncated write; the rest is intact"})
    except Exception as e:
        checks.append({"name": "journal", "status": "fail", "detail": f"unreadable: {e}"})
    lock_problem = _journal_lock_problem()
    checks.append({"name": "journal-lock", "status": "fail" if lock_problem else "ok",
                   "detail": lock_problem or "the journal directory grants the write lock"})

    # 3. Database — store.db (the derived index) is readable and in step with the journal.
    #    Content-duplicate facts collapse to one row, so rows <= journal fact-entries; the red flag
    #    is an empty DB over a non-empty journal (a missed rebuild).
    jfacts = sum(1 for e in entries if e.get("type") == "fact")
    newer = _newer_store_schema()
    try:
        if newer:
            raise NewerStoreSchema(newer)
        conn = get_conn()
        nfacts = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
        conn.close()
        if jfacts and nfacts == 0:
            checks.append({"name": "database", "status": "fail",
                           "detail": f"store.db has 0 facts but the journal has {jfacts} — run `hv doctor rebuild`"})
        else:
            checks.append({"name": "database", "status": "ok",
                           "detail": f"{nfacts} fact rows from {jfacts} journal fact-entries"})
    except NewerStoreSchema as e:
        checks.append({"name": "database", "status": "fail", "detail": str(e)})
    except Exception as e:
        checks.append({"name": "database", "status": "fail",
                       "detail": f"store.db unreadable ({e}) — run `hv doctor rebuild`"})

    # 3b. Owner — can governance still be signed, and is owner-key custody sane? Read-only, offline.
    #     ok    = this device holds the CURRENT owner key (governance is signable here).
    #     warn  = key absent (a declared standby may act / nobody can sign), key sprawl (>1 holder),
    #             or an open succession nomination awaiting a claim. Advisory — never 'fail'.
    try:
        gov = _governance_state(entries)
        if not gov["owner_id"]:
            checks.append({"name": "owner", "status": "ok", "detail": "no owner established yet (`hive-mind owner init`)"})
        else:
            held = _owner_key_state(gov) == "held"     # 2.0 S2: never reads the seed
            notes = []
            if gov["owner_term"]:
                notes.append(f"{gov['owner_term']} handoff(s) from genesis")
            if gov["nominations"]:
                notes.append(f"{len(gov['nominations'])} open nomination(s) awaiting claim")
            if gov["elections"]:
                armed = sum(1 for e in gov["elections"] if e["armed"] and not e["installed"])
                notes.append(f"{len(gov['elections'])} open election(s)"
                             + (f", {armed} dead-man-armed" if armed else ""))
            # Key sprawl: declared standbys each hold a copy, plus the owner device. >1 is advisory.
            holders = len(gov["standbys"]) + 1
            sprawl = holders > 1
            if held:
                status = "warn" if (gov["nominations"] or sprawl or gov["elections"]) else "ok"
                detail = "this device holds the current owner key"
                if sprawl:
                    notes.append(f"{holders} owner-key holders (owner + {len(gov['standbys'])} standby) — key sprawl")
            elif NODE_ID in gov["standbys"]:
                # A declared owner-key backup whose local copy is missing — a real problem.
                status = "warn"
                detail = ("this device is a declared owner-key backup but its key copy is missing "
                          "— re-import or re-escrow")
            else:
                # Plain member node: not holding the owner key is the normal, healthy state.
                status = "ok"
                detail = ("owner key not on this device (normal for a member node; "
                          "the owner device handles membership and config)")
            if notes:
                detail += " (" + "; ".join(notes) + ")"
            detail = f"{gov['owner_id']} (public id): {detail}"      # the id is public; the key never printed (#27)
            checks.append({"name": "owner", "status": status, "detail": detail})
    except Exception as e:
        checks.append({"name": "owner", "status": "warn", "detail": f"owner check error: {e}"})

    # 3b'. The owner key at rest (2.0 PR 3b, private #27). A plaintext owner seed is readable by every
    #      process running as this user, the agents included, and whoever reads it can sign governance.
    #      fail = a plaintext owner key on this device; ok = sealed, or no owner key here. From file names
    #      only: this never opens either form.
    form = _owner_key_form()
    if form == "plaintext":
        checks.append({"name": "owner-seal", "status": "fail",
                       "detail": "the owner key is stored unsealed, so any process running as this user can "
                                 "read the seed and sign governance. Run `hive-mind owner seal` (it asks for "
                                 "a passphrase; `hive-mind` then asks for it once per command)"})
    elif form == "sealed":
        checks.append({"name": "owner-seal", "status": "ok",
                       "detail": f"owner key sealed at rest ({OWNER_SEALED_PATH.name}); unlocked once per "
                                 "`hive-mind` command"})
    else:
        checks.append({"name": "owner-seal", "status": "ok", "detail": "no owner key on this device"})

    # 3b'. Stray plaintext owner-key copies: paths only, never contents; nothing is deleted.
    try:
        found = _owner_copies()
        if found:
            plain = [f for f in found if Path(f).name == ".owner-key"]
            named = [f for f in found if f not in plain]
            parts = []
            if named:
                parts.append(f"{len(named)} owner-key export file(s) found by name in HIVE_HOME, the current "
                             "directory, the home directory or the identity stash, not opened, so each may be "
                             "sealed or plaintext: " + ", ".join(named))
            if plain:
                parts.append("plaintext identity-stash owner key: " + ", ".join(plain))
            checks.append({"name": "owner-copies", "status": "warn", "detail": (
                "; ".join(parts) + ". Move off-device what you need, then delete the rest yourself; "
                "`hv doctor` never deletes or reads them")})
        else:
            checks.append({"name": "owner-copies", "status": "ok", "detail": "no stray owner-key copies found"})
    except Exception as e:
        checks.append({"name": "owner-copies", "status": "warn", "detail": f"owner-copies check error: {e}"})

    # 3c. Genesis — is the declaration that established this hive pinned, and is it the only one?
    #     ok   = pinned, and exactly one self-signed `owner` declaration in the journal.
    #     warn = no pin (legacy behaviour: the earliest declaration wins, and a timestamp is written
    #            by whoever wrote the entry). Advisory, because an un-upgraded fleet must keep working.
    #     fail = more than one declaration (one of them is a rival), or a pin whose declaration is not
    #            in this journal. Both need the operator; neither has a safe automatic answer.
    try:
        gov = _governance_state(entries)
        g = gov.get("genesis") or {}
        pin = _load_genesis_pin()
        cands = _self_signed_owner_acts(entries)
        if not cands and not pin:
            checks.append({"name": "genesis", "status": "ok",
                           "detail": "no owner established yet (`hive-mind owner init` pins as it claims)"})
        elif g.get("mismatch"):
            checks.append({"name": "genesis", "status": "fail",
                           "detail": (f"the pinned genesis {pin.get('genesis_ref')} is NOT in this journal, so "
                                      "this node has no owner and refuses foreign content. Sync from the "
                                      "device you pinned, or re-pin with `hive-mind owner pin --set --force`")})
        elif len(cands) > 1:
            refs = ", ".join(f"{e.get('node_id')}:{e.get('seq')}→{(e.get('payload') or {}).get('owner_id')}"
                             for e in cands)
            detail = (f"{len(cands)} self-signed owner declarations in this journal ({refs}) — one is a "
                      "rival, and the journal never removes it")
            detail += (f"; the pin keeps {g.get('ref')} as the owner" if g.get("pinned")
                       else "; UNPINNED, so the earliest timestamp currently wins — pin the one you trust")
            checks.append({"name": "genesis", "status": "fail", "detail": detail})
        elif not g.get("pinned"):
            checks.append({"name": "genesis", "status": "warn", "detail": (
                f"genesis {g.get('ref')} is not pinned on this device — another `owner` declaration with "
                "an earlier timestamp would replace the owner. `hive-mind owner pin --set` (or `hive-mind doctor --fix`)")})
        elif pin and pin.get("restored_at"):
            checks.append({"name": "genesis", "status": "warn", "detail": (
                f"the genesis pin for {g.get('ref')} was MISSING and was re-established from this journal at "
                f"{pin['restored_at']} (no network trust). Something removed it: compare {_genesis_fingerprint(entries, gov)} "
                "with the owner's device, then `hive-mind owner pin --set --force` to clear this notice")})
        else:
            checks.append({"name": "genesis", "status": "ok",
                           "detail": f"genesis {g.get('ref')} pinned; fingerprint {_genesis_fingerprint(entries, gov)}"})
    except Exception as e:
        checks.append({"name": "genesis", "status": "warn", "detail": f"genesis check error: {e}"})

    # 3d. Unsigned entries after genesis — visibility on the class the pin closes going forward. The
    #     historical pre-genesis ones are expected and are never counted as a problem.
    try:
        gov = _governance_state(entries)
        gts = gov.get("owner_ts") or ""
        if gts:
            after = [e for e in entries if "sig" not in e and (e.get("timestamp") or "") > gts]
            if after:
                by = sorted({e.get("node_id") for e in after})
                checks.append({"name": "unsigned", "status": "warn", "detail": (
                    f"{len(after)} unsigned entr{'y' if len(after) == 1 else 'ies'} positioned after "
                    f"genesis, from {', '.join(map(str, by))} — ingest refuses new ones once pinned; "
                    "these are already stored and the journal never removes them")})
            else:
                checks.append({"name": "unsigned", "status": "ok",
                               "detail": "no unsigned entries after genesis"})
    except Exception as e:
        checks.append({"name": "unsigned", "status": "warn", "detail": f"unsigned check error: {e}"})

    # 3b. Device keys — every admitted device must have a usable Curve25519 key derived from its
    #     OWN signed identity (capsules seal to that). `missing` = admitted but hasn't synced a
    #     signed entry yet (normal/transient → warn, never fail). `suspect` = a curve_pub was
    #     carried on its join/admit that disagrees with the identity-derived key; it is IGNORED
    #     (never trusted), but a disagreement is a tamper signal worth surfacing.
    if _x25519 is None:
        checks.append({"name": "device-keys", "status": "ok", "detail": "n/a (crypto lib absent)"})
    else:
        try:
            dgov = _governance_state(entries)
            _, missing, suspect = _recipient_pubkeys(entries, dgov)
            if suspect:
                checks.append({"name": "device-keys", "status": "warn",
                               "detail": f"{len(suspect)} device(s) carried a curve_pub that disagrees with "
                                         f"their identity-derived key — IGNORED: {', '.join(sorted(suspect))}"
                                         + (f"; {len(missing)} admitted device(s) have no synced key yet" if missing else "")})
            elif missing:
                checks.append({"name": "device-keys", "status": "warn",
                               "detail": f"{len(missing)} admitted device(s) have no synced device key yet "
                                         f"(can't receive capsules until they sync): {', '.join(missing)} — "
                                         f"they self-announce via `doctor --fix` within ~15 min once on "
                                         f"contract ≥1.18, or run `hv config identity announce` on that device"})
            else:
                checks.append({"name": "device-keys", "status": "ok",
                               "detail": "all admitted devices have an identity-derived capsule key"})
        except Exception as e:
            checks.append({"name": "device-keys", "status": "warn", "detail": f"check error: {e}"})

    # 3c. Capsule version conflicts — two devices sealed the same capsule version before syncing. The
    #     projection resolves it deterministically, but one value silently lost; surface it (advisory).
    try:
        conflicts = _capsule_version_conflicts(entries)
        if conflicts:
            checks.append({"name": "capsule-conflicts", "status": "warn",
                           "detail": f"{len(conflicts)} capsule(s) had a concurrent same-version seal "
                                     f"(one writer's value lost the deterministic tie): {', '.join(conflicts)}"
                                     f" — re-`hv capsule put` the intended value to supersede"})
    except Exception:
        pass

    # 3d. Capsule entries the projection DECLINES — signer was not the authorized writer (owner-at-
    #     write-time, or admitted under fertile). Surfaces what projection authorization drops so a
    #     depended-on secret is never silently unhonored on upgrade (hive #58/#61). Advisory.
    try:
        unauth = _content_unauthorized(entries, gov, "capsule", "capsule_putters")
        if unauth:
            checks.append({"name": "capsule-authz", "status": "warn",
                           "detail": f"{len(unauth)} capsule entr(y/ies) NOT honored — signer was not "
                                     f"the authorized writer: {', '.join(unauth)} — re-`hv capsule put` "
                                     f"from an authorized device to supersede"})
    except Exception:
        pass

    # 3e. Cell/comb (executable definitions) the projection DECLINES under the `cell_writers` policy —
    #     same surfacing so an upgrade never silently drops a depended-on agent/tool definition
    #     (hive #60). Advisory.
    try:
        unauth = (_content_unauthorized(entries, gov, "cell", "cell_writers")
                  + _content_unauthorized(entries, gov, "comb", "cell_writers"))
        if unauth:
            checks.append({"name": "cell-authz", "status": "warn",
                           "detail": f"{len(unauth)} cell/comb definition(s) NOT honored — signer was "
                                     f"not the authorized writer: {', '.join(unauth)} — re-publish with "
                                     f"`hv wire --add` from an authorized device to supersede"})
    except Exception:
        pass

    # 3f. `supersedes`/`resolves` LINKS the projection DOWNGRADED to evidence-only — signer was neither the
    #     owner-at-position (owner_sig on the payload) nor the target's author (design §5). The link still
    #     weighs on confidence; it just cannot hide/replace. The owner ratifies by re-issuing it owner-
    #     signed. Advisory.
    try:
        down = _links_unauthorized(entries, gov)
        if down:
            checks.append({"name": "link-authz", "status": "warn",
                           "detail": f"{len(down)} link(s) downgraded to evidence (signer neither owner-at-"
                                     f"position nor the target's author): {', '.join(down)} — re-issue "
                                     f"owner-signed to make it hard"})
    except Exception:
        pass

    # 3f1. Entries whose payload the projection cannot bind as written (#205): ingest refuses them now, but a
    #      journal is permanent, so one that landed earlier is skipped (a non-object payload, a hostile number)
    #      or projected by default. Each names its signer so the owner can revoke the device. Advisory.
    try:
        bad = []
        for e in entries:
            why = _payload_problem(e) or _hostile_problem(e)
            if why is not None:
                sg = _signer(e.get("node_id"), gov)
                who = sg["node_id"] if not sg["principal"] else f"{sg['node_id']}, principal {sg['principal']}"
                bad.append(f"{e.get('node_id')}:{e.get('seq')} ({e.get('type')}: {why}; "
                           f"{'skipped' if _unprojectable(e) else 'defaulted'}; signed by {who})")
        if bad:
            checks.append({"name": "payload-shape", "status": "warn",
                           "detail": f"{len(bad)} entr{'y' if len(bad) == 1 else 'ies'} with a malformed payload "
                                     f"field, skipped or projected by default: {', '.join(bad[:10])}"
                                     f"{' …' if len(bad) > 10 else ''}"})
    except Exception:
        pass

    # 3f1b. Module write policy (#50): entries a module's key landed that the API gate would have refused (a `retract`,
    #       a `manual` source, a foreign name, ...). They stay in the journal, which is permanent, and stop projecting.
    #       Each names its module and entry so the owner can see what to revoke or `module remove`. Advisory.
    try:
        breach = sorted(_module_policy_declined(entries, gov), key=lambda k: (str(k[0]), k[1] if isinstance(k[1], int) else 0))
        if breach:
            mods = gov.get("modules") or {}
            checks.append({"name": "module-policy", "status": "warn",
                           "detail": f"{len(breach)} entr{'y' if len(breach) == 1 else 'ies'} a module key wrote that the "
                                     f"module API would have refused; they do not project: "
                                     f"{', '.join(f'{n}:{q} (module {mods.get(n)})' for n, q in breach[:10])}"
                                     f"{' …' if len(breach) > 10 else ''}"})
    except Exception:
        pass

    # 3f1b2. Entity rewrites (SECREV B3, #51): an `entity` write by a device that is neither the name's creator nor
    #        the owner is weigh-only, as a link is: the projection skips it and the entity keeps the creator's
    #        (or the owner's) latest write. A journal written before this rule may hold some that used to win;
    #        they stay on disk and stop winning. Advisory.
    try:
        pentries = _projection_entries(entries)
        _md, weigh = _entity_verdicts(pentries, _governance_state(pentries))
        if weigh:
            by_key = {(e.get("node_id"), e.get("seq")): e for e in pentries}
            checks.append({"name": "entity-rewrite", "status": "warn",
                           "detail": f"{len(weigh)} entity write{'s' if len(weigh) != 1 else ''} by a device that is neither "
                                     f"the entity's creator nor the owner; they do not replace the entity: "
                                     + ", ".join(f"{n}:{q} ({_entity_name(by_key[(n, q)])})"
                                                 for n, q in sorted(weigh, key=lambda k: (str(k[0]), k[1] if isinstance(k[1], int) else 0))[:10])
                                     + (" …" if len(weigh) > 10 else "")})
    except Exception:
        pass

    # 3f1a. Bounded timestamps (#217): `ts-bounds` reports what the projection skips; `future-dated` and `flood` are
    #       detection only (local arrival times, owner-signed thresholds) and change nothing. Advisory.
    try:
        skipped = _ts_violations(entries, gov)
        if skipped:
            checks.append({"name": "ts-bounds", "status": "warn",
                           "detail": f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} outside the timestamp "
                                     f"bounds, skipped by the projection: "
                                     + ", ".join(f"{n}:{q} ({why})" for (n, q), why in sorted(skipped.items())[:5])
                                     + (" …" if len(skipped) > 5 else "")})
        by_kind = {}
        for f in _timestamp_findings(entries, gov):
            by_kind.setdefault(f[0], []).append(f)
        for kind, fs in by_kind.items():
            checks.append({"name": kind, "status": "warn",
                           "detail": f"{len(fs)} device(s): " + "; ".join(_timestamp_finding_text(f) for f in fs[:5])
                                     + (" …" if len(fs) > 5 else "")})
    except Exception:
        pass

    # 3f1c. Owner succession (SECREV A3): who carried each owner-signed act, and in what order the succession acts
    #       reached this node. A report only: it changes which act is honoured by nothing. FAIL = a succession act
    #       arrived after one that sorts later (a backdated act); warn = owner-signed acts dropped because no member
    #       carried them.
    try:
        if gov.get("owner_id"):
            inv = _succession_inversions(entries)
            if inv:
                bits = [f"{a} sorts before {b} but reached this node after it" for a, b in inv[:5]]
                checks.append({"name": "succession", "status": "fail",
                               "detail": "; ".join(bits) + " — a succession act dated into an earlier term. Verify the "
                                         "chain with the owner out of band and rotate the retired owner key"})
            elif gov.get("uncarried"):
                u = gov["uncarried"]
                checks.append({"name": "succession", "status": "warn",
                               "detail": f"{len(u)} owner-signed act(s) carried by a device that was not a member at "
                                         f"that position are not honoured: "
                                         + ", ".join(f"{n}:{q} ({a})" for n, q, a in u[:5])})
            else:
                checks.append({"name": "succession", "status": "ok",
                               "detail": "succession acts arrived in journal order; every owner-signed act was carried "
                                         "by a member"})
    except Exception:
        pass

    # 3f1b. The local quarantine (#205): entries ingest refused for their payload, kept verbatim on this node
    #       only (the newest QUARANTINE_MAX). Advisory.
    try:
        held = _read_quarantine()
        if held:
            latest = [f"{r.get('signer', {}).get('node_id')}: {r.get('reason')}" for r in held[-3:]]
            checks.append({"name": "quarantine", "status": "warn",
                           "detail": f"{len(held)} refused entr{'y' if len(held) == 1 else 'ies'} in "
                                     f"$HIVE_HOME/.quarantine.jsonl (local, never synced; newest {QUARANTINE_MAX} "
                                     f"kept); latest: {'; '.join(latest)}"})
    except Exception:
        pass

    # 3f2. The pre-genesis forget migration (3.0, plan 3.4). The grandfather is gone, so the check is about the
    #      MIGRATION SET: FAILS while a fact is hidden only by an unsigned pre-genesis owner forget (the facts 2.x
    #      still hid and 3.0 would show), listing each by `h:` id; OK on an empty list, whatever `forget_writers`
    #      says. `hive-mind migrate forget` re-issues them owner-signed; it also runs in `hive-mind update`.
    try:
        c = _forget_authz_check(entries, gov)
        if c is not None:
            checks.append(c)
    except Exception:
        pass

    # 3f3. Entity identity (3.0, §4.4): the store is at the current schema and the name -> identity split changed
    #      nothing for a legacy name group. Reports how many names more than one entity holds.
    try:
        _c = get_conn()
        try:
            checks.append(_entity_identity_check(_c, entries))
        finally:
            _c.close()
    except Exception:
        pass

    # 3g. Trust drift (design §8): a device whose short-window reliability or decision-outcome mean fell
    #     below the governed threshold relative to its long window. A SIGNAL for the human (possible
    #     compromise, prompt injection, tool failure, drift) — by decision it changes nothing: not admission,
    #     not purge, not link authority. Advisory.
    try:
        drift = _trust_drifting(entries, gov, os.environ.get("HIVE_NOW") or _now_iso())
        if drift:
            checks.append({"name": "trust-drift", "status": "warn",
                           "detail": f"{len(drift)} device(s) with falling reliability: {'; '.join(drift)} — "
                                     f"inspect recent writes (`hv search`, `hv audit`); nothing is enforced"})
    except Exception:
        pass

    # 3h. Fleet contract (1.19 PR2b, design §9): the verbs `--supersedes` / `--resolves` / `entity link`
    #     now emit `link` entries, which a pre-1.19 peer lands but does not honour (converged journals,
    #     divergent views). Lists admitted peers that are behind or cannot be verified. Upgrading is a
    #     human act — `--fix` never touches it. Advisory.
    try:
        if gov.get("owner_id"):
            fc = _fleet_contract(gov, _probe_peer_map_cached(), _PEER_CONTRACTS)
            if fc["behind"] or fc["unverified"]:
                parts = []
                if fc["behind"]:
                    parts.append("behind: " + ", ".join(f"{d[:11]}… ({c})" for d, c in fc["behind"]))
                if fc["unverified"]:
                    parts.append("unreachable, cannot verify: " + ", ".join(d[:11] + "…" for d in fc["unverified"]))
                checks.append({"name": "fleet-contract", "status": "warn",
                               "detail": f"{len(fc['behind'])} peer(s) below contract {_fleet_floor()}, "
                                         f"{len(fc['unverified'])} unverifiable — {'; '.join(parts)}. A peer below "
                                         f"this node's MAJOR still converges, but runs another agent surface (and "
                                         f"one before 1.19 does not honour `link` entries): upgrade it "
                                         f"(`hive-mind update`), or `hive-mind group purge` a dead device"})
    except Exception:
        pass

    # 3i. Modules (2.1, M7): one result per installed module, none on a node without any.
    try:
        import hive_module_doctor
        checks.extend(hive_module_doctor.checks(
            _hv_namespace(), gov, entries,
            probe=lambda: (_probe_peer_map_cached(), _PEER_CONTRACTS)))
    except Exception as e:
        checks.append({"name": "modules", "status": "warn", "detail": f"check error: {e}"})

    # 4. Hygiene — duplicates / obsolete / re-check, as surfaced by the audit. Advisory only.
    try:
        a = _compute_audit(depth="light")
        red = len(a["redundant"])
        obs = len(a["forgotten"]) + len(a["contested"]) + len(a["decayed"])
        rec = len(a["recheck"])
        if red or obs or rec:
            checks.append({"name": "hygiene", "status": "warn",
                           "detail": f"{red} duplicate, {obs} obsolete, {rec} to re-check — run `hv audit`"})
        else:
            checks.append({"name": "hygiene", "status": "ok", "detail": "no duplicates or obsolete facts"})
    except Exception as e:
        checks.append({"name": "hygiene", "status": "warn", "detail": f"audit error: {e}"})

    # 4b. Agent integration — are the Claude Code hooks (and the skill) wired? They live in a FOREIGN
    #     config (~/.claude/settings.json) the daemon self-heal never owned, so a node updated from
    #     before a hook existed can pass every check above yet silently miss half its hooks. Reconcile
    #     against the canonical spec; `--fix` (and the 15-min timer) re-asserts. Silent where there is
    #     no Claude Code on the node — the integration is simply not applicable there.
    try:
        w = _wire_agent(_builtin_claude_cell(), write=False)
        if w["applicable"]:
            if w["missing"] or w["stale"] or not w["skill_ok"]:
                bits = []
                if w["missing"]:
                    bits.append(f"{len(w['missing'])} dispatch shim(s) unwired")
                if w["stale"]:
                    bits.append(f"{len(w['stale'])} legacy hook(s) to migrate")
                if not w["skill_ok"]:
                    bits.append("hive-memory skill not linked")
                checks.append({"name": "agent-hooks", "status": "warn",
                               "detail": "; ".join(bits) + " — `hv doctor --fix`"})
            else:
                checks.append({"name": "agent-hooks", "status": "ok",
                               "detail": "Claude Code dispatch shim + skill wired"})
    except Exception as e:
        checks.append({"name": "agent-hooks", "status": "warn", "detail": f"check error: {e}"})

    # 5 + 6. Sync daemon liveness + peer reachability — best-effort network probes.
    try:
        import sync_common
        cfg = sync_common.load_peers()
        port, peers = cfg.get("port", 9876), cfg.get("peers", [])
    except Exception:
        port, peers = 9876, []
    try:
        import requests
    except Exception:
        requests = None

    orphans, _unit_exists = _orphan_daemons()
    if requests is None:
        status, detail = "warn", "python 'requests' not installed; cannot probe"
    else:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/sync/merkle-root", timeout=3)
            r.raise_for_status()
            status, detail = "ok", f"responding on 127.0.0.1:{port}"
        except Exception:
            status, detail = "warn", f"no response on 127.0.0.1:{port} (auto-sync off?) — `systemctl --user start hive-sync`"
    if orphans:
        # A daemon is running outside systemd's control (e.g. the unit is dead but a stale process
        # still holds the port and serves old code). The port may answer, but it's not the managed one.
        status = "warn"
        detail += f"; {len(orphans)} orphan daemon(s) outside systemd (PID {', '.join(map(str, orphans))}) — `hv doctor --fix`"
    checks.append({"name": "sync-daemon", "status": status, "detail": detail, "orphans": orphans})

    # sync-bind (#47): the liveness probe above answers on the loopback alias whether the primary bind
    # is loopback or the tailnet IP, so it can't see a daemon stuck on loopback. /hive/info (open
    # discovery, served on the alias) carries the live `advertised_addr`; compare it with what
    # resolve_bind picks now. No verdict when the daemon isn't answering: sync-daemon already warns.
    if requests is not None and status == "ok":
        try:
            import sync_common
            info = requests.get(f"http://127.0.0.1:{port}/hive/info", timeout=3).json()
            bcfg = sync_common.load_peers()
            sb_status, sb_detail = _sync_bind_verdict(sync_common.resolve_bind(bcfg), info.get("advertised_addr"),
                                                      sync_common.bind_is_auto(bcfg))
            checks.append({"name": "sync-bind", "status": sb_status, "detail": sb_detail})
        except Exception as e:
            checks.append({"name": "sync-bind", "status": "warn", "detail": f"check error: {e}"})
        # daemon-code (#112): the daemon answers, but is it still running the code in its checkout? A
        # bare `git pull` moves the files and leaves the process on the old code. Only a real mismatch
        # (stale=True) makes --fix restart; a check error never does.
        try:
            dc = _daemon_code_probe(requests, port)
            if dc:
                checks.append({"name": "daemon-code", "status": dc[0], "detail": dc[1], "stale": dc[0] == "warn"})
        except Exception as e:
            checks.append({"name": "daemon-code", "status": "warn", "detail": f"check error: {e}", "stale": False})

    if not peers:
        checks.append({"name": "peers", "status": "ok", "detail": "none configured"})
    elif requests is None:
        checks.append({"name": "peers", "status": "warn", "detail": f"{len(peers)} configured; cannot probe (no 'requests')"})
    else:
        local_root = merkle.merkle_root(merkle.chunk_hashes(entries))
        up, down, diverged = [], [], []
        answered = {}                       # base URL -> did it answer (peer-address reuses this probe)
        for p in peers:
            base = str(p.get("url", "")).rstrip("/")
            # Label by the peer's ADDRESS, not the .peers.json `id` (which may be a principal like
            # "david" — ambiguous, since many devices share a principal). `hv peers` maps each
            # address to its authoritative device_id + principal.
            pid = base.replace("http://", "") or p.get("id", "?")
            try:
                rr = requests.get(f"{base}/sync/merkle-root", timeout=5)
                rr.raise_for_status()
                (up if rr.json().get("root_hash") == local_root else diverged).append(pid)
                answered[base] = True
            except Exception:
                answered[base] = False
                down.append(pid)
        if down or diverged:
            bits = []
            if up:
                bits.append(f"{len(up)} in sync")
            if diverged:
                bits.append(f"diverged: {', '.join(diverged)} (run `hv sync now`)")
            if down:
                bits.append(f"unreachable: {', '.join(down)}")
            bits.append("`hv peers` for device + principal")
            checks.append({"name": "peers", "status": "warn", "detail": "; ".join(bits)})
        else:
            checks.append({"name": "peers", "status": "ok", "detail": f"{len(up)} peer(s) reachable and in sync"})

        # peer-address (#7): for each stored address that did not answer just now, has that peer's device
        # since verified itself from another address (a signed request that reached this node's daemon, or
        # a signed hello this node asked for)? Reuses the probe above. A candidate counts only while its
        # device would still pass _verify_sync_request (admitted, not purged), so a purged device is never
        # repointed. With none, the prover asks the entry's candidate hosts for a signed /hive/info (#107).
        try:
            import sync_common
            pp = sync_common.peers_path()
            cands = _load_peer_candidates()
            pgov = _governance_state(entries)
            if pgov.get("owner_id"):
                cands = {d: h for d, h in cands.items() if d in pgov["admitted"] and d not in pgov["purged"]}
            checks.append(_peer_address_check(peers, answered, cands, int(pp.stat().st_mtime) if pp else 0,
                                              _tailscale_hosts, _peer_address_prover(pgov)))
        except Exception as e:
            checks.append({"name": "peer-address", "status": "warn", "detail": f"check error: {e}"})

        # peer-identity (#107): what each ANSWERING peer's signed /hive/info proves, the protocol it
        # reports, and whether the fleet is ready for inbound and outbound enforce. Advisory.
        try:
            import sync_common
            ident = {}
            _probe_peer_map(timeout=5, peerlist=[p for p in peers if answered.get(str(p.get("url", "")).rstrip("/"))],
                            identity=ident)
            checks.append(_peer_identity_check(peers, ident, sync_common.sync_auth_outbound_mode()))
        except Exception as e:
            checks.append({"name": "peer-identity", "status": "warn", "detail": f"check error: {e}"})

    return checks


def _restart_managed_daemon():
    """Restart the supervisor-managed sync daemon (systemd on Linux/WSL, launchd on macOS, runit on
    Android/Termux). Returns a one-line description of what was restarted, or None if there is no
    managed service. Callers honour --dry-run before calling it."""
    if sys.platform == "darwin":
        if _launchd_hive_sync()[0]:
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.projectmentor.hive-sync"],
                           capture_output=True)
            time.sleep(2)
            return "restarted the launchd hive-sync agent."
    elif os.environ.get("TERMUX_VERSION") or os.path.isdir("/data/data/com.termux"):
        # Android/Termux: sys.platform is "linux" but there is no systemd; runit's runsvdir respawns
        # the daemon on its own, so this is best-effort parity.
        if shutil.which("sv"):
            subprocess.run(["sv", "restart", "hive-sync"], capture_output=True)
            time.sleep(2)
            return "restarted the runit hive-sync service."
    else:
        unit_exists, _ = _systemd_hive_sync()
        if unit_exists:
            subprocess.run(["systemctl", "--user", "restart", "hive-sync"], capture_output=True)
            time.sleep(2)
            return "restarted the systemd hive-sync unit."
    return None


def _daemon_code_verdict(loaded, current_digest, hive_listener):
    """#112, pure: (status, detail) for the `daemon-code` check, or None for no verdict. `loaded` is the
    daemon's /api/daemon record, the string "missing" when that route 404s, or None when nothing usable
    answered; `current_digest` is the source digest of the DAEMON'S OWN root now (never the checkout
    doctor runs from, so a worktree cannot flag or restart the live daemon); `hive_listener` says the
    404 came from a hive daemon (its /hive/info carries a hive_id), not some other process on the port.
    The digest is `hv verify`'s, so a docs-only commit counts and a re-sign alone does not."""
    if loaded == "missing":
        return (("warn", "the sync daemon predates the daemon-code check (no /api/daemon); "
                         "run `hive-mind update`") if hive_listener else None)
    if not isinstance(loaded, dict) or not loaded.get("digest") or not current_digest:
        return None
    root = loaded.get("root") or "?"
    if loaded["digest"] == current_digest:
        return "ok", f"the running sync daemon matches {root}'s source digest"
    return "warn", (f"the running sync daemon does not match {root}'s source digest "
                    f"(started {loaded.get('started_at') or '?'}); run `hive-mind update`")


def _daemon_code_probe(requests, port):
    """#112: read the daemon's /api/daemon (loopback) and re-fingerprint its own root; see the verdict.
    A daemon that stops answering here is no verdict, not a check error: sync-daemon owns liveness."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/api/daemon", timeout=2)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return None
    if r.status_code == 404:
        try:
            hive_listener = bool(requests.get(f"http://127.0.0.1:{port}/hive/info", timeout=2).json().get("hive_id"))
        except Exception:
            hive_listener = False
        return _daemon_code_verdict("missing", None, hive_listener)
    if r.status_code != 200:
        return None
    loaded = r.json()
    root = Path(loaded.get("root") or "")
    current = _source_manifest(root)["digest"] if loaded.get("root") and root.is_dir() else None
    return _daemon_code_verdict(loaded, current, True)


def _sync_bind_verdict(resolved, advertised, auto):
    """#47, pure: (status, detail) for the `sync-bind` check. `resolved` is what resolve_bind picks now,
    `advertised` the daemon's live `advertised_addr` from /hive/info (None when it is bound to
    loopback). Never asks the daemon to move toward loopback."""
    import sync_common
    if not auto:
        return "ok", "bind fixed by HIVE_BIND or .peers.json"
    if sync_common.is_loopback(resolved):
        return "ok", "no tailnet address; loopback-only is expected"
    if advertised is None:
        return "warn", (f"daemon bound to 127.0.0.1 while tailnet {resolved} is up (peers can't reach it) "
                        f"— `hv doctor --fix` restarts it")
    host = str(advertised).rsplit(":", 1)[0]
    if host != resolved:
        return "warn", f"tailnet IP changed: daemon on {host}, this node is now {resolved} — `hv doctor --fix` restarts it"
    return "ok", f"bound to the tailnet at {advertised}"


# ── peer-address (#7) ─────────────────────────────────────────────────────────────────────────────
# A peer whose tailnet IP changed (same device key, new address) used to fail silently until someone
# edited .peers.json. The daemon now notes where each device VERIFIED itself: a request whose
# Hive-Auth-* envelope passed _verify_sync_request, from a non-loopback source address, and (#107) a
# /sync/hello this node asked for whose responder signature proved the device at the address contacted
# ("via": "outbound"). Those are the proofs of a peer's identity the transport has, so they are the only
# basis on which doctor repoints an entry. For a peer that never contacts this node, doctor also asks the
# candidate addresses (tailscale names, other sightings, its join-request URL) for a signed /hive/info
# (_peer_address_prover); a name alone is only ever an unverified hint. The file is local operational
# state: never journaled, never synced, git-ignored.
PEER_CANDIDATES_PATH = HIVE_HOME / ".peer_candidates.json"
_PEER_CANDIDATES_PER_DEVICE = 8       # addresses kept per device, most recently seen first
_PEER_CANDIDATES_MAX_DEVICES = 256    # a pre-owner hive verifies any valid signature, so bound the file


def _atomic_write_text(path, text):
    """Write `path` through a temp file in the same directory and os.replace, so a concurrent reader sees
    the old file or the new one, never a torn one. An existing file keeps its mode; a new one is 0600.
    A symlinked file is rewritten at its target, so the link survives."""
    import tempfile
    path = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


