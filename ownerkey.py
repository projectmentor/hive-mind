"""Owner-key material and owner signing — the one place a governance signature is produced.

2.0 (public #136) makes `hv` the agent **data plane** and `hive-mind` the owner/operator **control
plane**, and requirement S2 is that `hv` cannot owner-sign at all. This module is the boundary that
makes S2 checkable instead of promised: everything that reads the owner seed, backs it up, or produces
an owner signature lives here, so the split's static import-graph test has a single module name to
assert is absent from `hv`'s transitive imports.

**This change moves code, not commands.** `hv` still imports this module and still owner-signs exactly
as before, because the five signing call sites have not moved yet — they move in the split, where the
import-graph test becomes true. Nothing here changes the journal, the wire, or the bytes of any
signature: the moved bodies are the previous ones verbatim.

Two deliberate properties, both there to keep the boundary honest:

* **No path constant, and no configuration about *where* the hive lives.** Every entry point takes its
  paths as arguments. So there is no `HIVE_HOME` default duplicated between here and `hv`, and nothing
  here can disagree with `hv` about which file is the owner key.
* **No third canonicaliser.** `sign_governance` hashes through `merkle._canonical`. `hv` carries its
  own byte-identical copy, whose docstring and merkle's each say "must match the other" — a third copy
  of the function that produces *the signed bytes* is the last thing this module should introduce, since
  a divergence would not be a bug in one node but a signature that fails to verify across the fleet.
"""

import base64
import os
import sys
from pathlib import Path

# Sibling modules, importable when this is loaded standalone (the control plane will) as well as from
# `hv`, which inserts the same directory before importing merkle.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merkle  # noqa: E402

try:
    import ed25519 as _ed25519      # bundled pure-Python signer
except Exception:                   # a build without the bundled crypto: no seed, so no signing
    _ed25519 = None


def load_seed(path):
    """The 32-byte owner seed at `path` if this device holds the owner key, else None.

    Read-only; never creates. Returns None rather than raising for every failure — a missing file, an
    unreadable one, a build with no crypto, or content that is not a 32-byte seed — because every
    caller's question is "can this device owner-sign?", and the answer to all of those is no."""
    try:
        path = Path(path)
        if _ed25519 is None or not path.exists():
            return None
        seed = base64.b64decode(path.read_text().strip())
        return seed if len(seed) == 32 else None
    except Exception:
        return None


def stash(seed, stash_dir):
    """Best-effort backup of the owner seed into `stash_dir` (the stable identity stash that survives an
    uninstall, the same location the installer's keep-identity save uses). Returns the path written, or
    None. `stash_dir` is passed in rather than resolved here, so the environment variable that selects
    it has exactly one reader."""
    try:
        d = Path(stash_dir)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass
        f = d / ".owner-key"
        f.write_text(base64.b64encode(seed).decode() + "\n")
        try:
            os.chmod(f, 0o600)
        except Exception:
            pass
        return str(f)
    except Exception:
        return None


def sign_governance(payload, owner_seed, owner_pub):
    """Attach `owner_pub`, then `owner_sig` — the owner's signature over the payload without `owner_sig`.

    This authorizes the governance ACTION independently of which device appends the entry, which is why
    it is the function that must become unreachable from `hv`: an agent that cannot produce these bytes
    cannot mint governance, whatever else it can do."""
    payload = dict(payload)
    payload["owner_pub"] = base64.b64encode(owner_pub).decode()
    body = merkle._canonical({k: v for k, v in payload.items() if k != "owner_sig"})
    payload["owner_sig"] = base64.b64encode(_ed25519.sign(body, owner_seed)).decode()
    return payload


def read_passphrase(prompt, confirm=False):
    """Read a passphrase from $HIVE_OWNER_PASSPHRASE if set (automation and tests; empty means cancel),
    else prompt on the tty. Returns the passphrase, or None to CANCEL — a blank line, Ctrl-C, Ctrl-D or
    a confirmation mismatch all bail out, so the operator can change their mind at the prompt."""
    env = os.environ.get("HIVE_OWNER_PASSPHRASE")
    if env is not None:
        return env or None           # set => use it; empty => cancel
    import getpass
    try:
        pw = getpass.getpass(prompt)
        if not pw:                   # blank line = bail out
            return None
        if confirm and pw != getpass.getpass("Confirm passphrase: "):
            print("Passphrases do not match.")
            return None
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return pw
