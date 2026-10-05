"""2.1 plan PR 6 (M1): `hive-mind module add | remove | update | list | quota`. Each security check has a test that
fails without it: the signature, the digests, the exact file set, the publisher pin, the platform check that runs
before anything is fetched or minted, and the quota ceiling. Drives the real `hive-mind` CLI against a module
repository built in a temp git repo."""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import _keys  # noqa: E402
import ed25519  # noqa: E402
import hive_modules  # noqa: E402
from test_succession import _run, _gov, _device_id  # noqa: E402

SEED = bytes(range(1, 33))
OTHER_SEED = bytes(range(101, 133))


def _b64(b):
    return base64.b64encode(b).decode()


def _publish(repo, name="demo", version="1.0.0", seed=SEED, extra=None, files=None, tamper=None, symlink=None,
             unlisted=None, sign_with=None, **manifest_extra):
    """Write a signed module into the git repo `repo` and commit it. `tamper` edits a file after the manifest was
    signed; `unlisted` adds a tracked file the manifest does not name; `sign_with` signs with another key."""
    repo.mkdir(parents=True, exist_ok=True)
    body = files or {"run.sh": "#!/bin/sh\nexit 0\n", "hooks/stop": "#!/bin/sh\nexit 0\n"}
    for rel, text in body.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
        (repo / rel).chmod(0o755)
    m = {"name": name, "version": version, "min_core": "2.0", "publisher": _b64(ed25519.pub_from_seed(seed)),
         "files": {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in body.items()},
         "hooks": ["stop"], "config": {"poll": "30"}, **manifest_extra}
    raw = json.dumps(m, indent=1).encode()
    (repo / "module.json").write_bytes(raw)
    (repo / "module.json.sig").write_text(_b64(ed25519.sign(raw, sign_with or seed)))
    if tamper:
        (repo / tamper).write_text("#!/bin/sh\necho owned\n")
    if unlisted:
        (repo / unlisted).write_text("stray")
    if symlink:
        (repo / symlink).unlink(missing_ok=True)
        os.symlink("/etc/passwd", repo / symlink)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A", "-f"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m"],
                   check=True)
    return _b64(ed25519.pub_from_seed(seed))


@pytest.fixture
def hive(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_MODULES_DIR", str(tmp_path / "modules"))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    home = tmp_path / "A"
    _run(home, "config", "identity", "init")
    _run(home, "owner", "init")
    _run(home, "group", "admit", _device_id(home), "--principal", "op")
    return home


def _add(home, repo, pub, name="demo", check=True, *extra):
    return _run(home, "module", "add", name, "--from", str(repo), "--publisher", pub, *extra, check=check)


def _state(home):
    return json.loads((home / ".modules.json").read_text())


def _key_dir_has_module(home, name="demo"):
    return (_keys.key_dir(home) / "modules" / name).exists()


def test_add_installs_verified_files_mints_a_key_and_admits_it_as_a_module(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    r = _add(hive, tmp_path / "repo", pub)
    assert "installed module demo 1.0.0" in r.stdout
    d = tmp_path / "modules" / "demo"
    assert (d / "run.sh").read_text().startswith("#!/bin/sh") and (d / "module.json").exists()
    assert not (d / ".git").exists()
    assert json.loads((d / "config").read_text()) == {"poll": "30"}
    rec = _state(hive)["demo"]
    assert rec["publisher"] == pub and rec["version"] == "1.0.0"
    _, gov = _gov(hive)
    dev = rec["device_id"]
    assert gov["modules"] == {dev: "demo"} and dev in gov["admitted"]
    assert gov["principals"][dev] == "op"                      # the operator's principal: cap_self bounds it
    assert _key_dir_has_module(hive)


@pytest.mark.parametrize("how", ["tamper", "unlisted", "symlink", "badsig"])
def test_a_module_that_fails_the_check_installs_nothing(hive, tmp_path, how):
    kw = {"tamper": dict(tamper="run.sh"), "unlisted": dict(unlisted="extra.sh"),
          "symlink": dict(symlink="hooks/stop"), "badsig": dict(sign_with=OTHER_SEED)}[how]
    pub = _publish(tmp_path / "repo", **kw)
    before = sorted(p.read_text() for p in (hive / "journal").glob("*.jsonl"))
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and r.stderr.startswith("hive-mind module:")
    assert not (tmp_path / "modules" / "demo").exists()
    assert not (hive / ".modules.json").exists()
    assert not _key_dir_has_module(hive)                       # nothing was minted
    assert sorted(p.read_text() for p in (hive / "journal").glob("*.jsonl")) == before   # nothing was admitted
    assert _gov(hive)[1]["modules"] == {}


def test_the_publisher_must_be_named_or_confirmed(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    r = _run(hive, "module", "add", "demo", "--from", str(tmp_path / "repo"), check=False)    # no tty, no --publisher
    assert r.returncode == 1 and "--publisher" in r.stderr and pub in r.stdout
    other = _b64(ed25519.pub_from_seed(OTHER_SEED))
    r = _run(hive, "module", "add", "demo", "--from", str(tmp_path / "repo"), "--publisher", other, check=False)
    assert r.returncode == 1 and "does not match" in r.stderr
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(hive)


def test_a_manifest_for_another_name_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", name="other")
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "not 'demo'" in r.stderr and not _key_dir_has_module(hive)


def test_a_module_that_needs_a_newer_core_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", min_core="9.0")
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "9.0" in r.stderr and not (tmp_path / "modules" / "demo").exists()


def test_a_manifest_may_ask_for_less_quota_but_never_more(hive, tmp_path):
    pub = _publish(tmp_path / "repo", quota={"per_hour": 600})
    r = _add(hive, tmp_path / "repo", pub, check=False)
    assert r.returncode == 1 and "never more" in r.stderr and not _key_dir_has_module(hive)
    pub = _publish(tmp_path / "repo2", quota={"per_hour": 5})
    _add(hive, tmp_path / "repo2", pub)
    assert _state(hive)["demo"]["quota"] == {"per_hour": 5}




@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_only_linux_has_a_module_backend(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    ok, why = hive_modules.platform_supported()
    assert not ok and "not supported on this platform yet" in why


def test_the_cli_says_not_supported_and_leaves_nothing_behind(hive, tmp_path, monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "1")                     # Termux: a Linux kernel with no module backend
    r = _run(hive, "module", "add", "demo", "--from", str(tmp_path / "no-such-repo"), "--publisher", "x", check=False)
    assert r.returncode == 1 and "not supported on this platform yet" in r.stderr
    assert not (tmp_path / "modules").exists() and not (hive / ".modules.json").exists()
    assert not _key_dir_has_module(hive)
    assert "no-such-repo" not in r.stderr                         # it never tried to fetch


def test_remove_revokes_the_device_and_leaves_the_journal(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    dev = _state(hive)["demo"]["device_id"]
    journal = sorted(p.name for p in (hive / "journal").glob("*.jsonl"))
    _run(hive, "module", "remove", "demo")
    assert dev not in _gov(hive)[1]["admitted"]
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(hive)
    assert "demo" not in (_state(hive) if (hive / ".modules.json").exists() else {})
    assert sorted(p.name for p in (hive / "journal").glob("*.jsonl")) == journal   # the journal keeps every entry
    r = _run(hive, "module", "remove", "demo", check=False)
    assert r.returncode == 1 and "not installed" in r.stderr


def test_update_swaps_atomically_keeps_the_key_the_device_and_local_config(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    d = tmp_path / "modules" / "demo"
    cfg = json.loads((d / "config").read_text())
    cfg["poll"] = "99"
    (d / "config").write_text(json.dumps(cfg))
    dev = _state(hive)["demo"]["device_id"]
    _publish(tmp_path / "repo", version="1.1.0", files={"run.sh": "#!/bin/sh\necho v2\n", "hooks/stop": "#!/bin/sh\n"},
             config={"poll": "30", "new": "x"})
    r = _run(hive, "module", "update", "demo")
    assert "to 1.1.0" in r.stdout
    assert "v2" in (d / "run.sh").read_text() and not (tmp_path / "modules" / ".demo.old").exists()
    assert json.loads((d / "config").read_text()) == {"poll": "99", "new": "x"}     # the node's value survives
    assert _state(hive)["demo"]["device_id"] == dev and dev in _gov(hive)[1]["admitted"]


def test_an_update_signed_by_a_different_publisher_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    _publish(tmp_path / "repo", version="2.0.0", seed=OTHER_SEED)
    r = _run(hive, "module", "update", "demo", check=False)
    assert r.returncode == 1 and "pinned" in r.stderr
    assert _state(hive)["demo"]["version"] == "1.0.0"
    assert "v2" not in (tmp_path / "modules" / "demo" / "run.sh").read_text()


def test_a_tampered_update_leaves_the_installed_copy_alone(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    before = (tmp_path / "modules" / "demo" / "run.sh").read_text()
    _publish(tmp_path / "repo", version="1.1.0", tamper="run.sh")
    assert _run(hive, "module", "update", "demo", check=False).returncode == 1
    assert (tmp_path / "modules" / "demo" / "run.sh").read_text() == before


def test_reset_and_update_leave_the_modules_directory_outside_the_checkout(hive, tmp_path):
    """`hive-mind reset` force-aligns the checkout and `hv verify` reads git-tracked files only: the modules
    directory is in neither. The default is under the home directory, never under HIVE_HOME."""
    assert not str(hive_modules.modules_dir()).startswith(str(hive))
    default = Path.home() / ".hive" / "modules"
    os.environ.pop("HIVE_MODULES_DIR", None)
    try:
        assert hive_modules.modules_dir() == default
    finally:
        os.environ["HIVE_MODULES_DIR"] = str(tmp_path / "modules")


def test_list_shows_the_fields_and_flags_a_tampered_install(hive, tmp_path):
    assert "No modules installed" in _run(hive, "module", "list").stdout
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    out = _run(hive, "module", "list").stdout
    row = next(l for l in out.splitlines() if l.startswith("demo"))
    assert "1.0.0" in row and hive_modules.fingerprint(pub) in row and "valid" in row and "admitted" in row
    (tmp_path / "modules" / "demo" / "run.sh").write_text("#!/bin/sh\necho owned\n")
    out = _run(hive, "module", "list").stdout
    assert "INVALID" in out and "run.sh" in out


def test_quota_raises_a_limit_and_the_api_reads_it(hive, tmp_path, monkeypatch):
    pub = _publish(tmp_path / "repo", quota={"per_hour": 5})
    _add(hive, tmp_path / "repo", pub)
    _run(hive, "module", "quota", "demo", "--per-day", "900", "--per-hour", "100")
    import hive_module_api as api
    monkeypatch.setattr(api.daemon, "hv", type("H", (), {"HIVE_HOME": hive})(), raising=False)
    assert api.limits_for("demo") == {**api.QUOTA_DEFAULTS, "per_hour": 100, "per_day": 900}
    assert api.limits_for("other") == api.QUOTA_DEFAULTS


def test_a_hand_edited_manifest_ask_cannot_raise_a_limit(hive, tmp_path, monkeypatch):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    st = _state(hive)
    st["demo"]["quota"] = {"per_hour": 10 ** 6, "per_day": 7}
    (hive / ".modules.json").write_text(json.dumps(st))
    import hive_module_api as api
    monkeypatch.setattr(api.daemon, "hv", type("H", (), {"HIVE_HOME": hive})(), raising=False)
    lim = api.limits_for("demo")
    assert lim["per_hour"] == api.QUOTA_DEFAULTS["per_hour"] and lim["per_day"] == 7


def test_a_device_without_the_owner_key_refuses_before_touching_anything(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    b = tmp_path / "B"
    _run(b, "config", "identity", "init")
    r = _run(b, "module", "add", "demo", "--from", str(tmp_path / "repo"), "--publisher", pub, "--principal", "op",
             check=False)
    assert r.returncode == 1 and "owner key" in r.stderr
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(b)


def test_the_data_plane_cannot_reach_module_management():
    """S2: `hv` imports neither this module nor the owner key, and this module reaches the owner steps only
    through the control plane's library (it imports no `ownerkey`)."""
    import ast

    def imports(path):
        got = set()
        for n in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(n, ast.Import):
                got |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                got.add(n.module.split(".")[0])
        return got
    assert "hive_modules" not in imports(PROJECT / "hv")
    assert "ownerkey" not in imports(PROJECT / "hive_modules.py")
    r = subprocess.run([sys.executable, str(PROJECT / "hv"), "module", "list"], capture_output=True, text=True)
    assert r.returncode != 0


def test_update_without_the_owner_key_leaves_the_installed_module_alone(hive, tmp_path):
    pub = _publish(tmp_path / "repo")
    _add(hive, tmp_path / "repo", pub)
    d = tmp_path / "modules" / "demo"
    before = (d / "run.sh").read_text()
    _publish(tmp_path / "repo", version="9.9.9", files={"run.sh": "#!/bin/sh\necho DOWNGRADED\n", "hooks/stop": "#!/bin/sh\n"})
    moved = [f for f in _keys.key_dir(hive).glob("owner-key*") if f.is_file()]
    assert moved
    for f in moved:
        f.rename(f.with_name(f.name + ".aside"))
    r = _run(hive, "module", "update", "demo", check=False)
    assert r.returncode == 1 and "owner key" in r.stderr
    assert (d / "run.sh").read_text() == before and _state(hive)["demo"]["version"] == "1.0.0"


def test_a_signed_manifest_with_an_unknown_top_level_key_is_refused(hive, tmp_path):
    pub = _publish(tmp_path / "repo", user="root")
    r = _add(hive, tmp_path / "repo", pub, "demo", False)
    assert r.returncode == 1 and "does not know" in r.stderr
    assert not (tmp_path / "modules" / "demo").exists() and not _key_dir_has_module(hive)
