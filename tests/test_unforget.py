"""#46 (contract 1.24): `hv unforget <fact> --reason "<why>"` reverses an owner-forget, and owner forgets
are honoured point-in-time (#46 Q1).

An unforget is an owner-signed `retract` carrying `unretracts_ref` (no `retracts_ref`, so a pre-1.24 node
skips it). The owner acts on one CONTENT form a timeline sorted on (timestamp, node_id, seq), and the
latest honoured act wins. An act is honoured when signed by the owner AS OF its own position
(`_owner_at`); a forget positioned before the genesis owner is grandfathered (#122 tracks that hole), an
unforget never is. Owner acts are governance, not evidence: they never move `last_evidence_at`.

Projection cases are crafted in memory with real signatures (the test_links builders) and projected with
the real `rebuild_db`; the CLI cases drive `hv` in a temp owner hive.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
from test_links import _loadhv, _owner_key, _device, _gov, _entry, _fact, _project, T0  # noqa: E402

FORGET_FLOOR = -1.0
PRE = "2025-12-31T00:00:00Z"                     # before the genesis owner (T0)


def _act(hv, dev, target, ts, owner=None, unforget=False):
    """An owner-source forget (or unforget) of `target`, device-signed by `dev`; `owner=(seed, pub)` signs
    the payload as the owner."""
    key = "unretracts_ref" if unforget else "retracts_ref"
    p = {key: [target["node_id"], target["seq"]], "reason": "r", "source": "owner:owner/owner"}
    return _entry(hv, dev, "retract", p, ts, owner=owner)


def _hive(hv, n=3):
    """Genesis owner A and n admitted devices. Returns (A, devices, base entries)."""
    a_seed, a_pub, a_oid = _owner_key(hv)
    devs = [_device(hv) for _ in range(n)]
    base = [_gov(hv, {"action": "owner", "owner_id": a_oid, "hive_id": "h1"}, a_seed, a_pub, T0, 1)]
    for i, d in enumerate(devs):
        base.append(_gov(hv, {"action": "admit", "device_id": d["id"], "principal": f"p{i}"},
                         a_seed, a_pub, f"2026-01-01T00:00:0{i + 1}Z", i + 2))
    return (a_seed, a_pub, a_oid), devs, base


def _transfer(hv, frm, to_pub, ts, seq):
    return _gov(hv, {"action": "transfer", "new_owner_pub": base64.b64encode(to_pub).decode()},
                frm[0], frm[1], ts, seq)


def _row(conn, content):
    r = conn.execute("SELECT confidence, last_evidence_at FROM facts WHERE content = ?", (content,)).fetchone()
    return r["confidence"], r["last_evidence_at"]


# ── the timeline: latest honoured act wins ─────────────────────────────────────────────────────────

def test_forget_then_unforget_is_live_and_re_derives_from_surviving_evidence(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, d2), base = _hive(hv)
    f = _fact(hv, d0, "the backup runs at 02:00", "2026-01-02T00:00:00Z")
    g = _fact(hv, d1, "the backup runs at 02:00", "2026-01-02T00:00:01Z")
    plain = _row(_project(hv, tmp_path, base + [f, g]), "the backup runs at 02:00")
    forgot = _act(hv, d2, f, "2026-01-03T00:00:00Z", owner=a[:2])
    assert _row(_project(hv, tmp_path, base + [f, g, forgot]), "the backup runs at 02:00")[0] == FORGET_FLOOR
    back = _act(hv, d2, f, "2026-01-04T00:00:00Z", owner=a[:2], unforget=True)
    # confidence AND last_evidence_at equal a hive that never forgot: the owner acts left no trace
    assert _row(_project(hv, tmp_path, base + [f, g, forgot, back]), "the backup runs at 02:00") == plain


def test_unforget_then_forget_stays_forgotten_and_forget_unforget_forget_ends_forgotten(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "port 443 is open", "2026-01-02T00:00:00Z")
    first = _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=a[:2])
    back = _act(hv, d1, f, "2026-01-04T00:00:00Z", owner=a[:2], unforget=True)
    again = _act(hv, d1, f, "2026-01-05T00:00:00Z", owner=a[:2])
    assert _row(_project(hv, tmp_path, base + [f, first, back, again]), "port 443 is open")[0] == FORGET_FLOOR
    early_back = _act(hv, d1, f, "2026-01-02T12:00:00Z", owner=a[:2], unforget=True)   # before the forget
    assert _row(_project(hv, tmp_path, base + [f, early_back, first]), "port 443 is open")[0] == FORGET_FLOOR


def test_unsigned_or_non_owner_unforget_is_ignored(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the api paginates at 100", "2026-01-02T00:00:00Z")
    forgot = _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=a[:2])
    stranger = _owner_key(hv)
    for bad in (_act(hv, d1, f, "2026-01-04T00:00:00Z", unforget=True),                    # unsigned
                _act(hv, d1, f, "2026-01-04T00:00:00Z", owner=stranger[:2], unforget=True)):  # not the owner
        conn = _project(hv, tmp_path, base + [f, forgot, bad])
        assert _row(conn, "the api paginates at 100")[0] == FORGET_FLOOR


def test_a_non_owner_source_carrying_unretracts_ref_does_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "dns is on 10.0.0.2", "2026-01-02T00:00:00Z")
    forgot = _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=a[:2])
    agent = _entry(hv, d1, "retract", {"unretracts_ref": [f["node_id"], f["seq"]], "reason": "r",
                                       "source": "claude-code"}, "2026-01-04T00:00:00Z", owner=a[:2])
    assert _row(_project(hv, tmp_path, base + [f, forgot, agent]), "dns is on 10.0.0.2")[0] == FORGET_FLOOR


# ── point-in-time owner authority (#46 Q1) ────────────────────────────────────────────────────────

def test_a_previous_owners_forget_survives_a_transfer_and_the_new_owner_can_lift_it(tmp_path, monkeypatch):
    """The Q1 resurrection bug: before 1.24 a forget honoured only the CURRENT owner, so a transfer
    brought A's forgets back."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, d2), base = _hive(hv)
    b_seed, b_pub, b_oid = _owner_key(hv)
    f = _fact(hv, d0, "vendor X is approved", "2026-01-02T00:00:00Z")
    forgot = _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=a[:2])
    xfer = _transfer(hv, a, b_pub, "2026-01-04T00:00:00Z", 10)
    entries = base + [f, forgot, xfer]
    assert hv._governance_state(entries)["owner_id"] == b_oid
    assert _row(_project(hv, tmp_path, entries), "vendor X is approved")[0] == FORGET_FLOOR
    back = _act(hv, d2, f, "2026-01-05T00:00:00Z", owner=(b_seed, b_pub), unforget=True)
    assert _row(_project(hv, tmp_path, entries + [back]), "vendor X is approved")[0] != FORGET_FLOOR
    # the retired owner A can no longer forget it again
    late = _act(hv, d1, f, "2026-01-06T00:00:00Z", owner=a[:2])
    assert _row(_project(hv, tmp_path, entries + [back, late]), "vendor X is approved")[0] != FORGET_FLOOR


def test_a_signature_by_the_next_owner_before_their_term_does_not_count(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    b_seed, b_pub, _ = _owner_key(hv)
    f = _fact(hv, d0, "the cache is safe", "2026-01-02T00:00:00Z")
    g = _fact(hv, d0, "the queue is safe", "2026-01-02T00:00:01Z")
    b_forget = _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=(b_seed, b_pub))            # B, before B's term
    a_forget = _act(hv, d1, g, "2026-01-03T00:00:01Z", owner=a[:2])
    b_unforget = _act(hv, d1, g, "2026-01-03T00:00:02Z", owner=(b_seed, b_pub), unforget=True)  # also early
    xfer = _transfer(hv, a, b_pub, "2026-01-04T00:00:00Z", 10)
    conn = _project(hv, tmp_path, base + [f, g, b_forget, a_forget, b_unforget, xfer])
    assert _row(conn, "the cache is safe")[0] != FORGET_FLOOR          # B's early forget ignored
    assert _row(conn, "the queue is safe")[0] == FORGET_FLOOR          # B's early unforget ignored


def test_the_timeline_sorts_on_timestamp_not_on_device_id(tmp_path, monkeypatch):
    """R1: the journal reads in (node_id, seq) order. A forget from A's device and a LATER unforget from B's
    device whose id sorts first must still end live."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, _, _), base = _hive(hv)
    b_seed, b_pub, _ = _owner_key(hv)
    da, db = _device(hv), _device(hv)
    while not db["id"] < da["id"]:
        da, db = _device(hv), _device(hv)
    base += [_gov(hv, {"action": "admit", "device_id": d["id"], "principal": p}, a[0], a[1],
                  f"2026-01-01T00:00:0{7 + i}Z", 7 + i) for i, (d, p) in enumerate(((da, "pa"), (db, "pb")))]
    f = _fact(hv, d0, "the lb drains in 30s", "2026-01-02T00:00:00Z")
    forgot = _act(hv, da, f, "2026-01-03T00:00:00Z", owner=a[:2])
    xfer = _transfer(hv, a, b_pub, "2026-01-04T00:00:00Z", 10)
    back = _act(hv, db, f, "2026-01-05T00:00:00Z", owner=(b_seed, b_pub), unforget=True)
    assert _row(_project(hv, tmp_path, base + [f, forgot, xfer, back]), "the lb drains in 30s")[0] != FORGET_FLOOR


# ── the grandfather (kept by David's ruling; #122) and the legacy shape ───────────────────────────

def test_an_unsigned_forget_before_genesis_stays_honoured_and_an_unforget_is_never_grandfathered(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "legacy fact", "2025-12-30T00:00:00Z")
    legacy = _act(hv, d1, f, PRE)                                       # unsigned, pre-genesis (#122)
    assert _row(_project(hv, tmp_path, base + [f, legacy]), "legacy fact")[0] == FORGET_FLOOR
    for i, early in enumerate((_act(hv, d1, f, "2025-12-31T00:00:01Z", unforget=True),
                               _act(hv, d1, f, "2025-12-31T00:00:02Z", owner=a[:2], unforget=True))):
        assert _row(_project(hv, tmp_path, base + [f, legacy, early]), "legacy fact")[0] == FORGET_FLOOR
    back = _act(hv, d1, f, "2026-01-05T00:00:00Z", owner=a[:2], unforget=True)
    assert _row(_project(hv, tmp_path, base + [f, legacy, back]), "legacy fact")[0] != FORGET_FLOOR


def test_owner_acts_never_move_last_evidence_at(tmp_path, monkeypatch):
    """An ignored forged forget used to refresh the fact's decay clock; an honoured one now leaves it too."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "ntp is pool.ntp.org", "2026-01-02T00:00:00Z")
    forged = _act(hv, d1, f, "2026-01-08T00:00:00Z")                   # unsigned, post-genesis: ignored
    conf, last = _row(_project(hv, tmp_path, base + [f, forged]), "ntp is pool.ntp.org")
    assert conf != FORGET_FLOOR and last == "2026-01-02T00:00:00Z"
    honoured = _act(hv, d1, f, "2026-01-08T00:00:00Z", owner=a[:2])
    assert _row(_project(hv, tmp_path, base + [f, honoured]), "ntp is pool.ntp.org") == (FORGET_FLOOR,
                                                                                                "2026-01-02T00:00:00Z")


def test_forget_and_unforget_act_on_the_content_bucket(tmp_path, monkeypatch):
    """Two journal entries with one text are one fact row: forget by one sid, unforget by the other."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, d2), base = _hive(hv)
    f = _fact(hv, d0, "the vpn uses wireguard", "2026-01-02T00:00:00Z")
    g = _fact(hv, d1, "the vpn uses wireguard", "2026-01-02T00:00:01Z")
    forgot = _act(hv, d2, f, "2026-01-03T00:00:00Z", owner=a[:2])
    back = _act(hv, d2, g, "2026-01-04T00:00:00Z", owner=a[:2], unforget=True)
    conn = _project(hv, tmp_path, base + [f, g, forgot, back])
    assert conn.execute("SELECT count(*) FROM facts WHERE content = 'the vpn uses wireguard'").fetchone()[0] == 1
    assert _row(conn, "the vpn uses wireguard")[0] != FORGET_FLOOR


# ── convergence and version skew ──────────────────────────────────────────────────────────────────

def test_two_nodes_project_identically(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    b_seed, b_pub, _ = _owner_key(hv)
    fs = [_fact(hv, d0, f"fact {i}", f"2026-01-02T00:00:0{i}Z") for i in range(3)]
    acts = [_act(hv, d1, fs[0], "2026-01-03T00:00:00Z", owner=a[:2]),
            _act(hv, d1, fs[1], "2026-01-03T00:00:01Z", owner=a[:2]),
            _transfer(hv, a, b_pub, "2026-01-04T00:00:00Z", 10),
            _act(hv, d1, fs[1], "2026-01-05T00:00:00Z", owner=(b_seed, b_pub), unforget=True),
            _act(hv, d1, fs[2], PRE)]
    rows = []
    for home, order in ((tmp_path / "n1", base + fs + acts), (tmp_path / "n2", list(reversed(base + fs + acts)))):
        conn = _project(_loadhv(home, monkeypatch), home, order)        # a second node, a different arrival order
        rows.append([tuple(r) for r in conn.execute(
            "SELECT content, confidence, contested, last_evidence_at FROM facts ORDER BY content")])
    assert rows[0] == rows[1]
    assert [r[1] == FORGET_FLOOR for r in rows[0]] == [True, False, True]   # fact 0 forgotten, 1 lifted, 2 legacy


def test_a_pre_1_24_node_skips_the_unforget(tmp_path, monkeypatch):
    """The unforget has no `retracts_ref`, the only key a pre-1.24 projection reads, so an older node keeps
    the fact forgotten (projection skew, never divergence: the journal and Merkle are shared)."""
    hv = _loadhv(tmp_path, monkeypatch)
    a, (d0, d1, _), base = _hive(hv)
    f = _fact(hv, d0, "the disk is 2TB", "2026-01-02T00:00:00Z")
    entries = base + [f, _act(hv, d1, f, "2026-01-03T00:00:00Z", owner=a[:2]),
                      _act(hv, d1, f, "2026-01-04T00:00:00Z", owner=a[:2], unforget=True)]
    assert "retracts_ref" not in entries[-1]["payload"]
    old_src = subprocess.run(["git", "-C", str(PROJECT), "show", "v1.23.0:hv"], capture_output=True, text=True)
    if old_src.returncode != 0:
        pytest.skip("v1.23.0 is not in this checkout (shallow clone)")
    old_hv = tmp_path / "hv_1_23"
    old_hv.write_text(old_src.stdout)
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("hv_1_23", str(old_hv))
    spec = importlib.util.spec_from_loader("hv_1_23", loader)
    old = importlib.util.module_from_spec(spec)
    loader.exec_module(old)
    assert old._content_evidence(entries, old._governance_state(entries))["the disk is 2TB"]["forget"] is True
    assert hv._content_evidence(entries, hv._governance_state(entries))["the disk is 2TB"]["forget"] is False
    # and a 1.23 node's ingest LANDS the unforget, so both nodes hold one journal (Merkle converges)
    (tmp_path / "journal").mkdir(exist_ok=True)
    accepted, _dups = old.append_foreign_entries(entries)
    assert accepted == len(entries)


# ── the CLI ───────────────────────────────────────────────────────────────────────────────────────

def _cli(tmp_path):
    def run(*args, check=True):
        env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, r.stderr
        return r

    def journal():
        out = []
        for f in sorted((tmp_path / "journal").glob("*.jsonl")):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out
    return run, journal


def test_cli_forget_unforget_forget(tmp_path):
    run, journal = _cli(tmp_path)
    run("owner", "init")
    sid = run("remember", "the backup runs at 02:00").stdout.split()[2]
    assert "FORGOTTEN (owner)" in run("retract", sid, "--owner", "--reason", "wrong host").stdout
    out = run("unforget", sid, "--reason", "it was right").stdout
    assert f"Fact {sid}" in out and "UNFORGOTTEN (owner)" in out
    u = [e for e in journal() if e["type"] == "retract"][-1]["payload"]
    assert set(u) >= {"unretracts_ref", "reason", "source", "owner_sig", "owner_pub"} and "retracts_ref" not in u
    assert u["source"] == "owner:owner/owner" and u["reason"] == "it was right"
    assert "FORGOTTEN (owner)" in run("retract", sid, "--owner", "--reason", "gone after all").stdout
    run("doctor", "rebuild")
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT confidence FROM facts").fetchone()[0] == FORGET_FLOOR


def test_cli_refusals_write_nothing(tmp_path):
    run, journal = _cli(tmp_path)
    run("owner", "init")
    sid = run("remember", "the api paginates at 100").stdout.split()[2]
    dsid = run("decide", "page at 100", "--rationale", "r").stdout.split()[2]
    n = len(journal())
    cases = [(("unforget", sid, "--reason", "r"), 1, "is not forgotten"),
             (("unforget", dsid, "--reason", "r"), 1, "is a decision, not a fact"),
             (("unforget", sid), 2, "--reason")]
    for args, code, msg in cases:
        r = run(*args, check=False)
        assert r.returncode == code and msg in r.stderr, (args, r.stderr)
    run("retract", sid, "--owner", "--reason", "x")
    n = len(journal())
    key = tmp_path / ".owner-key"
    real = key.read_text()
    key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")            # someone else's owner key
    r = run("unforget", sid, "--reason", "r", check=False)
    assert r.returncode == 1 and "not the hive's current owner" in r.stderr
    key.unlink()                                                                # a member device: no key
    r = run("unforget", sid, "--reason", "r", check=False)
    assert r.returncode == 1 and "needs the owner key" in r.stderr
    assert len(journal()) == n
    key.write_text(real)
