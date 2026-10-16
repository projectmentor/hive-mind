# ── Dashboard read API (operator-facing; reused by the sync daemon's /api/* and the hdash module) ──
# Pure read functions returning JSON-able dicts. They reuse the same projections the CLI uses
# (confidence decay, governance, search), so the dashboard never drifts from `hv`. Read-only:
# they never write the journal or store.db. The daemon already serves the corpus over the tailnet
# via /sync/chunk, so /api/* exposes nothing the sync surface doesn't already.
def _is_self_owner(gov):
    """True iff THIS device holds the established owner's key (same test the boot-time pending-admissions
    notice uses), decided without reading the seed (2.0 S2, `_owner_key_state`). Best-effort; False when
    it cannot tell."""
    try:
        return _owner_key_state(gov) == "held"
    except Exception:
        return False


def _short_root(root):
    disp = root if (root or "").startswith("sha256:") else ("sha256:" + root if root else "")
    return (disp[:18] + "…" + disp[-8:]) if len(disp) > 30 else (disp or "?")


def api_overview():
    """Header + counts for the dashboard Overview. Cheap: counts + one merkle root, no network."""
    entries = merkle.read_all_entries(JOURNAL_DIR)
    gov = _governance_state(entries)
    conn = get_conn()
    nf = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
    nd = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
    contested = conn.execute("SELECT count(*) FROM facts WHERE contested = 1").fetchone()[0]
    conn.close()
    root = merkle.merkle_root(merkle.chunk_hashes(entries))
    return {
        "node": NODE_LABEL, "node_id": NODE_ID,
        "hive": gov.get("hive_id"), "owner_id": gov.get("owner_id"),
        "role": "OWNER" if _is_self_owner(gov) else "MEMBER",
        "contract": CONTRACT_VERSION,
        "facts": nf, "decisions": nd, "contested": contested,
        "nodes": len(gov.get("admitted") or set()),
        "address": _self_address() or "",
        "merkle": _short_root(root), "merkle_full": root,
    }


def api_search(query="", tag=None, kind="all", min_confidence=0.0,
               limit=50, offset=0, sort="salience", status="all", include_forgotten=True):
    """Facts + decisions for `query`, mirroring `hv search` but PAGINATED and sortable/filterable.
    `sort`: confidence (effective confidence, default; `salience` is a legacy alias) | recency (created) |
    importance | utility (1.19 PR6, learned projections decayed at query time). `status` (facts): all (live,
    >= min_confidence) | contested | forgotten (owner-forgotten, normally hidden) | volatile. Returns
    the page slice plus `*_total` so the UI can page; empty query browses everything. `kind` is exclusive:
    fact | decision | idea return only that type, `all` returns facts, decisions and ideas above zero.
    `min_confidence` filters facts, and earned ideas under kind=all (as `hv search`); raw ideas
    (kind=idea) come back whatever their confidence. Decisions have no confidence filter.
    `include_forgotten=False` drops owner-forgotten facts in every status branch (and `status=forgotten`
    then matches nothing), so totals and paging stay right; the module API passes it (2.1 A5)."""
    conn = get_conn()
    now = os.environ.get("HIVE_NOW") or _now_iso()
    sort = "confidence" if sort in (None, "", "salience") else sort
    gov = _governance_state(merkle.read_all_entries(JOURNAL_DIR))
    cap = float((gov.get("config") or {}).get("importance_self_cap", CONF_IMPORTANCE_SELF_CAP))
    hl_fact = _halflife(gov, "fact")

    def sal(r, cls):
        hl = _halflife(gov, cls)
        imp = r["importance"] if r["importance"] is not None else 0.0
        return (round(_effective_salience(imp, r["last_link_at"], now, hl, floor=_salience_floor(cls, imp, cap)), 2),
                round(_effective_salience(r["utility"] or 0.0, r["last_link_at"], now, hl), 2))

    def order(items):
        k = {"recency": lambda f: (f["created"], f["confidence"]),
             "importance": lambda f: (f["importance"], f["confidence"], f["created"]),
             "utility": lambda f: (f["utility"], f["confidence"], f["created"])}.get(
            sort, lambda f: (f["confidence"], f["created"]))
        items.sort(key=k, reverse=True)

    facts, decisions, facts_total, dec_total = [], [], 0, 0
    if kind in ("all", "fact"):
        matched = []
        for r in _query_facts(conn, query or "", limit=10000):
            base = r["confidence"]
            cls = _fact_class(r["tags"])
            eff = _effective_confidence(base, r["last_evidence_at"], now, _halflife(gov, cls))
            tags = json.loads(r["tags"]) if r["tags"] else []
            if tag and tag not in tags:
                continue
            tl = [str(t).lower() for t in tags]
            if not include_forgotten and base <= FORGET_FLOOR:
                continue
            if status == "contested":
                if not r["contested"]:
                    continue
            elif status == "forgotten":
                if base > FORGET_FLOOR:
                    continue
            elif status == "volatile":
                if not ("volatile" in tl or any(t.startswith("ttl:") for t in tl)):
                    continue
            else:  # all live facts at/above the floor (forgotten ones stay hidden here)
                if eff < min_confidence:
                    continue
            imp, util = sal(r, cls)
            matched.append({"id": r["id"], "content": r["content"], "tags": tags,
                            "confidence": round(eff, 2), "contested": bool(r["contested"]),
                            "forgotten": base <= FORGET_FLOOR, "importance": imp, "utility": util,
                            "source": r["source_agent"], "created": (r["created_at"] or "")[:10]})
        fids = _ids_for(conn, "fact", [m["id"] for m in matched])
        for m in matched:
            m["ref"] = (fids.get(m["id"]) or {}).get("ref")
            m["sid"] = (fids.get(m["id"]) or {}).get("sid")
        order(matched)
        facts_total = len(matched)
        facts = matched[offset:offset + limit]
    if kind in ("all", "decision"):
        dmatched = []
        for r in _query_decisions(conn, query or "", limit=10000):
            tags = json.loads(r["tags"]) if r["tags"] else []
            if tag and tag not in tags:
                continue
            dmatched.append({"id": r["id"], "content": r["content"], "rationale": r["rationale"] or "",
                             "tags": tags, "superseded": bool(r["superseded_by"]),
                             "created": (r["created_at"] or "")[:10],
                             "informed_by": [_ref_str(x) for x in _clean_refs(r["informed_by"])],
                             "outcome_score": (round(_effective_confidence(r["outcome_score"] or 0.0, r["last_outcome_at"], now, hl_fact), 2)
                                               if r["last_outcome_at"] else None),
                             "last_outcome_at": r["last_outcome_at"]})
        dids = _ids_for(conn, "decision", [m["id"] for m in dmatched])
        for m in dmatched:
            m["ref"] = (dids.get(m["id"]) or {}).get("ref")
            m["sid"] = (dids.get(m["id"]) or {}).get("sid")
        dsup = _supersede_info(conn, [m["id"] for m in dmatched])
        for m in dmatched:
            m.update(dsup.get(m["id"]) or _NOT_SUPERSEDED)
        dec_total = len(dmatched)
        decisions = dmatched[offset:offset + limit]
    ideas, ideas_total = [], 0
    if kind in ("all", "idea"):
        imatched = []
        for r in _query_ideas(conn, query or "", limit=10000):
            eff = _effective_confidence(r["confidence"], r["last_evidence_at"], now, _halflife(gov, "idea"))
            if kind == "all" and (eff <= 0 or eff < min_confidence):
                continue                      # under all, as `hv search`: only earned ideas, above min_confidence
            itags = json.loads(r["tags"]) if r["tags"] else []
            if tag and tag not in itags:
                continue
            imp, util = sal(r, "idea")
            imatched.append({"id": r["id"], "content": r["content"], "tags": itags,
                             "confidence": round(eff, 2), "contested": bool(r["contested"]),
                             "importance": imp, "utility": util,
                             "source": r["source_agent"], "created": (r["created_at"] or "")[:10]})
        iids = _ids_for(conn, "idea", [m["id"] for m in imatched])
        for m in imatched:
            m["ref"] = (iids.get(m["id"]) or {}).get("ref")
            m["sid"] = (iids.get(m["id"]) or {}).get("sid")
        order(imatched)
        ideas_total = len(imatched)
        ideas = imatched[offset:offset + limit]
    conn.close()
    return {"facts": facts, "decisions": decisions, "ideas": ideas,
            "facts_total": facts_total, "decisions_total": dec_total, "ideas_total": ideas_total,
            "offset": offset, "limit": limit}


def api_tags(counts=False):
    """All distinct tags across facts + decisions, sorted. Lets the dashboard tag filter stay
    COMPLETE even when the result lists are paginated (the page only carries its own rows' tags).
    counts=True adds `counts`: {tag: number of facts + decisions carrying it} (additive read field)."""
    conn = get_conn()
    tags = set()
    tally = {}
    for tbl in ("facts", "decisions"):
        try:
            rows = conn.execute(f"SELECT tags FROM {tbl} WHERE tags IS NOT NULL AND tags != ''").fetchall()
        except Exception:
            continue
        for r in rows:
            try:
                for t in {str(t).strip() for t in (json.loads(r[0]) if r[0] else [])}:
                    if t:
                        tags.add(t)
                        tally[t] = tally.get(t, 0) + 1
            except Exception:
                continue
    conn.close()
    out = {"tags": sorted(tags)}
    if counts:
        out["counts"] = {t: tally[t] for t in out["tags"]}
    return out


SERIES_DAYS_MAX = 90


def api_series(days=30, include_forgotten=True):
    """Daily counts of facts, decisions and ideas for the last `days` days (capped at 90), oldest day first, as
    {"facts": [{"day", "n"}], "decisions": [...], "ideas": [...]}. Counts only, never row text. A day with no rows is
    left out. `created_at` is the row's own timestamp, so a row whose stamp is not a `YYYY-MM-DD` date is not counted.
    `include_forgotten=False` (the module API) leaves owner-forgotten facts out of the facts counts."""
    days = max(1, min(int(days), SERIES_DAYS_MAX))
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = get_conn()
    out = {}
    try:
        for tbl in ("facts", "decisions", "ideas"):
            where = "substr(created_at, 1, 10) >= ?"
            if tbl == "facts" and not include_forgotten:
                where += f" AND confidence > {FORGET_FLOOR}"
            try:
                rows = conn.execute(f"SELECT substr(created_at, 1, 10) AS d, count(*) FROM {tbl} WHERE {where} "
                                    "GROUP BY d ORDER BY d", (since,)).fetchall()
            except Exception:
                rows = []
            out[tbl] = [{"day": r[0], "n": r[1]} for r in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0] or "")]
    finally:
        conn.close()
    return out


def api_item(sid):
    """1.19 PR3b: resolve a short id `h:…` (or a `node_id:seq` ref) through journal_index to the row it
    names on THIS node — {kind, id, ref, sid, item} — so a dashboard deep link `/#h:…` survives rebuilds
    and works on every node. `item` is the same row shape `api_search` returns; {} when unknown."""
    conn = get_conn()
    try:
        try:
            kind, ref = _parse_ref_arg(conn, str(sid or ""))
        except ValueError as e:
            return {"error": str(e)}
        row = _index_row(conn, ref)
        if row is None or kind not in ("fact", "decision", "idea"):
            return {"error": f"{sid!r} is not a fact/decision/idea on this node"}
        now = os.environ.get("HIVE_NOW") or _now_iso()
        gov = _governance_state(merkle.read_all_entries(JOURNAL_DIR))
        cap = float((gov.get("config") or {}).get("importance_self_cap", CONF_IMPORTANCE_SELF_CAP))

        def sal(r, cls):
            hl = _halflife(gov, cls)
            imp = r["importance"] if r["importance"] is not None else 0.0
            return (round(_effective_salience(imp, r["last_link_at"], now, hl, floor=_salience_floor(cls, imp, cap)), 2),
                    round(_effective_salience(r["utility"] or 0.0, r["last_link_at"], now, hl), 2))
        item = {}
        if kind == "fact":
            r = conn.execute(f"SELECT {_FACT_COLS} FROM facts WHERE id = ?", (row[1],)).fetchone()
            if r:
                base = r["confidence"]
                cls = _fact_class(r["tags"])
                imp, util = sal(r, cls)
                item = {"id": r["id"], "content": r["content"], "tags": json.loads(r["tags"]) if r["tags"] else [],
                        "confidence": round(_effective_confidence(base, r["last_evidence_at"], now, _halflife(gov, cls)), 2),
                        "contested": bool(r["contested"]), "forgotten": base <= FORGET_FLOOR,
                        "importance": imp, "utility": util,
                        "source": r["source_agent"], "created": (r["created_at"] or "")[:10]}
        elif kind == "decision":
            r = conn.execute("SELECT id, content, rationale, tags, source_agent, created_at, superseded_by, informed_by, "
                             "outcome_score, last_outcome_at FROM decisions WHERE id = ?", (row[1],)).fetchone()
            if r:
                item = {"id": r["id"], "content": r["content"], "rationale": r["rationale"] or "",
                        "tags": json.loads(r["tags"]) if r["tags"] else [], "superseded": bool(r["superseded_by"]),
                        "source": r["source_agent"], "created": (r["created_at"] or "")[:10],
                        "informed_by": [_ref_str(x) for x in _clean_refs(r["informed_by"])],
                        "outcome_score": (round(_effective_confidence(r["outcome_score"] or 0.0, r["last_outcome_at"], now, _halflife(gov, "fact")), 2)
                                          if r["last_outcome_at"] else None),
                        "last_outcome_at": r["last_outcome_at"],
                        **(_supersede_info(conn, [r["id"]]).get(r["id"]) or _NOT_SUPERSEDED)}
        else:
            r = conn.execute("SELECT id, content, tags, source_agent, created_at, confidence, contested, "
                             "last_evidence_at, importance, utility, last_link_at FROM ideas WHERE id = ?", (row[1],)).fetchone()
            if r:
                imp, util = sal(r, "idea")
                item = {"id": r["id"], "content": r["content"], "tags": json.loads(r["tags"]) if r["tags"] else [],
                        "confidence": round(_effective_confidence(r["confidence"], r["last_evidence_at"], now, _halflife(gov, "idea")), 2),
                        "contested": bool(r["contested"]), "importance": imp, "utility": util,
                        "source": r["source_agent"], "created": (r["created_at"] or "")[:10]}
        canon = _ids_for(conn, kind, [row[1]]).get(row[1]) or {}
        if item:
            item["ref"], item["sid"] = canon.get("ref"), canon.get("sid")
        return {"kind": kind, "id": row[1], "ref": canon.get("ref"), "sid": canon.get("sid"), "item": item}
    finally:
        conn.close()


def api_related(kind, item_id, limit=25, include_forgotten=True):
    """Facts related to a fact/decision by SHARED TAGS (the simple, robust signal) — powers the
    'Related facts' table in a detail view. Excludes the item itself; ranked by number of shared tags,
    then effective confidence. Empty when the source has no tags. `include_forgotten=False` (the module API,
    3.1) drops owner-forgotten facts from the result and answers empty for a forgotten source."""
    conn = get_conn()
    try:
        tbl = "decisions" if kind == "decision" else "facts"
        row = conn.execute(f"SELECT tags{', confidence' if tbl == 'facts' else ''} FROM {tbl} WHERE id = ?",
                           (item_id,)).fetchone()
        src = set()
        if row and not include_forgotten and tbl == "facts" and row["confidence"] <= FORGET_FLOOR:
            row = None
        if row and row["tags"]:
            try:
                src = {str(t).strip() for t in json.loads(row["tags"]) if str(t).strip()}
            except Exception:
                src = set()
        if not src:
            return {"related": [], "tags": []}
        now = os.environ.get("HIVE_NOW") or _now_iso()
        out = []
        for r in conn.execute(f"SELECT {_FACT_COLS} FROM facts").fetchall():
            if kind == "fact" and r["id"] == item_id:
                continue
            if not include_forgotten and r["confidence"] <= FORGET_FLOOR:
                continue
            try:
                ftags = [str(t).strip() for t in (json.loads(r["tags"]) if r["tags"] else [])]
            except Exception:
                ftags = []
            shared = src & set(ftags)
            if not shared:
                continue
            eff = _effective_confidence(r["confidence"], r["last_evidence_at"], now)
            out.append({"id": r["id"], "content": r["content"], "tags": ftags,
                        "confidence": round(eff, 2), "contested": bool(r["contested"]),
                        "shared": sorted(shared), "shared_n": len(shared),
                        "source": r["source_agent"], "created": (r["created_at"] or "")[:10]})
        out.sort(key=lambda x: (x["shared_n"], x["confidence"]), reverse=True)
        return {"related": out[:limit], "tags": sorted(src)}
    finally:
        conn.close()


def api_audit(include_forgotten=True):
    """The real `hv audit` signal as counts (+ a small sample per category) for the dashboard: re-check
    (volatile past freshness), stale (forgotten/contested/decayed), duplicate (same-source redundant +
    paraphrase), contravened (un-reconciled corrections). Light depth — cheap enough for a card.
    `include_forgotten=False` (the module API, 3.1) leaves owner-forgotten facts out of every count and sample:
    the volatile branch of `_compute_audit` buckets a forgotten volatile fact before it checks the floor."""
    a = _compute_audit(depth="light")

    def live(items):
        if include_forgotten:
            return items
        return [it for it in items
                if not (isinstance(it, tuple) and hasattr(it[0], "keys") and "confidence" in it[0].keys()
                        and it[0]["confidence"] <= FORGET_FLOOR)]
    a["recheck"], a["contravened"] = live(a["recheck"]), live(a["contravened"])

    def sample(items, n=8):
        out = []
        for it in items[:n]:
            r = it[0] if isinstance(it, tuple) else it
            try:
                out.append({"id": r["id"], "content": (r["content"] or "")[:140]})
            except Exception:
                continue
        return out

    return {
        "recheck": len(a["recheck"]),
        "stale": len(a["forgotten"]) + len(a["contested"]) + len(a["decayed"]),
        "duplicate": len(a["redundant"]) + len(a["paraphrase"]),
        "contravened": len(a["contravened"]),
        "samples": {
            "recheck": sample(a["recheck"]),
            "contested": sample(a["contested"]),
            "decayed": sample(a["decayed"]),
            "contravened": sample([c[0] for c in a["contravened"]]),
        },
    }


def _tel_recent_dict(r, node_name=None):
    """One telemetry session row → a JSON-able dict (the unit both views aggregate from)."""
    try:
        m = json.loads(r["models"]) if r["models"] else {}
    except Exception:
        m = {}
    return {"when": (r["started_at"] or r["updated_at"] or "")[:19],
            "session_id": r["session_id"], "started_at": r["started_at"], "updated_at": r["updated_at"],
            "agent": (f'{r["agent"]}:{r["agent_identity"]}'.rstrip(":")) or "-",
            "project": r["project"] or "-",
            "duration_s": r["duration_s"] or 0, "tokens_in": r["tokens_in"] or 0,
            "tokens_out": r["tokens_out"] or 0, "cost_usd": r["cost_usd"], "models": m,
            "node": r["node_id"], "node_name": node_name or NODE_LABEL}


def _tel_aggregate(recents, limit, offset, sort="recent", project=None, agent=None, node=None,
                   day=None, model=None):
    """Shared telemetry projection over a list of per-session dicts: compute `facets`, FILTER by
    node/project/agent/day/model, total + by-day + by-model over the FILTERED set, then SORT
    (recent|cost|tokens|duration) and paginate. `day`/`model` let the by-day bars and by-model rows
    drill in. Each `by_day` row carries `sessions`, `tokens` (in + out) and `cost` (USD; None when no session
    that day is priced), the 21-day cap unchanged. Used by both the per-node and hive-combined views so they behave identically."""
    facets = {"projects": sorted({x["project"] for x in recents}),
              "agents": sorted({x["agent"] for x in recents}),
              "nodes": sorted({(x.get("node_name") or x.get("node") or "-") for x in recents})}

    def keep(x):
        if project and x["project"] != project:
            return False
        if agent and x["agent"] != agent:
            return False
        if node and (x.get("node_name") or x.get("node")) != node:
            return False
        if day and (x.get("when") or "")[:10] != day:
            return False
        if model and model not in (x.get("models") or {}):
            return False
        return True
    f = [x for x in recents if keep(x)]
    tot = {"sessions": len(f), "duration_s": 0.0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "priced": False}
    by_day, by_model = {}, {}
    for x in f:
        tot["duration_s"] += x["duration_s"] or 0
        tot["tokens_in"] += x["tokens_in"] or 0
        tot["tokens_out"] += x["tokens_out"] or 0
        if x["cost_usd"] is not None:
            tot["cost_usd"] += x["cost_usd"]
            tot["priced"] = True
        d = (x["when"] or "")[:10]
        if d:
            bd = by_day.setdefault(d, {"sessions": 0, "tokens": 0, "cost": 0.0, "priced": False})
            bd["sessions"] += 1
            bd["tokens"] += (x["tokens_in"] or 0) + (x["tokens_out"] or 0)
            if x["cost_usd"] is not None:
                bd["cost"] += x["cost_usd"]
                bd["priced"] = True
        for mname, mv in (x.get("models") or {}).items():
            pm = by_model.setdefault(mname, {"in": 0, "out": 0, "cost": 0.0, "priced": False})
            pm["in"] += mv.get("in", 0)
            pm["out"] += mv.get("out", 0)
            if "cost" in mv:
                pm["cost"] += mv["cost"]
                pm["priced"] = True
    if not tot["priced"]:
        tot["cost_usd"] = None
    keyf = {"cost": lambda x: x["cost_usd"] or 0,
            "tokens": lambda x: (x["tokens_in"] or 0) + (x["tokens_out"] or 0),
            "duration": lambda x: x["duration_s"] or 0}.get(sort, lambda x: x["when"] or "")
    f = sorted(f, key=keyf, reverse=True)
    return {"totals": tot, "recent": f[offset:offset + limit], "recent_total": len(f),
            "by_day": [{"day": d, "sessions": v["sessions"], "tokens": v["tokens"], "cost": v["cost"] if v["priced"] else None}
                       for d, v in sorted(by_day.items())][-21:],
            "by_model": by_model, "facets": facets}


def api_telemetry(limit=25, offset=0, sort="recent", project=None, agent=None, node=None,
                  day=None, model=None):
    """This node's session telemetry (LOCAL, never synced) — sortable, filterable, paginated. Read-only."""
    try:
        rows = _telemetry_rows("")
    except Exception:
        return {"available": False, "node": NODE_LABEL, "node_id": NODE_ID, "totals": {},
                "recent": [], "by_day": [], "by_model": {}, "facets": {}}
    agg = _tel_aggregate([_tel_recent_dict(r) for r in rows], limit, offset, sort, project, agent, node, day, model)
    return {"available": True, "node": NODE_LABEL, "node_id": NODE_ID, **agg}


def api_telemetry_hive(limit=25, offset=0, sort="recent", project=None, agent=None, node=None,
                       day=None, model=None, per_node_cap=2000, timeout=15):
    """COMBINED telemetry across the hive — the daemon fetches each REACHABLE admitted peer's
    /api/telemetry concurrently and merges with the local DB into one feed, then sorts/filters/paginates.
    Telemetry is local + unsynced, so an OFFLINE node contributes nothing: `nodes` lists EVERY admitted
    device with its reachability + session count, and `admitted`/`reachable` make the gap explicit rather
    than silent. (Offline-inclusive totals would need a synced rollup — a deliberate follow-up.)"""
    collected = [(NODE_LABEL, NODE_ID, api_telemetry(limit=per_node_cap, offset=0), True)]
    peers = api_peers(probe=True)

    def _live(p):   # reachable NOW (has an address AND its sync status isn't offline/stale)
        st = (p.get("status") or "").lower()
        return ":" in (p.get("addr") or "") and not any(x in st for x in ("offline", "stale", "no address"))
    targets = [(p["name"], p["device"], p["addr"]) for p in peers if not p["self"] and _live(p)]
    unreachable = [(p["name"], p["device"]) for p in peers if not p["self"] and not _live(p)]
    if targets:
        import concurrent.futures
        import requests

        import sync_common

        gov = _governance_state(merkle.read_all_entries(JOURNAL_DIR))
        enforce = sync_common.sync_auth_outbound_mode() == "enforce"

        def fetch(t):
            name, nid, addr = t
            try:
                if enforce and not _telemetry_target_verified(addr, nid, gov):
                    return (name, nid, None, False)    # no signed read to an address that has not proved itself
                # /api/telemetry is loopback-only on the peer (GHSA-242f); sign this cross-node read
                # with our device key so the admitted peer serves it.
                qs = f"limit={per_node_cap}&offset=0"
                hdrs = sync_common.sign_sync_request("GET", "/api/telemetry", qs, b"")
                tel = requests.get(f"http://{addr}/api/telemetry?{qs}", timeout=timeout,
                                   headers=hdrs or None, allow_redirects=False).json()
                return (name, nid, tel, True)
            except Exception:
                return (name, nid, None, False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
            collected.extend(ex.map(fetch, targets))

    nodes, all_recent = [], []
    for i, (name, nid, tel, ok) in enumerate(collected):
        avail = bool(tel and tel.get("available") and "totals" in tel)
        # collected[0] is self; the rest came from `targets`, i.e. they ARE sync-reachable. So a peer
        # that answered sync but has no telemetry is ONLINE on older code, NOT offline.
        status = "self" if i == 0 else ("online" if avail else "no-telemetry")
        entry = {"node": name, "node_id": nid, "status": status, "reachable": avail,
                 "sessions": tel.get("totals", {}).get("sessions", 0) if avail else 0}
        if avail:
            rows = []
            for x in tel.get("recent", []):
                row = dict(x)
                row["node_name"] = name
                rows.append(row)
            all_recent.extend(rows)
            # per-node by_day over the same filters as the totals, so a client needs no per-node reads
            entry["by_day"] = _tel_aggregate(rows, 0, 0, sort, project, agent, node, day, model)["by_day"]
        nodes.append(entry)
    for name, nid in unreachable:
        nodes.append({"node": name, "node_id": nid, "status": "offline", "reachable": False, "sessions": 0})
    order = {"self": 0, "online": 1, "no-telemetry": 2, "offline": 3}
    nodes.sort(key=lambda n: (order.get(n["status"], 9), n["node"]))
    agg = _tel_aggregate(all_recent, limit, offset, sort, project, agent, node, day, model)
    return {"available": True, "scope": "hive", "nodes": nodes, "admitted": len(nodes),
            "online": sum(1 for n in nodes if n["status"] != "offline"),
            "reporting": sum(1 for n in nodes if n["status"] in ("self", "online")),
            "reachable": sum(1 for n in nodes if n["reachable"]), **agg}


def _telemetry_target_verified(addr, device, gov):
    """True iff a /hive/info probe of `addr` is `verified` (sync_common.verify_hello) for exactly `device`:
    the gate before a signed telemetry read is sent under outbound enforce. A signature names no audience."""
    import sync_common
    hdrs = _probe_headers("/hive/info")
    if not hdrs:
        return False
    base = "http://" + addr
    try:
        info = _hive_info_fetch(base + "/hive/info", hdrs, _PEER_PROBE_TIMEOUT)
        v = sync_common.verify_hello(info, hdrs.get("Hive-Auth-Nonce"), "/hive/info", base, gov, device)
    except Exception:
        return False
    return v["outcome"] == "verified" and v["device"] == device


def _probe_headers(path):
    """Hive-Auth-* headers for a GET of `path` by one of hv's own probes, signed with THIS hive's device
    key ({} without one). Their nonce is what a protocol-3 responder signs its answer over (#107)."""
    import sync_common
    return sync_common.signed_request_headers(_device_signer(), "GET", path, "")


def _probe_peer_map(timeout=6, peerlist=None, identity=None):   # generous: mobile/Termux nodes over the tailnet can answer in 3-4s
    """Concurrently probe the local .peers.json peers — /sync/hello → node_id, /hive/info → label,
    /sync/merkle-root vs ours → in-sync|diverged. Returns {node_id: (address, status, label)}. Bounded
    by ONE short timeout (all probes run concurrently), so offline peers can't serialize into a long
    hang. Empty when `requests` or the peer list is unavailable. Same resolution `hv peers` uses, but
    parallel — this is what fills in a directly-admitted peer's name/address (the gregorius gap).
    The /hive/info probe is signed, so a protocol-3 peer signs its answer over our nonce (#107). Given
    an `identity` dict, each answering entry's base URL maps to what that answer proves
    (sync_common.verify_hello) plus the `protocol_version` and `advertised` address it reports: doctor's
    `peer-identity` check. `peerlist` probes those entries instead of .peers.json's."""
    try:
        import concurrent.futures
        import requests
        import sync_common
        if peerlist is None:
            peerlist = sync_common.load_peers().get("peers", []) or []
    except Exception:
        return {}
    if not peerlist:
        return {}
    entries = merkle.read_all_entries(JOURNAL_DIR)
    local_root = merkle.merkle_root(merkle.chunk_hashes(entries))
    gov = _governance_state(entries)
    peerlist = _live_peers(peerlist, gov)      # a revoked or purged device is sent nothing (SECREV B6)
    if not peerlist:
        return {}

    def probe(peer):
        url = str(peer.get("url", "")).rstrip("/")
        if not url:
            return None
        hdrs = _probe_headers("/hive/info")
        try:
            # node_id + label come from /hive/info (open discovery) — NOT /sync/hello, which is now
            # read-gated (GHSA-242f) and would 401 for a not-yet-admitted or unsigned probe.
            info = requests.get(url + "/hive/info", timeout=timeout, headers=hdrs or None).json()
        except Exception:
            return None
        if identity is not None and isinstance(info, dict):
            dev = peer.get("id") if _is_device_id(peer.get("id")) else None
            v = sync_common.verify_hello(info, hdrs.get("Hive-Auth-Nonce"), "/hive/info", url, gov, dev)
            identity[url] = dict(v, protocol_version=info.get("protocol_version"),
                                 advertised=info.get("advertised_addr"))
        nid = info.get("node_id")
        if not nid:
            return None
        # 1.19 PR2b: the peer's agent contract. `/hive/info` carries `contract` from PR2b on; older builds
        # don't, so fall back to `/api/verify` (open discovery too), whose `version` IS the contract the
        # peer's signed manifest was cut at — readable from any 1.14+ peer, which is what makes the
        # fleet-contract gate evaluable BEFORE the fleet runs PR2b.
        contract = info.get("contract")
        if not contract:
            try:
                contract = requests.get(url + "/api/verify", timeout=timeout).json().get("version")
            except Exception:
                contract = None
        _PEER_CONTRACTS[nid] = contract
        try:
            root = requests.get(url + "/sync/merkle-root", timeout=timeout).json().get("root_hash")
            st = "in sync" if root == local_root else "diverged"
        except Exception:
            st = "reachable"
        lbl = info.get("label", "")
        # owner-supplied local name OVERRIDES the node's own label: a "label" on the .peers.json entry
        # wins over a node's self-reported label (e.g. an Android/Termux generic hostname like
        # "localhost-…"), so the owner can name a peer (zfold-6) and it stays consistent even when the
        # slow node's /hive/info probe times out.
        return (nid, url.replace("http://", "").replace("https://", ""), st, peer.get("label", "") or lbl)

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(peerlist))) as ex:
        for res in ex.map(probe, peerlist):
            if res:
                out[res[0]] = (res[1], res[2], res[3])
    return out


_PEER_PROBE_CACHE = {"t": 0.0, "map": {}}
_PEER_PROBE_LOCK = threading.Lock()
_PEER_CONTRACTS = {}          # node_id -> the `contract` a reachable peer advertised in /hive/info (1.19 PR2b)
_LINK_WRITE_PATH_MIN = "1.19"  # every peer must be at least here before verbs that now emit `link` are safe


def _contract_tuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (TypeError, ValueError):
        return None


def _fleet_floor():
    """The contract every admitted peer should advertise (2.0, #136): the current MAJOR, and never below
    1.19, where `link` entries became honoured. Until the contract bump lands on 2.0 that is still 1.19; from
    then on it is 2.0, so a 1.x peer is flagged once this node runs 2.0."""
    major = _contract_tuple(CONTRACT_VERSION)
    floor = f"{major[0]}.0" if major else _LINK_WRITE_PATH_MIN
    return max(floor, _LINK_WRITE_PATH_MIN, key=lambda v: _contract_tuple(v) or (0,))


def _fleet_contract(gov, probed, contracts, minimum=None):
    """1.19 PR2b: the version-skew gate for the `link` write path (design §2, §9). For every admitted
    device other than this one: `ok` — reachable and advertising a contract ≥ `minimum`; `behind` —
    reachable but older (or a build so old it advertises no version anywhere — it cannot honour a
    `link`, so it counts as behind); `unverified` — not reachable, so nothing can be said. The contract
    comes from `/hive/info.contract` (PR2b+) or `/api/verify.version` (any 1.14+ build). Pure over
    (gov, probed, contracts) so it is testable without a network. `minimum` defaults to `_fleet_floor()`."""
    need = _contract_tuple(minimum or _fleet_floor())
    out = {"ok": [], "behind": [], "unverified": []}
    for d in sorted(gov.get("admitted") or set()):
        if d == NODE_ID or d in (gov.get("purged") or set()) or d in (gov.get("modules") or {}):
            continue                  # a module device (2.1) has no sync address: it is not a node to reach or to upgrade
        if d not in probed:
            out["unverified"].append(d)
            continue
        c = contracts.get(d)
        ct = _contract_tuple(c)
        if ct is None or ct < need:
            out["behind"].append((d, c or "unknown (no version advertised)"))
        else:
            out["ok"].append((d, c))
    return out


def _probe_peer_map_cached(ttl=5):
    """`_probe_peer_map` with a short TTL cache. The daemon is a long-lived process, so a Hive-tab view
    followed by a node drill-down (which each need the peer→address map) reuses one probe instead of
    re-probing every peer per request."""
    import time
    now = time.monotonic()
    if _PEER_PROBE_CACHE["map"] and (now - _PEER_PROBE_CACHE["t"]) < ttl:
        return _PEER_PROBE_CACHE["map"]
    m = _probe_peer_map()
    _PEER_PROBE_CACHE.update(t=now, map=m)
    return m


_PEER_PROBE_REFRESH = 300      # a no-fan-out reader (probe=0) starts at most one background probe this often
_PEER_PROBE_BG = {"running": False}


def _probe_peer_map_last():
    """The last probe map this process paid for, at any age, for a reader that asked for no live fan-out (probe=0,
    the module API's hcns). Without it a directly-admitted peer has no address there (only a join request carries
    one), so hcns dials only the peers that joined by request and the bus is a star, not a mesh. When the map is
    older than `_PEER_PROBE_REFRESH` (or there is none), ONE background refresh starts and the reader does not wait
    for it: the next read sees it. Never more than one refresh at a time."""
    if not _PEER_PROBE_CACHE["t"] or time.monotonic() - _PEER_PROBE_CACHE["t"] >= _PEER_PROBE_REFRESH:
        with _PEER_PROBE_LOCK:
            start = not _PEER_PROBE_BG["running"]
            _PEER_PROBE_BG["running"] = True
        if start:
            def refresh():
                try:
                    _PEER_PROBE_CACHE.update(t=time.monotonic(), map=_probe_peer_map())
                except Exception:
                    pass                            # the reader keeps the last map; the next stale read tries again
                finally:
                    _PEER_PROBE_BG["running"] = False
            threading.Thread(target=refresh, name="hv-peer-probe", daemon=True).start()
    return _PEER_PROBE_CACHE["map"]


def api_node_addr(node_id):
    """The 'host:port' for an admitted, reachable peer (from the cached probe), or None. The ONLY way a
    node-scoped request resolves a target address — callers never pass a host — so the daemon's per-node
    proxy can't be pointed at an arbitrary address (no SSRF)."""
    if not node_id or node_id == NODE_ID:
        return None
    info = _probe_peer_map_cached().get(node_id)
    return info[0] if info else None


def api_verify():
    """The local integrity + signature verdict (no network anchor fetch), as data — powers the header
    'authentic' modal. {ok, version, level, lines}."""
    v = _verify_status(Path(__file__).resolve().parent, check_anchor=False)
    return {"ok": v["ok"], "version": v.get("version"), "level": v.get("level"), "lines": v.get("lines", []),
            "official_source": OFFICIAL_SOURCE, "site": "https://hivemind.projectmentor.org"}


def api_peers(probe=True):
    """Admitted devices for the dashboard Hive tab: name, device, principal, address, last-seen,
    status. `seen` is the date; `seen_at` is the full timestamp of the device's latest journal entry (None if none),
    so a client can show minutes. Names/addresses/sync-status are resolved by a bounded concurrent live probe (so a
    directly-admitted peer like gregorius still shows a real name/address); unreachable peers fall
    back to their join-request label/URL and last-seen staleness. With `probe` off there is no fan-out on the request: rows use the last
    probe this process made (`_probe_peer_map_last`), so a directly-admitted peer keeps its address. `module` is the module name when the
    device is a module identity (the owner's `admit` marker, `gov["modules"]`), else None; the marker does not
    record the host device, so there is no `host`. Read-only; never mutates."""
    entries = merkle.read_all_entries(JOURNAL_DIR)
    gov = _governance_state(entries)
    last_seen = {}
    for e in entries:
        d, ts = e.get("node_id"), e.get("timestamp", "")
        if d and ts > last_seen.get(d, ""):
            last_seen[d] = ts
    probed = _probe_peer_map_cached() if probe else _probe_peer_map_last()
    out = []
    for d in sorted(gov.get("admitted") or set()):
        prin = gov["principals"].get(d) or "?"
        seen = (last_seen.get(d, "")[:10]) or "—"
        age = _age_days(last_seen.get(d, ""))
        short = (d[:11] + "…") if len(d) > 12 else d
        if d == NODE_ID:
            name, addr, status = NODE_LABEL, (_self_address() or "—"), "self"
        elif d in probed:
            paddr, pst, plbl = probed[d]
            name = plbl or _join_request_label(d) or short
            addr, status = paddr, pst
        else:
            name = _join_request_label(d) or short
            url = _join_request_url(d)
            addr = url.rstrip("/").replace("http://", "").replace("https://", "") if url else "—"
            status = "stale" if (age is not None and age > 14) else "offline"
        out.append({"name": name, "device": d, "principal": prin,
                    "addr": addr, "seen": seen, "seen_at": last_seen.get(d) or None, "status": status, "self": d == NODE_ID,
                    "module": (gov.get("modules") or {}).get(d)})
    return out


# Commands that left core for a module. 3.1 forwarded them; 4.0 answers with exit 2 and one line naming the module
# command that replaced them, the way the 2.0 command moves did. `hv` imports no module code and runs no module.
_REMOVED_VERBS = {
    "dash": "the dashboard is the hdash module: run `hdash dash` (`hive-mind module install hdash` to add it)",
    "telemetry": "session telemetry is the hwatch module: run `hwatch telemetry ...` (`hive-mind module install hwatch` to add it)",
}


def removed_verb_cmd(args):
    """`hv dash` and `hv telemetry …` (removed in 4.0): one line on stderr, exit 2, nothing run, nothing written."""
    print(f"hv {args.command}: removed in 4.0; {_REMOVED_VERBS[args.command]}", file=sys.stderr)
    return 2


# The session telemetry store is written and reported by the hwatch module (`hwatch telemetry`). Core only reads it,
# for the loopback `/api/telemetry` the dashboard shows: read-only, never created, and never part of the journal.
TELEMETRY_DB = HIVE_HOME / ".telemetry" / "telemetry.db"


def _telemetry_rows(since=""):
    """Every session row, newest first, from the store hwatch writes; none if there is no store yet."""
    if not TELEMETRY_DB.is_file():
        return []
    conn = sqlite3.connect(TELEMETRY_DB.resolve().as_uri() + "?mode=ro", uri=True)   # as_uri() escapes # ? % in the path
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM telemetry_sessions ORDER BY COALESCE(started_at, updated_at) DESC").fetchall()
    finally:
        conn.close()
