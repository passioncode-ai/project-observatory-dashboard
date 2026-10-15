"""Ed25519 keys and signatures as the contract admits them (contract `src/ed25519.ts`, DEC-0045, CT-29).

A key of small order — the identity, the points of order 2, 4 and 8, and the non-canonical `y = p`, `y = p + 1` — lets
anyone forge a signature without a private key, and some OpenSSL builds accept such a signature. Every verification
here refuses those keys first, so the answer does not depend on the library underneath.
"""
from __future__ import annotations

from typing import Union

SMALL_ORDER_ENCODINGS = (
    '0000000000000000000000000000000000000000000000000000000000000000',  # y = 0: order 4
    '0100000000000000000000000000000000000000000000000000000000000000',  # y = 1: the identity
    '26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05',  # order 8
    'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a',  # order 8
    'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',  # y = p − 1: order 2
    'edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',  # y = p, non-canonical 0
    'eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',  # y = p + 1, non-canonical 1
)
_SMALL_ORDER = frozenset(SMALL_ORDER_ENCODINGS)


def is_small_order_key(raw: bytes) -> bool:
    """True when the key must be refused: not 32 bytes, or its encoding with the sign bit cleared is a small-order one."""
    if len(raw) != 32:
        return True
    cleared = bytearray(raw)
    cleared[31] &= 0x7F
    return bytes(cleared).hex() in _SMALL_ORDER


def verify(raw_key: bytes, message: Union[bytes, str], signature: bytes) -> bool:
    """RFC 8032 Ed25519 under a 32-byte key of full order; a 64-byte signature. Needs the engine's `full` extra."""
    if is_small_order_key(raw_key) or len(signature) != 64:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:                           # the engine's `full` extra
        raise RuntimeError('Ed25519 needs the engine full extra (cryptography)') from error
    data = message.encode('utf-8') if isinstance(message, str) else message
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, data)
    except (InvalidSignature, ValueError):
        return False
    return True
