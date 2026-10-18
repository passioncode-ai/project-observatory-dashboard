"""The revocation delta and the timestamp heartbeat (contract `src/revocation-delta.ts`, DEC-0054).

The delta is an append-only chain of signed entries, one revocation each; entry n names the hash of the head before
it, so the head is `{sequence, sha256}` — the shape a device check-in reports and a relay's `welcome.deltaHead`
carries. The heartbeat is a signed timestamp that commits to the delta's head and to the estate revision. Both are
signed by an estate key: a hub key the highest estate-record revision the machine verified and applied certifies, not
revoked there or by an entry applied before. A machine whose heartbeat is more than 72 hours old, that has not applied
up to the heartbeat's head and revision, or that saw two signed histories, refuses what arrives from other machines
and keeps the person's own work running.

A state is a dict `{estate, entries, heartbeat?, broken?, forked?}` (`entries[n - 1]` is entry n), so the contract's
vectors are states as they are. A trust is `{record, at}`: `at` the verifier's clock in milliseconds (or a `datetime`).
Every function here answers and never raises: malformed input is a verdict, in the reference's order and codes.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import math
from typing import Any, Dict, List, Mapping, Optional

from . import ed25519, jcs, p256
from .estate import KEY_REFERENCE
from .identifiers import same_identifier
from .relay import CLOCK_SKEW_SECONDS, strict_time

DELTA_TYPE = 'revocation-delta/1'
HEARTBEAT_TYPE = 'heartbeat/1'
#: The estate key's algorithm until Fabric records D2; ES256 joins additively then (DEC-0055 item 9).
DELTA_ALG = 'EdDSA'
#: The revocation kinds an entry may carry: the estate record's, without `authority`.
DELTA_KINDS = ('machine', 'device-key', 'hub-key', 'publisher-key', 'package', 'grant')
#: v4 §7: without a heartbeat for 72 hours, calls from other machines fail closed.
HEARTBEAT_STALE_SECONDS = 72 * 60 * 60
#: The longest a running hub lets pass between two heartbeats.
HEARTBEAT_MAX_INTERVAL_SECONDS = 24 * 60 * 60
#: The interval a running hub SHOULD keep, beside a heartbeat after every entry and every revision.
HEARTBEAT_INTERVAL_SECONDS = 60 * 60
MAX_FUTURE_SECONDS = CLOCK_SKEW_SECONDS
#: The most entries one page carries, so a relay frame stays under 1 MiB.
MAX_PAGE_ENTRIES = 256

_MAX_SAFE = 2 ** 53 - 1
_HEX64 = '0123456789abcdef'


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_count(value: Any, minimum: int) -> bool:
    """JavaScript's `Number.isSafeInteger(value) && value >= minimum` over a parsed JSON value."""
    if not _is_number(value) or (isinstance(value, float) and not value.is_integer()):
        return False
    return minimum <= value <= _MAX_SAFE and value >= -_MAX_SAFE


def _same(a: Any, b: Any) -> bool:
    """JavaScript's `===` over parsed JSON: numbers by value, strings and literals by type and value, objects by identity."""
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    return type(a) is type(b) and a == b


def _list(value: Any) -> List[dict]:
    return [item for item in value if _is_obj(item)] if isinstance(value, list) else []


def _hex64(value: Any) -> bool:
    return _is_str(value) and len(value) == 64 and all(c in _HEX64 for c in value)


def _digest(value: Any) -> bool:
    return _is_str(value) and value.startswith('sha256:') and _hex64(value[7:])


def _sig_text(value: Any) -> bool:
    return _is_str(value) and len(value) == 86 and p256.b64url_decode(value) is not None


def _show(value: Any) -> str:
    if _is_str(value):
        return value
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def delta_time(value: Any) -> float:
    """The shared strict RFC 3339 reader as milliseconds, or NaN: an unreadable time is refused before any comparison."""
    ms = strict_time(value)
    return math.nan if ms is None else float(ms)


def _clock(trust: Any) -> float:
    """The clock a verifier is given, as milliseconds, or NaN when it is not readable."""
    at = trust.get('at') if _is_obj(trust) else None
    if isinstance(at, _dt.datetime):
        return at.timestamp() * 1000 if at.tzinfo is not None else math.nan
    return float(at) if _is_number(at) and math.isfinite(at) else math.nan


def signing_input(document: Mapping[str, Any]) -> bytes:
    """The bytes an entry's or a heartbeat's signature covers: RFC 8785 of the document without `sig`. Raises
    `jcs.CanonicalError` for a value that has no RFC 8785 form."""
    return jcs.canonical_bytes({key: value for key, value in document.items() if key != 'sig'})


def delta_entry_hash(entry: Mapping[str, Any]) -> str:
    """The head hash after an entry: the hex SHA-256 of its signing input."""
    return hashlib.sha256(signing_input(entry)).hexdigest()


def delta_genesis(estate: str) -> str:
    """The head before entry 1: SHA-256 of RFC 8785 `{type: "revocation-delta/1", estate, sequence: 0}`."""
    return hashlib.sha256(jcs.canonical_bytes({'type': DELTA_TYPE, 'estate': estate, 'sequence': 0})).hexdigest()


def empty_delta_state(estate: str) -> Dict[str, Any]:
    """A machine that has applied no entry yet."""
    return {'estate': estate, 'entries': []}


def _head_at(state: Mapping[str, Any], sequence: Any) -> str:
    """The hash of the head at `sequence` in the held chain: the genesis at 0; "" for what the state does not hold."""
    try:
        if sequence == 0:
            return delta_genesis(state['estate'])
        index = int(sequence) - 1
        entries = state['entries']
        entry = entries[index] if 0 <= index < len(entries) else None
        return delta_entry_hash(entry) if _is_obj(entry) else ''
    except (jcs.CanonicalError, TypeError, ValueError, KeyError, OverflowError):
        return ''


def _usable(state: Any) -> bool:
    return _is_obj(state) and _is_str(state.get('estate')) and isinstance(state.get('entries'), list)


def delta_head(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The head a machine has applied, in the check-in's `records.revocationDelta` shape."""
    sequence = len(state['entries'])
    return {'sequence': sequence, 'sha256': _head_at(state, sequence)}


def delta_revocations(state: Optional[Mapping[str, Any]]) -> List[dict]:
    """The revocations the delta holds in force: the applied entries', then those of signed entries that forked."""
    held = state if _is_obj(state) else {}
    return [entry['revocation'] for entry in _list(held.get('entries')) + _list(held.get('forked'))
            if _is_obj(entry.get('revocation'))]


def effective_revocations(record: Any, state: Optional[Mapping[str, Any]]) -> List[dict]:
    """Every revocation in force on a machine: the applied revision's, then the delta's."""
    return _list(record.get('revocations') if _is_obj(record) else None) + delta_revocations(state)


def is_revoked(revocations: Any, kind: str, ref: Any) -> bool:
    """Whether `revocations` revoke `ref` of `kind`, matched exactly; a `grant` ref by the normal form of its URI."""
    if not _is_str(ref) or not isinstance(revocations, (list, tuple)):
        return False

    def same(other: Any) -> bool:
        return same_identifier(other, ref) if kind == 'grant' else _same(other, ref)
    return any(_is_obj(r) and _same(r.get('kind'), kind) and same(r.get('ref')) for r in revocations)


def estate_signer(record: Any, kid: Any, revocations: Any) -> Dict[str, Any]:
    """The estate key a delta entry or a heartbeat is verified under: `{'key': raw 32 bytes}` or `{'problem': ...}` —
    `unknown-signer`, `revoked-signer`, `unusable-key` or `small-order-key`."""
    machines = [m for m in _list(record.get('machines') if _is_obj(record) else None)
                if _is_obj(m.get('hubKey')) and _same(m['hubKey'].get('kid'), kid)]
    if not _is_str(kid) or len(machines) != 1:
        return {'problem': 'unknown-signer'}
    machine = machines[0]
    jwk = machine['hubKey']
    if is_revoked(revocations, 'hub-key', kid) or is_revoked(revocations, 'machine', machine.get('id')):
        return {'problem': 'revoked-signer'}
    if jwk.get('alg') != DELTA_ALG or jwk.get('kty') != 'OKP' or jwk.get('crv') != 'Ed25519' \
            or not _is_str(jwk.get('x')) or 'd' in jwk:
        return {'problem': 'unusable-key'}
    raw = p256.b64url_canonical(jwk['x'])
    if raw is None or len(raw) != 32:
        return {'problem': 'unusable-key'}
    if ed25519.is_small_order_key(raw):
        return {'problem': 'small-order-key'}
    return {'key': raw}


def _verifies(data: bytes, sig: str, key: bytes) -> bool:
    raw = p256.b64url_decode(sig)
    return raw is not None and ed25519.verify(key, data, raw)


def _entry_shape(entry: Any) -> Optional[str]:
    """An entry's shape: the first thing wrong, or None."""
    if not _is_obj(entry):
        return 'an entry is an object'
    if entry.get('type') != DELTA_TYPE:
        return f"an entry's type is {DELTA_TYPE}"
    if not all(_is_str(entry.get(name)) for name in ('estate', 'kid', 'alg')):
        return 'estate, kid and alg are strings'
    if not _is_count(entry.get('sequence'), 1):
        return 'sequence is an integer from 1 to 2^53 - 1'
    if not _hex64(entry.get('prev')):
        return 'prev is 64 lower-case hex'
    if not _sig_text(entry.get('sig')):
        return 'sig is 64 bytes in base64url'
    revocation = entry.get('revocation')
    if not _is_obj(revocation) or not _is_str(revocation.get('kind')) or not _is_str(revocation.get('ref')):
        return 'revocation names its kind and ref'
    # A device-key ref is a kid or a key reference (DEC-0084): one with `@` that is neither is refused in every revision
    # that lists it (estate.py), while reconciliation requires the next revision to list it, so the chain would stop.
    if revocation['kind'] == 'device-key' and '@' in revocation['ref'] and not KEY_REFERENCE.fullmatch(revocation['ref']):
        return 'a device-key ref is a kid or a key reference <kid>@sha256:<64 lower-case hex digits>'
    at, revoked_at = delta_time(entry.get('at')), delta_time(revocation.get('revokedAt'))
    if math.isnan(at) or math.isnan(revoked_at):
        return 'at and revokedAt are RFC 3339 date-times with an offset, on days that exist'
    if revoked_at > at:
        return 'a revocation is never dated after the entry that carries it'
    try:
        signing_input(entry)
    except jcs.CanonicalError:
        return 'the entry has no RFC 8785 form'
    return None


def apply_delta(state: Any, page: Any, trust: Any) -> Dict[str, Any]:
    """Applies a page of entries (`{head, entries}`) to what a machine holds, in order: shape, estate, `EdDSA`, an
    admitted kind, not more than 60 s ahead, an estate key the record certifies and nothing revokes, the signature; then
    placement — an entry already held is skipped, one past the next sequence is a `gap`, a signed entry whose `prev` is
    not the held head (`broken-chain`) or that differs from the entry held at its sequence (`fork`) marks the state
    `broken` and keeps its revocation in force. On a refusal the entries before it stay applied.
    `{'ok': True, 'state', 'applied'}` or `{'ok': False, 'problem', 'index', 'state', 'reason'}`."""
    def fail(problem: str, index: int, current: Any, reason: str) -> Dict[str, Any]:
        return {'ok': False, 'problem': problem, 'index': index, 'state': current, 'reason': reason}

    now = _clock(trust)
    if not math.isfinite(now):
        return fail('no-clock', -1, state, "the verifier's clock is not a readable time")
    if not _usable(state):
        return fail('malformed', -1, state, 'the held state names its estate and lists its entries')
    if not _is_obj(page) or not isinstance(page.get('entries'), list):
        return fail('malformed', -1, state, 'a page is {head, entries}')
    record = trust.get('record')
    if not _is_obj(record) or not _same(record.get('estate'), state['estate']):
        return fail('wrong-estate', -1, state, "the trusted revision is not this estate's")
    current, applied = state, 0
    for index, entry in enumerate(page['entries']):
        shape = _entry_shape(entry)
        if shape is not None:
            return fail('malformed', index, current, shape)
        revocation = entry['revocation']
        if entry['estate'] != current['estate']:
            return fail('wrong-estate', index, current, f"the entry is estate {_show(entry['estate'])}'s")
        if entry['alg'] != DELTA_ALG:
            return fail('unsupported-alg', index, current, f'an estate key signs with {DELTA_ALG}')
        if revocation['kind'] not in DELTA_KINDS:
            return fail('not-revocable', index, current,
                        f"the delta does not carry a {_show(revocation['kind'])} revocation: a passkey is revoked only "
                        'by a revision')
        if delta_time(entry['at']) > now + MAX_FUTURE_SECONDS * 1000:
            return fail('not-yet-valid', index, current, "the entry is dated more than 60 s ahead of the verifier's clock")
        signer = estate_signer(record, entry['kid'], effective_revocations(record, current))
        if 'problem' in signer:
            return fail(signer['problem'], index, current,
                        f"no estate key the trusted revision certifies signs as {_show(entry['kid'])}")
        if not _verifies(signing_input(entry), entry['sig'], signer['key']):
            return fail('bad-signature', index, current, 'the signature does not verify')
        sequence = int(entry['sequence'])
        held = len(current['entries'])
        if sequence <= held:
            if _head_at(current, sequence) == delta_entry_hash(entry):
                continue
            return fail('fork', index, dict(current, broken=True, forked=list(current.get('forked') or []) + [entry]),
                        f'two signed entries carry sequence {sequence}')
        if sequence > held + 1:
            return fail('gap', index, current, f'the next entry is {held + 1}; ask from it with delta-query')
        if entry['prev'] != _head_at(current, held):
            return fail('broken-chain', index,
                        dict(current, broken=True, forked=list(current.get('forked') or []) + [entry]),
                        'prev is not the head this machine holds: a second signed history')
        current = dict(current, entries=list(current['entries']) + [entry])
        applied += 1
    return {'ok': True, 'state': current, 'applied': applied}


def _heartbeat_shape(heartbeat: Any) -> Optional[str]:
    """A heartbeat's shape: the first thing wrong, or None."""
    if not _is_obj(heartbeat):
        return 'a heartbeat is an object'
    if heartbeat.get('type') != HEARTBEAT_TYPE:
        return f"a heartbeat's type is {HEARTBEAT_TYPE}"
    if not all(_is_str(heartbeat.get(name)) for name in ('estate', 'kid', 'alg')):
        return 'estate, kid and alg are strings'
    if not _is_count(heartbeat.get('sequence'), 0):
        return 'sequence is an integer from 0 to 2^53 - 1'
    if math.isnan(delta_time(heartbeat.get('at'))):
        return 'at is an RFC 3339 date-time with an offset, on a day that exists'
    head = heartbeat.get('deltaHead')
    if not _is_obj(head) or not _is_count(head.get('sequence'), 0) or not _hex64(head.get('sha256')):
        return 'deltaHead is {sequence, sha256}, sha256 64 lower-case hex'
    revision = heartbeat.get('estateRecord')
    if not _is_obj(revision) or not _is_count(revision.get('revision'), 1) or not _digest(revision.get('digest')):
        return 'estateRecord is {revision, digest}, digest sha256: and 64 hex'
    if not _sig_text(heartbeat.get('sig')):
        return 'sig is 64 bytes in base64url'
    try:
        signing_input(heartbeat)
    except jcs.CanonicalError:
        return 'the heartbeat has no RFC 8785 form'
    return None


def _same_bytes(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    try:
        return signing_input(a) == signing_input(b)
    except jcs.CanonicalError:
        return False


def accept_heartbeat(state: Any, heartbeat: Any, trust: Any) -> Dict[str, Any]:
    """Accepts a heartbeat: shape, estate, `EdDSA`, not more than 60 s ahead, an estate key, the signature; then
    against what the machine holds — an older sequence is `not-newer`, the same sequence with other bytes or a head or
    revision contradicting the machine's own is a `fork` (the state becomes `broken`), a newer one moving time, head or
    revision back is `regressed`. `{'ok': True, 'state', 'newer'}` or `{'ok': False, 'problem', 'state', 'reason'}`."""
    def fail(problem: str, current: Any, reason: str) -> Dict[str, Any]:
        return {'ok': False, 'problem': problem, 'state': current, 'reason': reason}

    now = _clock(trust)
    if not math.isfinite(now):
        return fail('no-clock', state, "the verifier's clock is not a readable time")
    if not _usable(state):
        return fail('malformed', state, 'the held state names its estate and lists its entries')
    shape = _heartbeat_shape(heartbeat)
    if shape is not None:
        return fail('malformed', state, shape)
    h = heartbeat
    record = trust.get('record')
    if h['estate'] != state['estate'] or not _is_obj(record) or not _same(record.get('estate'), state['estate']):
        return fail('wrong-estate', state, f"the heartbeat is estate {_show(h['estate'])}'s")
    if h['alg'] != DELTA_ALG:
        return fail('unsupported-alg', state, f'an estate key signs with {DELTA_ALG}')
    at = delta_time(h['at'])
    if at > now + MAX_FUTURE_SECONDS * 1000:
        return fail('not-yet-valid', state, "the heartbeat is dated more than 60 s ahead of the verifier's clock")
    signer = estate_signer(record, h['kid'], effective_revocations(record, state))
    if 'problem' in signer:
        return fail(signer['problem'], state, f"no estate key the trusted revision certifies signs as {_show(h['kid'])}")
    if not _verifies(signing_input(h), h['sig'], signer['key']):
        return fail('bad-signature', state, 'the signature does not verify')
    head, revision = h['deltaHead'], h['estateRecord']

    def broken(reason: str) -> Dict[str, Any]:
        return fail('fork', dict(state, broken=True), reason)

    # A held heartbeat is one this function accepted; a corrupt held state never reads as "nothing newer is held".
    # A held `null` is present, so it is malformed too (the reference tests `!== undefined`).
    if 'heartbeat' in state and _heartbeat_shape(state['heartbeat']) is not None:
        return fail('malformed', state, 'the heartbeat held is not one the machine could have accepted')
    held = state['heartbeat'] if 'heartbeat' in state else None
    if held is not None:
        if h['sequence'] < held['sequence']:
            return fail('not-newer', state, 'the machine holds a newer heartbeat')
        if h['sequence'] == held['sequence']:
            if _same_bytes(h, held):
                return {'ok': True, 'state': state, 'newer': False}
            return broken(f"two signed heartbeats carry sequence {_show(h['sequence'])}")
        if at < delta_time(held['at']) or head['sequence'] < held['deltaHead']['sequence'] \
                or revision['revision'] < held['estateRecord']['revision']:
            return fail('regressed', state, 'a newer heartbeat never moves its time, the head or the revision back')
    sequence = head['sequence']
    if sequence <= len(state['entries']) and _head_at(state, sequence) != head['sha256']:
        return broken(f"the heartbeat's head at {_show(sequence)} is not the entry this machine holds there")
    if _same(revision['revision'], record.get('revision')) and not _same(revision['digest'], record.get('digest')):
        return broken(f"the heartbeat names another revision {_show(revision['revision'])} than the one this machine "
                      'applied')
    return {'ok': True, 'state': dict(state, heartbeat=h), 'newer': True}


def freshness_answer(at: float, applied: Mapping[str, Any], heartbeat: Any = None, broken: Any = None) -> Dict[str, str]:
    """The 72-hour rule as a pure function of what a machine holds: `broken` once two signed histories were seen;
    `stale` with no readable clock, no heartbeat, an unreadable one or one more than 72 hours old; `behind` when the
    heartbeat's head or revision is ahead of what the machine applied; else `current`. Only `current` accepts what
    arrives from other machines."""
    if broken is True:
        return {'answer': 'broken', 'reason': "two signed histories were seen; the person's own work continues"}
    if not _is_number(at) or not math.isfinite(at):
        return {'answer': 'stale', 'reason': "the machine's clock is not readable"}
    if not _is_obj(heartbeat):
        return {'answer': 'stale', 'reason': 'no heartbeat is held'}
    beat = delta_time(heartbeat.get('at'))
    head = heartbeat['deltaHead'].get('sequence') if _is_obj(heartbeat.get('deltaHead')) else None
    revision = heartbeat['estateRecord'].get('revision') if _is_obj(heartbeat.get('estateRecord')) else None
    if math.isnan(beat) or not _is_count(head, 0) or not _is_count(revision, 1):
        return {'answer': 'stale', 'reason': 'the heartbeat held is not readable'}
    if at - beat > HEARTBEAT_STALE_SECONDS * 1000:
        return {'answer': 'stale', 'reason': 'the heartbeat is more than 72 hours old'}
    # An applied count that is not a count is behind everything: it never reads as caught up.
    sequence = applied.get('sequence') if _is_count(applied.get('sequence'), 0) else -1
    applied_revision = applied.get('revision') if _is_count(applied.get('revision'), 1) else 0
    if sequence < head:
        return {'answer': 'behind', 'reason': f"the delta is applied to {_show(sequence)}, the heartbeat's head is "
                                              f'{_show(head)}'}
    if applied_revision < revision:
        return {'answer': 'behind', 'reason': f'revision {_show(applied_revision)} is applied, the heartbeat names '
                                              f'{_show(revision)}'}
    return {'answer': 'current', 'reason': 'a heartbeat of at most 72 hours, and the delta and the record applied up to it'}


def delta_freshness(state: Any, trust: Any) -> Dict[str, str]:
    """`freshness_answer` over a machine's state and trust, with `remote`: `accept` only when `current`."""
    record = trust['record'] if _is_obj(trust) and _is_obj(trust.get('record')) else {}
    ok = _usable(state)
    verdict = freshness_answer(_clock(trust),
                               {'sequence': len(state['entries']) if ok else None, 'revision': record.get('revision')},
                               state.get('heartbeat') if ok else None, ok and state.get('broken') is True)
    return dict(verdict, remote='accept' if verdict['answer'] == 'current' else 'refuse')


def unreconciled(revision: Any, entries: Any) -> Dict[str, Any]:
    """Reconciliation: a revision issued at or after an entry's `at` lists that entry's revocation. Returns the entries
    it leaves out, and whether an `issuedAt` or an entry was unreadable (reported, never compared)."""
    issued_at = delta_time(revision.get('issuedAt') if _is_obj(revision) else None)
    listed = _list(revision.get('revocations') if _is_obj(revision) else None)
    missing: List[Dict[str, Any]] = []
    unreadable = math.isnan(issued_at)
    for index, entry in enumerate(entries if isinstance(entries, (list, tuple)) else []):
        revocation = entry.get('revocation') if _is_obj(entry) and _is_obj(entry.get('revocation')) else None
        at = delta_time(entry.get('at') if _is_obj(entry) else None)
        if revocation is None or not _is_str(revocation.get('kind')) or not _is_str(revocation.get('ref')) \
                or math.isnan(at):
            unreadable = True
            continue
        if not unreadable and at <= issued_at and not is_revoked(listed, revocation['kind'], revocation['ref']):
            missing.append({'index': index, 'kind': revocation['kind'], 'ref': revocation['ref']})
    return {'missing': missing, 'unreadable': unreadable}


def reconcile_revision(state: Any, revision: Any) -> Dict[str, Any]:
    """Whether a machine holding `state` may apply `revision` (schema-valid and accepted by the estate verifier):
    `{'ok': True}`, or `{'ok': False, 'problem': 'malformed' | 'unreconciled', 'missing': [...]}`."""
    if not _usable(state):
        return {'ok': False, 'problem': 'malformed', 'missing': []}
    result = unreconciled(revision, state['entries'])
    if result['unreadable']:
        return {'ok': False, 'problem': 'malformed', 'missing': []}
    if result['missing']:
        return {'ok': False, 'problem': 'unreconciled', 'missing': result['missing']}
    return {'ok': True}
