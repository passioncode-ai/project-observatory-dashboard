"""Offline verification of a passkey assertion (contract `src/webauthn.ts`, `src/signing-challenges.ts`; W3C Web
Authentication Level 3 §7.2; DEC-0055, DEC-0063), as the estate record's signature uses it.

Checked, in the reference's order:
- the shape, the algorithm and the credential's key: a SEC 1 P-256 point for ES256, a full-order Ed25519 key for EdDSA;
- the client data: `type`, `challenge`, `origin`, and no cross-origin use;
- the authenticator data: `rpIdHash`, the flags, with user presence and user verification required;
- the signature over `authenticatorData ‖ SHA-256(clientDataJSON)`.

Not checked: attestation, the authenticator's provenance, the signature counter (passkeys sync), extensions. The caller
supplies a credential it already pinned.

A passkey signs only registered statements (`estate-revision/1`, ...) and only under a dedicated signing RP,
`sign.<domain>`; an origin outside that RP ID is never pinned.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Mapping, Optional, Sequence

from . import ed25519, jcs, p256
from .relay import json_depth_ok, strict_time

ASSERTION_ALGORITHMS = ('ES256', 'EdDSA')
FLAGS = {'UP': 0x01, 'UV': 0x04, 'BE': 0x08, 'BS': 0x10, 'AT': 0x40, 'ED': 0x80}
_B64URL = re.compile(r'^[A-Za-z0-9_-]+$')
_LABEL = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_SIGNING_RP = re.compile(rf'^sign(?:\.{_LABEL}){{2,}}$')
_ORIGIN_HOST = re.compile(r'^https://([a-z0-9.-]{1,253})$')


def node_b64url(text: str) -> bytes:
    """Base64url decoded as Node's `Buffer.from(text, "base64url")` decodes it: a dangling last character is dropped."""
    if len(text) % 4 == 1:
        text = text[:-1]
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _canonical(text: str) -> bool:
    return p256.b64url_encode(node_b64url(text)) == text


def _fail(problem: str) -> dict:
    return {'ok': False, 'problem': problem}


def credential_key(public_key: Any, alg: str) -> dict:
    """The credential's key validated before use: `{'alg', 'raw'}` or `{'problem': unusable-key | small-order-key}`."""
    if alg not in ASSERTION_ALGORITHMS:
        return {'problem': 'unusable-key'}
    if not isinstance(public_key, dict) or 'd' in public_key or not isinstance(public_key.get('x'), str) \
            or not _B64URL.match(public_key['x']) or not _canonical(public_key['x']):
        return {'problem': 'unusable-key'}
    if alg == 'ES256':
        y = public_key.get('y')
        if public_key.get('kty') != 'EC' or public_key.get('crv') != 'P-256' or not isinstance(y, str) \
                or not _B64URL.match(y) or not _canonical(y):
            return {'problem': 'unusable-key'}
        x, yb = node_b64url(public_key['x']), node_b64url(y)
        octets = b'\x04' + x + yb
        if len(x) != 32 or len(yb) != 32 or p256.key_refusal(octets) is not None:
            return {'problem': 'unusable-key'}
        return {'alg': 'ES256', 'raw': octets}
    if public_key.get('kty') != 'OKP' or public_key.get('crv') != 'Ed25519':
        return {'problem': 'unusable-key'}
    raw = node_b64url(public_key['x'])
    if len(raw) != 32:
        return {'problem': 'unusable-key'}
    if ed25519.is_small_order_key(raw):
        return {'problem': 'small-order-key'}
    return {'alg': 'EdDSA', 'raw': raw}


def _verify_es256_der(octets: bytes, message: bytes, signature: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as error:                           # the engine's `full` extra
        raise RuntimeError('ES256 needs the engine full extra (cryptography)') from error
    try:
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), octets)
        public.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_assertion(assertion: Any, credential: Mapping[str, Any], challenge: bytes) -> dict:
    """`{'ok': True, 'flags', 'signCount'}` or `{'ok': False, 'problem'}`. `credential` holds `credentialId`, `alg`,
    `publicKey` (a JWK), `rpId` and `origins`; its `credentialId` must equal the assertion's."""
    if not isinstance(assertion, dict):
        return _fail('malformed')
    fields = [assertion.get(name) for name in ('credentialId', 'alg', 'authenticatorData', 'clientDataJSON', 'sig')]
    if not all(isinstance(field, str) for field in fields):
        return _fail('malformed')
    credential_id, alg, authenticator_data, client_data_json, sig = fields
    if not all(_B64URL.match(field) for field in (credential_id, authenticator_data, client_data_json, sig)):
        return _fail('malformed')
    if credential_id != credential.get('credentialId'):
        return _fail('malformed')
    if alg not in ASSERTION_ALGORITHMS:
        return _fail('unknown-algorithm')
    if alg != credential.get('alg'):
        return _fail('key-algorithm-mismatch')
    found = credential_key(credential.get('publicKey'), alg)
    if 'problem' in found:
        return _fail(found['problem'])

    c_data = node_b64url(client_data_json)
    try:
        text = c_data.decode('utf-8')
        if text.startswith('﻿'):
            text = text[1:]

        def no_constants(name):
            raise ValueError(name)
        if not json_depth_ok(text):                    # the node's one depth bound (relay.MAX_JSON_DEPTH)
            return _fail('malformed')
        client = json.loads(text, parse_constant=no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        return _fail('malformed')
    if not isinstance(client, dict):
        return _fail('malformed')
    if client.get('type') != 'webauthn.get':
        return _fail('wrong-type')
    if client.get('challenge') != p256.b64url_encode(challenge):
        return _fail('wrong-challenge')
    origins = credential.get('origins') or []
    if not isinstance(client.get('origin'), str) or client['origin'] not in origins:
        return _fail('wrong-origin')
    if client.get('crossOrigin') is True or 'topOrigin' in client:
        return _fail('cross-origin')

    auth_data = node_b64url(authenticator_data)
    if len(auth_data) < 37:
        return _fail('malformed')
    if auth_data[:32] != hashlib.sha256(str(credential.get('rpId', '')).encode('utf-8')).digest():
        return _fail('wrong-rp')
    flags = auth_data[32]
    if flags & FLAGS['AT'] or (flags & FLAGS['BS'] and not flags & FLAGS['BE']):
        return _fail('bad-flags')
    if not flags & FLAGS['UP']:
        return _fail('no-user-presence')
    if not flags & FLAGS['UV']:
        return _fail('no-user-verification')

    signed = auth_data + hashlib.sha256(c_data).digest()
    signature = node_b64url(sig)
    if alg == 'ES256':
        good = _verify_es256_der(found['raw'], signed, signature)
    else:
        good = len(signature) == 64 and ed25519.verify(found['raw'], signed, signature)
    if not good:
        return _fail('bad-signature')
    return {'ok': True, 'flags': flags, 'signCount': int.from_bytes(auth_data[33:37], 'big')}

# ---- The register of signed statements (DEC-0063) -----------------------------------------------------------------

_SHA256 = re.compile(r'^sha256:[a-f0-9]{64}$')
#: The passkey-signed statements a node checks; key-signed ones (relay-auth/1, relay-blob/1) are built in `relay`.
#: Whose machines an `enroll-token/1` lets join: an estate's (DEC-0084).
_ESTATE_SCOPE = re.compile(r'estate:[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}')
#: 32 random bytes as unpadded base64url: 43 characters, the last carrying no stray bits.
NONCE32 = re.compile(r'[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]')
#: The enroll token's members beside `type` (DEC-0084), in the reference's order.
ENROLL_TOKEN_FIELDS = ('scope', 'genesis', 'purpose', 'ceiling', 'notBefore', 'expiresAt', 'nonce', 'uses')


def _matches(pattern: re.Pattern, value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _enroll_token_values(f: Mapping[str, Any]) -> bool:
    """The values only; the window's length (at most 900 s) is FAC-SEM-113's and the verifier's (`enroll_token`)."""
    return (_matches(_ESTATE_SCOPE, f.get('scope')) and _matches(_SHA256, f.get('genesis'))
            and f.get('purpose') == 'agent-host' and (f.get('ceiling') is None or _matches(_SHA256, f.get('ceiling')))
            and strict_time(f.get('notBefore')) is not None and strict_time(f.get('expiresAt')) is not None
            and _matches(NONCE32, f.get('nonce'))
            # `uses` is the number 1: JSON's 1 and 1.0 are one number, a boolean is none.
            and not isinstance(f.get('uses'), bool) and isinstance(f.get('uses'), (int, float)) and f.get('uses') == 1)


#: The passkey-signed statements this node reads, each its fields and the check of their values.
PASSKEY_STATEMENTS = {'estate-revision/1': (('digest',), lambda f: _matches(_SHA256, f.get('digest'))),
                      'enroll-token/1': (ENROLL_TOKEN_FIELDS, _enroll_token_values)}
#: The key-signed statements this node reads (a device key signs them, never a passkey).
KEY_STATEMENTS = {'enroll-join/1': (('digest', 'nonce'),
                                    lambda f: _matches(_SHA256, f.get('digest')) and _matches(NONCE32, f.get('nonce')))}


def key_statement(kind: str, fields: Any) -> Optional[bytes]:
    """The UTF-8 RFC 8785 bytes of a key-signed `{type, ...fields}`, under the same rules as `signing_statement`."""
    spec = KEY_STATEMENTS.get(kind)
    if spec is None or not isinstance(fields, dict) or sorted(fields) != sorted(spec[0]) or not spec[1](fields):
        return None
    try:
        return jcs.canonical_bytes(dict(fields, type=kind))
    except jcs.CanonicalError:
        return None


def signing_statement(kind: str, fields: Any) -> Optional[bytes]:
    """The UTF-8 RFC 8785 bytes of `{type, ...fields}`, or None when the type is not registered here, the fields are not
    exactly the type's, a value is outside what the register admits, or a value has no canonical form."""
    spec = PASSKEY_STATEMENTS.get(kind)
    if spec is None or not isinstance(fields, dict) or sorted(fields) != sorted(spec[0]) or not spec[1](fields):
        return None
    try:
        return jcs.canonical_bytes(dict(fields, type=kind))
    except jcs.CanonicalError:
        return None


def signing_challenge(kind: str, fields: Any) -> Optional[bytes]:
    statement = signing_statement(kind, fields)
    return hashlib.sha256(statement).digest() if statement is not None else None


def is_signing_rp_id(rp_id: Any) -> bool:
    """`sign.` followed by at least two more lower-case labels: a dedicated signing RP (v4 §7, DEC-0063)."""
    return isinstance(rp_id, str) and len(rp_id) <= 253 and _SIGNING_RP.match(rp_id) is not None


def origin_within(origin: Any, rp_id: str) -> bool:
    """https, no port and no path, its host the RP ID or a subdomain of it, lower case."""
    match = _ORIGIN_HOST.match(origin) if isinstance(origin, str) else None
    return match is not None and (match.group(1) == rp_id or match.group(1).endswith('.' + rp_id))


def verify_signing_assertion(assertion: Any, credential: Mapping[str, Any], kind: str, fields: Any) -> dict:
    """A passkey's signature over a registered statement: `wrong-rp` for a credential that is not a signing RP, origins
    outside its RP ID dropped, then `verify_assertion` over the challenge rebuilt from `kind` and `fields`."""
    challenge = signing_challenge(kind, fields)
    if challenge is None:
        return _fail('malformed')
    if not is_signing_rp_id(credential.get('rpId')):
        return _fail('wrong-rp')
    pinned = [o for o in credential.get('origins') or [] if origin_within(o, credential['rpId'])]
    return verify_assertion(assertion, dict(credential, origins=pinned), challenge)


def signing_credential_findings(credential: Any, path: str) -> list:
    """FAC-SEM-092 on one signing credential: a signing RP ID, and every pinned origin within it."""
    value = credential if isinstance(credential, dict) else {}
    if not is_signing_rp_id(value.get('rpId')):
        return [{'code': 'FAC-SEM-092', 'instancePath': f'{path}/rpId',
                 'message': 'a passkey signs only under a dedicated signing RP, sign.<domain>'}]
    origins = value.get('origins') if isinstance(value.get('origins'), list) else []
    return [{'code': 'FAC-SEM-092', 'instancePath': f'{path}/origins/{index}',
             'message': 'a signing ceremony runs only on the RP ID or a host under it, over https without a port'}
            for index, origin in enumerate(origins) if not origin_within(origin, value['rpId'])]
