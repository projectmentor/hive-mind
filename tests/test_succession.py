"""Owner resilience (Effort 1). Phase 1: owner-key backup/restore + standby declaration.
Drives the real `hv` CLI against an isolated temp HIVE_HOME (mirrors tests/test_governance.py),
and loads `hv` as a module to exercise the stdlib seal/unseal crypto directly."""

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import subprocess  # noqa: E402


def _run(home, *args, node_id=None, passphrase=None, now=None, check=True):
    # HIVE_IDENTITY_STASH is pinned UNDER the temp home so `owner init/import/restore/claim` never
    # touch the real ~/.config/hive-mind/identity/.owner-key backup on the developer's machine.
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    if passphrase is not None:
        env["HIVE_OWNER_PASSPHRASE"] = passphrase     # non-interactive passphrase for tests
    if now is not None:
        env["HIVE_NOW"] = now                         # dates governance entries (dead-man test clock)
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env,
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _gov(home):
    loader = importlib.machinery.SourceFileLoader("hvmod_succ", str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("hvmod_succ", loader))
    loader.exec_module(m)
    import merkle
    return m, m._governance_state(merkle.read_all_entries(str(home / "journal")))


def _owner_id(home):
    return _run(home, "owner", "show").stdout.splitlines()[0].split()[1]


def _sync(src, dst):
    """Model a hive sync between two device homes: union both journals by (node_id, seq) into dst,
    then rebuild. Mirrors the G-Set merge the real sync layer performs."""
    import json
    from collections import defaultdict
    dst_j = dst / "journal"
    dst_j.mkdir(parents=True, exist_ok=True)
    seen = {}
    for home in (dst, src):                                  # dst first; src adds anything new
        jd = home / "journal"
        if not jd.exists():
            continue
        for f in sorted(jd.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                seen[(e.get("node_id"), e.get("seq"))] = (e.get("timestamp", ""), line)
    byday = defaultdict(list)
    for ts, line in seen.values():
        byday[ts[:10]].append(line)
    for day, lines in byday.items():
        (dst_j / f"{day}.jsonl").write_text("\n".join(lines) + "\n")
    _run(dst, "rebuild")


def _claim_mint_pub(home, node_id):
    """Run `owner claim --mint` on a fresh successor home; return the printed prospective owner pub."""
    out = _run(home, "owner", "claim", "--mint", node_id=node_id).stdout
    for line in out.splitlines():
        if line.strip().startswith("pub:"):
            return line.split("pub:", 1)[1].strip()
    raise AssertionError(f"no pub printed by claim --mint:\n{out}")


def test_export_import_round_trip_resumes_same_owner(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    oid = _owner_id(home)
    keyfile = tmp_path / "owner.key"
    _run(home, "owner", "export", "--out", str(keyfile))
    assert keyfile.exists()
    (home / ".owner-key").unlink()                          # lose the owner device's key
    assert "does NOT hold the owner key" in _run(home, "owner", "show").stdout
    _run(home, "owner", "import", str(keyfile))             # restore on (this stand-in for) a new device
    show = _run(home, "owner", "show").stdout
    assert "holds the owner key" in show and oid in show    # same owner_id resumes


def test_import_mismatch_is_refused(tmp_path):
    import base64
    import json
    home = tmp_path
    _run(home, "owner", "init")
    bad = tmp_path / "bad.key"
    bad.write_text(json.dumps({"enc": "none", "owner_id": "o1:deadbeef",
                               "seed": base64.b64encode(os.urandom(32)).decode()}))
    out = _run(home, "owner", "import", str(bad), check=False).stdout
    assert "Refusing" in out
    out2 = _run(home, "owner", "import", str(bad), "--force", check=False).stdout
    assert "installed" in out2.lower()                      # --force overrides


def test_passphrase_seal_unseal_roundtrip_and_wrong_pass():
    loader = importlib.machinery.SourceFileLoader("hvmod_crypto", str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("hvmod_crypto", loader))
    loader.exec_module(m)
    seed = os.urandom(32)
    env = m._owner_seal(seed, "correct horse")
    assert env["enc"] == "scrypt-ctr-hmac-v1"
    assert m._owner_unseal(env, "correct horse") == seed
    try:
        m._owner_unseal(env, "wrong")
        assert False, "wrong passphrase should raise"
    except ValueError:
        pass


def test_standby_declaration_visible(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "owner", "standby", "k1:standbydev")
    _, gov = _gov(home)
    assert "k1:standbydev" in gov["standbys"]
    assert "k1:standbydev" in _run(home, "owner", "show").stdout
    _run(home, "owner", "standby", "k1:standbydev", "--off")
    _, gov = _gov(home)
    assert "k1:standbydev" not in gov["standbys"]


def test_hive_escrow_restore_round_trip(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    oid = _owner_id(home)
    _run(home, "owner", "escrow", passphrase="correct-horse-battery")   # encrypted blob into the journal
    _, gov = _gov(home)                                                  # it's an owner-signed governance entry
    import merkle
    entries = merkle.read_all_entries(str(home / "journal"))
    assert any(e.get("payload", {}).get("action") == "owner-escrow" for e in entries)
    (home / ".owner-key").unlink()                                      # lose the device's key
    assert "does NOT hold the owner key" in _run(home, "owner", "show").stdout
    out = _run(home, "owner", "restore", passphrase="correct-horse-battery").stdout
    assert "recovered from the hive" in out
    show = _run(home, "owner", "show").stdout
    assert "holds the owner key" in show and oid in show                # same owner resumes


def test_hive_escrow_cancels_on_empty_passphrase(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    out = _run(home, "owner", "escrow", passphrase="").stdout   # empty = bail out
    assert "Cancelled" in out and "NOT escrowed" in out
    import merkle
    entries = merkle.read_all_entries(str(home / "journal"))
    assert not any(e.get("payload", {}).get("action") == "owner-escrow" for e in entries)


def test_hive_escrow_refuses_short_passphrase(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    out = _run(home, "owner", "escrow", passphrase="short").stdout
    assert "too short" in out
    import merkle
    entries = merkle.read_all_entries(str(home / "journal"))
    assert not any(e.get("payload", {}).get("action") == "owner-escrow" for e in entries)


def test_hive_restore_wrong_passphrase_fails(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "owner", "escrow", passphrase="correct-horse-battery")
    out = _run(home, "owner", "restore", passphrase="wrong-passphrase-xx").stdout
    assert "Could not decrypt" in out


def test_backcompat_no_resilience_entries_equals_today(tmp_path):
    # A plain owner+admit hive must project identically to before (no standbys, owner unchanged).
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "group", "admit", "k1:aaa", "--principal", "alice")
    _, gov = _gov(home)
    assert gov["owner_id"] and "k1:aaa" in gov["admitted"] and gov["standbys"] == set()


# ── Phase 2: nominated succession + transfer + escrow tombstone ───────────────────────────────────

def test_nominate_claim_moves_owner_and_old_key_goes_inert(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _run(a, "owner", "init", node_id="k1:nodeA")
    oid_a = _owner_id(a)
    bpub = _claim_mint_pub(b, "k1:nodeB")                    # successor mints its prospective key
    _run(a, "owner", "nominate", bpub, node_id="k1:nodeA")   # current owner nominates it
    _sync(a, b)                                              # B learns the genesis + nomination
    out = _run(b, "owner", "claim", node_id="k1:nodeB").stdout
    assert "Claimed ownership" in out
    _sync(b, a)                                              # A learns of the handoff
    _, gov_a = _gov(a)
    _, gov_b = _gov(b)
    assert gov_a["owner_id"] == gov_b["owner_id"]            # converged
    assert gov_a["owner_id"] != oid_a                        # owner actually changed
    assert gov_a["owner_term"] == 1
    # The old owner key can no longer sign governance: A's admit is appended but the chain drops it.
    _run(a, "group", "admit", "k1:zzz", "--principal", "z", node_id="k1:nodeA")
    _, gov_a2 = _gov(a)
    assert "k1:zzz" not in gov_a2["admitted"]


def test_claim_without_nomination_is_inert(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _run(a, "owner", "init", node_id="k1:nodeA")
    oid_a = _owner_id(a)
    _claim_mint_pub(b, "k1:nodeB")                           # B mints but is NOT nominated
    _sync(a, b)
    out = _run(b, "owner", "claim", node_id="k1:nodeB").stdout
    assert "Claimed ownership" not in out                    # nothing to claim
    _sync(b, a)
    _, gov_a = _gov(a)
    assert gov_a["owner_id"] == oid_a and gov_a["owner_term"] == 0


def test_non_owner_nominate_is_ignored(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _run(a, "owner", "init", node_id="k1:nodeA")
    _claim_mint_pub(b, "k1:nodeB")                           # B holds a non-owner key
    _sync(a, b)
    # B (not the owner) nominates some key — signed by B's non-owner key, so the chain ignores it.
    other = _claim_mint_pub(tmp_path / "C", "k1:nodeC")
    _run(b, "owner", "nominate", other, node_id="k1:nodeB")
    _sync(b, a)
    _, gov_a = _gov(a)
    assert gov_a["nominations"] == set()


def test_transfer_is_immediate_handoff(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _run(a, "owner", "init", node_id="k1:nodeA")
    bpub = _claim_mint_pub(b, "k1:nodeB")                    # B holds the target key
    _run(a, "owner", "transfer", bpub, node_id="k1:nodeA")   # immediate handoff, no claim needed
    _sync(a, b)
    bid = _owner_id_for_pub_b64(tmp_path, bpub)
    _, gov_a = _gov(a)
    assert gov_a["owner_id"] == bid and gov_a["owner_term"] == 1


def _owner_id_for_pub_b64(tmp_path, pub_b64):
    import base64
    loader = importlib.machinery.SourceFileLoader("hvmod_oid", str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("hvmod_oid", loader))
    loader.exec_module(m)
    return m._owner_id_for_pub(base64.b64decode(pub_b64))


def test_two_claims_one_nomination_resolve_to_single_deterministic_owner(tmp_path):
    # Owner nominates TWO successors; both claim. The first by (ts,node,seq) wins and closes BOTH
    # nominations, so the second is inert — exactly one handoff, and replay is stable.
    a, s1, s2 = tmp_path / "A", tmp_path / "S1", tmp_path / "S2"
    _run(a, "owner", "init", node_id="k1:nodeA")
    p1 = _claim_mint_pub(s1, "k1:s1")
    p2 = _claim_mint_pub(s2, "k1:s2")
    _run(a, "owner", "nominate", p1, node_id="k1:nodeA")
    _run(a, "owner", "nominate", p2, node_id="k1:nodeA")
    _sync(a, s1)
    _sync(a, s2)
    _run(s1, "owner", "claim", node_id="k1:s1")
    _run(s2, "owner", "claim", node_id="k1:s2")
    # Merge everything onto A and project twice — same answer both times, exactly one handoff.
    _sync(s1, a)
    _sync(s2, a)
    _, gov1 = _gov(a)
    _, gov2 = _gov(a)
    assert gov1["owner_id"] == gov2["owner_id"]
    assert gov1["owner_term"] == 1                           # only one claim took effect
    assert gov1["owner_id"] in (_owner_id_for_pub_b64(tmp_path, p1), _owner_id_for_pub_b64(tmp_path, p2))


def test_revoke_escrow_tombstones_then_reescrow_restores(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "owner", "escrow", passphrase="correct-horse-battery")
    # Tombstone every escrow; restore must now find nothing live.
    _run(home, "owner", "revoke-escrow", "all")
    _, gov = _gov(home)
    assert gov["escrows"] == []
    (home / ".owner-key").unlink()
    out = _run(home, "owner", "restore", passphrase="correct-horse-battery").stdout
    assert "No (live) owner-escrow" in out
    # Recover the key (from the per-home stash, see _run) and re-escrow with a fresh passphrase.
    stash = home / "stash" / ".owner-key"
    (home / ".owner-key").write_text(stash.read_text())
    _run(home, "owner", "escrow", passphrase="brand-new-passphrase")
    (home / ".owner-key").unlink()
    out2 = _run(home, "owner", "restore", passphrase="brand-new-passphrase").stdout
    assert "recovered from the hive" in out2


def test_phase2_backcompat_plain_hive_unchanged(tmp_path):
    home = tmp_path
    _run(home, "owner", "init")
    _run(home, "group", "admit", "k1:aaa", "--principal", "alice")
    _, gov = _gov(home)
    assert gov["owner_term"] == 0 and gov["nominations"] == set() and gov["escrows"] == []
    assert "k1:aaa" in gov["admitted"]


# ── Phase 3: quorum election + dead-man switch ────────────────────────────────────────────────────
# Elections are DEVICE-signed (authority = hive membership, not the owner key), so these tests mint a
# REAL device key per home (`hv key init`) instead of overriding HIVE_NODE_ID — an unsigned election
# entry is dropped by `_governance_state`. The dead-man clock is driven by $HIVE_NOW (the `now=` arg):
# governance entries are dated by it, and a proposal carries that instant as its `basis_ts`.

import re  # noqa: E402


def _device_id(home):
    for line in _run(home, "whoami").stdout.splitlines():
        if line.startswith("device:"):
            return line.split()[1]
    raise AssertionError("no device id in whoami")


def _merge_into(dst, *srcs):
    """G-Set merge every src journal into dst (order-independent), rebuilding after each."""
    for s in srcs:
        _sync(s, dst)


def _election_pids(home):
    return re.findall(r"e1:[0-9a-f]+", _run(home, "owner", "elections").stdout)


def _setup_quorum_hive(tmp_path, quorum_m=2, dead_man_days=30, admit_day="2026-07-01"):
    """Owner A + two admitted members B, C, each with a real device key. The owner's last activity is
    `admit_day` (the admits/config), so an election basis_ts more than dead_man_days later is 'dark'."""
    a, b, c = tmp_path / "A", tmp_path / "B", tmp_path / "C"
    for h in (a, b, c):
        _run(h, "key", "init")
    db, dc = _device_id(b), _device_id(c)
    now = f"{admit_day}T00:00:00.000+00:00"
    _run(a, "owner", "init", now=now)
    _run(a, "group", "admit", db, "--principal", "bob", now=now)
    _run(a, "group", "admit", dc, "--principal", "carol", now=now)
    _run(a, "config", "quorum", "set", "quorum_m", str(quorum_m), now=now)
    _run(a, "config", "quorum", "set", "dead_man_days", str(dead_man_days), now=now)
    return a, b, c


def test_quorum_elects_owner_after_dead_man_inactivity(tmp_path):
    a, b, c = _setup_quorum_hive(tmp_path)
    _, gov0 = _gov(a)
    owner0 = gov0["owner_id"]
    _merge_into(b, a)                                        # B learns genesis + admits + config
    basis = "2026-08-15T00:00:00.000+00:00"                  # ~45 days of owner silence (> 30)
    _run(b, "owner", "propose-election", "--mint", now=basis)
    pid = _election_pids(b)[0]
    _merge_into(c, a, b)
    _run(c, "owner", "vote", pid, now=basis)                 # 2nd endorser → quorum
    _merge_into(a, b, c)
    _, gov = _gov(a)
    assert gov["owner_id"] != owner0                         # the election installed a new owner
    assert gov["owner_term"] == 1
    _, gov2 = _gov(a)                                        # deterministic on replay
    assert gov2["owner_id"] == gov["owner_id"] and gov2["owner_term"] == 1


def test_live_owner_heartbeating_is_not_unseated(tmp_path):
    a, b, c = _setup_quorum_hive(tmp_path)
    _, gov0 = _gov(a)
    owner0 = gov0["owner_id"]
    _run(a, "owner", "heartbeat", now="2026-08-10T00:00:00.000+00:00")   # 5 days before the basis
    _merge_into(b, a)
    basis = "2026-08-15T00:00:00.000+00:00"
    _run(b, "owner", "propose-election", "--mint", now=basis)
    pid = _election_pids(b)[0]
    _merge_into(c, a, b)
    _run(c, "owner", "vote", pid, now=basis)
    _merge_into(a, b, c)
    _, gov = _gov(a)
    assert gov["owner_id"] == owner0 and gov["owner_term"] == 0          # quorum met, but owner alive
    assert any(e["votes"] >= 2 and not e["armed"] and not e["installed"] for e in gov["elections"])


def test_below_quorum_and_non_admitted_do_not_elect(tmp_path):
    a, b, c = _setup_quorum_hive(tmp_path, quorum_m=2)
    _, gov0 = _gov(a)
    owner0 = gov0["owner_id"]
    d = tmp_path / "D"
    _run(d, "key", "init")                                   # D is never admitted
    _merge_into(b, a)
    basis = "2026-08-15T00:00:00.000+00:00"
    _run(b, "owner", "propose-election", "--mint", now=basis)
    pid = _election_pids(b)[0]
    _merge_into(a, b)                                        # only the proposer endorses (1/2)
    _, gov1 = _gov(a)
    assert gov1["owner_id"] == owner0 and not any(e["installed"] for e in gov1["elections"])
    _merge_into(d, a, b)
    out = _run(d, "owner", "vote", pid, now=basis, check=False).stdout
    assert "admitted" in out.lower()                         # a non-admitted device cannot vote


def test_racing_elections_resolve_to_one_deterministic_winner(tmp_path):
    a, b, c = _setup_quorum_hive(tmp_path, quorum_m=2)
    _merge_into(b, a)
    _merge_into(c, a)
    _run(b, "owner", "propose-election", "--mint", now="2026-08-15T00:00:00.000+00:00")
    _run(c, "owner", "propose-election", "--mint", now="2026-08-15T00:00:01.000+00:00")
    _merge_into(b, c)
    _merge_into(c, b)
    pids = sorted(set(_election_pids(b)))
    assert len(pids) == 2                                    # two racing proposals exist
    for pid in pids:                                         # drive BOTH to quorum (2 endorsers each)
        _run(b, "owner", "vote", pid, now="2026-08-16T00:00:00.000+00:00")
        _run(c, "owner", "vote", pid, now="2026-08-16T00:00:02.000+00:00")
    _merge_into(a, b, c)
    _, g1 = _gov(a)
    _, g2 = _gov(a)
    installed = [e for e in g1["elections"] if e["installed"]]
    assert len(installed) == 1 and g1["owner_term"] == 1     # exactly one election installs
    assert g1["owner_id"] == g2["owner_id"]                  # same winner on every replay


def test_elected_owner_governs_after_election(tmp_path):
    a, b, c = _setup_quorum_hive(tmp_path, quorum_m=2)
    _merge_into(b, a)
    basis = "2026-08-15T00:00:00.000+00:00"
    _run(b, "owner", "propose-election", "--mint", now=basis)   # B mints the new owner key (held on B)
    pid = _election_pids(b)[0]
    _merge_into(c, a, b)
    _run(c, "owner", "vote", pid, now=basis)
    _merge_into(b, c)                                           # B learns it won; B holds the owner key
    _, govb = _gov(b)
    newid = govb["owner_id"]
    assert newid != _owner_id(a) and govb["owner_term"] == 1
    # The elected owner can sign governance from here: its admit is honored (chain advanced to B).
    _run(b, "group", "admit", "k1:newdev", "--principal", "dave",
         now="2026-08-20T00:00:00.000+00:00")
    _, govb2 = _gov(b)
    assert govb2["owner_id"] == newid and "k1:newdev" in govb2["admitted"]


def test_quorum_off_is_backcompat(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _run(a, "key", "init")
    _run(b, "key", "init")
    db = _device_id(b)
    _run(a, "owner", "init")
    _run(a, "group", "admit", db, "--principal", "bob")
    _, gov0 = _gov(a)
    owner0 = gov0["owner_id"]
    assert gov0["config"]["quorum_m"] == 0 and gov0["elections"] == []   # defaults: elections OFF
    _merge_into(b, a)
    out = _run(b, "owner", "propose-election", "--mint",
               now="2027-01-01T00:00:00.000+00:00", check=False).stdout
    assert "OFF" in out or "quorum_m=0" in out                # proposing is refused while OFF
    _merge_into(a, b)
    _, gov = _gov(a)
    assert gov["owner_id"] == owner0 and gov["owner_term"] == 0 and gov["elections"] == []
