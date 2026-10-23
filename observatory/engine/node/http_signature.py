"""The device transport signature, `fabric-device` (contract DEC-0056, `docs/specification/devices.md#transport`).

A device signs every check-in, rotation, activity batch and share registration with an HTTP message signature (an RFC
9421 subset) beside mutual TLS, because a host that terminates TLS never sees a client certificate. The signer is
`sign_request`; `verify_request` is the receiver's check in the contract's order (steps 1 to 8), which this engine uses
for its own tests and its fake organization server, and which runs the contract's vectors
(`fabric/node-vectors/http-signature.json`).

Covered components `("@method" "@target-uri" "content-digest")`, parameters `created`, `nonce`, `keyid`, `alg` in that
order; `keyid` is the certificate's serial; `Content-Digest: sha-256=:<base64>:` of the body's bytes as sent; a retry is
signed afresh (a new nonce and a new `created`).
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from . import ed25519, p256

LABEL = 'fabric-device'
COMPONENTS = ('@method', '@target-uri', 'content-digest')
PARAMETERS = ('created', 'nonce', 'keyid', 'alg')
MAX_SKEW_SECONDS = 300
ALGORITHMS = {'es256': 'ecdsa-p256-sha256', 'ed25519': 'ed25519'}
NONCE = re.compile(r'[A-Za-z0-9_-]{22,64}\Z')
SERIAL = re.compile(r'[0-9a-f]{2,40}\Z')
#: The refusals of steps 1 to 8, in the contract's order: 401 and the code, but `organization-closed` (DEC-0103), which
#: is 410 with `closedAt` and is checked at step 4, after the certificate is found and before its revocation.
REFUSALS = ('signature-missing', 'digest-mismatch', 'signature-stale', 'certificate-unknown', 'organization-closed',
            'certificate-revoked', 'certificate-expired', 'signature-invalid', 'signature-replayed')
ORGANIZATION_CLOSED = 'organization-closed'


def content_digest(body: bytes) -> str:
    """RFC 9530 §2: `sha-256=:<base64 of SHA-256 of the body's bytes>:`."""
    return 'sha-256=:' + base64.b64encode(hashlib.sha256(body).digest()).decode('ascii') + ':'


def public_origin(base: Any) -> Optional[str]:
    """`https://` and the authority alone, host in lower case, no default port, no path; None for anything else."""
    if not isinstance(base, str):
        return None
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != 'https' or not parts.hostname or parts.path not in ('', '/') or parts.query or parts.fragment \
            or parts.username is not None or parts.password is not None or base.endswith('?') or base.endswith('#'):
        return None
    host = parts.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    return 'https://' + host + ('' if port in (None, 443) else ':%d' % port)


def target_uri(origin: str, target: str) -> str:
    """The receiver's public base URL followed by the request target as sent: its path and query."""
    return origin + target


def signature_params(created: int, nonce: str, keyid: str, alg: str) -> str:
    return '("@method" "@target-uri" "content-digest");created=%d;nonce="%s";keyid="%s";alg="%s"' % (
        created, nonce, keyid, alg)


def signature_base(method: str, uri: str, digest: str, params: str) -> str:
    """RFC 9421 §2.5: one line per covered component, `@signature-params` last, no newline after it."""
    return '"@method": %s\n"@target-uri": %s\n"content-digest": %s\n"@signature-params": %s' % (
        method, uri, digest, params)


def new_nonce() -> str:
    """16 random bytes, base64url without padding: 22 characters."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b'=').decode('ascii')


def sign_request(sign: Callable[[bytes], str], *, method: str, origin: str, target: str, body: bytes, serial: str,
                 alg: str = 'ecdsa-p256-sha256', created: Optional[int] = None,
                 nonce: Optional[str] = None) -> Dict[str, str]:
    """The three header fields of a signed device request. `sign` is the `#sig` key's signer: it takes the signature
    base's bytes and answers the raw signature in base64url (`DeviceKeys.sign`, ES256 r ‖ s, low-S)."""
    if not SERIAL.fullmatch(serial or ''):
        raise ValueError('a certificate serial is 2 to 40 lower-case hex digits')
    created = int(time.time()) if created is None else int(created)
    nonce = nonce or new_nonce()
    digest = content_digest(body)
    params = signature_params(created, nonce, serial, alg)
    base = signature_base(method, target_uri(origin, target), digest, params)
    raw = p256.b64url_decode(sign(base.encode('utf-8')))
    if raw is None or len(raw) != 64:
        raise ValueError('the signer answered no 64-octet signature')
    return {'Content-Digest': digest, 'Signature-Input': LABEL + '=' + params,
            'Signature': LABEL + '=:' + base64.b64encode(raw).decode('ascii') + ':'}


# ---- RFC 8941 dictionaries (with RFC 9651's Date and Display String items) ----------------------------------------

class _Bad(ValueError):
    pass


_KEY = re.compile(r'[a-z*][a-z0-9_\-.*]*')
_TOKEN = re.compile(r"[A-Za-z*][A-Za-z0-9!#$%&'*+\-.^_`|~:/]*")
_NUMBER = re.compile(r'-?[0-9]{1,15}(\.[0-9]{1,3})?')


class _Reader:
    def __init__(self, text: str):
        self.text, self.i = text, 0

    def peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ''

    def skip(self, chars: str = ' ') -> None:
        while self.peek() and self.peek() in chars:
            self.i += 1

    def match(self, pattern: 're.Pattern') -> str:
        m = pattern.match(self.text, self.i)
        if not m:
            raise _Bad('unexpected text')
        self.i = m.end()
        return m.group(0)


def _bare_item(r: _Reader) -> Tuple[str, Any]:
    c = r.peek()
    if c == '"':
        r.i += 1
        out = []
        while True:
            ch = r.peek()
            if not ch:
                raise _Bad('open string')
            r.i += 1
            if ch == '\\':
                nxt = r.peek()
                if nxt not in ('"', '\\'):
                    raise _Bad('bad escape')
                out.append(nxt)
                r.i += 1
            elif ch == '"':
                return 'string', ''.join(out)
            elif not (0x20 <= ord(ch) <= 0x7e):
                raise _Bad('bad character')
            else:
                out.append(ch)
    if c == ':':
        end = r.text.find(':', r.i + 1)
        if end < 0:
            raise _Bad('open byte sequence')
        text = r.text[r.i + 1:end]
        if not re.fullmatch(r'[A-Za-z0-9+/=]*', text):
            raise _Bad('bad byte sequence')
        r.i = end + 1
        try:
            return 'bytes', base64.b64decode(text, validate=True) if text else b''
        except ValueError as error:
            raise _Bad('bad base64') from error
    if c == '?':
        r.i += 1
        v = r.peek()
        if v not in ('0', '1'):
            raise _Bad('bad boolean')
        r.i += 1
        return 'boolean', v == '1'
    if c == '@':                                            # RFC 9651 Date
        r.i += 1
        text = r.match(_NUMBER)
        if '.' in text:
            raise _Bad('a date is an integer')
        return 'date', int(text)
    if c == '%':                                            # RFC 9651 Display String
        r.i += 1
        if r.peek() != '"':
            raise _Bad('bad display string')
        r.i += 1
        out = bytearray()
        while True:
            ch = r.peek()
            if not ch:
                raise _Bad('open display string')
            r.i += 1
            if ch == '%':
                hexes = r.text[r.i:r.i + 2]
                if not re.fullmatch(r'[0-9a-f]{2}', hexes):
                    raise _Bad('bad percent escape')
                out.append(int(hexes, 16))
                r.i += 2
            elif ch == '"':
                return 'display', out.decode('utf-8')
            elif not (0x20 <= ord(ch) <= 0x7e):
                raise _Bad('bad character')
            else:
                out.append(ord(ch))
    if c == '-' or c.isdigit():
        text = r.match(_NUMBER)
        return ('decimal', float(text)) if '.' in text else ('integer', int(text))
    if c and (c.isalpha() or c == '*'):
        return 'token', r.match(_TOKEN)
    raise _Bad('no item')


def _parameters(r: _Reader) -> list:
    params = []
    while r.peek() == ';':
        r.i += 1
        r.skip()
        key = r.match(_KEY)
        value: Tuple[str, Any] = ('boolean', True)
        if r.peek() == '=':
            r.i += 1
            value = _bare_item(r)
        params.append((key, value))
    return params


def _member_value(r: _Reader) -> Tuple[Any, list]:
    if r.peek() == '(':
        r.i += 1
        items = []
        while True:
            r.skip()
            if r.peek() == ')':
                r.i += 1
                return ('inner', items), _parameters(r)
            items.append((_bare_item(r), _parameters(r)))
            if r.peek() not in (' ', ')'):
                raise _Bad('bad inner list')
    item = _bare_item(r)
    return item, _parameters(r)


def parse_dictionary(text: str) -> list:
    """RFC 8941 §4.2.2: the members in order as `(key, value, parameters)`, duplicates kept (the caller decides)."""
    r = _Reader(text)
    members = []
    r.skip()
    if not r.peek():
        return members
    while True:
        key = r.match(_KEY)
        if r.peek() == '=':
            r.i += 1
            value, params = _member_value(r)
        else:
            value, params = ('boolean', True), _parameters(r)
        members.append((key, value, params))
        r.skip(' \t')
        if not r.peek():
            return members
        if r.peek() != ',':
            raise _Bad('expected a comma')
        r.i += 1
        r.skip(' \t')
        if not r.peek():
            raise _Bad('trailing comma')


def _field(headers: Mapping[str, Any], name: str) -> Optional[str]:
    values = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == name]
    if not values:
        return None
    flat = []
    for v in values:
        flat.extend(v if isinstance(v, (list, tuple)) else [v])
    if not all(isinstance(v, str) for v in flat):
        return None
    return ', '.join(flat)


def _members(headers: Mapping[str, Any], name: str):
    """The field's `fabric-device` members, or None when the field does not parse."""
    text = _field(headers, name)
    if text is None:
        return []
    try:
        return [m for m in parse_dictionary(text) if m[0] == LABEL]
    except _Bad:
        return None


def _parsed(headers: Mapping[str, Any]) -> Optional[dict]:
    """The `fabric-device` member of both fields in the binding's exact shape, or None (`signature-missing`)."""
    inputs, sigs = _members(headers, 'signature-input'), _members(headers, 'signature')
    if inputs is None or sigs is None or len(inputs) != 1 or len(sigs) != 1:
        return None
    _, value, params = inputs[0]
    if value[0] != 'inner' or len(value[1]) != len(COMPONENTS):
        return None
    for (item, item_params), want in zip(value[1], COMPONENTS):
        if item != ('string', want) or item_params:
            return None
    if [k for k, _ in params] != list(PARAMETERS):
        return None
    got = dict(params)
    created, nonce, keyid, alg = (got[k] for k in PARAMETERS)
    if created[0] != 'integer' or nonce[0] != 'string' or keyid[0] != 'string' or alg[0] != 'string':
        return None
    if not NONCE.fullmatch(nonce[1]) or not SERIAL.fullmatch(keyid[1]) or alg[1] not in ALGORITHMS.values():
        return None
    _, sig_value, sig_params = sigs[0]
    if sig_value[0] != 'bytes' or sig_params:
        return None
    return {'created': created[1], 'nonce': nonce[1], 'keyid': keyid[1], 'alg': alg[1], 'signature': sig_value[1],
            'params': signature_params(created[1], nonce[1], keyid[1], alg[1])}


def _present(headers: Mapping[str, Any]) -> bool:
    for name in ('signature-input', 'signature'):
        text = _field(headers, name)
        if text is not None and re.search(r'(^|[\s,])fabric-device\s*=', text):
            return True
    return False


def _ms(value: Any) -> Optional[int]:
    from .relay import strict_time
    return strict_time(value) if isinstance(value, str) and ('T' in value) else None


def _verifies(alg: str, raw_key: bytes, data: bytes, signature: bytes) -> bool:
    if alg == 'ed25519':
        return ed25519.verify(raw_key, data, signature)    # refuses a small-order key first (DEC-0045)
    if p256.key_refusal(raw_key) is not None:            # SEC 1 §3.2.2 before anything is verified under it
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw_key)
        der = encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big'))
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_request(request: Mapping[str, Any], *, public_base_url: str, certificate: Callable[[str], Optional[dict]],
                   at_ms: int, seen: Callable[[str, str], bool] = lambda serial, nonce: False,
                   client_certificate_serial: Optional[str] = None) -> dict:
    """The receiver's check (devices.md, steps 1 to 8). `request` is `{method, target, headers, body}` with `body` the
    bytes (or UTF-8 text) received; `certificate(serial)` answers `{serial, notBefore, notAfter, revoked?,
    organizationClosedAt?, key: {alg, publicKey}}` or None; `seen(serial, nonce)` says whether the pair is held. Answers
    `{'ok': True, 'serial', 'nonce', 'keepUntil'}`, `{'ok': False, 'code': 'organization-closed', 'status': 410,
    'closedAt'}` for a certificate of an organization that closed (DEC-0103, read before revocation; an unreadable
    `organizationClosedAt` is the receiver's own `context-invalid`), or `{'ok': False, 'code'}`; it records nothing (the
    caller records the pair after an accept)."""
    def refuse(code: str) -> dict:
        return {'ok': False, 'code': code}

    origin = public_origin(public_base_url)
    if origin is None:
        return refuse('base-url-invalid')
    headers = request.get('headers') or {}
    body = request.get('body', b'')
    body = body.encode('utf-8') if isinstance(body, str) else bytes(body)

    def lookup(serial: str):
        record = certificate(serial)
        if not isinstance(record, dict) or record.get('serial') != serial:
            return refuse('certificate-unknown')
        # Step 4 (DEC-0103): a closed organization answers before revocation, so the device learns the end, not one
        # machine's revocation. `null` is not absence: only an absent member means an open organization.
        if 'organizationClosedAt' in record:
            closed_at = record['organizationClosedAt']
            if not isinstance(closed_at, str) or _ms(closed_at) is None:
                return refuse('context-invalid')
            return {'ok': False, 'code': ORGANIZATION_CLOSED, 'status': 410, 'closedAt': closed_at}
        if record.get('revoked') not in (None, False):
            return refuse('certificate-revoked')
        start, end = _ms(record.get('notBefore')), _ms(record.get('notAfter'))
        if start is None or end is None or not start <= at_ms < end:
            return refuse('certificate-expired')
        return record

    if not _present(headers):
        if client_certificate_serial is None:
            return refuse('signature-missing')
        found = lookup(client_certificate_serial)
        return found if found.get('ok') is False else {'ok': True, 'serial': client_certificate_serial}
    parsed = _parsed(headers)
    if parsed is None:
        return refuse('signature-missing')
    digests = _field(headers, 'content-digest')
    if digests is None or digests.strip() != content_digest(body):
        return refuse('digest-mismatch')
    if abs(at_ms / 1000 - parsed['created']) > MAX_SKEW_SECONDS:
        return refuse('signature-stale')
    if client_certificate_serial is not None and client_certificate_serial != parsed['keyid']:
        return refuse('certificate-unknown')
    found = lookup(parsed['keyid'])
    if found.get('ok') is False:
        return found
    key = found.get('key') or {}
    if ALGORITHMS.get(key.get('alg')) != parsed['alg']:
        return refuse('signature-invalid')
    try:
        raw_key = base64.b64decode(key.get('publicKey', ''), validate=True)
    except (ValueError, TypeError):
        return refuse('signature-invalid')
    method, target = request.get('method'), request.get('target')
    if not isinstance(method, str) or not isinstance(target, str) or not target.startswith('/'):
        return refuse('signature-invalid')
    base = signature_base(method, target_uri(origin, target), content_digest(body), parsed['params'])
    if len(parsed['signature']) != 64 or not _verifies(parsed['alg'], raw_key, base.encode('utf-8'),
                                                       parsed['signature']):
        return refuse('signature-invalid')
    if seen(parsed['keyid'], parsed['nonce']):
        return refuse('signature-replayed')
    return {'ok': True, 'serial': parsed['keyid'], 'nonce': parsed['nonce'],
            'keepUntil': parsed['created'] + MAX_SKEW_SECONDS}


def headers_lower(headers: Iterable[Tuple[str, str]]) -> Dict[str, list]:
    """Request header fields keyed in lower case, each name's values in order (for a receiver's input)."""
    out: Dict[str, list] = {}
    for name, value in headers:
        out.setdefault(name.lower(), []).append(value)
    return out
