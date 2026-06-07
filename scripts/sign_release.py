#!/usr/bin/env python3
"""Sign verify.json with the release private key. Runs in CI (and locally for testing).

  HIVE_SIGNING_KEY=<base64 seed> python3 scripts/sign_release.py

Reads the 32-byte Ed25519 seed (base64) from the env var HIVE_SIGNING_KEY, signs the exact bytes of
verify.json, and writes the detached signature (base64) to verify.json.sig. Run gen_verify.py first
so verify.json is current; `hv verify` checks this signature against the bundled hivemind.pub.
"""
import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ed25519  # noqa: E402

seed_b64 = os.environ.get("HIVE_SIGNING_KEY", "").strip()
if not seed_b64:
    sys.exit("HIVE_SIGNING_KEY is not set (base64 of the 32-byte seed).")
seed = base64.b64decode(seed_b64)
if len(seed) != 32:
    sys.exit(f"HIVE_SIGNING_KEY decodes to {len(seed)} bytes; expected 32.")

manifest = ROOT / "verify.json"
if not manifest.exists():
    sys.exit("verify.json not found — run scripts/gen_verify.py first.")
sig = ed25519.sign(manifest.read_bytes(), seed)
(ROOT / "verify.json.sig").write_text(base64.b64encode(sig).decode() + "\n")
print(f"wrote verify.json.sig ({len(sig)} bytes, signed verify.json)")
