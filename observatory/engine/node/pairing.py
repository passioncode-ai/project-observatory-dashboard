"""The estate ceremonies' pure parts (contract `src/pairing.ts`, DEC-0084): how a new machine pairs with a trusted one.

Both sides commit, then both reveal (the hash commitment of ZRTP's short authentication string, RFC 6189 §4.4.1.1,
§7), so an attacker in the middle gets one guess at a six-digit code per rendezvous. Also: the one-time rendezvous,
read the same way by the relay and both sides; the estate fingerprint a person compares; and the checks a node runs
on the revision that adds or re-keys it (`join_listed`, `own_keys_decision`). Every function is total: malformed
input answers a refusal, never an exception. Shared vectors: `fabric/node-vectors/pairing.json`.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from . import jcs
from .estate import _valid_device_key, device_key_fingerprint, device_key_ref
from .relay import CLOCK_SKEW_SECONDS, strict_time

PAIRING_SCHEMA = 'pairing-message/1'
COMMIT_TYPE = 'fabric-enroll-commit/1'
CODE_TYPE = 'fabric-enroll-code/1'
FINGERPRINT_TYPE = 'estate-fingerprint/1'
CODE_DIGITS = 6
FINGERPRINT_GROUPS = 5
FINGERPRINT_GROUP_DIGITS = 5
RENDEZVOUS_SECONDS = 600
PAIRING_ROLES = ('new', 'trusted')

_NONCE = re.compile(r'[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]')
_MACHINE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}')
_SHA256 = re.compile(r'sha256:[a-f0-9]{64}')
_KEY_MEMBERS = ['alg', 'kid', 'publicKey', 'use']
_ALGORITHMS = {'sig': ('es256', 'ed25519'), 'kem': ('ecdh-p256',)}


def _sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode('utf-8')).digest()


def _uint40(digest: bytes, offset: int) -> int:
    return int.from_bytes(digest[offset:offset + 5], 'big')


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_role(value: Any) -> bool:
    return isinstance(value, str) and value in PAIRING_ROLES


def _full(pattern: 're.Pattern', value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def json_equal(a: Any, b: Any) -> bool:
    """Structural equality of JSON values as the reference reads it: object member order does not matter, and a
    boolean is never a number (Python's `True == 1` is not JSON's)."""
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(map(json_equal, a, b))
    if isinstance(a, dict) or isinstance(b, dict):
        return isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys() and \
            all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def machine_of_keys(keys: Any) -> Optional[str]:
    """The machine a key list belongs to, or None when it is not one machine's device keys as an estate record lists
    them: one or two keys, each exactly `{kid, use, alg, publicKey}`, one `sig` and at most one `kem`, kids
    `<id>#<use>` of one id, valid keys in canonical padded base64, no key in both roles."""
    if not isinstance(keys, list) or not 1 <= len(keys) <= 2:
        return None
    ids, uses, octets = set(), set(), set()
    for key in keys:
        if not isinstance(key, dict) or sorted(key) != _KEY_MEMBERS:
            return None
        use, kid, alg = key['use'], key['kid'], key['alg']
        if not (_is_str(use) and _is_str(kid) and _is_str(alg)) or alg not in _ALGORITHMS.get(use, ()):
            return None
        machine = kid[:-(len(use) + 1)] if kid.endswith('#' + use) else ''
        if not _full(_MACHINE_ID, machine) or use in uses or device_key_fingerprint(key) is None \
                or not _valid_device_key(key) or key['publicKey'] in octets:
            return None
        ids.add(machine)
        uses.add(use)
        octets.add(key['publicKey'])
    return next(iter(ids)) if len(ids) == 1 and 'sig' in uses else None


def _side_problem(side: Any) -> Optional[str]:
    """A side's members checked in the reference's order: role, nonce, keys, genesis."""
    if not isinstance(side, dict):
        return 'malformed'
    role = side.get('role')
    if not _is_role(role):
        return 'bad-role'
    if not _full(_NONCE, side.get('nonce')):
        return 'bad-nonce'
    if machine_of_keys(side.get('keys')) is None:
        return 'bad-keys'
    genesis = side.get('genesis')
    if (not _full(_SHA256, genesis)) if role == 'trusted' else ('genesis' in side):
        return 'bad-genesis'
    return None


def _covered(side: dict) -> dict:
    covered = {'keys': side['keys'], 'nonce': side['nonce']}
    if side['role'] == 'trusted':
        covered['genesis'] = side['genesis']
    return covered


def _commit_input(side: dict) -> str:
    return jcs.rfc8785(dict({'type': COMMIT_TYPE, 'role': side['role']}, **_covered(side)))


def pairing_commit(side: Any) -> dict:
    """`{'ok': True, 'commit': 'sha256:...'}` over `{type: fabric-enroll-commit/1, role, keys, nonce[, genesis]}`, or
    the side's first problem. The role and the keys are inside, so a side never swaps either after it saw the other."""
    problem = _side_problem(side)
    if problem:
        return {'ok': False, 'problem': problem}
    try:
        return {'ok': True, 'commit': 'sha256:' + _sha256(_commit_input(side)).hex()}
    except jcs.CanonicalError:
        return {'ok': False, 'problem': 'malformed'}


def _message(value: Any, step: str) -> Optional[dict]:
    if not isinstance(value, dict) or value.get('schema') != PAIRING_SCHEMA or value.get('step') != step:
        return None
    members = ','.join(sorted(value))
    if step == 'commit':
        return value if members == 'commit,role,schema,step' and _full(_SHA256, value.get('commit')) else None
    return value if members in ('keys,nonce,role,schema,step', 'genesis,keys,nonce,role,schema,step') else None


def check_reveal(commit: Any, reveal: Any, expected: Optional[str] = None) -> dict:
    """The other side's reveal against its commitment: `{'ok': True, 'machine': id}` or `malformed`,
    `role-mismatch`, a side problem, or `commit-mismatch` (the side then aborts)."""
    c, r = _message(commit, 'commit'), _message(reveal, 'reveal')
    if c is None or r is None:
        return {'ok': False, 'problem': 'malformed'}
    if r['role'] != c['role'] or (expected is not None and r['role'] != expected):
        return {'ok': False, 'problem': 'role-mismatch'}
    side = {k: v for k, v in r.items() if k not in ('schema', 'step')}
    made = pairing_commit(side)
    if not made['ok']:
        return made
    if made['commit'] != c['commit']:
        return {'ok': False, 'problem': 'commit-mismatch'}
    return {'ok': True, 'machine': machine_of_keys(r['keys'])}


def comparison_code(new_reveal: Any, trusted_reveal: Any) -> dict:
    """The six digits both screens show. Call it only through `checked_comparison_code`."""
    a, b = _message(new_reveal, 'reveal'), _message(trusted_reveal, 'reveal')
    if a is None or b is None:
        return {'ok': False, 'problem': 'malformed'}
    if a['role'] != 'new' or b['role'] != 'trusted':
        return {'ok': False, 'problem': 'role-mismatch' if _is_role(a['role']) and _is_role(b['role']) else 'bad-role'}
    problem = _side_problem(a) or _side_problem(b)
    if problem:
        return {'ok': False, 'problem': problem}
    if machine_of_keys(a['keys']) == machine_of_keys(b['keys']):
        return {'ok': False, 'problem': 'same-machine'}
    transcript = jcs.rfc8785({'type': CODE_TYPE, 'new': _covered(a), 'trusted': _covered(b)})
    value = _uint40(_sha256(transcript), 0) % 10 ** CODE_DIGITS
    return {'ok': True, 'code': str(value).zfill(CODE_DIGITS)}


def checked_comparison_code(new_commit: Any, new_reveal: Any, trusted_commit: Any, trusted_reveal: Any) -> dict:
    """The comparison code only after each reveal opened its own commitment: a code over unchecked reveals lets a
    side pick its nonce after it saw the other's and loses the one-guess bound."""
    for commit, reveal, role in ((new_commit, new_reveal, 'new'), (trusted_commit, trusted_reveal, 'trusted')):
        opened = check_reveal(commit, reveal, role)
        if not opened['ok']:
            return opened
    return comparison_code(new_reveal, trusted_reveal)


def estate_fingerprint(digest: Any) -> dict:
    """Five groups of five digits from revision 1's digest: what a person compares to know two screens show one
    network. 10^25 values: grinding another estate's revision 1 to match costs about 2^82 hashes."""
    if not _full(_SHA256, digest):
        return {'ok': False, 'problem': 'malformed'}
    f = _sha256(jcs.rfc8785({'type': FINGERPRINT_TYPE, 'digest': digest}))
    groups = [str(_uint40(f, 5 * i) % 10 ** FINGERPRINT_GROUP_DIGITS).zfill(FINGERPRINT_GROUP_DIGITS)
              for i in range(FINGERPRINT_GROUPS)]
    return {'ok': True, 'fingerprint': ' '.join(groups)}


def rendezvous_verdict(opened_at: Any, messages: Any, at_ms: Optional[int]) -> dict:
    """A rendezvous after the messages it carried, oldest first, at `at_ms`: `open`, `complete`, or burned for the
    first of malformed, expired (past 600 s, or more than 60 s before opening), repeated, out-of-order,
    commit-mismatch, aborted (also the one abort allowed after both reveals) and closed."""
    opened = strict_time(opened_at)
    if opened is None or not isinstance(at_ms, int) or isinstance(at_ms, bool) or not isinstance(messages, list):
        return {'state': 'burned', 'reason': 'malformed', 'index': -1}
    if at_ms > opened + RENDEZVOUS_SECONDS * 1000 or at_ms < opened - CLOCK_SKEW_SECONDS * 1000:
        return {'state': 'burned', 'reason': 'expired', 'index': -1}
    commits: dict = {}
    revealed: set = set()
    for index, item in enumerate(messages):
        def burn(reason: str) -> dict:
            return {'state': 'burned', 'reason': reason, 'index': index}
        if isinstance(item, dict) and item.get('schema') == PAIRING_SCHEMA and item.get('step') == 'abort' \
                and sorted(item) == ['role', 'schema', 'step'] and _is_role(item['role']):
            return burn('aborted')
        if len(revealed) == 2:
            return burn('closed')
        commit, reveal = _message(item, 'commit'), _message(item, 'reveal')
        role = (commit or reveal or {}).get('role')
        if not _is_role(role):
            return burn('malformed')
        if commit is not None:
            if role in commits:
                return burn('repeated')
            commits[role] = commit
            continue
        if role in revealed:
            return burn('repeated')
        if len(commits) < 2:
            return burn('out-of-order')
        opened_reveal = check_reveal(commits[role], reveal, role)
        if not opened_reveal['ok']:
            return burn('commit-mismatch' if opened_reveal['problem'] == 'commit-mismatch' else 'malformed')
        revealed.add(role)
    return {'state': 'complete'} if len(revealed) == 2 else {'state': 'open'}


def _machine(record: dict, machine_id: Any) -> Optional[dict]:
    machines = record.get('machines')
    if not isinstance(machines, list):
        return None
    return next((m for m in machines if isinstance(m, dict) and m.get('id') == machine_id), None)


def _revoked_machine(record: dict, machine_id: Any) -> bool:
    entries = record.get('revocations')
    return isinstance(entries, list) and any(isinstance(e, dict) and e.get('kind') == 'machine'
                                             and e.get('ref') == machine_id for e in entries)


def _same_keys(a: list, b: list) -> bool:
    return len(a) == len(b) and all(any(json_equal(key, other) for other in b) for key in a)


def join_listed(record: Any, new_reveal: Any, trusted_reveal: Any, genesis: Any) -> dict:
    """What the new machine checks before it pins and applies (after the chain verified): the revision 1 it verified
    is the `genesis` the trusted side revealed (`other-estate`), the record lists the new machine with exactly its
    revealed keys (`not-listed`, `other-keys`) and the trusted machine with its revealed `#sig` key
    (`trusted-not-listed`), revoking neither."""
    a, b = _message(new_reveal, 'reveal'), _message(trusted_reveal, 'reveal')
    if not isinstance(record, dict) or a is None or b is None or a['role'] != 'new' or b['role'] != 'trusted' \
            or _side_problem(a) or _side_problem(b):
        return {'ok': False, 'problem': 'malformed'}
    if b['genesis'] != genesis:
        return {'ok': False, 'problem': 'other-estate'}
    new_id, trusted_id = machine_of_keys(a['keys']), machine_of_keys(b['keys'])
    machine = _machine(record, new_id)
    if machine is None or _revoked_machine(record, new_id):
        return {'ok': False, 'problem': 'not-listed'}
    if not isinstance(machine.get('deviceKeys'), list) or not _same_keys(machine['deviceKeys'], a['keys']):
        return {'ok': False, 'problem': 'other-keys'}
    trusted = _machine(record, trusted_id)
    sig = next(key for key in b['keys'] if key['use'] == 'sig')
    if trusted is None or _revoked_machine(record, trusted_id) or not isinstance(trusted.get('deviceKeys'), list) \
            or not any(json_equal(key, sig) for key in trusted['deviceKeys']):
        return {'ok': False, 'problem': 'trusted-not-listed'}
    return {'ok': True}


def own_keys_decision(record: Any, machine_id: Any, held: Any) -> dict:
    """What a node does with a verified revision for its own keys: `apply` (with `retire`, the key references of held
    keys no longer listed, which the node destroys), `stop` `removed` or `keys-replaced`, or `malformed`."""
    if not isinstance(record, dict) or not isinstance(machine_id, str) or not isinstance(held, list):
        return {'action': 'malformed'}
    machine = _machine(record, machine_id)
    if machine is None or _revoked_machine(record, machine_id):
        return {'action': 'stop', 'reason': 'removed'}
    listed = machine.get('deviceKeys') if isinstance(machine.get('deviceKeys'), list) else []
    if not listed or not all(any(json_equal(key, mine) for mine in held) for key in listed):
        return {'action': 'stop', 'reason': 'keys-replaced'}
    retire = [device_key_ref(key) for key in held if not any(json_equal(key, other) for other in listed)]
    return {'action': 'apply', 'retire': [ref for ref in retire if isinstance(ref, str)]}
