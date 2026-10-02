"""Owner-key material and owner signing — the one place a governance signature is produced.

2.0 (public #136) makes `hv` the agent **data plane** and `hive-mind` the owner/operator **control
plane**, and requirement S2 is that `hv` cannot owner-sign at all. This module is the boundary that
makes S2 checkable instead of promised: everything that reads the owner seed, backs it up, or produces
an owner signature lives here, so the split's static import-graph test has a single module name to
assert is absent from `hv`'s transitive imports.

**Only the control plane loads it.** PR 1 (#152) moved the code here unchanged; the split (PR 2b, #159)
moved every caller onto the control plane (`hivemind_owner.py`), so `hv` no longer imports this module and
`tests/test_s2_split.py` asserts that `hv`'s import graph never reaches it. Nothing here changed the
journal, the wire, or the bytes of any signature: the moved bodies are the previous ones verbatim. See
docs/HV_ARCHITECTURE.md.

Two deliberate properties, both there to keep the boundary honest:

* **No path constant, and no configuration about *where* the hive lives.** Every entry point takes its
  paths as arguments. So there is no `HIVE_HOME` default duplicated between here and `hv`, and nothing
  here can disagree with `hv` about which file is the owner key.
* **No second canonicaliser.** `sign_governance` hashes through `merkle._canonical`, the one definition
  (#153; `hv._canonical` delegates to it). A copy of the function that produces *the signed bytes* is the
  last thing this module should introduce, since a divergence would not be a bug in one node but a
  signature that fails to verify across the fleet; tests/test_one_canonical.py fails on one.
"""

import base64
import os
from pathlib import Path

# NO sys.path manipulation here, deliberately. An earlier version inserted this file's resolved parent
# at position 0, which broke any staged or shadowed install: in such a layout this module is a symlink,
# so the resolved parent is the REAL source directory, and prepending it silently shadowed the staged
# copy for every later import — `hv doctor`'s crypto self-test loaded the genuine vectors instead of the
# staged ones, and reported ok. A library module must not decide import resolution for the process.
# The entry point owns sys.path: `hv` inserts its own directory before importing this, and the control
# plane will do the same.
import merkle

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


def load_sealed(path, passphrase, unseal):
    """The 32-byte owner seed in the sealed key at `path` (2.0 PR 3b, private #27), opened with
    `passphrase`. `unseal(envelope, passphrase)` is the AEAD opener, passed in so the cipher has one home
    (`hv._owner_unseal`, the same envelope `owner escrow` writes).

    Unlike `load_seed` this raises, because its callers must tell the operator which it was: ValueError for
    a wrong passphrase or a damaged file, OSError when the file cannot be read."""
    import json
    env = json.loads(Path(path).read_text())
    seed = unseal(env, passphrase)
    if len(seed) != 32:
        raise ValueError("the sealed owner key does not hold a 32-byte seed")
    return seed


def _write_0600(path, text):
    """Write `text` to `path` at mode 0600 from its creation, atomically: a temp file in the same
    directory, then a rename, so a crash never leaves a half-written or briefly world-readable key."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def write_sealed(path, envelope):
    """Write a sealed owner key (the JSON envelope `hv._owner_seal` returns) to `path` at 0600."""
    import json
    return _write_0600(path, json.dumps(envelope, indent=2) + "\n")


def stash_sealed(sealed_path, stash_dir):
    """Back up the SEALED owner key into `stash_dir` as `.owner-key.sealed` (0600, directory 0700), and
    drop a plaintext `.owner-key` there: it is either this key unsealed or an older key the previous
    `stash` would have overwritten, so keeping it would leave the seed readable in the one copy meant to
    outlive an uninstall. Returns the path written, or None. Best-effort, like `stash`."""
    try:
        d = Path(stash_dir)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass
        f = _write_0600(d / ".owner-key.sealed", Path(sealed_path).read_text())
        try:
            (d / ".owner-key").unlink()
        except FileNotFoundError:
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


def read_passphrase(prompt, confirm=False, env_var="HIVE_OWNER_PASSPHRASE"):
    """Read a passphrase from `env_var` if set (automation and tests; empty means cancel), else prompt on
    the tty. Returns the passphrase, or None to CANCEL — a blank line, Ctrl-C, Ctrl-D or a confirmation
    mismatch all bail out, so the operator can change their mind at the prompt.

    Two secrets use this, each with its own variable: `HIVE_OWNER_PASSPHRASE` encrypts an export or an
    escrow, and `HIVE_OWNER_KEY_PASSPHRASE` unlocks the owner key sealed at rest (2.0 PR 3b)."""
    env = os.environ.get(env_var)
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
