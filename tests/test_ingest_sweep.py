"""Admission across the single ingest sweep: a device's content waits for an admit later in its batch (#229)."""
import sys
from pathlib import Path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_genesis_pin import _loadhv, _owner_key, _device_key, _owner_act, _gov, _fact, HIVE_ID  # noqa
import pytest

T = "2026-01-01T00:00:%02d.000+00:00"

def _held(hv):
    return {(e["node_id"], e["seq"]) for e in hv.merkle.read_all_entries(hv.JOURNAL_DIR)}

def _genesis_batch(hv):
    """Owner device O wrote content before declaring the owner (as the live hive's did: 238 entries below
    genesis), then owner, admit O, admit D. D wrote one fact."""
    real = _owner_key(hv)
    o = _device_key(hv); d = _device_key(hv)
    O, D = o[2], d[2]
    pre = _fact(hv, O, T % 1, "pre-owner content on the owner's device", seq=1, dev=(o[0], o[1]))
    gen = _owner_act(hv, real, T % 2, seq=2, nid=O, dev=(o[0], o[1]))
    aO = _gov(hv, {"action": "admit", "device_id": O, "principal": "p"}, real[0], real[1], T % 3, 3, O, (o[0], o[1]))
    aD = _gov(hv, {"action": "admit", "device_id": D, "principal": "q"}, real[0], real[1], T % 4, 4, O, (o[0], o[1]))
    dfact = _fact(hv, D, T % 5, "content from D", seq=1, dev=(d[0], d[1]))
    return real, gen, [pre, gen, aO, aD, dfact], O, D

def test_pinned_joiner_first_sync_lands_admitted_content(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    real, gen, batch, O, D = _genesis_batch(hv)
    fp8 = hv.compute_hash(gen).split(":", 1)[-1][:8]
    hv._write_genesis_pin({"hive_id": HIVE_ID, "owner_id": real[2], "genesis_hash8": fp8})
    hv.append_foreign_entries(batch, advertised_chunks=None)
    assert _held(hv) == {(O, 1), (O, 2), (O, 3), (O, 4), (D, 1)}

def test_unadmitted_owner_key_holder_admit_is_stored_but_not_honoured(tmp_path, monkeypatch):
    """X (not admitted) carries an owner-signed admit of Y at seq 2 after its own content at seq 1. Ingest stores the
    act, and the projection does not honour it (SECREV A3: an owner-signed act counts only from a member), so Y's content
    does not land, in one batch or split."""
    hv = _loadhv(tmp_path, monkeypatch)
    real = _owner_key(hv)
    o = _device_key(hv); x = _device_key(hv); y = _device_key(hv)
    O = o[2]
    gen = _owner_act(hv, real, T % 1, seq=1, nid=O, dev=(o[0], o[1]))
    aO = _gov(hv, {"action": "admit", "device_id": O, "principal": "p"}, real[0], real[1], T % 2, 2, O, (o[0], o[1]))
    hv._write_genesis_pin(hv._genesis_pin_for(gen))
    (hv.JOURNAL_DIR / "2026-01-01.jsonl").write_text("\n".join(__import__("json").dumps(e) for e in (gen, aO)) + "\n")
    x1 = _fact(hv, x[2], T % 3, "x content", seq=1, dev=(x[0], x[1]))
    x2 = _gov(hv, {"action": "admit", "device_id": y[2], "principal": "q"}, real[0], real[1], T % 4, 2, x[2], (x[0], x[1]))
    y1 = _fact(hv, y[2], T % 5, "y content", seq=1, dev=(y[0], y[1]))
    hv.append_foreign_entries([x1, x2, y1])
    held = _held(hv)
    assert (x[2], 2) in held            # stored: the journals converge
    assert (y[2], 1) not in held        # but Y was never admitted by a member
    assert y[2] not in hv._governance_state(hv.merkle.read_all_entries(hv.JOURNAL_DIR))["admitted"]

@pytest.mark.parametrize("stranger_first", [True, False])
def test_unpinned_pre_owner_node_refuses_a_stranger_whatever_its_id(tmp_path, monkeypatch, stranger_first):
    hv = _loadhv(tmp_path, monkeypatch)
    for _ in range(200):
        real = _owner_key(hv); o = _device_key(hv); s = _device_key(hv)
        if (s[2] < o[2]) == stranger_first:
            break
    O, S = o[2], s[2]
    gen = _owner_act(hv, real, T % 1, seq=1, nid=O, dev=(o[0], o[1]))
    aO = _gov(hv, {"action": "admit", "device_id": O, "principal": "p"}, real[0], real[1], T % 2, 2, O, (o[0], o[1]))
    sf = _fact(hv, S, T % 5, "stranger", seq=1, dev=(s[0], s[1]))
    hv.append_foreign_entries([gen, aO, sf])
    assert (S, 1) not in _held(hv)
