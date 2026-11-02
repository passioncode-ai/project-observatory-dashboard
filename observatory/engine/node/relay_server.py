"""The interim relay (OBS-287, the relay project's RLY-04): a `fabric-relay/0.1` server for one person's own estate,
until the hosted relay (RLY-05) runs (contract `src/relay.ts`, DEC-0047, DEC-0066; relay.md §Transport, §Admission,
§Messages).

It runs on the person's always-on machine, inside the node's own server (MACHINE-UTILITY.md D1: a thread of the local
server, no unit of its own) or in the foreground (`full relay serve`). It admits from the estate record this machine's
node verified and applied (`node/records/`, `org.apply_record`), never from a configured list: the admission set is
`relay.peer_keys` over that record and the revocations of the delta entries the node applied.

What it carries, and how:

- `GET /v1/connect` with the subprotocol `fabric-relay.v1`: the request head within 10 s of the accept and the
  machine's `auth` within 10 s of the upgrade, both wall-clock deadlines across every byte (a client that sends one byte
  at a time is closed at them; a missed `auth` is `unauthenticated`, 4401); `hello` with a fresh 32-byte nonce, `auth`
  checked by `admit` (the reference's order and its one `unauthenticated` answer, with one signature's work on every
  path past the frame's own bytes), then `welcome` with the revision, the delta head and the limits;
- `send` to a recipient's mailbox (`accepted` with its per-mailbox `seq`), refused `malformed`, `too-large`,
  `rate-limited` (with `retryAfter`; the per-minute limits count a machine across all its connections),
  `unknown-recipient` or `mailbox-full`; a repeat of `(from, msgId)` within its `ttl` answers the first `seq`, after an
  acknowledgement too (tombstones bounded per sender and gone at their ttl);
- `deliver` in `seq` order to every connection the recipient holds, by that connection's own writer thread, a page at a
  time (`FLUSH_PAGE_ROWS`, `FLUSH_PAGE_BYTES`, read one machine at a time); deleted on `ack` or when the `ttl` the sender
  set ends (the node signs `exp` at `iat + ttl`, so the mailbox keeps a body no longer than its signed life);
- `presence` pushed when a machine comes and goes, `presence-query`, `ping`;
- `delta-query`, answered from the delta entries this machine's node applied (read-only).

Connections are bounded twice, below the process's descriptor limit (`fd_caps`): those not admitted yet (`MAX_PENDING`,
`MAX_PENDING_PER_SOURCE` per caller address, loopback included, a trusted terminator's forwarded address with
`trusted_proxy`, IPv6 by its /64; admission max-min fair across addresses, `RelayServer._place`: a newcomer whose
address holds at least two fewer slots than the fullest takes one of that address's, otherwise one gives way by
`RelayServer.victim`, never one answering inside `FAIR_ANSWER_SECONDS`) apart from admitted sessions (`MAX_SESSIONS`,
`MAX_SESSIONS_PER_KID` per machine, its oldest replaced), so unauthenticated clients cannot starve the estate's
machines. A newcomer refused on arrival is answered 503, half-closed and drained for up to `LINGER_SECONDS` before the
close (`MAX_LINGERING` at once), so the answer reaches it: a close over unread input is a reset, and a Windows caller
drops what it has not read. Each session writes from its own thread through a bounded queue: a machine that stops
reading stalls only itself and is closed when its queue fills, and no other session, `end` or `stop` waits on its
socket. A failed accept (out of descriptors) is retried with a backoff and logged, and `health` names an accept loop
that ended or keeps failing.

Every open session is checked against the current admission set before any frame it sends or accepts and every two
seconds: a machine the record or a delta no longer admits, or under another key, is closed `not-admitted` (4403), and
all of them `expired` (4403) once the record's window ends.

What it holds: bodies are opaque to it (it never decodes one) and are kept AES-256-GCM sealed in the spool, under a key
of the relay's own (`spool.key`, owner-only, beside the spool in an owner-only folder), bound to their recipient, sender,
`msgId` and `seq`. That keeps a copied spool (a backup, a snapshot, a disk listing) from carrying any body in clear; it
does not hide a body from a reader of this account, which holds the key too. What must stay secret from any relay is
sealed end to end by its sender (secret and credential envelopes, sealed calls and reports), as relay.md says.

Not carried yet (OBS-287's remaining part): blobs (`/v1/blobs/` answers 501), a hub's `delta` and `heartbeat` frames
(answered `unknown-type`; `welcome.heartbeat` is null), and the deployment into the person's Cloudflare account.

Logs are one JSON object per event: event names, kids, codes and counts. Never a nonce, a signature, a body, a key or
the estate id.
"""
from __future__ import annotations

import base64
import collections
import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import osprivacy

from . import estate as estate_rules, jcs, p256, relay, revocation
from .ws import GUID, MAX_FRAGMENTS, OP_BINARY, OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT

LOG = logging.getLogger('observatory.node.relay_server')

SERVICE = 'observatory-relay'
AUTH_DEADLINE_SECONDS = 10
IDLE_CLOSE_SECONDS = 90
REQUEST_HEAD_SECONDS = 10
MAX_REQUEST_HEAD_BYTES = 8192
#: Admitted sessions one relay holds at once; one more is refused `rate-limited` and closed 1008.
MAX_SESSIONS = 128
MAX_CONNECTIONS = MAX_SESSIONS                             # the name 0.31.0's first slice used
#: Connections one machine (one admitted kid) holds at once; one more replaces its oldest (closed 1001).
MAX_SESSIONS_PER_KID = 4
#: Connections not admitted yet (sending their request head, or upgraded and owing `auth`), apart from the sessions,
#: so they cannot starve admitted machines: past it one gives way (`RelayServer.victim`), or the newcomer is answered
#: 503 when every one is answering inside its fair window.
MAX_PENDING = 32
#: Of those, from one caller address, loopback included: the socket's peer, or behind a TLS terminator on this machine
#: the address it forwards (`trusted_proxy`, `X-Forwarded-For`), so one caller cannot hold the others' slots.
MAX_PENDING_PER_SOURCE = 8
#: A connection that sent its request and owes `auth` keeps its slot this long whatever arrives (its fair answer
#: window); one that has sent nothing may give way at once, after every older silent one of its address.
FAIR_ANSWER_SECONDS = 2.0
#: Descriptors left to the rest of the process (the node's server, the spool) below its soft limit.
FD_RESERVE = 48
#: An accept loop failing this long, or ended, is reported by `health`.
ACCEPT_FAILING_SECONDS = 1.0
#: The longest wait between two retries of a failing accept (the backoff doubles from 50 ms up to it).
ACCEPT_BACKOFF_SECONDS = 1.0
#: A connection refused before its request was read is half-closed and drained this long, at most this many bytes,
#: before it is closed: a close with unread input is a reset, and a Windows caller drops the 503 it has not read yet.
#: At most `MAX_LINGERING` drain at once (within `FD_RESERVE`); past it a refused connection is closed at once.
LINGER_SECONDS = 1.0
LINGER_BYTES = 64 * 1024
MAX_LINGERING = 16
#: Queued control frames (replies, presence, delta pages) one session may hold before it counts as not reading.
OUTBOX_BYTES = 4 * 1024 * 1024
#: Seconds a session closing with an error frame has to send it before its socket is shut regardless.
END_GRACE_SECONDS = 1.0
#: One delivery page: at most this many messages and, past the first, this many body bytes, read one machine at a time.
FLUSH_PAGE_ROWS = 8
FLUSH_PAGE_BYTES = 2 * 1024 * 1024
#: Tombstones `(from, msgId)` one sender keeps beyond the mailbox caps: past it, its oldest whose message has left the
#: mailbox go first; while every one guards a queued message its new sends are refused (`rate-limited`). Every
#: tombstone goes at its ttl.
MAX_TOMBSTONES_PER_SENDER = 20000
REVALIDATE_SECONDS = 2.0
PURGE_SECONDS = 60.0
MAX_QUERY_KIDS = 256
MAX_KID_CHARS = relay.MAX_KID_CHARS
MAX_ID_CHARS = 128
DELTA_PAGE_ENTRIES = 256
#: What this relay enforces, advertised in `welcome.limits`; every one the relay's own choice (relay.md §Messages).
DEFAULT_LIMITS = {'sendPerMinute': 120, 'presenceQueryPerMinute': 30, 'deltaQueryPerMinute': 12,
                  'mailboxMessages': 1000, 'mailboxBytes': 64 * 1024 * 1024}
RATE_LIMITED = {'send': 'sendPerMinute', 'presence-query': 'presenceQueryPerMinute',
                'delta-query': 'deltaQueryPerMinute'}

#: Close codes (contract `CLOSE_CODES`).
CLOSE_UNAUTHENTICATED, CLOSE_NOT_ADMITTED = 4401, 4403
CLOSE_TOO_LARGE, CLOSE_BINARY, CLOSE_GOING_AWAY, CLOSE_POLICY, CLOSE_INTERNAL = 1009, 1003, 1001, 1008, 1011
CLOSE_PROTOCOL, CLOSE_NOT_UTF8 = 1002, 1007

#: One fixed message per code: the reason goes to the log, never to the client (relay.md §Admission).
MESSAGES = {
    'protocol-unsupported': 'this relay speaks fabric-relay/0.1',
    'unsupported-alg': 'a machine proves itself with ES256',
    'unauthenticated': 'not authenticated',
    'not-yet-valid': 'the signed time is ahead of this relay',
    'replay': 'this nonce was used',
    'not-admitted': 'this machine is no longer admitted',
    'expired': 'the estate record this relay admits from has expired',
    'malformed': 'a frame this relay cannot read',
    'unknown-type': 'a frame type this relay does not take',
    'unknown-recipient': 'no such recipient',
    'too-large': 'over the size limit',
    'rate-limited': 'over the rate limit',
    'mailbox-full': "the recipient's mailbox is full",
    'internal': 'a relay fault',
}
#: Frame types a relay sends and a node never does (relay.md §Transport): answered `unknown-type`.
RELAY_ONLY = frozenset({'hello', 'welcome', 'accepted', 'deliver', 'presence', 'presence-result', 'error'})
#: A hub's frames, which this interim relay does not carry yet: answered `unknown-type`, the connection stays open.
HUB_FRAMES = frozenset({'delta', 'heartbeat'})

_DUMMY_KEY = b'\x04' + p256.GX.to_bytes(32, 'big') + p256.GY.to_bytes(32, 'big')   # the base point G
_DUMMY_SIG = b'\x5a' * 64


def _now_ms(clock: Callable[[], float]) -> int:
    return int(clock() * 1000)


def _default_log(record: dict) -> None:
    LOG.info(json.dumps(record, separators=(',', ':'), sort_keys=True))


# ---- Admission ------------------------------------------------------------------------------------------------------

def prove_es256(key: Any, data: Optional[str], sig: Any) -> bool:
    """One ES256 verification of equal cost whatever the inputs (the contract's `proveEs256`, DEC-0066): a missing or
    unusable key is replaced by the base point and a signature that is not 64 canonical bytes by 64 fixed bytes, the
    verification runs once either way and the low-S rule is applied after it. True only for a usable key, 64 canonical
    low-S bytes and a signature that verifies, so neither the answer nor the work tells an unproven caller whether this
    relay holds its kid, its estate or a record at all."""
    ed = key is not None and p256.is_ed25519_key(key)
    candidate = _DUMMY_KEY if key is None or ed else key
    valid = p256.key_refusal(candidate) is None
    usable = key is not None and not ed and valid and data is not None
    octets = p256.point(candidate if valid else _DUMMY_KEY)
    raw = p256.b64url_decode(sig) if isinstance(sig, str) else None
    canonical = raw is not None and len(raw) == 64 and p256.b64url_encode(raw) == sig
    signature = raw if canonical else _DUMMY_SIG
    verified = p256.es256_verifies(octets, (data or '').encode('utf-8'), signature)
    low = p256.is_low_s(signature)
    return (int(usable) & int(canonical) & int(verified) & int(low)) == 1


def admit(frame: Any, *, relay_origin: str, estate: Optional[str], nonce: str, nonce_used: bool,
          admitted_key: Callable[[Any], Any], now_ms: int) -> dict:
    """The contract's `admit` (src/relay.ts, DEC-0047 item 4, DEC-0066): the checks on the frame's own bytes first
    (protocol, `alg`, the times), then the nonce, then, as one step with one answer, the kid's key in the admission set,
    the signed estate and the signature. `{'ok': True, 'kid'}` or `{'ok': False, 'code'}`."""
    frame = frame if isinstance(frame, dict) else {}
    if frame.get('protocol') != relay.RELAY_PROTOCOL:
        return {'ok': False, 'code': 'protocol-unsupported'}
    if frame.get('alg') != relay.RELAY_ALG:
        return {'ok': False, 'code': 'unsupported-alg'}
    iat = relay.instant(frame.get('iat'))
    if iat is None:
        return {'ok': False, 'code': 'unauthenticated'}
    if iat - now_ms > relay.CLOCK_SKEW_SECONDS * 1000:
        return {'ok': False, 'code': 'not-yet-valid'}
    if now_ms - iat > relay.AUTH_WINDOW_SECONDS * 1000:
        return {'ok': False, 'code': 'unauthenticated'}
    if nonce_used:
        return {'ok': False, 'code': 'replay'}
    kid = frame.get('kid')
    # The lookup runs whatever the estate says and its answer is discarded on a mismatch: the same work either way.
    found = admitted_key(kid) if isinstance(kid, str) else None
    key = found if estate is not None and frame.get('estate') == estate else None
    try:
        text = relay.auth_signing_input(relay=relay_origin, nonce=nonce, estate=frame.get('estate'), kid=kid,
                                        iat=frame.get('iat'))
    except (jcs.CanonicalError, TypeError, ValueError, UnicodeEncodeError):
        text = None                                        # no canonical form: it cannot have been signed
    return {'ok': True, 'kid': kid} if prove_es256(key, text, frame.get('sig')) \
        else {'ok': False, 'code': 'unauthenticated'}


class NodeRecords:
    """The estate record this machine's node verified and applied, and the delta entries it applied since
    (`<node>/records/estate-record.json`, `delta-state.json`, as `org.apply_record` and `org.apply_delta_page` write
    them). `read` answers `(record, delta_state, stamp)`; the stamp changes when either file does."""

    def __init__(self, node: Path):
        self.folder = Path(node) / 'records'

    def _stamp(self) -> tuple:
        out = []
        for name in ('estate-record.json', 'delta-state.json'):
            try:
                info = (self.folder / name).stat()
                out.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                out.append(None)
        return tuple(out)

    def read(self) -> Tuple[Optional[dict], Optional[dict], Any]:
        """Both documents whole (the delta state with its `forked` entries, whose revocations stay in force), each
        through `private_io.read`, which refuses a file others may read or write. A file that is absent or refused is
        None; the record's own check is `Admission`'s."""
        import private_io
        stamp = self._stamp()
        out = []
        for name in ('estate-record.json', 'delta-state.json'):
            try:
                doc = json.loads(private_io.read(self.folder / name))
            except FileNotFoundError:
                doc = None
            except (ValueError, RuntimeError, OSError) as error:
                raise RuntimeError('%s unreadable (%s)' % (name, type(error).__name__)) from None
            out.append(doc if isinstance(doc, dict) else None)
        return out[0], out[1], stamp


class Admission:
    """The admission set, from a source of `(record, delta_state, stamp)`. The record was verified when the node
    applied it; here its bytes are checked against its own `digest` again whenever the files change, so a record
    edited on disk admits nobody (`record-unreadable`). `current` is `relay.peer_keys` over it, with the delta's
    revocations, plus what `welcome` names: the estate (compared inside `admit` only), the revision and the delta's
    head and entries."""

    def __init__(self, source: Callable[[], Tuple[Optional[dict], Optional[dict], Any]],
                 log: Callable[[dict], None] = _default_log):
        self.source = source
        self.log = log
        self._lock = threading.Lock()
        self._stamp: Any = object()
        self._held: Tuple[Optional[dict], Optional[dict], Optional[str]] = (None, None, 'no-record')

    def _load(self) -> Tuple[Optional[dict], Optional[dict], Optional[str]]:
        try:
            record, state, stamp = self.source()
        except Exception as error:                        # noqa: BLE001 - a source that fails admits nobody
            self.log({'event': 'relay.admission.unreadable', 'error': type(error).__name__})
            return None, None, 'record-unreadable'
        with self._lock:
            if stamp is not None and stamp == self._stamp:
                return self._held
        problem = None
        if not isinstance(record, dict):
            record, state, problem = None, None, 'no-record'
        else:
            try:
                intact = estate_rules.estate_record_digest(record) == record.get('digest')
            except (jcs.CanonicalError, TypeError, ValueError):
                intact = False
            if not intact:
                record, state, problem = None, None, 'record-unreadable'
            elif not isinstance(state, dict) or state.get('estate') != record.get('estate') \
                    or not isinstance(state.get('entries'), list):
                state = revocation.empty_delta_state(record.get('estate'))
        held = (record, state, problem)
        with self._lock:
            changed = held[2] != self._held[2] or (held[0] or {}).get('digest') != (self._held[0] or {}).get('digest')
            self._stamp, self._held = stamp, held
        if changed:
            self.log({'event': 'relay.admission.loaded', 'revision': (record or {}).get('revision'),
                      'problem': problem, 'deltaEntries': len((state or {}).get('entries') or [])})
        return held

    def current(self, now_ms: int) -> dict:
        record, state, problem = self._load()
        if record is None:
            return {'ok': False, 'refusal': problem or 'no-record'}
        answer = relay.peer_keys(record, now_ms, revocation.delta_revocations(state))
        if not answer.get('ok'):
            return answer
        return dict(answer, estate=record.get('estate'), state=state,
                    deltaHead=revocation.delta_head(state))


# ---- Mailboxes ------------------------------------------------------------------------------------------------------

def load_or_create_spool_key(folder: Path) -> bytes:
    """The relay's own 32-byte spool key, `<folder>/spool.key`, owner-only; made once, never replaced (a new key would
    make every queued body unreadable)."""
    import private_io
    path = Path(folder) / 'spool.key'
    if path.exists():
        raw = base64.b64decode(private_io.read(path).strip(), validate=True)
        if len(raw) != 32:
            raise ValueError('the spool key is not 32 bytes')
        return raw
    raw = secrets.token_bytes(32)
    private_io.write(path, base64.b64encode(raw).decode('ascii') + '\n')
    return raw


class Mailboxes:
    """Per-recipient mailboxes (relay.md §Messages) in one SQLite database: `seq` monotonic per mailbox, a body kept
    AES-256-GCM sealed under the spool key and bound to `(to, from, msgId, seq)`, a tombstone of `(from, msgId)` until
    its `ttl` so a repeat answers its first `seq`, and the limits `mailboxMessages` and `mailboxBytes`. `path` None
    keeps everything in memory. Thread-safe: one lock around every statement."""

    def __init__(self, path: Optional[Path], key: bytes, limits: Optional[dict] = None,
                 log: Callable[[dict], None] = _default_log):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aead = AESGCM(key)
        self.limits = dict(DEFAULT_LIMITS, **(limits or {}))
        self.log = log
        self._lock = threading.Lock()
        if path is not None:
            path = Path(path)
            if not path.exists():
                fd = osprivacy.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
                os.close(fd)
        self._db = sqlite3.connect(':memory:' if path is None else str(path), check_same_thread=False,
                                   isolation_level=None)
        self._db.executescript('''
            PRAGMA journal_mode = DELETE;
            CREATE TABLE IF NOT EXISTS mail (
              recipient TEXT NOT NULL, seq INTEGER NOT NULL, sender TEXT NOT NULL, msg_id TEXT NOT NULL,
              sealed BLOB NOT NULL, size INTEGER NOT NULL, sent_at TEXT NOT NULL, expires_at INTEGER NOT NULL,
              PRIMARY KEY (recipient, seq));
            CREATE TABLE IF NOT EXISTS counters (recipient TEXT PRIMARY KEY, next INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tombstones (
              sender TEXT NOT NULL, msg_id TEXT NOT NULL, recipient TEXT NOT NULL, seq INTEGER NOT NULL,
              expires_at INTEGER NOT NULL, PRIMARY KEY (sender, msg_id));
            CREATE INDEX IF NOT EXISTS mail_expiry ON mail (expires_at);
            CREATE INDEX IF NOT EXISTS tombstone_expiry ON tombstones (expires_at);''')

    @staticmethod
    def _aad(to: str, sender: str, msg_id: str, seq: int) -> bytes:
        return jcs.canonical_bytes({'type': 'observatory-relay-spool/1', 'to': to, 'from': sender, 'msgId': msg_id,
                                    'seq': seq})

    def append(self, to: str, sender: str, msg_id: str, body: str, ttl_seconds: int, now_ms: int) -> dict:
        """`{'seq', 'duplicate'}`, or `{'full': 'messages' | 'bytes'}` when the mailbox is at a limit."""
        data = body.encode('ascii')
        with self._lock:
            db = self._db
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('DELETE FROM mail WHERE recipient = ? AND expires_at <= ?', (to, now_ms))
                db.execute('DELETE FROM tombstones WHERE sender = ? AND msg_id = ? AND expires_at <= ?',
                           (sender, msg_id, now_ms))
                same = db.execute('SELECT seq FROM tombstones WHERE sender = ? AND msg_id = ?',
                                  (sender, msg_id)).fetchone()
                if same:
                    db.execute('COMMIT')
                    return {'seq': same[0], 'duplicate': True}
                if not self._tombstone_room(sender):
                    db.execute('COMMIT')
                    return {'full': 'tombstones'}
                count, size = db.execute('SELECT count(*), coalesce(sum(size), 0) FROM mail WHERE recipient = ?',
                                         (to,)).fetchone()
                if count >= self.limits['mailboxMessages']:
                    db.execute('COMMIT')
                    return {'full': 'messages'}
                if size + len(data) > self.limits['mailboxBytes']:
                    db.execute('COMMIT')
                    return {'full': 'bytes'}
                row = db.execute('SELECT next FROM counters WHERE recipient = ?', (to,)).fetchone()
                seq = row[0] if row else 1
                db.execute('INSERT INTO counters (recipient, next) VALUES (?, ?) '
                           'ON CONFLICT (recipient) DO UPDATE SET next = excluded.next', (to, seq + 1))
                nonce = secrets.token_bytes(12)
                sealed = nonce + self._aead.encrypt(nonce, data, self._aad(to, sender, msg_id, seq))
                expires = now_ms + ttl_seconds * 1000
                db.execute('INSERT INTO mail VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (to, seq, sender, msg_id, sealed, len(data), relay.rfc3339(now_ms), expires))
                db.execute('INSERT OR REPLACE INTO tombstones VALUES (?, ?, ?, ?, ?)',
                           (sender, msg_id, to, seq, expires))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return {'seq': seq, 'duplicate': False}

    def _tombstone_room(self, sender: str) -> bool:
        """Whether `sender` may get one more tombstone (under the lock, inside the transaction): at
        `MAX_TOMBSTONES_PER_SENDER` its oldest go whose message left the mailbox (acknowledged or expired); one whose
        message is still queued is never dropped, since a retry under its `msgId` would then queue a second copy. False
        when every one still guards a queued message: the send is refused, never forgotten."""
        db = self._db
        count = db.execute('SELECT count(*) FROM tombstones WHERE sender = ?', (sender,)).fetchone()[0]
        if count < MAX_TOMBSTONES_PER_SENDER:
            return True
        db.execute('DELETE FROM tombstones WHERE rowid IN (SELECT t.rowid FROM tombstones t WHERE t.sender = ? AND '
                   'NOT EXISTS (SELECT 1 FROM mail m WHERE m.recipient = t.recipient AND m.seq = t.seq) '
                   'ORDER BY t.rowid LIMIT ?)', (sender, count - MAX_TOMBSTONES_PER_SENDER + 1))
        return db.execute('SELECT count(*) FROM tombstones WHERE sender = ?', (sender,)).fetchone()[0] \
            < MAX_TOMBSTONES_PER_SENDER

    def page(self, to: str, now_ms: int, after_seq: int = 0, *, max_rows: Optional[int] = None,
             max_bytes: Optional[int] = None) -> List[dict]:
        """The next unacknowledged, unexpired messages after `after_seq`, in `seq` order: at most `max_rows`
        (`FLUSH_PAGE_ROWS`), and no more once their bodies reach `max_bytes` (`FLUSH_PAGE_BYTES`; the first is always
        taken). Rows are read and opened one at a time, so a page holds no more than that in memory. Expired ones are
        deleted on the way, and one whose seal does not open (another key, a damaged row) is skipped, deleted and
        logged, never delivered and never counted against the page: an empty page means the mailbox is exhausted."""
        from cryptography.exceptions import InvalidTag
        rows = FLUSH_PAGE_ROWS if max_rows is None else max_rows
        limit = FLUSH_PAGE_BYTES if max_bytes is None else max_bytes
        out: List[dict] = []
        broken: List[int] = []
        total = 0
        with self._lock:
            self._db.execute('DELETE FROM mail WHERE recipient = ? AND expires_at <= ?', (to, now_ms))
            cursor = self._db.execute('SELECT seq, sender, msg_id, sealed, sent_at FROM mail WHERE recipient = ? '
                                      'AND seq > ? ORDER BY seq', (to, after_seq))
            for seq, sender, msg_id, sealed, sent_at in cursor:
                try:
                    body = self._aead.decrypt(sealed[:12], sealed[12:],
                                              self._aad(to, sender, msg_id, seq)).decode('ascii')
                except (InvalidTag, ValueError, UnicodeDecodeError):
                    broken.append(seq)
                    continue
                out.append({'from': sender, 'msgId': msg_id, 'seq': seq, 'body': body, 'sentAt': sent_at})
                total += len(body)
                if total >= limit or len(out) >= max(rows, 1):
                    break
            cursor.close()
            if broken:
                self._db.executemany('DELETE FROM mail WHERE recipient = ? AND seq = ?', [(to, b) for b in broken])
        if broken:
            self.log({'event': 'relay.spool.unreadable', 'kid': to, 'count': len(broken)})
        return out

    def pending(self, to: str, now_ms: int, after_seq: int = 0) -> List[dict]:
        """Every pending message after `after_seq` (`page` without its bounds), for tools and tests."""
        return self.page(to, now_ms, after_seq, max_rows=self.limits['mailboxMessages'], max_bytes=1 << 62)

    def tombstones(self, sender: str) -> int:
        with self._lock:
            return self._db.execute('SELECT count(*) FROM tombstones WHERE sender = ?', (sender,)).fetchone()[0]

    def ack(self, to: str, seq: int) -> bool:
        with self._lock:
            return self._db.execute('DELETE FROM mail WHERE recipient = ? AND seq = ?', (to, seq)).rowcount > 0

    def purge(self, now_ms: int) -> int:
        with self._lock:
            gone = self._db.execute('DELETE FROM mail WHERE expires_at <= ?', (now_ms,)).rowcount
            self._db.execute('DELETE FROM tombstones WHERE expires_at <= ?', (now_ms,))
        return gone

    def count(self, to: Optional[str] = None) -> int:
        with self._lock:
            if to is None:
                return self._db.execute('SELECT count(*) FROM mail').fetchone()[0]
            return self._db.execute('SELECT count(*) FROM mail WHERE recipient = ?', (to,)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ---- The WebSocket, server side -------------------------------------------------------------------------------------

class FrameTooLarge(Exception):
    pass


class ProtocolError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


class PeerClosed(Exception):
    def __init__(self, code: Optional[int] = None):
        super().__init__('closed')
        self.code = code


_DONTWAIT = getattr(socket, 'MSG_DONTWAIT', 0)


class ServerSocket:
    """RFC 6455 from the server's side: client frames masked, ours not; text messages of at most 1 MiB in at most
    `MAX_FRAGMENTS` frames. One thread reads (`recv_text`) and one writes (`write`); `abort` is safe from any thread
    and never waits for the writer. `deadline` (monotonic seconds) bounds the whole read across every byte, as the
    auth phase needs; without one, each read waits at most `idle` seconds."""

    def __init__(self, sock: socket.socket, buffered: bytes = b''):
        self.sock = sock
        self.buffer = buffered
        self.lock = threading.Lock()
        self.closed = False
        self.deadline: Optional[float] = None
        self.idle: float = IDLE_CLOSE_SECONDS

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout('the deadline passed')
                self.sock.settimeout(remaining)
            else:
                self.sock.settimeout(self.idle)
            chunk = self.sock.recv(max(65536, n - len(self.buffer)))
            if not chunk:
                raise PeerClosed()
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _frame(self) -> Tuple[bool, int, bytes]:
        first, second = self._read(2)
        if first & 0x70:
            raise ProtocolError(CLOSE_PROTOCOL, 'reserved bits')
        if not second & 0x80:
            raise ProtocolError(CLOSE_PROTOCOL, 'an unmasked client frame')
        fin, opcode, n = bool(first & 0x80), first & 0x0F, second & 0x7F
        if n == 126:
            n = struct.unpack('!H', self._read(2))[0]
        elif n == 127:
            n = struct.unpack('!Q', self._read(8))[0]
        if opcode >= 0x8 and (n > 125 or not fin):
            raise ProtocolError(CLOSE_PROTOCOL, 'a control frame is short and whole')
        if n > relay.MAX_FRAME_BYTES:
            raise FrameTooLarge()                          # refused from its declared length, never read
        mask = self._read(4)
        payload = self._read(n)
        return fin, opcode, _unmask(payload, mask)

    def recv_text(self, on_ping: Callable[[bytes], None]) -> str:
        """The next text message; a ping goes to `on_ping` (the writer answers it). Raises `FrameTooLarge`,
        `ProtocolError` (a binary message: 1003, text not UTF-8: 1007, anything else out of order: 1002),
        `PeerClosed` (with the peer's code) and `socket.timeout`."""
        parts: List[bytes] = []
        total = 0
        while True:
            fin, opcode, payload = self._frame()
            if opcode == OP_PING:
                on_ping(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else None
                raise PeerClosed(code if code and (1000 <= code <= 1014 or 3000 <= code <= 4999) else 1000)
            if opcode == OP_BINARY:
                raise ProtocolError(CLOSE_BINARY, 'a binary message')
            if opcode not in (OP_TEXT, OP_CONT) or (opcode == OP_TEXT) == bool(parts):
                raise ProtocolError(CLOSE_PROTOCOL, 'a fragment out of order')
            parts.append(payload)
            total += len(payload)
            if total > relay.MAX_FRAME_BYTES:
                raise FrameTooLarge()
            if len(parts) > MAX_FRAGMENTS:
                raise ProtocolError(CLOSE_PROTOCOL, 'too many fragments')
            if fin:
                try:
                    return b''.join(parts).decode('utf-8')
                except UnicodeDecodeError:
                    raise ProtocolError(CLOSE_NOT_UTF8, 'text that is not UTF-8') from None

    @staticmethod
    def _header(opcode: int, n: int) -> bytes:
        if n < 126:
            return struct.pack('!BB', 0x80 | opcode, n)
        if n < 65536:
            return struct.pack('!BBH', 0x80 | opcode, 126, n)
        return struct.pack('!BBQ', 0x80 | opcode, 127, n)

    def write(self, opcode: int, payload: bytes) -> None:
        """The writer's send: blocks while the peer does not read, until the socket's timeout or an `abort`."""
        with self.lock:
            if self.closed:
                raise PeerClosed()
            self.sock.sendall(self._header(opcode, len(payload)) + payload)

    def abort(self, code: Optional[int] = None) -> None:
        """Ends the connection from any thread without waiting: a close frame only when the writer is idle and the
        socket takes it at once, then the socket shut both ways, which wakes a blocked reader and a blocked writer."""
        self.closed = True
        # Without MSG_DONTWAIT (Windows) a send could wait on a peer that does not read: the socket is only shut.
        if code is not None and _DONTWAIT and self.lock.acquire(blocking=False):
            try:
                self.sock.send(struct.pack('!BBH', 0x80 | OP_CLOSE, 2, code), _DONTWAIT)
            except OSError:
                pass
            finally:
                self.lock.release()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _unmask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    if n == 0:
        return payload
    key = int.from_bytes((mask * (n // 4 + 1))[:n], 'big')
    return (int.from_bytes(payload, 'big') ^ key).to_bytes(n, 'big')


# ---- A session ------------------------------------------------------------------------------------------------------

def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_ID_CHARS


def _kid(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_KID_CHARS


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_B64URL = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_')


class Session:
    """One connection: `hello`, `auth`, `welcome`, then the frames of an admitted machine.

    Two threads. The reader (the connection's own) reads and answers by queueing; the writer alone writes to the
    socket: queued frames first, then delivery pages from the machine's mailbox while it is woken. So a machine that
    stops reading stalls only its own writer: its queue is bounded (`OUTBOX_BYTES`, then the session is closed as not
    reading), and no other session, `end` or `stop` ever waits on its socket."""

    def __init__(self, server: 'RelayServer', ws: ServerSocket, peer: str):
        self.server, self.ws, self.peer = server, ws, peer
        self.kid: Optional[str] = None
        self.key: Optional[bytes] = None
        self.session_id = secrets.token_urlsafe(18)
        self.admitted_at = 0.0
        self.delivered = 0
        self.cond = threading.Condition()
        self.outbox: collections.deque = collections.deque()
        self.outbox_bytes = 0
        self.pong: Optional[bytes] = None                 # the latest ping's answer; older ones are never queued
        self.wake = False
        self.ended = False
        self.ended_at: Optional[float] = None
        self.writer = threading.Thread(target=self._write, name='relay-writer', daemon=True)

    # ---- Sending (any thread; never blocks on the socket) ---------------------------------------------------------

    def _id(self) -> str:
        return secrets.token_urlsafe(9)

    def _enqueue(self, item: tuple, size: int) -> None:
        with self.cond:
            if self.ended and item[0] == 'text':
                return
            self.outbox.append(item)
            self.outbox_bytes += size
            over = self.outbox_bytes > OUTBOX_BYTES
            self.cond.notify()
        if over:
            self.drop(None, 'not-reading')

    def send(self, frame: dict) -> None:
        data = json.dumps(dict({'v': 1, 'id': self._id()}, **frame), separators=(',', ':'),
                          ensure_ascii=False).encode('utf-8')
        self._enqueue(('text', data), len(data))

    def error(self, code: str, reply_to: Any = None, **extra) -> None:
        frame = {'type': 'error', 'code': code, 'message': MESSAGES[code]}
        if _valid_id(reply_to):
            frame['replyTo'] = reply_to
        frame.update(extra)
        self.send(frame)

    def _closing(self) -> bool:
        with self.cond:
            if self.ended:
                return False
            self.ended, self.ended_at = True, time.monotonic()
            return True

    def end(self, code: str, close: int, reason: str, **extra) -> None:
        """An `error` of `code`, then the close `close`, both through the writer; the reason goes to the log only.
        A writer that cannot send them within `END_GRACE_SECONDS` is cut off (`server._watch`, `finish`)."""
        if not self._closing():
            return
        frame = {'type': 'error', 'id': self._id(), 'code': code, 'message': MESSAGES[code], 'v': 1}
        frame.update(extra)
        data = json.dumps(frame, separators=(',', ':')).encode('utf-8')
        with self.cond:
            self.outbox.append(('text', data))
            self.outbox.append(('close', close))
            self.cond.notify()
        self.server.log({'event': 'relay.session.closed', 'kid': self.kid, 'code': code, 'close': close,
                         'reason': reason})

    def drop(self, close: Optional[int], reason: str) -> None:
        """Ends the session with no error frame: a close of `close` through the writer, or with None (a session that
        does not read) the socket shut at once."""
        if not self._closing():
            return
        if close is None:
            self.ws.abort()
        else:
            with self.cond:
                self.outbox.append(('close', close))
                self.cond.notify()
        self.server.log({'event': 'relay.session.' + ('slow' if reason == 'not-reading' else 'closed'),
                         'kid': self.kid, 'close': close, 'reason': reason})

    def wake_up(self) -> None:
        with self.cond:
            self.wake = True
            self.cond.notify()

    # ---- The writer -----------------------------------------------------------------------------------------------

    def _write(self) -> None:
        try:
            while True:
                with self.cond:
                    while not self.outbox and (self.pong is None or self.ended) \
                            and not (self.wake and self.kid is not None and not self.ended):
                        if self.ws.closed:
                            return
                        self.cond.wait(1.0)
                    if self.pong is not None and not self.ended:
                        item, self.pong = ('pong', self.pong), None
                        self.outbox_bytes -= len(item[1])
                    elif self.outbox:
                        item = self.outbox.popleft()
                        if item[0] == 'text':
                            self.outbox_bytes -= len(item[1])
                    else:
                        item, self.wake = None, False
                if item is None:
                    self._deliver_page()
                elif item[0] == 'text':
                    self.ws.write(OP_TEXT, item[1])
                elif item[0] == 'pong':
                    self.ws.write(OP_PONG, item[1])
                else:                                      # ('close', code): the last frame this connection sends
                    with contextlib.suppress(OSError, PeerClosed):
                        self.ws.write(OP_CLOSE, struct.pack('!H', item[1]))
                    self.ws.abort()
                    return
        except (OSError, PeerClosed):
            self.ws.abort()
        except Exception as error:                         # noqa: BLE001 - a writer fault ends its session only
            self.server.log({'event': 'relay.writer.fault', 'kid': self.kid, 'error': type(error).__name__})
            self.ws.abort()

    def _deliver_page(self) -> None:
        """One page of the machine's mailbox past what this connection was sent, read while no other connection of
        the machine reads one (`server.flush_lock`), then written; woken again while the page was full."""
        if not self.stands():
            return
        with self.server.flush_lock(self.kid):
            page = self.server.mailboxes.page(self.kid, self.server.now_ms(), self.delivered)
        for message in page:
            data = json.dumps(dict({'v': 1, 'id': self._id(), 'type': 'deliver'}, **message),
                              separators=(',', ':')).encode('utf-8')
            self.ws.write(OP_TEXT, data)
            self.delivered = message['seq']
        if page:                                           # until a page comes back empty: the mailbox past `delivered`
            self.wake_up()

    def finish(self) -> None:
        """After the reader ends: the writer gets `END_GRACE_SECONDS` to send what it holds, then the socket goes."""
        if self.writer.is_alive():
            with self.cond:
                if not self.ended:
                    self.ended, self.ended_at = True, time.monotonic()
                    self.outbox.append(('close', CLOSE_GOING_AWAY))
                self.cond.notify()
            self.writer.join(END_GRACE_SECONDS)
        self.ws.abort()

    # ---- Admission ------------------------------------------------------------------------------------------------

    def stands(self) -> bool:
        """Whether this admitted session still stands (`relay.session_stands`); ends it 4403 when not."""
        if self.ended:
            return False
        verdict = relay.session_stands({'kid': self.kid, 'key': self.key},
                                       self.server.admission.current(self.server.now_ms()))
        if not verdict['ok']:
            self.end(verdict['code'], CLOSE_NOT_ADMITTED, 'session-no-longer-stands')
            return False
        return True

    def run(self) -> None:
        server = self.server
        nonce = p256.b64url_encode(secrets.token_bytes(32))
        self.ws.deadline = time.monotonic() + AUTH_DEADLINE_SECONDS     # from the upgrade, across every byte
        self.writer.start()
        self.send({'type': 'hello', 'protocol': relay.RELAY_PROTOCOL, 'relay': server.origin, 'nonce': nonce,
                   'time': relay.rfc3339(server.now_ms())})
        try:
            text = self.ws.recv_text(self._ping)
        except socket.timeout:
            return self.end('unauthenticated', CLOSE_UNAUTHENTICATED, 'auth-deadline')
        except FrameTooLarge:
            return self.end('too-large', CLOSE_TOO_LARGE, 'auth-too-large')
        except ProtocolError as error:
            return self.drop(error.code, str(error))
        except PeerClosed:
            return self.drop(CLOSE_GOING_AWAY, 'peer-closed')
        try:
            frame = relay.parse_json(text.encode('utf-8'))
        except (ValueError, UnicodeError):
            frame = None
        if not isinstance(frame, dict) or frame.get('v') != 1 or frame.get('type') != 'auth':
            return self.end('unauthenticated', CLOSE_UNAUTHENTICATED, 'not-auth')
        now = server.now_ms()
        admission = server.admission.current(now)
        keys = admission.get('keys', {}) if admission.get('ok') else {}
        verdict = admit(frame, relay_origin=server.origin, estate=admission.get('estate'), nonce=nonce,
                        nonce_used=False, admitted_key=keys.get, now_ms=now)
        if not verdict['ok']:
            # The real reason, for the operator's log: what the relay holds, never what the client sent.
            why = verdict['code'] if verdict['code'] != 'unauthenticated' else (
                'admission-' + admission.get('refusal', '') if not admission.get('ok') else 'not-proven')
            return self.end(verdict['code'], CLOSE_UNAUTHENTICATED, why)
        self.kid, self.key = verdict['kid'], bytes(keys[verdict['kid']])
        if not server.promote(self):
            return self.end('rate-limited', CLOSE_POLICY, 'sessions-full', retryAfter=30)
        self.ws.deadline = None
        self.send({'type': 'welcome', 'session': self.session_id, 'estateRevision': admission['revision'],
                   'deltaHead': admission['deltaHead'], 'heartbeat': None, 'limits': dict(server.limits)})
        server.joined(self)
        self.wake_up()
        try:
            self._serve()
        finally:
            server.left(self)

    def _ping(self, payload: bytes) -> None:
        """A ping's answer replaces any still waiting (RFC 6455 lets a pong answer the latest ping only), counted at
        its size against `OUTBOX_BYTES`, so a caller that pings and never reads holds one pong, not one per ping."""
        with self.cond:
            self.outbox_bytes += len(payload) - (len(self.pong) if self.pong is not None else 0)
            self.pong = payload
            over = self.outbox_bytes > OUTBOX_BYTES
            self.cond.notify()
        if over:
            self.drop(None, 'not-reading')

    def _serve(self) -> None:
        while not self.ended:
            try:
                text = self.ws.recv_text(self._ping)
            except socket.timeout:
                return self.drop(CLOSE_GOING_AWAY, 'idle')
            except FrameTooLarge:
                return self.end('too-large', CLOSE_TOO_LARGE, 'frame-too-large')
            except ProtocolError as error:
                return self.drop(error.code, str(error))
            except PeerClosed as closed:
                return self.drop(closed.code or 1000, 'peer-closed')
            if not self.stands():
                return
            self.server.seen(self.kid)
            try:
                frame = relay.parse_json(text.encode('utf-8'))
            except (ValueError, UnicodeError):
                frame = None
            if not isinstance(frame, dict) or frame.get('v') != 1 or not isinstance(frame.get('type'), str) \
                    or not _valid_id(frame.get('id')):
                self.error('malformed', frame.get('id') if isinstance(frame, dict) else None)
                continue
            try:
                self._handle(frame)
            except Exception as error:                     # noqa: BLE001 - a fault ends this session, never the relay
                self.server.log({'event': 'relay.session.fault', 'kid': self.kid, 'error': type(error).__name__})
                return self.end('internal', CLOSE_INTERNAL, 'fault')

    # ---- Frames ---------------------------------------------------------------------------------------------------

    def _rate(self, kind: str, reply: str) -> bool:
        """Whether a frame of `kind` is within this machine's limit, counted across all its connections; answers
        `rate-limited` (with `retryAfter`) when not, and closes 1008 a connection that sends that type again before
        its `retryAfter` passed."""
        verdict = self.server.rate(self.kid, kind)
        if verdict is None:
            return True
        if verdict == 'again':
            self.end('rate-limited', CLOSE_POLICY, 'kept-sending-' + kind)
            return False
        self.error('rate-limited', reply, retryAfter=verdict)
        self.server.log({'event': 'relay.rate-limited', 'kid': self.kid, 'type': kind, 'retryAfter': verdict})
        return False

    def _handle(self, frame: dict) -> None:
        kind, reply = frame['type'], frame['id']
        if kind == 'send':
            return self._send(frame, reply)
        if kind == 'ack':
            if not _int(frame.get('seq')) or frame['seq'] < 0:
                return self.error('malformed', reply)
            self.server.mailboxes.ack(self.kid, frame['seq'])
            return None
        if kind == 'ping':
            return self.send({'type': 'pong', 'replyTo': reply})
        if kind == 'presence-query':
            return self._presence(frame, reply)
        if kind == 'delta-query':
            return self._delta(frame, reply)
        if kind == 'auth':
            return self.error('malformed', reply)        # admitted once; a second auth is not a frame of a session
        return self.error('unknown-type', reply)          # unknown, relay-only and hub frames alike

    def _send(self, frame: dict, reply: str) -> None:
        to, msg_id, body, ttl = frame.get('to'), frame.get('msgId'), frame.get('body'), frame.get('ttl')
        if not _kid(to) or not to.endswith('#sig') or not _valid_id(msg_id) or not isinstance(body, str) or not body \
                or not set(body) <= _B64URL or len(body) % 4 == 1 or not _int(ttl) \
                or not 1 <= ttl <= relay.MAX_MESSAGE_TTL_SECONDS:
            return self.error('malformed', reply)
        if len(body) * 3 // 4 > relay.MAX_BODY_BYTES:
            return self.error('too-large', reply)
        if not self._rate('send', reply):
            return None
        now = self.server.now_ms()
        admission = self.server.admission.current(now)
        if to not in (admission.get('keys') or {}):
            return self.error('unknown-recipient', reply)
        answer = self.server.mailboxes.append(to, self.kid, msg_id, body, ttl, now)
        if answer.get('full') == 'tombstones':
            # The sender's own bound, not the recipient's mailbox: it retries once its recipients acknowledged.
            self.server.log({'event': 'relay.tombstones-full', 'kid': self.kid})
            return self.error('rate-limited', reply, retryAfter=60)
        if 'full' in answer:
            self.server.log({'event': 'relay.mailbox-full', 'kid': to, 'limit': answer['full']})
            return self.error('mailbox-full', reply)
        self.send({'type': 'accepted', 'replyTo': reply, 'msgId': msg_id, 'seq': answer['seq']})
        if not answer['duplicate']:
            self.server.queued(to)
        return None

    def _presence(self, frame: dict, reply: str) -> None:
        kids = frame.get('kids')
        if not isinstance(kids, list) or len(kids) > MAX_QUERY_KIDS or not all(map(_kid, kids)):
            return self.error('malformed', reply)
        if not self._rate('presence-query', reply):
            return None
        admitted = self.server.admission.current(self.server.now_ms()).get('keys') or {}
        presences, unknown = [], []
        for kid in dict.fromkeys(kids):                    # every asked kid exactly once
            state = self.server.presence_of(kid) if kid in admitted else None
            if state is None:
                unknown.append(kid)
            else:
                presences.append(state)
        return self.send({'type': 'presence-result', 'replyTo': reply, 'presences': presences, 'unknown': unknown})

    def _delta(self, frame: dict, reply: str) -> None:
        start = frame.get('fromSeq')
        if not _int(start) or start < 0:
            return self.error('malformed', reply)
        if not self._rate('delta-query', reply):
            return None
        state = self.server.admission.current(self.server.now_ms()).get('state') or {}
        entries = state.get('entries') or []
        index = max(start, 1) - 1
        while index < len(entries):
            page = entries[index:index + DELTA_PAGE_ENTRIES]
            index += len(page)
            head = revocation.delta_head({'estate': state.get('estate'), 'entries': entries[:index]})
            self.send({'type': 'delta', 'replyTo': reply, 'head': head, 'entries': page})
        return self.send({'type': 'delta', 'replyTo': reply, 'head': revocation.delta_head(state), 'entries': []})


# ---- The server -----------------------------------------------------------------------------------------------------

def fd_caps(soft_limit: Optional[int], open_fds: int) -> Tuple[int, int]:
    """`(pending, sessions)` caps that keep the relay's own descriptors (one per connection) below the process's soft
    limit, `FD_RESERVE` left to the rest of the process; the defaults when the limit leaves room for them or is
    unknown."""
    if not soft_limit or soft_limit < 0 or soft_limit >= 1 << 40:
        return MAX_PENDING, MAX_SESSIONS
    budget = soft_limit - open_fds - FD_RESERVE
    if budget >= MAX_PENDING + MAX_SESSIONS:
        return MAX_PENDING, MAX_SESSIONS
    budget = max(budget, 2)
    pending = max(1, min(MAX_PENDING, budget // 4))
    return pending, max(1, min(MAX_SESSIONS, budget - pending))


def _process_fds() -> Tuple[Optional[int], int]:
    """The soft descriptor limit and how many are open now, where the platform says; `(None, 0)` elsewhere."""
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, OSError, ValueError):
        return None, 0
    for folder in ('/dev/fd', '/proc/self/fd'):
        try:
            return soft, len(os.listdir(folder))
        except OSError:
            continue
    return soft, 0


def _loopback_address(address: str) -> bool:
    try:
        import ipaddress
        return ipaddress.ip_address(address.split('%', 1)[0]).is_loopback
    except ValueError:
        return False


def address_group(address: str) -> str:
    """What a per-address count counts against: an IPv4 address itself (/32), an IPv6 address by its /64 (one host or
    one network's worth of addresses is one caller), an IPv4-mapped IPv6 address as its IPv4; anything else as given."""
    import ipaddress
    try:
        parsed = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(str(parsed) + '/64', strict=False))
    return str(parsed)


_UNSAFE = re.compile(r'[^0-9A-Za-z.:\[\]_-]')


def forwarded_for(headers: Dict[str, str]) -> Tuple[Optional[str], bool]:
    """The caller's address a TLS terminator on this machine forwards: the rightmost `X-Forwarded-For` entry (the one
    the terminator itself appended; those before it are the caller's own words), as an address literal, with a port
    stripped (`192.0.2.1:5678`, `[2001:db8::1]:443`). `(address, False)`, `(None, False)` without the header, and
    `(None, True)` for an entry that is no address."""
    import ipaddress
    value = headers.get('x-forwarded-for')
    if not value:
        return None, False
    last = value.split(',')[-1].strip()
    if last.startswith('['):
        last = last[1:last.find(']')] if ']' in last else ''
    elif last.count(':') == 1:                             # IPv4 with a port; a bare IPv6 address has several colons
        last = last.split(':', 1)[0]
    try:
        return str(ipaddress.ip_address(last)), False
    except ValueError:
        return None, True


def sanitized(value: str, limit: int = 64) -> str:
    """A header value fit for a log line: at most `limit` characters, anything outside a plain address alphabet
    replaced by `?`."""
    return _UNSAFE.sub('?', value[:limit])


class _Pending:
    """A connection not admitted yet. `address` is whom it counts against (`MAX_PENDING_PER_SOURCE`): the socket's
    peer, or the address a trusted terminator forwards. `progress` is 0 until its request head arrived, then 1."""
    __slots__ = ('sock', 'peer', 'address', 'at', 'ws', 'progress', 'session')

    def __init__(self, sock: Optional[socket.socket], address: str, at: Optional[float] = None, progress: int = 0):
        self.sock, self.peer, self.address = sock, address, address_group(address)
        self.at = time.monotonic() if at is None else at
        self.ws, self.progress, self.session = None, progress, None


class RelayServer:
    """The listening relay: `start()` binds and serves on threads of its own, `stop()` ends every connection (1001)
    without waiting on any of them. `origin` is what machines sign into `relay-auth/1`: its loopback `http://` address,
    or the `https://` origin of the TLS terminator in front of it.

    Two separate budgets: connections not admitted yet (`max_pending`, at most `max_pending_per_source` per caller
    address, each within the request-head and auth deadlines; past a cap one gives way by `victim`) and admitted
    sessions (`max_connections`, at most `max_per_kid` per machine). Both stay below the process's descriptor limit
    (`fd_caps`). With `trusted_proxy`, a connection from a loopback peer counts against the address its
    `X-Forwarded-For` names; without it the header is ignored and the peer is the address."""

    def __init__(self, *, origin: str, admission: Admission, mailboxes: Mailboxes, host: str = '127.0.0.1',
                 port: int = 0, limits: Optional[dict] = None, clock: Callable[[], float] = time.time,
                 log: Callable[[dict], None] = _default_log, max_connections: int = MAX_SESSIONS,
                 max_pending: int = MAX_PENDING, max_pending_per_source: int = MAX_PENDING_PER_SOURCE,
                 trusted_proxy: bool = False, max_per_kid: int = MAX_SESSIONS_PER_KID):
        if not relay.is_relay_origin(origin):
            raise ValueError('not a relay origin: https on a host name, or http on a loopback literal')
        self.origin, self.admission, self.mailboxes = origin, admission, mailboxes
        self.host, self.requested_port = host, port
        self.limits = {name: value for name, value in dict(DEFAULT_LIMITS, **(limits or {})).items() if value}
        self.clock, self.log = clock, log
        self.max_connections, self.max_pending = max_connections, max_pending
        self.max_pending_per_source, self.trusted_proxy = max_pending_per_source, trusted_proxy
        self.max_per_kid = max_per_kid
        self._lock = threading.Lock()
        self._sessions: Dict[str, List[Session]] = {}
        self._pending: Dict[int, _Pending] = {}
        self._per_source: Dict[str, int] = {}
        self._last_seen: Dict[str, int] = {}
        self._rates: Dict[Tuple[str, str], collections.deque] = {}
        self._limited_until: Dict[Tuple[str, str], float] = {}
        self._flush_locks: Dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self._accepting = threading.Event()
        self._accept_failing: Optional[Tuple[float, str]] = None
        self._lingering = threading.BoundedSemaphore(MAX_LINGERING)
        self._listener: Any = None
        self.port: Optional[int] = None

    def now_ms(self) -> int:
        return _now_ms(self.clock)

    # ---- Lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> 'RelayServer':
        soft, open_now = _process_fds()
        pending, sessions = fd_caps(soft, open_now)
        if (pending, sessions) != (MAX_PENDING, MAX_SESSIONS):
            self.log({'event': 'relay.fd-caps', 'limit': soft, 'open': open_now, 'maxSessions': sessions,
                      'maxPending': pending})
        self.max_pending = min(self.max_pending, pending)
        self.max_connections = min(self.max_connections, sessions)
        family = socket.AF_INET6 if ':' in self.host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.requested_port))
        listener.listen(64)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._accepting.set()
        threading.Thread(target=self._accept, name='relay-accept', daemon=True).start()
        threading.Thread(target=self._watch, name='relay-watch', daemon=True).start()
        self.log({'event': 'relay.listening', 'host': self.host, 'port': self.port, 'origin': self.origin,
                  'protocol': relay.RELAY_PROTOCOL, 'maxSessions': self.max_connections,
                  'maxPending': self.max_pending})
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()
        with self._lock:
            sessions = [s for group in self._sessions.values() for s in group]
            pending = list(self._pending.values())
        for session in sessions:
            session.ws.abort(CLOSE_GOING_AWAY)
        for entry in pending:
            _shut(entry)
        self.log({'event': 'relay.stopped', 'sessions': len(sessions)})

    def health(self) -> Optional[str]:
        """None while the relay listens and accepts, else why not, for the node's `degraded`."""
        if self._stop.is_set():
            return None
        if not self._accepting.is_set():
            return 'relay: the accept loop ended; restart the node\'s server'
        failing = self._accept_failing
        if failing is not None and time.monotonic() - failing[0] >= ACCEPT_FAILING_SECONDS:
            return 'relay: not accepting connections for %ds (%s)' % (time.monotonic() - failing[0], failing[1])
        return None

    def _accept(self) -> None:
        """Accepts until `stop`. A failed accept (out of descriptors, say) is logged and retried with a backoff, never
        the end of the loop; `health` names a failure that lasts."""
        backoff, logged = 0.05, 0.0
        try:
            while not self._stop.is_set():
                try:
                    sock, address = self._listener.accept()
                except Exception as error:                 # noqa: BLE001 - OSError (EMFILE) or worse: retried
                    if self._stop.is_set():
                        return
                    name = errno.errorcode.get(getattr(error, 'errno', None) or 0, type(error).__name__)
                    if self._accept_failing is None:
                        self._accept_failing = (time.monotonic(), name)
                    if time.monotonic() - logged >= 10:
                        logged = time.monotonic()
                        self.log({'event': 'relay.accept.error', 'error': name})
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, ACCEPT_BACKOFF_SECONDS)
                    continue
                backoff = 0.05
                if self._accept_failing is not None:
                    self.log({'event': 'relay.accept.recovered', 'error': self._accept_failing[1]})
                    self._accept_failing = None
                self._admit_connection(sock, address[0] if isinstance(address, tuple) else str(address))
        finally:
            self._accepting.clear()

    @staticmethod
    def victim(entries: List[_Pending], now: float) -> Optional[_Pending]:
        """Which pending connection gives way to a newcomer: from the address holding the most of `entries`, the one
        that sent nothing before one answering, the oldest first. One answering inside `FAIR_ANSWER_SECONDS` never
        gives way; None when every one is such. So a flood from one address displaces itself before anyone else, and
        a caller that sent its request keeps the time to answer."""
        shares = collections.Counter(e.address for e in entries)
        pool = [e for e in entries if e.progress == 0 or now - e.at >= FAIR_ANSWER_SECONDS]
        return min(pool, key=lambda e: (-shares[e.address], e.progress, e.at), default=None)

    def _count(self, address: str, delta: int) -> None:
        """`_per_source` by `delta`; an address at zero leaves the map (under `_lock`)."""
        left = self._per_source.get(address, 0) + delta
        if left > 0:
            self._per_source[address] = left
        else:
            self._per_source.pop(address, None)

    def _unlist(self, entry: _Pending) -> bool:
        """Takes `entry` out of the pending budget (under `_lock`); False when it was not in it."""
        if self._pending.pop(id(entry), None) is None:
            return False
        self._count(entry.address, -1)
        return True

    def _place(self, entry: _Pending) -> Tuple[Optional[str], Optional[_Pending], bool]:
        """Puts `entry` in the pending budget under its address group (under `_lock`): `(None, evicted-or-None,
        fair)`, or `(refusal, None, False)`.

        Max-min fair across addresses. At its own address's cap, a connection of that address gives way (`victim`,
        whose fair window holds). At the whole budget's cap, a newcomer whose address holds at least two fewer slots
        than the fullest address takes that address's slot, the silent one first, else the oldest, fair window or not
        (`fair` True): so a newcomer from a fresh address gets in while any address holds two or more. Otherwise
        (the move would only swap who holds a slot) one of the fullest addresses gives way under the fair window, or
        the newcomer is refused.

        Behind a trusted terminator a connection counts against the terminator's own address until its request names
        the caller, then moves to the caller's address keeping its slot (it never exceeds the budget then); an
        eviction it made on arrival came from the fullest address, so it never takes a slot from an address holding
        fewer than two."""
        others = [e for e in self._pending.values() if e is not entry]
        same = [e for e in others if e.address == entry.address]
        now, evicted, fair = time.monotonic(), None, False
        if len(same) >= self.max_pending_per_source:
            evicted = self.victim(same, now)
            if evicted is None:
                return 'per-source', None, False
        elif len(others) >= self.max_pending:
            shares = collections.Counter(e.address for e in others)
            most = max(shares.values())
            if len(same) + 2 <= most:
                fullest = [e for e in others if shares[e.address] == most]
                evicted, fair = min(fullest, key=lambda e: (e.progress, e.at)), True
            else:
                evicted = self.victim([e for e in others if shares[e.address] >= len(same)], now)
                if evicted is None:
                    return 'pending-full', None, False
        if evicted is not None:
            self._unlist(evicted)
        self._pending[id(entry)] = entry
        self._count(entry.address, +1)
        return None, evicted, fair

    def _evicted(self, entry: Optional[_Pending], fair: bool = False) -> None:
        if entry is not None:
            _shut(entry)
            self.log({'event': 'relay.pending.evicted', 'progress': entry.progress, 'fair': fair})

    def _refuse(self, sock: socket.socket, refusal: str, unread: bool = False) -> None:
        """Answers 503 and closes. `unread`: the caller's request was never read (refused on arrival), so the socket
        is half-closed and drained before it closes (`_linger`), else the close would reset the connection and a
        Windows caller would lose the answer."""
        self.log({'event': 'relay.refused', 'reason': refusal})
        sock.setblocking(False)
        _respond(sock, 503, _error_body('rate-limited', 'too many connections'))
        if unread and self._lingering.acquire(blocking=False):
            threading.Thread(target=self._linger, args=(sock,), name='relay-linger', daemon=True).start()
            return
        with contextlib.suppress(OSError):
            sock.close()

    def _linger(self, sock: socket.socket) -> None:
        """Sends FIN after the answer, then reads and discards what the caller still sends until it closes, for at
        most `LINGER_SECONDS` and `LINGER_BYTES`; then closes. Holds one of the `MAX_LINGERING` slots throughout."""
        try:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)
            deadline, read = time.monotonic() + LINGER_SECONDS, 0
            while read < LINGER_BYTES and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    sock.settimeout(remaining)
                    chunk = sock.recv(4096)
                except OSError:                            # a timeout, or the caller reset it
                    break
                if not chunk:
                    break
                read += len(chunk)
        finally:
            with contextlib.suppress(OSError):
                sock.close()
            self._lingering.release()

    def _admit_connection(self, sock: socket.socket, peer: str) -> None:
        entry = _Pending(sock, peer)
        with self._lock:
            refusal, evicted, fair = self._place(entry)
        self._evicted(evicted, fair)
        if refusal is not None:
            return self._refuse(sock, refusal, unread=True)
        threading.Thread(target=self._connection, args=(entry,), name='relay-conn', daemon=True).start()

    def _readdress(self, entry: _Pending, headers: Dict[str, str]) -> Optional[str]:
        """After the head: the connection has answered (`progress` 1), and behind a trusted terminator on this machine
        it counts against the caller's forwarded address from now on. A refusal when that address is full."""
        evicted, fair, unreadable = None, False, False
        with self._lock:
            entry.progress = 1
            forwarded = None
            if self.trusted_proxy and _loopback_address(entry.peer):
                forwarded, unreadable = forwarded_for(headers)
            group = address_group(forwarded) if forwarded is not None else None
            if group is not None and group != entry.address and self._unlist(entry):
                entry.address = group
                refusal, evicted, fair = self._place(entry)
            else:
                refusal = None                             # unchanged, or evicted meanwhile (its socket already shut)
        if unreadable:
            # Once per connection, and never more of the header than a short, plain-character excerpt.
            self.log({'event': 'relay.forwarded.unreadable',
                      'value': sanitized(headers.get('x-forwarded-for', '').split(',')[-1].strip())})
        self._evicted(evicted, fair)
        return refusal

    def _release_pending(self, entry: _Pending) -> None:
        with self._lock:
            self._unlist(entry)

    def _watch(self) -> None:
        """Every `REVALIDATE_SECONDS`: every open session still stands, or is closed 4403; a session whose closing
        frames did not go out within `END_GRACE_SECONDS` is cut off. Every minute, expired mail and tombstones go."""
        purged = time.monotonic()
        while not self._stop.wait(REVALIDATE_SECONDS):
            with self._lock:
                sessions = [s for group in self._sessions.values() for s in group]
            for session in sessions:
                try:
                    if session.ended:
                        if session.ended_at and time.monotonic() - session.ended_at > END_GRACE_SECONDS:
                            session.ws.abort()
                    else:
                        session.stands()
                except Exception as error:                 # noqa: BLE001 - the watcher never stops
                    self.log({'event': 'relay.watch.fault', 'error': type(error).__name__})
            if time.monotonic() - purged >= PURGE_SECONDS:
                purged = time.monotonic()
                try:
                    gone = self.mailboxes.purge(self.now_ms())
                    if gone:
                        self.log({'event': 'relay.mail.expired', 'count': gone})
                except Exception as error:                 # noqa: BLE001
                    self.log({'event': 'relay.purge.fault', 'error': type(error).__name__})

    # ---- One connection -------------------------------------------------------------------------------------------

    def _connection(self, entry: _Pending) -> None:
        sock, session = entry.sock, None
        try:
            head, rest = self._read_head(sock, entry.at + REQUEST_HEAD_SECONDS)
            if head is None:
                return
            method, target, headers = head
            refusal = self._readdress(entry, headers)
            if refusal is not None:
                return self._refuse(sock, refusal)
            if target == '/healthz' and method in ('GET', 'HEAD'):
                return _respond(sock, 200, {'ok': True, 'protocol': relay.RELAY_PROTOCOL, 'service': SERVICE})
            if target.startswith('/v1/blobs/'):
                return _respond(sock, 501, _error_body('internal', 'blobs are not carried by this relay yet'))
            if target.split('?', 1)[0] != relay.RELAY_PATH:
                return _respond(sock, 404, _error_body('malformed', 'no such route'))
            if method != 'GET':
                return _respond(sock, 405, _error_body('malformed', 'the relay connects with GET'))
            refusal = _upgrade_refusal(headers)
            if refusal is not None:
                status, message = refusal
                return _respond(sock, status, _error_body('malformed', message),
                                extra='Sec-WebSocket-Version: 13\r\n' if status == 426 else '')
            accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + GUID).encode('ascii'))
                                      .digest()).decode('ascii')
            sock.settimeout(max(0.1, entry.at + REQUEST_HEAD_SECONDS - time.monotonic()))
            sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          'Sec-WebSocket-Accept: %s\r\nSec-WebSocket-Protocol: %s\r\n\r\n'
                          % (accept, relay.RELAY_SUBPROTOCOL)).encode('ascii'))
            entry.ws = ServerSocket(sock, rest)
            session = entry.session = Session(self, entry.ws, entry.address)
            session.run()
        except (OSError, PeerClosed):
            pass
        except Exception as error:                         # noqa: BLE001 - one connection's fault is logged, not fatal
            self.log({'event': 'relay.connection.fault', 'error': type(error).__name__})
        finally:
            self._release_pending(entry)
            if session is not None:
                session.finish()
            with contextlib.suppress(OSError):
                sock.close()

    @staticmethod
    def _read_head(sock: socket.socket, deadline: float):
        """The request head, read against one wall-clock deadline across every byte (a client that sends one byte at a
        time is closed at it like one that sends nothing)."""
        data = b''
        while b'\r\n\r\n' not in data:
            if len(data) > MAX_REQUEST_HEAD_BYTES:
                _respond(sock, 431, _error_body('too-large', 'the request head is over 8 KiB'))
                return None, b''
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, b''
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                return None, b''
            if not chunk:
                return None, b''
            data += chunk
        raw, rest = data.split(b'\r\n\r\n', 1)
        if len(raw) > MAX_REQUEST_HEAD_BYTES:
            _respond(sock, 431, _error_body('too-large', 'the request head is over 8 KiB'))
            return None, b''
        lines = raw.decode('iso-8859-1').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) != 3 or not parts[2].startswith('HTTP/1.'):
            _respond(sock, 400, _error_body('malformed', 'not an HTTP/1.1 request'))
            return None, b''
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(':')
            if not sep:
                continue
            name = name.strip().lower()
            headers[name] = (headers[name] + ', ' + value.strip()) if name in headers else value.strip()
        return (parts[0], parts[1], headers), rest

    # ---- The estate's sessions ------------------------------------------------------------------------------------

    def promote(self, session: Session) -> bool:
        """An admitted session leaves the pending budget for the sessions'. False when the sessions are full; a
        machine at `max_per_kid` connections gives up its oldest (closed 1001)."""
        replaced = None
        with self._lock:
            entry = next((e for e in self._pending.values() if e.ws is session.ws), None)
            if entry is not None:
                self._unlist(entry)
            group = self._sessions.get(session.kid, [])
            total = sum(len(g) for g in self._sessions.values())
            if len(group) >= self.max_per_kid:
                replaced = group.pop(0)
            elif total >= self.max_connections:
                return False
        if replaced is not None:
            self.log({'event': 'relay.session.replaced', 'kid': session.kid})
            replaced.drop(CLOSE_GOING_AWAY, 'replaced')
        return True

    def joined(self, session: Session) -> None:
        session.admitted_at = time.monotonic()
        with self._lock:
            group = self._sessions.setdefault(session.kid, [])
            first = not group
            group.append(session)
            self._last_seen[session.kid] = self.now_ms()
            others = [s for kid, g in self._sessions.items() if kid != session.kid for s in g]
        self.log({'event': 'relay.session.admitted', 'kid': session.kid, 'session': session.session_id})
        if first:
            self._announce(session.kid, 'online', others)

    def left(self, session: Session) -> None:
        with self._lock:
            group = self._sessions.get(session.kid, [])
            if session not in group:
                return                                     # replaced: its machine is still here
            group.remove(session)
            last = not group
            if last:
                self._sessions.pop(session.kid, None)
            self._last_seen[session.kid] = self.now_ms()
            others = [s for g in self._sessions.values() for s in g]
        if last:
            self._announce(session.kid, 'offline', others)

    def _announce(self, kid: str, state: str, sessions: List[Session]) -> None:
        frame = {'type': 'presence', 'kid': kid, 'state': state, 'lastSeen': relay.rfc3339(self.now_ms())}
        for other in sessions:
            if other.stands():
                other.send(dict(frame))

    def rate(self, kid: str, kind: str):
        """None within the machine's per-minute limit for `kind`, counted over all its connections; else the seconds
        to wait, or 'again' when it sends once more before that."""
        limit = self.limits.get(RATE_LIMITED[kind])
        if not limit:
            return None
        now = time.monotonic()
        with self._lock:
            window = self._rates.setdefault((kid, kind), collections.deque())
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) < limit:
                window.append(now)
                return None
            if self._limited_until.get((kid, kind), 0) > now:
                return 'again'
            retry = max(1, math.ceil(window[0] + 60 - now))
            self._limited_until[(kid, kind)] = now + retry
            return retry

    def flush_lock(self, kid: str) -> threading.Lock:
        with self._lock:
            return self._flush_locks.setdefault(kid, threading.Lock())

    def seen(self, kid: str) -> None:
        with self._lock:
            self._last_seen[kid] = self.now_ms()

    def presence_of(self, kid: str) -> Optional[dict]:
        """`{kid, state, lastSeen}` for a machine this relay has seen since it started, else None (`unknown`)."""
        with self._lock:
            seen = self._last_seen.get(kid)
            online = bool(self._sessions.get(kid))
        if seen is None:
            return None
        return {'kid': kid, 'state': 'online' if online else 'offline', 'lastSeen': relay.rfc3339(seen)}

    def queued(self, kid: str) -> None:
        """Wakes the writers of every connection `kid` holds; never waits on any of them."""
        with self._lock:
            sessions = list(self._sessions.get(kid, ()))
        for session in sessions:
            session.wake_up()

    def snapshot(self) -> dict:
        """What `status` reports: counts only."""
        with self._lock:
            return {'pending': len(self._pending), 'sessions': sum(len(g) for g in self._sessions.values()),
                    'machinesOnline': len(self._sessions), 'queued': self.mailboxes.count()}


def _shut(entry: _Pending) -> None:
    if entry.ws is not None:
        entry.ws.abort(CLOSE_UNAUTHENTICATED)
    else:
        with contextlib.suppress(OSError):
            entry.sock.shutdown(socket.SHUT_RDWR)


def _upgrade_refusal(headers: Dict[str, str]) -> Optional[Tuple[int, str]]:
    if headers.get('upgrade', '').lower() != 'websocket' \
            or 'upgrade' not in [t.strip().lower() for t in headers.get('connection', '').split(',')]:
        return 426, 'the relay speaks WebSocket'
    if headers.get('sec-websocket-version') != '13':
        return 426, 'WebSocket version 13'
    try:
        key = base64.b64decode(headers.get('sec-websocket-key', ''), validate=True)
    except ValueError:
        key = b''
    if len(key) != 16:
        return 400, 'a Sec-WebSocket-Key of 16 bytes'
    offered = [t.strip() for t in headers.get('sec-websocket-protocol', '').split(',')]
    if relay.RELAY_SUBPROTOCOL not in offered:
        return 400, 'the subprotocol fabric-relay.v1'
    return None


def _error_body(code: str, message: str) -> dict:
    return {'error': {'code': code, 'message': message}}


def _respond(sock: socket.socket, status: int, body: dict, extra: str = '') -> None:
    data = json.dumps(body, separators=(',', ':')).encode('utf-8')
    reason = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed', 426: 'Upgrade Required',
              431: 'Request Header Fields Too Large', 501: 'Not Implemented', 503: 'Service Unavailable'}[status]
    try:
        sock.sendall(('HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nCache-Control: no-store\r\n'
                      'Content-Length: %d\r\nConnection: close\r\n%s\r\n' % (status, reason, len(data), extra))
                     .encode('ascii') + data)
    except OSError:
        pass


# ---- Built from a workspace -----------------------------------------------------------------------------------------

def build(node: Path, *, origin: str, host: str = '127.0.0.1', port: int = 0, data: Optional[Path] = None,
          memory: bool = False, limits: Optional[dict] = None, trusted_proxy: bool = False,
          log: Callable[[dict], None] = _default_log) -> RelayServer:
    """A relay admitting from this machine's node (`<node>/records/`), its spool under `data` (default
    `<node>/relay/`, owner-only) or, with `memory`, in memory only. Not started."""
    import private_io
    admission = Admission(NodeRecords(Path(node)).read, log=log)
    if memory:
        mailboxes = Mailboxes(None, secrets.token_bytes(32), limits, log=log)
    else:
        folder = Path(data) if data is not None else Path(node) / 'relay'
        private_io.parent(folder)
        mailboxes = Mailboxes(folder / 'spool.sqlite', load_or_create_spool_key(folder), limits, log=log)
    return RelayServer(origin=origin, admission=admission, mailboxes=mailboxes, host=host, port=port, limits=limits,
                       trusted_proxy=trusted_proxy, log=log)
