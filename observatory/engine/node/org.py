"""This machine's organization enrollments: `fabric-device/0.1`, `fabric-activity/0.1` and `share-registration/1`
(contract DEC-0031, DEC-0056, DEC-0082, DEC-0086; engine OBS-161, the producer half of OE-05 and OE-01b).

One enrollment per organization (DEC-0082), each in its own owner-only folder `<node>/orgs/<org.id>/`:

- `enrollment.json`: the server's origin, the certificate (serial, window, binding), the stream epoch, the protocols the
  server accepts, the check-in interval and `collectionStart`, and the device's check-in sequence. No secret: the
  enrollment token is read from the vault for the one request that needs it and is never stored, logged or printed.
- `outbox.sqlite`: the activity stream of this enrollment (`collector.id` `observatory-node`, the epoch the server
  issued), every event persisted once with its id and seq before it is sent, sent in contiguous slices of at most 1000,
  each batch signed afresh, retried with backoff under the same `batchId`, consumed by the server's ack; and the share
  registrations with the server's latest answer to each.
- `journal.jsonl`: structured, secret-free lines of what was done (bounded).

The device key is the machine's `#sig` key (`node/device_keys.py`): the certificate request is signed by it, and every
request after enrollment carries its `fabric-device` HTTP signature, `keyid` the certificate's serial
(`http_signature.py`). A share registration is derived only from the estate revision and the revocation-delta entries
this machine verified and applied (`records/`), and only toward a server whose enrollment response lists
`share-registration/1`; `grant.usage` for a grant is sent only when the server's latest answer to that grant's
registration lets it (`share_registration.gate`).
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import private_io

from . import http_signature, share_registration
from .relay import strict_time

LOG = logging.getLogger('observatory.node.org')

DEVICE_PROTOCOL = 'fabric-device/0.1'
ACTIVITY_PROTOCOL = 'fabric-activity/0.1'
SHARE_PROTOCOL = share_registration.TYPE
COLLECTOR_ID = 'observatory-node'
ORG_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
TOKEN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._:~-]{0,127})\.([A-Za-z0-9_-]{16,256})\Z')
#: The receiver names its paths; the contract names none. These are the first organization server's, overridable.
PATHS = {'enroll': '/v1/devices/enroll', 'check-in': '/v1/devices/check-in', 'rotate': '/v1/devices/rotate',
         'batches': '/v1/activity/batches', 'share-registration': '/v1/devices/share-registrations'}
MAX_BATCH = 1000
#: The outbox's bound: past it the oldest pending events are dropped and one `buffer.overflow` names them.
MAX_PENDING = 10000
BACKOFF_FLOOR, BACKOFF_CEILING = 30, 3600
JOURNAL_BYTES = 256 * 1024
_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
_BOOT = 'boot-' + secrets.token_hex(4)

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]


class OrgError(RuntimeError):
    """A refusal with a code: `{'error': code, 'message': ...}` for the terminal, never a secret."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):           # a signed body is never re-sent to another target
        return None


def default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float = 30) -> Tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, data=body, method='POST', headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(64 * 1024)


def ulid(ms: Optional[int] = None) -> str:
    """A ULID (Crockford base32, 26 characters): 48 bits of milliseconds, 80 random bits."""
    t = int(time.time() * 1000) if ms is None else ms
    head = ''.join(_CROCKFORD[(t >> (5 * i)) & 31] for i in reversed(range(10)))
    rand = int.from_bytes(secrets.token_bytes(10), 'big')
    return head + ''.join(_CROCKFORD[(rand >> (5 * i)) & 31] for i in reversed(range(16)))


def iso(seconds: float) -> str:
    return _dt.datetime.fromtimestamp(int(seconds), _dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def platform_name() -> str:
    return {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux' if sys.platform.startswith('linux')
                                                        else 'other')


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_code(doc: Any) -> Optional[str]:
    """The receiver's refusal code, bounded: `{error: {code}}` or `{error: code}` or `{code}`."""
    if isinstance(doc, dict):
        err = doc.get('error')
        code = err.get('code') if isinstance(err, dict) else err if isinstance(err, str) else doc.get('code')
        if isinstance(code, str) and re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', code):
            return code
    return None


def read_token(slot: str) -> str:
    """The enrollment token from the vault slot `PROJECT/ENV/NAME`, vault only (never an env file)."""
    parts = slot.split('/')
    if len(parts) != 3 or not all(parts):
        raise OrgError('token-slot', 'the token slot is PROJECT/ENV/NAME')
    tools = Path(__file__).resolve().parents[1] / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        import use_secret
        value, _ = use_secret.resolve(parts[0], parts[2], parts[1], vault_only=True)
    except LookupError:
        raise OrgError('token-missing', 'the vault holds no value in that slot') from None
    value = (value or '').strip()
    if not TOKEN.fullmatch(value):
        raise OrgError('token-malformed', 'the slot does not hold an enrollment token of the form <tokenId>.<secret>')
    return value


# ---- What this machine applied of its estate record -----------------------------------------------------------

def records_folder(node: Path) -> Path:
    return Path(node) / 'records'


def applied_records(node: Path) -> Tuple[Optional[dict], List[dict]]:
    """The estate revision this machine verified and applied, and the revocation-delta entries it applied since
    (`records/estate-record.json`, `records/delta-state.json`), or `(None, [])`."""
    folder = records_folder(node)
    try:
        record = json.loads(private_io.read(folder / 'estate-record.json'))
    except (FileNotFoundError, ValueError, RuntimeError, OSError):
        return None, []
    try:
        state = json.loads(private_io.read(folder / 'delta-state.json'))
        entries = [e for e in state.get('entries', []) if isinstance(e, dict)] if isinstance(state, dict) else []
    except (FileNotFoundError, ValueError, RuntimeError, OSError):
        entries = []
    return (record if isinstance(record, dict) else None), entries


def apply_record(node: Path, record: Any, at_ms: int, authorities: Optional[list] = None) -> dict:
    """Verifies an estate revision (`verify_estate_record`) against the one applied before it, or, for the first,
    under the passkeys the operator pinned (`authorities`), and stores it as applied. A draft or a revision that does
    not verify is refused and nothing changes."""
    from . import estate
    folder = records_folder(node)
    current, _ = applied_records(node)
    if current is not None:
        if isinstance(record, dict) and record.get('revision') == current.get('revision') \
                and record.get('digest') == current.get('digest'):
            return {'ok': True, 'revision': current.get('revision'), 'unchanged': True}
        verdict = estate.verify_estate_record(record, at_ms=at_ms, parent=current)
    else:
        if not authorities:
            raise OrgError('no-authorities', 'the first revision this machine applies needs the passkeys it pins')
        verdict = estate.verify_estate_record(record, at_ms=at_ms, authorities=authorities)
    if not verdict.get('ok'):
        raise OrgError('record-refused', 'the estate revision does not verify: %s' % verdict.get('problem'))
    with private_io.lock(folder / '.records.lock'):
        private_io.write(folder / 'estate-record.json', json.dumps(record, sort_keys=True))
        state_path = folder / 'delta-state.json'
        if current is None or current.get('estate') != record.get('estate'):
            from .revocation import empty_delta_state
            private_io.write(state_path, json.dumps(empty_delta_state(record.get('estate'))))
    return {'ok': True, 'revision': verdict.get('revision'), 'digest': verdict.get('digest')}


def apply_delta_page(node: Path, page: Any, at_ms: int) -> dict:
    """Applies a page of revocation-delta entries (`revocation.apply_delta`) under the applied revision."""
    from . import revocation
    record, _ = applied_records(node)
    if record is None:
        raise OrgError('no-record', 'apply an estate revision before its delta')
    folder = records_folder(node)
    with private_io.lock(folder / '.records.lock'):
        try:
            state = json.loads(private_io.read(folder / 'delta-state.json'))
        except (FileNotFoundError, ValueError, RuntimeError, OSError):
            state = revocation.empty_delta_state(record.get('estate'))
        result = revocation.apply_delta(state, page, {'record': record, 'at': at_ms})
        private_io.write(folder / 'delta-state.json', json.dumps(result.get('state', state)))
    if not result.get('ok'):
        raise OrgError('delta-refused', 'the delta page was refused: %s' % result.get('problem'))
    return {'ok': True, 'applied': result.get('applied', 0)}


# ---- One enrollment ---------------------------------------------------------------------------------------------

class Enrollment:
    """One organization enrollment of this machine: its state, outbox, requests and schedule."""

    def __init__(self, node: Path, org_id: str, *, keys=None, transport: Transport = default_transport,
                 clock: Callable[[], float] = time.time, meter_db: Optional[Path] = None,
                 paths: Optional[Dict[str, str]] = None):
        if not isinstance(org_id, str) or not ORG_ID.fullmatch(org_id):
            raise OrgError('org-id', 'an organization id is [A-Za-z0-9][A-Za-z0-9_-]{0,127}')
        self.node, self.org_id = Path(node), org_id
        self.folder = self.node / 'orgs' / org_id
        self._keys, self.transport, self.clock, self.meter_db = keys, transport, clock, meter_db
        self.paths = dict(PATHS, **(paths or {}))

    # -- state -------------------------------------------------------------------------------------------------
    @property
    def keys(self):
        if self._keys is None:
            from .device_keys import DeviceKeys
            self._keys = DeviceKeys.load_or_create((self.node / 'device').resolve())
        return self._keys

    def state(self) -> Optional[dict]:
        try:
            doc = json.loads(private_io.read(self.folder / 'enrollment.json'))
        except FileNotFoundError:
            return None
        return doc if isinstance(doc, dict) and doc.get('schema') == 'org-enrollment/1' else None

    def _save(self, doc: dict) -> None:
        private_io.write(self.folder / 'enrollment.json', json.dumps(doc, indent=1, sort_keys=True) + '\n')

    def journal(self, event: str, **fields: Any) -> None:
        """One structured line: what happened, codes and counts, never a token, key or body."""
        line = json.dumps({'at': iso(self.clock()), 'event': event, 'org': self.org_id, **fields}, sort_keys=True)
        LOG.info(line)
        path = self.folder / 'journal.jsonl'
        try:
            private_io.parent(self.folder)
            if path.exists() and path.stat().st_size > JOURNAL_BYTES:
                kept = path.read_text(encoding='utf-8').splitlines()[-200:]
                private_io.write(path, '\n'.join(kept) + '\n')
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass

    @contextlib.contextmanager
    def _db(self):
        private_io.parent(self.folder)
        path = self.folder / 'outbox.sqlite'
        fresh = not path.exists()
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if fresh:
                os.chmod(path, 0o600)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (epoch INTEGER NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL
                  UNIQUE, kind TEXT NOT NULL, key TEXT UNIQUE, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                  reason TEXT, PRIMARY KEY (epoch, seq));
                CREATE TABLE IF NOT EXISTS registrations (grant_id TEXT PRIMARY KEY, doc TEXT NOT NULL, digest TEXT
                  NOT NULL, answer TEXT, gate TEXT NOT NULL DEFAULT 'hold', record_digest TEXT, sent_at INTEGER,
                  attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);""")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _meta(conn, name: str, default: Any = None) -> Any:
        row = conn.execute('SELECT value FROM meta WHERE name = ?', (name,)).fetchone()
        return json.loads(row['value']) if row else default

    @staticmethod
    def _set_meta(conn, name: str, value: Any) -> None:
        conn.execute('INSERT INTO meta(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value',
                     (name, json.dumps(value, sort_keys=True)))

    def _backoff(self, attempts: int) -> int:
        return int(self.clock()) + min(BACKOFF_CEILING, BACKOFF_FLOOR * 2 ** min(attempts, 10)) + random.randint(0, 15)

    # -- requests ----------------------------------------------------------------------------------------------
    def _post(self, origin: str, path: str, doc: dict, headers: Dict[str, str]) -> Tuple[int, Any]:
        body = json.dumps(doc, separators=(',', ':')).encode('utf-8')
        try:
            status, raw = self.transport(origin + path, {'Content-Type': 'application/json', **headers}, body, 30)
        except (OSError, ValueError) as error:
            self.journal('transport-failed', path=path, error=type(error).__name__)
            return 0, None
        return status, _json(raw)

    def _signed(self, state: dict, path: str, doc: dict) -> Tuple[int, Any]:
        """A device request signed afresh (DEC-0056): a new nonce and `created` on every attempt."""
        body = json.dumps(doc, separators=(',', ':')).encode('utf-8')
        headers = http_signature.sign_request(self.keys.sign, method='POST', origin=state['server'], target=path,
                                              body=body, serial=state['certificate']['serial'],
                                              created=int(self.clock()))
        try:
            status, raw = self.transport(state['server'] + path, {'Content-Type': 'application/json', **headers},
                                         body, 30)
        except (OSError, ValueError) as error:
            self.journal('transport-failed', path=path, error=type(error).__name__)
            return 0, None
        answer = _json(raw)
        if status == 401:
            self.journal('request-refused', path=path, status=401, code=_error_code(answer))
        return status, answer

    def _certificate(self, response: Any, epoch_expected: Optional[int] = None) -> dict:
        """The certificate an enrollment or rotation answered, checked against what was asked: this organization and
        device, a serial, a window, and the machine's own `#sig` key in the DER."""
        cert = response.get('certificate') if isinstance(response, dict) else None
        if not isinstance(cert, dict):
            raise OrgError('response-malformed', 'the answer carries no certificate')
        binding = cert.get('binding') if isinstance(cert.get('binding'), dict) else {}
        if (binding.get('org') or {}).get('id') != self.org_id or (binding.get('device') or {}).get('id') \
                != self.keys.device_id:
            raise OrgError('binding-mismatch', 'the certificate binds another organization or device')
        serial, start, end = cert.get('serial'), strict_time(cert.get('notBefore')), strict_time(cert.get('notAfter'))
        if not isinstance(serial, str) or not http_signature.SERIAL.fullmatch(serial) or start is None \
                or end is None or end <= start:
            raise OrgError('response-malformed', 'the certificate has no usable serial or window')
        if end - start > 30 * 86400 * 1000:
            raise OrgError('certificate-too-long', 'a device certificate lives at most 30 days (FAC-SEM-044)')
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
            der = base64.b64decode(cert.get('der', ''), validate=True)
            spki = x509.load_der_x509_certificate(der).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        except (ValueError, TypeError) as error:
            raise OrgError('response-malformed', 'the certificate is not DER') from error
        if spki != self.keys.sig_spki():
            raise OrgError('key-mismatch', "the certificate does not certify this machine's #sig key")
        out = {'serial': serial, 'notBefore': cert['notBefore'], 'notAfter': cert['notAfter'],
               'issuer': cert.get('issuer'), 'user': bool(binding.get('user'))}
        return out

    # -- enrollment --------------------------------------------------------------------------------------------
    def enrollment_request(self, token_id: str) -> dict:
        spki = self.keys.sig_spki()
        return {'protocol': DEVICE_PROTOCOL, 'kind': 'enrollment-request', 'org': {'id': self.org_id},
                'device': {'id': self.keys.device_id, 'platform': platform_name()},
                'key': {'algorithm': 'ecdsa-p256', 'storage': 'platform-keystore', 'exportable': False,
                        'publicKeySha256': 'sha256:' + hashlib.sha256(spki).hexdigest()},
                'deviceKeys': self.keys.enrollment_keys(),
                'csr': base64.b64encode(self.keys.csr()).decode('ascii'),
                'auth': {'method': 'enrollment-token', 'tokenId': token_id},
                'requestedAt': iso(self.clock())}

    def enroll(self, server: str, token: str) -> dict:
        origin = http_signature.public_origin(server)
        if origin is None:
            raise OrgError('server-invalid', 'the server is https:// and an authority alone')
        match = TOKEN.fullmatch(token or '')
        if not match:
            raise OrgError('token-malformed', 'an enrollment token is <tokenId>.<secret>')
        previous = self.state()
        status, answer = self._post(origin, self.paths['enroll'], self.enrollment_request(match.group(1)),
                                    {'Authorization': 'EnrollmentToken ' + token})
        if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'enrollment-response':
            code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
            self.journal('enroll-refused', status=status, code=code)
            raise OrgError('enroll-refused', 'the server refused the enrollment: %s (HTTP %s)' % (code, status))
        cert = self._certificate(answer)
        epoch = answer.get('streamEpoch')
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1 \
                or (previous and epoch <= previous.get('epoch', 0)):
            raise OrgError('epoch-invalid', 'the stream epoch is not above every epoch issued to this device')
        interval = (answer.get('checkIn') or {}).get('intervalSeconds')
        protocols = [p for p in answer.get('protocols', []) if isinstance(p, str)] \
            if isinstance(answer.get('protocols'), list) else []
        doc = {'schema': 'org-enrollment/1', 'org': self.org_id, 'device': self.keys.device_id, 'server': origin,
               'certificate': cert, 'epoch': epoch, 'protocols': protocols,
               'checkInSeconds': interval if isinstance(interval, int) and 30 <= interval <= 86400 else 300,
               'collectionStart': answer.get('collectionStart') if strict_time(answer.get('collectionStart'))
               is not None else None,
               'policyRootKey': answer.get('policyRootKey'), 'enrolledAt': iso(self.clock()),
               'sequenceNum': 0, 'fullState': False, 'lastCheckIn': None, 'rotate': False,
               'reEnroll': False, 'nextCheckInAt': 0}
        private_io.parent(self.folder)
        self._save(doc)
        with self._db() as conn:                           # a new enrollment: registrations are sent again
            conn.execute("UPDATE registrations SET next_at = 0, sent_at = NULL WHERE gate != 'hold-degraded'")
        self.journal('enrolled', epoch=epoch, serial=cert['serial'], notAfter=cert['notAfter'],
                     protocols=protocols, reEnrollment=previous is not None)
        return self.describe()

    def rotate(self) -> dict:
        state = self._require()
        doc = {'protocol': DEVICE_PROTOCOL, 'kind': 'rotation-request', 'device': {'id': self.keys.device_id},
               'csr': base64.b64encode(self.keys.csr()).decode('ascii')}
        status, answer = self._signed(state, self.paths['rotate'], doc)
        if status != 200 or not isinstance(answer, dict):
            code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
            self.journal('rotate-refused', status=status, code=code)
            if code in ('epoch-superseded', 'certificate-revoked', 'certificate-expired'):
                state['reEnroll'] = True
                self._save(state)
            raise OrgError('rotate-refused', 'the server refused the rotation: %s (HTTP %s)' % (code, status))
        state['certificate'] = self._certificate(answer)
        if isinstance(answer.get('protocols'), list):
            state['protocols'] = [p for p in answer['protocols'] if isinstance(p, str)]
        state['rotate'] = False
        self._save(state)
        self.journal('rotated', serial=state['certificate']['serial'], notAfter=state['certificate']['notAfter'])
        return self.describe()

    def rotation_due(self, state: dict) -> bool:
        cert = state['certificate']
        start, end = strict_time(cert['notBefore']) / 1000, strict_time(cert['notAfter']) / 1000
        return bool(state.get('rotate')) or self.clock() >= start + (end - start) * 2 / 3

    def expired(self, state: dict) -> bool:
        return self.clock() >= strict_time(state['certificate']['notAfter']) / 1000

    def _require(self) -> dict:
        state = self.state()
        if state is None:
            raise OrgError('not-enrolled', 'this machine is not enrolled in that organization')
        if self.expired(state):
            raise OrgError('certificate-expired', 'the certificate expired: enroll again with a new token')
        return state

    # -- check-in ----------------------------------------------------------------------------------------------
    def check_in_document(self, state: dict) -> dict:
        from configuration import VERSION
        with self._db() as conn:
            last = conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?', (state['epoch'],)).fetchone()['s'] or 0
            floor = conn.execute("SELECT MIN(seq) AS s FROM events WHERE epoch = ? AND state = 'pending'",
                                 (state['epoch'],)).fetchone()['s']
            dropped = self._meta(conn, 'dropped', [])
        doc = {'protocol': DEVICE_PROTOCOL, 'kind': 'check-in', 'protocols': [DEVICE_PROTOCOL],
               'device': {'id': self.keys.device_id}, 'sequenceNum': state['sequenceNum'] + 1, 'at': iso(self.clock()),
               'agent': {'version': VERSION, 'buildChannel': 'official'},
               'health': {'state': 'healthy'}, 'logging': {'enabled': True},
               'collectors': [{'collector': {'id': COLLECTOR_ID, 'epoch': state['epoch']}, 'source': 'meter',
                               'lastSeq': last, 'bufferedFrom': floor if floor is not None else last + 1,
                               'dropped': [{'fromSeq': d['fromSeq'], 'toSeq': d['toSeq']} for d in dropped
                                           if d.get('epoch') == state['epoch'] and not d.get('acked')],
                               'healthy': True}],
               'policy': {'layers': [{'source': 'server', 'revision': 0, 'status': 'applied'}]},
               'certificate': {'serial': state['certificate']['serial'], 'notAfter': state['certificate']['notAfter']}}
        if state.get('fullState'):
            doc['fullState'] = True
        return doc

    def check_in(self) -> dict:
        state = self._require()
        doc = self.check_in_document(state)
        status, answer = self._signed(state, self.paths['check-in'], doc)
        state['sequenceNum'] = doc['sequenceNum']          # a sequence number is spent once sent, answered or not
        if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'check-in-response':
            code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
            state['lastCheckIn'] = {'at': iso(self.clock()), 'result': code}
            state['nextCheckInAt'] = int(self.clock()) + min(state['checkInSeconds'], 300)
            if code in ('certificate-revoked', 'certificate-expired', 'certificate-unknown'):
                state['reEnroll'] = True
            self._save(state)
            self.journal('check-in-failed', status=status, code=code)
            return {'ok': False, 'code': code, 'status': status}
        flags = answer.get('flags') if isinstance(answer.get('flags'), list) else []
        state['fullState'] = 'report_full_state' in flags
        nxt = answer.get('nextCheckInSeconds')
        state['checkInSeconds'] = nxt if isinstance(nxt, int) and 30 <= nxt <= 86400 else state['checkInSeconds']
        state['nextCheckInAt'] = int(self.clock()) + state['checkInSeconds']
        if strict_time(answer.get('collectionStart')) is not None:
            state['collectionStart'] = answer['collectionStart']
        action = (answer.get('certificate') or {}).get('action') if isinstance(answer.get('certificate'), dict) else None
        state['rotate'] = state.get('rotate') or action == 'rotate'
        state['reEnroll'] = action == 're-enroll'
        server_at = strict_time(answer.get('at'))
        state['lastCheckIn'] = {'at': iso(self.clock()), 'result': 'ok',
                                'skewSeconds': None if server_at is None else round(server_at / 1000 - self.clock())}
        self._save(state)
        self.journal('checked-in', sequenceNum=doc['sequenceNum'], action=action, flags=flags)
        return {'ok': True, 'nextCheckInSeconds': state['checkInSeconds'], 'action': action}

    # -- the outbox --------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, data: dict, *, occurred: str, key: Optional[str] = None, source: str = 'meter',
                monotonic: str = '0') -> Optional[dict]:
        """Persists one event of this enrollment's stream once (`key` dedupes), with its id and the next seq."""
        state = self._require()
        with self._db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                if key and conn.execute('SELECT 1 FROM events WHERE key = ?', (key,)).fetchone():
                    conn.execute('COMMIT')
                    return None
                seq = (conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?',
                                    (state['epoch'],)).fetchone()['s'] or 0) + 1
                event = {'protocol': ACTIVITY_PROTOCOL, 'eventId': ulid(int(self.clock() * 1000)), 'kind': kind,
                         'source': source, 'collector': {'id': COLLECTOR_ID, 'epoch': state['epoch']}, 'seq': seq,
                         'occurred': occurred, 'bootId': _BOOT, 'monotonicNs': monotonic, 'data': data}
                conn.execute('INSERT INTO events(epoch, seq, event_id, kind, key, body) VALUES(?, ?, ?, ?, ?, ?)',
                             (state['epoch'], seq, event['eventId'], kind, key, json.dumps(event, sort_keys=True)))
                self._bound(conn, state)
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
        return event

    def _bound(self, conn, state: dict) -> None:
        """Past MAX_PENDING the oldest pending events are dropped, from the head, and one `buffer.overflow` names them
        (activity.md, streams). It never renumbers."""
        pending = conn.execute("SELECT COUNT(*) AS n FROM events WHERE state = 'pending'").fetchone()['n']
        if pending <= MAX_PENDING:
            return
        excess = pending - MAX_PENDING + 1
        rows = conn.execute("SELECT epoch, seq, kind FROM events WHERE state = 'pending' AND epoch = ? ORDER BY seq "
                            "LIMIT ?", (state['epoch'], excess)).fetchall()
        if not rows:
            return
        first, last = rows[0]['seq'], rows[-1]['seq']
        by_kind: Dict[str, int] = {}
        for row in rows:
            by_kind[row['kind']] = by_kind.get(row['kind'], 0) + 1
        conn.execute("UPDATE events SET state = 'dropped' WHERE epoch = ? AND seq BETWEEN ? AND ? AND state = "
                     "'pending'", (state['epoch'], first, last))
        dropped = self._meta(conn, 'dropped', [])
        dropped.append({'epoch': state['epoch'], 'fromSeq': first, 'toSeq': last})
        self._set_meta(conn, 'dropped', dropped[-50:])
        seq = conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?', (state['epoch'],)).fetchone()['s'] + 1
        event = {'protocol': ACTIVITY_PROTOCOL, 'eventId': ulid(), 'kind': 'buffer.overflow', 'source': 'meter',
                 'collector': {'id': COLLECTOR_ID, 'epoch': state['epoch']}, 'seq': seq, 'occurred': iso(self.clock()),
                 'bootId': _BOOT, 'monotonicNs': str(time.monotonic_ns()),
                 'data': {'dropped': {'events': len(rows), 'ranges': [{'fromSeq': first, 'toSeq': last}],
                                      'byKind': [{'kind': k, 'count': n} for k, n in sorted(by_kind.items())]}}}
        conn.execute('INSERT INTO events(epoch, seq, event_id, kind, body) VALUES(?, ?, ?, ?, ?)',
                     (state['epoch'], seq, event['eventId'], 'buffer.overflow', json.dumps(event, sort_keys=True)))

    def _slice(self, conn) -> Tuple[Optional[int], List[sqlite3.Row]]:
        """The next batch: the oldest stream with pending events, its consecutive run from the oldest, at most 1000."""
        head = conn.execute("SELECT epoch, seq FROM events WHERE state = 'pending' ORDER BY epoch, seq LIMIT 1").fetchone()
        if head is None:
            return None, []
        rows = conn.execute("SELECT seq, event_id, body FROM events WHERE state = 'pending' AND epoch = ? AND seq >= ? "
                            "ORDER BY seq LIMIT ?", (head['epoch'], head['seq'], MAX_BATCH)).fetchall()
        run = []
        for row in rows:
            if run and row['seq'] != run[-1]['seq'] + 1:
                break
            run.append(row)
        return head['epoch'], run

    def flush(self, max_batches: int = 20) -> dict:
        """Sends pending events, batch by batch, until the outbox is empty, a batch fails, or the backoff holds."""
        state = self._require()
        sent = {'batches': 0, 'accepted': 0, 'duplicates': 0, 'rejected': 0}
        with self._db() as conn:
            retry = self._meta(conn, 'retry', {'attempts': 0, 'nextAt': 0})
            if retry['nextAt'] > self.clock():
                return dict(sent, held='backoff', nextAttempt=iso(retry['nextAt']))
            for _ in range(max_batches):
                epoch, run = self._slice(conn)
                if not run:
                    break
                inflight = self._meta(conn, 'inflight')
                span = [epoch, run[0]['seq'], run[-1]['seq']]
                batch_id = inflight['batchId'] if inflight and inflight.get('span') == span else ulid()
                self._set_meta(conn, 'inflight', {'batchId': batch_id, 'span': span})
                doc = {'protocol': ACTIVITY_PROTOCOL, 'device': {'id': self.keys.device_id}, 'batchId': batch_id,
                       'sent': iso(self.clock()), 'events': [json.loads(r['body']) for r in run]}
                status, ack = self._signed(state, self.paths['batches'], doc)
                if status != 200 or not isinstance(ack, dict):
                    code = _error_code(ack) or ('unreachable' if status == 0 else 'http-%d' % status)
                    retry = {'attempts': retry['attempts'] + 1, 'nextAt': self._backoff(retry['attempts'])}
                    self._set_meta(conn, 'retry', retry)
                    self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': code, 'status': status,
                                                       'events': len(run)})
                    self.journal('batch-failed', status=status, code=code, events=len(run), batchId=batch_id)
                    return dict(sent, held=code, nextAttempt=iso(retry['nextAt']))
                counts = self._consume(conn, epoch, run, ack)
                retry = {'attempts': 0, 'nextAt': 0}
                self._set_meta(conn, 'retry', retry)
                self._set_meta(conn, 'inflight', None)
                self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': 'ok', 'status': 200,
                                                   'events': len(run), **counts})
                self.journal('batch-sent', batchId=batch_id, events=len(run), **counts)
                sent['batches'] += 1
                for name in ('accepted', 'duplicates', 'rejected'):
                    sent[name] += counts[name]
        return sent

    def _consume(self, conn, epoch: int, run: List[sqlite3.Row], ack: dict) -> dict:
        """Applies an ack: rejected events are consumed with their reason; every seq up to the stream's ack, and in
        its `held` ranges, is consumed. What the ack does not name stays pending and goes in a later batch."""
        rejected = [r for r in ack.get('rejected', []) if isinstance(r, dict)] if isinstance(ack.get('rejected'), list) else []
        for item in rejected:
            conn.execute("UPDATE events SET state = 'rejected', reason = ? WHERE event_id = ?",
                         (str(item.get('reason'))[:40], item.get('eventId')))
        for entry in ack.get('acks', []) if isinstance(ack.get('acks'), list) else []:
            collector = entry.get('collector') if isinstance(entry, dict) else None
            if not isinstance(collector, dict) or collector.get('id') != COLLECTOR_ID:
                continue
            stream = collector.get('epoch')
            seq = entry.get('seq')
            if isinstance(seq, int):
                conn.execute("UPDATE events SET state = 'acked' WHERE epoch = ? AND seq <= ? AND state = 'pending'",
                             (stream, seq))
            for held in entry.get('held', []) if isinstance(entry.get('held'), list) else []:
                if isinstance(held, dict) and isinstance(held.get('fromSeq'), int) and isinstance(held.get('toSeq'), int):
                    conn.execute("UPDATE events SET state = 'acked' WHERE epoch = ? AND seq BETWEEN ? AND ? AND "
                                 "state = 'pending'", (stream, held['fromSeq'], held['toSeq']))
        dropped = self._meta(conn, 'dropped', [])
        overflow_acked = {(json.loads(r['body'])['data']['dropped']['ranges'][0]['fromSeq'])
                          for r in conn.execute("SELECT body FROM events WHERE kind = 'buffer.overflow' AND state != "
                                                "'pending' AND epoch = ?", (epoch,))}
        for d in dropped:
            if d.get('epoch') == epoch and d['fromSeq'] in overflow_acked:
                d['acked'] = True
        self._set_meta(conn, 'dropped', dropped)
        num = lambda name: ack.get(name) if isinstance(ack.get(name), int) else 0
        return {'accepted': num('accepted'), 'duplicates': num('duplicates'), 'rejected': len(rejected)}

    # -- share registrations and grant usage -------------------------------------------------------------------
    def accepts_shares(self, state: dict) -> bool:
        return SHARE_PROTOCOL in (state.get('protocols') or [])

    def register_shares(self) -> dict:
        """Derives this enrollment's registrations from the applied estate revision and delta entries and sends what
        changed, what is due again (weekly) and what waits on a backoff. Nothing at all before the server lists
        `share-registration/1`."""
        state = self._require()
        if not self.accepts_shares(state):
            return {'sent': 0, 'held': 'protocol-not-listed'}
        record, entries = applied_records(self.node)
        if record is None:
            return {'sent': 0, 'held': 'no-applied-record'}
        record_digest = record.get('digest') if isinstance(record.get('digest'), str) else None
        now = int(self.clock())
        out = {'sent': 0, 'degraded': [], 'answers': {}}
        with self._db() as conn:
            previous = [r['grant_id'] for r in conn.execute(
                "SELECT grant_id, doc, answer FROM registrations") if json.loads(r['doc']).get('state') == 'active'
                and share_registration.gate(json.loads(r['answer']) if r['answer'] else None) == 'send']
            derived = share_registration.derive(record, entries, self.org_id, previous)
            out['degraded'] = derived['degraded']
            for registration in derived['registrations']:
                grant_id, digest = registration['grantId'], share_registration.digest(registration)
                row = conn.execute('SELECT * FROM registrations WHERE grant_id = ?', (grant_id,)).fetchone()
                if row is not None and row['digest'] == digest:
                    if row['gate'] == 'hold-degraded' and row['record_digest'] == record_digest:
                        continue                           # a typed refusal waits for the record to change
                    if row['gate'] == 'send' and row['sent_at'] and now - row['sent_at'] < share_registration.RESEND_SECONDS:
                        continue
                    if row['gate'] == 'hold' and row['next_at'] > now:
                        continue
                if row is not None and row['digest'] == digest and registration['state'] == 'withdrawn' \
                        and row['gate'] == 'send':
                    continue                               # a withdrawal is final once taken
                status, answer = self._signed(state, self.paths['share-registration'], registration)
                if status == 200 and isinstance(answer, dict):
                    shaped = {'status': 200, 'receipt': answer}
                elif status == 401:
                    shaped = {'status': 401, 'code': _error_code(answer)}
                elif isinstance(answer, dict) and isinstance(answer.get('error'), dict):
                    shaped = {'status': status, 'body': answer}
                else:
                    shaped = {'status': status}
                gate = share_registration.gate(shaped)
                attempts = (row['attempts'] + 1) if row is not None and gate == 'hold' else 0
                conn.execute(
                    'INSERT INTO registrations(grant_id, doc, digest, answer, gate, record_digest, sent_at, attempts, '
                    'next_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(grant_id) DO UPDATE SET doc = excluded.doc, '
                    'digest = excluded.digest, answer = excluded.answer, gate = excluded.gate, record_digest = '
                    'excluded.record_digest, sent_at = excluded.sent_at, attempts = excluded.attempts, next_at = '
                    'excluded.next_at',
                    (grant_id, json.dumps(registration, sort_keys=True), digest, json.dumps(shaped, sort_keys=True),
                     gate, record_digest, now, attempts, self._backoff(attempts) if gate == 'hold' else 0))
                self.journal('share-registration', grantState=registration['state'], status=status, gate=gate,
                             code=_error_code(answer) if status != 200 else (answer or {}).get('state'))
                out['sent'] += 1
                out['answers'][grant_id] = gate
        return out

    def usage_days(self, until_ms: int) -> List[dict]:
        """The meter's settled grant-check receipts summed per (UTC day, grant): ended days only."""
        if self.meter_db is None or not Path(self.meter_db).exists():
            return []
        import paths as _paths
        conn = sqlite3.connect(_paths.sqlite_uri(self.meter_db), uri=True, timeout=30)
        try:
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch') AS day, grant_id, COUNT(*) AS calls, SUM(settled_usd) AS cost "
                "FROM reservations WHERE settled_usd IS NOT NULL AND created_at < ? GROUP BY day, grant_id "
                "ORDER BY day, grant_id", ((until_ms // 86400000) * 86400,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [{'day': r[0], 'grantId': r[1], 'calls': int(r[2]), 'costUsd': round(float(r[3]), 6)} for r in rows]

    def collect_grant_usage(self) -> dict:
        """Enqueues one `grant.usage` per (ended UTC day, grant) for the share grants the applied revision places in
        this organization's context, from the collection start on, each only when the grant's registration answer
        lets it (`gate`); a held grant's days wait in the meter and are enqueued once the gate opens."""
        state = self._require()
        start = strict_time(state.get('collectionStart'))
        if start is None:
            return {'enqueued': 0, 'held': 'no-collection-start'}
        record, _ = applied_records(self.node)
        context = 'org:' + self.org_id
        grants = {g['meta']['id'] for g in (record or {}).get('shareGrants', []) if isinstance(g, dict)
                  and g.get('context') == context and isinstance(g.get('meta'), dict)
                  and share_registration.is_share_grant_id(g['meta'].get('id'))}
        with self._db() as conn:
            gates = {r['grant_id']: r['gate'] for r in conn.execute('SELECT grant_id, gate FROM registrations')}
        enqueued, held = 0, set()
        for day in self.usage_days(int(self.clock() * 1000)):
            if day['grantId'] not in grants or strict_time(day['day'] + 'T00:00:00Z') < start:
                continue
            if gates.get(day['grantId']) != 'send':
                held.add(day['grantId'])
                continue
            data = {'day': day['day'], 'grantId': day['grantId'], 'context': context, 'calls': day['calls'],
                    'unpricedCalls': 0, 'costUsd': day['costUsd']}
            if self.enqueue('grant.usage', data, occurred=day['day'] + 'T00:00:00Z',
                            key='grant.usage|%s|%s' % (day['day'], day['grantId'])):
                enqueued += 1
        if enqueued:
            self.journal('grant-usage-enqueued', events=enqueued)
        return {'enqueued': enqueued, 'heldGrants': len(held)}

    # -- the schedule ------------------------------------------------------------------------------------------
    def tick(self) -> dict:
        """What is due, in order: rotation, check-in, share registrations, grant usage, the outbox. Each step's
        failure is recorded and the next still runs."""
        state = self.state()
        if state is None:
            return {'org': self.org_id, 'result': 'not-enrolled'}
        if self.expired(state) or state.get('reEnroll'):
            self.journal('needs-enrollment', expired=self.expired(state))
            return {'org': self.org_id, 'result': 'needs-enrollment'}
        report: Dict[str, Any] = {'org': self.org_id}
        steps = [('rotate', lambda: self.rotate() if self.rotation_due(self.state()) else 'not-due'),
                 ('checkIn', lambda: self.check_in() if self.clock() >= (self.state().get('nextCheckInAt') or 0)
                  else 'not-due'),
                 ('shares', self.register_shares), ('grantUsage', self.collect_grant_usage), ('outbox', self.flush)]
        for name, step in steps:
            try:
                result = step()
                report[name] = {'result': 'done'} if isinstance(result, dict) and 'device' in result else result
            except OrgError as error:
                report[name] = {'error': error.code, 'message': str(error)[:200]}
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                report[name] = {'error': type(error).__name__}
        return report

    # -- status and leaving ------------------------------------------------------------------------------------
    def describe(self) -> dict:
        state = self.state()
        if state is None:
            return {'org': self.org_id, 'enrolled': False}
        not_after = strict_time(state['certificate']['notAfter']) / 1000
        out = {'org': self.org_id, 'enrolled': True, 'server': state['server'], 'device': state['device'],
               'epoch': state['epoch'], 'certificate': {'serial': state['certificate']['serial'],
                                                        'notAfter': state['certificate']['notAfter'],
                                                        'daysLeft': round((not_after - self.clock()) / 86400, 2),
                                                        'rotationDue': self.rotation_due(state)},
               'protocols': state.get('protocols', []), 'shareRegistration': self.accepts_shares(state),
               'collectionStart': state.get('collectionStart'), 'lastCheckIn': state.get('lastCheckIn'),
               'checkInSeconds': state.get('checkInSeconds'), 'needsEnrollment': self.expired(state)
               or bool(state.get('reEnroll'))}
        with self._db() as conn:
            counts = {r['state']: r['n'] for r in conn.execute('SELECT state, COUNT(*) AS n FROM events GROUP BY state')}
            out['outbox'] = {'pending': counts.get('pending', 0), 'acked': counts.get('acked', 0),
                             'rejected': counts.get('rejected', 0), 'dropped': counts.get('dropped', 0),
                             'lastBatch': self._meta(conn, 'lastBatch'), 'retry': self._meta(conn, 'retry')}
            out['registrations'] = {r['gate']: r['n'] for r in conn.execute(
                'SELECT gate, COUNT(*) AS n FROM registrations GROUP BY gate')}
        degraded = []
        if out['needsEnrollment']:
            degraded.append('org:needs-enrollment')
        if not out['shareRegistration']:
            degraded.append('org:share-registration-not-listed')
        if out['registrations'].get('hold-degraded'):
            degraded.append('org:share-registration-refused')
        if out['outbox']['pending'] and (out['outbox']['retry'] or {}).get('attempts'):
            degraded.append('org:outbox-retrying')
        out['degraded'] = degraded
        return out

    def leave(self) -> dict:
        """Removes what enrollment created, locally: this organization's folder. The device keys stay (they are the
        machine's), and the server keeps its records until an administrator retires the device."""
        if not self.folder.exists():
            return {'org': self.org_id, 'left': False, 'reason': 'not-enrolled'}
        if self.folder.is_symlink() or not self.folder.resolve().is_relative_to((self.node / 'orgs').resolve()):
            raise OrgError('refused', 'the organization folder is not inside this node')
        shutil.rmtree(self.folder)
        return {'org': self.org_id, 'left': True}


def enrolled_orgs(node: Path) -> List[str]:
    folder = Path(node) / 'orgs'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and ORG_ID.fullmatch(p.name)
                  and (p / 'enrollment.json').exists())
