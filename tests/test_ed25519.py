"""The fast Ed25519 (#70) against RFC 8032 and against the frozen reference it replaced.

Nodes must agree on which journal entries are validly signed, so `verify` must accept exactly what the
reference accepted (including its permissive decoding) and `sign` must be byte-identical. The reference
lives in tests/_ed25519_reference.py and is slow, so it is called sparingly here.
"""
import hashlib
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ed25519 as fast                        # noqa: E402
import _ed25519_reference as ref              # noqa: E402

Q = fast._q
L = fast._l

# RFC 8032 §7.1: TEST 1, TEST 2, TEST 3 and TEST SHA(abc). (secret, public, message, signature)
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf", hashlib.sha512(b"abc").hexdigest(),
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


def _enc(y, sign=0):
    """A raw 32-byte point encoding: y in the low 255 bits (unreduced), the x sign in the top bit."""
    return (y | (sign << 255)).to_bytes(32, "little")


def test_rfc8032_vectors():
    for sk, pk, msg, sig in RFC8032:
        sk, pk, msg, sig = map(bytes.fromhex, (sk, pk, msg, sig))
        assert fast.pub_from_seed(sk) == pk
        assert fast.sign(msg, sk) == sig
        assert fast.verify(msg, sig, pk)
        assert not fast.verify(msg + b"x", sig, pk)


def test_sign_and_pub_are_byte_identical_to_the_reference():
    for i in range(3):
        seed, msg = os.urandom(32), os.urandom(17 * i)
        assert fast.pub_from_seed(seed) == ref.pub_from_seed(seed)
        assert fast.sign(msg, seed) == ref.sign(msg, seed)


def test_reference_permissive_cases_are_still_accepted():
    """What the reference accepts although strict RFC 8032 would not: dropping any of these would make an
    upgraded node reject entries an older node accepted."""
    seed, msg = os.urandom(32), b"a journal entry"
    pub, sig = fast.pub_from_seed(seed), fast.sign(msg, seed)
    S = int.from_bytes(sig[32:], "little")
    for k in (1, 2, 3):                                      # malleated S + k*l (no S < l check)
        mal = sig[:32] + (S + k * L).to_bytes(32, "little")
        assert fast.verify(msg, mal, pub) and ref.verify(msg, mal, pub), k


def test_decoding_matches_the_reference_on_edge_encodings():
    named = {
        "non-canonical y = q+3 decodes (y kept unreduced)": _enc(Q + 3),
        "non-canonical identity y = q+1": _enc(Q + 1),
        "identity with sign bit 1 (x = q)": _enc(1, 1),
        "order-2 point y = q-1": _enc(Q - 1),
        "order-4 point y = 0": _enc(0),
        "all ones": _enc(2 ** 255 - 1, 1),
        "off-curve y = 2": _enc(2),
    }
    for name, s in named.items():
        def outcome(mod):
            try:
                mod._decodepoint(s)
                return "ok"
            except Exception:
                return "reject"
        assert outcome(fast) == outcome(ref), name


def test_raw_r_bytes_are_hashed_like_the_reference():
    """Small-order keys make [h]A depend on h mod 4 (or mod 2), so whether a signature (R, S=0) verifies
    depends on exactly which bytes of R were hashed. Each family mixes accepts and rejects over eight
    messages; an implementation that hashed a canonical re-encoding of a non-canonical R, or of x = q with
    the sign bit set, would give a different pattern."""
    families = {
        "R order-4, non-canonical y = q; A order-4": (_enc(Q), _enc(0)),
        "R order-4, y = q, sign 1; A order-4, sign 1": (_enc(Q, 1), _enc(0, 1)),
        "R identity with sign bit 1 (x = q); A order-4": (_enc(1, 1), _enc(0)),
        "R identity, non-canonical y = q+1; A order-2": (_enc(Q + 1), _enc(Q - 1)),
    }
    for name, (r_bytes, pub) in families.items():
        sig = r_bytes + bytes(32)
        got = [fast.verify(b"edge-%d" % i, sig, pub) for i in range(8)]
        want = [ref.verify(b"edge-%d" % i, sig, pub) for i in range(8)]
        assert got == want, name
        assert any(got) and not all(got), f"{name}: the family no longer discriminates"


def test_rejections_match_the_reference():
    seed, msg = os.urandom(32), b"another journal entry"
    pub, sig = fast.pub_from_seed(seed), fast.sign(msg, seed)
    other = fast.pub_from_seed(os.urandom(32))
    cases = {
        "valid": (msg, sig, pub),
        "wrong message": (msg + b".", sig, pub),
        "flipped bit in S": (msg, sig[:40] + bytes([sig[40] ^ 1]) + sig[41:], pub),
        "flipped R sign bit": (msg, sig[:31] + bytes([sig[31] ^ 0x80]) + sig[32:], pub),
        "S = 0": (msg, sig[:32] + bytes(32), pub),
        "all-zero signature": (msg, bytes(64), pub),
        "zero key": (msg, sig, bytes(32)),
        "another key": (msg, sig, other),
        "off-curve R": (msg, _enc(2) + sig[32:], pub),
    }
    for name, (m, s, p) in cases.items():
        assert fast.verify(m, s, p) == ref.verify(m, s, p), name
    assert fast.verify(*cases["valid"]) and not fast.verify(*cases["wrong message"])


def test_verify_never_raises():
    for args in ((b"m", b"short", bytes(32)), (b"m", bytes(64), b"short"), (None, bytes(64), bytes(32)),
                 (b"m", None, bytes(32)), ("text", bytes(64), bytes(32))):
        assert fast.verify(*args) is False
