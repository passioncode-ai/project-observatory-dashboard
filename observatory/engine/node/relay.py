"""The node's half of fabric-relay/0.1 (contract `src/relay.ts`, DEC-0047, DEC-0066): what a machine signs to join a
relay, to send a message and to put or fetch a blob, and how it checks what the relay delivers to it.

A relay is a router, not a party to trust: a delivered body is believed only for the signature of the sender's `#sig`
key in the verified estate record, its signed recipient is this machine's own kid (never a value the relay asserts),
and its signed `exp` bounds its life and the replay window. The checks run in the reference's order and name its
refusal codes, so a node and the contract kit give one answer for one body.
"""
from __future__ import annotations

import base64
import json
import re
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

# ASCII digits only, and fullmatch: `\d` takes other scripts' digits and `$` a trailing newline.
_INSTANT = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?Z')
_RFC3339 = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})')


def _days_in(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar, for every year 0000-9999 (as JavaScript's `Date`
    counts them; `calendar.timegm` refuses year 0)."""
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _epoch_ms(match: 're.Match', offset_minutes: int = 0) -> Optional[int]:
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not 1 <= month <= 12 or not 1 <= day <= _days_in(year, month) or hour > 23 or minute > 59 or second > 59:
        return None
    fraction = match.group(7)
    millis = int((fraction[1:] + '00')[:3]) if fraction else 0
    seconds = _days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second - offset_minutes * 60
    return seconds * 1000 + millis


def instant(value: Any) -> Optional[int]:
    """The milliseconds of an RFC 3339 UTC instant (`Z` only, upper-case `T`, at most nine fraction digits) on a day its
    month has, or None; hour 24 and a leap second are refused. The relay's times are read only through this."""
    match = _INSTANT.fullmatch(value) if isinstance(value, str) else None
    return _epoch_ms(match) if match else None


def strict_time(value: Any) -> Optional[int]:
    """The contract's `strictTime`: an RFC 3339 date-time with any offset (an estate record's window), or None."""
    match = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    offset = match.group(8)
    if offset not in ('Z', 'z') and (int(offset[1:3]) > 23 or int(offset[4:6]) > 59):
        return None                                        # +24:00 and +12:60 are no offset (JavaScript refuses them)
    minutes = 0 if offset in ('Z', 'z') else (1 if offset[0] == '+' else -1) * (int(offset[1:3]) * 60 + int(offset[4:6]))
    return _epoch_ms(match, minutes)


def iso_ms(ms: int) -> str:
    """An instant as `YYYY-MM-DDTHH:MM:SS.mmmZ`, the form the vectors print."""
    seconds, millis = divmod(ms, 1000)
    return _civil(seconds) + '.%03dZ' % millis


def rfc3339(ms: int) -> str:
    """An instant as whole-second RFC 3339 UTC, the form a node signs."""
    return _civil(ms // 1000) + 'Z'


def _civil(seconds: int) -> str:
    """`YYYY-MM-DDTHH:MM:SS` of whole seconds since the epoch, for years 0000-9999 (the inverse of
    `_days_from_civil`)."""
    days, rest = divmod(seconds, 86400)
    z = days + 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + (3 if mp < 10 else -9)
    year = yoe + era * 400 + (month <= 2)
    return '%04d-%02d-%02dT%02d:%02d:%02d' % (year, month, day, rest // 3600, rest // 60 % 60, rest % 60)

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


_BLOB_DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def relay_request_header(signer: Callable[[str], str], *, relay: str, method: str, digest: str, kid: str,
                         recipients: Sequence[str], iat: str) -> str:
    """The `Fabric-Relay-Request` value of a blob PUT or GET: base64url of RFC 8785 {alg, kid, recipients, iat, sig}.
    Everything a relay would refuse is refused here first, so a node never signs a request no relay accepts."""
    if method not in ('PUT', 'GET'):
        raise ValueError('a blob request is a PUT or a GET')
    if not is_relay_origin(relay):
        raise ValueError('not a relay origin')
    if not isinstance(digest, str) or not _BLOB_DIGEST.fullmatch(digest):
        raise ValueError('a blob digest is sha256: and 64 lowercase hex digits')
    if instant(iat) is None:
        raise ValueError('iat is not an RFC 3339 UTC instant')
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


#: How deeply a document a node parses may nest: at most 64 levels of arrays and objects, the same bound
#: `jcs.MAX_DEPTH` applies to what a node signs. The reference has no bound; a relay message's payload nests a few
#: levels, and the bound keeps a hostile relay's deeply nested frame from exhausting the parser (`malformed`).
MAX_JSON_DEPTH = 64


def _depth_ok(text: str) -> bool:
    """Whether brackets outside strings nest at most `MAX_JSON_DEPTH` deep, counted before anything is parsed."""
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in '[{':
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif char in ']}':
            depth -= 1
    return True


def json_depth_ok(text: str) -> bool:
    """Whether JSON text nests at most `MAX_JSON_DEPTH` deep, counted before it is parsed (strings skipped)."""
    return _depth_ok(text)


def parse_json(raw: bytes):
    """JSON from a relay or a peer: no NaN or Infinity, at most `MAX_FRAME_BYTES` and `MAX_JSON_DEPTH` deep, UTF-8;
    ValueError otherwise (never RecursionError or MemoryError). The session parses every relay frame with it."""
    def no_constants(name: str):
        raise ValueError(name)

    def finite(text: str) -> float:
        value = float(text)
        if value in (float('inf'), float('-inf')):          # 1e400 is no number JSON can carry
            raise ValueError('a number out of range')
        return value
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError('too large')
    text = raw.decode('utf-8')
    if not _depth_ok(text):
        raise ValueError('too deep')
    try:
        return json.loads(text, parse_constant=no_constants, parse_float=finite)
    except (RecursionError, MemoryError) as error:
        raise ValueError('unparseable') from error


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
        header, payload = parse_json(header_raw), parse_json(payload_raw)
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
    # JavaScript's `!==`: a value of another type is never equal, so `["x"]` is no `"x"` and no `["x"]` either.
    same = lambda value, wanted: isinstance(value, str) and isinstance(wanted, str) and value == wanted
    if not (same(payload['from'], expect['from']) and same(payload['to'], expect['to'])
            and same(payload['msgId'], expect['msgId'])):
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
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError:                             # a lone surrogate is no header value
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    if len(encoded) > MAX_RELAY_REQUEST_BYTES:
        return {'ok': False, 'status': 431, 'code': 'too-large'}
    raw = p256.b64url_canonical(value)
    if raw is None:
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    try:
        parsed = parse_json(raw)
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
    # Only string references count: an object or an array names nothing and cannot be hashed.
    machines = {entry.get('ref') for entry in revocations if entry.get('kind') == 'machine' and isinstance(entry.get('ref'), str)}
    keys = {entry.get('ref') for entry in revocations if entry.get('kind') == 'device-key' and isinstance(entry.get('ref'), str)}
    admitted, excluded, claimed = {}, [], set()
    for machine in entries(record.get('machines')):
        if not isinstance(machine.get('id'), str) or machine['id'] in machines:
            continue
        for key in entries(machine.get('deviceKeys')):
            kid, public = key.get('kid'), key.get('publicKey')
            if key.get('use') != 'sig' or not isinstance(kid, str) or not kid.endswith('#sig') \
                    or not isinstance(public, str) or kid in keys:
                continue
            octets = _base64(public)
            if kid in claimed:
                # A kid named twice speaks for nobody: neither of its keys is believed, and it is excluded once.
                # Whatever the earlier listings were excluded for, the kid is now excluded once, as bad-key.
                admitted.pop(kid, None)
                excluded[:] = [entry for entry in excluded if entry['kid'] != kid]
                excluded.append({'kid': kid, 'reason': 'bad-key'})
            elif p256.is_ed25519_key(octets):
                excluded.append({'kid': kid, 'reason': 'unsupported-alg'})
            elif p256.key_refusal(octets) is not None or kid != machine['id'] + '#sig':
                # A kid is bound to the machine that lists it (FAC-SEM-073 holds this for a verified record; a node
                # checks it again before it believes a key).
                excluded.append({'kid': kid, 'reason': 'bad-key'})
            else:
                admitted[kid] = octets
            claimed.add(kid)
    revoked_kids = sorted(keys | {machine['id'] + '#sig' for machine in entries(record.get('machines'))
                                  if isinstance(machine.get('id'), str) and machine['id'] in machines})
    return {'ok': True, 'revision': record.get('revision'), 'keys': admitted, 'excluded': excluded,
            'revoked': revoked_kids}


_STRICT_B64 = re.compile(r'[A-Za-z0-9+/]*={0,2}')


def _base64(text: str) -> bytes:
    """The bytes of canonical, padded standard base64 (re-encoding gives the same text), else b'', which no key
    validation accepts. Stricter than Node's lenient decoder on purpose: one key has one spelling."""
    if not isinstance(text, str) or len(text) % 4 or not _STRICT_B64.fullmatch(text):
        return b''
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        return b''
    return raw if base64.b64encode(raw).decode('ascii') == text else b''
