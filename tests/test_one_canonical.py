"""One canonicaliser (#153).

`_canonical` produces the bytes every hash and every signature covers: `compute_hash` and the prev_hash
chain, the chunk hashes and Merkle root, the device signature over an entry, the owner signature over a
governance payload, the election id and the /sync/hello body digest. Two copies can only agree or split
the fleet. A divergence would be a signature or root that fails to verify on the other side, and the
journal is append-only, so the disagreement would be permanent.

`merkle._canonical` is THE definition; merkle imports nothing from hv, which is the direction the 2.0
split wants. `hv._canonical` delegates to it and keeps the name because hv's own callers and
`sync_common.load_hv()` consumers resolve it there.

Three guards:
  * the tree holds exactly one implementation, and the checker is shown failing on mutants;
  * a fixed entry's bytes, hashes and signatures are the values the code produced BEFORE #153, so a
    green suite is not the only evidence that the hashing path is unchanged;
  * `hv._canonical` behaves as `merkle._canonical`, and is a delegation at call time, not a copy.
"""

import ast
import collections
import hashlib
import importlib.machinery
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import merkle  # noqa: E402


def _loadhv(home, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(home))
    loader = importlib.machinery.SourceFileLoader("hvmod_one_canonical", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_one_canonical", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


# ── exactly one canonicaliser in the tree ───────────────────────────────────────────────────────

IMPLEMENTATION = ("merkle.py", "_canonical")      # the one definition
DELEGATION = ("hv", "_canonical")                 # the one delegation: `return merkle._canonical(obj)`

# Canonical-shaped json.dumps calls that are NOT a second canonicaliser, each with the reason. Adding an
# entry is a review decision; it is not a way to make this test pass.
ALLOWED = {
    ("hv", "_governance_state"):
        "an in-process memo key for the governance projection; never written, hashed or signed across nodes",
    ("tests/test_hv.py", "_canonical"):
        "the test oracle for hv's hash chain, independent of the code under test on purpose; if it "
        "diverged, the suite would fail, not the fleet",
}
# The same, for Python embedded in non-Python files (shell heredocs and the like).
ALLOWED_TEXT = {
    "scripts/common/smoke.sh": "the smoke test's own oracle for the hash chain, the same reasoning as test_hv.py",
}

_TEXT_CANONICAL = (re.compile(r"sort_keys\s*=\s*True"),
                   re.compile(r"""separators\s*=\s*\(\s*["'],["']\s*,\s*["']:["']\s*\)"""))


def _is_canonical_dumps(node):
    """`json.dumps(..., sort_keys=True, separators=(",", ":"))`, however `dumps` was imported."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
    if name != "dumps":
        return False
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    sort, sep = kw.get("sort_keys"), kw.get("separators")
    return (isinstance(sort, ast.Constant) and sort.value is True
            and isinstance(sep, (ast.Tuple, ast.List))
            and [getattr(e, "value", None) for e in sep.elts] == [",", ":"])


def _sites(rel, source):
    """(rel, scope, kind) for every def named `_canonical` (kind "def") and every canonical json.dumps
    call (kind "dumps"). `scope` is the enclosing def, dotted through classes and nested defs, or
    "<module>"; a lambda is "<lambda>"."""
    out = []

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
                if not isinstance(child, ast.ClassDef) and child.name == "_canonical":
                    out.append((rel, inner, "def"))
                walk(child, inner)
            elif isinstance(child, ast.Lambda):
                walk(child, f"{scope}.<lambda>" if scope else "<lambda>")
            else:
                if _is_canonical_dumps(child):
                    out.append((rel, scope or "<module>", "dumps"))
                walk(child, scope)

    walk(ast.parse(source, filename=rel), "")
    return out


def _is_pure_delegation(source):
    """True iff the source's only top-level `_canonical` is `def _canonical(x): [docstring] return
    merkle._canonical(x)`: one parameter, no decorator, nothing else in the body."""
    defs = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_canonical"]
    if len(defs) != 1:
        return False
    fn = defs[0]
    a = fn.args
    if fn.decorator_list or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg or a.defaults or len(a.args) != 1:
        return False
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    return (isinstance(call, ast.Call) and not call.keywords and len(call.args) == 1
            and isinstance(call.func, ast.Attribute) and call.func.attr == "_canonical"
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "merkle"
            and isinstance(call.args[0], ast.Name) and call.args[0].id == a.args[0].arg)


def violations(files):
    """Every canonicaliser in `files` ({relpath: Path}) other than the one implementation and the one
    delegation, and a missing implementation. An empty list means the tree is as #153 left it."""
    problems, sites = [], []
    for rel, path in sorted(files.items()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel == "hv" or rel.endswith(".py"):
            try:
                sites += _sites(rel, text)
            except SyntaxError as e:
                problems.append(f"{rel}: does not parse ({e.msg}), so it cannot be scanned")
        elif not rel.endswith(".md") and all(p.search(text) for p in _TEXT_CANONICAL) \
                and rel not in ALLOWED_TEXT:
            problems.append(f"{rel}: embeds a canonical json.dumps (sort_keys=True, separators=(',', ':'))")
    counts = collections.Counter(sites)
    offenders = collections.defaultdict(set)
    for rel, scope, kind in counts:
        if (rel, scope) not in (IMPLEMENTATION, DELEGATION) and (rel, scope) not in ALLOWED:
            offenders[(rel, scope)].add("a def named _canonical" if kind == "def" else "a canonical json.dumps")
    for (rel, scope), what in sorted(offenders.items()):
        problems.append(f"{rel}: {scope} is a second canonicaliser ({', '.join(sorted(what))}); "
                        "use merkle._canonical")
    impl = IMPLEMENTATION + ("def",), IMPLEMENTATION + ("dumps",)
    if counts[impl[0]] != 1 or counts[impl[1]] != 1:
        problems.append("merkle._canonical is missing or defined more than once: the one definition "
                        "must live in merkle.py")
    if "hv" in files and not _is_pure_delegation(files["hv"].read_text(encoding="utf-8")):
        problems.append("hv._canonical is not a pure delegation to merkle._canonical")
    return problems


def _tracked():
    """{relpath: Path} for the tracked tree; outside a git checkout, every file bar caches and VCS dirs."""
    try:
        out = subprocess.run(["git", "-C", str(PROJECT), "ls-files", "-z"], capture_output=True, timeout=30)
        rels = [n.decode() for n in out.stdout.split(b"\x00") if n] if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        rels = []
    if not rels:
        skip = {".git", "__pycache__", ".pytest_cache", "node_modules", "journal"}
        rels = [str(p.relative_to(PROJECT)) for p in PROJECT.rglob("*")
                if p.is_file() and not set(p.relative_to(PROJECT).parts) & skip]
    return {r: PROJECT / r for r in rels if (PROJECT / r).is_file()}


def test_the_tree_has_exactly_one_canonicaliser():
    files = _tracked()
    for must in ("hv", "merkle.py", "ownerkey.py", "sync_common.py", "tests/test_hv.py"):
        assert must in files, f"the scan did not see {must}; it would pass vacuously"
    assert violations(files) == []


_COPY = '\n\ndef _canonical(obj):\n    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()\n'

MUTANTS = {
    "a second _canonical in ownerkey.py": ("ownerkey.py", lambda s: s + _COPY, "ownerkey.py: _canonical"),
    "the copy migrate_journal.py used to carry": (
        "utilities/migrate_journal.py", lambda s: s + _COPY, "utilities/migrate_journal.py: _canonical"),
    "a renamed copy in sync_common.py": (
        "sync_common.py",
        lambda s: s + '\n\ndef stable_bytes(o):\n    return json.dumps(o, separators=(",", ":"), sort_keys=True).encode()\n',
        "sync_common.py: stable_bytes"),
    "a method named _canonical": (
        "hivemind_ctl.py",
        lambda s: s + "\n\nclass _Signer:\n    def _canonical(self, obj):\n        return merkle._canonical(obj)\n",
        "hivemind_ctl.py: _Signer._canonical"),
    "a module-level lambda": (
        "merkle.py", lambda s: s + '\ncanon = lambda o: json.dumps(o, sort_keys=True, separators=(",", ":"))\n',
        "merkle.py: <lambda>"),
    "hv's own copy restored (the tree before #153)": (
        "hv",
        lambda s: s.replace("    return merkle._canonical(obj)\n",
                            '    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()\n', 1),
        "hv._canonical is not a pure delegation"),
    "merkle's definition renamed away": (
        "merkle.py", lambda s: s.replace("def _canonical(obj):", "def _stable(obj):", 1),
        "merkle._canonical is missing"),
    "a copy inlined in a shell script": (
        "scripts/common/sync_smoke.sh",
        lambda s: s + "\npython3 - <<'PY'\nimport json\nprint(json.dumps({}, sort_keys=True, separators=(',', ':')))\nPY\n",
        "scripts/common/sync_smoke.sh: embeds a canonical json.dumps"),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_the_checker_fails_on_a_mutant(name, tmp_path):
    """The guard is proven to fail: a temp copy of one real source, with a second canonicaliser added,
    replaces the real file in the scan, and the checker must name it."""
    rel, mutate, expected = MUTANTS[name]
    files = _tracked()
    original = files[rel].read_text(encoding="utf-8")
    mutated = mutate(original)
    assert mutated != original, f"the mutant for {name!r} did not change {rel}"
    copy = tmp_path / rel
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(mutated, encoding="utf-8")
    files[rel] = copy
    problems = violations(files)
    assert any(expected in p for p in problems), (name, problems)


# ── the bytes are the bytes they were ───────────────────────────────────────────────────────────
# Captured from origin/release/2.0 at 5d48644, BEFORE #153 touched anything, by signing the entry below
# with the fixed seed bytes(range(32)) (the owner seed is bytes(range(32, 64))). Ed25519 is
# deterministic, so re-signing reproduces the signatures exactly. Never regenerate these to make a
# failure go away: a change here changes every hash and signature on the fleet.

NODE_ID = "k1:56475aa75463474c"
PUB = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
SIG = "lSskIzk4CWfh5i8baQjihWF+RzFzNNyUG4izk6IZtwei4Ac3UBm+IcWsGr2SeNj8MBuJLLga+uzLatJLsPIwAg=="
CANONICAL = (
    b'{"node_id":"k1:56475aa75463474c","payload":{"big":9223372036854775809,"confidence":0.85,'
    b'"content":"Z\\u00fcrich caf\\u00e9, na\\u00efve \\u2014 \\u2603 \\u65e5\\u672c\\u8a9e '
    b'\\ud83d\\udc1d \\"quoted\\" back\\\\slash\\nnewline\\ttab","count":3,"neg":-17,'
    b'"nested":{"a":[1,2.5,{"j":[true,false],"k":null}],"empty_list":[],"empty_obj":{},"z":{"a":1,'
    b'"b":2}},"pinned":true,"retracted":false,"supersedes":null,"tags":["zeta","alpha",'
    b'"\\u00fcn\\u00ef"],"tiny":1e-07,"whole":1.0},'
    b'"prev_hash":"sha256:abababababababababababababababababababababababababababababababab",'
    b'"pub":"A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=","seq":7,'
    b'"sig":"lSskIzk4CWfh5i8baQjihWF+RzFzNNyUG4izk6IZtwei4Ac3UBm+IcWsGr2SeNj8MBuJLLga+uzLatJLsPIwAg==",'
    b'"timestamp":"2026-09-27T12:34:56.789+00:00","type":"fact"}'
)
ENTRY_HASH = "sha256:c2ed1b9e46cae4263d519cf9daa8878e57c7ea3e9853e5788bc293c0e19af470"
SIGNING_BYTES_SHA256 = "4a47ff6d67675692fce0fc24eaa35e547a3a706f72c6f8a5451abbe69b3e94ea"

GOV_PAYLOAD = {"action": "set-config", "key": "forget_writers", "value": "owner",
               "note": "é ☃", "n": 2, "list": [3, 1, 2]}
OWNER_ID = "o1:24f6ed6acbfe1009"
GOV_OWNER_PUB = "Kay64UG8yvCyLhqU000LxzYeUm0L/hLIl5S8kyKWbdc="
GOV_OWNER_SIG = "Zs5tB+RO7IstvFgIrxaAFvZCyxid20zSVqsIky9kQr0kQl8Tq66B4uYB5BwbqZQUIxW0oJ3nazDkEtdj3UbiAA=="
ELECTION_ID = "e1:712d09b160e538b1"          # _election_id(GOV_OWNER_PUB, ELECTION_TS)
ELECTION_TS = "2026-09-27T00:00:00.000+00:00"

HELLO_BODY = {"node_id": NODE_ID, "hive_id": "h1:0011223344556677",
              "chunks": {NODE_ID: ["sha256:" + "cd" * 32]}, "genesis": None, "protocol_version": 3,
              "label": "café ☃", "hello_sig": "ignored"}
HELLO_DIGEST = "sha256:d48caee2e26dae24bbd41aa4fc3e8a78ef17d463a19e36c40bb659b12b4dda8e"


def _entry():
    """The fixed entry, unsigned: nested payload, unicode (BMP and astral), escapes, a list, ints (one
    past 2**63), floats, bools and None, with keys deliberately out of order."""
    return {
        "type": "fact",
        "seq": 7,
        "node_id": NODE_ID,
        "timestamp": "2026-09-27T12:34:56.789+00:00",
        "prev_hash": "sha256:" + "ab" * 32,
        "payload": {
            "content": 'Zürich café, naïve — ☃ 日本語 🐝 "quoted" back\\slash\nnewline\ttab',
            "tags": ["zeta", "alpha", "ünï"],
            "confidence": 0.85,
            "count": 3,
            "big": 2 ** 63 + 1,
            "neg": -17,
            "tiny": 1e-07,
            "whole": 1.0,
            "pinned": True,
            "retracted": False,
            "supersedes": None,
            "nested": {"z": {"b": 2, "a": 1}, "a": [1, 2.5, {"k": None, "j": [True, False]}],
                       "empty_list": [], "empty_obj": {}},
        },
    }


def _signed():
    e = _entry()
    e["pub"], e["sig"] = PUB, SIG
    return e


def test_golden_canonical_bytes_and_entry_hash(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    e = _signed()
    assert hv._canonical(e) == CANONICAL
    assert merkle._canonical(e) == CANONICAL
    assert ENTRY_HASH == "sha256:" + hashlib.sha256(CANONICAL).hexdigest()
    assert hv.compute_hash(e) == ENTRY_HASH
    assert merkle.hash_entries([e]) == ENTRY_HASH
    assert merkle.node_chunk_hashes([e]) == {NODE_ID: [ENTRY_HASH]}
    assert merkle.chunk_hashes([e]) == [ENTRY_HASH]


def test_golden_device_signature(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    e = _signed()
    assert hashlib.sha256(hv._entry_signing_bytes(e)).hexdigest() == SIGNING_BYTES_SHA256
    assert hv._verify_entry(e) is True
    seed = bytes(range(32))
    pub = hv._ed25519.pub_from_seed(seed)
    assert hv._device_id_for_pub(pub) == NODE_ID
    assert hv._sign_entry(_entry(), seed, pub) == e          # same bytes signed, same signature


def test_golden_governance_signature_election_id_and_hello_digest(tmp_path, monkeypatch):
    import ownerkey
    import sync_common
    hv = _loadhv(tmp_path, monkeypatch)
    owner_seed = bytes(range(32, 64))
    owner_pub = hv._ed25519.pub_from_seed(owner_seed)
    gov = ownerkey.sign_governance(GOV_PAYLOAD, owner_seed, owner_pub)
    assert (gov["owner_pub"], gov["owner_sig"]) == (GOV_OWNER_PUB, GOV_OWNER_SIG)
    assert hv._verify_governance(gov) == OWNER_ID            # the verifier hashes through hv._canonical
    assert hv._election_id(GOV_OWNER_PUB, ELECTION_TS) == ELECTION_ID
    assert sync_common.hello_body_digest(HELLO_BODY) == HELLO_DIGEST   # via load_hv()._canonical


# ── hv._canonical is merkle._canonical ──────────────────────────────────────────────────────────

_circular = []
_circular.append(_circular)

SPREAD = [
    None, True, False, 0, -1, 2 ** 64, -(2 ** 70), 3.14, 1e308, 5e-324, -0.0, 1.0,
    float("inf"), float("-inf"), float("nan"),
    "", "plain", "é ☃ 日本語 🐝", "\x00\x1f\x7f  ", '"\\/', "\ud800",
    [], {}, (1, "two", 3.0), [1, [2, [3, [None]]]],
    {"b": 1, "a": {"d": [None, True], "c": 2.0}}, {"ü": 1, "u": 2, "": 3, "U": 4},
    {2: "b", 10: "a"}, {1.5: "f"},
    _entry(), _signed(), GOV_PAYLOAD, HELLO_BODY,
]
RAISES = [{1: "a", "b": 2}, {True: "t", None: "n"}, {"s": {1, 2}}, b"bytes", object(), _circular]


def test_hv_canonical_matches_merkle_for_a_spread_of_inputs(tmp_path, monkeypatch):
    hv = _loadhv(tmp_path, monkeypatch)
    for obj in SPREAD:
        got = hv._canonical(obj)
        assert isinstance(got, bytes)
        assert got == merkle._canonical(obj), obj
    for obj in RAISES:
        with pytest.raises(Exception) as via_hv:
            hv._canonical(obj)
        with pytest.raises(Exception) as via_merkle:
            merkle._canonical(obj)
        assert (via_hv.type, str(via_hv.value)) == (via_merkle.type, str(via_merkle.value)), obj


def test_hv_canonical_is_a_delegation_not_a_copy(tmp_path, monkeypatch):
    """Behaviourally, not just by AST: replace merkle's function and hv's name follows it."""
    hv = _loadhv(tmp_path, monkeypatch)
    monkeypatch.setattr(hv.merkle, "_canonical", lambda obj: b"merkle-owns-this")
    assert hv._canonical({"a": 1}) == b"merkle-owns-this"
    assert hv.compute_hash({"a": 1}) == "sha256:" + hashlib.sha256(b"merkle-owns-this").hexdigest()
