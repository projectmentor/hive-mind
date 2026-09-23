"""#70: performance tripwires, and the safety properties of the verification memo and the store marker.

The budgets are deterministic where they can be (verification counts, read from `_VERIFY_STATS`) and
generous where they can't (wall clock on a noisy CI runner): their job is to catch a return to minutes,
not to benchmark. The journal is synthetic but genuinely signed: 1,000 entries from three admitted devices.
"""
import copy
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"
sys.path.insert(0, str(PROJECT))

N_ENTRIES = 1000


def _loadhv(home, mp, name="hvmod_perf"):
    """A fresh in-process `hv` (fresh memo and caches) bound to HIVE_HOME=home."""
    mp.setenv("HIVE_HOME", str(home))
    mp.setenv("HIVE_NOW", "2026-03-01T00:00:00Z")
    loader = importlib.machinery.SourceFileLoader(name, str(HV))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _write_journal(home, entries):
    jd = Path(home) / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    for f in jd.glob("*.jsonl"):
        f.unlink()
    (jd / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _ts(i):
    return f"2026-01-{1 + i // 86400:02d}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"


class _Hive:
    """Signs entries the way `hv` does: device-signed; governance also owner-signed."""

    def __init__(self, hv, n_devices=3):
        self.hv = hv
        self.oseed = os.urandom(32)
        self.opub = hv._ed25519.pub_from_seed(self.oseed)
        self.oid = hv._owner_id_for_pub(self.opub)
        self.devs = []
        for _ in range(n_devices):
            seed = os.urandom(32)
            pub = hv._ed25519.pub_from_seed(seed)
            self.devs.append({"seed": seed, "pub": pub, "id": hv._device_id_for_pub(pub), "seq": 0})
        self.gseq = 0
        self.base = [self.gov({"action": "owner", "owner_id": self.oid, "hive_id": "h1"}, _ts(0))]
        for i, d in enumerate(self.devs):
            self.base.append(self.gov({"action": "admit", "device_id": d["id"], "principal": f"p{i}"}, _ts(1 + i)))

    def gov(self, payload, ts):
        self.gseq += 1
        p = self.hv._sign_governance_payload(payload, self.oseed, self.opub)
        return {"node_id": "ownerdev", "seq": self.gseq, "type": "governance", "timestamp": ts, "payload": p}

    def entry(self, dev, typ, payload, ts):
        dev["seq"] += 1
        e = {"node_id": dev["id"], "seq": dev["seq"], "type": typ, "timestamp": ts, "payload": payload}
        return self.hv._sign_entry(e, dev["seed"], dev["pub"])


def _synthetic(hv, n=N_ENTRIES):
    """Facts (some corroborated across devices), decisions, ideas and links of every evidence kind."""
    h = _Hive(hv)
    out = list(h.base)
    facts, decisions, ideas = [], [], []
    i = 0
    while len(out) < n:
        i += 1
        d = h.devs[i % 3]
        ts = _ts(100 + i)
        kind = i % 20
        if kind < 10 or not facts:
            content = f"fact about topic {i % 37} number {i}" if kind != 3 or len(facts) < 5 else facts[-5]["payload"]["content"]
            e = h.entry(d, "fact", {"content": content, "tags": [f"t{i % 11}", "perf"], "importance": 0.5,
                                    "source": "manual"}, ts)
            facts.append(e)
        elif kind < 13:
            e = h.entry(d, "decision", {"content": f"decision {i}", "rationale": "r", "source": "manual",
                                        "tags": ["perf"]}, ts)
            decisions.append(e)
        elif kind < 15:
            e = h.entry(d, "idea", {"content": f"idea {i}", "tags": ["perf"], "source": "manual",
                                    "channel": "introspect"}, ts)
            ideas.append(e)
        else:
            src = facts[-1]
            if kind == 15 and ideas:
                tgt, lk, data = ideas[-1], "supports", {}
            elif kind == 16 and decisions:
                tgt, lk, data = decisions[-1], "outcome-of", {"polarity": 1}
            elif kind == 17 and decisions:
                src, tgt, lk, data = decisions[-1], facts[-3] if len(facts) > 3 else facts[0], "informed", {}
            elif kind == 18:
                tgt, lk, data = facts[len(facts) // 2], "contradicts", {}
            else:
                tgt, lk, data = facts[len(facts) // 3], "supports", {}
            e = h.entry(d, "link", {"kind": lk, "from_ref": [src["node_id"], src["seq"]],
                                    "to_ref": [tgt["node_id"], tgt["seq"]], "data": data, "source": "manual"}, ts)
        out.append(e)
    return h, out


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    home = tmp_path_factory.mktemp("synth-build")
    with pytest.MonkeyPatch.context() as mp:
        hv = _loadhv(home, mp, "hvmod_perf_build")
        h, entries = _synthetic(hv)
    return h, entries


def _signed_items(entries):
    """Distinct signatures a projection could verify: entry signatures and owner signatures."""
    sigs = {e["sig"] for e in entries if "sig" in e}
    sigs |= {e["payload"]["owner_sig"] for e in entries if "owner_sig" in (e.get("payload") or {})}
    return len(sigs)


# ── deterministic budgets ─────────────────────────────────────────────────────────────────────────────

def test_rebuild_verifies_each_signature_at_most_once_and_a_second_pass_verifies_nothing(
        synthetic, tmp_path, monkeypatch):
    _h, entries = synthetic
    hv = _loadhv(tmp_path, monkeypatch)
    _write_journal(tmp_path, entries)
    hv.init_db()
    hv.rebuild_db()
    first = hv._VERIFY_STATS["misses"]
    assert 0 < first <= _signed_items(entries)
    hv._GOV_CACHE.clear()                                    # count at the wrapper, below every cache
    hv.rebuild_db()
    assert hv._VERIFY_STATS["misses"] == first               # zero new verifications
    assert hv._VERIFY_STATS["hits"] > 0


# ── wall-clock tripwires (generous on purpose) ────────────────────────────────────────────────────────

def _cli(home, *args, stdin="", env_extra=None):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_NOW="2026-03-01T00:00:00Z", **(env_extra or {}))
    t = time.monotonic()
    r = subprocess.run([sys.executable, str(HV), *args], env=env, input=stdin, capture_output=True, text=True)
    return r, time.monotonic() - t


def test_rebuild_and_adapter_paths_fit_their_budgets(synthetic, tmp_path, monkeypatch):
    _h, entries = synthetic
    hv = _loadhv(tmp_path, monkeypatch)
    _write_journal(tmp_path, entries)
    hv.init_db()
    t = time.monotonic()
    hv.rebuild_db()
    assert time.monotonic() - t < 10.0, "full rebuild of 1,000 signed entries"
    r, dt = _cli(tmp_path, "search", "topic")
    assert r.returncode == 0 and dt < 2.0, f"hv search took {dt:.1f}s"
    # the adapters' own timeouts: the Claude Code digest runs under `timeout 8`, Hermes 10 s, MCP 30 s
    r, dt = _cli(tmp_path, "nudge", "--event", "session-start", "--session", "s1", "--cwd", str(tmp_path),
                 "--agent", "claude-code")
    assert r.returncode == 0 and dt < 8.0, f"session-start digest took {dt:.1f}s"
    r, dt = _cli(tmp_path, "search", "topic", "--format", "json")
    assert r.returncode == 0 and dt < 10.0, f"hv search --format json took {dt:.1f}s"
    r, dt = _cli(tmp_path, "remember", "a new observation for the perf hive", "--tags", "perf")
    assert r.returncode == 0 and dt < 10.0, f"hv remember took {dt:.1f}s"


# ── memo safety: it caches the cryptographic check only, never authorization ─────────────────────────

def test_a_tampered_entry_misses_the_memo_and_is_rejected(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    h = _Hive(hv, n_devices=1)
    e = h.entry(h.devs[0], "fact", {"content": "the build is green", "tags": [], "source": "manual"}, _ts(5))
    assert hv._verify_entry(e)
    misses = hv._VERIFY_STATS["misses"]
    assert hv._verify_entry(copy.deepcopy(e))                     # same bytes: a hit
    assert hv._VERIFY_STATS["misses"] == misses
    bad = copy.deepcopy(e)
    bad["payload"]["content"] = "the build is red"                # one field changed, same sig
    assert not hv._verify_entry(bad)
    assert hv._VERIFY_STATS["misses"] == misses + 1               # a full check, not a cached answer


def test_node_id_binding_is_checked_even_when_the_signature_is_memoized(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    h = _Hive(hv, n_devices=2)
    victim, attacker = h.devs
    e = {"node_id": victim["id"], "seq": 1, "type": "fact", "timestamp": _ts(5),
         "payload": {"content": "planted", "tags": [], "source": "manual"}}
    e = hv._sign_entry(e, attacker["seed"], attacker["pub"])      # attacker's key, victim's node_id
    import base64
    assert hv._verify_sig(hv._entry_signing_bytes(e), base64.b64decode(e["sig"]), base64.b64decode(e["pub"]))
    assert not hv._verify_entry(e)                                # the binding still fails


def test_a_revoked_device_stops_counting_in_the_same_process(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    h = _Hive(hv, n_devices=2)
    a, b = h.devs
    content = "the vendor rotates keys every 90 days"
    fa = h.entry(a, "fact", {"content": content, "tags": [], "source": "manual"}, _ts(10))
    fb = h.entry(b, "fact", {"content": content, "tags": [], "source": "manual"}, _ts(11))
    sup = h.entry(b, "link", {"kind": "supports", "from_ref": [fb["node_id"], fb["seq"]],
                              "to_ref": [fa["node_id"], fa["seq"]], "data": {}, "source": "manual"}, _ts(12))
    before = hv._content_confidence(hv._content_evidence(h.base + [fa, fb, sup],
                                                         hv._governance_state(h.base + [fa, fb, sup]))[content],
                                    hv._governance_state(h.base + [fa, fb, sup]))
    hits = hv._VERIFY_STATS["hits"]
    revoked = h.base + [h.gov({"action": "revoke", "device_id": b["id"]}, _ts(13)), fa, fb, sup]
    gov = hv._governance_state(revoked)
    after = hv._content_confidence(hv._content_evidence(revoked, gov)[content], gov)
    assert after < before                                          # b no longer counts
    assert hv._VERIFY_STATS["hits"] > hits                         # while its signatures came from the memo


def test_the_memo_is_bounded_and_never_caches_a_rejection(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    monkeypatch.setattr(hv, "_VERIFY_MEMO_MAX", 5)
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    for i in range(10):
        m = b"message %d" % i
        assert hv._verify_sig(m, hv._ed25519.sign(m, seed), pub)
    assert len(hv._VERIFY_MEMO) == 5
    bad = bytes(64)
    misses = hv._VERIFY_STATS["misses"]
    assert not hv._verify_sig(b"x", bad, pub) and not hv._verify_sig(b"x", bad, pub)
    assert hv._VERIFY_STATS["misses"] == misses + 2 and len(hv._VERIFY_MEMO) == 5


# ── the store marker and the locked-store write path ─────────────────────────────────────────────────

def test_rebuild_marks_the_store_and_an_out_of_band_append_is_caught_up(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    h = _Hive(hv, n_devices=1)
    f1 = h.entry(h.devs[0], "fact", {"content": "first", "tags": [], "source": "manual"}, _ts(10))
    _write_journal(tmp_path, h.base + [f1])
    hv.init_db()
    hv.rebuild_db()
    assert hv._ensure_store_current() is False                     # current: nothing to do
    f2 = h.entry(h.devs[0], "fact", {"content": "second", "tags": [], "source": "manual"}, _ts(11))
    _write_journal(tmp_path, h.base + [f1, f2])                    # an append the store never saw
    assert hv._ensure_store_current() is True
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT count(*) FROM facts WHERE content = 'second'").fetchone()[0] == 1
    conn.close()


def test_concurrent_remembers_both_succeed(tmp_path):
    _cli(tmp_path, "stats")
    env = dict(os.environ, HIVE_HOME=str(tmp_path))
    procs = [subprocess.Popen([sys.executable, str(HV), "remember", f"parallel fact {i}"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT count(*) FROM facts WHERE content LIKE 'parallel fact %'").fetchone()[0] == 2
    conn.close()


def test_a_locked_store_after_the_append_is_reported_as_journaled_then_caught_up(tmp_path):
    _cli(tmp_path, "remember", "an earlier fact")                  # store exists and is current
    lock = sqlite3.connect(tmp_path / "store.db", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")                                # hold the write lock
    try:
        r, _dt = _cli(tmp_path, "remember", "written while the store is locked",
                      env_extra={"HIVE_BUSY_TIMEOUT_MS": "300"})
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert r.returncode == 0, r.stderr                             # success: the journal has it
    assert "journaled; the local store is busy" in r.stdout
    journal = [json.loads(line) for f in (tmp_path / "journal").glob("*.jsonl")
               for line in f.read_text().splitlines() if line.strip()]
    assert sum(1 for e in journal if (e.get("payload") or {}).get("content") == "written while the store is locked") == 1
    conn = sqlite3.connect(tmp_path / "store.db")
    assert conn.execute("SELECT count(*) FROM facts WHERE content = 'written while the store is locked'").fetchone()[0] == 0
    conn.close()
    r, _dt = _cli(tmp_path, "search", "locked")                    # the next command catches up
    assert "written while the store is locked" in r.stdout


# ── the rest of the write paths, and a read under a locked catch-up (review of #87) ──────────────────

import re  # noqa: E402


def _journal(tmp_path):
    return [json.loads(line) for f in (tmp_path / "journal").glob("*.jsonl")
            for line in f.read_text().splitlines() if line.strip()]


def test_retract_and_entity_under_a_locked_store_are_journaled_then_caught_up(tmp_path):
    r, _dt = _cli(tmp_path, "remember", "the build uses make")
    sid = re.search(r"h:[0-9a-f]{10}", r.stdout).group(0)
    busy = {"HIVE_BUSY_TIMEOUT_MS": "300"}
    lock = sqlite3.connect(tmp_path / "store.db", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        r1, _ = _cli(tmp_path, "retract", sid, env_extra=busy)
        r2, _ = _cli(tmp_path, "entity", "add", "--name", "deploy-pipeline", env_extra=busy)
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    for r in (r1, r2):
        assert r.returncode == 0, r.stderr                 # journaled: success, never a retry-inviting failure
        assert "journaled; the local store is busy" in r.stdout
    j = _journal(tmp_path)
    assert sum(1 for e in j if e["type"] == "retract") == 1
    assert sum(1 for e in j if e["type"] == "entity" and e["payload"].get("name") == "deploy-pipeline") == 1
    r, _ = _cli(tmp_path, "entity", "list")                 # the next store command catches up
    assert "deploy-pipeline" in r.stdout


def test_retract_and_entity_advance_the_marker(tmp_path, monkeypatch):
    r, _dt = _cli(tmp_path, "remember", "the cache is warm at boot")
    sid = re.search(r"h:[0-9a-f]{10}", r.stdout).group(0)
    _cli(tmp_path, "retract", sid)
    _cli(tmp_path, "entity", "add", "--name", "cache")
    _cli(tmp_path, "entity", "link", "--name", "cache", "--fact-id", sid)
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._ensure_store_current() is False              # current: no redundant rebuild on the next command


def test_a_read_survives_a_locked_catch_up(tmp_path):
    _cli(tmp_path, "remember", "first fact about caching")  # the store is current
    with open(next((tmp_path / "journal").glob("*.jsonl")), "a") as f:   # an append the store never saw
        f.write(json.dumps({"node_id": "k1:oobnode00000000", "seq": 1, "type": "fact",
                            "timestamp": "2026-02-01T00:00:00Z",
                            "payload": {"content": "second fact about caching", "tags": [],
                                        "source": "manual"}}) + "\n")
    lock = sqlite3.connect(tmp_path / "store.db", isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        r, _ = _cli(tmp_path, "search", "caching", env_extra={"HIVE_BUSY_TIMEOUT_MS": "300"})
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert r.returncode == 0 and "could not catch up" in r.stderr   # warned, not a traceback
    assert "first fact about caching" in r.stdout                      # answered from the store as it is
    r, _ = _cli(tmp_path, "search", "caching")                         # lock gone: it catches up
    assert "second fact about caching" in r.stdout
