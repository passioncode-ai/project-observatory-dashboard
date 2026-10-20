"""Ed25519 public keys of small order: the one predicate every Ed25519 verification in the engine asks first.

Contract `src/ed25519.ts` (`isSmallOrderKey`, DEC-0045 item 10, CT-29). A key of small order (the identity, the
points of order 2, 4 and 8, and the non-canonical `y = p`, `y = p + 1`) lets anyone forge a signature without a
private key, and some Ed25519 implementations accept such a signature. The delegation verifier (`delegation.py`),
the release-signature check (`release_signature.py`) and the agent-network node (`node/ed25519.py`, which
re-exports this) refuse those keys before verifying anything under them, so the answer does not depend on the
library underneath.

A top-level module, not `node.ed25519` alone: `tools/node.py` shadows the `node` package for a program started from
`tools/` (the keyserver, the HTTP doors), and this check must load wherever a token is verified. Standard library
only.
"""
from __future__ import annotations

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
