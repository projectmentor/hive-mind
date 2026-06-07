"""Pure-Python Ed25519 (RFC 8032), zero dependencies — bundled so `hv verify` and the installer
can check a release signature anywhere Python runs, with no pip install and no system crypto.

Reference implementation (public domain, from the Ed25519 authors), wrapped in a small,
named API. Slow by design (one signature per verify is fine); do NOT use in a hot loop.

API:
    seed  = 32 random bytes (the private key); keep secret.
    pub   = pub_from_seed(seed)          -> 32 bytes (publish this).
    sig   = sign(message, seed)          -> 64 bytes (detached signature).
    ok    = verify(message, sig, pub)    -> bool (never raises).
"""
import hashlib

_b = 256
_q = 2 ** 255 - 19
_l = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m):
    return hashlib.sha512(m).digest()


def _expmod(b, e, m):
    r = 1
    b %= m
    while e:
        if e & 1:
            r = (r * b) % m
        b = (b * b) % m
        e >>= 1
    return r


def _inv(x):
    return _expmod(x, _q - 2, _q)


_d = -121665 * _inv(121666) % _q
_I = _expmod(2, (_q - 1) // 4, _q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = _expmod(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = 4 * _inv(5) % _q
_Bx = _xrecover(_By)
_B = [_Bx % _q, _By % _q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return [x3 % _q, y3 % _q]


def _scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _encodeint(y):
    bits = [(y >> i) & 1 for i in range(_b)]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8))


def _encodepoint(P):
    x, y = P
    bits = [(y >> i) & 1 for i in range(_b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8))


def _Hint(m):
    h = _H(m)
    return sum(2 ** i * _bit(h, i) for i in range(2 * _b))


def _secret_scalar(seed):
    h = _H(seed)
    a = 2 ** (_b - 2) + sum(2 ** i * _bit(h, i) for i in range(3, _b - 2))
    return h, a


def pub_from_seed(seed):
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    _, a = _secret_scalar(seed)
    return _encodepoint(_scalarmult(_B, a))


def sign(message, seed):
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h, a = _secret_scalar(seed)
    pk = _encodepoint(_scalarmult(_B, a))
    r = _Hint(h[_b // 8:_b // 4] + message)
    R = _scalarmult(_B, r)
    S = (r + _Hint(_encodepoint(R) + pk + message) * a) % _l
    return _encodepoint(R) + _encodeint(S)


def _isoncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodeint(s):
    return sum(2 ** i * _bit(s, i) for i in range(0, _b))


def _decodepoint(s):
    y = sum(2 ** i * _bit(s, i) for i in range(0, _b - 1))
    x = _xrecover(y)
    if x & 1 != _bit(s, _b - 1):
        x = _q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("point not on curve")
    return P


def verify(message, sig, pub):
    """True iff `sig` is a valid Ed25519 signature of `message` under `pub`. Never raises."""
    try:
        if len(sig) != 64 or len(pub) != 32:
            return False
        R = _decodepoint(sig[0:32])
        A = _decodepoint(pub)
        S = _decodeint(sig[32:64])
        h = _Hint(_encodepoint(R) + pub + message)
        return _scalarmult(_B, S) == _edwards(R, _scalarmult(A, h))
    except Exception:
        return False
