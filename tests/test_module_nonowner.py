"""hwatch C6 (M1b): install a module on a device that does not hold the owner key.

Node A is the owner. Node B has no owner key. `module add` on B stages and prints the owner's admit line and installs
nothing; the install completes with a second `module add` once A's admit is in B's journal (here the journal files are
copied, as sync does). Mutants of each rule were armed by hand against these tests; the results are in the PR.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import _keys  # noqa: E402
import hive_modules  # noqa: E402
from test_module_lifecycle import (LINUX_ONLY, OTHER_SEED, _publish, _state, units, _run, _gov, _device_id)  # noqa: E402,F401


class Nodes:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.mp = tmp_path, monkeypatch
        monkeypatch.delenv("TERMUX_VERSION", raising=False)
        self.a, self.b = tmp_path / "A", tmp_path / "B"
        self.mods = {"a": tmp_path / "modules-a", "b": tmp_path / "modules-b"}
        self.run_a("config", "identity", "init")
        self.run_a("owner", "init")
        self.run_a("group", "admit", _device_id(self.a), "--principal", "op")
        self.run_b("config", "identity", "init")

    def _run(self, who, *args, check=True):
        self.mp.setenv("HIVE_MODULES_DIR", str(self.mods[who]))
        return _run(self.a if who == "a" else self.b, *args, check=check)

    def run_a(self, *args, check=True):
        return self._run("a", *args, check=check)

    def run_b(self, *args, check=True):
        return self._run("b", *args, check=check)

    def add_b(self, repo, pub, *extra, check=True, name="demo"):
        return self.run_b("module", "add", name, "--from", str(repo), "--publisher", pub, "--principal", "op", *extra,
                          check=check)

    def sync_to_b(self):
        (self.b / "journal").mkdir(exist_ok=True)
        for f in (self.a / "journal").glob("*.jsonl"):
            shutil.copyfile(f, self.b / "journal" / f"from-a-{f.name}")

    def b_modules(self, name="demo"):
        return self.mods["b"] / name

    def b_key(self, name="demo"):
        return _keys.key_dir(self.b) / "modules" / name

    def b_journal(self):
        return sorted((p.name, p.read_text()) for p in (self.b / "journal").glob("*.jsonl")) if (self.b / "journal").exists() else []


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    return Nodes(tmp_path, monkeypatch)


def _admit_line(out):
    m = re.search(r"^\s+(hive-mind group admit \S+ --module \S+ --principal \S+)$", out, re.M)
    assert m, out
    return m.group(1).split()[1:]          # ["group", "admit", ...]


def _commit(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _nothing_installed(n, units_calls=None, unit_dir=None):
    assert not n.b_modules().exists()
    assert not (n.b / ".modules.json").exists()
    if unit_dir is not None:
        assert not list(unit_dir.glob("hive-module-demo*")) if unit_dir.exists() else True


# ── the first run stages and installs nothing ───────────────────────────────────────────────────────────────────

@LINUX_ONLY
def test_add_without_the_owner_key_stages_prints_the_owners_line_and_installs_nothing(nodes, tmp_path, units):
    unit_dir, calls = units
    pub = _publish(tmp_path / "repo", service={"command": ["run.sh"], "restart": "always"})
    before = nodes.b_journal()
    r = nodes.add_b(tmp_path / "repo", pub)
    dev = json.loads((nodes.mods["b"] / ".demo.pending.json").read_text())["device_id"]
    for field in (dev, "--module demo", "--principal op", hive_modules.fingerprint(pub), _commit(tmp_path / "repo")):
        assert field in r.stdout, field
    assert "NOT installed" in r.stdout
    _nothing_installed(nodes, unit_dir=unit_dir)
    assert not calls() and not list(unit_dir.glob("hive-module-demo*")) if unit_dir.exists() else not calls()
    assert (nodes.mods["b"] / ".demo.staging").is_dir() and nodes.b_key().exists()     # staged, key minted
    assert nodes.b_journal() == before                                                  # B wrote no governance entry
    assert _gov(nodes.a)[1]["modules"] == {}                                            # nor did anything admit it


@LINUX_ONLY
def test_the_second_run_installs_only_once_the_owners_admit_has_reached_the_node(nodes, tmp_path, units):
    unit_dir, calls = units
    pub = _publish(tmp_path / "repo", service={"command": ["run.sh"], "restart": "always"})
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    dev = json.loads((nodes.mods["b"] / ".demo.pending.json").read_text())["device_id"]
    nodes.sync_to_b()
    r = nodes.add_b(tmp_path / "repo", pub, check=False)          # A's journal has no admit of the module yet
    assert r.returncode == 1 and "has not admitted" in r.stderr and dev in r.stderr
    _nothing_installed(nodes)
    nodes.run_a(*_admit_line(out))
    r = nodes.add_b(tmp_path / "repo", pub, check=False)          # admitted on A, not yet synced to B
    assert r.returncode == 1 and "has not admitted" in r.stderr
    _nothing_installed(nodes)
    nodes.sync_to_b()
    r = nodes.add_b(tmp_path / "repo", pub)
    assert "installed module demo 1.0.0" in r.stdout and dev in r.stdout
    assert (nodes.b_modules() / "run.sh").exists() and not (nodes.b_modules() / ".git").exists()
    assert json.loads((nodes.b_modules() / "config").read_text()) == {"poll": "30"}
    rec = json.loads((nodes.b / ".modules.json").read_text())["demo"]
    assert rec["device_id"] == dev and rec["publisher"] == pub and rec["commit"] == _commit(tmp_path / "repo")
    assert not (nodes.mods["b"] / ".demo.pending.json").exists() and not (nodes.mods["b"] / ".demo.staging").exists()
    assert list(unit_dir.glob("hive-module-demo*"))               # the unit is written only now
    assert nodes.b_key().exists()
    # the admit that made it count is the owner's, with the module marker
    assert _gov(nodes.a)[1]["modules"] == {dev: "demo"}


@LINUX_ONLY
def test_the_admit_must_name_this_module_and_this_device(nodes, tmp_path):
    """An admit of the staged device as a plain device, or a module admit of another device, does not complete it."""
    pub = _publish(tmp_path / "repo")
    nodes.add_b(tmp_path / "repo", pub)
    dev = json.loads((nodes.mods["b"] / ".demo.pending.json").read_text())["device_id"]
    nodes.run_a("group", "admit", dev, "--principal", "op")                  # admitted, but not as a module
    nodes.sync_to_b()
    r = nodes.add_b(tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "has not admitted" in r.stderr
    _nothing_installed(nodes)


@LINUX_ONLY
def test_a_staged_tree_changed_between_the_runs_is_refused(nodes, tmp_path):
    pub = _publish(tmp_path / "repo")
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()
    (nodes.mods["b"] / ".demo.staging" / "run.sh").write_text("#!/bin/sh\necho owned\n")
    r = nodes.add_b(tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "digest" in r.stderr
    _nothing_installed(nodes)


@LINUX_ONLY
def test_a_staged_manifest_swapped_for_another_publishers_is_refused(nodes, tmp_path):
    """The staged tree is re-signed by a different key and is internally consistent: only the digest recorded at the
    first run, and the pin, catch it."""
    pub = _publish(tmp_path / "repo")
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()
    forged = _publish(tmp_path / "forged", seed=OTHER_SEED)
    for f in ("module.json", "module.json.sig"):
        shutil.copyfile(tmp_path / "forged" / f, nodes.mods["b"] / ".demo.staging" / f)
    for f in (tmp_path / "forged" / "run.sh", tmp_path / "forged" / "hooks" / "stop"):
        shutil.copyfile(f, nodes.mods["b"] / ".demo.staging" / f.relative_to(tmp_path / "forged"))
    r = nodes.add_b(tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "not the one this node verified" in r.stderr
    _nothing_installed(nodes)
    assert forged != pub


@LINUX_ONLY
def test_a_staged_manifest_re_signed_by_the_same_publisher_is_refused(nodes, tmp_path):
    """Same key, a valid signature and matching files, but not the manifest this node verified (here a different
    version): the signature check passes, so only the digest recorded at the first run catches it."""
    pub = _publish(tmp_path / "repo")
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()
    _publish(tmp_path / "again", version="9.9.9")
    for f in ("module.json", "module.json.sig"):
        shutil.copyfile(tmp_path / "again" / f, nodes.mods["b"] / ".demo.staging" / f)
    r = nodes.add_b(tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "not the one this node verified" in r.stderr
    _nothing_installed(nodes)


@LINUX_ONLY
def test_the_second_run_must_name_the_repository_the_first_staged(nodes, tmp_path):
    pub = _publish(tmp_path / "repo")
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()
    _publish(tmp_path / "other")
    r = nodes.run_b("module", "add", "demo", "--from", str(tmp_path / "other"), "--publisher", pub, check=False)
    assert r.returncode == 1 and "staged from" in r.stderr
    _nothing_installed(nodes)


@LINUX_ONLY
def test_a_module_that_fails_the_check_stages_nothing_on_a_device_without_the_owner_key(nodes, tmp_path):
    pub = _publish(tmp_path / "repo", tamper="run.sh")
    r = nodes.add_b(tmp_path / "repo", pub, check=False)
    assert r.returncode == 1
    assert not (nodes.mods["b"] / ".demo.staging").exists() and not (nodes.mods["b"] / ".demo.pending.json").exists()
    assert not nodes.b_key().exists()


@LINUX_ONLY
def test_a_device_with_no_principal_must_be_given_one(nodes, tmp_path):
    pub = _publish(tmp_path / "repo")
    r = nodes.run_b("module", "add", "demo", "--from", str(tmp_path / "repo"), "--publisher", pub, check=False)
    assert r.returncode == 1 and "--principal" in r.stderr and not nodes.b_key().exists()


# ── --abort ──────────────────────────────────────────────────────────────────────────────────────────────────────

@LINUX_ONLY
def test_abort_removes_the_staging_the_record_and_the_key(nodes, tmp_path):
    pub = _publish(tmp_path / "repo")
    nodes.add_b(tmp_path / "repo", pub)
    assert nodes.b_key().exists()
    nodes.run_b("module", "add", "--abort", "demo")
    assert not nodes.b_key().exists()
    assert not (nodes.mods["b"] / ".demo.staging").exists() and not (nodes.mods["b"] / ".demo.pending.json").exists()
    _nothing_installed(nodes)
    r = nodes.run_b("module", "add", "--abort", "demo", check=False)
    assert r.returncode == 1 and "no pending add" in r.stderr
    nodes.add_b(tmp_path / "repo", pub)                           # and the name is free to stage again


@LINUX_ONLY
def test_abort_never_touches_an_installed_module(nodes, tmp_path):
    pub = _publish(tmp_path / "repo")
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()
    nodes.add_b(tmp_path / "repo", pub)
    r = nodes.run_b("module", "add", "--abort", "demo", check=False)
    assert r.returncode == 1 and "is installed" in r.stderr
    assert nodes.b_key().exists() and (nodes.b_modules() / "run.sh").exists()


@LINUX_ONLY
def test_the_owner_machine_refuses_to_add_over_a_pending_stage_and_stages_nothing_itself(nodes, tmp_path):
    """The owner path is unchanged: it admits in the same command. A pending record from a device without the key
    is not silently overwritten."""
    pub = _publish(tmp_path / "repo")
    (nodes.mods["a"]).mkdir(parents=True, exist_ok=True)
    (nodes.mods["a"] / ".demo.pending.json").write_text(json.dumps({"device_id": "k1:x", "principal": "op"}))
    r = nodes.run_a("module", "add", "demo", "--from", str(tmp_path / "repo"), "--publisher", pub, check=False)
    assert r.returncode == 1 and "pending add" in r.stderr
    assert _gov(nodes.a)[1]["modules"] == {}


# ── doctor, update, remove, quota ───────────────────────────────────────────────────────────────────────────────

@LINUX_ONLY
def test_doctor_fix_never_completes_a_pending_add(nodes, tmp_path, units):
    unit_dir, calls = units
    pub = _publish(tmp_path / "repo", service={"command": ["run.sh"], "restart": "always"})
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()                                             # admitted and synced: the add could now complete
    nodes.run_b("doctor", "--fix", check=False)
    _nothing_installed(nodes)
    assert not list(unit_dir.glob("hive-module-demo*")) if unit_dir.exists() else True
    assert (nodes.mods["b"] / ".demo.staging").is_dir()


def _installed_on_b(nodes, tmp_path, **kw):
    pub = _publish(tmp_path / "repo", **kw)
    out = nodes.add_b(tmp_path / "repo", pub).stdout
    nodes.run_a(*_admit_line(out))
    nodes.sync_to_b()
    nodes.add_b(tmp_path / "repo", pub)
    return pub, json.loads((nodes.b / ".modules.json").read_text())["demo"]["device_id"]


@LINUX_ONLY
def test_update_works_without_the_owner_key_and_changes_no_membership_or_quota(nodes, tmp_path):
    pub, dev = _installed_on_b(nodes, tmp_path)
    nodes.run_b("module", "quota", "demo", "--per-hour", "5", check=False)      # refused below
    state0 = json.loads((nodes.b / ".modules.json").read_text())
    journal0 = nodes.b_journal()
    _publish(tmp_path / "repo", version="1.1.0", files={"run.sh": "#!/bin/sh\necho v2\n", "hooks/stop": "#!/bin/sh\nexit 0\n"})
    r = nodes.run_b("module", "update", "demo")
    assert "updated module demo to 1.1.0" in r.stdout
    assert "v2" in (nodes.b_modules() / "run.sh").read_text()
    rec = json.loads((nodes.b / ".modules.json").read_text())["demo"]
    assert rec["version"] == "1.1.0" and rec["device_id"] == dev and rec["publisher"] == pub
    assert rec["owner_quota"] == state0["demo"]["owner_quota"] == {} and rec["quota"] == state0["demo"]["quota"]
    assert nodes.b_journal() == journal0                           # no governance written
    assert _gov(nodes.a)[1]["modules"] == {dev: "demo"}            # and the owner's view is untouched


@LINUX_ONLY
def test_update_without_the_owner_key_still_pins_the_publisher_and_checks_the_tree(nodes, tmp_path):
    pub, _dev = _installed_on_b(nodes, tmp_path)
    before = (nodes.b_modules() / "run.sh").read_text()
    _publish(tmp_path / "repo", version="2.0.0", seed=OTHER_SEED)
    r = nodes.run_b("module", "update", "demo", check=False)
    assert r.returncode == 1 and "pinned" in r.stderr
    _publish(tmp_path / "repo2", name="demo", version="3.0.0", tamper="run.sh")
    r = nodes.run_b("module", "update", "demo", "--from", str(tmp_path / "repo2"), check=False)
    assert r.returncode == 1
    assert (nodes.b_modules() / "run.sh").read_text() == before
    assert json.loads((nodes.b / ".modules.json").read_text())["demo"]["version"] == "1.0.0"


@LINUX_ONLY
def test_quota_still_needs_the_owner_key(nodes, tmp_path):
    _installed_on_b(nodes, tmp_path)
    r = nodes.run_b("module", "quota", "demo", "--per-hour", "5", check=False)
    assert r.returncode == 1 and "owner key" in r.stderr
    assert json.loads((nodes.b / ".modules.json").read_text())["demo"]["owner_quota"] == {}


@LINUX_ONLY
def test_remove_without_the_owner_key_deletes_locally_and_says_the_device_stays_admitted(nodes, tmp_path, units):
    unit_dir, calls = units
    pub, dev = _installed_on_b(nodes, tmp_path, service={"command": ["run.sh"], "restart": "always"})
    assert list(unit_dir.glob("hive-module-demo*"))
    r = nodes.run_b("module", "remove", "demo")
    assert f"hive-mind group revoke {dev}" in r.stdout and "STAYS ADMITTED" in r.stdout
    assert not nodes.b_modules().exists() and not nodes.b_key().exists()
    assert "demo" not in (json.loads((nodes.b / ".modules.json").read_text()) if (nodes.b / ".modules.json").exists() else {})
    assert not list(unit_dir.glob("hive-module-demo*"))
    assert any("disable --now" in c for c in calls())             # the unit was stopped
    assert dev in _gov(nodes.a)[1]["admitted"]                     # nothing was revoked by this command
    nodes.run_a("group", "revoke", dev)                            # the owner's line does it
    assert dev not in _gov(nodes.a)[1]["admitted"]
