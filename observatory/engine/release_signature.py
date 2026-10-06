"""The organization's signature on a release, checked before an update installs it.

WHY (audit A04, 2026-10-05). An update used to be accepted when the wheel matched two
digests — GitHub's asset digest and its line in SHA256SUMS — and both come from the same
release. Whoever could publish a release (a leaked token, a compromised workflow) could
then put code on every installation, and since 0.17.0 that happens without a person, once
a day. Every release carries `SHA256SUMS.asc`, a detached OpenPGP signature made in CI with
the organization's release key (passioncode-ai/.github → release-signing). That signature is
now required, checked against the key pinned below — never a key the release brings along.

WHAT IS IMPLEMENTED, AND ONLY THAT: OpenPGP v4 signatures with EdDSA over Ed25519 (public
key algorithm 22, the key's curve OID 1.3.6.1.4.1.11591.15.1), of type 0x00 (a binary
document), hashed with SHA-256, SHA-384 or SHA-512. Anything else is refused by name. The
Ed25519 check is `cryptography`'s, which the `full` extra carries; an install without that
extra (a bare `pip install`) checks with `_ed25519_verify` below, RFC 8032 §5.1.7 in plain
Python, so that install can still update itself into the `full` set. The test suite checks
the two against each other on valid and invalid signatures.

A NEW KEY (rotation, or the 3-year expiry on 2029-10-02) is added to PINNED in a release
signed by the OLD key, before releases are signed with the new one; then installs that update
always hold a key that verifies the next release.

    verify(data, armored_signature) -> the signing key's fingerprint, or SignatureError
"""
from __future__ import annotations

import base64
import hashlib
import struct

#: The organization's release keys, by fingerprint. The fingerprint is recomputed from the
#: key packet and must match: a pasted key with another fingerprint is refused at load.
PINNED = (
    {"fingerprint": "63B30DC324BD697487AA31944FAFB8AEC803B6A7",
     "name": "PassionCode.ai releases <contact@passioncode.ai>",
     "armored": """-----BEGIN PGP PUBLIC KEY BLOCK-----

mDMEasEA0BYJKwYBBAHaRw8BAQdAzg/il4KR76Liuy7pPkEmveL2sTU8kh3FZn+H
pfrbJV+0MFBhc3Npb25Db2RlLmFpIHJlbGVhc2VzIDxjb250YWN0QHBhc3Npb25j
b2RlLmFpPoi1BBMWCgBdFiEEY7MNwyS9aXSHqjGUT6+4rsgDtqcFAmrBANAbFIAA
AAAABAAObWFudTIsMi41KzEuMTIsMCwzAhsDBQkFo5qABQsJCAcCAiICBhUKCQgL
AgQWAgMBAh4HAheAAAoJEE+vuK7IA7anM7sBAItPsg0cAR8JcR97aXmgmg+X44cZ
PhciNxImjH/WhJBjAP9xU0g3+Y/af6sNeoV8PM2w1K1echJaa97VLI38Z/8lCA==
=5KqR
-----END PGP PUBLIC KEY BLOCK-----
"""},
)

ED25519_OID = bytes.fromhex("2B06010401DA470F01")
EDDSA = 22
HASHES = {8: hashlib.sha256, 9: hashlib.sha384, 10: hashlib.sha512}


class SignatureError(Exception):
    """Why a signature was refused. Never carries the data it was over."""


# --- armour and packets ------------------------------------------------------------

def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def dearmor(text: str) -> bytes:
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines or not lines[0].startswith("-----BEGIN PGP "):
        raise SignatureError("not an ASCII-armored OpenPGP block")
    try:
        start = lines.index("") + 1  # armour headers end at the first blank line
    except ValueError:
        start = 1
    body, crc = [], None
    for line in lines[start:]:
        if line.startswith("-----END PGP "):
            break
        if line.startswith("=") and len(line) == 5:
            crc = line[1:]
            continue
        body.append(line)
    else:
        raise SignatureError("the armored block has no END line")
    try:
        data = base64.b64decode("".join(body), validate=True)
    except ValueError:
        raise SignatureError("the armored block is not valid base64") from None
    if crc is not None:
        try:
            expected = int.from_bytes(base64.b64decode(crc, validate=True), "big")
        except ValueError:
            raise SignatureError("the armour checksum is not valid base64") from None
        if _crc24(data) != expected:
            raise SignatureError("the armour checksum does not match")
    return data


def packets(data: bytes):
    """(tag, body) for each packet; old and new formats, no partial lengths."""
    i = 0
    while i < len(data):
        ctb = data[i]
        if not ctb & 0x80:
            raise SignatureError("not an OpenPGP packet")
        if ctb & 0x40:  # new format
            tag, i = ctb & 0x3F, i + 1
            first = data[i]
            if first < 192:
                length, i = first, i + 1
            elif first < 224:
                length, i = ((first - 192) << 8) + data[i + 1] + 192, i + 2
            elif first == 255:
                length, i = struct.unpack(">I", data[i + 1:i + 5])[0], i + 5
            else:
                raise SignatureError("partial-length packets are not supported")
        else:  # old format
            tag, kind, i = (ctb >> 2) & 0x0F, ctb & 0x03, i + 1
            if kind == 3:
                length = len(data) - i
            else:
                size = (1, 2, 4)[kind]
                length, i = int.from_bytes(data[i:i + size], "big"), i + size
        body = data[i:i + length]
        if len(body) != length:
            raise SignatureError("a packet is truncated")
        yield tag, body
        i += length


def _mpi(body: bytes, i: int) -> tuple[bytes, int]:
    bits = struct.unpack(">H", body[i:i + 2])[0]
    size = (bits + 7) // 8
    value = body[i + 2:i + 2 + size]
    if len(value) != size:
        raise SignatureError("a number in the packet is truncated")
    return value, i + 2 + size


# --- keys ---------------------------------------------------------------------------

def parse_key(armored: str) -> dict:
    """The primary public key: its v4 fingerprint and its 32-byte Ed25519 point."""
    for tag, body in packets(dearmor(armored)):
        if tag != 6:
            continue
        if body[0] != 4:
            raise SignatureError("only v4 keys are supported")
        if body[5] != EDDSA:
            raise SignatureError(f"key algorithm {body[5]} is not EdDSA")
        oid_len = body[6]
        if body[7:7 + oid_len] != ED25519_OID:
            raise SignatureError("the key's curve is not Ed25519")
        point, _ = _mpi(body, 7 + oid_len)
        if len(point) != 33 or point[0] != 0x40:
            raise SignatureError("the key's Ed25519 point is malformed")
        fingerprint = hashlib.sha1(b"\x99" + struct.pack(">H", len(body)) + body).hexdigest().upper()
        return {"fingerprint": fingerprint, "public": point[1:]}
    raise SignatureError("no public key packet")


def pinned_keys(pinned=None) -> dict[str, bytes]:
    """fingerprint -> Ed25519 point, each recomputed from its packet and checked."""
    out = {}
    for entry in (PINNED if pinned is None else pinned):
        key = parse_key(entry["armored"])
        if key["fingerprint"] != entry["fingerprint"].replace(" ", "").upper():
            raise SignatureError("a pinned key does not have the fingerprint it is pinned under")
        out[key["fingerprint"]] = key["public"]
    return out


# --- Ed25519 without a dependency (RFC 8032 §5.1) -------------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decode_point(raw: bytes):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = 2 * t1 * t2 * _D % _P
    Dd = 2 * z1 * z2 % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s: int, point):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, point)
        point = _add(point, point)
        s >>= 1
    return q


def _equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def _ed25519_verify(public: bytes, signature: bytes, message: bytes) -> bool:
    """RFC 8032 §5.1.7, cofactorless; True only for a valid signature."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _decode_point(public), _decode_point(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


def _check(public: bytes, signature: bytes, message: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return _ed25519_verify(public, signature, message)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# --- signatures ---------------------------------------------------------------------

def _subpackets(area: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(area):
        first = area[i]
        if first < 192:
            length, i = first, i + 1
        elif first < 255:
            length, i = ((first - 192) << 8) + area[i + 1] + 192, i + 2
        else:
            length, i = struct.unpack(">I", area[i + 1:i + 5])[0], i + 5
        if length < 1 or i + length > len(area):
            raise SignatureError("a signature subpacket is malformed")
        out.append((area[i] & 0x7F, area[i + 1:i + length]))
        i += length
    return out


def parse_signature(armored: str) -> dict:
    sigs = [body for tag, body in packets(dearmor(armored)) if tag == 2]
    if len(sigs) != 1:
        raise SignatureError(f"expected one signature packet, found {len(sigs)}")
    body = sigs[0]
    if body[0] != 4:
        raise SignatureError("only v4 signatures are supported")
    sig_type, algo, hash_algo = body[1], body[2], body[3]
    if sig_type != 0x00:
        raise SignatureError(f"signature type 0x{sig_type:02x} is not a binary-document signature")
    if algo != EDDSA:
        raise SignatureError(f"signature algorithm {algo} is not EdDSA")
    if hash_algo not in HASHES:
        raise SignatureError(f"hash algorithm {hash_algo} is not accepted")
    hashed_len = struct.unpack(">H", body[4:6])[0]
    hashed_end = 6 + hashed_len
    hashed = body[6:hashed_end]
    unhashed_len = struct.unpack(">H", body[hashed_end:hashed_end + 2])[0]
    unhashed = body[hashed_end + 2:hashed_end + 2 + unhashed_len]
    i = hashed_end + 2 + unhashed_len
    left16 = body[i:i + 2]
    r, i = _mpi(body, i + 2)
    s, i = _mpi(body, i)
    issuer = None
    for kind, value in _subpackets(hashed):
        if kind == 33 and len(value) == 21 and value[0] == 4:
            issuer = value[1:].hex().upper()
    key_id = None
    for kind, value in _subpackets(hashed) + _subpackets(unhashed):
        if kind == 16 and len(value) == 8:
            key_id = value.hex().upper()
    if len(r) > 32 or len(s) > 32:
        raise SignatureError("the EdDSA signature values are too long")
    return {"hash": hash_algo, "prefix": body[:hashed_end], "left16": left16,
            "signature": r.rjust(32, b"\0") + s.rjust(32, b"\0"),
            "issuer": issuer, "key_id": key_id}


def verify(data: bytes, armored_signature: str, pinned=None) -> str:
    """The fingerprint of the pinned key whose signature over `data` this is.

    Raises SignatureError when it is not one: malformed, an unaccepted algorithm, a key
    that is not pinned, or a signature that does not verify."""
    try:
        sig = parse_signature(armored_signature)
    except SignatureError:
        raise
    except (IndexError, struct.error, ValueError, KeyError, TypeError, UnicodeError) as exc:
        # Truncated packets and short MPIs ran off the end of the buffer and surfaced as
        # IndexError or struct.error — a traceback with no refusal recorded (audit A04
        # review). Malformed input is a refusal like any other.
        raise SignatureError(f"the signature is malformed ({type(exc).__name__})") from None
    keys = pinned_keys(pinned)
    if sig["issuer"]:
        candidates = [sig["issuer"]] if sig["issuer"] in keys else []
    elif sig["key_id"]:
        candidates = [f for f in keys if f.endswith(sig["key_id"])]
    else:
        candidates = list(keys)
    if not candidates:
        raise SignatureError("the release is signed by a key this engine does not trust")
    trailer = b"\x04\xff" + struct.pack(">I", len(sig["prefix"]))
    digest = HASHES[sig["hash"]](data + sig["prefix"] + trailer).digest()
    if digest[:2] != sig["left16"]:
        raise SignatureError("the signature does not match these bytes")
    for fingerprint in candidates:
        if _check(keys[fingerprint], sig["signature"], digest):
            return fingerprint
    raise SignatureError("the signature does not verify with the organization's release key")
