"""Security hardening batch 2 — DoS limits, journal-append atomicity, capsule per-recipient
tolerance, governance memoization, election clock-skew bound, auto device-key, and the new
visibility helpers. Fast unit tests (direct module load) plus one loopback daemon integration."""

import base64
import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import x25519        # noqa: E402
import ed25519       # noqa: E402
import merkle        # noqa: E402


def _load(name, home, monkeypatch, **env):
    """Load a fresh copy of `name` ('hv' or 'hive_sync_daemon') with HIVE_HOME (+ extra env) set first —
    NODE_ID / DEVICE_KEY_PATH are resolved at import, so env must be in place before exec."""
    monkeypatch.setenv("HIVE_HOME", str(home))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    path = str(PROJECT / ("hv" if name == "hv" else f"{name}.py"))
    loader = importlib.machinery.SourceFileLoader(f"{name}_b2_{home.name}", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


# ── P1-6: rate limiter ────────────────────────────────────────────────────────────────────────
def test_rate_limiter_burst_then_deny(tmp_path, monkeypatch):
    sd = _load("hive_sync_daemon", tmp_path, monkeypatch)
    rl = sd._RateLimiter(capacity=3, refill_per_sec=0)   # no refill → deterministic burst test
    assert [rl.allow("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert rl.allow("1.2.3.4") is False                  # bucket drained
    assert rl.allow("9.9.9.9") is True                   # a different peer has its own bucket


# ── P0-1: sync daemon body-size cap (loopback integration) ──────────────────────────────────────
def _free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


def test_oversized_post_rejected_413(tmp_path, monkeypatch):
    sd = _load("hive_sync_daemon", tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "MAX_BODY_BYTES", 100)       # shrink the cap so the test body is tiny
    server, _bind, port = sd.make_server(bind="127.0.0.1", port=_free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        body = json.dumps({"entries": ["x" * 500]}).encode()   # > 100 bytes
        req = urllib.request.Request(f"http://127.0.0.1:{port}/sync/ingest", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        code = None
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 413, f"expected 413 for oversized body, got {code}"
    finally:
        server.shutdown()


# ── P0-2: journal append atomicity under concurrency ────────────────────────────────────────────
def test_append_line_no_interleave_under_concurrency(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)
    target = tmp_path / "journal" / "concurrent.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    THREADS, PER = 16, 40
    big = "P" * 8000                                      # > PIPE_BUF (4096): O_APPEND alone could tear

    def writer(tid):
        for i in range(PER):
            hv._append_line(target, json.dumps({"t": tid, "i": i, "pad": big}))

    ts = [threading.Thread(target=writer, args=(n,)) for n in range(THREADS)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = [ln for ln in target.read_text().splitlines() if ln.strip()]
    assert len(lines) == THREADS * PER
    for ln in lines:
        json.loads(ln)                                   # every line is intact JSON — no interleave


# ── P0-3: capsule build tolerates a bad recipient ──────────────────────────────────────────────
def test_capsule_build_skips_bad_recipient(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)
    seed = os.urandom(32)
    good_cp = x25519.ed_pub_to_curve_pub(ed25519.pub_from_seed(seed))
    good_scalar = x25519.ed_seed_to_curve_scalar(seed)
    recipients = {
        "k1:good": good_cp,
        "k1:wronglen": b"\x00" * 31,                     # not 32 bytes → skipped
        "k1:loworder": b"\x00" * 32,                     # degenerate shared secret → skipped
    }
    cap = hv._capsule_build("TOK", "credential", b"s3cret", recipients, 1, "h1:x", "k1:good")
    sealed = {w["device_id"] for w in cap["wraps"]}
    assert sealed == {"k1:good"}                         # only the valid recipient got a wrap
    assert hv._capsule_open(cap, good_scalar, "k1:good") == b"s3cret"


# ── P1-1: governance memoization is transparent ─────────────────────────────────────────────────
def _owner_corpus(hv, n_admit=2):
    seed = os.urandom(32)
    pub = ed25519.pub_from_seed(seed)
    oid = hv._owner_id_for_pub(pub)

    def govent(payload, seq, ts):
        p = hv._sign_governance_payload(payload, seed, pub)
        return {"node_id": "n", "seq": seq, "type": "governance", "timestamp": ts,
                "payload": p, "prev_hash": "sha256:genesis"}

    entries = [govent({"action": "owner", "owner_id": oid,
                       "owner_pub": base64.b64encode(pub).decode(), "hive_id": "h1:abc"},
                      1, "2026-01-01T00:00:00Z")]
    for i in range(n_admit):
        entries.append(govent({"action": "admit", "device_id": f"k1:dev{i}"}, 2 + i,
                               f"2026-01-0{2+i}T00:00:00Z"))
    return seed, pub, oid, entries, govent


def test_governance_memo_matches_uncached_and_invalidates(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)
    _seed, _pub, _oid, entries, govent = _owner_corpus(hv)
    assert hv._governance_state(entries) == hv._governance_state_uncached(entries)   # byte-identical
    # mutating the returned copy must NOT corrupt the cache
    g = hv._governance_state(entries)
    g["admitted"].add("k1:injected")
    assert "k1:injected" not in hv._governance_state(entries)["admitted"]
    # a new governance entry busts the cache → new admitted set
    before = hv._governance_state(entries)["admitted"]
    entries.append(govent({"action": "admit", "device_id": "k1:late"}, 99, "2026-02-01T00:00:00Z"))
    after = hv._governance_state(entries)["admitted"]
    assert "k1:late" in after and "k1:late" not in before


# ── P1-2: election clock-skew bound (deterministic) ─────────────────────────────────────────────
def _election_corpus(hv, basis_ts, entry_ts):
    """owner + quorum_m=1 + an admitted voter that proposes an election with the given basis_ts on an
    entry stamped entry_ts. Returns the full entries list."""
    oseed = os.urandom(32); opub = ed25519.pub_from_seed(oseed); oid = hv._owner_id_for_pub(opub)
    vseed = os.urandom(32); vpub = ed25519.pub_from_seed(vseed); vid = hv._device_id_for_pub(vpub)
    nseed = os.urandom(32); npub_b64 = base64.b64encode(ed25519.pub_from_seed(nseed)).decode()

    def owner_ent(payload, seq, ts):
        return {"node_id": "owner", "seq": seq, "type": "governance", "timestamp": ts,
                "payload": hv._sign_governance_payload(payload, oseed, opub), "prev_hash": "sha256:genesis"}

    entries = [
        owner_ent({"action": "owner", "owner_id": oid, "owner_pub": base64.b64encode(opub).decode(),
                   "hive_id": "h1:abc"}, 1, "2026-01-01T00:00:00Z"),
        owner_ent({"action": "set-config", "key": "quorum_m", "value": 1}, 2, "2026-01-01T00:00:01Z"),
        owner_ent({"action": "admit", "device_id": vid}, 3, "2026-01-01T00:00:02Z"),
    ]
    pid = hv._election_id(npub_b64, basis_ts)
    prop = {"node_id": vid, "seq": 1, "type": "governance", "timestamp": entry_ts,
            "payload": {"action": "propose-election", "proposal_id": pid,
                        "new_owner_pub": npub_b64, "basis_ts": basis_ts}, "prev_hash": "sha256:genesis"}
    hv._sign_entry(prop, vseed, vpub)                    # device-signed (projection requires it)
    entries.append(prop)
    return entries


def test_future_dated_basis_ts_is_rejected(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)
    # basis_ts far in the future relative to the proposal's OWN entry timestamp → dropped
    future = hv._governance_state(_election_corpus(hv, basis_ts="2099-01-01T00:00:00Z",
                                                   entry_ts="2026-03-01T00:00:00Z"))
    assert future["elections"] == []
    # basis_ts == entry timestamp (legitimate) → the proposal is counted
    normal = hv._governance_state(_election_corpus(hv, basis_ts="2026-03-01T00:00:00Z",
                                                   entry_ts="2026-03-01T00:00:00Z"))
    assert len(normal["elections"]) == 1


# ── P1-4: auto-mint device key ──────────────────────────────────────────────────────────────────
def test_ensure_device_key_mints_on_fresh_node(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)
    assert hv._device_seed() is None
    assert hv._ensure_device_key() is True               # fresh, empty → mints
    assert hv._device_seed() is not None
    assert hv.NODE_ID.startswith("k1:")
    assert hv._ensure_device_key() is False              # idempotent: already has a key


def test_ensure_device_key_respects_node_id_override(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch, HIVE_NODE_ID="forced-id")
    assert hv._ensure_device_key() is False              # override present → never auto-mints
    assert hv._device_seed() is None


# ── P2-1 / P2-3: visibility helpers ─────────────────────────────────────────────────────────────
def test_capsule_version_conflicts_detected(tmp_path, monkeypatch):
    hv = _load("hv", tmp_path, monkeypatch)

    def cap_ent(node, name, ver):
        return {"node_id": node, "seq": ver, "type": "capsule",
                "payload": {"name": name, "version": ver}}
    # same (name, version) from two devices = conflict; same device = not
    entries = [cap_ent("k1:a", "TOK", 2), cap_ent("k1:b", "TOK", 2),
               cap_ent("k1:a", "OTHER", 1), cap_ent("k1:a", "OTHER", 2)]
    assert hv._capsule_version_conflicts(entries) == ["TOK"]


def test_merkle_corrupt_lines_counts_skipped(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir()
    f = jdir / "2026-01-01.jsonl"
    f.write_text(
        json.dumps({"node_id": "n", "seq": 1, "type": "fact", "payload": {}}) + "\n"
        + "{not json at all\n"                            # unparseable
        + json.dumps({"type": "fact"}) + "\n"            # missing node_id/seq
    )
    count, where = merkle.corrupt_lines(jdir)
    assert count == 2 and "2026-01-01.jsonl" in where
    assert len(merkle.read_all_entries(jdir)) == 1       # the good line still loads
