"""crypto_selftest.py — known-answer self-tests for the bundled crypto (x25519.py, ed25519.py) and
the capsule key-agreement. `run_all()` is invoked by the `hv doctor` `crypto` check (a HARD failure
if any suite breaks) so a corrupted or tampered crypto module is caught in the field on the 15-min
timer, not only at build time. Zero deps (stdlib only); deterministic (fixed seeds/vectors)."""

import binascii

try:
    import x25519
    import ed25519
    import chacha20poly1305
except Exception:                       # pragma: no cover - import guard
    x25519 = None
    ed25519 = None
    chacha20poly1305 = None

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

# RFC 8439 §2.8.2 ChaCha20-Poly1305 AEAD known-answer vector (the authoritative published vector).
_AEAD_KEY = "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
_AEAD_NONCE = "070000004041424344454647"
_AEAD_AAD = "50515253c0c1c2c3c4c5c6c7"
_AEAD_PT = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one "
            b"tip for the future, sunscreen would be it.")
_AEAD_CT = ("d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
            "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
            "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
            "3ff4def08e4b7a9de576d26586cec64b6116")
_AEAD_TAG = "1ae10b594f09e26a7e902ecbd0600691"

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


def _aead_ok():
    """ChaCha20-Poly1305: matches the RFC 8439 §2.8.2 published vector, round-trips, and rejects a
    tampered ciphertext and a tampered AAD. This is the symmetric KAT the bundled crypto previously
    lacked — it anchors the capsule/owner-seal cipher to an external authority, not a self-minted byte
    string."""
    if chacha20poly1305 is None:
        return False
    try:
        key, nonce, aad = _h(_AEAD_KEY), _h(_AEAD_NONCE), _h(_AEAD_AAD)
        ct, tag = chacha20poly1305.encrypt(key, nonce, _AEAD_PT, aad)
        if _b2h(ct) != _AEAD_CT or _b2h(tag) != _AEAD_TAG:
            return False
        if chacha20poly1305.decrypt(key, nonce, ct, tag, aad) != _AEAD_PT:
            return False
        bad = bytes([ct[0] ^ 1]) + ct[1:]
        for args in ((key, nonce, bad, tag, aad), (key, nonce, ct, tag, aad + b"x")):
            try:
                chacha20poly1305.decrypt(*args)
                return False                           # tamper MUST raise
            except ValueError:
                pass
        return True
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
    return {"x25519": _x25519_ok(), "ed25519": _ed25519_ok(), "edcurve": _edcurve_ok(),
            "aead": _aead_ok(), "sealed_box": _sealed_box_ok()}


if __name__ == "__main__":
    import sys
    res = run_all()
    for name, ok in res.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    sys.exit(0 if all(res.values()) else 1)
