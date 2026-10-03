"""2.1 plan PR 3 (A2): module identity. A module has its own device key, and the owner's `admit` act carries
`module: <name>` to mark it. The election walk then leaves a marked device out: it neither proposes nor votes,
under either `quorum_by`. The mark is the owner's (an `admit` is owner-signed), never the device's own claim.
Drives the real `hv` / `hive-mind` CLIs the way tests/test_succession.py does."""

import stat
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import _keys  # noqa: E402
import _planes  # noqa: E402
from test_succession import (_run, _gov, _device_id, _merge_into, _election_pids,  # noqa: E402
                             _propose_minted)

BASIS = "2026-08-15T00:00:00.000+00:00"        # ~45 days after the admits: the dead-man switch is armed
NOW = "2026-07-01T00:00:00.000+00:00"


def _setup(tmp_path, module_c=None, quorum_m=2, quorum_by=None):
    a, b, c = tmp_path / "A", tmp_path / "B", tmp_path / "C"
    for h in (a, b, c):
        _run(h, "config", "identity", "init")
    _run(a, "owner", "init", now=NOW)
    _run(a, "group", "admit", _device_id(b), "--principal", "op", now=NOW)
    extra = ["--module", module_c] if module_c else []
    _run(a, "group", "admit", _device_id(c), "--principal", "op", *extra, now=NOW)
    _run(a, "config", "quorum", "set", "quorum_m", str(quorum_m), now=NOW)
    _run(a, "config", "quorum", "set", "dead_man_days", "30", now=NOW)
    if quorum_by:
        _run(a, "config", "quorum", "set", "quorum_by", quorum_by, now=NOW)
    return a, b, c


def test_an_admit_with_a_module_marks_the_device_and_a_plain_one_does_not(tmp_path):
    a, b, c = _setup(tmp_path, module_c="hwatch")
    _, gov = _gov(a)
    assert gov["modules"] == {_device_id(c): "hwatch"}
    assert _device_id(b) in gov["admitted"] and _device_id(c) in gov["admitted"]
    assert gov["principals"][_device_id(c)] == "op"          # the operator's principal: cap_self still bounds it


def test_a_later_admit_without_the_module_clears_the_mark(tmp_path):
    a, b, c = _setup(tmp_path, module_c="hwatch")
    _run(a, "group", "admit", _device_id(c), "--principal", "op", now=NOW)
    _, gov = _gov(a)
    assert gov["modules"] == {}


def test_a_bad_module_name_is_refused_and_writes_nothing(tmp_path):
    a, b, c = _setup(tmp_path)
    before = sorted(p.read_text() for p in (a / "journal").glob("*.jsonl"))
    r = _run(a, "group", "admit", _device_id(c), "--module", "Bad Name", now=NOW, check=False)
    assert "not a valid module name" in r.stdout
    assert sorted(p.read_text() for p in (a / "journal").glob("*.jsonl")) == before
    assert _gov(a)[1]["modules"] == {}


def test_a_device_cannot_mark_a_device_as_a_module_by_itself(tmp_path, monkeypatch):
    """The mark rides on an owner-signed `admit`. A device-signed governance entry saying the same projects to
    nothing: it cannot exclude another device from a vote, nor clear its own mark."""
    import test_links as TL
    hv = TL._loadhv(tmp_path, monkeypatch)
    _, (d1, d2, _d3), base = TL._owned_hive(hv)
    forged = TL._entry(hv, d1, "governance", {"action": "admit", "device_id": d2["id"], "module": "hwatch"},
                       "2026-01-05T00:00:00Z")
    conn = TL._project(hv, tmp_path, base + [forged])
    conn.close()
    import merkle
    gov = hv._governance_state(merkle.read_all_entries(str(tmp_path / "journal")))
    assert gov["modules"] == {}


@pytest.mark.parametrize("quorum_by", [None, "principal"])
def test_a_module_device_vote_does_not_count_toward_quorum(tmp_path, quorum_by):
    # device mode: B (operator) proposes, C (module) votes: 1 voter of 2 -> not installed. Under `principal`
    # B and C share one unit already, so the vote could never have added a second; same outcome.
    a, b, c = _setup(tmp_path, module_c="hwatch", quorum_by=quorum_by)
    _, gov0 = _gov(a)
    _merge_into(b, a)
    _propose_minted(b, now=BASIS)
    pid = _election_pids(b)[0]
    _merge_into(c, a, b)
    _run(c, "owner", "vote", pid, now=BASIS)
    _merge_into(a, b, c)
    _, gov = _gov(a)
    assert gov["owner_id"] == gov0["owner_id"] and gov["owner_term"] == 0
    assert all(e["votes"] == 1 for e in gov["elections"])


def test_the_same_vote_counts_when_the_device_is_not_a_module(tmp_path):
    a, b, c = _setup(tmp_path, module_c=None)
    _, gov0 = _gov(a)
    _merge_into(b, a)
    _propose_minted(b, now=BASIS)
    pid = _election_pids(b)[0]
    _merge_into(c, a, b)
    _run(c, "owner", "vote", pid, now=BASIS)
    _merge_into(a, b, c)
    _, gov = _gov(a)
    assert gov["owner_id"] != gov0["owner_id"] and gov["owner_term"] == 1     # control: it installs


def test_a_module_device_cannot_propose_an_election(tmp_path):
    a, b, c = _setup(tmp_path, module_c="hwatch")
    _merge_into(c, a)
    _propose_minted(c, now=BASIS)
    _merge_into(a, b, c)
    _, gov = _gov(a)
    assert gov["elections"] == []


# ── the key mint helper (control plane) ───────────────────────────────────────────────────────────────

def test_a_module_key_is_minted_on_the_control_plane_only(tmp_path):
    hv = _planes.load_hv(tmp_path, "hvmod_mi_dp", control_plane=False)
    assert not hasattr(hv, "mint_module_key")
    hv = _planes.load_hv(tmp_path, "hvmod_mi_cp")
    assert callable(hv.mint_module_key)


def test_mint_module_key_writes_a_private_key_and_returns_its_identity(tmp_path):
    import base64
    hv = _planes.load_hv(tmp_path, "hvmod_mi_mint")
    did, pub_b64 = hv.mint_module_key("hwatch")
    path = _keys.key_dir(tmp_path) / "modules" / "hwatch" / "device-key"
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    seed = base64.b64decode(path.read_text().strip())
    assert len(seed) == 32 and hv._ed25519.pub_from_seed(seed) == base64.b64decode(pub_b64)
    assert did == hv._device_id_for_pub(base64.b64decode(pub_b64)) and did != hv.NODE_ID
    # it is a device key, not this device's own: the host's key is untouched
    assert not (_keys.key_dir(tmp_path) / "device-key").exists() or \
        (_keys.key_dir(tmp_path) / "device-key").read_text() != path.read_text()


def test_mint_module_key_refuses_to_replace_a_key_or_take_a_bad_name(tmp_path):
    hv = _planes.load_hv(tmp_path, "hvmod_mi_refuse")
    hv.mint_module_key("hwatch")
    path = _keys.key_dir(tmp_path) / "modules" / "hwatch" / "device-key"
    first = path.read_text()
    with pytest.raises(FileExistsError):
        hv.mint_module_key("hwatch")
    assert path.read_text() == first
    for bad in ("", "Bad", "../x", "a/b", "x-hw:k", None, 7):
        with pytest.raises(ValueError):
            hv.mint_module_key(bad)
    assert sorted(p.name for p in (_keys.key_dir(tmp_path) / "modules").iterdir()) == ["hwatch"]
