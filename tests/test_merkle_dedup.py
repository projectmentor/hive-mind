"""Merkle robustness: physically-duplicate (node_id, seq) journal rows must not
shift the root. The journal is a G-Set keyed by (node_id, seq), so a repeated
line (a file re-imported/concatenated during recovery) names the SAME logical
entry. If the Merkle index counted it twice, the node would look permanently
'diverged' from peers holding the same set, and sync could not heal it
(append_foreign_entries also de-dups by (node_id, seq), so the peer's correct
copy is rejected as a duplicate and the extra row is never removed).

Regression for the 2026-06-21 egmbl5a<->gregorius false-divergence.
"""

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import merkle


def _entry(node, seq, content):
    return {"node_id": node, "seq": seq, "type": "fact",
            "timestamp": f"2026-06-14T00:00:{seq:02d}+00:00",
            "payload": {"content": content}, "prev_hash": "sha256:x"}


def _write(journal_dir, name, entries):
    (journal_dir / name).write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_duplicate_rows_are_collapsed(tmp_path):
    a = _entry("k1:aaa", 1, "alpha")
    b = _entry("k1:aaa", 2, "beta")
    # 'a' appears twice, byte-identical, across two files.
    _write(tmp_path, "2026-06-14.jsonl", [a, b, a])
    _write(tmp_path, "2026-06-15.jsonl", [a])

    es = merkle.read_all_entries(tmp_path)
    assert len(es) == 2
    assert [(e["node_id"], e["seq"]) for e in es] == [("k1:aaa", 1), ("k1:aaa", 2)]


def test_root_is_dup_invariant(tmp_path):
    a = _entry("k1:aaa", 1, "alpha")
    b = _entry("k1:bbb", 5, "beta")

    clean = tmp_path / "clean"
    clean.mkdir()
    _write(clean, "j.jsonl", [a, b])

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _write(dirty, "j.jsonl", [a, b, a, b, a])  # same set, extra physical rows

    root_clean = merkle.merkle_root(merkle.chunk_hashes(merkle.read_all_entries(clean)))
    root_dirty = merkle.merkle_root(merkle.chunk_hashes(merkle.read_all_entries(dirty)))
    assert root_clean == root_dirty

    # ...and the per-node chunk vectors used to localize sync deltas also agree.
    assert merkle.node_chunk_hashes(merkle.read_all_entries(clean)) == \
           merkle.node_chunk_hashes(merkle.read_all_entries(dirty))


def test_conflicting_duplicate_picks_deterministic_representative(tmp_path):
    # Two rows share (node, seq) but differ — must not happen for signed entries,
    # but the tie-break must still be deterministic so every node converges.
    lo = _entry("k1:aaa", 1, "aaa-low")
    hi = _entry("k1:aaa", 1, "zzz-high")
    one = tmp_path / "one"; one.mkdir(); _write(one, "j.jsonl", [lo, hi])
    two = tmp_path / "two"; two.mkdir(); _write(two, "j.jsonl", [hi, lo])  # reversed order
    a = merkle.read_all_entries(one)
    b = merkle.read_all_entries(two)
    assert len(a) == 1 and a == b   # same representative regardless of file order
