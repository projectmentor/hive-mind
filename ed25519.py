"""Pure-Python Ed25519 (RFC 8032), zero dependencies — bundled so `hv`, `hv verify` and the installer can
sign and check signatures anywhere Python runs, with no pip install and no system crypto.

Fast since v1.20.1 (#70): extended twisted-Edwards coordinates (one modular inversion per encoding
instead of one per point addition), a 4-bit window for [h]A and a precomputed table for the base point.
The public-domain reference it replaced took on the order of a second per verify; this takes a few ms.

ACCEPT-SET PARITY IS DELIBERATE. Every node must agree on which journal entries are validly signed, so
`verify` accepts exactly what the reference accepted, no more and no less, and `sign` is deterministic and
byte-identical to it. `tests/test_ed25519.py` checks both against the frozen reference in
`tests/_ed25519_reference.py`. Kept on purpose, although strict RFC 8032 would reject them:
  - no `S < l` check (a malleated `S + l` verifies);
  - `y` is the low 255 bits of an encoding, without a `y < q` check (a non-canonical `y >= q` decodes);
  - `x = 0` with the sign bit set decodes (to `x = q`);
  - the cofactorless equation `[S]B == R + [h]A`, with `h` never reduced below the group order.
Tightening any of these would be a consensus change: a separate, versioned decision.

NOT SIDE-CHANNEL HARDENED (neither was the reference): Python big-integer arithmetic is not constant time,
so do not sign on hardware whose timing an attacker can measure closely.

API:
    seed  = 32 random bytes (the private key); keep secret.
    pub   = pub_from_seed(seed)          -> 32 bytes (publish this).
    sig   = sign(message, seed)          -> 64 bytes (detached signature).
    ok    = verify(message, sig, pub)    -> bool (never raises).
"""
import hashlib

_q = 2 ** 255 - 19
_l = 2 ** 252 + 27742317777372353535851937790883648493
_GROUP_ORDER = 8 * _l                     # every point on the curve has order dividing 8l
_d = -121665 * pow(121666, _q - 2, _q) % _q
_d2 = 2 * _d % _q
_I = pow(2, (_q - 1) // 4, _q)            # sqrt(-1)


def _inv(x):
    return pow(x, _q - 2, _q)


def _xrecover(y):
    """The reference's x-recovery, unchanged, including its lack of a second square check: an x that does
    not square back to xx is rejected by the on-curve test in _decodepoint, exactly as before."""
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


# Points are extended coordinates (X, Y, Z, T): x = X/Z, y = Y/Z, x*y = T/Z.
_ZERO = (0, 1, 1, 0)
_By = 4 * _inv(5) % _q
_Bx = _xrecover(_By)
_B = (_Bx, _By, 1, _Bx * _By % _q)


def _pt_add(P, Q):
    """Unified addition (add-2008-hwcd-3, a = -1). Complete on this curve: no exceptional inputs,
    including the identity and small-order points."""
    X1, Y1, Z1, T1 = P
    X2, Y2, Z2, T2 = Q
    A = (Y1 - X1) * (Y2 - X2) % _q
    B = (Y1 + X1) * (Y2 + X2) % _q
    C = T1 * _d2 * T2 % _q
    D = 2 * Z1 * Z2 % _q
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _q, G * H % _q, F * G % _q, E * H % _q)


def _pt_dbl(P):
    """Doubling (dbl-2008-hwcd, a = -1)."""
    X1, Y1, Z1, _ = P
    A = X1 * X1 % _q
    B = Y1 * Y1 % _q
    C = 2 * Z1 * Z1 % _q
    H = A + B
    E = H - (X1 + Y1) * (X1 + Y1) % _q
    G = A - B
    F = C + G
    return (E * F % _q, G * H % _q, F * G % _q, E * H % _q)


def _pt_eq(P, Q):
    """Projective equality: the same affine point, whatever the Z."""
    return ((P[0] * Q[2] - Q[0] * P[2]) % _q == 0
            and (P[1] * Q[2] - Q[1] * P[2]) % _q == 0)


def _mul(P, e):
    """[e]P for any point P and any e >= 0 (4-bit fixed window)."""
    table = [_ZERO, P]
    for _ in range(14):
        table.append(_pt_add(table[-1], P))
    R = _ZERO
    for i in range((e.bit_length() + 3) // 4 * 4 - 4, -1, -4):
        R = _pt_dbl(_pt_dbl(_pt_dbl(_pt_dbl(R))))
        w = (e >> i) & 15
        if w:
            R = _pt_add(R, table[w])
    return R


_BASE_TABLE = None          # _BASE_TABLE[i][w] = [w * 16**i]B, built on first use (~10 ms)


def _mul_base(e):
    """[e]B for 0 <= e < 2**256. Callers reduce e mod l first (B has prime order l)."""
    global _BASE_TABLE
    if _BASE_TABLE is None:
        rows, P = [], _B
        for _ in range(64):
            row = [_ZERO, P]
            for _ in range(14):
                row.append(_pt_add(row[-1], P))
            rows.append(row)
            P = _pt_dbl(_pt_dbl(_pt_dbl(_pt_dbl(P))))
        _BASE_TABLE = rows
    R, i = _ZERO, 0
    while e:
        w = e & 15
        if w:
            R = _pt_add(R, _BASE_TABLE[i][w])
        e >>= 4
        i += 1
    return R


def _encode(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % _q, P[1] * zi % _q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decodepoint(s):
    """The reference's decoding: y is the low 255 bits (no y < q check), x takes the sign bit's parity
    (so x = 0 with the sign bit set becomes q), and the point must satisfy the curve equation."""
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if x & 1 != s[31] >> 7:
        x = _q - x
    if (-x * x + y * y - 1 - _d * x * x * y * y) % _q != 0:
        raise ValueError("point not on curve")
    x, y = x % _q, y % _q
    return (x, y, 1, x * y % _q)


def _secret_scalar(seed):
    h = hashlib.sha512(seed).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    return h, a


def pub_from_seed(seed):
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    _, a = _secret_scalar(seed)
    return _encode(_mul_base(a % _l))


def sign(message, seed):
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h, a = _secret_scalar(seed)
    pk = _encode(_mul_base(a % _l))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little")
    Rb = _encode(_mul_base(r % _l))               # [r]B == [r mod l]B
    k = int.from_bytes(hashlib.sha512(Rb + pk + message).digest(), "little")
    return Rb + ((r + k * a) % _l).to_bytes(32, "little")


def verify(message, sig, pub):
    """True iff `sig` is a valid Ed25519 signature of `message` under `pub`, by the reference's rules
    (see the module docstring). Never raises."""
    try:
        if len(sig) != 64 or len(pub) != 32:
            return False
        sig, pub = bytes(sig), bytes(pub)
        R = _decodepoint(sig[:32])
        A = _decodepoint(pub)
        S = int.from_bytes(sig[32:], "little")                 # no S < l check (reference parity)
        # The reference hashed its re-encoding of R, which for every encoding it can decode round-trips to
        # the original 32 bytes, so hashing the raw bytes is identical (and is what RFC 8032 specifies).
        h = int.from_bytes(hashlib.sha512(sig[:32] + pub + bytes(message)).digest(), "little")
        # [S]B: B has prime order l, so S may be reduced mod l. [h]A: A may carry a torsion component, so h
        # is reduced only mod the full group order 8l, never mod l.
        return _pt_eq(_mul_base(S % _l), _pt_add(R, _mul(A, h % _GROUP_ORDER)))
    except Exception:
        return False
