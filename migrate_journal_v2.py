#!/usr/bin/env python3
"""
One-time migration to Phase-2 journal-identity references.

Phase 1 stored cross-row links by LOCAL SQLite id: `entity_fact` payloads carried
`entity_id`/`fact_id` and `decision` payloads carried `supersedes` (a local id).
Local ids are reassigned on every rebuild and differ between nodes, so those
links break the moment another node merges this journal.

This rewrites legacy link entries IN PLACE to reference journal identity
`(node_id, seq)` instead — `entity_ref` / `fact_ref` / `supersedes_ref` — leaving
every fact/decision/entity entry's identity untouched. Because changing a
payload changes that entry's hash, the per-node `prev_hash` chain is recomputed.

The local-id -> (node_id, seq) map is derived by replaying the journal exactly as
`hv rebuild` does (facts dedup by content; decisions/entities are sequential).

Idempotent: entries already using *_ref are left alone. Backs up journal/ first.
Run `./hv rebuild` afterwards to verify links/supersede resolve via the new refs.
"""

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HIVE_HOME = Path(os.environ.get("HIVE_HOME", Path.home() / "projects" / "hive-mind"))
JOURNAL_DIR = HIVE_HOME / "journal"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merkle  # noqa: E402


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _hash(entry):
    return "sha256:" + hashlib.sha256(_canonical(entry)).hexdigest()


def build_localid_maps(entries):
    """Replay journal id-assignment (matching hv rebuild) to map each kind's
    local id -> the journal identity (node_id, seq) that produced it."""
    fact_by_id, content_to_id, next_fact = {}, {}, 0
    dec_by_id, next_dec = {}, 0
    ent_by_id, name_to_id, next_ent = {}, {}, 0

    for e in entries:
        t, nid, seq, p = e["type"], e["node_id"], e["seq"], e["payload"]
        if t == "fact":
            c = p["content"]
            if c not in content_to_id:               # first occurrence inserts a row
                next_fact += 1
                content_to_id[c] = next_fact
                fact_by_id[next_fact] = (nid, seq)
        elif t == "decision":
            next_dec += 1
            dec_by_id[next_dec] = (nid, seq)
        elif t == "entity":
            n = p["name"]
            if n not in name_to_id:                  # upsert by name; first insert sets the id
                next_ent += 1
                name_to_id[n] = next_ent
                ent_by_id[next_ent] = (nid, seq)

    return fact_by_id, dec_by_id, ent_by_id


def convert(entries):
    """Rewrite legacy link entries to journal-identity refs. Returns count changed."""
    fact_by_id, dec_by_id, ent_by_id = build_localid_maps(entries)
    changed = 0

    for e in entries:
        p = e["payload"]
        if e["type"] == "entity_fact" and "entity_ref" not in p:
            eid, fid = p.get("entity_id"), p.get("fact_id")
            e["payload"] = {
                "entity_ref": list(ent_by_id[eid]) if eid in ent_by_id else None,
                "fact_ref": list(fact_by_id[fid]) if fid in fact_by_id else None,
                "confidence": p.get("confidence", 1.0),
            }
            changed += 1
        elif e["type"] == "decision" and p.get("supersedes") and "supersedes_ref" not in p:
            old = p["supersedes"]
            p["supersedes_ref"] = list(dec_by_id[old]) if old in dec_by_id else None
            p.pop("supersedes", None)
            changed += 1

    # Recompute each node's prev_hash chain (payload edits changed entry hashes).
    by_node = {}
    for e in entries:
        by_node.setdefault(e["node_id"], []).append(e)
    for ents in by_node.values():
        ents.sort(key=lambda e: e["seq"])
        prev = "sha256:genesis"
        for e in ents:
            e["prev_hash"] = prev
            prev = _hash(e)

    return changed


def main():
    entries = merkle.read_all_entries(JOURNAL_DIR)
    if not entries:
        print(f"No journal entries at {JOURNAL_DIR}; nothing to migrate.")
        return

    legacy = sum(
        1 for e in entries
        if (e["type"] == "entity_fact" and "entity_ref" not in e["payload"])
        or (e["type"] == "decision" and e["payload"].get("supersedes") and "supersedes_ref" not in e["payload"])
    )
    if legacy == 0:
        print("Journal already uses journal-identity refs; nothing to do.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = HIVE_HOME / f"journal.bak.v2.{stamp}"
    shutil.copytree(JOURNAL_DIR, backup)
    print(f"Backed up journal -> {backup}")

    changed = convert(entries)

    # Rewrite the journal, regrouping by timestamp date (same scheme as hv).
    for f in JOURNAL_DIR.glob("*.jsonl"):
        f.unlink()
    by_date = {}
    for e in entries:
        by_date.setdefault(e["timestamp"][:10], []).append(e)
    for date, ents in sorted(by_date.items()):
        ents.sort(key=lambda e: (e["node_id"], e["seq"]))
        with open(JOURNAL_DIR / f"{date}.jsonl", "w") as fh:
            for e in ents:
                fh.write(json.dumps(e) + "\n")

    print(f"Converted {changed} legacy link entr{'y' if changed == 1 else 'ies'} to journal-identity refs.")
    print("Next: run `./hv rebuild` and verify entity links / supersede resolve.")


if __name__ == "__main__":
    main()
