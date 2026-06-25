"""chacha20poly1305.py — pure-Python RFC 8439 ChaCha20-Poly1305 AEAD. Zero deps (stdlib only).

This replaces the bundled hand-rolled SHA-256-CTR keystream (`_kdf_keystream`) + HMAC encrypt-then-MAC
so the crypto self-test can anchor to the RFC's *published* test vectors instead of self-minted ones,
and so the symmetric layer is one analyzed AEAD rather than a hand-assembled generic composition.

API (mirrors `ed25519.py`/`x25519.py`: bytes in, bytes out, no base64 inside the module):
  encrypt(key32, nonce12, plaintext, aad=b"") -> (ciphertext, tag16)
  decrypt(key32, nonce12, ciphertext, tag16, aad=b"") -> plaintext   # raises ValueError on auth fail

Nonce discipline: a (key, nonce) pair must never repeat. Every caller in `hv` uses a fresh random key
per message (a per-capsule CEK, a per-recipient ephemeral wrap key, a per-seal scrypt key), so nonce
reuse is structurally impossible regardless of the random 96-bit nonce.

Threat model (same as x25519.py): Python big-int math is NOT constant-time; defense rests on 0600
local keys + the signed, admission-gated journal — the real adversary is offline journal tampering,
which signing + admission already cover. The Poly1305 tag compare uses hmac.compare_digest."""

import hmac
import struct

_MASK = 0xFFFFFFFF
_CONST = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)   # "expand 32-byte k"
_P1305 = (1 << 130) - 5
_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def _rotl32(v, c):
    v &= _MASK
    return ((v << c) | (v >> (32 - c))) & _MASK


def _qr(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _MASK; s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _MASK; s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _MASK; s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _MASK; s[b] = _rotl32(s[b] ^ s[c], 7)


def _chacha20_block(key, counter, nonce):
    """RFC 8439 §2.3 — the 64-byte ChaCha20 block for a given 32-bit counter."""
    state = [*_CONST, *struct.unpack("<8I", key), counter & _MASK, *struct.unpack("<3I", nonce)]
    w = list(state)
    for _ in range(10):                                     # 20 rounds = 10 column+diagonal pairs
        _qr(w, 0, 4, 8, 12); _qr(w, 1, 5, 9, 13); _qr(w, 2, 6, 10, 14); _qr(w, 3, 7, 11, 15)
        _qr(w, 0, 5, 10, 15); _qr(w, 1, 6, 11, 12); _qr(w, 2, 7, 8, 13); _qr(w, 3, 4, 9, 14)
    return struct.pack("<16I", *((w[i] + state[i]) & _MASK for i in range(16)))


def _chacha20_xor(key, counter, nonce, data):
    out = bytearray()
    for i in range(0, len(data), 64):
        ks = _chacha20_block(key, counter + (i >> 6), nonce)
        out += bytes(b ^ ks[j] for j, b in enumerate(data[i:i + 64]))
    return bytes(out)


def _poly1305_mac(msg, otk):
    """RFC 8439 §2.5 — one-time Poly1305 over `msg` with 32-byte one-time key `otk`."""
    r = int.from_bytes(otk[0:16], "little") & _CLAMP
    s = int.from_bytes(otk[16:32], "little")
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i:i + 16]
        acc = ((acc + int.from_bytes(block + b"\x01", "little")) * r) % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(x):
    return b"\x00" * (-len(x) % 16)


def _mac_data(aad, ct):
    return aad + _pad16(aad) + ct + _pad16(ct) + struct.pack("<QQ", len(aad), len(ct))


def encrypt(key, nonce, plaintext, aad=b""):
    """Seal: returns (ciphertext, 16-byte tag). RFC 8439 §2.8."""
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")
    otk = _chacha20_block(key, 0, nonce)[:32]               # §2.6 poly1305_key_gen (counter 0)
    ct = _chacha20_xor(key, 1, nonce, plaintext)            # data starts at counter 1
    tag = _poly1305_mac(_mac_data(aad, ct), otk)
    return ct, tag


def decrypt(key, nonce, ciphertext, tag, aad=b""):
    """Open: verify the tag (constant-time) BEFORE decrypting. Raises ValueError on any mismatch."""
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")
    otk = _chacha20_block(key, 0, nonce)[:32]
    if not hmac.compare_digest(_poly1305_mac(_mac_data(aad, ciphertext), otk), tag):
        raise ValueError("authentication failed (tampered, wrong key, or corrupt)")
    return _chacha20_xor(key, 1, nonce, ciphertext)
