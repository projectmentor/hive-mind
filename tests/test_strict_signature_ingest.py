"""Strict Ed25519 at the ingest door (#213, RFC 8032 §5.1.7).

`S + l` is a second valid-looking encoding of any signature, so anyone who has seen a signed entry could
re-encode its `sig` without the key. `append_foreign_entries` keeps the first copy of a (node_id, seq), and
`sig` is inside the entry hash and the Merkle chunk hashes, so a node fed the malleated copy first would hold
different bytes from the fleet for good. Before v2.0.3 that copy verified; it is now refused, and the original
that follows lands.
"""
import base64
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owned_hive, _fact, _gov, _owner_key  # noqa: E402

L = 2 ** 252 + 27742317777372353535851937790883648493


def _malleate(b64sig):
    raw = base64.b64decode(b64sig)
    s = int.from_bytes(raw[32:], "little") + L
    return base64.b64encode(raw[:32] + s.to_bytes(32, "little")).decode()


def _journal(hv):
    import merkle
    return {(e["node_id"], e["seq"]): e for e in merkle.read_all_entries(hv.JOURNAL_DIR)}


def test_malleated_entry_is_refused_and_the_original_lands(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    hv.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    _, (dev, *_), base = _owned_hive(hv)
    assert hv.append_foreign_entries(base)[0] == len(base)
    good = _fact(hv, dev, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    bad = dict(good, sig=_malleate(good["sig"]))
    assert hv._verify_entry(good) and not hv._verify_entry(bad)
    assert hv.append_foreign_entries([bad]) == (0, 0)                    # refused, not a duplicate
    assert (dev["id"], 1) not in _journal(hv)
    assert hv.append_foreign_entries([good])[0] == 1                     # the original is not shadowed
    assert _journal(hv)[(dev["id"], 1)]["sig"] == good["sig"]


def test_malleated_entry_does_not_block_the_rest_of_its_batch(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    hv.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    _, (dev, *_), base = _owned_hive(hv)
    assert hv.append_foreign_entries(base)[0] == len(base)
    good = _fact(hv, dev, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    bad = dict(good, sig=_malleate(good["sig"]))
    nxt = _fact(hv, dev, "the rollback finished", "2026-01-03T00:00:00Z")
    assert hv.append_foreign_entries([bad, good, nxt]) == (2, 0)
    journal = _journal(hv)
    assert journal[(dev["id"], good["seq"])]["sig"] == good["sig"]
    assert (dev["id"], nxt["seq"]) in journal


def test_malleated_owner_signature_does_not_verify(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    oseed, opub, oid = _owner_key(hv)
    gov = _gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub,
               "2026-01-01T00:00:00Z", 1)
    assert hv._verify_governance(gov["payload"]) == oid
    gov["payload"]["owner_sig"] = _malleate(gov["payload"]["owner_sig"])
    assert hv._verify_governance(gov["payload"]) is None
