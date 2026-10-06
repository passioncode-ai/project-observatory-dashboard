"""A throwaway OpenPGP release key for tests: Ed25519, v4, the shape CI signs with.

`release_signature` must be tested against signatures it did not help make, so this
module builds the packets from RFC 4880 / RFC 9580 directly — independently of the parser —
and the real 0.17.3 vectors in test_release_signature.py pin both to what gpg emits.
The key is generated per process and never leaves memory.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

OID = bytes.fromhex("2B06010401DA470F01")


def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def armor(kind: str, data: bytes) -> str:
    b64 = base64.b64encode(data).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    crc = base64.b64encode(_crc24(data).to_bytes(3, "big")).decode()
    return "\n".join([f"-----BEGIN PGP {kind}-----", "", *lines, f"={crc}", f"-----END PGP {kind}-----", ""])


def packet(tag: int, body: bytes) -> bytes:
    """New-format packet header (RFC 4880 4.2.2)."""
    n = len(body)
    if n < 192:
        head = bytes([n])
    elif n < 8384:
        n -= 192
        head = bytes([(n >> 8) + 192, n & 0xFF])
    else:
        head = b"\xff" + struct.pack(">I", n)
    return bytes([0xC0 | tag]) + head + body


def mpi(value: bytes) -> bytes:
    value = value.lstrip(b"\0") or b"\0"
    return struct.pack(">H", (len(value) - 1) * 8 + value[0].bit_length()) + value


def subpacket(kind: int, value: bytes) -> bytes:
    return bytes([len(value) + 1, kind]) + value


class Key:
    def __init__(self, created: int | None = None):
        self.private = Ed25519PrivateKey.generate()
        point = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.body = (b"\x04" + struct.pack(">I", created or int(time.time())) + bytes([22, len(OID)]) + OID
                     + mpi(b"\x40" + point))
        self.fingerprint = hashlib.sha1(b"\x99" + struct.pack(">H", len(self.body)) + self.body).hexdigest().upper()

    def public_armored(self) -> str:
        return armor("PUBLIC KEY BLOCK", packet(6, self.body))

    def pinned(self) -> tuple[dict, ...]:
        return ({"fingerprint": self.fingerprint, "name": "test release key", "armored": self.public_armored()},)

    def sign(self, data: bytes, *, hash_algo: int = 10, sig_type: int = 0x00, issuer: bool = True) -> str:
        """A detached signature over `data`, armored as gpg --armor --detach-sign writes it."""
        hashed = subpacket(2, struct.pack(">I", int(time.time())))
        if issuer:
            hashed += subpacket(33, b"\x04" + bytes.fromhex(self.fingerprint))
        prefix = bytes([4, sig_type, 22, hash_algo]) + struct.pack(">H", len(hashed)) + hashed
        h = {8: hashlib.sha256, 9: hashlib.sha384, 10: hashlib.sha512}[hash_algo]
        digest = h(data + prefix + b"\x04\xff" + struct.pack(">I", len(prefix))).digest()
        raw = self.private.sign(digest)
        unhashed = subpacket(16, bytes.fromhex(self.fingerprint[-16:]))
        body = prefix + struct.pack(">H", len(unhashed)) + unhashed + digest[:2] + mpi(raw[:32]) + mpi(raw[32:])
        return armor("SIGNATURE", packet(2, body))
