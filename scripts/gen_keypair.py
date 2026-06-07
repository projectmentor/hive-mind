#!/usr/bin/env python3
"""Generate a HiveMind release signing keypair. RUN THIS ONCE, on a trusted machine, offline.

  python3 scripts/gen_keypair.py

It writes the PUBLIC key to hivemind.pub (commit this + publish it to the website) and prints the
PRIVATE key to the screen ONCE. Store the private key in the GitHub Actions secret HIVE_SIGNING_KEY
and nowhere else. Never commit it, never paste it into a chat, never put it in the corpus. If it
leaks, generate a new keypair and rotate (replace hivemind.pub on main + on the website).
"""
import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ed25519  # noqa: E402

seed = os.urandom(32)
pub = ed25519.pub_from_seed(seed)
pub_b64 = base64.b64encode(pub).decode()
seed_b64 = base64.b64encode(seed).decode()

(ROOT / "hivemind.pub").write_text(pub_b64 + "\n")

print(f"\nPublic key written to {ROOT / 'hivemind.pub'}")
print(f"  {pub_b64}")
print("\n  -> commit hivemind.pub and upload it to https://hivemind.projectmentor.org/.well-known/hivemind.pub\n")
print("PRIVATE KEY (store in the GitHub secret HIVE_SIGNING_KEY, then forget it — shown only now):")
print(f"  {seed_b64}\n")
print("Set it with:  gh secret set HIVE_SIGNING_KEY --repo projectmentor/hive-mind --body '<the value above>'\n")
