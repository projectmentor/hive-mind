"""
Merkle index over the Hive Mind journal.

Pure functions, no side effects. Used now for `hv doctor merkle` (verification) and by
the Phase 2 sync daemon for bandwidth-efficient delta detection.

The journal is a G-Set CRDT: a set of entries keyed by (node_id, seq). To make
the Merkle root identical across nodes that hold the same set, entries are read
from every journal file, de-duplicated by (node_id, seq), and sorted into a
canonical order by (node_id, seq) before chunking. The de-dup matters: a
(node_id, seq) names ONE logical entry, so a physically-repeated line (a file
re-imported or concatenated during recovery) must collapse to one — otherwise it
would shift a chunk hash and make the node look permanently "diverged" from peers
that hold the same set, and sync could not heal it (append_foreign_entries also
de-dups by (node_id, seq), so the peer's correct copy is rejected as a duplicate
and the extra row is never removed).
"""

import hashlib
import json
from pathlib import Path

CHUNK_SIZE = 100

# The envelope of a journal entry: the fields every reader trusts before it looks at a payload (SECREV A1).
SEQ_MAX = 2**53                 # exclusive: a seq must survive a JSON round trip through a float, and SQLite binds int64
SEQ_JUMP_MAX = 2**20            # ingest: at most this far past the highest seq already held for the device
# The types this node reads payload fields of: a null payload is as unreadable as a non-object one (#304). Kept equal
# to vocabulary.ENTRY_TYPES by tests/test_envelope.py; a type outside it still lands and projects to nothing.
_OBJECT_PAYLOAD_TYPES = ("capsule", "cell", "comb", "decision", "entity", "entity_fact", "fact", "governance", "idea",
                         "link", "retract")
_STR_FIELDS = ("action", "device_id", "proposal_id", "basis_ts", "label", "principal", "module")   # of a governance payload


def envelope_problem(e, tip=None):
    """Why this entry's envelope cannot be trusted, or None. Pure over the entry; never raises.

    `node_id` a non-empty str, `seq` an int (not a bool) in [1, 2**53), `type` a non-empty str, `payload` an
    object (null only for a type this node does not know), and `prev_hash` / `timestamp` / `sig` str when present (nothing reads an absent one). The governance and name-keyed rows below
    check the few payload fields that readers key a dict on or compare. The type vocabulary is deliberately
    NOT closed: an entry of a type this node does not know still lands and projects to nothing (the
    version-skew rule in vocabulary.py). `tip`, when given, is the highest seq this node holds for the device:
    ingest refuses a seq more than SEQ_JUMP_MAX past it, since every per-device structure is sized by the
    highest seq."""
    if not isinstance(e, dict):
        return "entry is not an object"
    nid, seq = e.get("node_id"), e.get("seq")
    if not isinstance(nid, str) or not nid:
        return "node_id is not a string"
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq < SEQ_MAX:
        return "seq is not an integer in [1, 2**53)"
    if tip is not None and seq > tip + SEQ_JUMP_MAX:
        return "seq is too far past the held tip"
    typ = e.get("type")
    if not isinstance(typ, str) or not typ:
        return "type is not a string"
    for field in ("prev_hash", "timestamp", "sig"):
        if field in e and not isinstance(e[field], str):
            return f"{field} is not a string"
    p = e.get("payload")
    if p is None and typ not in _OBJECT_PAYLOAD_TYPES:
        return None
    if not isinstance(p, dict):
        return "payload is not an object"
    if typ == "governance":
        for field in _STR_FIELDS:
            if p.get(field) is not None and not isinstance(p[field], str):
                return f"governance {field} is not a string"
    elif typ in ("cell", "comb", "capsule"):
        if p.get("name") is not None and not isinstance(p["name"], str):
            return f"{typ} name is not a string"
        v = p.get("version")
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not -2**63 <= v < 2**63):
            return f"{typ} version is not an integer"
    return None


def _canonical(obj):
    """Stable bytes for hashing: THE definition (#153). Every entry hash and prev_hash chain, chunk hash
    and root, device signature and owner signature covers these bytes. `hv._canonical` delegates here and
    `ownerkey` calls it directly. Changing it changes every hash and signature on the fleet, so
    tests/test_one_canonical.py pins the output for a fixed entry and fails on any second copy."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def read_all_entries(journal_dir):
    """Read every new-format entry from all journal files, de-duplicated by
    (node_id, seq) and returned in canonical (node_id, seq) order. When two rows
    share a key, the lexicographically-smallest canonical encoding wins — a stable
    tie-break, so every node picks the SAME representative even in the (unexpected)
    event the duplicates differ in content. Old-format lines and lines whose envelope fails
    `envelope_problem` are skipped (counted by `corrupt_lines`), so a poisoned journal heals on upgrade."""
    journal_dir = Path(journal_dir)
    if not journal_dir.exists():
        return []
    by_key = {}
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if envelope_problem(e) is not None:
                continue
            k = (e["node_id"], e["seq"])
            prev = by_key.get(k)
            if prev is None or _canonical(e) < _canonical(prev):
                by_key[k] = e
    entries = list(by_key.values())
    entries.sort(key=lambda e: (e["node_id"], e["seq"]))
    return entries


def occupied_seqs(journal_dir, node_id):
    """The seqs in [1, SEQ_MAX) that any parseable journal line holds for `node_id`, INCLUDING a line the reader
    skips for a failed envelope (it still occupies its key on a peer that accepts it). Occupied means equal under
    the G-Set key equality after json.loads: 2, 2.0 and True (for 1) are one key, "2" is not. A seq outside the
    range occupies nothing: no peer accepts it."""
    journal_dir = Path(journal_dir)
    held = set()
    if not journal_dir.exists():
        return held
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict) or e.get("node_id") != node_id:
                continue
            seq = e.get("seq")
            if isinstance(seq, str) or not isinstance(seq, (int, float)):
                continue
            if seq == seq and seq not in (float("inf"), float("-inf")) and seq == int(seq) and 1 <= seq < SEQ_MAX:
                held.add(int(seq))
    return held


def corrupt_lines(journal_dir):
    """Count (and sample the files of) non-empty journal lines that read_all_entries SILENTLY skips —
    unparseable JSON or entries whose envelope is malformed (e.g. a truncated/garbled .jsonl from a crash mid-
    write). Surfaced by `hv doctor` so silent data loss becomes visible. Returns (count, [filenames])."""
    journal_dir = Path(journal_dir)
    if not journal_dir.exists():
        return 0, []
    count, sample = 0, []

    def _flag(name):
        nonlocal count
        count += 1
        if name not in sample and len(sample) < 5:
            sample.append(name)

    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                _flag(f.name)
                continue
            if envelope_problem(e) is not None:
                _flag(f.name)
    return count, sample


def chunk_hashes(entries, size=CHUNK_SIZE):
    """SHA-256 over each contiguous chunk of `size` entries (canonical bytes)."""
    hashes = []
    for i in range(0, len(entries), size):
        h = hashlib.sha256()
        for e in entries[i:i + size]:
            h.update(_canonical(e))
        hashes.append("sha256:" + h.hexdigest())
    return hashes


def hash_entries(entries):
    """SHA-256 over the canonical bytes of an ordered list of entries."""
    return "sha256:" + hashlib.sha256(b"".join(_canonical(e) for e in entries)).hexdigest()


def node_max_seq(entries):
    """{node_id: highest seq present} — the per-node high-water marks."""
    out = {}
    for e in entries:
        out[e["node_id"]] = max(out.get(e["node_id"], 0), e["seq"])
    return out


def node_chunk_hashes(entries, size=CHUNK_SIZE):
    """Per-node chunk hashes for delta localization. For each node, chunk k
    covers seq window [k*size+1 .. (k+1)*size]; the hash is SHA-256 over the
    canonical bytes of whatever entries fall in that window (an empty window
    hashes to the empty digest, identically on both peers). Returns
    {node_id: [hash_chunk0, hash_chunk1, ...]}.

    This matches the /sync/chunk?node=X&start&end endpoint and the (node_id,seq)
    dedup key, and tolerates gaps (a missing entry changes its chunk's hash)."""
    by_node = {}
    for e in entries:
        by_node.setdefault(e["node_id"], []).append(e)
    out = {}
    for node, ents in by_node.items():
        ents.sort(key=lambda e: e["seq"])
        nchunks = (ents[-1]["seq"] + size - 1) // size
        buckets = [[] for _ in range(nchunks)]
        for e in ents:
            buckets[(e["seq"] - 1) // size].append(e)
        out[node] = [
            "sha256:" + hashlib.sha256(b"".join(_canonical(e) for e in bucket)).hexdigest()
            for bucket in buckets
        ]
    return out


def entries_in_range(entries, node, start, end):
    """Entries for `node` with start <= seq <= end, sorted by seq."""
    sel = [e for e in entries if e["node_id"] == node and start <= e["seq"] <= end]
    sel.sort(key=lambda e: e["seq"])
    return sel


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
