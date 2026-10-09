#!/usr/bin/env python3
"""Generate verify.json, the canonical source fingerprint that `hv verify` checks against.

Run at release time (and ideally via CI on any code change), then commit verify.json. The
published copy at the canonical source is what proves an install is official; keep it in sync
with the code on `main`, or `hv verify` will report a false 'modified'.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
loader = importlib.machinery.SourceFileLoader("hvmod", str(ROOT / "hv"))
spec = importlib.util.spec_from_loader("hvmod", loader)
hv = importlib.util.module_from_spec(spec)
loader.exec_module(hv)

def _sequence():
    """`git rev-list --count HEAD`: the signed, monotonic counter `hive-mind update` compares so an older signed
    tree re-wrapped as a descendant commit is refused. A shallow clone would count 1, so it is an error."""
    def git(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)
    if git("rev-parse", "--is-shallow-repository").stdout.strip() != "false":
        sys.exit("gen_verify: refusing to write a sequence from a shallow or non-git checkout (needs full history).")
    r = git("rev-list", "--count", "HEAD")
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        sys.exit(f"gen_verify: git rev-list --count HEAD failed: {r.stderr.strip()}")
    return int(r.stdout)


m = hv._source_manifest(ROOT)
m["sequence"] = _sequence()
(ROOT / "verify.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
print(f"wrote verify.json: v{m['version']}, sequence {m['sequence']}, {len(m['files'])} files, digest {m['digest'][:16]}...")
