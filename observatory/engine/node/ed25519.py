"""Ed25519 keys and signatures as the contract admits them (contract `src/ed25519.ts`, DEC-0045, CT-29).

A key of small order — the identity, the points of order 2, 4 and 8, and the non-canonical `y = p`, `y = p + 1` — lets
anyone forge a signature without a private key, and some OpenSSL builds accept such a signature. Every verification
here refuses those keys first, so the answer does not depend on the library underneath.
"""
from __future__ import annotations

from typing import Union

# The list and the predicate live in the engine's top-level `ed25519_order.py`, so the delegation verifier and the
# release-signature check share them (OBS-162); they are re-exported here for the node's modules.
from ed25519_order import SMALL_ORDER_ENCODINGS, is_small_order_key  # noqa: E402,F401


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
