"""
Merkle index over the Hive Mind journal.

Pure functions, no side effects. Used now for `hv merkle` (verification) and by
the Phase 2 sync daemon for bandwidth-efficient delta detection.

The journal is a G-Set CRDT: a set of entries keyed by (node_id, seq). To make
the Merkle root identical across nodes that hold the same set, entries are read
from every journal file and sorted into a canonical order by (node_id, seq)
before chunking.
"""

import hashlib
import json
from pathlib import Path

CHUNK_SIZE = 100


def _canonical(obj):
    """Stable bytes for hashing — must match hv._canonical."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def read_all_entries(journal_dir):
    """Read every new-format entry from all journal files, in canonical
    (node_id, seq) order. Old-format / malformed lines are skipped."""
    journal_dir = Path(journal_dir)
    if not journal_dir.exists():
        return []
    entries = []
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "node_id" in e and "seq" in e:
                entries.append(e)
    entries.sort(key=lambda e: (e["node_id"], e["seq"]))
    return entries


def chunk_hashes(entries, size=CHUNK_SIZE):
    """SHA-256 over each contiguous chunk of `size` entries (canonical bytes)."""
    hashes = []
    for i in range(0, len(entries), size):
        h = hashlib.sha256()
        for e in entries[i:i + size]:
            h.update(_canonical(e))
        hashes.append("sha256:" + h.hexdigest())
    return hashes


def merkle_root(hashes):
    """Fold a list of chunk hashes into a single root by pairwise SHA-256.
    An odd node is paired with itself. Empty journal -> 'sha256:genesis'."""
    if not hashes:
        return "sha256:genesis"
    level = list(hashes)
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else level[i]
            nxt.append("sha256:" + hashlib.sha256((left + right).encode()).hexdigest())
        level = nxt
    return level[0]
