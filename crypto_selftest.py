"""crypto_selftest.py — known-answer self-tests for the bundled crypto (x25519.py, ed25519.py) and
the capsule key-agreement. `run_all()` is invoked by the `hv doctor` `crypto` check (a HARD failure
if any suite breaks) so a corrupted or tampered crypto module is caught in the field on the 15-min
timer, not only at build time. Zero deps (stdlib only); deterministic (fixed seeds/vectors)."""

import binascii

try:
    import x25519
    import ed25519
except Exception:                       # pragma: no cover - import guard
    x25519 = None
    ed25519 = None

_h = binascii.unhexlify
_b2h = lambda b: binascii.hexlify(b).decode()

# RFC 7748 §5.2 X25519 known-answer vectors.
_X_VECS = [
    ("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4",
     "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c",
     "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"),
    ("4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d",
     "e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493",
     "95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957"),
]

# Ed25519 pin: deterministic sign over a fixed seed+message (catches any drift in the bundled impl).
_ED_SEED = bytes(range(32))
_ED_MSG = b"hive-mind crypto self-test v1"
_ED_PUB = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
_ED_SIG = ("b88af2fcd7d42284720ed2b00b593f9efabd0de1e5484a32cfbd73bbc20fedde"
           "fddf09cf21956b30a0fc7c548431fcdd4bf189461d5cf3a62aafcd8b8b785008")


def _x25519_ok():
    if x25519 is None:
        return False
    try:
        return all(_b2h(x25519.x25519(_h(k), _h(u))) == exp for k, u, exp in _X_VECS)
    except Exception:
        return False


def _ed25519_ok():
    if ed25519 is None:
        return False
    try:
        if _b2h(ed25519.pub_from_seed(_ED_SEED)) != _ED_PUB:
            return False
        sig = ed25519.sign(_ED_MSG, _ED_SEED)
        if _b2h(sig) != _ED_SIG:                       # Ed25519 signing is deterministic
            return False
        pub = _h(_ED_PUB)
        if not ed25519.verify(_ED_MSG, sig, pub):
            return False
        if ed25519.verify(_ED_MSG, sig[:-1] + bytes([sig[-1] ^ 1]), pub):
            return False                               # a flipped signature MUST fail
        return True
    except Exception:
        return False


def _edcurve_ok():
    if x25519 is None or ed25519 is None:
        return False
    try:
        seed = _ED_SEED
        return (x25519.ed_pub_to_curve_pub(ed25519.pub_from_seed(seed))
                == x25519.scalarmult_base(x25519.ed_seed_to_curve_scalar(seed)))
    except Exception:
        return False


def _sealed_box_ok():
    """The capsule key-agreement: ephemeral×recipient_pub == recipient_scalar×ephemeral_pub, and a
    low-order recipient key is rejected."""
    if x25519 is None or ed25519 is None:
        return False
    try:
        r = bytes(range(32, 64))
        r_sc = x25519.ed_seed_to_curve_scalar(r)
        r_cp = x25519.ed_pub_to_curve_pub(ed25519.pub_from_seed(r))
        eph = bytes(range(64, 96))
        if x25519.shared_secret(eph, r_cp) != x25519.shared_secret(r_sc, x25519.scalarmult_base(eph)):
            return False
        try:
            x25519.shared_secret(eph, b"\x00" * 32)
            return False                               # low-order MUST raise
        except ValueError:
            return True
    except Exception:
        return False


def run_all():
    """Run every suite → {name: ok_bool}. All True == crypto is correct on this node."""
    return {"x25519": _x25519_ok(), "ed25519": _ed25519_ok(),
            "edcurve": _edcurve_ok(), "sealed_box": _sealed_box_ok()}


if __name__ == "__main__":
    import sys
    res = run_all()
    for name, ok in res.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(res.values()) else 1)
