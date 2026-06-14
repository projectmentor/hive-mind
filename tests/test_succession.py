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


def _run(home, *args, node_id=None, passphrase=None, check=True):
    # HIVE_IDENTITY_STASH is pinned UNDER the temp home so `owner init/import/restore/claim` never
    # touch the real ~/.config/hive-mind/identity/.owner-key backup on the developer's machine.
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home) / "stash"))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    if passphrase is not None:
        env["HIVE_OWNER_PASSPHRASE"] = passphrase     # non-interactive passphrase for tests
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
