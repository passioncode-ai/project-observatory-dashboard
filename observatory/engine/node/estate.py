"""`estate-record/1` (contract `src/estate-record-rules.ts`, DEC-0055): one revision of one person's estate — the
passkeys that sign it, the machines with their device keys and hub-key certificates, the packages placed per machine,
secret placements by reference, share grants and revocations. A revision names its parent's digest and is signed by a
passkey assertion over a challenge derived from its own digest.

`verify_estate_record` is what a node applies a revision on, in the order of estate-record.md's ER table:
shape, digest, the chain to the parent, the signing passkey among the trusted ones, the assertion, the validity window,
then the record's own rules (FAC-SEM-072…076, 092, 110, 111). The caller has validated the record against
`estate-record.schema.json`; this function does not. A node trusts its peers' keys only from a revision this accepted
(`relay.peer_keys` reads them).
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, List, Optional, Sequence

from . import ed25519, jcs, p256, webauthn
from .identifiers import normal_identifier
from .relay import CLOCK_SKEW_SECONDS, strict_time

ESTATE_RECORD_SCHEMA = 'estate-record/1'
ESTATE_CHALLENGE_TYPE = 'estate-revision/1'
MAX_VALIDITY_SECONDS = 30 * 24 * 60 * 60
PERSONAL_CONTEXT = 'personal'
_ORG_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
_MISSING = object()


def _list(value: Any) -> List[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _indexed(value: Any) -> List[tuple]:
    return [(item, index) for index, item in enumerate(value) if isinstance(item, dict)] if isinstance(value, list) else []


def _show(value: Any) -> str:
    """A value as JavaScript's `String()` writes it, for messages and keys."""
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    if value is True or value is False:
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        try:
            return jcs.rfc8785(value)
        except jcs.CanonicalError:
            return 'NaN' if value != value else ('Infinity' if value > 0 else '-Infinity')
    if isinstance(value, list):
        return ','.join('' if v is None else _show(v) for v in value)
    return '[object Object]'


def _safe_integer(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (isinstance(value, int) or (math.isfinite(value) and value.is_integer())) and abs(value) <= 2 ** 53 - 1


def _to_number(value: Any) -> float:
    """`Number(value)` for the values a revision holds: a number as itself, anything else NaN (no number is written
    as a string in a record)."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return math.nan


def _same_number(a: Any, b: float) -> bool:
    return not isinstance(a, bool) and isinstance(a, (int, float)) and float(a) == b


def _time(value: Any) -> Optional[int]:
    return strict_time(value)


def _lt(a: Optional[int], b: Optional[int]) -> bool:
    """`a < b` as JavaScript compares NaN: false whenever either is not a time."""
    return a is not None and b is not None and a < b


def parse_context(value: Any) -> Optional[dict]:
    """`personal`, or `org:` and an opaque id; None for anything else (compared as written)."""
    if not isinstance(value, str):
        return None
    if value == PERSONAL_CONTEXT:
        return {'kind': 'personal'}
    if not value.startswith('org:') or not _ORG_ID.match(value[4:]):
        return None
    return {'kind': 'org', 'org': value[4:]}


def _context_of(entry: dict) -> Any:
    return PERSONAL_CONTEXT if 'context' not in entry else entry['context']


def _key_of(*parts: Any) -> str:
    return repr(parts)


def estate_record_digest(record: dict) -> str:
    """`sha256:` and the hex SHA-256 of the RFC 8785 form of the record without `digest` and `signature`; raises
    `jcs.CanonicalError` for a record that has no canonical form."""
    covered = {k: v for k, v in record.items() if k not in ('digest', 'signature')}
    return 'sha256:' + hashlib.sha256(jcs.canonical_bytes(covered)).hexdigest()


def estate_challenge_input(digest: str) -> bytes:
    return jcs.canonical_bytes({'type': ESTATE_CHALLENGE_TYPE, 'digest': digest})


def estate_challenge(digest: str) -> bytes:
    return hashlib.sha256(estate_challenge_input(digest)).digest()


def _revocation_key(entry: dict) -> str:
    return _show(entry.get('kind')) + ' ' + _show(entry.get('ref'))


def _fail(problem: str, **extra) -> dict:
    return dict({'ok': False, 'problem': problem}, **extra)


def verify_estate_record(record: Any, *, at_ms: Optional[int], parent: Any = _MISSING,
                         authorities: Optional[Sequence[Any]] = None) -> dict:
    """The reference verifier: `{'ok': True, 'digest', 'revision', 'signedBy', 'signCount'}` or `{'ok': False,
    'problem'[, 'codes']}`. `parent` is the revision before it, already verified; without one, only revision 1 starts a
    chain, under the passkeys this machine pinned (`authorities`). `at_ms` is this machine's clock; None refuses."""
    if not isinstance(at_ms, int) or isinstance(at_ms, bool):
        return _fail('no-clock')
    if not isinstance(record, dict) or not isinstance(record.get('digest'), str) \
            or not _safe_integer(record.get('revision')) or not isinstance(record.get('signature'), dict):
        return _fail('malformed')
    signature = record['signature']
    if signature.get('format') != 'webauthn-assertion' or not isinstance(signature.get('credentialId'), str):
        return _fail('malformed')
    try:
        digest = estate_record_digest(record)
    except jcs.CanonicalError:
        return _fail('malformed')
    if digest != record['digest']:
        return _fail('digest-mismatch')

    revoked: list = []
    if parent is not _MISSING:
        if not isinstance(parent, dict):
            return _fail('broken-chain')
        try:
            parent_digest = estate_record_digest(parent)
        except jcs.CanonicalError:
            return _fail('broken-chain')
        if parent.get('estate') != record.get('estate') or not _same_number(record['revision'], _to_number(parent.get('revision')) + 1) \
                or record.get('parent') != parent_digest:
            return _fail('broken-chain')
        # A machine that only verifies still refuses a revision issued before its parent, or one that drops a
        # revocation: a passkey cannot quietly un-revoke a stolen machine.
        if _lt(_time(record.get('issuedAt')), _time(parent.get('issuedAt'))):
            return _fail('broken-chain')
        kept = {_revocation_key(entry) for entry in _list(record.get('revocations'))}
        if any(_revocation_key(entry) not in kept for entry in _list(parent.get('revocations'))):
            return _fail('broken-chain')
        revoked = [entry.get('ref') for entry in _list(parent.get('revocations')) if entry.get('kind') == 'authority']
        trusted_any = _list(parent.get('authorities'))
    else:
        if record['revision'] != 1 or 'parent' in record:
            return _fail('broken-chain')
        trusted_any = list(authorities) if isinstance(authorities, (list, tuple)) else []
    if len(trusted_any) == 0:
        return _fail('no-trusted-authority')
    matches = [entry for entry in trusted_any if isinstance(entry, dict)
               and entry.get('credentialId') == signature['credentialId']]
    if len(matches) > 1:
        return _fail('duplicate-authority')
    if not matches:
        return _fail('unknown-authority')
    authority = matches[0]
    if authority.get('credentialId') in revoked:
        return _fail('revoked-authority')
    credential = {
        'credentialId': _show(authority.get('credentialId')),
        'alg': _show(authority.get('alg')),
        'publicKey': authority.get('publicKey') if isinstance(authority.get('publicKey'), dict) else {},
        'rpId': _show(authority.get('rpId')),
        'origins': [o for o in authority.get('origins', []) if isinstance(o, str)]
        if isinstance(authority.get('origins'), list) else [],
    }
    assertion = webauthn.verify_signing_assertion(signature, credential, ESTATE_CHALLENGE_TYPE, {'digest': digest})
    if not assertion['ok']:
        return _fail(assertion['problem'])

    issued, until = _time(record.get('issuedAt')), _time(record.get('validUntil'))
    if issued is None or until is None or until <= issued or until - issued > MAX_VALIDITY_SECONDS * 1000:
        return _fail('too-long-lived')
    if at_ms < issued - CLOCK_SKEW_SECONDS * 1000:
        return _fail('not-yet-valid')
    if at_ms > until:
        return _fail('expired')
    codes = list(dict.fromkeys(finding['code'] for finding in record_findings(record)))
    if codes:
        return _fail('rule-violation', codes=codes)
    return {'ok': True, 'digest': digest, 'revision': int(record['revision']), 'signedBy': credential['credentialId'],
            'signCount': assertion['signCount']}


def placement_trust(record: dict, machine_id: str, agent_ref: str, context: str = PERSONAL_CONTEXT) -> Optional[dict]:
    """The publisher keys a package placed on a machine in a context is verified under, or None when the record places
    no such agent there."""
    machine = next((m for m in _list(record.get('machines')) if m.get('id') == machine_id), None)
    placement = next((p for p in _list(machine.get('packages') if machine else None)
                      if p.get('agentRef') == agent_ref and _context_of(p) == context), None)
    if placement is None:
        return None
    return {'publisher': [k for k in _list(record.get('publisherKeys')) if k.get('kid') == placement.get('publisherKid')]}

# ---- The record's own rules ---------------------------------------------------------------------------------------


def _flagger(findings: list, code: str):
    return lambda path, message: findings.append({'code': code, 'instancePath': path, 'message': message})


def _revision_whole(value: dict, flag) -> None:
    """FAC-SEM-072 on one revision: its digest is its own, its window is positive and at most 30 days."""
    try:
        if estate_record_digest(value) != value.get('digest'):
            flag('/digest', 'the record changed after it was drafted, or was hashed over another form than RFC 8785')
    except jcs.CanonicalError:
        flag('/digest', 'the record has no RFC 8785 form, so it has no digest')
    issued, until = _time(value.get('issuedAt')), _time(value.get('validUntil'))
    if issued is None or until is None:
        flag('/issuedAt' if issued is None else '/validUntil',
             'issuedAt and validUntil are RFC 3339 date-times with an offset, on days that exist')
    elif until <= issued or until - issued > MAX_VALIDITY_SECONDS * 1000:
        flag('/validUntil', 'an estate record is valid after it is issued and for at most 30 days')


def revision_chain(value: dict) -> list:
    """FAC-SEM-072 across two revisions, `{parent, next}`."""
    findings: list = []
    flag = _flagger(findings, 'FAC-SEM-072')
    parent = value.get('parent') if isinstance(value.get('parent'), dict) else {}
    nxt = value.get('next') if isinstance(value.get('next'), dict) else {}
    if nxt.get('estate') != parent.get('estate'):
        flag('/next/estate', "a revision continues its parent's estate")
    if not _same_number(nxt.get('revision'), _to_number(parent.get('revision')) + 1):
        flag('/next/revision', 'a revision never skips or repeats one')
    try:
        parent_digest = estate_record_digest(parent)
    except jcs.CanonicalError:
        parent_digest = None
    if nxt.get('parent') != parent_digest:
        flag('/next/parent', 'parent names the digest of the revision before it')
    if _lt(_time(nxt.get('issuedAt')), _time(parent.get('issuedAt'))):
        flag('/next/issuedAt', 'a revision is never issued before its parent')
    kept = {_revocation_key(entry) for entry in _list(nxt.get('revocations'))}
    for entry, index in _indexed(parent.get('revocations')):
        if _revocation_key(entry) not in kept:
            flag(f'/parent/revocations/{index}', 'a revision keeps every revocation before it')
    return findings


def _valid_device_key(key: dict) -> bool:
    raw = _node_base64(_show(key.get('publicKey', '') if key.get('publicKey') is not None else ''))
    if key.get('alg') == 'ed25519':
        return len(raw) == 32 and not ed25519.is_small_order_key(raw)
    return p256.key_refusal(raw) is None


def _node_base64(text: str) -> bytes:
    """Base64 as Node's lenient decoder reads it: characters outside the alphabet skipped, padding optional."""
    import base64
    cleaned = re.sub(r'[^A-Za-z0-9+/]', '', text.replace('-', '+').replace('_', '/').split('=')[0])
    if len(cleaned) % 4 == 1:
        cleaned = cleaned[:-1]
    return base64.b64decode(cleaned + '=' * (-len(cleaned) % 4))


def _keys(value: dict, flag) -> None:
    """FAC-SEM-073: one `#sig` and at most one `#kem` per machine, each named by the machine and its role, valid, in one
    role only; machine ids and hub kids unique, hub keys of full order; passkeys valid and listed once."""
    ids, hub_kids, hub_keys = [], [], []
    for machine, m in _indexed(value.get('machines')):
        at = f'/machines/{m}'
        if machine.get('id') in ids:
            flag(f'{at}/id', 'a machine is listed twice')
        ids.append(machine.get('id'))
        device_keys = _list(machine.get('deviceKeys'))
        if sum(1 for k in device_keys if k.get('use') == 'sig') != 1:
            flag(f'{at}/deviceKeys', 'a machine has exactly one signing device key, <id>#sig')
        if sum(1 for k in device_keys if k.get('use') == 'kem') > 1:
            flag(f'{at}/deviceKeys', 'a machine has at most one key-agreement device key, <id>#kem')
        seen = []
        for key, k in _indexed(machine.get('deviceKeys')):
            if key.get('kid') != f"{_show(machine.get('id'))}#{_show(key.get('use'))}":
                flag(f'{at}/deviceKeys/{k}/kid', 'a device key is listed as <machine id>#sig or #kem, by its role')
            if not _valid_device_key(key):
                flag(f'{at}/deviceKeys/{k}/publicKey', 'a device key is valid for its algorithm')
            if key.get('publicKey') in seen:
                flag(f'{at}/deviceKeys/{k}/publicKey', 'one key is never used for the other role')
            seen.append(key.get('publicKey'))
        hub = machine.get('hubKey')
        if isinstance(hub, dict):
            if hub.get('kid') in hub_kids:
                flag(f'{at}/hubKey/kid', 'one kid per machine')
            hub_kids.append(hub.get('kid'))
            if hub.get('x') in hub_keys:
                flag(f'{at}/hubKey/x', 'one key per machine')
            hub_keys.append(hub.get('x'))
            if ed25519.is_small_order_key(webauthn.node_b64url(_show(hub.get('x') if hub.get('x') is not None else ''))):
                flag(f'{at}/hubKey/x', 'a hub key of small order verifies forged tokens and is never certified')
    credentials = []
    for authority, index in _indexed(value.get('authorities')):
        if authority.get('credentialId') in credentials:
            flag(f'/authorities/{index}/credentialId', 'a passkey is listed once')
        credentials.append(authority.get('credentialId'))
        if 'problem' in webauthn.credential_key(authority.get('publicKey'), _show(authority.get('alg'))):
            flag(f'/authorities/{index}/publicKey', "a passkey's public key is valid for its algorithm")


def _placements(value: dict, flag) -> None:
    """FAC-SEM-074: an agent placed once per machine and context, under a publisher key the record pins once."""
    kids = []
    for key, index in _indexed(value.get('publisherKeys')):
        if key.get('kid') in kids:
            flag(f'/publisherKeys/{index}/kid', 'a publisher key is pinned twice')
        kids.append(key.get('kid'))
    for machine, m in _indexed(value.get('machines')):
        agents = set()
        for placement, p in _indexed(machine.get('packages')):
            at = f'/machines/{m}/packages/{p}'
            key = _key_of(placement.get('agentRef'), _context_of(placement))
            if key in agents:
                flag(f'{at}/agentRef', 'placed twice on one machine in one context')
            agents.add(key)
            if placement.get('publisherKid') not in kids:
                flag(f'{at}/publisherKid', 'the publisher key is not in publisherKeys')


_JWT_SHAPE = re.compile(r'eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]*')
_SECRET_VALUE = re.compile(r'(^bearer\s.+|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}'
                           r'|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})', re.I)
_LONG_RUN = re.compile(r'[A-Za-z0-9+/=_-]{40,}')


def secret_value(value: str) -> bool:
    """The contract's credential detector (`src/secret-shape.ts` `secretValue`): a JWT anywhere, a provider key or
    `Bearer …`, or a run of 40+ base64 characters mixing upper case, lower case and digits."""
    long_secret = any(re.search('[a-z]', run) and re.search('[A-Z]', run) and re.search('[0-9]', run)
                      for run in _LONG_RUN.findall(value))
    return bool(_JWT_SHAPE.search(value) or _SECRET_VALUE.search(value) or long_secret)


def _secret_references(value: dict, flag) -> None:
    """FAC-SEM-075: a secret placement is a reference, fills a slot of an agent placed on that machine in its context,
    from a machine of this record (an organization's instance: from itself), once per agent, slot and context."""
    machine_ids = [m.get('id') for m in _list(value.get('machines'))]
    for machine, m in _indexed(value.get('machines')):
        placed = {_key_of(p.get('agentRef'), _context_of(p)) for p in _list(machine.get('packages'))}
        slots = set()
        for secret, s in _indexed(machine.get('secrets')):
            at = f'/machines/{m}/secrets/{s}'
            for field in ('slot', 'from', 'agentRef', 'context'):
                text = secret.get(field)
                if isinstance(text, str) and secret_value(text):
                    flag(f'{at}/{field}', 'a secret placement names a slot, never a value')
            if _key_of(secret.get('agentRef'), _context_of(secret)) not in placed:
                flag(f'{at}/agentRef', 'a secret goes only where a placed package declares it')
            if secret.get('from') not in machine_ids:
                flag(f'{at}/from', 'the slot is filled from a machine of this estate')
            elif _context_of(secret) != PERSONAL_CONTEXT and secret.get('from') != machine.get('id'):
                flag(f'{at}/from', "an organization's instance fills its slot from its own machine's vault")
            key = _key_of(secret.get('agentRef'), secret.get('slot'), _context_of(secret))
            if key in slots:
                flag(f'{at}/slot', 'a slot is placed twice for one agent in one context on one machine')
            slots.add(key)


def _grant_key(ident: Any) -> Any:
    return normal_identifier(ident) if isinstance(ident, str) else _key_of(ident)


def _placement_grant_problems(record: dict, placement: dict) -> list:
    """FAC-SEM-111 for one placement: an organization's instance names a share grant of this record, for the same
    context, not revoked; a personal one names none."""
    context = _context_of(placement)
    if context == PERSONAL_CONTEXT:
        return [] if 'grant' not in placement else [('/grant', 'a personal instance runs under no share grant')]
    if not isinstance(placement.get('grant'), str):
        return [('/grant', 'an instance placed in an organization context names its share grant')]
    ident = normal_identifier(placement['grant'])
    grants = [g for g in _list(record.get('shareGrants')) if isinstance(g.get('meta'), dict)
              and isinstance(g['meta'].get('id'), str) and normal_identifier(g['meta']['id']) == ident]
    problems = []
    if not grants:
        problems.append(('/grant', 'the record has no such share grant'))
    elif (parse_context(context) or {}).get('kind') != 'org':
        problems.append(('/context', "not an organization's context id (org:<orgId>)"))
    elif not any(g.get('context') == context for g in grants):
        problems.append(('/grant', "the share grant is not for the placement's context"))
    if any(e.get('kind') == 'grant' and isinstance(e.get('ref'), str) and normal_identifier(e['ref']) == ident
           for e in _list(record.get('revocations'))):
        problems.append(('/grant', 'the record revokes the share grant'))
    return problems


def _grants_and_revocations(value: dict, flag) -> None:
    """FAC-SEM-076: share grants are external and named once; a revocation is listed once; the record lists nothing it
    revokes (revocation outranks addition). Grant ids are compared in normal form."""
    grant_ids = []
    for grant, index in _indexed(value.get('shareGrants')):
        if grant.get('principalKind') != 'external':
            flag(f'/shareGrants/{index}/principalKind', 'a share grant is to a principal outside the estate')
        ident = grant['meta'].get('id') if isinstance(grant.get('meta'), dict) else None
        if _grant_key(ident) in grant_ids:
            flag(f'/shareGrants/{index}/meta/id', 'a grant is listed twice')
        grant_ids.append(_grant_key(ident))
    machines = _list(value.get('machines'))
    listed = {
        'authority': [e.get('credentialId') for e in _list(value.get('authorities'))],
        'machine': [m.get('id') for m in machines],
        'device-key': [k.get('kid') for m in machines for k in _list(m.get('deviceKeys'))],
        'hub-key': [m['hubKey'].get('kid') for m in machines if isinstance(m.get('hubKey'), dict)],
        'publisher-key': [k.get('kid') for k in _list(value.get('publisherKeys'))],
        'package': [p.get('digest') for m in machines for p in _list(m.get('packages'))],
        'grant': grant_ids,
    }
    seen = set()
    for revocation, index in _indexed(value.get('revocations')):
        ref = _grant_key(revocation.get('ref')) if revocation.get('kind') == 'grant' else revocation.get('ref')
        key = _show(revocation.get('kind')) + ' ' + _show(ref)
        if key in seen:
            flag(f'/revocations/{index}', 'revoked twice')
        seen.add(key)
        kind = revocation.get('kind')
        if isinstance(kind, str) and kind in listed and ref in listed[kind]:
            flag(f'/revocations/{index}/ref', 'the record still lists what it revokes: revocation outranks addition')


def record_findings(value: dict) -> list:
    """FAC-SEM-072…076 over one record, FAC-SEM-092 on each passkey, FAC-SEM-110 on each share grant and FAC-SEM-111 on
    each placement."""
    findings: list = []
    _revision_whole(value, _flagger(findings, 'FAC-SEM-072'))
    _keys(value, _flagger(findings, 'FAC-SEM-073'))
    _placements(value, _flagger(findings, 'FAC-SEM-074'))
    _secret_references(value, _flagger(findings, 'FAC-SEM-075'))
    _grants_and_revocations(value, _flagger(findings, 'FAC-SEM-076'))
    for grant, index in _indexed(value.get('shareGrants')):
        if grant.get('principalKind') == 'external' and grant.get('context') == PERSONAL_CONTEXT:
            findings.append({'code': 'FAC-SEM-110', 'instancePath': f'/shareGrants/{index}/context',
                             'message': "a grant to a principal outside the estate is never used in the personal context"})
    flag111 = _flagger(findings, 'FAC-SEM-111')
    for machine, m in _indexed(value.get('machines')):
        for placement, p in _indexed(machine.get('packages')):
            for suffix, message in _placement_grant_problems(value, placement):
                flag111(f'/machines/{m}/packages/{p}{suffix}', message)
    for authority, index in _indexed(value.get('authorities')):
        findings.extend(webauthn.signing_credential_findings(authority, f'/authorities/{index}'))
    return findings
