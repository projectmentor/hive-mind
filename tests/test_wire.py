"""Phase 0 — `hv wire`: the unified wiring verb that absorbs `hv doctor wire-agent`.

`hv wire <name>` auto-dispatches by the resolved cell's `kind`:
  • kind:agent → reconcile foreign config (the Claude hooks) — must be BYTE-IDENTICAL to the old
    `hv doctor wire-agent` (golden parity), which stays as a hidden deprecated alias;
  • kind:tool  → Phase-1 executor (a stub here that resolves the cell).
`--add`/`--list`/`--show` manage the cell registry (journaled cells + built-ins).
Tests drive the real `hv` CLI against isolated temp homes.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HV = PROJECT / "hv"


def run(home, *args, claude_dir=None, check=True):
    env = dict(os.environ, HIVE_HOME=str(home))
    if claude_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    r = subprocess.run([sys.executable, str(HV), *args], env=env, capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def test_wire_claude_golden_parity_with_old_alias(tmp_path):
    """`hv wire claude` and the deprecated `hv doctor wire-agent` produce the SAME settings.json."""
    c_new, c_old = tmp_path / "cnew", tmp_path / "cold"
    c_new.mkdir(parents=True); c_old.mkdir(parents=True)
    run(tmp_path / "h1", "wire", "claude", claude_dir=c_new)
    run(tmp_path / "h2", "doctor", "wire-agent", claude_dir=c_old)
    s_new = (c_new / "settings.json").read_text()
    s_old = (c_old / "settings.json").read_text()
    assert s_new == s_old
    assert "hive_dispatch.sh session-start" in s_new   # the real shim landed


def test_wire_claude_is_idempotent(tmp_path):
    c = tmp_path / "c"; c.mkdir(parents=True)
    run(tmp_path / "h", "wire", "claude", claude_dir=c)
    before = (c / "settings.json").read_text()
    r = run(tmp_path / "h", "wire", "claude", claude_dir=c)
    assert (c / "settings.json").read_text() == before    # no churn on re-run
    assert "already wired" in r.stdout


def test_wire_not_applicable_without_claude(tmp_path):
    r = run(tmp_path / "h", "wire", "claude", claude_dir=tmp_path / "absent")
    assert "not present" in r.stdout                      # integration not applicable, no error


def test_cell_add_list_show_and_tool_stub(tmp_path):
    home = tmp_path / "h"
    cell = tmp_path / "cf.json"
    cell.write_text(json.dumps({"name": "cloudflare", "kind": "tool", "version": 1,
                                "requires": ["CLOUDFLARE_API_TOKEN"], "spec": {}, "verify": {}}))
    run(home, "wire", "--add", str(cell))
    lst = run(home, "wire", "--list").stdout
    assert "claude" in lst and "cloudflare" in lst and "kind=tool" in lst and "(built-in)" in lst
    show = run(home, "wire", "--show", "cloudflare").stdout
    assert "CLOUDFLARE_API_TOKEN" in show
    assert "Phase 1" in run(home, "wire", "cloudflare").stdout          # tool stub
    assert "No cell named" in run(home, "wire", "does-not-exist").stdout


def test_cell_latest_version_wins_over_journal(tmp_path):
    home = tmp_path / "h"
    v1 = tmp_path / "v1.json"; v2 = tmp_path / "v2.json"
    v1.write_text(json.dumps({"name": "vultr", "kind": "tool", "version": 1, "note": "old"}))
    v2.write_text(json.dumps({"name": "vultr", "kind": "tool", "version": 2, "note": "new"}))
    run(home, "wire", "--add", str(v1))
    run(home, "wire", "--add", str(v2))
    assert '"note": "new"' in run(home, "wire", "--show", "vultr").stdout
