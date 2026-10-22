"""`share-registration/1`, the node's half (contract DEC-0086, `docs/specification/share-registration.md`).

A port of the contract kit's `deriveShareRegistrations`, `gateGrantUsage`, `isShareGrantId` and
`shareRegistrationDigest` (`src/share-registration.ts`), run against the contract's own vectors
(`fabric/node-vectors/share-registration.json`: `grantId`, `derive` and `gate`). The node derives registrations only from
the estate revision and the revocation-delta entries it verified and applied, never from a draft; the receiver's half
(`decideShareRegistration`, `joinGrantUsage`) is the organization server's.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, List, Optional

from . import jcs
from .identifiers import same_identifier
from .relay import strict_time

TYPE = 'share-registration/1'
RECEIPT_TYPE = 'share-registration-receipt/1'
#: A node sends each active registration again at least this often.
RESEND_SECONDS = 7 * 24 * 60 * 60

SHARE_GRANT_ID = re.compile(r'urn:fabric:share:(?!0{32}\Z)[0-9a-f]{32}\Z')
ORG_CONTEXT = re.compile(r'org:[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
AGENT_REF = re.compile(r'urn:fabric:agent:[a-z][a-z0-9]*(?:-[a-z0-9]+)*:[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')
INSTANT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\.[0-9]{1,3})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})\Z')
_SUB_MS = re.compile(r':[0-9]{2}\.[0-9]{4,}')
_MAX_SAFE = 2 ** 53 - 1
ACTIVE_MEMBERS = ('type', 'grantId', 'context', 'state', 'grantRevision', 'agentRef', 'startsAt', 'expiresAt')
WITHDRAWN_MEMBERS = ('type', 'grantId', 'context', 'state', 'withdrawnAt')
SEND_AFTER = {'stale-revision': 409, 'already-withdrawn': 409}
DEGRADED_AFTER = {'revision-conflict': 409, 'agent-mismatch': 409, 'wrong-organization': 403,
                  'unassigned-device': 403, 'malformed': 400, 'schema-unsupported': 400}
#: DEC-0100, SR-5a: the person's acknowledged notice does not name a share registration's purposes. Held, not degraded:
#: the node keeps the sums and sends the registration again at the seven-day resend (or when its policy names a later
#: `noticeVersion`).
NOTICE_AFTER = {'purpose-not-collected': 403}


def _str(value: Any) -> bool:
    return isinstance(value, str)


def _list(value: Any) -> List[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _show(value: Any) -> str:
    """JavaScript's `String(value)` for what a record can hold."""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, dict)):
        return '[object Object]' if isinstance(value, dict) else ','.join(_show(v) for v in value)
    return str(value)


def is_share_grant_id(value: Any) -> bool:
    return _str(value) and SHARE_GRANT_ID.fullmatch(value) is not None


def is_org_context(value: Any) -> bool:
    return _str(value) and ORG_CONTEXT.fullmatch(value) is not None


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_SAFE


def _agent_ref_ok(value: Any) -> bool:
    return _str(value) and len(value) <= 146 and AGENT_REF.fullmatch(value) is not None


def _time(value: Any) -> Optional[int]:
    return strict_time(value) if _str(value) and INSTANT.fullmatch(value) else None


def _iso_ms(ms: int) -> str:
    from .relay import iso_ms
    return iso_ms(ms)


def _carried(value: Any, up: bool = False) -> Optional[str]:
    """A time as a registration carries it: as written with at most three fraction digits; with more, floored (or, for
    a window's start, rounded up by a millisecond and written in UTC). None for a time `strictTime` cannot read."""
    if not _str(value) or strict_time(value) is None:
        return None
    if not _SUB_MS.search(value):
        return value
    match = re.search(r'(\.[0-9]{3})([0-9]+)', value)
    extra = up and re.search(r'[1-9]', match.group(2)) is not None
    cut = value[:match.start()] + match.group(1) + value[match.end():]
    return _iso_ms(strict_time(cut) + 1) if extra else cut


def _instant(value: Any) -> Optional[int]:
    text = _carried(value)
    return None if text is None else strict_time(text)


def digest(registration: Any) -> Optional[str]:
    """`sha256:` and the hex SHA-256 of the UTF-8 RFC 8785 form; None when it has none."""
    try:
        return 'sha256:' + hashlib.sha256(jcs.canonical_bytes(registration)).hexdigest()
    except (jcs.CanonicalError, TypeError, ValueError):
        return None


def well_formed(doc: Any) -> bool:
    if not isinstance(doc, dict) or doc.get('type') != TYPE or not is_share_grant_id(doc.get('grantId')) \
            or not is_org_context(doc.get('context')):
        return False
    if doc.get('state') == 'active':
        start, end = _time(doc.get('startsAt')), _time(doc.get('expiresAt'))
        return set(doc) == set(ACTIVE_MEMBERS) and _positive(doc.get('grantRevision')) \
            and _agent_ref_ok(doc.get('agentRef')) and start is not None and end is not None and start < end
    return doc.get('state') == 'withdrawn' and set(doc) == set(WITHDRAWN_MEMBERS) \
        and _time(doc.get('withdrawnAt')) is not None


def _revoked_at(revocations: List[dict], grant_id: str):
    matching = [r for r in revocations if same_identifier(r.get('ref'), grant_id)]
    best = None
    for candidate in (r.get('revokedAt') for r in matching):
        ms = _instant(candidate)
        if ms is not None and (best is None or ms < _instant(best)):
            best = candidate
    return bool(matching), _carried(best)


def derive(record: Any, applied_entries: Any, org_id: Any, previously_registered: Any) -> dict:
    """`deriveShareRegistrations`: `{'registrations': [...], 'degraded': [{'grantId', 'code'}]}` for one enrollment.
    Total: malformed input answers no registration."""
    answer: dict = {'registrations': [], 'degraded': []}
    context = 'org:' + _show(org_id)
    if not isinstance(record, dict) or not _str(org_id) or not is_org_context(context):
        return answer
    revocations = [r for r in _list(record.get('revocations')) + [e.get('revocation') for e in _list(applied_entries)]
                   if isinstance(r, dict) and r.get('kind') == 'grant']
    placements = [p for m in _list(record.get('machines')) for p in _list(m.get('packages'))
                  if p.get('context') == context]

    def emit(registration: dict, grant_id: str) -> None:
        if well_formed(registration):
            answer['registrations'].append(registration)
        else:
            answer['degraded'].append({'grantId': grant_id, 'code': 'share-grant-unreadable'})

    def withdrawal(grant_id: str, at: Any) -> None:
        emit({'type': TYPE, 'grantId': grant_id, 'context': context, 'state': 'withdrawn', 'withdrawnAt': at},
             grant_id)

    listed: list = []
    for grant in _list(record.get('shareGrants')):
        if grant.get('context') != context:
            continue
        meta = grant.get('meta') if isinstance(grant.get('meta'), dict) else {}
        grant_id = meta.get('id')
        if not is_share_grant_id(grant_id):
            answer['degraded'].append({'grantId': _show(grant_id), 'code': 'share-grant-id'})
            continue
        if grant_id in listed:
            continue
        listed.append(grant_id)
        revoked, at = _revoked_at(revocations, grant_id)
        if revoked:
            withdrawal(grant_id, at)
            continue
        refs: list = []
        for placement in placements:
            if same_identifier(placement.get('grant'), grant_id) and placement.get('agentRef') not in refs:
                refs.append(placement.get('agentRef'))
        if not refs:
            continue
        if len(refs) > 1 or not _agent_ref_ok(refs[0]):
            answer['degraded'].append({'grantId': grant_id, 'code': 'share-grant-agents'})
            continue
        emit({'type': TYPE, 'grantId': grant_id, 'context': context, 'state': 'active',
              'grantRevision': meta.get('revision'), 'agentRef': refs[0],
              'startsAt': _carried(grant.get('startsAt'), True), 'expiresAt': _carried(grant.get('expiresAt'))},
             grant_id)

    earlier = [i for i in previously_registered if is_share_grant_id(i)] if isinstance(previously_registered, list) else []
    for grant_id in dict.fromkeys(earlier):
        if grant_id in listed:
            continue
        revoked, at = _revoked_at(revocations, grant_id)
        withdrawal(grant_id, at if revoked else _carried(record.get('issuedAt')))
    return answer


def gate(answer: Any) -> str:
    """`gateGrantUsage`: `send`, `hold-degraded`, `hold-notice` (DEC-0100) or `hold` for a grant's usage toward one
    enrollment, given that enrollment's latest answer to the grant's registration (None before any)."""
    def own(value: Any, key: str) -> Any:
        return value.get(key) if isinstance(value, dict) else None
    status = own(answer, 'status')
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    if status == 200 and own(own(answer, 'receipt'), 'type') == RECEIPT_TYPE:
        return 'send'
    code = own(own(own(answer, 'body'), 'error'), 'code')
    if not _str(code):
        return 'hold'
    if code in SEND_AFTER and SEND_AFTER[code] == status:
        return 'send'
    if code in NOTICE_AFTER and NOTICE_AFTER[code] == status:
        return 'hold-notice'
    return 'hold-degraded' if code in DEGRADED_AFTER and DEGRADED_AFTER[code] == status else 'hold'
