#!/usr/bin/env python3
"""Generate verify.json, the canonical source fingerprint that `hv verify` checks against.

Run at release time (and ideally via CI on any code change), then commit verify.json. The
published copy at the canonical source is what proves an install is official; keep it in sync
with the code on `main`, or `hv verify` will report a false 'modified'.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
loader = importlib.machinery.SourceFileLoader("hvmod", str(ROOT / "hv"))
spec = importlib.util.spec_from_loader("hvmod", loader)
hv = importlib.util.module_from_spec(spec)
loader.exec_module(hv)

m = hv._source_manifest(ROOT)
(ROOT / "verify.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
print(f"wrote verify.json: v{m['version']}, {len(m['files'])} files, digest {m['digest'][:16]}...")
