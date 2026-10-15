"""The node's half of fabric-relay/0.1 (contract `src/relay.ts`, DEC-0047, DEC-0066): what a machine signs to join a
relay, to send a message and to put or fetch a blob, and how it checks what the relay delivers to it.

A relay is a router, not a party to trust: a delivered body is believed only for the signature of the sender's `#sig`
key in the verified estate record, its signed recipient is this machine's own kid (never a value the relay asserts),
and its signed `exp` bounds its life and the replay window. The checks run in the reference's order and name its
refusal codes, so a node and the contract kit give one answer for one body.
"""
from __future__ import annotations

import base64
import calendar
import json
import re
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from . import jcs, p256

RELAY_PROTOCOL = 'fabric-relay/0.1'
RELAY_SUBPROTOCOL = 'fabric-relay.v1'
RELAY_PATH = '/v1/connect'
RELAY_ALG = 'ES256'
MESSAGE_TYP = 'fabric-relay-message+jwt'
MESSAGE_TYPE = 'fabric-relay-message/1'
MAX_FRAME_BYTES = 1_048_576
MAX_BODY_BYTES = 783_360
MAX_MESSAGE_TTL_SECONDS = 604_800
AUTH_WINDOW_SECONDS = 60
CLOCK_SKEW_SECONDS = 60
REQUEST_HEADER = 'Fabric-Relay-Request'
MAX_RELAY_REQUEST_BYTES = 8192
MAX_BLOB_RECIPIENTS = 64
MAX_KID_CHARS = 256

MESSAGE_REFUSALS = ('malformed', 'unsupported-alg', 'unknown-sender', 'bad-key', 'bad-signature', 'mismatch',
                    'not-yet-valid', 'lifetime-too-long', 'expired', 'replay')

# ---- Times ---------------------------------------------------------------------------------------------------------

_INSTANT = re.compile(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?Z$')
_RFC3339 = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$')


def _days_in(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _epoch_ms(match: 're.Match', offset_minutes: int = 0) -> Optional[int]:
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not 1 <= month <= 12 or not 1 <= day <= _days_in(year, month) or hour > 23 or minute > 59 or second > 59:
        return None
    fraction = match.group(7)
    millis = int((fraction[1:] + '00')[:3]) if fraction else 0
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset_minutes * 60
    return seconds * 1000 + millis


def instant(value: Any) -> Optional[int]:
    """The milliseconds of an RFC 3339 UTC instant (`Z` only, upper-case `T`, at most nine fraction digits) on a day its
    month has, or None; hour 24 and a leap second are refused. The relay's times are read only through this."""
    match = _INSTANT.match(value) if isinstance(value, str) else None
    return _epoch_ms(match) if match else None


def strict_time(value: Any) -> Optional[int]:
    """The contract's `strictTime`: an RFC 3339 date-time with any offset (an estate record's window), or None."""
    match = _RFC3339.match(value) if isinstance(value, str) else None
    if not match:
        return None
    offset = match.group(8)
    minutes = 0 if offset in ('Z', 'z') else (1 if offset[0] == '+' else -1) * (int(offset[1:3]) * 60 + int(offset[4:6]))
    return _epoch_ms(match, minutes)


def iso_ms(ms: int) -> str:
    """An instant as `YYYY-MM-DDTHH:MM:SS.mmmZ`, the form the vectors print."""
    seconds, millis = divmod(ms, 1000)
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds)) + '.%03dZ' % millis


def rfc3339(ms: int) -> str:
    """An instant as whole-second RFC 3339 UTC, the form a node signs."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ms // 1000))

# ---- Origins -------------------------------------------------------------------------------------------------------

_OCTET = r'(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)'
_IPV4 = _OCTET + r'(?:\.' + _OCTET + r'){3}'
_H16 = r'(?:0|[1-9a-f][0-9a-f]{0,3})'
_IPV6 = '|'.join([
    f'(?:{_H16}:){{7}}{_H16}', f'(?:{_H16}:){{1,7}}:', f'(?:{_H16}:){{1,6}}:{_H16}', f'(?:{_H16}:){{1,5}}(?::{_H16}){{1,2}}',
    f'(?:{_H16}:){{1,4}}(?::{_H16}){{1,3}}', f'(?:{_H16}:){{1,3}}(?::{_H16}){{1,4}}', f'(?:{_H16}:){{1,2}}(?::{_H16}){{1,5}}',
    f'{_H16}:(?::{_H16}){{1,6}}', f':(?:(?::{_H16}){{1,7}}|:)',
])
_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_NAME = rf'(?=[a-z0-9.-]{{1,253}}(?::|$))(?:{_LABEL}\.)*(?![0-9]+(?::|$))(?!0x[0-9a-f]*(?::|$)){_LABEL}'
_PORT = r'(?:6553[0-5]|655[0-2]\d|65[0-4]\d{2}|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3})'
RELAY_ORIGIN_PATTERN = (
    rf'^(?:https://(?:{_IPV4}|\[(?:{_IPV6})\]|{_NAME})(?::(?!443$){_PORT})?'
    rf'|http://(?:127\.{_OCTET}\.{_OCTET}\.{_OCTET}|\[::1\])(?::(?!80$){_PORT})?)$'
)
_ORIGIN = re.compile(RELAY_ORIGIN_PATTERN)


def is_relay_origin(value: Any) -> bool:
    """What `hello.relay` may be: https on a host name or an address literal, or http on a loopback literal for a relay
    on this machine; no path, no default port. A node signs only for an origin this accepts."""
    return isinstance(value, str) and _ORIGIN.fullmatch(value) is not None

# ---- What a node signs ---------------------------------------------------------------------------------------------


def auth_signing_input(*, relay: str, nonce: str, estate: str, kid: str, iat: str) -> str:
    """`relay-auth/1`: the bytes a machine signs to prove its device key to a relay."""
    return jcs.rfc8785({'type': 'relay-auth/1', 'alg': RELAY_ALG, 'relay': relay, 'nonce': nonce, 'estate': estate,
                        'kid': kid, 'iat': iat})


def blob_signing_input(*, relay: str, method: str, digest: str, recipients: Sequence[str], iat: str) -> str:
    """`relay-blob/1`: the bytes a sender signs for a blob PUT or GET."""
    return jcs.rfc8785({'type': 'relay-blob/1', 'alg': RELAY_ALG, 'relay': relay, 'method': method, 'digest': digest,
                        'recipients': list(recipients), 'iat': iat})


def auth_frame(signer: Callable[[str], str], *, frame_id: str, relay: str, nonce: str, estate: str, kid: str,
               iat: str) -> dict:
    """The `auth` frame answering a relay's `hello`. `signer` is the device key's ES256 (low-S, base64url)."""
    if not is_relay_origin(relay):
        raise ValueError('not a relay origin')
    if instant(iat) is None:
        raise ValueError('iat is not an RFC 3339 UTC instant')
    sig = signer(auth_signing_input(relay=relay, nonce=nonce, estate=estate, kid=kid, iat=iat))
    return {'v': 1, 'type': 'auth', 'id': frame_id, 'protocol': RELAY_PROTOCOL, 'alg': RELAY_ALG, 'estate': estate,
            'kid': kid, 'iat': iat, 'sig': sig}


def message_body(signer: Callable[[str], str], *, sender: str, to: str, msg_id: str, iat: str, exp: str,
                 payload: Any) -> str:
    """A message body: a compact JWS whose header and payload are their own RFC 8785 bytes, `kid` the signed `from`."""
    start, end = instant(iat), instant(exp)
    if start is None or end is None:
        raise ValueError('iat and exp are RFC 3339 UTC instants')
    if not 0 < end - start <= MAX_MESSAGE_TTL_SECONDS * 1000:
        raise ValueError('a message lives more than 0 s and at most 7 days')
    header = p256.b64url_encode(jcs.canonical_bytes({'alg': RELAY_ALG, 'kid': sender, 'typ': MESSAGE_TYP}))
    body = p256.b64url_encode(jcs.canonical_bytes({'type': MESSAGE_TYPE, 'from': sender, 'to': to, 'msgId': msg_id,
                                                   'iat': iat, 'exp': exp, 'payload': payload}))
    compact = header + '.' + body + '.' + signer(header + '.' + body)
    if len(compact.encode('utf-8')) > MAX_BODY_BYTES:
        raise ValueError('a message body is at most %d bytes' % MAX_BODY_BYTES)
    return compact


def relay_request_header(signer: Callable[[str], str], *, relay: str, method: str, digest: str, kid: str,
                         recipients: Sequence[str], iat: str) -> str:
    """The `Fabric-Relay-Request` value of a blob PUT or GET: base64url of RFC 8785 {alg, kid, recipients, iat, sig}."""
    if method not in ('PUT', 'GET'):
        raise ValueError('a blob request is a PUT or a GET')
    if not _sig_kid(kid) or not 1 <= len(recipients) <= MAX_BLOB_RECIPIENTS or not all(map(_sig_kid, recipients)) \
            or len(set(recipients)) != len(recipients):
        raise ValueError('recipients are 1 to 64 distinct #sig kids')
    sig = signer(blob_signing_input(relay=relay, method=method, digest=digest, recipients=recipients, iat=iat))
    value = p256.b64url_encode(jcs.canonical_bytes({'alg': RELAY_ALG, 'kid': kid, 'recipients': list(recipients),
                                                    'iat': iat, 'sig': sig}))
    if len(value) > MAX_RELAY_REQUEST_BYTES:
        raise ValueError('the request header is at most %d bytes' % MAX_RELAY_REQUEST_BYTES)
    return value


def _sig_kid(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_KID_CHARS and value.endswith('#sig')

# ---- What a node checks --------------------------------------------------------------------------------------------

_PAYLOAD_MEMBERS = ['exp', 'from', 'iat', 'msgId', 'payload', 'to', 'type']
_HEADER_MEMBERS = {'alg', 'typ', 'kid'}


def _refuse(reason: str) -> dict:
    return {'ok': False, 'reason': reason}


def _json(raw: bytes):
    def no_constants(name: str):
        raise ValueError(name)
    return json.loads(raw.decode('utf-8'), parse_constant=no_constants)


def verify_message_body(body: Any, expect: Mapping[str, str], sender_key: Callable[[str], Any], now_ms: int) -> dict:
    """A recipient's check of a delivered body, in the reference's order: form (`malformed`), algorithm, the sender's
    key, the signature, then the signed `from`, `to` and `msgId` against the deliver frame's and this machine's own kid
    (`mismatch`), and the signed times. `{'ok': True, 'payload': ...}` or `{'ok': False, 'reason': <code>}`;
    `check_replay` follows an accepted body."""
    parts = body.split('.') if isinstance(body, str) else []
    if len(parts) != 3:
        return _refuse('malformed')
    header_raw, payload_raw = p256.b64url_decode(parts[0]), p256.b64url_decode(parts[1])
    if header_raw is None or payload_raw is None:
        return _refuse('malformed')
    try:
        header, payload = _json(header_raw), _json(payload_raw)
    except (ValueError, UnicodeDecodeError):
        return _refuse('malformed')
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return _refuse('malformed')
    # Both are their own RFC 8785 bytes: no duplicate member, no second spelling of the signed document.
    if not jcs.is_canonical(header, header_raw) or not jcs.is_canonical(payload, payload_raw):
        return _refuse('malformed')
    if header.get('typ') != MESSAGE_TYP or payload.get('type') != MESSAGE_TYPE:
        return _refuse('malformed')
    # A closed header: no `crit`, `jwk`, `b64` or other member can name a key or change what is signed.
    if not set(header) <= _HEADER_MEMBERS or sorted(payload) != _PAYLOAD_MEMBERS:
        return _refuse('malformed')
    iat, exp = instant(payload['iat']), instant(payload['exp'])
    if not isinstance(payload['from'], str) or iat is None or exp is None:
        return _refuse('malformed')
    if 'kid' in header and header['kid'] != payload['from']:
        return _refuse('malformed')
    if header.get('alg') != RELAY_ALG:
        return _refuse('unsupported-alg')
    key = sender_key(payload['from'])
    if not key:
        return _refuse('unknown-sender')
    verdict = p256.verify_es256(key, parts[0] + '.' + parts[1], parts[2])
    if verdict in ('bad-key', 'unsupported-alg'):
        return _refuse(verdict)
    if verdict is not True:
        return _refuse('bad-signature')
    if payload['from'] != expect['from'] or payload['to'] != expect['to'] or payload['msgId'] != expect['msgId']:
        return _refuse('mismatch')
    if iat - now_ms > CLOCK_SKEW_SECONDS * 1000:
        return _refuse('not-yet-valid')
    if exp <= iat or exp - iat > MAX_MESSAGE_TTL_SECONDS * 1000:
        return _refuse('lifetime-too-long')
    if now_ms >= exp:
        return _refuse('expired')
    return {'ok': True, 'payload': payload}


def check_replay(seen: Callable[[str, str], bool], payload: Mapping[str, Any], now_ms: int) -> dict:
    """After an accepted body: `expired` past its signed `exp` (an `exp` that is not an instant fails closed), `replay`
    for a `(from, msgId)` the recipient already accepted. The recipient keeps each pair until its `exp`."""
    exp = instant(payload.get('exp'))
    if exp is None or now_ms >= exp:
        return _refuse('expired')
    return _refuse('replay') if seen(payload['from'], payload['msgId']) else {'ok': True}


def parse_relay_request(value: Any) -> dict:
    """A `Fabric-Relay-Request` value read strictly (the relay's own first step); a node uses it to check what it is
    about to send. `{'ok': True, 'request': {...}}` or `{'ok': False, 'status': ..., 'code': ...}`."""
    if not isinstance(value, str) or not value:
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    if len(value.encode('utf-8')) > MAX_RELAY_REQUEST_BYTES:
        return {'ok': False, 'status': 431, 'code': 'too-large'}
    raw = p256.b64url_canonical(value)
    if raw is None:
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    try:
        parsed = _json(raw)
    except (ValueError, UnicodeDecodeError):
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    if not isinstance(parsed, dict) or sorted(parsed) != ['alg', 'iat', 'kid', 'recipients', 'sig'] \
            or not jcs.is_canonical(parsed, raw):
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    if not all(isinstance(parsed[name], str) for name in ('alg', 'kid', 'iat', 'sig')) \
            or not isinstance(parsed['recipients'], list):
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    return {'ok': True, 'request': parsed}

# ---- Whom a node believes ------------------------------------------------------------------------------------------


def peer_keys(record: Optional[Mapping[str, Any]], at_ms: int,
              extra_revocations: Sequence[Mapping[str, Any]] = ()) -> dict:
    """The `#sig` keys of a verified estate record's machines, minus revoked machines and keys (the record's own and
    those of verified revocation deltas), minus every key that could never verify a relay signature, which `excluded`
    names. The contract's `relayAdmissionSet` over a held record: a node answers `sender_key` from it, and refuses every
    sender once the record's window has ended (`expired`) until a newer verified revision arrives."""
    if not record:
        return {'ok': False, 'refusal': 'no-record'}
    start, until = strict_time(record.get('issuedAt')), strict_time(record.get('validUntil'))
    if start is None or until is None or at_ms > until:
        return {'ok': False, 'refusal': 'expired'}
    if at_ms < start - CLOCK_SKEW_SECONDS * 1000:
        return {'ok': False, 'refusal': 'not-yet-valid'}

    def entries(value: Any) -> list:
        return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []

    revocations = entries(record.get('revocations')) + entries(list(extra_revocations))
    machines = {entry.get('ref') for entry in revocations if entry.get('kind') == 'machine'}
    keys = {entry.get('ref') for entry in revocations if entry.get('kind') == 'device-key'}
    admitted, excluded = {}, []
    for machine in entries(record.get('machines')):
        if machine.get('id') in machines:
            continue
        for key in entries(machine.get('deviceKeys')):
            kid, public = key.get('kid'), key.get('publicKey')
            if key.get('use') != 'sig' or not isinstance(kid, str) or not kid.endswith('#sig') \
                    or not isinstance(public, str) or kid in keys:
                continue
            octets = _base64(public)
            if p256.is_ed25519_key(octets):
                excluded.append({'kid': kid, 'reason': 'unsupported-alg'})
            elif p256.key_refusal(octets) is not None:
                excluded.append({'kid': kid, 'reason': 'bad-key'})
            else:
                admitted[kid] = octets
    return {'ok': True, 'revision': record.get('revision'), 'keys': admitted, 'excluded': excluded}


def _base64(text: str) -> bytes:
    """Base64 SEC 1 octets as the reference reads them (Node's lenient decoder): stray characters are skipped."""
    cleaned = re.sub(r'[^A-Za-z0-9+/]', '', text.replace('-', '+').replace('_', '/'))
    return base64.b64decode(cleaned + '=' * (-len(cleaned) % 4)) if len(cleaned) % 4 != 1 else b''
