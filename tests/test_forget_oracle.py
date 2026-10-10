"""PR 1 of release 3.0 (plan 3.6): the frozen 2.4 `_content_evidence` oracle, and the forget corpus it is
checked against. Tests only; no production code changes.

The gate: on every corpus journal, under each `forget_writers` setting, the oracle equals the live
`_content_evidence` (the whole result, not only the forget flag). The corpus is also pinned by hand-written
expectations, so "oracle equals live" cannot pass by both being wrong the same way. Later PRs (the migration,
the arm's removal) reuse the oracle and these journals for their differentials.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tests"))
sys.path.insert(0, str(PROJECT / "tests" / "oracles"))
from test_links import _loadhv, _project  # noqa: E402
from test_unforget import FORGET_FLOOR, _row  # noqa: E402
import forget_corpus as fc  # noqa: E402
import content_evidence_2_4 as oracle_mod  # noqa: E402

ORACLE_FILE = PROJECT / "tests" / "oracles" / "content_evidence_2_4.py"
ORACLE_SHA256 = "d468fb88d4035c44aae240aca679a0e6082d624f6220a8f907ceb6f50035b90d"
FROZEN_AT = "53a3eeb"
CASES = [(name, policy) for name in fc.CORPUS for policy in fc.POLICIES]


def _frozen_text():
    src = ORACLE_FILE.read_text()
    return src.split("# --- begin frozen 2.4 text ---\n", 1)[1].split("# --- end frozen 2.4 text ---\n", 1)[0]


def _forgotten(result):
    return {c for c, ev in result.items() if ev["forget"]}


def _build(tmp_path, monkeypatch, name, policy):
    hv = _loadhv(tmp_path, monkeypatch)
    scn = fc.CORPUS[name](hv)
    return hv, scn, fc.journal(hv, scn, policy)


# ── the oracle is the 2.4 text ─────────────────────────────────────────────────────────────────────

def test_the_frozen_text_is_pinned():
    assert hashlib.sha256(_frozen_text().encode()).hexdigest() == ORACLE_SHA256, \
        "tests/oracles/content_evidence_2_4.py is frozen; it is never edited"


def test_the_frozen_text_is_what_2_4_shipped():
    shown = subprocess.run(["git", "-C", str(PROJECT), "show", f"{FROZEN_AT}:hv"], capture_output=True, text=True)
    if shown.returncode != 0:
        pytest.skip(f"{FROZEN_AT} is not in this checkout (shallow clone)")
    import ast
    tree = ast.parse(shown.stdout)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_content_evidence")
    shipped = "\n".join(shown.stdout.splitlines()[node.lineno - 1:node.end_lineno]) + "\n"
    assert _frozen_text() == shipped


def test_the_oracle_is_a_separate_function_from_the_live_one(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    bound = oracle_mod.bind(hv)
    assert bound is not hv._content_evidence and bound.__code__ is not hv._content_evidence.__code__


# ── the gate: oracle == live on every corpus journal ───────────────────────────────────────────────

@pytest.mark.parametrize("name,policy", CASES)
def test_oracle_equals_live(tmp_path, monkeypatch, name, policy):
    hv, scn, entries = _build(tmp_path, monkeypatch, name, policy)
    oracle = oracle_mod.bind(hv)
    gov = hv._governance_state(entries)
    for links in (True, False):
        assert oracle(entries, gov, links) == hv._content_evidence(entries, gov, links)
    assert oracle(entries) == hv._content_evidence(entries)          # gov=None: the default governance


# ── the corpus says what the 2.4 rule says ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,policy", CASES)
def test_corpus_expectations(tmp_path, monkeypatch, name, policy):
    hv, scn, entries = _build(tmp_path, monkeypatch, name, policy)
    gov = hv._governance_state(entries)
    if scn.owner is not None:
        assert gov["config"].get("forget_writers", "legacy") == fc.effective(scn, policy)
    column = 1 if fc.effective(scn, policy) == "owner" else 0
    want = {c for c, flags in scn.expect.items() if flags[column]}
    assert _forgotten(oracle_mod.bind(hv)(entries, gov)) == want


@pytest.mark.parametrize("name", list(fc.CORPUS))
def test_the_grandfather_arm_is_what_the_policy_switches(tmp_path, monkeypatch, name):
    """`legacy` and `owner` differ on exactly the contents whose expectation differs; nothing else moves."""
    hv, scn, _ = _build(tmp_path, monkeypatch, name, "absent")
    oracle = oracle_mod.bind(hv)
    legacy, owner = (_forgotten(oracle(es, hv._governance_state(es)))
                     for es in (fc.journal(hv, scn, "legacy"), fc.journal(hv, scn, "owner")))
    assert legacy - owner == {c for c, (lg, ow) in scn.expect.items() if lg and not ow}
    assert owner - legacy == set()                                   # closing never forgets more


def test_the_corpus_covers_what_the_plan_names():
    assert set(fc.CORPUS) >= {
        "pre_genesis_unsigned_latest", "pre_genesis_forget_then_signed_unforget",
        "pre_genesis_forget_reforgotten_post_genesis", "member_device_backdated", "owner_transfer_between",
        "owner_succession_between", "owner_election_between", "dangling_pre_genesis_forget",
        "shared_content_one_forgotten", "pre_owner_hive", "closed_at_genesis"}
    assert set(fc.POLICIES) == {"absent", "legacy", "owner"}


# ── the corpus is real: the live projection hides what the oracle says is forgotten ────────────────

@pytest.mark.parametrize("name,policy", CASES)
def test_projection_hides_exactly_the_forgotten_contents(tmp_path, monkeypatch, name, policy):
    hv, scn, entries = _build(tmp_path, monkeypatch, name, policy)
    conn = _project(hv, tmp_path, entries)
    hidden = {r["content"] for r in conn.execute("SELECT content, confidence FROM facts") if r["confidence"] == FORGET_FLOOR}
    assert hidden == _forgotten(oracle_mod.bind(hv)(entries, hv._governance_state(entries)))
