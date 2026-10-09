"""Private keys live outside the working tree (2.0 PR 3a, private #27).

`HIVE_HOME` is the git checkout, the directory every agent CLI runs in and some index, so a 0600 seed in
it is still one `cat` away from any tool running as the user. The seeds move to a key directory (0700):
`$HIVE_KEY_DIR`, else the path recorded in `$HIVE_HOME/.key-dir`, else a default derived from the
checkout's path. Legacy root keys keep working, with an advisory; each plane's `doctor --fix` moves its
own: `hv` the device key, `hive-mind` the owner key.
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

POSIX = os.name != "nt"


def _env(home, **extra):
    env = dict(os.environ, HIVE_HOME=str(home), HIVE_IDENTITY_STASH=str(Path(home).parent / "stash"),
               HIVE_OWNER_PASSPHRASE="testpass", **extra)
    env.pop("HIVE_KEY_DIR", None) if "HIVE_KEY_DIR" not in extra else None
    return env


def _hv(home, *argv, **extra):
    return subprocess.run([sys.executable, str(PROJECT / "hv"), *argv], env=_env(home, **extra),
                          capture_output=True, text=True)


def _ctl(home, *argv, **extra):
    return subprocess.run([sys.executable, str(PROJECT / "hivemind_ctl.py"), *argv], env=_env(home, **extra),
                          capture_output=True, text=True)


def _lib(home, monkeypatch, name="hv_keydir"):
    monkeypatch.setenv("HIVE_HOME", str(home))
    monkeypatch.delenv("HIVE_KEY_DIR", raising=False)
    loader = importlib.machinery.SourceFileLoader(name, str(PROJECT / "hv"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


def _key_dir(home):
    return Path((Path(home) / ".key-dir").read_text().strip())


def _in_tree(home):
    return sorted(p.name for p in Path(home).iterdir() if p.name in (".device-key", ".owner-key", ".owner-pub"))


def _owner_seed_b64(home):
    """The owner seed of `home`, as base64, from an unencrypted `owner export` (the key is sealed at rest
    since 2.0 PR 3b, so there is no plaintext file to read)."""
    out = Path(home).parent / f"{Path(home).name}-export.json"
    assert _ctl(home, "owner", "export", "--plaintext", "--out", str(out)).returncode == 0
    return json.loads(out.read_text())["seed"]


def _check(home, name, *argv, script=_hv):
    r = script(home, "doctor", "--format", "json", *argv)
    return next(c for c in json.loads(r.stdout)["checks"] if c["name"] == name)


# ── where new keys land ──────────────────────────────────────────────────────────────────────────────

def test_new_keys_land_outside_the_working_tree(tmp_path):
    home = tmp_path / "h"
    assert _hv(home, "config", "identity", "init").returncode == 0
    assert _ctl(home, "owner", "init").returncode == 0
    assert _in_tree(home) == [], "a seed was written into the working tree"
    kd = _key_dir(home)
    assert not str(kd).startswith(str(home)), "the key directory must be outside the checkout"
    assert (kd / "device-key").exists() and (kd / "owner-key.sealed").exists() and (kd / "owner-pub").exists()
    if POSIX:
        assert stat.S_IMODE(kd.stat().st_mode) == 0o700
        assert stat.S_IMODE((kd / "device-key").stat().st_mode) == 0o600
        assert stat.S_IMODE((kd / "owner-key.sealed").stat().st_mode) == 0o600
    assert "this device holds the owner key" in _hv(home, "owner", "show").stdout


def test_hive_key_dir_is_honoured(tmp_path):
    home, kd = tmp_path / "h", tmp_path / "chosen-keys"
    assert _hv(home, "config", "identity", "init", HIVE_KEY_DIR=str(kd)).returncode == 0
    assert (kd / "device-key").exists() and _in_tree(home) == []


def test_a_renamed_checkout_keeps_its_keys(tmp_path, monkeypatch):
    home = tmp_path / "h"
    assert _hv(home, "config", "identity", "init").returncode == 0
    kd = _key_dir(home)
    seed = _lib(home, monkeypatch, "hv_before_move")._device_seed()
    moved = tmp_path / "renamed"
    shutil.move(str(home), str(moved))
    m = _lib(moved, monkeypatch, "hv_after_move")
    assert m.KEY_DIR == kd, "the pointer in .key-dir, not the checkout's new path, names the key directory"
    assert seed is not None and m._device_seed() == seed, "a renamed checkout must still find its device key"


# ── legacy root keys: still work, advised, relocated by the right plane ──────────────────────────────

def _legacy_node(tmp_path):
    """A node from before PR 3a: device and owner keys at the root of the checkout, both plaintext."""
    src = tmp_path / "fresh"
    assert _hv(src, "config", "identity", "init").returncode == 0 and _ctl(src, "owner", "init").returncode == 0
    kd = _key_dir(src)
    shutil.copy2(kd / "device-key", tmp_path / "device-seed")
    shutil.copy2(kd / "device-key", src / ".device-key")
    (src / ".owner-key").write_text(_owner_seed_b64(src) + "\n")
    os.chmod(src / ".owner-key", 0o600)
    shutil.copy2(kd / "owner-pub", src / ".owner-pub")
    for f in ("device-key", "owner-key.sealed", "owner-pub"):
        (kd / f).unlink()
    (src / ".key-dir").unlink()
    kd.rmdir()
    return src


def test_legacy_keys_still_work_and_are_advised(tmp_path):
    home = _legacy_node(tmp_path)
    assert "this device holds the owner key" in _hv(home, "owner", "show").stdout
    c = _check(home, "keyperm")
    assert c["status"] == "warn" and "still in the working tree" in c["detail"]


def test_each_plane_relocates_its_own_keys(tmp_path):
    home = _legacy_node(tmp_path)
    device_id = _hv(home, "whoami").stdout.splitlines()[0].split()[1]
    r = _hv(home, "doctor", "--fix")
    assert "moved device-key" in r.stdout and "owner-key, owner-pub are still in the working tree" in r.stdout
    assert _in_tree(home) == [".owner-key", ".owner-pub"], "hv must move only the device key"
    r = _ctl(home, "doctor", "--fix")
    assert "moved" in r.stdout and "owner-key" in r.stdout
    assert _in_tree(home) == []
    kd = _key_dir(home)
    assert (kd / "owner-key").exists() and (kd / "owner-pub").exists()
    assert "this device holds the owner key" in _hv(home, "owner", "show").stdout
    assert _check(home, "keyperm")["status"] == "ok"
    assert _hv(home, "whoami").stdout.splitlines()[0].split()[1] == device_id, "relocation changed identity"
    assert (kd / "device-key").read_bytes() == (tmp_path / "device-seed").read_bytes()


def test_relocation_repoints_the_running_process(tmp_path, monkeypatch):
    """The rest of a `doctor --fix` run reads the moved key, never the path it just emptied: a stale
    DEVICE_KEY_PATH reads as "no device key", and `_ensure_device_key` would mint a new identity."""
    home = _legacy_node(tmp_path)
    m = _lib(home, monkeypatch)
    seed = m._device_seed()
    assert seed is not None and m.DEVICE_KEY_PATH == m.LEGACY_DEVICE_KEY_PATH
    moved, _other, problems = m._relocate_legacy_keys(dry=False)
    assert moved == ["device-key"] and problems == []
    assert m.DEVICE_KEY_PATH == m.KEY_DIR / "device-key"
    assert m._device_seed() == seed
    assert m._ensure_device_key() is False, "a relocated key must not look missing"


def test_an_interrupted_move_finishes_instead_of_reporting_a_clash(tmp_path):
    home = _legacy_node(tmp_path)
    kd = home.parent / "chosen"
    kd.mkdir(mode=0o700)
    shutil.copy2(home / ".device-key", kd / "device-key")         # the copy landed; the unlink did not
    r = _hv(home, "doctor", "--fix", HIVE_KEY_DIR=str(kd))
    assert "could NOT move" not in r.stdout and not (home / ".device-key").exists()


def test_relocation_never_overwrites_a_key_already_in_the_key_dir(tmp_path):
    home = tmp_path / "h"
    assert _hv(home, "config", "identity", "init").returncode == 0
    (home / ".device-key").write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    os.chmod(home / ".device-key", 0o600)
    r = _hv(home, "doctor", "--fix")
    assert "could NOT move" in r.stdout and "both copies exist" in r.stdout
    assert (home / ".device-key").exists(), "the clashing legacy copy is left for the operator"


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_an_open_key_directory_is_a_hard_failure(tmp_path):
    home = tmp_path / "h"
    assert _hv(home, "config", "identity", "init").returncode == 0
    os.chmod(_key_dir(home), 0o755)
    c = _check(home, "keyperm")
    assert c["status"] == "fail" and "must be 0700" in c["detail"]


# ── presence: honest without reading the seed (the #159 review) ──────────────────────────────────────

@pytest.mark.parametrize("form", ["sealed", "plaintext"])
def test_presence_answers_held_only_for_a_plausible_key_file(tmp_path, monkeypatch, form):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    if form == "plaintext":                               # a node not yet `owner seal`ed
        seed = _owner_seed_b64(home)
        (_key_dir(home) / "owner-key.sealed").unlink()
        (_key_dir(home) / "owner-key").write_text(seed + "\n")
    m = _lib(home, monkeypatch)
    gov = m._governance_state(m.merkle.read_all_entries(m.JOURNAL_DIR))
    key = m.OWNER_SEALED_PATH if form == "sealed" else m.OWNER_KEY_PATH
    real = key.read_bytes()
    assert m._owner_key_state(gov) == "held"
    too_long = b"y" * (5000 if form == "sealed" else 200)
    for fake, why in ((b"", "empty"), (b"x" * 10, "truncated"), (too_long, "too long")):
        key.write_bytes(fake)
        assert m._owner_key_state(gov) == "present", f"a {why} file in the key's place is not a key"
    key.unlink()
    key.mkdir()
    assert m._owner_key_state(gov) == "present", "a directory in the key's place is not a key"
    key.rmdir()
    key.write_bytes(real)
    assert m._owner_key_state(gov) == "held"


@pytest.mark.parametrize("which, size", [("OWNER_SEALED_PATH", 240), ("OWNER_KEY_PATH", 44)])
def test_presence_rejects_a_non_regular_file_of_a_key_s_size(tmp_path, monkeypatch, which, size):
    """A FIFO, device or directory whose size happens to be a key's is still not a key: the size alone is
    not the test, the file type is too. For each form, with the other form absent."""
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    m = _lib(home, monkeypatch)
    gov = m._governance_state(m.merkle.read_all_entries(m.JOURNAL_DIR))
    if which == "OWNER_KEY_PATH":
        m.OWNER_SEALED_PATH.unlink()

    class NotAFile:
        def stat(self):
            return os.stat_result((stat.S_IFIFO | 0o600, 0, 0, 1, 0, 0, size, 0, 0, 0))

    monkeypatch.setattr(m, which, NotAFile())
    assert m._owner_key_state(gov) == "present"


@pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="needs POSIX modes and a non-root user")
def test_an_unsearchable_key_directory_is_unknown_not_absent(tmp_path, monkeypatch):
    home = tmp_path / "h"
    assert _ctl(home, "owner", "init").returncode == 0
    m = _lib(home, monkeypatch)
    gov = m._governance_state(m.merkle.read_all_entries(m.JOURNAL_DIR))
    kd = m.KEY_DIR
    os.chmod(kd, 0)
    try:
        assert m._owner_key_state(gov) == "unknown"
    finally:
        os.chmod(kd, 0o700)


# ── public ids labelled public (#27 part 1) ──────────────────────────────────────────────────────────

def test_whoami_and_doctor_label_ids_public_and_never_print_a_key(tmp_path):
    home = tmp_path / "h"
    assert _hv(home, "config", "identity", "init").returncode == 0 and _ctl(home, "owner", "init").returncode == 0
    out = _hv(home, "whoami").stdout
    assert "public id" in out.splitlines()[0]
    assert any(l.startswith("owner:") and "(public id)" in l for l in out.splitlines())
    assert any(l.startswith("keys:") and "never printed" in l for l in out.splitlines())
    seed = _owner_seed_b64(home)
    assert seed not in out and seed not in _hv(home, "doctor").stdout
    assert "(public id)" in _check(home, "owner")["detail"]
    # a parser that reads the id as the second word still gets the id
    assert out.splitlines()[0].split()[1].startswith("k1:")


# ── the manifest covers exactly one extensionless file (5848726950) ──────────────────────────────────

def _unsigned_entry_points(root):
    """Tracked extensionless files that could run (executable bit or a shebang) yet the signed manifest
    skips. LICENSE or a Dockerfile have no suffix too, but they are not something a user executes."""
    loader = importlib.machinery.SourceFileLoader("hv_manifest", str(PROJECT / "hv"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("hv_manifest", loader))
    loader.exec_module(mod)
    covered = set(mod._source_manifest(root)["files"])
    staged = subprocess.run(["git", "-C", str(root), "ls-files", "-s", "-z"], capture_output=True, check=True)
    out = []
    for rec in staged.stdout.split(b"\x00"):
        if not rec:
            continue
        meta, rel = rec.decode().split("\t", 1)
        if Path(rel).suffix or rel in covered:
            continue
        runs = meta.split()[0] == "100755" or (Path(root) / rel).read_bytes()[:2] == b"#!"
        if runs:
            out.append(rel)
    return sorted(out)


def test_no_runnable_extensionless_file_ships_unsigned():
    assert _unsigned_entry_points(PROJECT) == [], "an extensionless entry point would ship unsigned"


@pytest.mark.parametrize("mode, body", [("100755", "# stub\n"), ("100644", "#!/bin/sh\n")])
def test_the_manifest_tripwire_fails_on_a_new_entry_point(tmp_path, mode, body):
    """Proven, not trusted (h:157bd5e469): a second entry point is caught, by its mode or by its shebang,
    while a LICENSE-like file is not."""
    root = tmp_path / "repo"
    root.mkdir()
    for name, text in (("hv", "#!/usr/bin/env python3\n"), ("LICENSE", "MIT\n"), ("hive-mind", body)):
        (root / name).write_text(text)
    os.chmod(root / "hive-mind", 0o755 if mode == "100755" else 0o644)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "update-index", "--chmod=" + ("+x" if mode == "100755" else "-x"),
                    "hive-mind"], check=True)
    assert _unsigned_entry_points(root) == ["hive-mind"]


# ── identity save and restore, through the key directory ────────────────────────────────────────────

@pytest.mark.skipif(not POSIX, reason="bash installer primitive")
def test_identity_is_saved_from_and_restored_into_the_key_directory(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hv").symlink_to(PROJECT / "hv")          # the primitive asks hv where the keys are
    assert _hv(src, "config", "identity", "init").returncode == 0
    device_id = (src / ".device-id").read_text().strip()
    stash = tmp_path / "stash"
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "hv").symlink_to(PROJECT / "hv")
    script = f'''
        export HIVE_IDENTITY_STASH="{stash}"
        . "{PROJECT}/scripts/installer/_identity.sh"
        keep_identity_save "{src}" || exit 11
        keep_identity_can_restore "{dst}" || exit 12
        keep_identity_restore "{dst}" || exit 13
    '''
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k not in ("HIVE_HOME", "HIVE_KEY_DIR")})
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not (dst / ".device-key").exists(), "restore must not put the seed back into the tree"
    assert (_key_dir(dst) / "device-key").exists()
    assert (dst / ".device-id").read_text().strip() == device_id
    assert _hv(dst, "whoami").stdout.splitlines()[0].split()[1] == device_id
