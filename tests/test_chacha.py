"""ChaCha20-Poly1305 (RFC 8439) — the bundled pure-Python AEAD that replaced the hand-rolled
SHA-256-CTR keystream. Anchored to the RFC's *published* §2.8.2 vector, not a self-minted one."""

import binascii
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import chacha20poly1305 as c   # noqa: E402

_h = binascii.unhexlify

# RFC 8439 §2.8.2
KEY = _h("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
NONCE = _h("070000004041424344454647")
AAD = _h("50515253c0c1c2c3c4c5c6c7")
PT = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one "
      b"tip for the future, sunscreen would be it.")
CT = _h("d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
        "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
        "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
        "3ff4def08e4b7a9de576d26586cec64b6116")
TAG = _h("1ae10b594f09e26a7e902ecbd0600691")


def test_rfc8439_known_answer():
    ct, tag = c.encrypt(KEY, NONCE, PT, AAD)
    assert ct == CT
    assert tag == TAG


def test_roundtrip():
    ct, tag = c.encrypt(KEY, NONCE, PT, AAD)
    assert c.decrypt(KEY, NONCE, ct, tag, AAD) == PT


def test_empty_plaintext_and_aad():
    ct, tag = c.encrypt(KEY, NONCE, b"", b"")
    assert ct == b""
    assert c.decrypt(KEY, NONCE, ct, tag, b"") == b""


def test_tampered_ciphertext_rejected():
    ct, tag = c.encrypt(KEY, NONCE, PT, AAD)
    bad = bytes([ct[0] ^ 1]) + ct[1:]
    try:
        c.decrypt(KEY, NONCE, bad, tag, AAD)
        assert False, "tampered ciphertext must raise"
    except ValueError:
        pass


def test_tampered_aad_rejected():
    ct, tag = c.encrypt(KEY, NONCE, PT, AAD)
    try:
        c.decrypt(KEY, NONCE, ct, tag, AAD + b"x")
        assert False, "changed AAD must raise"
    except ValueError:
        pass


def test_wrong_key_rejected():
    ct, tag = c.encrypt(KEY, NONCE, PT, AAD)
    other = bytes([KEY[0] ^ 0xFF]) + KEY[1:]
    try:
        c.decrypt(other, NONCE, ct, tag, AAD)
        assert False, "wrong key must raise"
    except ValueError:
        pass


def test_bad_lengths_rejected():
    for bad in [(b"\x00" * 31, NONCE), (KEY, b"\x00" * 11)]:
        try:
            c.encrypt(bad[0], bad[1], PT, AAD)
            assert False, "bad key/nonce length must raise"
        except ValueError:
            pass
