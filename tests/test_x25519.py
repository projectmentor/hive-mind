"""Phase 2 — pure-Python X25519 (RFC 7748) + Ed25519↔Curve25519 conversion.

These are the load-bearing crypto KATs the capsule layer rests on (human-review-gated). They pin:
the RFC 7748 §5.2 vectors, the ed↔curve round-trip invariant (so we can seal to a device's existing
Ed25519 key), ECDH agreement for the ephemeral-wrap pattern, and the low-order / y==1 rejections.
"""

import binascii
import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import x25519        # noqa: E402
import ed25519       # noqa: E402

_h2b = binascii.unhexlify
_b2h = lambda b: binascii.hexlify(b).decode()


def test_rfc7748_x25519_vectors():
    for k, u, exp in [
        ("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4",
         "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c",
         "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"),
        ("4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d",
         "e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493",
         "95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957"),
    ]:
        assert _b2h(x25519.x25519(_h2b(k), _h2b(u))) == exp


def test_base_point_equals_scalarmult_of_9():
    s = os.urandom(32)
    assert x25519.scalarmult_base(s) == x25519.x25519(s, (9).to_bytes(32, "little"))


def test_ed_to_curve_roundtrip_invariant():
    """scalarmult_base(ed_seed_to_curve_scalar(seed)) == ed_pub_to_curve_pub(ed_pub(seed)) — the
    libsodium-compatible relationship that lets a capsule seal to a device's published Ed25519 key."""
    for _ in range(8):
        seed = os.urandom(32)
        assert (x25519.ed_pub_to_curve_pub(ed25519.pub_from_seed(seed))
                == x25519.scalarmult_base(x25519.ed_seed_to_curve_scalar(seed)))


def test_ecdh_agreement_ephemeral_wrap():
    """The exact capsule pattern: sender does ephemeral×recipient_pub, recipient does
    own_scalar×ephemeral_pub — they agree."""
    recip = os.urandom(32)
    r_scalar = x25519.ed_seed_to_curve_scalar(recip)
    r_pub = x25519.ed_pub_to_curve_pub(ed25519.pub_from_seed(recip))
    eph = os.urandom(32)
    assert x25519.shared_secret(eph, r_pub) == x25519.shared_secret(r_scalar, x25519.scalarmult_base(eph))


def test_low_order_shared_secret_rejected():
    with pytest.raises(ValueError):
        x25519.shared_secret(os.urandom(32), b"\x00" * 32)


def test_y_equals_one_conversion_rejected():
    with pytest.raises(ValueError):
        x25519.ed_pub_to_curve_pub((1).to_bytes(32, "little"))      # y == 1 → Montgomery infinity


def test_curve_scalar_is_clamped():
    cs = x25519.ed_seed_to_curve_scalar(os.urandom(32))
    assert cs[0] & 0b111 == 0          # low 3 bits cleared
    assert cs[31] & 0x80 == 0          # top bit cleared
    assert cs[31] & 0x40 == 0x40       # bit 254 set
