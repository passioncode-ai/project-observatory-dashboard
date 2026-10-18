"""`enroll-token/1` (DEC-0084, RM-21 P5): the one-time token with which a machine that has no screen joins an estate.

Fabric issues it under the person's signing credential, a passkey assertion over the register's `enroll-token/1`
statement (DEC-0063), for 15 minutes at most, for one use, purpose `agent-host`. The node reads it from stdin or a file
only its user can read (`read_token`), never from argv, a URL, the environment or a log, and presents it with its `#sig`
key and that key's proof (`join_proof`); the hub verifies it (`verify_enroll_token`), binds its nonce to that key's
fingerprint on the first use and refuses every later one (`redeem_enroll_token`). The token gives the machine no trust:
only a passkey-signed revision that lists the machine does. It authenticates the join, and its `genesis`, revision 1's
digest, authenticates the estate to the node.

A port of the contract's `src/enroll-token.ts`, run over its `enroll-token.json`; refusals come in the reference's order.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import sys
from typing import Any, BinaryIO, Mapping, Optional

import osprivacy

from . import ed25519, p256, webauthn
from .estate import _show, device_key_fingerprint
from .pairing import machine_of_keys
from .relay import CLOCK_SKEW_SECONDS, parse_json, strict_time

ENROLL_TOKEN_SCHEMA = 'enroll-token/1'
#: The register's type (DEC-0063): the statement the passkey's challenge is derived from.
ENROLL_TOKEN_TYPE = 'enroll-token/1'
ENROLL_PURPOSE = 'agent-host'
#: The register type of the joining machine's proof of its key: `{digest, nonce}`, signed by the key.
ENROLL_JOIN_TYPE = 'enroll-join/1'
#: DV-05: a token lives at most 15 minutes.
MAX_ENROLL_TOKEN_SECONDS = 900
#: A token file or stdin larger than this is no token (a real one is about 1.5 KB).
MAX_TOKEN_BYTES = 64 * 1024

_TOKEN_MEMBERS = ['schema', 'signature', 'statement']
#: `estate-record.schema.json#/$defs/assertion`'s members, sorted.
_ASSERTION_MEMBERS = ['alg', 'authenticatorData', 'clientDataJSON', 'credentialId', 'format', 'sig']
#: 64 bytes in their one canonical unpadded base64url form: 85 characters and a last one whose low 4 bits are zero.
_PROOF = re.compile(r'[A-Za-z0-9_-]{85}[AQgw]')


def _fail(problem: str) -> dict:
    return {'ok': False, 'problem': problem}


def _list(value: Any) -> list:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _is_one(value: Any) -> bool:
    """JSON's number 1 (1 and 1.0 are one number); a boolean is none."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == 1


def _statement_fields(token: Any) -> Optional[dict]:
    """The statement's fields beside `type`, or None when the token is not `{schema: "enroll-token/1", statement,
    signature}` whose statement has the register's `type` and exactly its fields, with values the register admits."""
    if not isinstance(token, dict) or token.get('schema') != ENROLL_TOKEN_SCHEMA:
        return None
    statement = token.get('statement')
    if not isinstance(statement, dict) or statement.get('type') != ENROLL_TOKEN_TYPE:
        return None
    # The token holds exactly the schema's members: nothing unsigned rides beside the statement (DEC-0084).
    if sorted(token) != _TOKEN_MEMBERS:
        return None
    fields = {name: value for name, value in statement.items() if name != 'type'}
    return fields if webauthn.signing_statement(ENROLL_TOKEN_TYPE, fields) is not None else None


def verify_enroll_token(token: Any, record: Any, genesis: Any, at_ms: Any) -> dict:
    """The contract's `verifyEnrollToken`, for the hub that admits a join and the node that pins the estate. `record` is
    the estate's current revision the verifier applied, `genesis` the digest of its revision 1, `at_ms` the clock in
    milliseconds. In this order, the first failure answering:

    1. `no-clock`: the clock is not a number;
    2. `malformed`: not an `enroll-token/1` of exactly `{schema, statement, signature}` with a register statement and a
       `webauthn-assertion` signature of exactly the schema's members whose `credentialId` is a string, or a record
       that is not an object with a string `estate`;
    3. `wrong-estate`: `scope` is not `estate:<the record's estate>`, or `genesis` is not the verifier's revision 1;
    4. the passkey among the record's `authorities`: `no-trusted-authority`, `revoked-authority` (the record revokes
       it), `duplicate-authority`, `unknown-authority`;
    5. the assertion over the register's `enroll-token/1` challenge (`webauthn.verify_signing_assertion`);
    6. the window: `too-long-lived` (`expiresAt` not after `notBefore`, or more than 900 s after it), `not-yet-valid`
       (the clock more than 60 s before `notBefore`), `expired` (after `expiresAt`).

    Accepted: `{ok, nonce, expiresAt, signedBy}`."""
    if isinstance(at_ms, bool) or not isinstance(at_ms, int):
        return _fail('no-clock')
    fields = _statement_fields(token)
    if fields is None or not isinstance(record, dict) or not isinstance(record.get('estate'), str):
        return _fail('malformed')
    signature = token.get('signature')
    if not isinstance(signature, dict) or sorted(signature) != _ASSERTION_MEMBERS \
            or signature.get('format') != 'webauthn-assertion' or not isinstance(signature.get('credentialId'), str):
        return _fail('malformed')
    if fields['scope'] != 'estate:' + record['estate'] or fields['genesis'] != genesis:
        return _fail('wrong-estate')

    authorities = _list(record.get('authorities'))
    if not authorities:
        return _fail('no-trusted-authority')
    if any(entry.get('kind') == 'authority' and entry.get('ref') == signature['credentialId']
           for entry in _list(record.get('revocations'))):
        return _fail('revoked-authority')
    matches = [entry for entry in authorities if entry.get('credentialId') == signature['credentialId']]
    if len(matches) > 1:
        return _fail('duplicate-authority')
    if not matches:
        return _fail('unknown-authority')
    authority = matches[0]
    credential = {
        'credentialId': signature['credentialId'],
        'alg': _show(authority.get('alg')),
        'publicKey': authority.get('publicKey') if isinstance(authority.get('publicKey'), dict) else {},
        'rpId': _show(authority.get('rpId')),
        'origins': [o for o in authority.get('origins') if isinstance(o, str)]
        if isinstance(authority.get('origins'), list) else [],
    }
    assertion = webauthn.verify_signing_assertion(signature, credential, ENROLL_TOKEN_TYPE, fields)
    if not assertion['ok']:
        return _fail(assertion['problem'])

    # The register admitted both instants, so neither is None here.
    not_before, expires_at = strict_time(fields['notBefore']), strict_time(fields['expiresAt'])
    if expires_at <= not_before or expires_at - not_before > MAX_ENROLL_TOKEN_SECONDS * 1000:
        return _fail('too-long-lived')
    if at_ms < not_before - CLOCK_SKEW_SECONDS * 1000:
        return _fail('not-yet-valid')
    if at_ms > expires_at:
        return _fail('expired')
    return {'ok': True, 'nonce': fields['nonce'], 'expiresAt': fields['expiresAt'],
            'signedBy': credential['credentialId']}


def enroll_join_input(token: Any) -> Optional[bytes]:
    """The bytes the joining machine signs with the `#sig` key it presents: the register's `enroll-join/1` statement,
    `{type, digest, nonce}`, where `digest` is `sha256:` and the hex SHA-256 of the token's statement bytes and `nonce`
    the token's. None for a token without a register statement."""
    fields = _statement_fields(token)
    statement = webauthn.signing_statement(ENROLL_TOKEN_TYPE, fields) if fields is not None else None
    if statement is None:
        return None
    return webauthn.key_statement(ENROLL_JOIN_TYPE, {'digest': 'sha256:' + hashlib.sha256(statement).hexdigest(),
                                                     'nonce': fields['nonce']})


def _proves(key: Mapping[str, Any], data: bytes, proof: Any) -> bool:
    """Whether `proof` is the presented key's signature over `data`: ES256 as 64 bytes r ‖ s, low-S (as relay-auth/1);
    Ed25519 as 64 bytes. Only the canonical spelling of the 64 bytes counts."""
    if not isinstance(proof, str) or _PROOF.fullmatch(proof) is None:
        return False
    octets = base64.b64decode(key['publicKey'])
    if key['alg'] == 'es256':
        return p256.verify_es256(octets, data, proof) is True
    raw = p256.b64url_canonical(proof)
    return raw is not None and ed25519.verify(octets, data, raw)


def redeem_enroll_token(token: Any, machine_key: Any, proof: Any, ledger: Any) -> dict:
    """The contract's `redeemEnrollToken`: the hub's single use of a token `verify_enroll_token` accepted. `machine_key`
    is the joining machine's `#sig` device key, `proof` its signature over `enroll_join_input(token)`, `ledger` the
    hub's map from nonce to entry (`{state: redeemed, fingerprint}` or `{state: revoked}`), kept per nonce at least
    until the token's `expiresAt` plus 60 s. In this order:

    - `malformed`: no register statement in the token, or no ledger map;
    - `bad-key`: `machine_key` is not one valid `<id>#sig` device key;
    - `bad-proof`: `proof` is not that key's signature over the join input;
    - `revoked`: Fabric revoked the token before its use;
    - `reused`: a second use, by any key, the same included, with `log` the line the hub records;
    - `ok`: the first use, with `record` what the hub stores BEFORE it answers. The ledger is the caller's: this
      function never writes it, so the hub stores `record` and answers in one step of its own."""
    data = enroll_join_input(token)
    if data is None or not isinstance(ledger, dict):
        return _fail('malformed')
    nonce = _statement_fields(token)['nonce']
    # One key alone is a machine's key list only when it is a valid `<id>#sig` key, so a `#kem` key is refused here too.
    if machine_of_keys([machine_key]) is None:
        return _fail('bad-key')
    if not _proves(machine_key, data, proof):
        return _fail('bad-proof')
    fingerprint = device_key_fingerprint(machine_key)
    entry = ledger.get(nonce)
    if isinstance(entry, dict) and entry.get('state') == 'revoked':
        return _fail('revoked')
    if nonce in ledger:
        bound = _show(entry.get('fingerprint')) if isinstance(entry, dict) else _show(entry)
        return {'ok': False, 'problem': 'reused', 'log': {'nonce': nonce, 'presented': fingerprint, 'boundTo': bound}}
    return {'ok': True, 'record': {'nonce': nonce, 'state': 'redeemed', 'fingerprint': fingerprint}}


def join_proof(keys: Any, token: Any) -> str:
    """This machine's proof for a join: its `#sig` key's ES256 signature (base64url, low-S) over the token's
    `enroll-join/1` statement. `keys` is the machine's `DeviceKeys`."""
    data = enroll_join_input(token)
    if data is None:
        raise ValueError('not an enroll-token/1 with a register statement')
    return keys.sign(data)


def read_token(source: str, stdin: Optional[BinaryIO] = None) -> dict:
    """The enroll token from `-` (stdin) or a file only this user can read: a regular file owned by this user that nobody
    else may read (`osprivacy.private`), not a symlink, checked on the open descriptor. Never from argv, a URL or the environment: `source` names
    where the token is, it is never the token, so text that looks like one is refused, as is a URL or an `env:` name.
    The token is parsed strictly (`relay.parse_json`); ValueError says what is wrong without echoing the token."""
    if not isinstance(source, str) or not source:
        raise ValueError('name the token file, or - for stdin')
    if source != '-' and (source.lstrip()[:1] in ('{', '[', '"') or '://' in source
                          or source.lower().startswith(('env:', '$'))):
        raise ValueError('the token is read from stdin or a file only you can read, never from the command line, '
                         'a URL or the environment')
    if source == '-':
        stream = stdin if stdin is not None else sys.stdin.buffer
        raw = stream.read(MAX_TOKEN_BYTES + 1)
    else:
        try:
            fd = osprivacy.open(source, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        except OSError as error:
            raise ValueError('the token file cannot be opened (%s); a symlink is refused' % error.strerror) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('the token file is not a regular file')
            if not osprivacy.owned_by_me(fd):
                raise ValueError('the token file belongs to another user')
            if not osprivacy.private(fd):
                raise ValueError('the token file may be read by others: make it yours alone (%s)'
                                 % osprivacy.describe(fd))
            chunks, size = [], 0
            while size <= MAX_TOKEN_BYTES:
                chunk = os.read(fd, MAX_TOKEN_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b''.join(chunks)
        finally:
            os.close(fd)
    if len(raw) > MAX_TOKEN_BYTES:
        raise ValueError('more than %d bytes: no enroll token is that long' % MAX_TOKEN_BYTES)
    try:
        token = parse_json(raw.strip())
    except ValueError:
        raise ValueError('not JSON: an enroll token is the JSON Fabric gave you') from None
    if _statement_fields(token) is None:
        raise ValueError('not an enroll-token/1 with a register statement')
    return token


def token_findings(value: Any) -> list:
    """FAC-SEM-113 on one `enroll-token/1` (kind `enroll-token`): its statement is the register's, `type`
    `enroll-token/1` and exactly its fields, `purpose` `agent-host`, `uses` 1, `scope` `estate:<id>`, `genesis` a
    digest, a 32-byte `nonce`, `ceiling` null or a digest, and its window is RFC 3339 read strictly, positive and at
    most 900 s."""
    findings: list = []

    def flag(path: str, message: str) -> None:
        findings.append({'code': 'FAC-SEM-113', 'instancePath': path, 'message': message})

    statement = value.get('statement') if isinstance(value, dict) else None
    if not isinstance(statement, dict):
        flag('/statement', 'an enroll token carries its signed statement')
        return findings
    if statement.get('type') != ENROLL_TOKEN_TYPE:
        flag('/statement/type', "the statement's type is enroll-token/1, the register's (DEC-0063)")
    fields = webauthn.ENROLL_TOKEN_FIELDS
    extra = [name for name in statement if name != 'type' and name not in fields]
    missing = [name for name in fields if name not in statement]
    if extra or missing:
        flag('/statement', "the statement has exactly the register's fields: " + ', '.join(fields))
    if statement.get('purpose') != ENROLL_PURPOSE:
        flag('/statement/purpose', "an enroll token's purpose is agent-host")
    if not _is_one(statement.get('uses')):
        flag('/statement/uses', 'an enroll token is used once: uses is 1, and the hub binds its nonce on the first use')
    not_before, expires_at = strict_time(statement.get('notBefore')), strict_time(statement.get('expiresAt'))
    rest = {name: v for name, v in statement.items() if name != 'type'}
    # Every other member the register checks, reported once: the clauses above and below name their own members.
    named = not extra and not missing and statement.get('purpose') == ENROLL_PURPOSE and _is_one(statement.get('uses')) \
        and not_before is not None and expires_at is not None
    if named and webauthn.signing_statement(ENROLL_TOKEN_TYPE, rest) is None:
        flag('/statement', 'scope is estate:<id>, genesis a sha256: digest, nonce 32 bytes in base64url, and ceiling '
                           'null or a digest')
    if not_before is None or expires_at is None:
        flag('/statement/expiresAt' if not_before is not None else '/statement/notBefore',
             'notBefore and expiresAt are RFC 3339 date-times with an offset, on days that exist')
    elif expires_at <= not_before or expires_at - not_before > MAX_ENROLL_TOKEN_SECONDS * 1000:
        flag('/statement/expiresAt', 'an enroll token is valid after notBefore and for at most 15 minutes (900 s)')
    return findings


def enroll_token_rules(kind: str, value: Any) -> Optional[list]:
    """The rule set of kind `enroll-token` (FAC-SEM-113), None for any other kind."""
    return token_findings(value) if kind == 'enroll-token' else None
