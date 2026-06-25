"""x25519.py — pure-Python X25519 (RFC 7748) + Ed25519↔Curve25519 conversion (libsodium-compatible).

Zero dependencies (stdlib `hashlib` only), bundled for offline verification — mirrors `ed25519.py`.
This lets the hive seal secrets ("capsules") to a device's ALREADY-PUBLISHED Ed25519 key: the device
key is converted to its Curve25519 counterpart and used as an X25519 recipient. No new key material
is distributed.

SLOW BY DESIGN. Python big-int arithmetic is variable-time, so this is NOT constant-time. The
Montgomery ladder runs a uniform operation sequence with no secret-dependent branches (removing the
obvious data-dependent path), but a co-resident attacker measuring fine-grained timing could still
extract scalar bits. The threat model is 0600 on-disk keys + offline journal tampering (which the
signature + admission gates cover), NOT a local timing oracle. Do NOT use in a hot loop.

Public API (bytes in/out, little-endian; no base64 — encode at the `hv` call site, like ed25519.py):
  x25519(scalar32, u32) -> bytes32           # RFC 7748 named function (clamps the scalar)
  scalarmult_base(scalar32) -> bytes32        # x25519(scalar, 9)
  shared_secret(scalar32, peer_pub32)->bytes32# x25519, but RAISES on the all-zero (low-order) output
  ed_pub_to_curve_pub(ed_pub32) -> bytes32    # Montgomery u = (1+y)/(1-y); raises on the y==1 point
  ed_seed_to_curve_scalar(ed_seed32)->bytes32 # sha512(seed)[:32] then clamp (libsodium sk_to_curve)

Invariant (the round-trip these enable):
  x25519(ed_seed_to_curve_scalar(seed), scalarmult_base(...9...)) == ed_pub_to_curve_pub(<ed pub of seed>)
"""

import hashlib

_P = 2 ** 255 - 19
_A24 = 121665                      # (486662 - 2) // 4, the Montgomery constant for Curve25519
_NINE = (9).to_bytes(32, "little")


def _decode_le(b):
    return int.from_bytes(b, "little")


def _encode_le(n):
    return (n % _P).to_bytes(32, "little")


def _decode_u(b):
    if len(b) != 32:
        raise ValueError("u-coordinate must be 32 bytes")
    a = bytearray(b)
    a[31] &= 0x7f                  # RFC 7748: ignore the most-significant bit of the u-coordinate
    return _decode_le(bytes(a))


def _clamp(b):
    if len(b) != 32:
        raise ValueError("scalar must be 32 bytes")
    a = bytearray(b)
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return _decode_le(bytes(a))


def _cswap(swap, a, b):
    """Conditional swap on swap∈{0,1}. mask = -swap is 0 or all-ones (Python two's-complement int)."""
    mask = (-swap) & (a ^ b)
    return a ^ mask, b ^ mask


def _ladder(k_int, u_int):
    """RFC 7748 §5 Montgomery ladder (X-only scalar multiplication), 255-bit scalar."""
    x1 = u_int
    x2, z2 = 1, 0
    x3, z3 = u_int, 1
    swap = 0
    for t in range(254, -1, -1):
        k_t = (k_int >> t) & 1
        swap ^= k_t
        x2, x3 = _cswap(swap, x2, x3)
        z2, z3 = _cswap(swap, z2, z3)
        swap = k_t
        A = (x2 + z2) % _P
        AA = (A * A) % _P
        B = (x2 - z2) % _P
        BB = (B * B) % _P
        E = (AA - BB) % _P
        C = (x3 + z3) % _P
        D = (x3 - z3) % _P
        DA = (D * A) % _P
        CB = (C * B) % _P
        x3 = ((DA + CB) % _P) * ((DA + CB) % _P) % _P
        z3 = (x1 * (((DA - CB) % _P) * ((DA - CB) % _P) % _P)) % _P
        x2 = (AA * BB) % _P
        z2 = (E * ((AA + _A24 * E) % _P)) % _P
    x2, x3 = _cswap(swap, x2, x3)
    z2, z3 = _cswap(swap, z2, z3)
    return (x2 * pow(z2, _P - 2, _P)) % _P


def x25519(scalar32, u32):
    """RFC 7748 X25519(k, u): clamp the scalar, decode the u-coordinate, scalar-multiply."""
    return _encode_le(_ladder(_clamp(scalar32), _decode_u(u32)))


def scalarmult_base(scalar32):
    return x25519(scalar32, _NINE)


def shared_secret(scalar32, peer_pub32):
    """ECDH shared secret. RAISES on the all-zero output (RFC 7748 §6.1 contributory check): a peer
    that contributed a low-order point would force a predictable/zero secret — never derive a key
    from it."""
    out = x25519(scalar32, peer_pub32)
    if out == b"\x00" * 32:
        raise ValueError("degenerate X25519 shared secret (low-order peer point)")
    return out


def ed_pub_to_curve_pub(ed_pub32):
    """Map an Ed25519 public key to its Curve25519 (Montgomery) public key: u = (1 + y)/(1 - y).
    The Ed25519 encoding stores y in the low 255 bits; the top bit of byte 31 is x's sign and is
    ignored (X25519 is x-only, so a point and its negation share the same u — the shared secret is
    unaffected). RAISES on y == 1 (which maps to the point at infinity)."""
    if len(ed_pub32) != 32:
        raise ValueError("ed25519 public key must be 32 bytes")
    y = _decode_le(ed_pub32) & ((1 << 255) - 1)
    denom = (1 - y) % _P
    if denom == 0:
        raise ValueError("ed25519 point maps to Montgomery infinity (y == 1)")
    u = ((1 + y) * pow(denom, _P - 2, _P)) % _P
    return _encode_le(u)


def ed_seed_to_curve_scalar(ed_seed32):
    """Derive the Curve25519 secret scalar from an Ed25519 seed, the libsodium
    `crypto_sign_ed25519_sk_to_curve25519` way: sha512(seed)[:32], then apply the X25519 clamp. This
    is the same secret scalar Ed25519 derives, so it matches `ed_pub_to_curve_pub` of the same key."""
    if len(ed_seed32) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    h = bytearray(hashlib.sha512(ed_seed32).digest()[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)
