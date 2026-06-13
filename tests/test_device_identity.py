"""Device-identity tests: per-node Ed25519 keys, signed entries, verify-on-ingest.

Each test drives the real `hv` CLI against an isolated temp HIVE_HOME (the `hive`
fixture). A node with no .device-key keeps its legacy hostname identity; `hv key init`
mints a key and flips the node to device-identity mode (signed writes under the
device_id fingerprint).
"""

import base64
import copy
import importlib.machinery
import importlib.util
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _loadhv():
    """Import ./hv as a module. _verify_entry and friends are pure (no HIVE_HOME state)."""
    loader = importlib.machinery.SourceFileLoader("hvmod_di", str(PROJECT / "hv"))
    spec = importlib.util.spec_from_loader("hvmod_di", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_no_key_is_legacy_hostname_identity(hive):
    hive.run("remember", "a legacy fact written with no device key")
    e = hive.entries()[0]
    assert "sig" not in e
    assert not e["node_id"].startswith("k1:")


def test_key_init_and_show(hive):
    out = hive.run("key", "init").stdout
    assert "device_id: k1:" in out
    assert "device_id: k1:" in hive.run("key", "show").stdout


def test_writes_are_signed_under_device_id(hive):
    hive.run("key", "init")
    hive.run("remember", "a fact written after minting a device key")
    e = hive.entries()[0]
    assert e["node_id"].startswith("k1:")
    assert "sig" in e and "pub" in e
    # node_id is the fingerprint of the embedded signing key
    hv = _loadhv()
    assert e["node_id"] == hv._device_id_for_pub(base64.b64decode(e["pub"]))


def test_genuine_verifies_tamper_and_forge_fail(hive):
    hive.run("key", "init")
    hive.run("remember", "verify this signed fact")
    hv = _loadhv()
    e = hive.entries()[0]
    assert hv._verify_entry(e) is True

    # tampering the payload breaks the signature
    tampered = copy.deepcopy(e)
    tampered["payload"]["content"] = "TAMPERED"
    assert hv._verify_entry(tampered) is False

    # forging: an attacker signs with their own key but keeps the victim's node_id.
    # node_id no longer equals the fingerprint of the (attacker) pub -> rejected.
    atk_seed = os.urandom(32)
    atk_pub = hv._ed25519.pub_from_seed(atk_seed)
    forged = copy.deepcopy(e)
    forged["pub"] = base64.b64encode(atk_pub).decode()
    forged["sig"] = base64.b64encode(
        hv._ed25519.sign(hv._entry_signing_bytes(forged), atk_seed)).decode()
    assert hv._verify_entry(forged) is False


def test_ingest_rejects_a_forged_entry(hive):
    # A genuine signed entry from a *foreign* device is accepted; a tampered copy is rejected.
    hive.run("key", "init")
    hive.run("remember", "seed so the node has its own chain")
    hv = _loadhv()

    # craft a genuine foreign entry (a second device)
    foreign_seed = os.urandom(32)
    foreign_pub = hv._ed25519.pub_from_seed(foreign_seed)
    foreign_id = hv._device_id_for_pub(foreign_pub)
    entry = {
        "node_id": foreign_id, "seq": 1, "type": "fact",
        "timestamp": "2026-06-08T00:00:00.000+00:00",
        "payload": {"content": "a genuine foreign fact", "tags": [], "source": "peer"},
        "prev_hash": "sha256:genesis",
    }
    hv._sign_entry(entry, foreign_seed, foreign_pub)
    assert hv._verify_entry(entry) is True

    forged = copy.deepcopy(entry)
    forged["payload"]["content"] = "forged content"
    assert hv._verify_entry(forged) is False


def test_init_guard_refuses_on_a_populated_hostname_journal(hive):
    # Writing under the hostname first, then `hv key init`, must refuse (migration is the path).
    hive.run("remember", "a pre-existing hostname-authored fact")
    out = hive.run("key", "init", check=False).stdout
    assert "migrate-device-identity" in out


# --- migration ---------------------------------------------------------------

import json
import shutil
import subprocess
import sys

HV = PROJECT / "hv"


def _run(home, *args, node_id=None):
    env = dict(os.environ, HIVE_HOME=str(home))
    if node_id:
        env["HIVE_NODE_ID"] = node_id
    r = subprocess.run([sys.executable, str(HV), *args], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _root(home):
    out = subprocess.run([sys.executable, str(HV), "merkle"],
                         env=dict(os.environ, HIVE_HOME=str(home)),
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.strip().startswith("Root:")][0]


def test_migration_is_deterministic_across_nodes(tmp_path):
    # Build a converged 2-host journal with a cross-ref (entity link).
    base = tmp_path / "base"
    _run(base, "remember", "alpha fact", "--source", "claude-code", node_id="node-a")
    _run(base, "entity", "add", "--name", "Thing", "--type", "concept", node_id="node-a")
    _run(base, "entity", "link", "--name", "Thing", "--fact-id", "1", node_id="node-a")
    _run(base, "remember", "beta fact", "--source", "hermes", node_id="node-b")

    mapping = {"node-a": "k1:aaaaaaaaaaaaaaaa", "node-b": "k1:bbbbbbbbbbbbbbbb"}
    mapfile = tmp_path / "map.json"
    mapfile.write_text(json.dumps(mapping))

    # Two nodes apply the SAME map to copies of the same journal.
    n1, n2 = tmp_path / "n1", tmp_path / "n2"
    for n in (n1, n2):
        n.mkdir()
        shutil.copytree(base / "journal", n / "journal")
        _run(n, "migrate-device-identity", "--map", str(mapfile))

    assert _root(n1) == _root(n2)   # determinism -> peers stay converged

    # hostnames gone, cross-ref remapped to the device_id
    text = "".join((n1 / "journal" / f).read_text() for f in os.listdir(n1 / "journal"))
    assert "node-a" not in text and "node-b" not in text
    assert "k1:aaaaaaaaaaaaaaaa" in text


def test_doctor_subcommands_and_aliases(tmp_path):
    """`hv merkle` and `hv migrate-device-identity` now live under `hv doctor`; the old
    top-level forms keep working as silent argv aliases."""
    home = tmp_path
    _run(home, "remember", "a fact", node_id="node-a")
    canon = _run(home, "doctor", "merkle").stdout
    alias = _run(home, "merkle").stdout
    assert "Root:" in canon and canon == alias
    mapping = {"node-a": "k1:aaaaaaaaaaaaaaaa"}
    mapfile = tmp_path / "m.json"
    mapfile.write_text(json.dumps(mapping))
    # canonical and alias both reach the migrate handler (dry-run, nothing written)
    assert _run(home, "doctor", "migrate-identity", "--map", str(mapfile), "--dry-run").stdout
    assert _run(home, "migrate-device-identity", "--map", str(mapfile), "--dry-run").stdout
