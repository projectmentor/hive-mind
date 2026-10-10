"""3.0 PR 3 (plan 3.3, 3.6 (a)-(h)): step 0 of `hive-mind update`, the forget-migration gate after the re-exec.

The OLD updater (a 2.x script: fetch, merge or reset, exec the new script) has already switched the tree when the NEW
script's step 0 runs. Step 0 passes a closed hive, migrates an open one on `y` (owner key, terminal), and otherwise
refuses with exit 1: if this update's own switch is identified, the tree goes back to the pre-switch HEAD with
`reset --keep`; if not, HEAD is left where it is. The refused tree is byte-identical (HEAD, file bytes, untracked
files), the daemon is never restarted and no unit is written.

The old updater is a stub here (`OLD_UPDATER`: what 2.4's `_update.sh` does around the switch, nothing else), so the
no-marker path is a real parent -> child run. The same-second cases plant the parent's reflog rows by hand and fix the
child's start second with a stub `date`, since a real run cannot be made to land every switch in one unix second.
"""
import os
import pty
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_update_script import (REPO, Sandbox, _backdated_forget, _ctl, _git, _major_upstream,  # noqa: E402,F401
                                sandbox)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux")

NEW_SCRIPT = REPO / "scripts" / "installer" / "_update.sh"

# 2.4's updater around the switch (_update.sh:150-263): fetch, fast-forward or hard-reset, export only
# HIVE_UPDATE_REEXEC, exec the new script. No marker, no verification (that is not what is under test).
OLD_UPDATER = r"""#!/usr/bin/env bash
set -euo pipefail
git -C "$HIVE_DIR" fetch --tags origin
_HEAD="$(git -C "$HIVE_DIR" rev-parse HEAD)"; _NEW="$(git -C "$HIVE_DIR" rev-parse "@{u}")"
if [ "$_HEAD" != "$_NEW" ]; then
  if git -C "$HIVE_DIR" merge-base --is-ancestor HEAD "$_NEW"; then git -C "$HIVE_DIR" merge --ff-only -q "$_NEW"
  else git -C "$HIVE_DIR" reset -q --hard "$_NEW"; fi
fi
export HIVE_UPDATE_REEXEC=1
exec bash "$HIVE_DIR/scripts/installer/_update.sh" "$@"
"""


def _env(sb, **extra):
    return sb.env(**extra)


def run_update(sb, old=False, tty=None, extra_env=None, args=()):
    """Run the updater. `old`: through the 2.4-style stub parent. `tty`: None gives no terminal at all, a string
    is typed at one. Returns (returncode, stdout, stderr)."""
    cmd = ["bash", str(sb.tmp / "old_updater.sh" if old else NEW_SCRIPT_IN(sb)), *args]
    env = _env(sb, **(extra_env or {}))
    if old:
        (sb.tmp / "old_updater.sh").write_text(OLD_UPDATER)
    if tty is None:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=300)
        return r.returncode, r.stdout, r.stderr
    master, slave = pty.openpty()
    try:
        p = subprocess.Popen(cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                             start_new_session=True)
        os.write(master, tty.encode())
        out, err = p.communicate(timeout=300)
    finally:
        os.close(master)
        os.close(slave)
    return p.returncode, out, err


def NEW_SCRIPT_IN(sb):
    return sb.hive / "scripts" / "installer" / "_update.sh"


def snap(sb):
    """HEAD, every file's bytes (tracked or not, bytecode too, outside .git) and the untracked list. Not
    `status --porcelain`, which differs for a staged edit that `reset --keep` returns unstaged."""
    files = {str(p.relative_to(sb.hive)): p.read_bytes() for p in sorted(sb.hive.rglob("*"))
             if p.is_file() and ".git" not in p.relative_to(sb.hive).parts}
    return (_git(sb.hive, "rev-parse", "HEAD"), files, _git(sb.hive, "ls-files", "-o", "--exclude-standard"))


def open_hive(sb, fact="the backup runs at 02:00"):
    """An owned hive whose forget_writers is still open, with one fact kept forgotten only by the grandfather."""
    assert _ctl(sb, "owner", "init").returncode == 0
    assert _ctl(sb, "config", "set", "forget_writers", "legacy").returncode == 0
    return _backdated_forget(sb, fact)


def check_rc(sb):
    return _ctl(sb, "migrate", "forget", "--check").returncode


def dirty(sb, staged=False):
    """Edits the upstream commits do not touch: a tracked file, and an untracked one."""
    (sb.hive / "CONTRIBUTING.md").write_text("a local edit\n")
    (sb.hive / "notes.txt").write_text("untracked\n")
    if staged:
        _git(sb.hive, "add", "CONTRIBUTING.md")


def untouched(sb):
    assert not sb.log.exists(), sb.log.read_text()                       # the daemon was never restarted
    assert not (sb.home / ".config" / "systemd" / "user" / "hive-sync.service").exists()   # no unit was written


def plant_reflog(sb, rows):
    """Append raw rows to HEAD's reflog: (old, new, unix second, subject)."""
    with open(sb.hive / ".git" / "logs" / "HEAD", "a") as fh:
        for old, new, ts, subject in rows:
            fh.write(f"{old} {new} t <t@example.invalid> {ts} +0000\t{subject}\n")


def stub_date(sb, second):
    """A `date` that reports `second` for `+%s`, so the child's captured start is fixed."""
    d = sb.stub / "date"
    d.write_text(f'#!/bin/sh\nif [ "$1" = "+%s" ]; then echo {second}; exit 0; fi\nexec /bin/date "$@"\n')
    d.chmod(0o755)


# ── (a) closed ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("old", [False, True], ids=["new-parent", "old-parent"])
def test_a_closed_hive_updates(sandbox, old):
    sb = sandbox()
    assert _ctl(sb, "owner", "init").returncode == 0                      # born closed
    new = sb.upstream_commit()
    rc, out, err = run_update(sb, old=old)
    assert rc == 0, out + err
    assert "No fact depends on a pre-genesis unsigned forget" in out and "Update complete" in out
    assert _git(sb.hive, "rev-parse", "HEAD") == new
    assert out.index("No fact depends") < out.index("Refreshing commands")      # step 0 is first
    assert "restart hive-sync" in sb.log.read_text()


def test_a_hive_with_no_owner_updates(sandbox):
    sb = sandbox()
    new = sb.upstream_commit()
    rc, out, err = run_update(sb, old=True)
    assert rc == 0 and "Update complete" in out, out + err
    assert _git(sb.hive, "rev-parse", "HEAD") == new


# ── (b) open, owner key, y at a terminal; (e) a re-run is a no-op ─────────────────────────────────────────────

def test_b_open_with_the_owner_key_and_y_migrates_and_completes_and_e_rerun_is_a_noop(sandbox):
    sb = sandbox()
    sid = open_hive(sb)
    assert check_rc(sb) == 1
    new = sb.upstream_commit()
    rc, out, err = run_update(sb, old=True, tty="y\n")
    assert rc == 0, out + err
    assert sid in out and "Forget migration done" in out and "Update complete" in out
    assert _git(sb.hive, "rev-parse", "HEAD") == new
    assert check_rc(sb) == 0
    assert out.index("Forget migration done") < out.index("Refreshing commands")
    journal = {p.name: p.read_bytes() for p in (sb.hive / "journal").glob("*.jsonl")}
    rc2, out2, err2 = run_update(sb, old=True, tty="y\n")                         # (e)
    assert rc2 == 0 and "No fact depends" in out2, out2 + err2
    assert {p.name: p.read_bytes() for p in (sb.hive / "journal").glob("*.jsonl")} == journal


# ── (c) open, N or no terminal: the tree is byte-identical, with and without a dirty tree ─────────────────────

def refuse_scenario(sb, answer, dirt, old=True):
    """Open hive, a switch to a new commit, the updater run. Returns (before, after, rc, out, err)."""
    open_hive(sb)
    sb.upstream_commit()
    if dirt:
        dirty(sb, staged=dirt == "staged")
    before = snap(sb)
    rc, out, err = run_update(sb, old=old, tty=answer)
    return before, snap(sb), rc, out, err


@pytest.mark.parametrize("dirt", [None, "dirty", "staged"], ids=["clean", "dirty", "staged"])
@pytest.mark.parametrize("answer", [None, "n\n"], ids=["no-tty", "N"])
@pytest.mark.parametrize("old", [True, False], ids=["old-parent", "new-parent"])
def test_c_open_and_declined_leaves_the_tree_byte_identical(sandbox, answer, dirt, old):
    sb = sandbox()
    before, after, rc, out, err = refuse_scenario(sb, answer, dirt, old=old)
    assert rc == 1, out + err
    assert "REFUSED" in err and "hive-mind migrate forget" in err and "hive-mind sync" in err
    assert "Update complete" not in out and "Refreshing commands" not in out and "Pinned this hive" not in out
    assert "was undone" in err
    assert after == before
    untouched(sb)
    assert check_rc(sb) == 1


# ── (d) open, no owner key on this device ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dirt", [None, "dirty"])
def test_d_open_on_a_device_without_the_owner_key_is_refused_the_same_way(sandbox, dirt):
    sb = sandbox()
    open_hive(sb)
    keys = [p for p in sb.home.rglob("owner-key*")] + [p for p in sb.hive.glob(".owner-key*")]
    assert keys
    for k in keys:
        k.rename(str(k) + ".away")
    sb.upstream_commit()
    if dirt:
        dirty(sb)
    before = snap(sb)
    rc, out, err = run_update(sb, old=True, tty="y\n")                          # y cannot help: there is no key
    assert rc == 1, out + err
    assert "does not hold the owner key" in out and "REFUSED" in err and "was undone" in err
    assert snap(sb) == before
    untouched(sb)


# ── (f) already at the upstream commit: nothing to undo ──────────────────────────────────────────────────────

def test_f_already_at_upstream_with_stale_reflog_subjects_and_a_planted_orig_head_leaves_head_alone(sandbox):
    sb = sandbox()
    open_hive(sb)
    b0 = _git(sb.hive, "rev-parse", "HEAD")
    b1 = sb.upstream_commit()
    _git(sb.hive, "pull", "-q", "--ff-only")                                    # the node is at the upstream commit
    old = int(time.time()) - 3600                                               # both rows are before the bound
    plant_reflog(sb, [(b1, b1, old, f"merge {b1}: Fast-forward"), (b1, b1, old, f"reset: moving to {b1}")])
    (sb.hive / ".git" / "ORIG_HEAD").write_text(b0 + "\n")                      # a trap: an older commit
    dirty(sb)
    before = snap(sb)
    rc, out, err = run_update(sb, old=True, tty="n\n")
    assert rc == 1, out + err
    assert "REFUSED" in err and "did not switch the tree" in err and "was undone" not in err
    assert snap(sb) == before and _git(sb.hive, "rev-parse", "HEAD") == b1
    untouched(sb)


@pytest.mark.parametrize("subject", ["pull: Fast-forward", "pull --ff-only: Fast-forward", "reset: moving to @{u}",
                                     "reset: moving to HEAD~1", "merge FETCH_HEAD: Fast-forward",
                                     "reset: moving to {sha}", "merge {sha}: Fast-forward"],
                         ids=["pull", "pull-ff-only", "reset-upstream", "reset-head-1", "merge-fetch-head",
                              "backdated-reset", "backdated-merge"])
def test_f_a_subject_that_is_not_this_updates_switch_is_never_rolled_back(sandbox, subject):
    """Rows that are fresh (inside the bound) but are not the script's exact subject, and the exact subject
    when it is older than the bound: HEAD stays."""
    sb = sandbox()
    open_hive(sb)
    b1 = sb.upstream_commit()
    _git(sb.hive, "pull", "-q", "--ff-only")
    start = int(time.time())
    stub_date(sb, start)
    ts = start - 3600 if "{sha}" in subject else start
    plant_reflog(sb, [(b1, b1, ts, subject.replace("{sha}", b1))])
    before = snap(sb)
    rc, out, err = run_update(sb, old=True, tty="n\n")
    assert rc == 1, out + err
    assert snap(sb) == before and "was undone" not in err
    untouched(sb)


# ── (g) a fresh switch is undone: the marker, the 2.4 reflog path, and the same-second case ───────────────────

def two_ahead(sb):
    """Upstream is two commits ahead of the node. Returns (pre-switch HEAD, middle, tip)."""
    b0 = _git(sb.hive, "rev-parse", "HEAD")
    mid = sb.upstream_commit()
    tip = sb.upstream_commit()
    return b0, mid, tip


@pytest.mark.parametrize("old", [False, True], ids=["marker", "reflog-no-marker"])
def test_g_a_fresh_switch_across_two_commits_returns_to_the_pre_switch_head(sandbox, old):
    sb = sandbox()
    open_hive(sb)
    b0, mid, tip = two_ahead(sb)
    dirty(sb)
    before = snap(sb)
    rc, out, err = run_update(sb, old=old, tty=None)
    assert rc == 1, out + err
    head = _git(sb.hive, "rev-parse", "HEAD")
    assert head == b0 and head not in (mid, tip), (head, b0, mid, tip)
    assert snap(sb) == before and "was undone" in err
    untouched(sb)


def test_g_the_2_4_reflog_path_in_the_same_unix_second_as_the_start(sandbox):
    sb = sandbox()
    open_hive(sb)
    b0, mid, tip = two_ahead(sb)
    _git(sb.hive, "fetch", "-q", "origin")
    dirty(sb)
    before_files = snap(sb)[1:]                                                  # before the parent's switch
    second = int(time.time()) - 2
    stub_date(sb, second)
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{second} +0000"}
    subprocess.run(["git", "-C", str(sb.hive), "merge", "--ff-only", "-q", tip], check=True, env={**env, **{
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.invalid"}})
    rc, out, err = run_update(sb, extra_env={"HIVE_UPDATE_REEXEC": "1"}, tty=None)         # the child alone
    assert rc == 1, out + err
    assert _git(sb.hive, "rev-parse", "HEAD") == b0 and "was undone" in err


def test_h_a_failing_fetch_in_the_child_still_runs_step_0_and_undoes_the_parents_switch(sandbox):
    """The re-exec'd process used to reach step 0 only after `git fetch`; a non-zero fetch left the parent's
    switch in place with the migration unchecked (hive-mind#340)."""
    sb = sandbox()
    open_hive(sb)
    b0, mid, tip = two_ahead(sb)
    _git(sb.hive, "fetch", "-q", "origin")
    dirty(sb)
    before_files = snap(sb)[1:]
    second = int(time.time()) - 2
    stub_date(sb, second)
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{second} +0000", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    subprocess.run(["git", "-C", str(sb.hive), "merge", "--ff-only", "-q", mid], check=True, env=env)   # the parent
    _git(sb.hive, "remote", "set-url", "origin", str(sb.tmp / "no-such-remote"))                      # the fetch fails
    rc, out, err = run_update(sb, extra_env={"HIVE_UPDATE_REEXEC": "1"}, tty=None)
    assert rc == 1 and "REFUSED" in err, out + err
    assert "Fetching latest" not in out                                         # refused before the fetch ran
    assert _git(sb.hive, "rev-parse", "HEAD") == b0
    assert snap(sb)[1:] == before_files
    untouched(sb)
    assert snap(sb)[1:] == before_files


def test_g_the_2_4_rewrite_path_returns_to_the_pre_switch_head(sandbox):
    sb = sandbox()
    open_hive(sb)
    b0 = _git(sb.hive, "rev-parse", "HEAD")
    _git(sb.src, "reset", "-q", "--hard", "HEAD~0")
    # history rewrite: a different commit on the same parent chain's root
    (sb.src / "README.md").write_text("rewritten\n")
    from test_update_script import _fix_manifest_digest
    _fix_manifest_digest(sb.src)
    _git(sb.src, "add", "-A")
    _git(sb.src, "commit", "-q", "--amend", "-m", "rewritten history")
    _git(sb.src, "push", "-q", "-f", "origin", "main")
    rc, out, err = run_update(sb, old=True, tty=None, args=["--allow-rewind"])
    assert rc == 1, out + err
    assert _git(sb.hive, "rev-parse", "HEAD") == b0 and "was undone" in err


def test_h_a_failing_fetch_in_the_child_still_runs_step_0_and_undoes_the_parents_switch(sandbox):
    """The re-exec'd process used to reach step 0 only after `git fetch`; a non-zero fetch left the parent's
    switch in place with the migration unchecked (hive-mind#340)."""
    sb = sandbox()
    open_hive(sb)
    b0, mid, tip = two_ahead(sb)
    _git(sb.hive, "fetch", "-q", "origin")
    dirty(sb)
    before_files = snap(sb)[1:]
    second = int(time.time()) - 2
    stub_date(sb, second)
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{second} +0000", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    subprocess.run(["git", "-C", str(sb.hive), "merge", "--ff-only", "-q", mid], check=True, env=env)   # the parent
    _git(sb.hive, "remote", "set-url", "origin", str(sb.tmp / "no-such-remote"))                      # the fetch fails
    rc, out, err = run_update(sb, extra_env={"HIVE_UPDATE_REEXEC": "1"}, tty=None)
    assert rc == 1 and "REFUSED" in err, out + err
    assert "Fetching latest" not in out                                         # refused before the fetch ran
    assert _git(sb.hive, "rev-parse", "HEAD") == b0
    assert snap(sb)[1:] == before_files
    untouched(sb)
    untouched(sb)


# ── (h) the 2.4 switch plus one fast-forward of the child's own, all in the start second ─────────────────────

def test_h_a_switch_plus_the_childs_own_fast_forward_returns_to_the_pre_switch_head_not_the_middle(sandbox):
    sb = sandbox()
    open_hive(sb)
    b0, mid, tip = two_ahead(sb)
    _git(sb.hive, "fetch", "-q", "origin")
    dirty(sb)
    before_files = snap(sb)[1:]                                                  # before the parent's switch
    second = int(time.time()) - 2
    stub_date(sb, second)
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{second} +0000", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    subprocess.run(["git", "-C", str(sb.hive), "merge", "--ff-only", "-q", mid], check=True, env=env)   # the parent
    rc, out, err = run_update(sb, extra_env={"HIVE_UPDATE_REEXEC": "1"}, tty=None)   # the child fast-forwards to tip
    assert rc == 1, out + err
    head = _git(sb.hive, "rev-parse", "HEAD")
    assert head == b0, f"stopped at {'the middle commit' if head == mid else head}"
    assert snap(sb)[1:] == before_files


def test_h_the_child_alone_in_a_later_second_rolls_back_to_the_head_it_read_before_its_fetch(sandbox):
    """No inherited flag, no qualifying row (the parent was already at HEAD): this process's own switch."""
    sb = sandbox()
    open_hive(sb)
    b0 = _git(sb.hive, "rev-parse", "HEAD")
    sb.upstream_commit()
    rc, out, err = run_update(sb, extra_env={"HIVE_UPDATE_REEXEC": "1"}, tty=None)
    assert rc == 1, out + err
    assert _git(sb.hive, "rev-parse", "HEAD") == b0 and "did not switch the tree" in err   # step 0 ran before its fetch


def test_h_a_failing_fetch_in_the_child_still_runs_step_0_and_undoes_the_parents_switch(sandbox):
    """The re-exec'd process used to reach step 0 only after `git fetch`; a non-zero fetch left the parent's
    switch in place with the migration unchecked (hive-mind#340)."""
    sb = sandbox()
    open_hive(sb)
    b0, mid, tip = two_ahead(sb)
    _git(sb.hive, "fetch", "-q", "origin")
    dirty(sb)
    before_files = snap(sb)[1:]
    second = int(time.time()) - 2
    stub_date(sb, second)
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{second} +0000", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    subprocess.run(["git", "-C", str(sb.hive), "merge", "--ff-only", "-q", mid], check=True, env=env)   # the parent
    _git(sb.hive, "remote", "set-url", "origin", str(sb.tmp / "no-such-remote"))                      # the fetch fails
    rc, out, err = run_update(sb, extra_env={"HIVE_UPDATE_REEXEC": "1"}, tty=None)
    assert rc == 1 and "REFUSED" in err, out + err
    assert "Fetching latest" not in out                                         # refused before the fetch ran
    assert _git(sb.hive, "rev-parse", "HEAD") == b0
    assert snap(sb)[1:] == before_files
    untouched(sb)


# ── the marker: only the pre-exec process sets it; the child never does ──────────────────────────────────────

def test_the_marker_is_exported_by_the_pre_exec_process_only(sandbox):
    sb = sandbox()
    text = NEW_SCRIPT.read_text()
    pre = text.index('if [ -z "${HIVE_UPDATE_REEXEC:-}" ]; then\n  unset HIVE_UPDATE_SWITCHED')
    assert 'export HIVE_UPDATE_PRE_HEAD="${HIVE_UPDATE_PRE_HEAD:-$_HEAD}"' in text[pre:pre + 300]
    assert text.count("export HIVE_UPDATE_SWITCHED=1") == 2 and text.count("[ -n \"${HIVE_UPDATE_REEXEC:-}\" ] || export") == 2
    assert "ORIG_HEAD" not in re.sub(r"#.*", "", text.split("_rollback_target() {")[1].split("\n}\n")[0])


# ── the mutant: without the rollback, (c) fails ──────────────────────────────────────────────────────────────

def test_a_mutant_without_the_rollback_fails_c(sandbox):
    sb = sandbox()
    mutated = NEW_SCRIPT.read_text().replace('reset --keep -q "$_target"', "true")
    assert mutated != NEW_SCRIPT.read_text()
    (sb.src / "scripts" / "installer" / "_update.sh").write_text(mutated)
    before, after, rc, out, err = refuse_scenario(sb, None, "dirty")
    assert rc == 1, out + err
    assert after != before                                                      # the switched tree stayed: (c) catches it
    assert after[0] != before[0]
