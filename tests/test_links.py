"""Contract 1.19 — the generic `link` journal type, read side (design doc §2, §5; hive #50).

Links are EVIDENCE, not commands. One journal type, an open `kind` vocabulary, one pass-2 resolver:
  - unknown kind → lands in the journal, projects to nothing (the `announce` rule)
  - supports / contradicts / resolves → identity-weighted ± evidence on the target FACT, folded by
    `_content_evidence` under the existing governance (cap_self, same-device discount, admission)
  - supersedes / resolves COMMAND (superseded_by / facts.resolves) only when `_link_authority` says
    `hard`: owner-signed for the owner AS OF that journal position, or the target's author. From any
    other admitted device they are downgraded to weigh-only and `doctor link-authz` lists them.
  - grounding rule: a link or a fact assertion whose `channel` is `introspect` weighs
    `introspect_support_weight` (default 0); assertions since 1.21 (#73). An unrecognised channel
    counts as `introspect` (#72); an unrecognised source class weighs 1.0, like an absent one
Every projection is pure over the journal — the two-node differential at the bottom is the guarantee.

Entries are crafted in-memory exactly as `hv` would sign them (device signature; optional owner
signature on the payload), written to a temp HIVE_HOME journal, and projected with the real
`rebuild_db`. CLI-level regressions of the legacy write paths use the shared `hive` fixture.
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

T0 = "2026-01-01T00:00:00Z"


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    monkeypatch.setenv("HIVE_NOW", "2026-01-10T00:00:00Z")   # deterministic decay clock
    loader = importlib.machinery.SourceFileLoader("hvmod_links", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_links", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _owner_key(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return seed, pub, hv._owner_id_for_pub(pub)


def _device(hv):
    seed = os.urandom(32)
    pub = hv._ed25519.pub_from_seed(seed)
    return {"seed": seed, "pub": pub, "id": hv._device_id_for_pub(pub), "seq": 0}


def _gov(hv, payload, oseed, opub, ts, seq):
    p = hv._sign_governance_payload(payload, oseed, opub)
    return {"node_id": "ownerdev", "seq": seq, "type": "governance", "timestamp": ts, "payload": p}


def _entry(hv, dev, typ, payload, ts, owner=None):
    """A device-signed entry from `dev` (seq auto-incremented). `owner=(seed, pub)` owner-signs the payload."""
    dev["seq"] += 1
    if owner is not None:
        payload = hv._sign_governance_payload(payload, owner[0], owner[1])
    e = {"node_id": dev["id"], "seq": dev["seq"], "type": typ, "timestamp": ts, "payload": payload}
    return hv._sign_entry(e, dev["seed"], dev["pub"])


def _fact(hv, dev, content, ts, source="manual", channel=None):
    p = {"content": content, "tags": [], "importance": 0.5, "source": source}
    if channel is not None:
        p["channel"] = channel
    return _entry(hv, dev, "fact", p, ts)


def _decision(hv, dev, content, ts):
    return _entry(hv, dev, "decision", {"content": content, "rationale": "r", "source": "manual", "tags": []}, ts)


def _link(hv, dev, kind, frm, to, ts, data=None, channel=None, source="manual", owner=None):
    p = {"kind": kind, "from_ref": [frm["node_id"], frm["seq"]], "to_ref": [to["node_id"], to["seq"]],
         "data": data or {}, "source": source}
    if channel:
        p["channel"] = channel
    return _entry(hv, dev, "link", p, ts, owner=owner)


def _project(hv, home, entries):
    """Write `entries` as the whole journal of `home`, rebuild, return an open sqlite connection."""
    jd = Path(home) / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    for f in jd.glob("*.jsonl"):
        f.unlink()
    (jd / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    hv.init_db()
    hv.rebuild_db()
    conn = sqlite3.connect(Path(home) / "store.db")
    conn.row_factory = sqlite3.Row
    return conn


def _conf(conn, content):
    return conn.execute("SELECT confidence FROM facts WHERE content = ?", (content,)).fetchone()["confidence"]


def _owned_hive(hv, n_devices=3):
    """Genesis owner + n admitted devices (distinct principals). Returns (owner, devices, base_entries)."""
    oseed, opub, oid = _owner_key(hv)
    devs = [_device(hv) for _ in range(n_devices)]
    base = [_gov(hv, {"action": "owner", "owner_id": oid, "hive_id": "h1"}, oseed, opub, T0, 1)]
    for i, d in enumerate(devs):
        base.append(_gov(hv, {"action": "admit", "device_id": d["id"], "principal": f"p{i}"},
                         oseed, opub, f"2026-01-01T00:00:0{i + 1}Z", i + 2))
    return (oseed, opub, oid), devs, base


# ── unknown / malformed kinds ─────────────────────────────────────────────────────────────────────

def test_unknown_kind_lands_and_projects_to_nothing(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, a, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    before = _conf(_project(hv, tmp_path, base + [f1, f2]), f1["payload"]["content"])
    weird = _link(hv, b, "frobnicates", f2, f1, "2026-01-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [f1, f2, weird])
    assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 0     # no row, no error
    assert _conf(conn, f1["payload"]["content"]) == before                    # no confidence change
    # ...and it did land: the journal still holds it (convergence is untouched)
    assert any(e["type"] == "link" and e["payload"]["kind"] == "frobnicates"
               for e in hv.merkle.read_all_entries(str(tmp_path / "journal")))


def test_dangling_ref_is_skipped_deterministically(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    ghost = {"node_id": "k1:0000000000000000", "seq": 99}
    dangling = _link(hv, b, "supports", ghost, f1, "2026-01-03T00:00:00Z")
    dangling2 = _link(hv, b, "supports", f1, ghost, "2026-01-03T00:00:01Z")
    conn = _project(hv, tmp_path, base + [f1, dangling, dangling2])
    assert conn.execute("SELECT count(*) FROM links").fetchone()[0] == 0
    assert round(_conf(conn, f1["payload"]["content"]), 6) == round(hv._confidence_for(1.0), 6)


# ── supports / contradicts as evidence ────────────────────────────────────────────────────────────

def test_supports_from_second_identity_raises_and_same_principal_is_capped(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, a, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    one = _conf(_project(hv, tmp_path, base + [f1, f2]), f1["payload"]["content"])
    # b (another principal) supports f1 → two identities → 0.675
    two = _conf(_project(hv, tmp_path, base + [f1, f2, _link(hv, b, "supports", f2, f1, "2026-01-03T00:00:00Z")]),
                f1["payload"]["content"])
    assert round(one, 6) == round(hv._confidence_for(1.0), 6)
    assert round(two, 6) == round(hv._confidence_for(2.0), 6)
    assert two > one
    # a supporting its OWN fact from a second agent on the same device: same principal → cap_self (0.70)
    own = _link(hv, a, "supports", f2, f1, "2026-01-03T00:00:00Z", source="hermes:primary/other/sess1")
    conn = _project(hv, tmp_path, base + [f1, f2, own])
    c = _conf(conn, f1["payload"]["content"])
    assert c <= hv.CONF_CAP_SELF + 1e-9
    assert conn.execute("SELECT authority FROM links WHERE kind='supports'").fetchone()["authority"] == "hard"


def test_contradicts_lowers_and_introspect_weighs_zero_until_the_knob_is_raised(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, a, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    base_conf = _conf(_project(hv, tmp_path, base + [f1, f2]), f1["payload"]["content"])
    # sense-channel contradiction (absent channel = sense) lowers f1 to net 0
    sense = _link(hv, b, "contradicts", f2, f1, "2026-01-03T00:00:00Z")
    lowered = _conf(_project(hv, tmp_path, base + [f1, f2, sense]), f1["payload"]["content"])
    assert lowered < base_conf and round(lowered, 6) == 0.0
    # introspect-channel contradiction weighs 0 by default: no change
    intro = _link(hv, b, "contradicts", f2, f1, "2026-01-03T00:00:00Z", channel="introspect")
    conn = _project(hv, tmp_path, base + [f1, f2, intro])
    assert round(_conf(conn, f1["payload"]["content"]), 6) == round(base_conf, 6)
    assert conn.execute("SELECT channel FROM links").fetchone()["channel"] == "introspect"
    # the owner raises the governed knob → the same journal now weighs it (identically on every node)
    knob = _gov(hv, {"action": "set-config", "key": "introspect_support_weight", "value": 1.0},
                oseed, opub, "2026-01-01T00:00:09Z", 9)
    conn = _project(hv, tmp_path, base + [knob, f1, f2, intro])
    assert round(_conf(conn, f1["payload"]["content"]), 6) == 0.0
    assert hv._governance_state(base + [knob])["config"]["introspect_support_weight"] == 1.0


def test_introspect_fact_assertion_does_not_corroborate_until_the_knob_is_raised(tmp_path, monkeypatch):
    """#73: the grounding rule covers ASSERTIONS, not only links. A second identity restating a fact
    from `introspect` adds nothing; restating it from `sense` (or with no channel) corroborates."""
    hv = _loadhv(tmp_path, monkeypatch)
    (oseed, opub, _oid), (a, b, _c), base = _owned_hive(hv)
    content = "the staging database runs postgres 16"
    fa = _fact(hv, a, content, "2026-01-02T00:00:00Z")
    one = _conf(_project(hv, tmp_path, base + [fa]), content)
    assert one > 0
    intro = _fact(hv, b, content, "2026-01-03T00:00:00Z", channel="introspect")
    assert round(_conf(_project(hv, tmp_path, base + [fa, intro]), content), 6) == round(one, 6)
    sense = _fact(hv, b, content, "2026-01-03T00:00:00Z", channel="sense")
    two = _conf(_project(hv, tmp_path, base + [fa, sense]), content)
    assert two > one
    absent = _fact(hv, b, content, "2026-01-03T00:00:00Z")               # absent channel = sense
    assert round(_conf(_project(hv, tmp_path, base + [fa, absent]), content), 6) == round(two, 6)
    # the owner raises the governed knob → the same introspect restatement now counts like a sense one
    knob = _gov(hv, {"action": "set-config", "key": "introspect_support_weight", "value": 1.0},
                oseed, opub, "2026-01-01T00:00:09Z", 9)
    raised = _conf(_project(hv, tmp_path, base + [knob, fa, intro]), content)
    assert round(raised, 6) == round(two, 6)


def test_introspect_only_fact_lands_with_zero_confidence_and_can_still_earn_it(tmp_path, monkeypatch):
    """#73: an introspect-only fact is still in the corpus (a row, searchable, linkable), at confidence 0
    with no evidence timestamp; a `sense` supports link from another identity then raises it."""
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    fi = _fact(hv, a, "the retry storm is probably caused by the cache", "2026-01-02T00:00:00Z",
               channel="introspect")
    fi2 = _fact(hv, b, "the retry storm is probably caused by the cache", "2026-01-02T00:00:05Z",
                channel="introspect")
    obs = _fact(hv, b, "cache hit rate fell to 3% during the retry storm", "2026-01-02T00:00:10Z")
    conn = _project(hv, tmp_path, base + [fi, fi2, obs])
    row = conn.execute("SELECT confidence, last_evidence_at FROM facts WHERE content = ?",
                       (fi["payload"]["content"],)).fetchone()
    assert row is not None                                    # it landed
    assert row["confidence"] == 0.0 and row["last_evidence_at"] is None
    sup = _link(hv, b, "supports", obs, fi, "2026-01-03T00:00:00Z")
    assert _conf(_project(hv, tmp_path, base + [fi, fi2, obs, sup]), fi["payload"]["content"]) > 0


def test_unrecognised_channel_counts_as_introspect_everywhere(tmp_path, monkeypatch):
    """#72: one normaliser (`_channel`) for every reader. Absent = sense; `act` keeps full weight; any
    label outside the vocabulary (a typo, a newer word, a non-string) counts as `introspect`, so it
    never earns observation weight, whether on a link or on an assertion."""
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._channel({}) == "sense" and hv._channel({"channel": ""}) == "sense"
    assert hv._channel(None) == "sense"
    assert [hv._channel({"channel": c}) for c in ("sense", "act", "introspect")] == ["sense", "act", "introspect"]
    for odd in ("introspective", "reasoning", "SENSE", 5, ["sense"], {"x": 1}):
        assert hv._channel({"channel": odd}) == "introspect", odd
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the nightly backup finished at 02:14", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, a, "the backup volume has 40% free", "2026-01-02T00:00:01Z")
    one = _conf(_project(hv, tmp_path, base + [f1, f2]), f1["payload"]["content"])
    # a mistyped channel on a supports link: no weight (it used to count in full)
    typo = _link(hv, b, "supports", f2, f1, "2026-01-03T00:00:00Z", channel="introspective")
    assert round(_conf(_project(hv, tmp_path, base + [f1, f2, typo]), f1["payload"]["content"]), 6) == round(one, 6)
    # `act` is a known channel and keeps full weight
    act = _link(hv, b, "supports", f2, f1, "2026-01-03T00:00:00Z", channel="act")
    assert _conf(_project(hv, tmp_path, base + [f1, f2, act]), f1["payload"]["content"]) > one
    # an unknown channel on a restated fact adds nothing either
    odd = _fact(hv, b, f1["payload"]["content"], "2026-01-03T00:00:01Z", channel="reasoning")
    assert round(_conf(_project(hv, tmp_path, base + [f1, f2, odd]), f1["payload"]["content"]), 6) == round(one, 6)


def test_unrecognised_channel_counts_with_introspect_in_trust_velocity(tmp_path, monkeypatch):
    """#72: `_signer_reliability` splits by channel; `act` counts with `sense` (unchanged) and an
    unrecognised label now counts with `introspect` instead of `sense`."""
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, _b, _c), base = _owned_hive(hv)
    facts = [_fact(hv, a, "observed the queue drain", "2026-01-05T00:00:00Z"),
             _fact(hv, a, "restarted the worker", "2026-01-05T00:00:01Z", channel="act"),
             _fact(hv, a, "the queue probably drains on its own", "2026-01-05T00:00:02Z", channel="musing")]
    entries = base + facts
    rel = hv._signer_reliability(entries, hv._governance_state(entries), 30.0, "2026-01-10T00:00:00Z")
    ch = rel[a["id"]]["channels"]
    assert ch["sense"]["asserted"] == 2 and ch["introspect"]["asserted"] == 1


def test_unrecognised_source_class_weighs_like_primary(tmp_path, monkeypatch):
    """#72, decided: the source class is a self-declared DISCOUNT, so an unrecognised class claims none
    and weighs 1.0, like an absent class. `subagent` and `cron` still discount."""
    hv = _loadhv(tmp_path, monkeypatch)
    assert hv._identity_weight("n", "claude-code:myproject/inst")[1] == 1.0     # context name in the slot
    assert hv._identity_weight("n", "claude-code:primary/inst")[1] == 1.0
    assert hv._identity_weight("n", "claude-code")[1] == 1.0                    # flat = absent class
    assert hv._identity_weight("n", "claude-code:subagent/inst")[1] == 0.5
    assert hv._identity_weight("n", "claude-code:cron/inst")[1] == 0.3
    _, (a, b, _c), base = _owned_hive(hv)
    content = "the vendor API rate limit is 600 requests per minute"
    fa = _fact(hv, a, content, "2026-01-02T00:00:00Z")
    named = _fact(hv, b, content, "2026-01-03T00:00:00Z", source="claude-code:myproject/inst")
    primary = _fact(hv, b, content, "2026-01-03T00:00:00Z", source="claude-code:primary/inst")
    sub = _fact(hv, b, content, "2026-01-03T00:00:00Z", source="claude-code:subagent/inst")
    c_named = _conf(_project(hv, tmp_path, base + [fa, named]), content)
    c_primary = _conf(_project(hv, tmp_path, base + [fa, primary]), content)
    c_sub = _conf(_project(hv, tmp_path, base + [fa, sub]), content)
    assert round(c_named, 6) == round(c_primary, 6) and c_sub < c_primary


# ── supersedes: hard vs downgraded ────────────────────────────────────────────────────────────────

def test_supersedes_by_author_is_hard_by_other_device_is_downgraded(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    new_a = _decision(hv, a, "ship on monday instead", "2026-01-02T00:00:01Z")
    new_b = _decision(hv, b, "ship never", "2026-01-02T00:00:02Z")
    # the AUTHOR of `old` supersedes it → hard
    conn = _project(hv, tmp_path, base + [old, new_a, new_b, _link(hv, a, "supersedes", new_a, old, "2026-01-03T00:00:00Z")])
    rows = {r["content"]: r["superseded_by"] for r in conn.execute("SELECT content, superseded_by FROM decisions")}
    assert rows["ship on friday"] is not None and rows["ship on monday instead"] is None
    assert conn.execute("SELECT authority FROM links WHERE kind='supersedes'").fetchone()["authority"] == "hard"
    # a different admitted, NON-owner device tries → downgraded: superseded_by untouched, row = evidence
    attack = _link(hv, b, "supersedes", new_b, old, "2026-01-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [old, new_a, new_b, attack])
    rows = {r["content"]: r["superseded_by"] for r in conn.execute("SELECT content, superseded_by FROM decisions")}
    assert rows["ship on friday"] is None                       # NOT hidden
    assert conn.execute("SELECT authority FROM links WHERE kind='supersedes'").fetchone()["authority"] == "evidence"
    entries = base + [old, new_a, new_b, attack]
    down = hv._links_unauthorized(entries, hv._governance_state(entries))
    assert len(down) == 1 and down[0].startswith("supersedes") and b["id"] in down[0]


def test_owner_signed_supersedes_is_hard_and_point_in_time_survives_transfer(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a_seed, a_pub, a_oid = _owner_key(hv)        # owner A (genesis)
    b_seed, b_pub, b_oid = _owner_key(hv)        # owner B (successor)
    auth, adev = _device(hv), _device(hv)        # `auth` writes the decision; `adev` is A's device
    base = [
        _gov(hv, {"action": "owner", "owner_id": a_oid, "hive_id": "h1"}, a_seed, a_pub, T0, 1),
        _gov(hv, {"action": "admit", "device_id": auth["id"], "principal": "p0"}, a_seed, a_pub, "2026-01-01T00:00:01Z", 2),
        _gov(hv, {"action": "admit", "device_id": adev["id"], "principal": "p1"}, a_seed, a_pub, "2026-01-01T00:00:02Z", 3),
    ]
    old = _decision(hv, auth, "ship on friday", "2026-01-02T00:00:00Z")
    new = _decision(hv, adev, "ship on monday instead", "2026-01-02T00:00:01Z")
    # A's device is neither the author nor the owner KEY — device-signed only → evidence
    plain = _link(hv, adev, "supersedes", new, old, "2026-01-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [old, new, plain])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='ship on friday'").fetchone()[0] is None
    # the same link OWNER-SIGNED (owner-key possession proven on the payload) → hard
    signed = _link(hv, adev, "supersedes", new, old, "2026-01-03T00:00:00Z", owner=(a_seed, a_pub))
    conn = _project(hv, tmp_path, base + [old, new, signed])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='ship on friday'").fetchone()[0] is not None
    assert conn.execute("SELECT authority FROM links").fetchone()["authority"] == "hard"
    # A transfers to B AFTER the signed link: the earlier link stays hard (owner-at-T) …
    transfer = _gov(hv, {"action": "transfer", "new_owner_pub": base64.b64encode(b_pub).decode()},
                    a_seed, a_pub, "2026-01-04T00:00:00Z", 4)
    entries = base + [old, new, signed, transfer]
    assert hv._governance_state(entries)["owner_id"] == b_oid
    conn = _project(hv, tmp_path, entries)
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='ship on friday'").fetchone()[0] is not None
    # … but a NEW link signed by retired owner A, positioned after the transfer, is only evidence.
    # (Target authored by `auth`, not by A's device, so the author rule cannot rescue it.)
    other = _decision(hv, auth, "ship in april", "2026-01-05T00:00:00Z")
    late_new = _decision(hv, adev, "ship in march", "2026-01-05T00:00:01Z")
    late = _link(hv, adev, "supersedes", late_new, other, "2026-01-05T00:00:02Z", owner=(a_seed, a_pub))
    conn = _project(hv, tmp_path, entries + [other, late_new, late])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='ship in april'").fetchone()[0] is None
    assert conn.execute("SELECT authority FROM links WHERE from_kind='decision' ORDER BY created_at DESC").fetchone()["authority"] == "evidence"


# ── resolves: retract-equivalent evidence; provenance only when hard ─────────────────────────────

def test_hard_resolves_equals_a_retract_and_evidence_resolves_still_weighs(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f_old = _fact(hv, a, "the daemon listens on port 9876 today", "2026-01-02T00:00:00Z")
    f_new = _fact(hv, a, "the daemon listens on port 9877 today", "2026-01-02T00:00:01Z")
    # reference: a legacy `retract` by the author
    a_r = dict(a)
    retract = _entry(hv, a_r, "retract", {"retracts_ref": [f_old["node_id"], f_old["seq"]], "reason": "", "source": "manual"},
                     "2026-01-03T00:00:00Z")
    ref_conf = _conf(_project(hv, tmp_path, base + [f_old, f_new, retract]), f_old["payload"]["content"])
    # hard `resolves` by the author: identical confidence effect + provenance on the SOURCE row
    hard = _link(hv, a, "resolves", f_new, f_old, "2026-01-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [f_old, f_new, hard])
    assert round(_conf(conn, f_old["payload"]["content"]), 6) == round(ref_conf, 6)
    row = conn.execute("SELECT resolves FROM facts WHERE content = ?", (f_new["payload"]["content"],)).fetchone()
    old_id = conn.execute("SELECT id FROM facts WHERE content = ?", (f_old["payload"]["content"],)).fetchone()["id"]
    assert row["resolves"] == old_id
    assert conn.execute("SELECT authority FROM links WHERE kind='resolves'").fetchone()["authority"] == "hard"
    # evidence `resolves` by another device: still lowers confidence (one more negative identity), NO provenance
    base_conf = _conf(_project(hv, tmp_path, base + [f_old, f_new]), f_old["payload"]["content"])
    ev = _link(hv, b, "resolves", f_new, f_old, "2026-01-03T00:00:00Z")
    conn = _project(hv, tmp_path, base + [f_old, f_new, ev])
    assert _conf(conn, f_old["payload"]["content"]) < base_conf
    assert conn.execute("SELECT resolves FROM facts WHERE content = ?", (f_new["payload"]["content"],)).fetchone()["resolves"] is None
    assert conn.execute("SELECT authority FROM links WHERE kind='resolves'").fetchone()["authority"] == "evidence"
    entries = base + [f_old, f_new, ev]
    assert len(hv._links_unauthorized(entries, hv._governance_state(entries))) == 1


def test_entity_link_writes_entity_facts_row(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    _, (a, b, _c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    ent = _entry(hv, a, "entity", {"name": "deploy-bot", "type": "project", "attributes": {}}, "2026-01-02T00:00:01Z")
    conn = _project(hv, tmp_path, base + [f1, ent, _link(hv, b, "entity", ent, f1, "2026-01-03T00:00:00Z", data={"confidence": 0.8})])
    r = conn.execute("SELECT ef.confidence FROM entity_facts ef JOIN entities e ON e.id = ef.entity_id WHERE e.name='deploy-bot'").fetchone()
    assert r is not None and abs(r["confidence"] - 0.8) < 1e-9


def test_pre_owner_only_the_author_rule_grants_hard(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    a, b = _device(hv), _device(hv)               # no owner anywhere (bootstrap hive)
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:00Z")
    new = _decision(hv, b, "ship never", "2026-01-02T00:00:01Z")
    conn = _project(hv, tmp_path, [old, new, _link(hv, b, "supersedes", new, old, "2026-01-03T00:00:00Z")])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='ship on friday'").fetchone()[0] is None
    conn = _project(hv, tmp_path, [old, new, _link(hv, a, "supersedes", new, old, "2026-01-03T00:00:00Z")])
    assert conn.execute("SELECT superseded_by FROM decisions WHERE content='ship on friday'").fetchone()[0] is not None


# ── legacy write paths are untouched (no dual-emit, resolvers stay) ──────────────────────────────

def test_verbs_emit_one_link_each_and_no_legacy_field(hive):
    """1.19 PR2b: the write-path switch. Each verb writes exactly ONE `link` INSTEAD of its legacy field /
    entry — never both (no dual-emit). The legacy resolvers stay for entries already in journals."""
    hive.run("remember", "the daemon listens on port 9876 today", "--source", "alice")
    fid = hive.query("SELECT id FROM facts")[0]["id"]
    hive.run("remember", "the daemon listens on port 9877 today", "--source", "alice", "--resolves", str(fid))
    hive.run("decide", "ship on friday", "--rationale", "r")
    did = hive.query("SELECT id FROM decisions")[0]["id"]
    hive.run("decide", "ship on monday instead", "--rationale", "r", "--supersedes", str(did))
    hive.run("entity", "add", "--name", "Daemon", "--type", "project")
    hive.run("entity", "link", "--name", "Daemon", "--fact-id", str(fid), "--confidence", "0.9")
    es = hive.entries()
    types = [e["type"] for e in es]
    assert types.count("link") == 3 and "retract" not in types and "entity_fact" not in types
    assert sorted(e["payload"]["kind"] for e in es if e["type"] == "link") == ["entity", "resolves", "supersedes"]
    assert not any("resolves_ref" in e["payload"] for e in es if e["type"] == "fact")
    assert not any("supersedes_ref" in e["payload"] for e in es if e["type"] == "decision")
    # single-device bootstrap hive: this device authored every target → every link is hard → same effects
    hive.run("doctor", "rebuild")
    assert hive.query("SELECT superseded_by FROM decisions WHERE content='ship on friday'")[0]["superseded_by"] is not None
    assert hive.query("SELECT resolves FROM facts WHERE content LIKE '%9877%'")[0]["resolves"] == fid
    assert hive.query("SELECT confidence FROM facts WHERE id = ?", (fid,))[0]["confidence"] <= 0
    assert hive.query("SELECT count(*) c FROM entity_facts")[0]["c"] == 1
    assert {r["authority"] for r in hive.query("SELECT authority FROM links")} == {"hard"}


def test_config_knob_bounds_via_cli(tmp_path):
    def run(*args, check=True):
        env = dict(os.environ, HIVE_HOME=str(tmp_path), HIVE_IDENTITY_STASH=str(tmp_path / "stash"))
        r = subprocess.run([sys.executable, str(PROJECT / "hv"), *args], env=env, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, r.stderr
        return r
    run("owner", "init")
    assert "must be in [0, 1]" in run("config", "confidence", "set", "introspect_support_weight", "1.5").stdout
    assert "set introspect_support_weight = 0.25" in run("config", "confidence", "set", "introspect_support_weight", "0.25").stdout
    run("version")


# ── the convergence guarantee ────────────────────────────────────────────────────────────────────

def test_two_node_differential_is_byte_identical(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path / "n1", monkeypatch)
    (oseed, opub, _oid), (a, b, c), base = _owned_hive(hv)
    f1 = _fact(hv, a, "the deploy succeeded at commit abc123", "2026-01-02T00:00:00Z")
    f2 = _fact(hv, a, "the deploy took eleven minutes", "2026-01-02T00:00:01Z")
    old = _decision(hv, a, "ship on friday", "2026-01-02T00:00:02Z")
    new = _decision(hv, b, "ship never", "2026-01-02T00:00:03Z")
    entries = base + [f1, f2, old, new,
                      _link(hv, b, "supports", f2, f1, "2026-01-03T00:00:00Z"),
                      _link(hv, c, "contradicts", f1, f2, "2026-01-03T00:00:01Z", channel="introspect"),
                      _link(hv, b, "supersedes", new, old, "2026-01-03T00:00:02Z"),
                      _link(hv, c, "supersedes", new, old, "2026-01-03T00:00:03Z", owner=(oseed, opub)),
                      _link(hv, b, "informed", new, f1, "2026-01-03T00:00:04Z"),
                      _link(hv, b, "outcome-of", f2, new, "2026-01-03T00:00:05Z", data={"polarity": 1}),
                      _link(hv, a, "unknownkind", f1, f2, "2026-01-03T00:00:06Z"),
                      _fact(hv, c, "the deploy took eleven minutes", "2026-01-03T00:00:07Z", channel="introspect")]

    def snapshot(home, order):
        h = _loadhv(home, monkeypatch)
        conn = _project(h, home, list(order))
        q = lambda sql: [tuple(r) for r in conn.execute(sql)]
        return (q("SELECT kind, from_kind, to_kind, signer, authority, channel, created_at FROM links ORDER BY 1,2,3,4"),
                q("SELECT content, round(confidence, 6), contested FROM facts ORDER BY content"),
                q("SELECT content, superseded_by IS NOT NULL FROM decisions ORDER BY content"))

    s1 = snapshot(tmp_path / "n1", entries)
    s2 = snapshot(tmp_path / "n2", reversed(entries))          # same journal, different arrival order
    assert s1 == s2
    assert len(s1[0]) == 6                                       # the six known-kind links; the unknown one is absent
