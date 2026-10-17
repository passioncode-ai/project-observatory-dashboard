"""The secret envelope (`fabric-secret-envelope/1`, contract `src/secret-envelope.ts`, DEC-0047 item 7, DEC-0066): a
secret sealed to one machine's key-agreement key (`#kem`) by the machine that holds its slot, carried by a relay as a
blob and opened only in the target's vault.

Seal: HPKE (RFC 9180) base mode, DHKEM(P-256, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM, suite 16/1/1, info
`fabric-secret-envelope/1`. The header is the AEAD's additional data; the sender signs `{header, ct}` with its `#sig`
key (ES256, low-S, RFC 8785 bytes). Every key passes SEC 1 validation before it is used.

Open, in the reference's order, naming the first check that fails:
1. `malformed`;
2. `unsupported-alg`;
3. `wrong-recipient`;
4. `bad-key`;
5. `bad-signature`;
6. `sender-not-allowed` (the estate record does not let the sender hold the slot);
7. `placement-stale`;
8. `replayed` or `sequence-reused` (the target's mark for the slot's current placement);
9. the times: `not-yet-valid`, `lifetime-too-long`, `expired`;
10. `cannot-open`.

On success it returns the secret and the mark the target writes with it in one transaction; nothing is stored after a
refusal. The secret never appears in a log or an error.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from . import jcs, p256, relay

ENVELOPE_TYPE = 'fabric-secret-envelope/1'
ENVELOPE_SUITE = (16, 1, 1)
MAX_ENVELOPE_LIFETIME_SECONDS = 86_400
ENVELOPE_REFUSALS = ('malformed', 'unsupported-alg', 'wrong-recipient', 'bad-key', 'bad-signature', 'sender-not-allowed',
                     'placement-stale', 'replayed', 'sequence-reused', 'not-yet-valid', 'lifetime-too-long', 'expired',
                     'cannot-open')
PERSONAL_CONTEXT = 'personal'
_INFO = ENVELOPE_TYPE.encode('ascii')

# ---- HPKE base mode for the one suite the contract admits ----------------------------------------------------------

_KEM_SUITE = b'KEM' + (16).to_bytes(2, 'big')
_HPKE_SUITE = b'HPKE' + (16).to_bytes(2, 'big') + (1).to_bytes(2, 'big') + (1).to_bytes(2, 'big')
_V1 = b'HPKE-v1'


def _extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, previous, counter = b'', b'', 1
    while len(out) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        out += previous
        counter += 1
    return out[:length]


def _labeled_extract(suite: bytes, salt: bytes, label: str, ikm: bytes) -> bytes:
    return _extract(salt, _V1 + suite + label.encode() + ikm)


def _labeled_expand(suite: bytes, prk: bytes, label: str, info: bytes, length: int) -> bytes:
    return _expand(prk, length.to_bytes(2, 'big') + _V1 + suite + label.encode() + info, length)


def _shared_secret(dh: bytes, enc: bytes, pk_r: bytes) -> bytes:
    prk = _labeled_extract(_KEM_SUITE, b'', 'eae_prk', dh)
    return _labeled_expand(_KEM_SUITE, prk, 'shared_secret', enc + pk_r, 32)


def _key_schedule(shared: bytes, info: bytes):
    psk_id_hash = _labeled_extract(_HPKE_SUITE, b'', 'psk_id_hash', b'')
    info_hash = _labeled_extract(_HPKE_SUITE, b'', 'info_hash', info)
    context = b'\x00' + psk_id_hash + info_hash
    secret = _labeled_extract(_HPKE_SUITE, shared, 'secret', b'')
    return (_labeled_expand(_HPKE_SUITE, secret, 'key', context, 16),
            _labeled_expand(_HPKE_SUITE, secret, 'base_nonce', context, 12))


def _ec():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:                           # the engine's `full` extra
        raise RuntimeError('the secret envelope needs the engine full extra (cryptography)') from error
    return ec, serialization, AESGCM


def _private(key: Any):
    """A P-256 private key from 32 raw bytes or a `cryptography` key object."""
    ec, _, _ = _ec()
    if isinstance(key, (bytes, bytearray)):
        return ec.derive_private_key(int.from_bytes(key, 'big'), ec.SECP256R1())
    return key


def _point_of(private) -> bytes:
    _, serialization, _ = _ec()
    return private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _dh(private, public_octets: bytes) -> bytes:
    ec, _, _ = _ec()
    return private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_octets))


def hpke_seal_base(pk_r: bytes, info: bytes, aad: bytes, pt: bytes, sk_e: Any = None):
    """HPKE SealBase, sequence 0. `sk_e` fixes the ephemeral key (vectors only). Returns `(enc, ct)`."""
    ec, _, AESGCM = _ec()
    ephemeral = _private(sk_e) if sk_e is not None else ec.generate_private_key(ec.SECP256R1())
    enc = _point_of(ephemeral)
    key, nonce = _key_schedule(_shared_secret(_dh(ephemeral, pk_r), enc, pk_r), info)
    return enc, AESGCM(key).encrypt(nonce, pt, aad)


def hpke_open_base(sk_r: Any, enc: bytes, info: bytes, aad: bytes, ct: bytes) -> bytes:
    """HPKE OpenBase, sequence 0; raises when the ciphertext, the header or the key does not match."""
    _, _, AESGCM = _ec()
    recipient = _private(sk_r)
    pk_r = _point_of(recipient)
    key, nonce = _key_schedule(_shared_secret(_dh(recipient, enc), enc, pk_r), info)
    return AESGCM(key).decrypt(nonce, ct, aad)

# ---- The envelope ---------------------------------------------------------------------------------------------------

_HEADER_MEMBERS = sorted(['agentRef', 'alg', 'enc', 'envelopeSequence', 'exp', 'iat', 'placement', 'recipient',
                          'sender', 'slot', 'suite', 'type'])
_AGENT_REF = re.compile(r'^urn:fabric:agent:[a-z][a-z0-9]*(?:-[a-z0-9]+)*:[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
_SLOT = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
_B64URL = re.compile(r'^[A-Za-z0-9_-]+$')
_SIG = re.compile(r'^[A-Za-z0-9_-]{86}$')


def _kid(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 256


def _counter(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (isinstance(value, int) or value.is_integer()) and 1 <= value <= 2 ** 53 - 1


def _signing_input(envelope: Mapping[str, Any]) -> bytes:
    return jcs.canonical_bytes({'header': envelope['header'], 'ct': envelope['ct']})


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    """`sha256:` and the hex SHA-256 of the envelope's RFC 8785 bytes: the blob name a relay stores it under."""
    return 'sha256:' + hashlib.sha256(jcs.canonical_bytes(envelope)).hexdigest()


def _numbers(values: list) -> list:
    """Numbers as JSON writes them (16.0 is 16); anything else stays itself, so it never equals the suite."""
    return [float(v) if not isinstance(v, bool) and isinstance(v, (int, float)) else v for v in values]


def envelope_shape(value: Any) -> bool:
    """Exactly the shape of `secret-envelope.schema.json`, with `iat` and `exp` real instants."""
    if not isinstance(value, dict) or sorted(value) != ['ct', 'header', 'sig'] or not isinstance(value['header'], dict):
        return False
    h = value['header']
    return (sorted(h) == _HEADER_MEMBERS and h['type'] == ENVELOPE_TYPE and isinstance(h['alg'], str)
            and isinstance(h['suite'], list) and _numbers(h['suite']) == list(ENVELOPE_SUITE)
            and _kid(h['recipient']) and _kid(h['sender'])
            and isinstance(h['agentRef'], str) and len(h['agentRef']) <= 146 and bool(_AGENT_REF.match(h['agentRef']))
            and isinstance(h['slot'], str) and bool(_SLOT.match(h['slot']))
            and _counter(h['placement']) and _counter(h['envelopeSequence'])
            and relay.instant(h['iat']) is not None and relay.instant(h['exp']) is not None
            and isinstance(h['enc'], str) and bool(_B64URL.match(h['enc']))
            and isinstance(value['ct'], str) and bool(_B64URL.match(value['ct']))
            and isinstance(value['sig'], str) and bool(_SIG.match(value['sig'])))


def _instant_text(ms: int) -> str:
    """An instant as the reference writes it: whole seconds `...Z`, otherwise with milliseconds."""
    seconds, millis = divmod(ms, 1000)
    base = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds))
    return base + ('Z' if millis == 0 else '.%03dZ' % millis)


def seal_envelope(*, recipient_kid: str, recipient_key: Any, sender_kid: str, signer: Callable[[bytes], str],
                  agent_ref: str, slot: str, placement: int, envelope_sequence: int, iat_ms: int, exp_ms: int,
                  secret: bytes, ephemeral_private_key: Any = None) -> dict:
    """Seals `secret` to `recipient_key` (the target's `#kem`) and signs it with `signer` (this machine's `#sig`).
    `envelope_sequence` is the sealer's next sequence for (agent_ref, slot), kept durably beside the vault slot; a
    re-send sends the stored envelope, never a new one at a used sequence."""
    if not recipient_kid.endswith('#kem'):
        raise ValueError('wrong-recipient: an envelope is sealed to a #kem key')
    if not sender_kid.endswith('#sig'):
        raise ValueError('bad-key: an envelope is signed by a #sig key')
    if p256.key_refusal(recipient_key) is not None:
        raise ValueError('bad-key: the recipient key is not a valid P-256 key')
    if not _counter(envelope_sequence):
        raise ValueError('malformed: envelopeSequence is an integer from 1 to 2^53 - 1')
    ec, _, _ = _ec()
    ephemeral = _private(ephemeral_private_key) if ephemeral_private_key is not None \
        else ec.generate_private_key(ec.SECP256R1())
    header = {'type': ENVELOPE_TYPE, 'alg': relay.RELAY_ALG, 'suite': list(ENVELOPE_SUITE), 'recipient': recipient_kid,
              'sender': sender_kid, 'agentRef': agent_ref, 'slot': slot, 'placement': placement,
              'envelopeSequence': envelope_sequence, 'iat': _instant_text(iat_ms), 'exp': _instant_text(exp_ms),
              'enc': p256.b64url_encode(_point_of(ephemeral))}
    # A sealer never spends a sequence on an envelope every target refuses: shape and life are checked first.
    if not envelope_shape({'header': header, 'ct': 'AA', 'sig': 'A' * 86}):
        raise ValueError("malformed: the envelope would not have the schema's shape")
    life = relay.instant(header['exp']) - relay.instant(header['iat'])
    if life <= 0 or life > MAX_ENVELOPE_LIFETIME_SECONDS * 1000:
        raise ValueError('lifetime-too-long: an envelope lives more than zero and at most 24 hours')
    _, ct = hpke_seal_base(p256.point(recipient_key), _INFO, jcs.canonical_bytes(header), secret, ephemeral)
    unsigned = {'header': header, 'ct': p256.b64url_encode(ct)}
    return dict(unsigned, sig=signer(_signing_input(unsigned)))


def _refuse(refusal: str) -> dict:
    return {'ok': False, 'refusal': refusal}


def open_envelope(value: Any, ctx: Mapping[str, Any]) -> dict:
    """`ctx` holds `own_kem_kid`, `own_kem_private_key` (32 bytes or a key object), `sender_key(kid)`,
    `may_hold(sender, agent_ref, slot)`, `current_placement(agent_ref, slot)`, `applied_revision()`,
    `last_opened(agent_ref, slot)` (the mark `{since, sequence, digest}` or None) and `now_ms`."""
    if not envelope_shape(value):
        return _refuse('malformed')
    header = value['header']
    if header['alg'] != relay.RELAY_ALG:
        return _refuse('unsupported-alg')
    if header['recipient'] != ctx['own_kem_kid']:
        return _refuse('wrong-recipient')
    sender_key = ctx['sender_key'](header['sender'])
    if sender_key is not None and p256.is_ed25519_key(sender_key):
        return _refuse('unsupported-alg')
    enc_point = p256.b64url_decode(header['enc']) or b''
    if sender_key is None or p256.key_refusal(sender_key) is not None or p256.key_refusal(enc_point) is not None:
        return _refuse('bad-key')
    if p256.verify_es256(sender_key, _signing_input(value), value['sig']) is not True:
        return _refuse('bad-signature')
    if not ctx['may_hold'](header['sender'], header['agentRef'], header['slot']):
        return _refuse('sender-not-allowed')
    current = ctx['current_placement'](header['agentRef'], header['slot'])
    applied = ctx['applied_revision']()
    if current is None or applied is None or header['placement'] < current or header['placement'] > applied:
        return _refuse('placement-stale')
    # Anti-rollback: an equal sequence passes only for the same envelope; a mark ahead of the run fails closed.
    digest = envelope_digest(value)
    mark = ctx['last_opened'](header['agentRef'], header['slot'])
    if mark is not None and mark['since'] >= current:
        if mark['since'] > current or header['envelopeSequence'] < mark['sequence']:
            return _refuse('replayed')
        if header['envelopeSequence'] == mark['sequence'] and mark['digest'] != digest:
            return _refuse('sequence-reused')
    iat, exp, now = relay.instant(header['iat']), relay.instant(header['exp']), ctx['now_ms']
    if iat - now > relay.CLOCK_SKEW_SECONDS * 1000:
        return _refuse('not-yet-valid')
    if exp <= iat or exp - iat > MAX_ENVELOPE_LIFETIME_SECONDS * 1000:
        return _refuse('lifetime-too-long')
    if now >= exp:
        return _refuse('expired')
    try:
        secret = hpke_open_base(ctx['own_kem_private_key'], enc_point, _INFO, jcs.canonical_bytes(header),
                                p256.b64url_decode(value['ct']) or b'')
    except Exception:                                      # any failure to open is one answer, never its detail
        return _refuse('cannot-open')
    return {'ok': True, 'secret': secret, 'mark': {'agentRef': header['agentRef'], 'slot': header['slot'],
                                                   'since': current, 'sequence': header['envelopeSequence'],
                                                   'digest': digest}}

# ---- The open context from the estate record -----------------------------------------------------------------------


def _list(value: Any) -> list:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _revoked(record: dict, kind: str) -> list:
    return [entry.get('ref') for entry in _list(record.get('revocations')) if entry.get('kind') == kind]


def _placed_from(record: dict, machine_id: str, agent_ref: str, slot: str) -> Any:
    """The `from` of a machine's personal placement of (agent_ref, slot): an envelope names no context, and an
    organization's instance fills its slot on its own machine."""
    machine = next((m for m in _list(record.get('machines')) if m.get('id') == machine_id), None)
    secret = next((s for s in _list(machine.get('secrets') if machine else None)
                   if s.get('agentRef') == agent_ref and s.get('slot') == slot
                   and ('context' not in s or s['context'] == PERSONAL_CONTEXT)), None)
    return secret.get('from') if secret else None


def _signing_key(record: dict, machine_id: Any):
    if machine_id in _revoked(record, 'machine'):
        return None
    machine = next((m for m in _list(record.get('machines')) if m.get('id') == machine_id), None)
    key = next((k for k in _list(machine.get('deviceKeys') if machine else None)
                if k.get('use') == 'sig' and isinstance(k.get('kid'), str) and k['kid'].endswith('#sig')), None)
    if key is None or not isinstance(key.get('publicKey'), str) or key['kid'] in _revoked(record, 'device-key'):
        return None
    return key['kid'], relay._base64(key['publicKey'])


def estate_envelope_context(applied: Sequence[dict], machine_id: str) -> dict:
    """`may_hold`, `current_placement`, `sender_key` and `applied_revision` for `machine_id`, from the estate-record
    revisions it verified and applied, oldest first. The latest decides who may hold a slot and under which key; the
    current placement is the earliest revision of the unbroken run, ending at the latest, that names the same `from`."""
    latest = applied[-1] if applied else None

    def may_hold(sender_kid: str, agent_ref: str, slot: str) -> bool:
        if latest is None:
            return False
        source = _placed_from(latest, machine_id, agent_ref, slot)
        found = _signing_key(latest, source) if source is not None else None
        return found is not None and found[0] == sender_kid

    def current_placement(agent_ref: str, slot: str) -> Optional[int]:
        if latest is None:
            return None
        source = _placed_from(latest, machine_id, agent_ref, slot)
        if source is None:
            return None
        since = len(applied) - 1
        while since > 0 and _placed_from(applied[since - 1], machine_id, agent_ref, slot) == source:
            since -= 1
        return applied[since].get('revision')

    def sender_key(kid: str):
        if latest is None:
            return None
        for machine in _list(latest.get('machines')):
            found = _signing_key(latest, machine.get('id'))
            if found is not None and found[0] == kid:
                return found[1]
        return None

    return {'may_hold': may_hold, 'current_placement': current_placement, 'sender_key': sender_key,
            'applied_revision': lambda: latest.get('revision') if latest is not None else None}
