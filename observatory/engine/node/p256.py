"""P-256 public keys and ES256 signatures, as the relay family admits them (contract `src/p256.ts`, DEC-0047, DEC-0053,
DEC-0066).

A key is SEC 1 octets (65 bytes, `0x04 ‖ X ‖ Y`) or an RFC 7518 JWK; `key_refusal` is SEC 1 v2 §3.2.2 and runs before
anything is verified or sealed under a key, because an off-curve point fed to ECDH leaks the private key. A signature is
raw `r ‖ s` (64 bytes) in base64url, and only its low-S form verifies (s ≤ n/2): then nobody but the signer can make a
second valid spelling of it. `sign_es256` signs and normalises; any other signer — a Secure Enclave, a TPM — passes its
output through `to_low_s`.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Optional, Union

P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = 0xffffffff00000001000000000000000000000000fffffffffffffffffffffffc
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
HALF_N = N // 2

#: Why a key is refused; `None` from `key_refusal` means a valid public key.
REFUSALS = ('bad-encoding', 'point-at-infinity', 'coordinate-out-of-range', 'not-on-curve')

Key = Union[bytes, dict]
_B64URL = re.compile(r'^[A-Za-z0-9_-]*$')


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def b64url_decode(text: str) -> Optional[bytes]:
    """The bytes of an unpadded base64url string, or None when it is not one."""
    if not isinstance(text, str) or not _B64URL.match(text) or len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    except (ValueError, TypeError):
        return None


def b64url_canonical(text: Any) -> Optional[bytes]:
    """The bytes of a base64url string that is their only spelling (re-encoding gives the same text), else None."""
    raw = b64url_decode(text) if isinstance(text, str) else None
    return raw if raw is not None and b64url_encode(raw) == text else None


def _coordinate(value: Any) -> Optional[int]:
    raw = b64url_canonical(value)
    # RFC 7518 §6.2.1.2: the full 32-byte coordinate, leading zero octets included.
    if raw is None or len(raw) != 32:
        return None
    return int.from_bytes(raw, 'big')


def _decode(key: Any):
    if isinstance(key, (bytes, bytearray)):
        if len(key) == 1 and key[0] == 0x00:
            return 'point-at-infinity'
        if len(key) != 65 or key[0] != 0x04:
            return 'bad-encoding'
        return int.from_bytes(key[1:33], 'big'), int.from_bytes(key[33:], 'big')
    if not isinstance(key, dict) or key.get('kty') != 'EC' or key.get('crv') != 'P-256':
        return 'bad-encoding'
    x, y = _coordinate(key.get('x')), _coordinate(key.get('y'))
    if x is None or y is None:
        return 'bad-encoding'
    return x, y


def key_refusal(key: Any) -> Optional[str]:
    """SEC 1 §3.2.2: None for a point of the group other than the identity, else why the key is refused. The fourth
    step (nQ = O) holds for every P-256 point because the cofactor is 1."""
    point = _decode(key)
    if isinstance(point, str):
        return point
    x, y = point
    if x >= P or y >= P:
        return 'coordinate-out-of-range'
    if (y * y) % P != (x * x * x + A * x + B) % P:
        return 'not-on-curve'
    return None


def is_ed25519_key(key: Any) -> bool:
    """An Ed25519 key (32 raw bytes, or a JWK of kty OKP / crv Ed25519): a relay names it `unsupported-alg`."""
    if isinstance(key, (bytes, bytearray)):
        return len(key) == 32
    return isinstance(key, dict) and key.get('kty') == 'OKP' and key.get('crv') == 'Ed25519'


def point(key: Key) -> bytes:
    """The 65 SEC 1 octets of a valid key in either form; raises ValueError on a refused key."""
    refusal = key_refusal(key)
    if refusal is not None:
        raise ValueError('bad-key: ' + refusal)
    if isinstance(key, (bytes, bytearray)):
        return bytes(key)
    return b'\x04' + b64url_decode(key['x']) + b64url_decode(key['y'])


def jwk(key: Key) -> dict:
    octets = point(key)
    return {'kty': 'EC', 'crv': 'P-256', 'x': b64url_encode(octets[1:33]), 'y': b64url_encode(octets[33:])}


def is_low_s(raw: bytes) -> bool:
    return len(raw) == 64 and int.from_bytes(raw[32:], 'big') <= HALF_N


def to_low_s(raw: bytes) -> bytes:
    """(r, n − s) when s > n/2, else the same bytes; both verify under plain ECDSA."""
    if len(raw) != 64:
        raise ValueError('an ES256 signature is 64 bytes (r ‖ s)')
    s = int.from_bytes(raw[32:], 'big')
    return raw if s <= HALF_N else raw[:32] + (N - s).to_bytes(32, 'big')


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
        from cryptography.exceptions import InvalidSignature
    except ImportError as error:                           # the engine's `full` extra
        raise RuntimeError('ES256 needs the engine full extra (cryptography)') from error
    return hashes, ec, decode_dss_signature, encode_dss_signature, InvalidSignature


def sign_es256(private_key: Any, data: Union[str, bytes]) -> str:
    """ES256 over `data` (a string is UTF-8) by a `cryptography` P-256 private key: raw r ‖ s, low-S, base64url."""
    hashes, ec, decode_dss_signature, _, _ = _crypto()
    message = data.encode('utf-8') if isinstance(data, str) else data
    r, s = decode_dss_signature(private_key.sign(message, ec.ECDSA(hashes.SHA256())))
    return b64url_encode(to_low_s(r.to_bytes(32, 'big') + s.to_bytes(32, 'big')))


def verify_es256(key: Any, data: Union[str, bytes], sig: Any):
    """The contract's `verifyEs256`: 'unsupported-alg' for an Ed25519 key, 'bad-key' for a key SEC 1 refuses, False for a
    signature that is not 64 canonical base64url bytes, is high-S or does not verify, True otherwise."""
    if is_ed25519_key(key):
        return 'unsupported-alg'
    if key_refusal(key) is not None:
        return 'bad-key'
    raw = b64url_canonical(sig)
    if raw is None or len(raw) != 64 or not is_low_s(raw):
        return False
    hashes, ec, _, encode_dss_signature, InvalidSignature = _crypto()
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point(key))
    der = encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'))
    message = data.encode('utf-8') if isinstance(data, str) else data
    try:
        public.verify(der, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True
