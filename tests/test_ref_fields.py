"""2.1 PR 2: `vocabulary.REF_FIELDS`, the payload fields the code reads as references to other entries.

#150 listed them and deferred the check; the guideline from #130 is that any ref-bearing state is
migration-covered. Two tests hold the table to the code, both ways, and one drives a rebuild for every row:
the target absent, present, or forgotten leaves no dangling edge and no crash.
"""
import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import vocabulary  # noqa: E402
from test_links import _loadhv  # noqa: E402
from test_vocabulary import _sources  # noqa: E402

# Fields that are refs but are not named `*_ref`. The scan finds `*_ref` itself, so a new one cannot hide.
NAMED_REFS = {"informed_by", "revokes", "entity_id", "fact_id", "supersedes"}
# `*_ref` names that are not journal payload fields: a key of the local `.genesis-pin` file (LOCAL_FILES).
NOT_PAYLOAD = {"genesis_ref"}


def _constants(src):
    return {n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _read_as_payload_key(src):
    """String literals used as `x.get("k")` or `x["k"]`: how a payload field is read."""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            out.add(n.args[0].value)
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
            out.add(n.slice.value)
    return out


def test_every_ref_field_the_code_reads_is_in_the_table():
    read = set()
    for src in _sources().values():
        read |= {k for k in _read_as_payload_key(src) if (k.endswith("_ref") or k in NAMED_REFS) and k not in NOT_PAYLOAD}
    missing = sorted(read - set(vocabulary.REF_FIELDS))
    assert not missing, f"add to vocabulary.REF_FIELDS (and cover it in test_every_row_survives_a_rebuild…): {missing}"


def test_every_ref_field_in_the_table_is_used_by_the_code():
    used = set().union(*(_constants(src) for src in _sources().values()))
    dead = [k for k, r in vocabulary.REF_FIELDS.items() if r["status"] != vocabulary.RESERVED and k not in used]
    assert not dead, f"in REF_FIELDS but named nowhere in the code: {dead}"


def test_ref_fields_shape():
    for name, rec in vocabulary.REF_FIELDS.items():
        assert rec["shape"] in ("pair", "pairs", "ref", "local_id"), name
        assert rec["status"] in vocabulary.STATUSES and rec["types"] and rec["meaning"].strip(), name
        assert (rec["status"] == vocabulary.RESERVED) == (name in vocabulary.ENVELOPE_FIELDS), name
        assert not name.startswith("x-"), name


# ── a rebuild over every row ─────────────────────────────────────────────────────────────────────────

def _e(node, seq, typ, payload, ts):
    return {"node_id": node, "seq": seq, "type": typ, "timestamp": f"2026-01-01T00:00:{ts:02d}Z",
            "payload": payload, "prev_hash": "sha256:genesis"}


def _fact(seq, content):
    return _e("dev", seq, "fact", {"content": content, "tags": [], "importance": 0.5, "source": "manual"}, seq)


def _decision(seq, content, **extra):
    return _e("dev", seq, "decision", {"content": content, "rationale": "r", "source": "manual", "tags": [], **extra}, seq)


NEIGHBOUR = ["dev", 3]          # a fact the carriers of `from_ref`, `to_ref` and `entity_ref` point at, besides the target


# field -> (entries that carry the field given `ref`, the kind of target it needs). Each carrier is seq 10+.
def _carrier(field, ref):
    if field in ("retracts_ref", "unretracts_ref"):
        return "fact", [_e("dev", 10, "retract", {field: ref, "reason": "", "source": "claude:primary/x"}, 10)]
    if field in ("from_ref", "to_ref"):
        pair = {"from_ref": ref, "to_ref": NEIGHBOUR} if field == "from_ref" else {"from_ref": NEIGHBOUR, "to_ref": ref}
        return "fact", [_e("dev", 10, "link", {"kind": "supports", **pair, "data": {}, "source": "manual"}, 10)]
    if field == "informed_by":
        return "fact", [_decision(10, "a decision", informed_by=[ref])]
    if field == "revokes":
        return "decision", [_decision(10, "revoke it", revokes=ref, tags=["revocation"])]
    if field == "supersedes_ref":
        return "decision", [_decision(10, "the replacement", supersedes_ref=ref)]
    if field == "resolves_ref":
        return "fact", [_e("dev", 10, "fact", {"content": "the resolver", "tags": [], "source": "manual",
                                                "resolves_ref": ref}, 10)]
    if field == "fact_ref":
        return "fact", [_e("dev", 10, "entity", {"name": "box", "type": "host"}, 9),
                        _e("dev", 11, "entity_fact", {"entity_ref": ["dev", 9], "fact_ref": ref}, 11)]
    if field == "escrow_ref":
        return "escrow", [_e("dev", 10, "governance", {"action": "revoke-escrow", "escrow_ref": ref}, 10)]
    if field == "entity_ref":
        return "entity", [_e("dev", 11, "entity_fact", {"entity_ref": ref, "fact_ref": NEIGHBOUR}, 11)]
    lid = 999 if ref[0] == "ghost" else 1      # a legacy local id: one no node has, or the first row's
    if field == "entity_id":
        return "entity", [_e("dev", 11, "entity_fact", {"entity_id": lid, "fact_id": 1}, 11)]
    if field == "fact_id":
        return "fact", [_e("dev", 9, "entity", {"name": "box", "type": "host"}, 9),
                        _e("dev", 11, "entity_fact", {"entity_id": 1, "fact_id": lid}, 11)]
    if field == "supersedes":
        return "decision", [_decision(10, "the replacement", supersedes=lid)]
    raise AssertionError(f"{field}: no carrier in test_ref_fields.py; add one for the new REF_FIELDS row")


def _target(kind, mode):
    """(ref, entries that make the target present, forgotten, or absent)."""
    if mode == "absent":
        return ["ghost", 1], []
    if kind == "fact":
        base = [_fact(1, "the target")]
    elif kind == "decision":
        base = [_decision(1, "the target decision")]
    elif kind == "escrow":
        base = [_e("dev", 1, "governance", {"action": "owner-escrow", "envelope": "x"}, 1)]
    else:
        base = [_e("dev", 1, "entity", {"name": "target", "type": "host"}, 1)]
    if mode == "forgotten":
        assert kind == "fact"
        base.append(_e("dev", 2, "retract", {"retracts_ref": ["dev", 1], "reason": "", "source": "owner:owner/owner"}, 2))
    return ["dev", 1], base


def _dangling(conn):
    q = lambda sql: [tuple(r) for r in conn.execute(sql)]   # noqa: E731
    return {
        "entity_facts": q("SELECT entity_id, fact_id FROM entity_facts WHERE entity_id NOT IN (SELECT id FROM entities)"
                          " OR fact_id NOT IN (SELECT id FROM facts)"),
        "superseded_by": q("SELECT id FROM decisions WHERE superseded_by IS NOT NULL AND superseded_by NOT IN "
                           "(SELECT id FROM decisions)"),
        "resolves": q("SELECT id FROM facts WHERE resolves IS NOT NULL AND resolves NOT IN (SELECT id FROM facts)"),
        "links": q("SELECT kind FROM links WHERE (from_kind='fact' AND from_id NOT IN (SELECT id FROM facts)) OR "
                   "(to_kind='fact' AND to_id NOT IN (SELECT id FROM facts)) OR "
                   "(from_kind='decision' AND from_id NOT IN (SELECT id FROM decisions)) OR "
                   "(to_kind='decision' AND to_id NOT IN (SELECT id FROM decisions))"),
    }


ROWS = sorted(k for k, r in vocabulary.REF_FIELDS.items() if r["status"] != vocabulary.RESERVED)


@pytest.mark.parametrize("mode", ["absent", "present", "forgotten"])
@pytest.mark.parametrize("field", ROWS)
def test_every_row_survives_a_rebuild_with_the_target_absent_present_or_forgotten(tmp_path, monkeypatch, field, mode):
    hv = _loadhv(tmp_path, monkeypatch)
    kind, carrier = _carrier(field, ["ghost", 1])
    if mode == "forgotten" and kind != "fact":
        pytest.skip("only a fact can be forgotten; another target's counterpart is covered under `present`")
    ref, base = _target(kind, mode)
    kind, carrier = _carrier(field, ref)
    extras = [_fact(NEIGHBOUR[1], "a neighbour")]
    entries = sorted(extras + base + carrier, key=lambda e: (e["node_id"], e["seq"]))
    jd = tmp_path / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    hv.init_db()
    hv.rebuild_db()                                                # no crash
    hv.rebuild_db()                                                # and idempotent
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.row_factory = sqlite3.Row
    assert not {k: v for k, v in _dangling(conn).items() if v}, "a ref left a dangling edge"
    for row in conn.execute("SELECT informed_by FROM decisions WHERE informed_by IS NOT NULL"):
        assert isinstance(hv._local_ids_for_refs(conn, json.loads(row["informed_by"])), list)
    page = hv.feed_page({}, 1000)                                  # the feed reads the same journal
    assert len(page["entries"]) == len(entries)
