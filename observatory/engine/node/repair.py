"""The repair runner: an organization's `repair-request/1`, run only as the contract's `repairDecision` answers, the
person told first, every request and run audited, answered once with `repair-result/1` (contract DEC-0101, corrected by
DEC-0102, gated by DEC-0117; FAC-SEM-068, FAC-SEM-129; engine OBS-193).

An administrator asks one enrolled machine to run one package, by `agentRef`, version and digest, signed like a policy
layer by a key of the organization's current policy key set and carried in a check-in response's `records.repairs`.
What this module keeps, and why:

- **The decision is the contract's.** `repair_decision` ports `repairDecision` (`src/node-summary.ts` at the engine's
  pin) and is held to the contract's shared vectors (`fabric/node-vectors/repair/repair-decision.json`): in order,
  `already-answered`, `bad-signature` (no key of the current set with that `keyId` and algorithm, inside its window at
  that moment by `relay.key_window_holds`, verifies the settings canonical form of the request without its signature;
  a request with no canonical form is refused here), `wrong-device`, `expired` (outside `issuedAt` <= now < `expiresAt`,
  or a window longer than 7 days), `policy-expired` (remote execution stops with the policy, exactly FAC-SEM-068's
  answer, `remote_execution_allowed`), `not-allowlisted` (`repair.packages` from the signed layers only) and
  `person-declined` (the person was not told before it started, or declined; a machine with role `org-cloud` binds no
  person, so the organization's own signed request is its notice).
- **No shell, no command.** A package that passes runs only as a local action this machine's owner allow-listed for
  its `agentRef` in the owner-only `config/node-repair.json`: `restart` or `ensure_running` of a target enrolled with
  `node.lifecycle` (OBS-79), asked of the lifecycle broker through its own door with an access binding the operator
  issued (`LifecycleExecutor`). Nothing in a request becomes an executable, a path, an argument or an environment. A
  package the organization allows but this machine has no action for ends `failed` (`run-failed`) before the person is
  told, because nothing would run.
- **The person first.** A request that passes everything but the person step is shown at once (`org status`, Health,
  doctor: the task line, the package and when it runs) and runs no earlier than `NOTICE_SECONDS` later, unless the
  person declines (`full org repair --org ID --decline REPAIR_ID`). The decision is taken again at the moment it would
  start, with the time the person was told, so a request that expired or a policy that expired meanwhile is refused.
- **Once per id, every time audited.** One row per request id in the enrollment's owner-only outbox database
  (`repairs`): the request, when it was received and the person told, the lifecycle operation, and the one stored
  `repair-result/1`, which a redelivered request gets again unchanged, never a second run. Each step writes one
  secret-free journal line (codes, ids, the package; never the bearer, the signature or a body).

Every decision function answers and never raises on malformed input: malformed input is an answer, as in the contract.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ed25519, p256
from .estate import _MISSING, _member, _node_base64, _show
from .node_summary import signed_policy_value
from .relay import key_window_holds, rfc3339, strict_time

TYPE = 'repair-request/1'
RESULT_TYPE = 'repair-result/1'
#: The longest a repair request lives (FAC-SEM-129).
MAX_REPAIR_DAYS = 7
DAY_MS = 86_400_000
#: `repairDecision`'s reasons, in its order.
REASONS = ('already-answered', 'bad-signature', 'wrong-device', 'expired', 'policy-expired', 'not-allowlisted',
           'person-declined')
#: What a `repair-result/1` may carry: every refusal but `already-answered` (never sent, DEC-0102), and `run-failed`.
RESULT_REASONS = ('bad-signature', 'wrong-device', 'expired', 'policy-expired', 'not-allowlisted', 'person-declined',
                  'run-failed')
#: How long a person sees a repair before it may start. The maintenance job runs every 15 minutes, so the notice is
#: up for at least one pass of Health and `org status` before anything runs.
NOTICE_SECONDS = 15 * 60
#: A lifecycle operation that has not ended this long after it started is answered `failed` (`run-failed`).
RUN_TIMEOUT_SECONDS = 30 * 60
#: The most results one check-in carries (`device-check-in.schema.json`, `records.repairResults`) and the most requests
#: one response may carry (`records.repairs`).
MAX_RESULTS_PER_CHECK_IN = 64
MAX_REQUESTS_PER_RESPONSE = 16
#: How deep and how large a request may be before it is read at all, and how many repairs may wait and how many rows
#: the book keeps (a request the schema admits is a few hundred bytes and four levels deep). Past the book's bounds a
#: new request is not recorded: the server sends an unanswered one again.
MAX_DEPTH = 8
MAX_BODY_BYTES = 64 * 1024
MAX_PENDING = 64
MAX_BOOK_ROWS = 10000
#: How long past its request's own `expiresAt` an answered id is still remembered (a clock that moved back a day).
PRUNE_MARGIN_SECONDS = 86400
#: The local actions a repair may run, each one `node.lifecycle` tool. Never `stop`: a repair brings a service back.
ACTIONS = {'restart': 'lifecycle.restart', 'ensure_running': 'lifecycle.ensure_running'}
CONFIG_NAME = 'node-repair.json'
CONFIG_MAX_BYTES = 256 * 1024
#: Schema shapes (`activity-common`, `common`, `fabric-package`, `repair-request.schema.json`).
OPAQUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z')
AGENT_REF = re.compile(r'urn:fabric:agent:[a-z][a-z0-9]*(?:-[a-z0-9]+)*:[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')
SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z')
SHA256 = re.compile(r'sha256:[a-f0-9]{64}\Z')
SIGNATURE_VALUE = re.compile(r'[A-Za-z0-9+/]{86}==\Z')
TASK_FORBIDDEN = re.compile('[\u0000-\u001f  ]')
#: `node.lifecycle`'s id shape (`broker.ID`): a target id and a request id.
LIFECYCLE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}\Z')
SLOT = re.compile(r'[^/\s]+/[^/\s]+/[^/\s]+\Z')
#: The lifecycle operation states that mean the target is up.
LIFECYCLE_SUCCESS = frozenset(('ready', 'already_ready'))


# ---- the settings canonical form and a signing key ---------------------------------------------------------------

class CanonicalError(ValueError):
    """A value with no settings canonical form: a fraction, a non-finite number, an integer outside +-(2^53 - 1), a
    lone surrogate, or something JSON cannot carry."""


def _settings_string(text: str) -> str:
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise CanonicalError('a lone surrogate has no canonical form')
    return json.dumps(text, ensure_ascii=False)


def settings_canonical_json(value: Any) -> str:
    """The contract's settings form (`settingsCanonicalJson`, DEC-0025, DEC-0043): keys sorted by code point at every
    level, `", "` and `": "`, strings escaped as `JSON.stringify` escapes them, integers in decimal. A float that is a
    whole number is the integer JSON's reader in JavaScript sees (`1.0` reads as `1`); every other number refuses."""
    if value is None:
        return 'null'
    if value is True or value is False:
        return 'true' if value else 'false'
    if isinstance(value, int):
        if abs(value) > 2 ** 53 - 1:
            raise CanonicalError('an integer outside +-(2^53 - 1) has no canonical form')
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or abs(value) > 2 ** 53 - 1:
            raise CanonicalError('a fraction has no canonical form')
        return str(int(value))
    if isinstance(value, str):
        return _settings_string(value)
    if isinstance(value, list):
        return '[' + ', '.join(settings_canonical_json(item) for item in value) + ']'
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalError('an object key is a string')
        return '{' + ', '.join('%s: %s' % (_settings_string(key), settings_canonical_json(value[key]))
                               for key in sorted(value)) + '}'
    raise CanonicalError('%s is not a JSON value' % type(value).__name__)


def policy_signing_input(document: Any) -> bytes:
    """The bytes a policy-style signature covers (`policySigningInput`): the settings form of the document without
    `signature`, UTF-8. Raises CanonicalError when it has none."""
    doc = document if isinstance(document, dict) else {}
    return settings_canonical_json({k: v for k, v in doc.items() if k != 'signature'}).encode('utf-8')


def verify_with(key: Any, data: bytes, signature: Any) -> bool:
    """`verifyWith`: whether a base64 signature over `data` verifies under `key` (`{alg?, publicKey}`): `ed25519` (the
    default) with a raw 32-byte key of full order, or `es256` with a raw 65-byte P-256 point SEC 1 accepts and a 64-byte
    R || S signature. Base64 is read as Node reads it. False for anything else."""
    if not isinstance(key, dict) or not isinstance(key.get('publicKey'), str) or not isinstance(signature, str):
        return False
    raw, value = _node_base64(key['publicKey']), _node_base64(signature)
    alg = key.get('alg')
    alg = 'ed25519' if alg is None else alg
    try:
        if alg == 'ed25519':
            return len(raw) == 32 and not ed25519.is_small_order_key(raw) and ed25519.verify(raw, data, value)
        if alg == 'es256':
            if p256.key_refusal(raw) is not None or len(value) != 64:
                return False
            hashes, ec, _, encode_dss_signature, invalid = p256._crypto()
            public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
            der = encode_dss_signature(int.from_bytes(value[:32], 'big'), int.from_bytes(value[32:], 'big'))
            try:
                public.verify(der, data, ec.ECDSA(hashes.SHA256()))
            except invalid:
                return False
            return True
    except (ValueError, TypeError):
        return False
    return False


# ---- FAC-SEM-068 and FAC-SEM-129 ----------------------------------------------------------------------------------

def _list(value: Any) -> List[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def policy_expiry(layers: Any) -> Optional[float]:
    """The earliest `expiresAt` of the signed layers in milliseconds (`policyExpiry`), None when none expires. A bound
    that does not read as an RFC 3339 instant is read as already past: the contract's `Date.parse` would make it never
    expire, and remote execution is the action a doubt must stop (the schema admits no such layer)."""
    times = []
    for layer in _list(layers):
        if layer.get('source') == 'user' or 'expiresAt' not in layer:
            continue
        ms = strict_time(layer['expiresAt'])
        times.append(float('-inf') if ms is None else ms)
    return min(times) if times else None


def remote_execution_allowed(layers: Any, at_ms: Any) -> bool:
    """FAC-SEM-068 for `remote-execution` (`expectedDecision`): allowed unless the policy expired, `at` after its
    earliest `expiresAt`. A policy with no `expiresAt` does not expire (DEC-0102). A clock that does not read refuses."""
    if isinstance(at_ms, bool) or not isinstance(at_ms, (int, float)) or not math.isfinite(at_ms):
        return False
    expiry = policy_expiry(layers)
    return expiry is None or not at_ms > expiry


def _same(a: Any, b: Any) -> bool:
    """JavaScript's `===` on two JSON values: `undefined` (absent) equals only itself, objects only themselves."""
    if a is _MISSING or b is _MISSING:
        return a is b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _time(value: Any) -> Optional[int]:
    return strict_time(value) if isinstance(value, str) else None


def _refuse(reason: str) -> dict:
    return {'decision': 'refuse', 'reason': reason}


def repair_decision(value: Any) -> dict:
    """`repairDecision` (FAC-SEM-129) over `{request, keys, device: {id, org, role?}, layers, at, notifiedAt?, declined?,
    answered?}`: `{decision: run}` or `{decision: refuse, reason}`, the reasons in the contract's order. Times read
    strictly as RFC 3339; a request or clock that does not read is refused, never raised."""
    value = value if isinstance(value, dict) else {}
    request = value.get('request') if isinstance(value.get('request'), dict) else {}
    if value.get('answered') is True:                          # DEC-0102: an id runs at most once
        return _refuse('already-answered')
    signature = request.get('signature') if isinstance(request.get('signature'), dict) else {}
    at = _time(value.get('at'))                                # DEC-0115: the clock read as the key bounds are
    key = next((k for k in _list(value.get('keys'))
                if _same(_member(k, 'keyId'), _member(signature, 'keyId'))
                and _same('ed25519' if k.get('alg') is None else k['alg'], _member(signature, 'alg'))), None)
    try:
        signed: Optional[bytes] = policy_signing_input(request)
    except CanonicalError:                                     # no canonical form: refused, never raised (DEC-0102)
        signed = None
    if key is None or signed is None or at is None or not key_window_holds(at, key) \
            or not verify_with(key, signed, signature.get('value')):
        return _refuse('bad-signature')
    device = value.get('device') if isinstance(value.get('device'), dict) else {}
    asked_device = request.get('device') if isinstance(request.get('device'), dict) else {}
    asked_org = request.get('org') if isinstance(request.get('org'), dict) else {}
    if _show(_member(asked_device, 'id')) != _show(_member(device, 'id')) \
            or _show(_member(asked_org, 'id')) != _show(_member(device, 'org')):
        return _refuse('wrong-device')
    start, end = _time(request.get('issuedAt')), _time(request.get('expiresAt'))
    if start is None or end is None or not (start <= at < end and end - start <= MAX_REPAIR_DAYS * DAY_MS):
        return _refuse('expired')
    layers = _list(value.get('layers'))
    if not remote_execution_allowed(layers, at):               # DEC-0102: exactly FAC-SEM-068's remote execution
        return _refuse('policy-expired')
    allowed = signed_policy_value(layers, 'repair.packages')   # DEC-0102: from the signed layers only
    package = request.get('package') if isinstance(request.get('package'), dict) else {}
    if not isinstance(allowed, list) or not any(
            _same(_member(entry, 'agentRef'), _member(package, 'agentRef'))
            and _same(_member(entry, 'digest'), _member(package, 'digest')) for entry in _list(allowed)):
        return _refuse('not-allowlisted')
    notified = _time(value.get('notifiedAt'))
    if device.get('role') != 'org-cloud' and (value.get('declined') is True or notified is None or notified > at):
        return _refuse('person-declined')                      # DEC-0102: org-cloud's notice is the signed request
    return {'decision': 'run'}


# ---- the documents' shapes ---------------------------------------------------------------------------------------

def _closed(doc: Any, required: Tuple[str, ...], allowed: Tuple[str, ...], where: str, out: List[str]) -> bool:
    if not isinstance(doc, dict):
        out.append('%s is not an object' % where)
        return False
    out.extend('%s/%s is missing' % (where, name) for name in required if name not in doc)
    out.extend('%s/%s is not allowed' % (where, name) for name in doc if name not in allowed)
    return True


def _match(pattern: 're.Pattern', value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def body_problem(doc: Any) -> Optional[str]:
    """Why a document cannot even be read safely, walked without recursion: `too-deep` past `MAX_DEPTH` levels,
    `too-large` past about `MAX_BODY_BYTES`, `lone-surrogate` for a string or key holding half a UTF-16 pair (JSON's
    `"\\ud800"`, which no UTF-8 store and no canonical form can hold), or None."""
    stack, size = [(doc, 1)], 0
    while stack:
        value, depth = stack.pop()
        if depth > MAX_DEPTH:
            return 'too-deep'
        if isinstance(value, str):
            size += len(value) + 2
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
                return 'lone-surrogate'
        elif isinstance(value, dict):
            size += 2
            for key, item in value.items():
                if not isinstance(key, str) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in key):
                    return 'lone-surrogate'
                size += len(key) + 4
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            size += 2
            stack.extend((item, depth + 1) for item in value)
        else:
            size += 24
        if size > MAX_BODY_BYTES:
            return 'too-large'
    return None


def request_problems(doc: Any) -> List[str]:
    """What `repair-request.schema.json` refuses in `doc`, each with its pointer; empty when it admits it. The runner
    acts on nothing the schema refuses. A document too deep, too large or holding a lone surrogate is refused before
    anything else is read (`body_problem`)."""
    unreadable = body_problem(doc)
    if unreadable is not None:
        return ['/ ' + unreadable]
    out: List[str] = []
    fields = ('type', 'org', 'id', 'device', 'package', 'task', 'issuedAt', 'expiresAt', 'signature')
    if not _closed(doc, fields, fields, '', out):
        return out
    if 'type' in doc and doc['type'] != TYPE:
        out.append('/type is not %s' % TYPE)
    for name in ('org', 'device'):
        if name in doc and _closed(doc[name], ('id',), ('id',), '/' + name, out) and 'id' in doc[name] \
                and not _match(OPAQUE_ID, doc[name]['id']):
            out.append('/%s/id is not an opaque id' % name)
    if 'id' in doc and not _match(OPAQUE_ID, doc['id']):
        out.append('/id is not an opaque id')
    package = doc.get('package')
    if 'package' in doc and _closed(package, ('agentRef', 'version', 'digest'), ('agentRef', 'version', 'digest'),
                                    '/package', out):
        if 'agentRef' in package and not (_match(AGENT_REF, package['agentRef']) and len(package['agentRef']) <= 146):
            out.append('/package/agentRef is not an agent reference')
        if 'version' in package and not _match(SEMVER, package['version']):
            out.append('/package/version is not a semantic version')
        if 'digest' in package and not _match(SHA256, package['digest']):
            out.append('/package/digest is not a sha256 digest')
    task = doc.get('task')
    if 'task' in doc and not (isinstance(task, str) and 1 <= len(task.encode('utf-16-le')) // 2 <= 300
                              and not TASK_FORBIDDEN.search(task)):
        out.append('/task is not one line of 1 to 300 characters')
    for name in ('issuedAt', 'expiresAt'):
        if name in doc and _time(doc[name]) is None:
            out.append('/%s is not an RFC 3339 date-time' % name)
    signature = doc.get('signature')
    if 'signature' in doc and _closed(signature, ('alg', 'keyId', 'value'), ('alg', 'keyId', 'value'), '/signature',
                                      out):
        if 'alg' in signature and signature['alg'] not in ('ed25519', 'es256'):
            out.append('/signature/alg is not ed25519 or es256')
        if 'keyId' in signature and not _match(OPAQUE_ID, signature['keyId']):
            out.append('/signature/keyId is not an opaque id')
        if 'value' in signature and not _match(SIGNATURE_VALUE, signature['value']):
            out.append('/signature/value is not 64 bytes of base64')
    return out


def result_problems(doc: Any) -> List[str]:
    """What `repair-result.schema.json` refuses in `doc`; empty when it admits it. Every result this node builds is
    checked with it before it is stored."""
    out: List[str] = []
    fields = ('type', 'id', 'outcome', 'reason', 'notifiedAt', 'startedAt', 'at')
    if not _closed(doc, ('type', 'id', 'outcome', 'at'), fields, '', out):
        return out
    if 'type' in doc and doc['type'] != RESULT_TYPE:
        out.append('/type is not %s' % RESULT_TYPE)
    if 'id' in doc and not _match(OPAQUE_ID, doc['id']):
        out.append('/id is not an opaque id')
    if 'outcome' in doc and doc['outcome'] not in ('succeeded', 'failed', 'refused'):
        out.append('/outcome is not succeeded, failed or refused')
    if 'reason' in doc and doc['reason'] not in RESULT_REASONS:
        out.append('/reason is not one of the result reasons')
    for name in ('notifiedAt', 'startedAt', 'at'):
        if name in doc and _time(doc[name]) is None:
            out.append('/%s is not an RFC 3339 date-time' % name)
    if doc.get('outcome') == 'succeeded':
        out.extend('/%s is missing' % name for name in ('notifiedAt', 'startedAt') if name not in doc)
        if 'reason' in doc:
            out.append('/reason is not allowed when it succeeded')
    elif 'reason' not in doc:
        out.append('/reason is missing')
    return out


def build_result(repair_id: str, outcome: str, at: str, *, reason: Optional[str] = None,
                 notified_at: Optional[str] = None, started_at: Optional[str] = None) -> dict:
    """One `repair-result/1`, checked against its schema (ValueError when it would be refused)."""
    doc: Dict[str, Any] = {'type': RESULT_TYPE, 'id': repair_id, 'outcome': outcome}
    if reason is not None:
        doc['reason'] = reason
    if notified_at is not None:
        doc['notifiedAt'] = notified_at
    if started_at is not None:
        doc['startedAt'] = started_at
    doc['at'] = at
    problems = result_problems(doc)
    if problems:
        raise ValueError('; '.join(problems))
    return doc


# ---- the owner's allow-list of local actions ---------------------------------------------------------------------

class ConfigProblem(ValueError):
    """`config/node-repair.json` cannot be used: the reason, worded by what failed."""


def empty_config() -> dict:
    return {'schemaVersion': 1, 'agents': {}, 'lifecycle': None}


def validate_config(doc: Any) -> dict:
    """The closed shape: `{schemaVersion: 1, agents: {agentRef: {action, targetId}}, lifecycle?: {bearerSlot}}`, where
    `action` is `restart` or `ensure_running`, `targetId` a `node.lifecycle` target id, and `bearerSlot` the vault slot
    `PROJECT/ENV/NAME` that holds the bearer of the access binding the operator issued for the repair runner."""
    if not isinstance(doc, dict) or set(doc) - {'schemaVersion', 'agents', 'lifecycle'} \
            or doc.get('schemaVersion') != 1 or not isinstance(doc.get('agents'), dict):
        raise ConfigProblem('it must be {"schemaVersion": 1, "agents": {...}, "lifecycle": {"bearerSlot": ...}}')
    agents = {}
    for ref, entry in doc['agents'].items():
        if not _match(AGENT_REF, ref) or not isinstance(entry, dict) or set(entry) != {'action', 'targetId'} \
                or entry['action'] not in ACTIONS or not _match(LIFECYCLE_ID, entry['targetId']):
            raise ConfigProblem('agents.%s must be {"action": "restart" or "ensure_running", "targetId": ID}'
                                % (ref if _match(AGENT_REF, ref) else '<agentRef>'))
        agents[ref] = {'action': entry['action'], 'targetId': entry['targetId']}
    lifecycle = doc.get('lifecycle')
    if lifecycle is not None and not (isinstance(lifecycle, dict) and set(lifecycle) == {'bearerSlot'}
                                      and _match(SLOT, lifecycle['bearerSlot'])):
        raise ConfigProblem('lifecycle must be {"bearerSlot": "PROJECT/ENV/NAME"}')
    return {'schemaVersion': 1, 'agents': agents, 'lifecycle': lifecycle}


def read_config(path: Optional[Path] = None) -> Tuple[dict, Optional[str]]:
    """The owner's allow-list (`config/node-repair.json`) and why it cannot be used, or None. Absent: no agent, no
    executor. The file must be a regular file, not a link, owner-only, in a folder nobody else may write to; anything
    else runs nothing, with the reason."""
    import osprivacy
    if path is None:
        import paths as _paths
        path = Path(_paths.CONFIG) / CONFIG_NAME
    path = Path(path)
    where = 'config/%s' % CONFIG_NAME
    if not path.exists() and not path.is_symlink():
        return empty_config(), None
    if path.is_symlink():
        return empty_config(), '%s is a symbolic link; make it a regular file' % where
    folder = path.parent.resolve()
    try:
        if osprivacy.others_may_write(folder):
            return empty_config(), '%s is in a folder others may write to' % where
        fd = osprivacy.open(folder / path.name, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return empty_config(), '%s is not a regular file' % where
            if not osprivacy.private(stream.fileno()):
                return empty_config(), '%s is readable by others; make it owner-only (chmod 600 on POSIX)' % where
            if info.st_size > CONFIG_MAX_BYTES:
                return empty_config(), '%s is larger than 256 KB' % where
            doc = json.loads(stream.read())
    except (OSError, ValueError) as error:
        return empty_config(), '%s is unreadable (%s)' % (where, type(error).__name__)
    try:
        return validate_config(doc), None
    except ConfigProblem as problem:
        return empty_config(), '%s: %s' % (where, problem)


# ---- the lifecycle door ------------------------------------------------------------------------------------------

class ExecutorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


HttpTransport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]


def _loopback_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float = 30) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))    # loopback only: never a proxy
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1 << 16)
    except (urllib.error.URLError, OSError):
        return 0, b''


def vault_bearer(slot: str) -> str:
    """The access binding's bearer from the vault slot `PROJECT/ENV/NAME`, vault only, read for one call and never
    stored, printed or logged."""
    project, env, name = slot.split('/')
    tools = Path(__file__).resolve().parents[1] / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        import use_secret
        value, _ = use_secret.resolve(project, name, env, vault_only=True)
    except LookupError:
        raise ExecutorError('bearer-missing') from None
    value = (value or '').strip()
    if not value:
        raise ExecutorError('bearer-missing')
    return value


class LifecycleExecutor:
    """Runs one allow-listed action through `node.lifecycle`'s own door: the engine's local server, `POST /mcp`, with the
    bearer of an access binding the operator issued for the repair runner and granted only the targets it may repair
    (`full access-binding issue`, `full lifecycle allow --binding ID`). It is a caller like any other: the broker's
    grant, its person rules (`in_use_by_person`, `held_off`) and its receipts hold. The lifecycle request id is derived
    from the organization and the repair id, so a start retried after a crash is answered by the first operation."""

    def __init__(self, bearer: Callable[[], str], *, port: Optional[int] = None,
                 transport: Optional[HttpTransport] = None):
        self._bearer = bearer
        self.port = port if port is not None else int(os.environ.get('OBSERVATORY_SERVER_PORT', '47311'))
        self.transport = transport or _loopback_transport
        self._sequence = 0

    def _call(self, name: str, arguments: dict) -> dict:
        self._sequence += 1
        body = json.dumps({'jsonrpc': '2.0', 'id': self._sequence, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}}).encode()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'Authorization': 'Bearer ' + self._bearer()}
        status, raw = self.transport('http://127.0.0.1:%d/mcp' % self.port, headers, body, 30)
        if status == 0:
            raise ExecutorError('lifecycle-unreachable')
        try:
            answer = json.loads(raw)
        except ValueError:
            answer = None
        if status != 200 or not isinstance(answer, dict):
            code = answer.get('error') if isinstance(answer, dict) and isinstance(answer.get('error'), str) else None
            raise ExecutorError(code or 'lifecycle-http-%d' % status)
        result = answer.get('result') if isinstance(answer.get('result'), dict) else {}
        structured = result.get('structuredContent')
        if not isinstance(structured, dict):
            raise ExecutorError('lifecycle-answer-unreadable')
        if isinstance(structured.get('error'), str):
            raise ExecutorError(structured['error'])
        return structured

    def start(self, action: dict, request_id: str) -> dict:
        """Admits the action: `{operationId}`. Raises ExecutorError with the broker's code when it refuses."""
        listed = self._call('lifecycle.list', {})
        target = next((t for t in listed.get('targets') or [] if isinstance(t, dict)
                       and t.get('targetId') == action['targetId']), None)
        if target is None:
            raise ExecutorError('target-not-granted')
        receipt = self._call(ACTIONS[action['action']], {
            'targetId': action['targetId'], 'requestId': request_id,
            'expectedPolicyRevision': target.get('policyRevision'), 'grantRevision': listed.get('grantRevision')})
        if not isinstance(receipt.get('operationId'), str):
            raise ExecutorError('lifecycle-answer-unreadable')
        return {'operationId': receipt['operationId'], 'state': receipt.get('state')}

    def poll(self, operation_id: str) -> dict:
        """`{state: running}`, `{state: succeeded}`, or `{state: failed, code}` with the broker's final state."""
        receipt = self._call('lifecycle.operation.get', {'operationId': operation_id})
        state = receipt.get('state')
        if state == 'running':
            return {'state': 'running'}
        if state in LIFECYCLE_SUCCESS:
            return {'state': 'succeeded', 'code': state}
        return {'state': 'failed', 'code': state if isinstance(state, str) else 'unknown'}


def executor_from(config: dict) -> Optional[LifecycleExecutor]:
    """The executor the owner's allow-list names, or None when it names no bearer slot."""
    lifecycle = config.get('lifecycle')
    if not isinstance(lifecycle, dict):
        return None
    slot = lifecycle['bearerSlot']
    return LifecycleExecutor(lambda: vault_bearer(slot))


# ---- the book: one row per request id ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS repairs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL,
  request TEXT NOT NULL, agent_ref TEXT, version TEXT, task TEXT, received_at TEXT NOT NULL, notified_at TEXT,
  run_after REAL, declined_at TEXT, operation_id TEXT, started_at TEXT, result TEXT, answered_at TEXT, sent_at TEXT,
  note TEXT, expires_at INTEGER);
"""


def lifecycle_request_id(org_id: str, repair_id: str) -> str:
    """The lifecycle broker's request id for one repair: stable across retries, inside the broker's id shape."""
    return 'repair-' + hashlib.sha256(('%s\n%s' % (org_id, repair_id)).encode('utf-8')).hexdigest()[:32]


def _iso(seconds: float) -> str:
    return rfc3339(int(seconds * 1000))


class Context:
    """What one decision is taken under: this enrollment's device, organization and role, the policy key set and
    signed layers it applied, the owner's allow-list and executor, and the clock."""

    def __init__(self, *, org: str, device: str, role: Optional[str], keys: List[dict], layers: List[dict],
                 agents: Dict[str, dict], executor: Any, now: float, journal: Callable[..., None],
                 notice_seconds: int = NOTICE_SECONDS):
        self.org, self.device, self.role = org, device, role
        self.keys, self.layers, self.agents, self.executor = keys, layers, agents, executor
        self.now, self.journal, self.notice_seconds = now, journal, notice_seconds

    def decide(self, request: dict, *, notified_at: Optional[str] = None, declined: bool = False) -> dict:
        value = {'request': request, 'keys': self.keys,
                 'device': dict({'id': self.device, 'org': self.org}, **({'role': self.role} if self.role else {})),
                 'layers': self.layers, 'at': _iso(self.now), 'declined': declined}
        if notified_at is not None:
            value['notifiedAt'] = notified_at
        return repair_decision(value)


def _answer(conn, ctx: Context, row_id: str, outcome: str, *, reason: Optional[str] = None,
            notified_at: Optional[str] = None, started_at: Optional[str] = None, note: Optional[str] = None,
            agent_ref: Optional[str] = None) -> dict:
    result = build_result(row_id, outcome, _iso(ctx.now), reason=reason, notified_at=notified_at,
                          started_at=started_at)
    # An answered request needs no body: only its id (it runs once) and its result are kept.
    conn.execute("UPDATE repairs SET state = 'answered', result = ?, answered_at = ?, sent_at = NULL, note = ?, "
                 "request = '{}' WHERE id = ?", (json.dumps(result, sort_keys=True), _iso(ctx.now), note, row_id))
    ctx.journal('repair-answered', repair=row_id, outcome=outcome, reason=reason, note=note, agentRef=agent_ref)
    return result


def receive(conn, ctx: Context, requests: Any) -> dict:
    """Takes the `records.repairs` of one check-in response: each request is recorded once by its id and either
    answered at once (refused by the decision, or `failed` when this machine has no action for the package) or shown
    to the person and left to `advance`. A redelivered id gets its stored result again and is never run twice.
    Nothing one request holds stops the others or the check-in: a failure on it is journalled by its exception's
    name, never its text, and it is left unrecorded so a later delivery is read again."""
    counts = {'received': 0, 'refused': 0, 'failed': 0, 'notified': 0, 'redelivered': 0, 'malformed': 0,
              'dropped': 0}
    items = requests if isinstance(requests, list) else []
    if len(items) > MAX_REQUESTS_PER_RESPONSE:
        ctx.journal('repair-over-limit', received=len(items), kept=MAX_REQUESTS_PER_RESPONSE)
        items = items[:MAX_REQUESTS_PER_RESPONSE]
    for item in items:
        rid = item.get('id') if isinstance(item, dict) else None
        if not _match(OPAQUE_ID, rid):
            counts['malformed'] += 1
            ctx.journal('repair-malformed', problem='no repair id that reads')
            continue
        try:
            _receive_one(conn, ctx, item, rid, counts)
        except Exception as error:                             # noqa: BLE001: a hostile body stops nothing
            counts['malformed'] += 1
            ctx.journal('repair-unreadable', repair=rid, error=type(error).__name__)
    _prune(conn, ctx.now)
    return counts


def _receive_one(conn, ctx: Context, item: dict, rid: str, counts: dict) -> None:
    problems = request_problems(item)
    if problems:
        canonical, digest = None, 'refused:' + problems[0][:60]
    else:
        canonical = settings_canonical_json(item)
        digest = 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    held = conn.execute('SELECT state, digest FROM repairs WHERE id = ?', (rid,)).fetchone()
    if held is not None:
        counts['redelivered'] += 1
        if held['state'] == 'answered':                        # DEC-0102: the same stored result, sent again
            conn.execute('UPDATE repairs SET sent_at = NULL WHERE id = ?', (rid,))
        ctx.journal('repair-redelivered', repair=rid, state=held['state'], same=held['digest'] == digest)
        return
    rows = conn.execute('SELECT COUNT(*) AS n FROM repairs').fetchone()['n'] or 0
    if rows >= MAX_BOOK_ROWS:
        counts['dropped'] += 1                                 # not recorded: the server sends it again later
        ctx.journal('repair-book-full', repair=rid, rows=rows)
        return
    counts['received'] += 1
    with _transaction(conn):                                   # recorded and answered (or shown) as one
        _take(conn, ctx, item, rid, canonical, digest, counts, problems)


@contextlib.contextmanager
def _transaction(conn):
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def _take(conn, ctx: Context, item: dict, rid: str, canonical: Optional[str], digest: str, counts: dict,
          problems: List[str]) -> None:
    package = item.get('package') if isinstance(item.get('package'), dict) else {}
    agent_ref = package.get('agentRef') if _match(AGENT_REF, package.get('agentRef')) else None
    version = package.get('version') if _match(SEMVER, package.get('version')) else None
    # The body and the task line are kept only for a request the schema admits; a refused one keeps its id alone.
    task = None if problems else item['task']
    # The request's own expiry, read even from a refused request: the id is forgotten only after it (`_prune`).
    expires = item.get('expiresAt')
    expires_at = _time(expires) if isinstance(expires, str) and len(expires) <= 64 else None
    conn.execute('INSERT INTO repairs(id, digest, state, request, agent_ref, version, task, received_at, expires_at) '
                 "VALUES (?, ?, 'received', ?, ?, ?, ?, ?, ?)",
                 (rid, digest, canonical or '{}', agent_ref, version, task, _iso(ctx.now), expires_at))
    ctx.journal('repair-received', repair=rid, agentRef=agent_ref, version=version,
                digest=package.get('digest') if _match(SHA256, package.get('digest')) else None)
    if problems:
        # Nothing the schema refuses is acted on or shown; refused as one whose signed form cannot be trusted.
        counts['refused'] += 1
        note = problems[0][2:] if problems[0].startswith('/ ') else 'shape'
        _answer(conn, ctx, rid, 'refused', reason='bad-signature', note=note, agent_ref=agent_ref)
        return
    first = ctx.decide(item)
    if first['decision'] == 'refuse' and first['reason'] != 'person-declined':
        counts['refused'] += 1
        _answer(conn, ctx, rid, 'refused', reason=first['reason'], agent_ref=agent_ref)
        return
    action = ctx.agents.get(agent_ref)
    if action is None or ctx.executor is None:
        # The organization allows it, but nothing on this machine would run it: answered before the person is
        # told, because there is nothing to tell them about.
        counts['failed'] += 1
        _answer(conn, ctx, rid, 'failed', reason='run-failed',
                note='no-local-action' if action is None else 'no-executor', agent_ref=agent_ref)
        return
    pending = conn.execute("SELECT COUNT(*) AS n FROM repairs WHERE state NOT IN ('answered', 'received')"
                           ).fetchone()['n']
    if pending >= MAX_PENDING:                             # not recorded: the server sends it again later
        conn.execute('DELETE FROM repairs WHERE id = ?', (rid,))
        counts['received'] -= 1
        counts['dropped'] += 1
        ctx.journal('repair-book-full', repair=rid, pending=pending)
        return
    notified = _iso(ctx.now)                               # org-cloud: when it verified the request (DEC-0102)
    run_after = ctx.now if ctx.role == 'org-cloud' else ctx.now + ctx.notice_seconds
    conn.execute("UPDATE repairs SET state = 'notified', notified_at = ?, run_after = ? WHERE id = ?",
                 (notified, run_after, rid))
    counts['notified'] += 1
    ctx.journal('repair-notified', repair=rid, agentRef=agent_ref, action=action['action'],
                target=action['targetId'], runAfter=_iso(run_after))


def decline(conn, ctx: Context, repair_id: str) -> Optional[str]:
    """The person declines a repair that has not started: it is answered `person-declined` at once and never runs.
    Answers the row's state before the decline, or None when there is no such repair."""
    row = conn.execute('SELECT state, notified_at, agent_ref FROM repairs WHERE id = ?', (repair_id,)).fetchone()
    if row is None:
        return None
    if row['state'] == 'notified':
        with _transaction(conn):
            conn.execute('UPDATE repairs SET declined_at = ? WHERE id = ?', (_iso(ctx.now), repair_id))
            ctx.journal('repair-declined', repair=repair_id)
            _answer(conn, ctx, repair_id, 'refused', reason='person-declined', notified_at=row['notified_at'],
                    agent_ref=row['agent_ref'])
    return row['state']


def advance(conn, ctx: Context) -> dict:
    """What is due: a declined repair answered `person-declined`; one whose notice has stood long enough decided again
    at this moment and started through the executor; a started one polled to its end. Answers counts."""
    counts = {'declined': 0, 'refused': 0, 'started': 0, 'succeeded': 0, 'failed': 0, 'waiting': 0}
    for row in conn.execute("SELECT * FROM repairs WHERE state IN ('notified', 'starting', 'running') ORDER BY "
                            'received_at, id').fetchall():
        rid, agent_ref = row['id'], row['agent_ref']
        if row['state'] == 'notified':
            request = json.loads(row['request'])
            if row['declined_at'] and ctx.role != 'org-cloud':
                counts['declined'] += 1
                _answer(conn, ctx, rid, 'refused', reason='person-declined', notified_at=row['notified_at'],
                        agent_ref=agent_ref)
                continue
            if ctx.now < (row['run_after'] or 0):
                counts['waiting'] += 1
                continue
            decision = ctx.decide(request, notified_at=row['notified_at'], declined=bool(row['declined_at']))
            if decision['decision'] == 'refuse':
                counts['refused'] += 1
                _answer(conn, ctx, rid, 'refused', reason=decision['reason'], notified_at=row['notified_at'],
                        agent_ref=agent_ref)
                continue
            action = ctx.agents.get(agent_ref)
            if action is None or ctx.executor is None:
                counts['failed'] += 1
                _answer(conn, ctx, rid, 'failed', reason='run-failed', notified_at=row['notified_at'],
                        note='no-local-action' if action is None else 'no-executor', agent_ref=agent_ref)
                continue
            # Recorded before the effect: a crash between here and the answer starts it again under the same
            # lifecycle request id, which the broker answers with the first operation.
            conn.execute("UPDATE repairs SET state = 'starting', started_at = ? WHERE id = ?", (_iso(ctx.now), rid))
            row = conn.execute('SELECT * FROM repairs WHERE id = ?', (rid,)).fetchone()
        if row['state'] == 'starting':
            action = ctx.agents.get(agent_ref)
            if action is None or ctx.executor is None:
                counts['failed'] += 1
                _answer(conn, ctx, rid, 'failed', reason='run-failed', notified_at=row['notified_at'],
                        note='no-local-action' if action is None else 'no-executor', agent_ref=agent_ref)
                continue
            try:
                admitted = ctx.executor.start(action, lifecycle_request_id(ctx.org, rid))
            except ExecutorError as error:
                counts['failed'] += 1
                _answer(conn, ctx, rid, 'failed', reason='run-failed', notified_at=row['notified_at'],
                        note=error.code[:60], agent_ref=agent_ref)
                continue
            conn.execute("UPDATE repairs SET state = 'running', operation_id = ? WHERE id = ?",
                         (admitted['operationId'], rid))
            counts['started'] += 1
            ctx.journal('repair-started', repair=rid, agentRef=agent_ref, action=action['action'],
                        target=action['targetId'], operation=admitted['operationId'])
            row = conn.execute('SELECT * FROM repairs WHERE id = ?', (rid,)).fetchone()
        if row['state'] == 'running':
            outcome = None
            if ctx.executor is None:
                outcome = {'state': 'failed', 'code': 'no-executor'}
            else:
                try:
                    outcome = ctx.executor.poll(row['operation_id'])
                except ExecutorError as error:
                    outcome = {'state': 'running', 'code': error.code}
            started = strict_time(row['started_at'] or '')
            if outcome['state'] == 'running':
                if started is not None and ctx.now - started / 1000 > RUN_TIMEOUT_SECONDS:
                    outcome = {'state': 'failed', 'code': 'timeout'}
                else:
                    counts['waiting'] += 1
                    continue
            if outcome['state'] == 'succeeded':
                counts['succeeded'] += 1
                _answer(conn, ctx, rid, 'succeeded', notified_at=row['notified_at'], started_at=row['started_at'],
                        note=outcome.get('code'), agent_ref=agent_ref)
            else:
                counts['failed'] += 1
                _answer(conn, ctx, rid, 'failed', reason='run-failed', notified_at=row['notified_at'],
                        started_at=row['started_at'], note=str(outcome.get('code') or 'failed')[:60],
                        agent_ref=agent_ref)
    _prune(conn, ctx.now)
    return counts


def _prune(conn, now: float) -> None:
    """An answered id is forgotten only once a server has taken its result and its own request's `expiresAt` is more
    than `PRUNE_MARGIN_SECONDS` past: a request with that id is refused `expired` by then, whatever the book holds. Never
    by when it was answered (a request that arrived before its `issuedAt` was answered early and is still live), and a
    row whose request had no readable expiry is kept, bounded by `MAX_BOOK_ROWS`."""
    conn.execute("DELETE FROM repairs WHERE state = 'answered' AND sent_at IS NOT NULL AND expires_at IS NOT NULL "
                 'AND expires_at < ?', (int((now - PRUNE_MARGIN_SECONDS) * 1000),))


def due_results(conn, limit: int = MAX_RESULTS_PER_CHECK_IN) -> List[dict]:
    """The stored results not yet taken by a check-in, oldest first, at most `limit`."""
    rows = conn.execute("SELECT result FROM repairs WHERE state = 'answered' AND sent_at IS NULL AND result IS NOT NULL "
                        'ORDER BY answered_at, id LIMIT ?', (limit,)).fetchall()
    return [json.loads(row['result']) for row in rows]


def mark_sent(conn, ids: List[str], at: float) -> None:
    conn.executemany('UPDATE repairs SET sent_at = ? WHERE id = ?', [(_iso(at), rid) for rid in ids])


def drop_pending(conn) -> int:
    """An enrollment that ended (removed, or its organization closed) runs nothing more: every repair not yet answered
    is forgotten; answered ids stay, so none runs twice should the organization enroll this machine again."""
    return conn.execute("DELETE FROM repairs WHERE state != 'answered'").rowcount


def report(conn, recent: int = 5) -> dict:
    """What the person is shown: the repairs waiting to run (the task line, the package, when it runs, whether declined)
    and the latest results, with the counts of results waiting for a check-in."""
    pending = [{'id': r['id'], 'task': r['task'], 'agentRef': r['agent_ref'], 'version': r['version'],
                'notifiedAt': r['notified_at'], 'state': r['state'],
                'runsAfter': _iso(r['run_after']) if r['run_after'] is not None else None,
                'declined': bool(r['declined_at'])}
               for r in conn.execute("SELECT * FROM repairs WHERE state IN ('notified', 'starting', 'running') "
                                     'ORDER BY received_at, id')]
    last = []
    for r in conn.execute("SELECT * FROM repairs WHERE state = 'answered' ORDER BY answered_at DESC, id LIMIT ?",
                          (recent,)):
        result = json.loads(r['result']) if r['result'] else {}
        last.append({'id': r['id'], 'task': r['task'], 'agentRef': r['agent_ref'], 'outcome': result.get('outcome'),
                     'reason': result.get('reason'), 'at': result.get('at'), 'note': r['note'],
                     'sent': r['sent_at'] is not None})
    unsent = conn.execute("SELECT COUNT(*) AS n FROM repairs WHERE state = 'answered' AND sent_at IS NULL"
                          ).fetchone()['n']
    return {'pending': pending, 'recent': last, 'unsent': unsent}

