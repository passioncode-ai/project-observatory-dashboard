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

Each enrollment has device keys of its own (devices.md), in `<node>/orgs/<org.id>/keys/` (`node/device_keys.py`): an
owner-only PKCS#8 file, declared as such (`storage: file`, `exportable: true`, CT-65); the certificate request is
signed by its `#sig` key, and every request after enrollment carries its `fabric-device` HTTP signature, `keyid` the
certificate's serial (`http_signature.py`). A hardware key store (the Keychain helper, DEC-0053) plugs in through
`Enrollment(keys=...)` with its own `storage`. Every command that changes an enrollment holds its lock
(`<node>/orgs/.locks/<org.id>.lock`) and reads the state again under it. A share registration is derived only from the estate revision and the revocation-delta entries
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
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import osprivacy
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
#: Dropping happens in steps, so a burst past the bound ends in one `buffer.overflow`, not one per event.
OVERFLOW_HEADROOM = 1000
BACKOFF_FLOOR, BACKOFF_CEILING = 30, 3600
#: A batch the receiver refuses for what it is (malformed, too large, another device's) is never sent again as it is:
#: retrying it would hold the stream forever. Every other failure (409 `activation-closed`, 429, 5xx, no answer) is
#: retried with backoff under the same `batchId`.
PERMANENT_BATCH_STATUSES = frozenset({400, 413, 422})
PERMANENT_BATCH_CODES = frozenset({'device-mismatch', 'payload-too-large', 'batch-invalid', 'collector-mismatch'})
#: An enrollment refused because the server's policy does not take this key's storage (CT-65: a server MAY refuse a
#: file-held key by policy): a named state, not a retry.
STORAGE_REFUSALS = frozenset({'key-storage-refused', 'key-storage-not-allowed', 'storage-not-allowed',
                              'attestation-required', 'key-not-hardware'})
#: The refusals that mean this machine is no longer in the organization (OBS-204), each only with HTTP 401 as the
#: contract sends them (devices.md, the step table): its certificate was revoked (an administrator removed the machine,
#: or the organization was closed and every machine's certificate revoked), or the server no longer knows it and no
#: previous certificate is left to fall back to (CT-65; the fallback runs first). The contract gives no code that
#: tells a closed organization from one revoked machine, so neither does this. The enrollment ends there: nothing more
#: is sent, its buffered events are dropped with one count recorded, and only `org leave` or a new token's
#: `org enroll` follow. A 5xx is never terminal: it is retried as any other failure.
REMOVED_CODES = frozenset({'certificate-revoked', 'certificate-unknown'})
#: `unknown-org`, a deleted organization, is not in the contract yet (PROVISIONAL until it is): a 4xx answer with it is
#: terminal only when it is answered again at least this long after the first, with no successful request between.
#: Until then the enrollment is retried as usual and its status says the server does not know the organization.
UNKNOWN_ORG = 'unknown-org'
UNKNOWN_ORG_CONFIRM_SECONDS = 3600
#: The certificate refusals that end the enrollment: the machine needs a new token. `REMOVED_CODES` end it only with
#: HTTP 401 (`_ends`).
ENDING_CODES = frozenset({'epoch-superseded', 'certificate-expired'}) | REMOVED_CODES


def _ends(status: int, code: Optional[str]) -> bool:
    return code in ('epoch-superseded', 'certificate-expired') or (status == 401 and code in REMOVED_CODES)
#: `org status`, Health and doctor warn this long before the certificate expires (OBS-199). A node offline past its
#: certificate's lifetime needs a new enrollment token until the contract's re-enrollment rule lands.
EXPIRY_WARNING_SECONDS = 3 * 86400
TOKEN_SLOT_PLACEHOLDER = 'PROJECT/ENV/NAME'
JOURNAL_BYTES = 256 * 1024
_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
_BOOT = 'boot-' + secrets.token_hex(4)

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]


class OrgError(RuntimeError):
    """A refusal with a code: `{'error': code, 'message': ...}` for the terminal, never a secret. A refusal a person
    resolves carries the step in words (`next_step`) and the command it ends with (`command`)."""

    def __init__(self, code: str, message: str, *, next_step: Optional[str] = None, command: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.next_step, self.command = next_step, command


def enroll_command(org_id: str, server: Optional[str]) -> str:
    return 'project-observatory full org enroll --server %s --org %s --token-slot %s' % (
        server or 'https://ORG-SERVER', org_id, TOKEN_SLOT_PLACEHOLDER)


def notice(org_id: str, state: dict, now: float) -> dict:
    """What a person must know about one enrollment, from its saved state alone (no network, no key): the
    certificate's expiry date, its state (`valid`, `expiring` in the last three days, `expired`, `needs-enrollment`
    when the server asked for it, `removed` when the organization no longer has this machine), and, when the person
    has something to do, the step in words and the command. `org status`, Health and doctor all read this.

    `now` is this machine's clock; the expiry is judged on the server's, as the last check-in measured it
    (`skewSeconds`), the clock every request is signed on."""
    now = now + (state.get('skewSeconds') or 0)
    server = state.get('server') if isinstance(state.get('server'), str) else None
    not_after_text = str((state.get('certificate') or {}).get('notAfter') or '')
    not_after_ms = strict_time(not_after_text)
    left = (not_after_ms / 1000 - now) if not_after_ms is not None else 0.0
    out: Dict[str, Any] = {'org': org_id, 'server': server, 'notAfter': not_after_text,
                           'expiresOn': not_after_text[:10], 'daysLeft': round(left / 86400, 2)}
    enroll = enroll_command(org_id, server)
    new_token = ("ask the organization's administrator for a new enrollment token, put it in a vault slot "
                 '(python "$(project-observatory full-path)/tools/vault.py" put PROJECT ENV NAME, the token on stdin) '
                 'and run: ' + enroll)
    removed = state.get('removed')
    if isinstance(removed, dict):
        out.update(state='removed', removed=removed, command='project-observatory full org leave --org %s' % org_id,
                   enrollCommand=enroll,
                   nextStep='this machine was removed from organization %s on %s (the server answered %s): nothing '
                            'more is sent to it and what it held was dropped. Remove its local state with: '
                            'project-observatory full org leave --org %s; to join again, %s'
                            % (org_id, removed.get('at') or '?', removed.get('code') or '?', org_id, new_token))
    elif left <= 0:
        out.update(state='expired', command=enroll,
                   nextStep='the certificate expired on %s, so the organization accepts nothing from this machine; '
                            'a node offline past its certificate needs a new enrollment token: %s'
                            % (not_after_text, new_token))
    elif state.get('reEnroll'):
        out.update(state='needs-enrollment', command=enroll,
                   nextStep='the organization asked this machine to enroll again: ' + new_token)
    elif left <= EXPIRY_WARNING_SECONDS:
        out.update(state='expiring', command='project-observatory full org rotate --org %s' % org_id,
                   nextStep='the certificate expires on %s. The maintenance job renews it while the server answers; '
                            'renew it now with: project-observatory full org rotate --org %s. If this machine stays '
                            'offline past %s it needs a new enrollment token' % (not_after_text, org_id,
                                                                                 not_after_text))
    else:
        out['state'] = 'valid'
    unknown = state.get('unknownOrg')
    if isinstance(unknown, dict) and not isinstance(removed, dict):
        out['unknownOrg'] = unknown
        out['warning'] = ('the organization server does not know this organization (since %s, answer %s). Requests '
                          'are retried as usual; if it still does not know it an hour after that, this machine is '
                          'taken as removed from the organization (this code is provisional until the contract '
                          'defines it)' % (unknown.get('since') or '?', UNKNOWN_ORG))
    return out


def node_folder(home: Path) -> Path:
    """The node folder `full org` and the maintenance job use: `OBSERVATORY_NODE`, else `<home>/node`."""
    return Path(os.environ.get('OBSERVATORY_NODE') or Path(home) / 'node').expanduser()


def notices(node: Path, now: Optional[float] = None) -> List[dict]:
    """Every enrollment's `notice`, read-only: nothing is created, locked or sent. An unreadable one is named, never
    skipped."""
    now = time.time() if now is None else now
    out = []
    try:
        orgs = enrolled_orgs(node)
    except OSError as error:                               # an unreadable node folder is a state, not a crash
        return [{'org': None, 'state': 'unreadable', 'reason': type(error).__name__}]
    for org_id in orgs:
        try:
            state = Enrollment(Path(node).resolve(), org_id).state()
        except (OSError, ValueError, RuntimeError) as error:
            out.append({'org': org_id, 'state': 'unreadable', 'reason': type(error).__name__})
            continue
        if state is not None:
            out.append(notice(org_id, state, now))
    return out


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


def _protocols(doc: Any) -> Optional[List[str]]:
    """The protocol list an answer carries (DEC-0067), or None when it carries none: the held list is replaced only by
    a list that arrives (DEC-0094)."""
    value = doc.get('protocols') if isinstance(doc, dict) else None
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else None


def _key_schema_refusal(status: int, code: str, answer: Any) -> bool:
    """A 422 schema refusal whose detail points at `/key/storage` or `/key/exportable`: what a server on a contract
    before CT-65 (DEC-0094, which admits `storage: file`) answers a file-held key. Named `key-storage-refused`, not a generic
    enrollment refusal, because a new token would not help: the server or its contract has to change."""
    if status != 422 or ('schema' not in code and code not in ('invalid-request', 'request-invalid', 'validation')):
        return False
    error = answer.get('error') if isinstance(answer, dict) else None
    detail = json.dumps({k: v for k, v in error.items() if k != 'code'}) if isinstance(error, dict) else ''
    return bool(re.search(r'/key/(storage|exportable)\b', detail))


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
        self._held = threading.local()                     # re-entry is per thread: another thread waits

    # -- the lock ----------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """One writer per enrollment at a time, across processes (the CLI and the maintenance job): every command that
        changes the state or the outbox runs under it and reads the state again inside it, so a check-in can never save
        a copy older than a rotation's certificate, and a tick never recreates what `leave` removed. The lock file is
        outside the enrollment's folder, so removing the folder never frees a lock another process waits on."""
        depth = getattr(self._held, 'depth', 0)
        if depth:
            self._held.depth = depth + 1
            try:
                yield
            finally:
                self._held.depth = depth
            return
        with private_io.lock(self.node / 'orgs' / '.locks' / (self.org_id + '.lock')):
            self._held.depth = 1
            try:
                yield
            finally:
                self._held.depth = 0

    # -- state -------------------------------------------------------------------------------------------------
    @property
    def keys(self):
        """This enrollment's own device keys (devices.md: each organization enrollment of a machine gets keys of its
        own), in `<node>/orgs/<org>/keys/`: a device id and `#sig`/`#kem` pair no other organization sees."""
        if self._keys is None:
            from .device_keys import DeviceKeys
            self._keys = DeviceKeys.load_or_create(self.node.resolve() / 'orgs' / self.org_id / 'keys')
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
            fd = osprivacy.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
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
                  attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
                  active_registered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);""")
            with contextlib.suppress(sqlite3.OperationalError):    # an outbox from before the column
                conn.execute('ALTER TABLE registrations ADD COLUMN active_registered INTEGER NOT NULL DEFAULT 0')
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

    def _signed(self, state: dict, path: str, doc: dict,
                rebuild: Optional[Callable[[dict], dict]] = None) -> Tuple[int, Any]:
        """A device request signed afresh (DEC-0056): a new nonce and `created` on every attempt, `created` on the
        server's clock as the last check-in measured it (`skewSeconds`). A `signature-stale` refusal forgets that
        measure and asks for a check-in now, which measures it again. The first answer past the signature check under
        a rotated certificate lets the previous certificate go (CT-65); a `certificate-unknown` before that goes back
        to the previous one, which the server keeps valid until the new serial is first presented. A refusal that
        says the machine is no longer in the organization (`REMOVED_CODES`) ends the enrollment here, for every
        caller (OBS-204); a removed enrollment sends nothing."""
        if isinstance(state.get('removed'), dict):
            raise self._removed_error(state)
        body = json.dumps(doc, separators=(',', ':')).encode('utf-8')
        created = int(self.clock() + (state.get('skewSeconds') or 0))
        headers = http_signature.sign_request(self.keys.sign, method='POST', origin=state['server'], target=path,
                                              body=body, serial=state['certificate']['serial'], created=created)
        try:
            status, raw = self.transport(state['server'] + path, {'Content-Type': 'application/json', **headers},
                                         body, 30)
        except (OSError, ValueError) as error:
            self.journal('transport-failed', path=path, error=type(error).__name__)
            return 0, None
        answer = _json(raw)
        if status == 401:
            code = _error_code(answer)
            self.journal('request-refused', path=path, status=401, code=code)
            previous = state.get('previousCertificate')
            if code == 'signature-stale':
                state['skewSeconds'] = 0
                state['nextCheckInAt'] = 0
                self._save(state)
            elif code == 'certificate-unknown' and isinstance(previous, dict) \
                    and self.clock() + (state.get('skewSeconds') or 0) < strict_time(previous['notAfter']) / 1000:
                # The server does not know the rotated certificate; the previous one is still valid (CT-65). Back to
                # it, and the same request once more under it: only a refusal of that one can end the enrollment.
                state['certificate'] = state.pop('previousCertificate')
                self._save(state)
                self.journal('certificate-fallback', serial=state['certificate']['serial'])
                return self._signed(state, path, rebuild(state) if rebuild else doc)
        elif 200 <= status < 300 and (state.get('previousCertificate') or state.get('unknownOrg')):
            state.pop('previousCertificate', None)         # a 2xx proves the server took the new serial
            state.pop('unknownOrg', None)                  # and that it knows the organization
            self._save(state)
        code = _error_code(answer)
        if status == 401 and code in REMOVED_CODES:        # only with 401, as the contract sends them
            self._remove(state, code, status)
        elif code == UNKNOWN_ORG and 400 <= status < 500:
            self._unknown_org(state, status)
        return status, answer

    def _unknown_org(self, state: dict, status: int) -> None:
        """`unknown-org` (provisional, not in the contract yet): the first answer is recorded and retried as usual; one
        at least `UNKNOWN_ORG_CONFIRM_SECONDS` later AND in a later tick (`tickSeq`, counted by `tick`), with no
        successful request between, ends the enrollment. The tick keeps a wall clock that jumps an hour between two
        requests of one tick from removing the machine."""
        now = self.clock()
        tick = state.get('tickSeq') or 0
        first = state.get('unknownOrg')
        if not isinstance(first, dict) or not isinstance(first.get('at'), (int, float)):
            state['unknownOrg'] = {'since': iso(now), 'at': int(now), 'status': status, 'tick': tick}
            self._save(state)
            self.journal('unknown-org-seen', status=status)
        elif now - first['at'] >= UNKNOWN_ORG_CONFIRM_SECONDS and tick > (first.get('tick') or 0):
            self._remove(state, UNKNOWN_ORG, status)

    def _remove(self, state: dict, code: str, status: int) -> None:
        """The organization no longer has this machine (OBS-204): its pending events can be accepted nowhere, so they
        are dropped with one count recorded, the retry and in-flight batch are forgotten, and the state says when and
        why. `state` is the caller's own copy, changed in place, so what it saves next keeps the removal."""
        if isinstance(state.get('removed'), dict):
            return
        with self._db() as conn:
            dropped = conn.execute("UPDATE events SET state = 'dropped', key = NULL, reason = ? WHERE state = 'pending'",
                                   (('removed:' + code)[:40],)).rowcount
            conn.execute("DELETE FROM meta WHERE name IN ('retry', 'inflight')")
        state['removed'] = {'at': iso(self.clock()), 'code': code, 'status': status, 'droppedEvents': dropped}
        if isinstance(state.get('unknownOrg'), dict):
            state['removed']['firstSeen'] = state.pop('unknownOrg').get('since')
        state['reEnroll'] = True
        state.pop('pendingRotation', None)
        self._save(state)
        self.journal('removed-from-organization', code=code, status=status, droppedEvents=dropped)

    def _removed_error(self, state: dict) -> 'OrgError':
        said = notice(self.org_id, state, self.clock())
        return OrgError('removed-from-organization', 'this machine was removed from the organization on %s (%s)'
                        % (state['removed'].get('at'), state['removed'].get('code')),
                        next_step=said.get('nextStep'), command=said.get('command'))
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
                'key': {'algorithm': 'ecdsa-p256', 'storage': getattr(self.keys, 'storage', 'file'),
                        'exportable': bool(getattr(self.keys, 'exportable', True)),
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
        with self._locked():
            previous = self.state()
            # A machine removed from its organization starts over with a new token (OBS-204): the organization it
            # joins may be a new one whose epochs start again, and nothing of the old stream is carried.
            over = previous is not None and isinstance(previous.get('removed'), dict)
            status, answer = self._post(origin, self.paths['enroll'], self.enrollment_request(match.group(1)),
                                        {'Authorization': 'EnrollmentToken ' + token})
            if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'enrollment-response':
                code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
                self.journal('enroll-refused', status=status, code=code)
                if code in STORAGE_REFUSALS or 'storage' in code or _key_schema_refusal(status, code, answer):
                    raise OrgError('key-storage-refused', "the server's policy refuses this machine's %s-held device "
                                   'key: %s (HTTP %s)' % (getattr(self.keys, 'storage', 'file'), code, status))
                raise OrgError('enroll-refused', 'the server refused the enrollment: %s (HTTP %s)' % (code, status))
            cert = self._certificate(answer)
            epoch = answer.get('streamEpoch')
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1 \
                    or (previous and not over and epoch <= previous.get('epoch', 0)):
                raise OrgError('epoch-invalid', 'the stream epoch is not above every epoch issued to this device')
            interval = (answer.get('checkIn') or {}).get('intervalSeconds')
            doc = {'schema': 'org-enrollment/1', 'org': self.org_id, 'device': self.keys.device_id, 'server': origin,
                   'certificate': cert, 'epoch': epoch, 'protocols': _protocols(answer) or [],
                   'checkInSeconds': interval if isinstance(interval, int) and 30 <= interval <= 86400 else 300,
                   'collectionStart': answer.get('collectionStart') if strict_time(answer.get('collectionStart'))
                   is not None else None,
                   'policyRootKey': answer.get('policyRootKey'), 'enrolledAt': iso(self.clock()),
                   'sequenceNum': 0, 'fullState': False, 'lastCheckIn': None, 'rotate': False,
                   'reEnroll': False, 'nextCheckInAt': 0, 'skewSeconds': 0}
            if over:
                doc['previousRemoval'] = previous['removed']
            private_io.parent(self.folder)
            self._save(doc)
            with self._db() as conn:                       # a new enrollment: registrations are sent again
                if over:                                   # nothing pending survived the removal; start a new stream
                    conn.execute('DELETE FROM events')
                    conn.execute('DELETE FROM registrations')
                    conn.execute('DELETE FROM meta')
                conn.execute("UPDATE registrations SET next_at = 0, sent_at = NULL WHERE gate != 'hold-degraded'")
            self.journal('enrolled', epoch=epoch, serial=cert['serial'], notAfter=cert['notAfter'],
                         protocols=doc['protocols'], reEnrollment=previous is not None, afterRemoval=over)
            self._register_quietly()
            return self.describe()
    def rotate(self) -> dict:
        """A new certificate for the same key. The request is persisted before it is sent and sent again, unchanged,
        after a lost answer: the server answers the same request with the same certificate (CT-65)."""
        with self._locked():
            state = self._require()
            pending = state.get('pendingRotation')
            if not isinstance(pending, dict) or pending.get('serial') != state['certificate']['serial']:
                pending = {'serial': state['certificate']['serial'], 'at': iso(self.clock()),
                           'request': {'protocol': DEVICE_PROTOCOL, 'kind': 'rotation-request',
                                       'device': {'id': self.keys.device_id},
                                       'csr': base64.b64encode(self.keys.csr()).decode('ascii')}}
                state['pendingRotation'] = pending
                self._save(state)
            status, answer = self._signed(state, self.paths['rotate'], pending['request'])
            if status != 200 or not isinstance(answer, dict):
                code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
                self.journal('rotate-refused', status=status, code=code)
                if _ends(status, code):
                    state['reEnroll'] = True
                    state.pop('pendingRotation', None)
                elif status in (400, 422):                 # refused for what it is: the next rotation asks anew
                    state.pop('pendingRotation', None)
                self._save(state)
                raise OrgError('rotate-refused', 'the server refused the rotation: %s (HTTP %s)' % (code, status))
            cert = self._certificate(answer)
            if cert['serial'] != state['certificate']['serial']:
                state['previousCertificate'] = state['certificate']
            state['certificate'] = cert
            protocols = _protocols(answer)
            if protocols is not None:
                state['protocols'] = protocols
            state['rotate'] = False
            state.pop('pendingRotation', None)
            self._save(state)
            self.journal('rotated', serial=cert['serial'], notAfter=cert['notAfter'])
            return self.describe()
    def rotation_due(self, state: dict) -> bool:
        cert = state['certificate']
        start, end = strict_time(cert['notBefore']) / 1000, strict_time(cert['notAfter']) / 1000
        return bool(state.get('rotate')) or self.clock() >= start + (end - start) * 2 / 3

    def expired(self, state: dict) -> bool:
        """On the server's clock, as the last check-in measured it: the clock every request is signed on."""
        return self.clock() + (state.get('skewSeconds') or 0) >= strict_time(state['certificate']['notAfter']) / 1000

    def _require(self) -> dict:
        """The saved state, or a refusal that names what happened and the person's next step (OBS-199, OBS-204):
        a removed or expired enrollment never reaches the network."""
        state = self.state()
        if state is None:
            raise OrgError('not-enrolled', 'this machine is not enrolled in that organization')
        if isinstance(state.get('removed'), dict):
            raise self._removed_error(state)
        if self.expired(state):
            said = notice(self.org_id, state, self.clock())
            raise OrgError('certificate-expired', 'the certificate expired on %s: this machine needs a new enrollment '
                           'token' % state['certificate']['notAfter'], next_step=said.get('nextStep'),
                           command=said.get('command'))
        return state

    # -- check-in ----------------------------------------------------------------------------------------------
    def check_in_document(self, state: dict) -> dict:
        from configuration import VERSION
        with self._db() as conn:
            last = conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?', (state['epoch'],)).fetchone()['s'] or 0
            floor = conn.execute("SELECT MIN(seq) AS s FROM events WHERE epoch = ? AND state = 'pending'",
                                 (state['epoch'],)).fetchone()['s']
            dropped = self._undelivered_drops(conn, state['epoch'])
            retrying = (self._meta(conn, 'retry') or {}).get('attempts', 0) > 0
        healthy = not retrying and not dropped
        health = {'state': 'healthy'} if healthy else \
            {'state': 'degraded', 'detail': 'activity outbox retrying' if retrying else 'activity events dropped'}
        doc = {'protocol': DEVICE_PROTOCOL, 'kind': 'check-in', 'protocols': [DEVICE_PROTOCOL],
               'device': {'id': self.keys.device_id}, 'sequenceNum': state['sequenceNum'] + 1, 'at': iso(self.clock()),
               'agent': {'version': VERSION, 'buildChannel': 'official'},
               'health': health, 'logging': {'enabled': True},
               'collectors': [{'collector': {'id': COLLECTOR_ID, 'epoch': state['epoch']}, 'source': 'meter',
                               'lastSeq': last, 'bufferedFrom': floor if floor is not None else last + 1,
                               'dropped': dropped, 'healthy': healthy}],
               'policy': {'layers': [{'source': 'server', 'revision': 0, 'status': 'applied'}]},
               'certificate': {'serial': state['certificate']['serial'], 'notAfter': state['certificate']['notAfter']}}
        if state.get('fullState'):
            doc['fullState'] = True
        return doc
    def check_in(self) -> dict:
        with self._locked():
            state = self._require()
            doc = self.check_in_document(state)
            state['sequenceNum'] = doc['sequenceNum']      # a sequence number is spent once sent, answered or not
            self._save(state)
            def again(current: dict) -> dict:              # resent under the previous certificate: it names that one
                current['sequenceNum'] += 1
                self._save(current)
                return dict(doc, sequenceNum=current['sequenceNum'],
                            certificate={'serial': current['certificate']['serial'],
                                         'notAfter': current['certificate']['notAfter']})
            status, answer = self._signed(state, self.paths['check-in'], doc, again)
            if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'check-in-response':
                code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
                state['lastCheckIn'] = {'at': iso(self.clock()), 'result': code}
                state['nextCheckInAt'] = 0 if code == 'signature-stale' else \
                    int(self.clock()) + min(state['checkInSeconds'], 300)
                if _ends(status, code):
                    state['reEnroll'] = True
                self._save(state)
                self.journal('check-in-failed', status=status, code=code)
                out = {'ok': False, 'code': code, 'status': status}
                if _ends(status, code):                   # never a silent refusal: what happened and the step
                    said = notice(self.org_id, state, self.clock())
                    out.update({k: said[k] for k in ('removed', 'nextStep', 'command') if k in said})
                return out
            flags = answer.get('flags') if isinstance(answer.get('flags'), list) else []
            state['fullState'] = 'report_full_state' in flags
            nxt = answer.get('nextCheckInSeconds')
            state['checkInSeconds'] = nxt if isinstance(nxt, int) and 30 <= nxt <= 86400 else state['checkInSeconds']
            state['nextCheckInAt'] = int(self.clock()) + state['checkInSeconds']
            if strict_time(answer.get('collectionStart')) is not None:
                state['collectionStart'] = answer['collectionStart']
            protocols = _protocols(answer)                 # DEC-0094: the held list is replaced from wherever it comes
            listed_before = self.accepts_shares(state)
            if protocols is not None:
                state['protocols'] = protocols
            action = (answer.get('certificate') or {}).get('action') if isinstance(answer.get('certificate'), dict) \
                else None
            state['rotate'] = state.get('rotate') or action == 'rotate'
            state['reEnroll'] = action == 're-enroll'
            server_at = strict_time(answer.get('at'))
            skew = None if server_at is None else round(server_at / 1000 - self.clock())
            if skew is not None and abs(skew) <= 86400:
                state['skewSeconds'] = skew
            state['lastCheckIn'] = {'at': iso(self.clock()), 'result': 'ok', 'skewSeconds': skew}
            self._save(state)
            self.journal('checked-in', sequenceNum=state['sequenceNum'], action=action, flags=flags)
            newly_listed = self.accepts_shares(state) and not listed_before
        if newly_listed:                                   # the server began to take registrations: send them now
            self._register_quietly()
        return {'ok': True, 'nextCheckInSeconds': state['checkInSeconds'], 'action': action}
    # -- the outbox --------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, data: dict, *, occurred: str, key: Optional[str] = None, source: str = 'meter',
                monotonic: str = '0') -> Optional[dict]:
        """Persists one event of this enrollment's stream once (`key` dedupes), with its id and the next seq."""
        with self._locked():
            state = self._require()
            with self._db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    if key and conn.execute('SELECT 1 FROM events WHERE key = ?', (key,)).fetchone():
                        conn.execute('COMMIT')
                        return None
                    event = self._insert(conn, state['epoch'], kind, data, occurred=occurred, key=key, source=source,
                                         monotonic=monotonic)
                    self._bound(conn, state)
                    conn.execute('COMMIT')
                except BaseException:
                    conn.execute('ROLLBACK')
                    raise
            return event

    def _insert(self, conn, epoch: int, kind: str, data: dict, *, occurred: str, key: Optional[str] = None,
                source: str = 'meter', monotonic: str = '0') -> dict:
        seq = (conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?', (epoch,)).fetchone()['s'] or 0) + 1
        event = {'protocol': ACTIVITY_PROTOCOL, 'eventId': ulid(int(self.clock() * 1000)), 'kind': kind,
                 'source': source, 'collector': {'id': COLLECTOR_ID, 'epoch': epoch}, 'seq': seq,
                 'occurred': occurred, 'bootId': _BOOT, 'monotonicNs': monotonic, 'data': data}
        conn.execute('INSERT INTO events(epoch, seq, event_id, kind, key, body) VALUES(?, ?, ?, ?, ?, ?)',
                     (epoch, seq, event['eventId'], kind, key, json.dumps(event, sort_keys=True)))
        return event
    def _bound(self, conn, state: dict) -> None:
        """Past MAX_PENDING the oldest pending events are dropped down to OVERFLOW_HEADROOM under the bound, so a burst
        ends in one `buffer.overflow` (activity.md, streams). It never renumbers."""
        pending = conn.execute("SELECT COUNT(*) AS n FROM events WHERE state = 'pending'").fetchone()['n']
        if pending <= MAX_PENDING:
            return
        rows = conn.execute("SELECT seq, kind FROM events WHERE state = 'pending' AND epoch = ? AND kind != "
                            "'buffer.overflow' ORDER BY seq LIMIT ?",
                            (state['epoch'], pending - (MAX_PENDING - OVERFLOW_HEADROOM))).fetchall()
        self._drop(conn, state['epoch'], rows)

    def _drop(self, conn, epoch: int, rows: List[sqlite3.Row]) -> None:
        """Consumes `rows` (seq, kind) as dropped and names them in one `buffer.overflow`: the newest one not yet sent
        takes the new ranges; otherwise a new one. A dropped event's key is freed, so a `grant.usage` day is enqueued
        again once it may be sent."""
        if not rows:
            return
        seqs = sorted(r['seq'] for r in rows)
        conn.executemany("UPDATE events SET state = 'dropped', key = NULL WHERE epoch = ? AND seq = ?",
                         [(epoch, q) for q in seqs])
        ranges: List[dict] = []
        for q in seqs:
            if ranges and ranges[-1]['toSeq'] + 1 == q:
                ranges[-1]['toSeq'] = q
            else:
                ranges.append({'fromSeq': q, 'toSeq': q})
        by_kind: Dict[str, int] = {}
        for row in rows:
            by_kind[row['kind']] = by_kind.get(row['kind'], 0) + 1
        inflight = self._meta(conn, 'inflight') or {}
        span = inflight.get('span') or [None, 0, -1]
        target = conn.execute("SELECT seq, body FROM events WHERE epoch = ? AND kind = 'buffer.overflow' AND state = "
                              "'pending' ORDER BY seq DESC LIMIT 1", (epoch,)).fetchone()
        if target is not None and not (span[0] == epoch and span[1] <= target['seq'] <= span[2]):
            event = json.loads(target['body'])
            held = event['data']['dropped']
            merged = sorted(held['ranges'] + ranges, key=lambda r: r['fromSeq'])
            held['ranges'] = []
            for r in merged:
                if held['ranges'] and held['ranges'][-1]['toSeq'] + 1 >= r['fromSeq']:
                    held['ranges'][-1]['toSeq'] = max(held['ranges'][-1]['toSeq'], r['toSeq'])
                else:
                    held['ranges'].append(dict(r))
            held['events'] += len(rows)
            kinds = {k['kind']: k['count'] for k in held['byKind']}
            for k, n in by_kind.items():
                kinds[k] = kinds.get(k, 0) + n
            held['byKind'] = [{'kind': k, 'count': n} for k, n in sorted(kinds.items())]
            conn.execute('UPDATE events SET body = ? WHERE epoch = ? AND seq = ?',
                         (json.dumps(event, sort_keys=True), epoch, target['seq']))
            return
        self._insert(conn, epoch, 'buffer.overflow',
                     {'dropped': {'events': len(rows), 'ranges': ranges,
                                  'byKind': [{'kind': k, 'count': n} for k, n in sorted(by_kind.items())]}},
                     occurred=iso(self.clock()), monotonic=str(time.monotonic_ns()))

    def _undelivered_drops(self, conn, epoch: int) -> List[dict]:
        """The dropped ranges whose `buffer.overflow` the server has not taken yet: what a check-in reports."""
        out: List[dict] = []
        for row in conn.execute("SELECT body FROM events WHERE epoch = ? AND kind = 'buffer.overflow' AND state = "
                                "'pending' ORDER BY seq", (epoch,)):
            out.extend({'fromSeq': r['fromSeq'], 'toSeq': r['toSeq']}
                       for r in json.loads(row['body'])['data']['dropped']['ranges'])
        return out
    def _slice(self, conn, limit: int = MAX_BATCH) -> Tuple[Optional[int], List[sqlite3.Row]]:
        """The next batch: the oldest stream with pending events, its consecutive run from the oldest, at most
        `limit`."""
        head = conn.execute("SELECT epoch, seq FROM events WHERE state = 'pending' ORDER BY epoch, seq LIMIT 1").fetchone()
        if head is None:
            return None, []
        rows = conn.execute("SELECT seq, event_id, kind, body FROM events WHERE state = 'pending' AND epoch = ? AND "
                            "seq >= ? ORDER BY seq LIMIT ?", (head['epoch'], head['seq'], limit)).fetchall()
        run = []
        for row in rows:
            if run and row['seq'] != run[-1]['seq'] + 1:
                break
            run.append(row)
        return head['epoch'], run
    def flush(self, max_batches: int = 20) -> dict:
        """Sends pending events, batch by batch, until the outbox is empty, a batch fails, or the backoff holds.

        A `grant.usage` whose grant's registration answer no longer lets it (`gate` is checked again at send) ends the
        batch before it; at the head of the stream it is dropped and named in a `buffer.overflow`, and the day is
        enqueued again once the gate opens. A batch refused for what it is (400, 422, `device-mismatch`) is consumed
        as rejected; a 413 halves the batch size first. A 200 that acknowledges none of the batch is held and retried
        under the same `batchId`."""
        with self._locked():
            state = self._require()
            sent = {'batches': 0, 'accepted': 0, 'duplicates': 0, 'rejected': 0}
            with self._db() as conn:
                retry = self._meta(conn, 'retry', {'attempts': 0, 'nextAt': 0})
                if retry['nextAt'] > self.clock():
                    return dict(sent, held='backoff', nextAttempt=iso(retry['nextAt']))
                gates = {r['grant_id']: r['gate'] for r in conn.execute('SELECT grant_id, gate FROM registrations')}
                for _ in range(max_batches * 4):
                    if sent['batches'] >= max_batches:
                        break
                    limit = self._meta(conn, 'batchLimit', MAX_BATCH)
                    epoch, run = self._slice(conn, limit)
                    if not run:
                        break
                    cut = next((i for i, r in enumerate(run) if r['kind'] == 'grant.usage'
                                and gates.get(json.loads(r['body'])['data'].get('grantId')) != 'send'), None)
                    if cut == 0:
                        self._drop(conn, epoch, run[:1])
                        self.journal('grant-usage-withheld', seq=run[0]['seq'])
                        continue
                    run = run[:cut] if cut else run
                    inflight = self._meta(conn, 'inflight')
                    span = [epoch, run[0]['seq'], run[-1]['seq']]
                    batch_id = inflight['batchId'] if inflight and inflight.get('span') == span else ulid()
                    self._set_meta(conn, 'inflight', {'batchId': batch_id, 'span': span})
                    doc = {'protocol': ACTIVITY_PROTOCOL, 'device': {'id': self.keys.device_id}, 'batchId': batch_id,
                           'sent': iso(self.clock()), 'events': [json.loads(r['body']) for r in run]}
                    status, ack = self._signed(state, self.paths['batches'], doc)
                    if isinstance(state.get('removed'), dict):  # nothing to retry: the organization is gone
                        return dict(sent, held='removed', removed=state['removed'])
                    code = None if status == 200 and isinstance(ack, dict) else \
                        _error_code(ack) or ('unreachable' if status == 0 else 'http-%d' % status)
                    # A refusal that names the organization or the certificate is not about the batch's content: it
                    # is held and retried, never rejected, whatever its status (a 401 removal returned above).
                    about_us = code in REMOVED_CODES or code == UNKNOWN_ORG
                    if status == 413 and len(run) > 1 and not about_us:
                        self._set_meta(conn, 'batchLimit', max(1, len(run) // 2))
                        self._set_meta(conn, 'inflight', None)
                        self.journal('batch-too-large', events=len(run), batchId=batch_id)
                        continue
                    if code is not None and not about_us and (status in PERMANENT_BATCH_STATUSES
                                                              or code in PERMANENT_BATCH_CODES):
                        conn.executemany("UPDATE events SET state = 'rejected', reason = ? WHERE epoch = ? AND seq = ?",
                                         [(code[:40], epoch, r['seq']) for r in run])
                        self._set_meta(conn, 'inflight', None)
                        self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': code, 'status': status,
                                                           'events': len(run), 'permanent': True})
                        self.journal('batch-refused', status=status, code=code, events=len(run), batchId=batch_id)
                        sent['rejected'] += len(run)
                        continue
                    if code is None:
                        counts = self._consume(conn, epoch, run, ack)
                        left = conn.execute("SELECT COUNT(*) AS n FROM events WHERE epoch = ? AND seq BETWEEN ? AND ? "
                                            "AND state = 'pending'", (epoch, span[1], span[2])).fetchone()['n']
                        if left == len(run):
                            code = 'not-acknowledged'
                    if code is not None:
                        retry = {'attempts': retry['attempts'] + 1, 'nextAt': self._backoff(retry['attempts'])}
                        self._set_meta(conn, 'retry', retry)
                        self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': code, 'status': status,
                                                           'events': len(run)})
                        self.journal('batch-failed', status=status, code=code, events=len(run), batchId=batch_id)
                        return dict(sent, held=code, nextAttempt=iso(retry['nextAt']))
                    retry = {'attempts': 0, 'nextAt': 0}
                    self._set_meta(conn, 'retry', retry)
                    self._set_meta(conn, 'inflight', None)
                    self._set_meta(conn, 'batchLimit', min(MAX_BATCH, limit * 2))
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
            conn.execute("UPDATE events SET state = 'rejected', reason = ? WHERE event_id = ? AND state = 'pending'",
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
        num = lambda name: ack.get(name) if isinstance(ack.get(name), int) else 0
        return {'accepted': num('accepted'), 'duplicates': num('duplicates'), 'rejected': len(rejected)}
    # -- share registrations and grant usage -------------------------------------------------------------------
    def accepts_shares(self, state: dict) -> bool:
        return SHARE_PROTOCOL in (state.get('protocols') or [])

    def register_shares(self) -> dict:
        """Derives this enrollment's registrations from the applied estate revision and delta entries and sends what
        changed, what is due again (weekly) and what waits on a backoff. Nothing at all before the server lists
        `share-registration/1`.

        `previouslyRegistered` is, as share-registration.md reads, the grant ids this node registered active toward
        this enrollment: every id whose active registration the server took (`active_registered`), every id whose
        active registration went out without an answer that refused it, and every id whose last registration is still
        held (`hold`), so a held withdrawal is derived, and sent, again."""
        with self._locked():
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
                previous = sorted(r['grant_id'] for r in conn.execute(
                    'SELECT grant_id, doc, gate, active_registered FROM registrations')
                    if r['active_registered'] or r['gate'] == 'hold'
                    or (json.loads(r['doc']).get('state') == 'active' and r['gate'] == 'send'))
                derived = share_registration.derive(record, entries, self.org_id, previous)
                out['degraded'] = derived['degraded']
                for registration in derived['registrations']:
                    grant_id, digest = registration['grantId'], share_registration.digest(registration)
                    row = conn.execute('SELECT * FROM registrations WHERE grant_id = ?', (grant_id,)).fetchone()
                    if row is not None and row['digest'] == digest:
                        if row['gate'] == 'hold-degraded' and row['record_digest'] == record_digest:
                            continue                       # a typed refusal waits for the record to change
                        if row['gate'] == 'send' and registration['state'] == 'withdrawn':
                            continue                       # a withdrawal is final once taken
                        if row['gate'] == 'send' and row['sent_at'] \
                                and now - row['sent_at'] < share_registration.RESEND_SECONDS:
                            continue
                        if row['gate'] == 'hold' and row['next_at'] > now:
                            continue
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
                    active = registration['state'] == 'active'
                    was = bool(row['active_registered']) if row is not None else False
                    if gate == 'send':
                        registered = active                # taken: active is registered, a withdrawal ends it
                    elif gate == 'hold':
                        registered = was or active         # unanswered: it may have landed
                    else:
                        registered = was                   # refused: nothing changed on the server
                    attempts = (row['attempts'] + 1) if row is not None and gate == 'hold' else 0
                    conn.execute(
                        'INSERT INTO registrations(grant_id, doc, digest, answer, gate, record_digest, sent_at, '
                        'attempts, next_at, active_registered) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                        'ON CONFLICT(grant_id) DO UPDATE SET doc = excluded.doc, digest = excluded.digest, answer = '
                        'excluded.answer, gate = excluded.gate, record_digest = excluded.record_digest, sent_at = '
                        'excluded.sent_at, attempts = excluded.attempts, next_at = excluded.next_at, '
                        'active_registered = excluded.active_registered',
                        (grant_id, json.dumps(registration, sort_keys=True), digest, json.dumps(shaped, sort_keys=True),
                         gate, record_digest, now, attempts, self._backoff(attempts) if gate == 'hold' else 0,
                         int(registered)))
                    self.journal('share-registration', grantState=registration['state'], status=status, gate=gate,
                                 code=_error_code(answer) if status != 200 else (answer or {}).get('state'))
                    out['sent'] += 1
                    out['answers'][grant_id] = gate
            return out

    def _register_quietly(self) -> dict:
        """Registrations sent on a change (an enrollment, an applied revision, a protocol list), not only on the
        schedule; a failure is the schedule's to retry."""
        try:
            result = self.register_shares()
            return {k: result[k] for k in ('sent', 'held') if k in result}
        except (OrgError, OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            code = getattr(error, 'code', type(error).__name__)
            self.journal('share-registration-deferred', error=code)
            return {'deferred': code}
    def usage_days(self, until_ms: int) -> List[dict]:
        """The meter's settled grant-check receipts summed per (UTC day, grant), for ended days whose every
        reservation is settled or past its expiry: a day is never summed while a call of it may still settle (a
        reservation lives `meter.RESERVATION_SECONDS`, and may settle after midnight). `waiting` counts the others."""
        if self.meter_db is None or not Path(self.meter_db).exists():
            return []
        import paths as _paths
        now = until_ms // 1000
        conn = sqlite3.connect(_paths.sqlite_uri(self.meter_db), uri=True, timeout=30)
        try:
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch') AS day, grant_id, COUNT(settled_usd) AS calls, "
                "SUM(settled_usd) AS cost, SUM(CASE WHEN settled_usd IS NULL AND expires_at > ? THEN 1 ELSE 0 END) "
                "AS open FROM reservations WHERE created_at < ? GROUP BY day, grant_id ORDER BY day, grant_id",
                (now, (until_ms // 86400000) * 86400)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [{'day': r[0], 'grantId': r[1], 'calls': int(r[2]), 'costUsd': round(float(r[3] or 0), 6),
                 'waiting': bool(r[4])} for r in rows if r[2] or r[4]]
    def collect_grant_usage(self) -> dict:
        """Enqueues one `grant.usage` per (ended UTC day, grant) for the share grants the applied revision places in
        this organization's context, from the collection start on, each only when the grant's registration answer
        lets it (`gate`) and once none of that day's reservations can still settle; a held grant's days wait in the
        meter and are enqueued once the gate opens."""
        with self._locked():
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
            enqueued, held, waiting = 0, set(), 0
            for day in self.usage_days(int(self.clock() * 1000)):
                if day['grantId'] not in grants or strict_time(day['day'] + 'T00:00:00Z') < start:
                    continue
                if day['waiting']:
                    waiting += 1
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
            return {'enqueued': enqueued, 'heldGrants': len(held), 'waitingDays': waiting}
    # -- the schedule ------------------------------------------------------------------------------------------
    def tick(self) -> dict:
        """What is due, in order: rotation, check-in, share registrations, grant usage, the outbox. Each step's
        failure is recorded and the next still runs. The whole tick holds the enrollment's lock."""
        with self._locked():
            state = self.state()
            if state is None:
                return {'org': self.org_id, 'result': 'not-enrolled'}
            state['tickSeq'] = (state.get('tickSeq') or 0) + 1   # what a later tick means (`_unknown_org`)
            self._save(state)
            said = notice(self.org_id, state, self.clock())
            step = {k: said[k] for k in ('removed', 'nextStep', 'command') if k in said}
            if said['state'] == 'removed':                 # recorded once, when it happened; nothing is retried
                return {'org': self.org_id, 'result': 'removed', **step}
            if self.expired(state) or state.get('reEnroll'):
                self.journal('needs-enrollment', expired=self.expired(state))
                return {'org': self.org_id, 'result': 'needs-enrollment', **step}
            report: Dict[str, Any] = {'org': self.org_id}
            steps = [('rotate', lambda: self.rotate() if self.rotation_due(self.state()) else 'not-due'),
                     ('checkIn', lambda: self.check_in() if self.clock() >= (self.state().get('nextCheckInAt') or 0)
                      else 'not-due'),
                     ('shares', self.register_shares), ('grantUsage', self.collect_grant_usage),
                     ('outbox', self.flush)]
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
        said = notice(self.org_id, state, self.clock())
        cert_state = said['state'] if said['state'] in ('expiring', 'expired') else \
            'expired' if self.expired(state) else 'valid'
        out = {'org': self.org_id, 'enrolled': True, 'server': state['server'], 'device': state['device'],
               'state': 'enrolled' if said['state'] in ('valid', 'expiring') else said['state'],
               'epoch': state['epoch'], 'certificate': {'serial': state['certificate']['serial'],
                                                        'notAfter': state['certificate']['notAfter'],
                                                        'expiresOn': said['expiresOn'], 'state': cert_state,
                                                        'daysLeft': said['daysLeft'],
                                                        'rotationDue': self.rotation_due(state)},
               'protocols': state.get('protocols', []), 'shareRegistration': self.accepts_shares(state),
               'collectionStart': state.get('collectionStart'), 'lastCheckIn': state.get('lastCheckIn'),
               'checkInSeconds': state.get('checkInSeconds'), 'needsEnrollment': self.expired(state)
               or bool(state.get('reEnroll')), 'rotationPending': isinstance(state.get('pendingRotation'), dict),
               'key': self._key_declaration()}
        out.update({k: said[k] for k in ('removed', 'nextStep', 'command', 'unknownOrg', 'warning') if k in said})
        out['outbox'] = {'pending': 0, 'acked': 0, 'rejected': 0, 'dropped': 0, 'lastBatch': None, 'retry': None}
        out['registrations'] = {}
        path = self.folder / 'outbox.sqlite'
        # Read-only, and nothing is created: a status read while `leave` runs never brings the folder back.
        if path.is_file():
            import paths as _paths
            conn = sqlite3.connect(_paths.sqlite_uri(path), uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                counts = {r['state']: r['n'] for r in conn.execute(
                    'SELECT state, COUNT(*) AS n FROM events GROUP BY state')}
                out['outbox'].update({k: counts.get(k, 0) for k in ('pending', 'acked', 'rejected', 'dropped')})
                out['outbox'].update(lastBatch=self._meta(conn, 'lastBatch'), retry=self._meta(conn, 'retry'))
                out['registrations'] = {r['gate']: r['n'] for r in conn.execute(
                    'SELECT gate, COUNT(*) AS n FROM registrations GROUP BY gate')}
            except sqlite3.Error:
                out['outbox']['unreadable'] = True
            finally:
                conn.close()
        degraded = []
        if 'removed' in out:
            degraded.append('org:removed')
        elif out['needsEnrollment']:
            degraded.append('org:needs-enrollment')
        if 'unknownOrg' in out:
            degraded.append('org:unknown-org')
        if cert_state == 'expired' and 'removed' not in out:
            degraded.append('org:certificate-expired')
        elif cert_state == 'expiring':
            degraded.append('org:certificate-expiring')
        if not out['shareRegistration']:
            degraded.append('org:share-registration-not-listed')
        if out['registrations'].get('hold-degraded'):
            degraded.append('org:share-registration-refused')
        if out['outbox']['pending'] and (out['outbox']['retry'] or {}).get('attempts'):
            degraded.append('org:outbox-retrying')
        out['degraded'] = degraded
        return out

    def _key_declaration(self) -> dict:
        """What the enrollment request declares about the key, read without loading or creating a key."""
        if self._keys is None:
            from .device_keys import DeviceKeys
            source = DeviceKeys
        else:
            source = self._keys
        return {'storage': getattr(source, 'storage', 'file'), 'exportable': bool(getattr(source, 'exportable', True))}

    def leave(self) -> dict:
        """Removes what enrollment created, locally: this organization's folder, its own device keys with it (they
        belong to this enrollment and no other). The server keeps its records until an administrator retires the
        device. Under the lock, so a tick in flight finishes first and the next one finds nothing to recreate."""
        with self._locked():
            if not self.folder.exists():
                return {'org': self.org_id, 'left': False, 'reason': 'not-enrolled'}
            if self.folder.is_symlink() or not self.folder.resolve().is_relative_to((self.node / 'orgs').resolve()):
                raise OrgError('refused', 'the organization folder is not inside this node')
            shutil.rmtree(self.folder)
            self._keys = None
            return {'org': self.org_id, 'left': True}


def enrolled_orgs(node: Path) -> List[str]:
    folder = Path(node) / 'orgs'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and ORG_ID.fullmatch(p.name)
                  and (p / 'enrollment.json').exists())
