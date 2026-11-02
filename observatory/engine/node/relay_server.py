"""The interim relay (OBS-287, the relay project's RLY-04): a `fabric-relay/0.1` server for one person's own estate,
until the hosted relay (RLY-05) runs (contract `src/relay.ts`, DEC-0047, DEC-0066; relay.md §Transport, §Admission,
§Messages).

It runs on the person's always-on machine, inside the node's own server (MACHINE-UTILITY.md D1: a thread of the local
server, no unit of its own) or in the foreground (`full relay serve`). It admits from the estate record this machine's
node verified and applied (`node/records/`, `org.apply_record`), never from a configured list: the admission set is
`relay.peer_keys` over that record and the revocations of the delta entries the node applied.

What it carries, and how:

- `GET /v1/connect` with the subprotocol `fabric-relay.v1`: `hello` with a fresh 32-byte nonce, the machine's `auth`
  within 10 s checked by `admit` (the reference's order and its one `unauthenticated` answer, with one signature's work
  on every path past the frame's own bytes), then `welcome` with the revision, the delta head and the limits;
- `send` to a recipient's mailbox (`accepted` with its per-mailbox `seq`), refused `malformed`, `too-large`,
  `rate-limited` (with `retryAfter`), `unknown-recipient` or `mailbox-full`; a repeat of `(from, msgId)` within its
  `ttl` answers the first `seq`, after an acknowledgement too;
- `deliver` in `seq` order to every connection the recipient holds, deleted on `ack` or when the `ttl` the sender set
  ends (the node signs `exp` at `iat + ttl`, so the mailbox keeps a body no longer than its signed life);
- `presence` pushed when a machine comes and goes, `presence-query`, `ping`;
- `delta-query`, answered from the delta entries this machine's node applied (read-only).

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
import hashlib
import json
import logging
import math
import os
import secrets
import socket
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import estate as estate_rules, jcs, p256, relay, revocation
from .ws import GUID, MAX_FRAGMENTS, OP_BINARY, OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT

LOG = logging.getLogger('observatory.node.relay_server')

SERVICE = 'observatory-relay'
AUTH_DEADLINE_SECONDS = 10
IDLE_CLOSE_SECONDS = 90
REQUEST_HEAD_SECONDS = 10
MAX_REQUEST_HEAD_BYTES = 8192
#: Connections one relay holds at once, those still sending their request head included; one more is answered 503.
MAX_CONNECTIONS = 128
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
                fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
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
              expires_at INTEGER NOT NULL, PRIMARY KEY (sender, msg_id));''')

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

    def pending(self, to: str, now_ms: int, after_seq: int = 0) -> List[dict]:
        """Unacknowledged, unexpired messages after `after_seq`, in `seq` order; expired ones are deleted on the way,
        and one whose seal does not open (another key, a damaged row) is deleted and logged, never delivered."""
        from cryptography.exceptions import InvalidTag
        with self._lock:
            self._db.execute('DELETE FROM mail WHERE recipient = ? AND expires_at <= ?', (to, now_ms))
            rows = self._db.execute('SELECT seq, sender, msg_id, sealed, sent_at FROM mail WHERE recipient = ? '
                                    'AND seq > ? ORDER BY seq', (to, after_seq)).fetchall()
        out, broken = [], []
        for seq, sender, msg_id, sealed, sent_at in rows:
            try:
                body = self._aead.decrypt(sealed[:12], sealed[12:], self._aad(to, sender, msg_id, seq)).decode('ascii')
            except (InvalidTag, ValueError, UnicodeDecodeError):
                broken.append(seq)
                continue
            out.append({'from': sender, 'msgId': msg_id, 'seq': seq, 'body': body, 'sentAt': sent_at})
        if broken:
            with self._lock:
                self._db.executemany('DELETE FROM mail WHERE recipient = ? AND seq = ?', [(to, s) for s in broken])
            self.log({'event': 'relay.spool.unreadable', 'kid': to, 'count': len(broken)})
        return out

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
    pass


class ServerSocket:
    """RFC 6455 from the server's side: client frames masked, ours not; text messages of at most 1 MiB in at most
    `MAX_FRAGMENTS` frames; a ping answered; a close echoed."""

    def __init__(self, sock: socket.socket, buffered: bytes = b''):
        self.sock = sock
        self.buffer = buffered
        self.lock = threading.Lock()
        self.closed = False

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
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

    def recv_text(self) -> str:
        """The next text message; raises `FrameTooLarge`, `ProtocolError` (a binary message: 1003, text not UTF-8:
        1007, anything else out of order: 1002), `PeerClosed` and `socket.timeout`."""
        parts: List[bytes] = []
        total = 0
        while True:
            fin, opcode, payload = self._frame()
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else None
                self.close(code if code and (1000 <= code <= 1014 or 3000 <= code <= 4999) else 1000)
                raise PeerClosed()
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

    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            header = struct.pack('!BB', 0x80 | opcode, n)
        elif n < 65536:
            header = struct.pack('!BBH', 0x80 | opcode, 126, n)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 127, n)
        with self.lock:
            if self.closed and opcode != OP_CLOSE:
                raise PeerClosed()
            self.sock.sendall(header + payload)

    def send_json(self, frame: dict) -> None:
        self._send(OP_TEXT, json.dumps(frame, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))

    def close(self, code: int) -> None:
        """A close frame with `code`, once, then the socket shut both ways (which wakes a blocked reader)."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.sock.sendall(struct.pack('!BBH', 0x80 | OP_CLOSE, 2, code))
            except OSError:
                pass
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
    """One connection: `hello`, `auth`, `welcome`, then the frames of an admitted machine."""

    def __init__(self, server: 'RelayServer', ws: ServerSocket, peer: str):
        self.server, self.ws, self.peer = server, ws, peer
        self.kid: Optional[str] = None
        self.key: Optional[bytes] = None
        self.session_id = secrets.token_urlsafe(18)
        self.delivered = 0
        self.flush_lock = threading.Lock()
        self.windows: Dict[str, collections.deque] = {kind: collections.deque() for kind in RATE_LIMITED}
        self.limited_until: Dict[str, float] = {}
        self.ended = False

    # ---- Sending --------------------------------------------------------------------------------------------------

    def _id(self) -> str:
        return secrets.token_urlsafe(9)

    def send(self, frame: dict) -> None:
        self.ws.send_json(dict({'v': 1, 'id': self._id()}, **frame))

    def error(self, code: str, reply_to: Any = None, **extra) -> None:
        frame = {'type': 'error', 'code': code, 'message': MESSAGES[code]}
        if _valid_id(reply_to):
            frame['replyTo'] = reply_to
        frame.update(extra)
        try:
            self.send(frame)
        except (OSError, PeerClosed):
            pass

    def end(self, code: str, close: int, reason: str) -> None:
        """An `error` of `code`, then the close `close`; the reason goes to the log only."""
        if self.ended:
            return
        self.ended = True
        self.error(code)
        self.ws.close(close)
        self.server.log({'event': 'relay.session.closed', 'kid': self.kid, 'code': code, 'close': close,
                         'reason': reason})

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
        self.send({'type': 'hello', 'protocol': relay.RELAY_PROTOCOL, 'relay': server.origin, 'nonce': nonce,
                   'time': relay.rfc3339(server.now_ms())})
        self.ws.sock.settimeout(AUTH_DEADLINE_SECONDS)
        try:
            text = self.ws.recv_text()
        except socket.timeout:
            return self.end('unauthenticated', CLOSE_UNAUTHENTICATED, 'auth-deadline')
        except FrameTooLarge:
            return self.end('too-large', CLOSE_TOO_LARGE, 'auth-too-large')
        except ProtocolError as error:
            self.ended = True
            self.ws.close(error.code)
            return server.log({'event': 'relay.session.closed', 'close': error.code, 'reason': str(error)})
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
        self.send({'type': 'welcome', 'session': self.session_id, 'estateRevision': admission['revision'],
                   'deltaHead': admission['deltaHead'], 'heartbeat': None, 'limits': dict(server.limits)})
        server.joined(self)
        try:
            self.flush()
            self.ws.sock.settimeout(IDLE_CLOSE_SECONDS)
            self._serve()
        finally:
            server.left(self)

    def _serve(self) -> None:
        while not self.ended:
            try:
                text = self.ws.recv_text()
            except socket.timeout:
                self.ended = True
                self.ws.close(CLOSE_GOING_AWAY)
                return self.server.log({'event': 'relay.session.closed', 'kid': self.kid, 'close': CLOSE_GOING_AWAY,
                                        'reason': 'idle'})
            except FrameTooLarge:
                return self.end('too-large', CLOSE_TOO_LARGE, 'frame-too-large')
            except ProtocolError as error:
                self.ended = True
                self.ws.close(error.code)
                return self.server.log({'event': 'relay.session.closed', 'kid': self.kid, 'close': error.code,
                                        'reason': str(error)})
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
            except (OSError, PeerClosed):
                raise
            except Exception as error:                     # noqa: BLE001 - a fault ends this session, never the relay
                self.server.log({'event': 'relay.session.fault', 'kid': self.kid, 'error': type(error).__name__})
                return self.end('internal', CLOSE_INTERNAL, 'fault')

    # ---- Frames ---------------------------------------------------------------------------------------------------

    def _rate(self, kind: str, reply: str) -> bool:
        """Whether a frame of `kind` is within its limit; answers `rate-limited` (with `retryAfter`) when not, and
        closes 1008 a node that sends that type again before its `retryAfter` passed."""
        limit = self.server.limits.get(RATE_LIMITED[kind])
        if not limit:
            return True
        now = time.monotonic()
        window = self.windows[kind]
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) < limit:
            window.append(now)
            return True
        if self.limited_until.get(kind, 0) > now:
            self.end('rate-limited', CLOSE_POLICY, 'kept-sending-' + kind)
            return False
        retry = max(1, math.ceil(window[0] + 60 - now))
        self.limited_until[kind] = now + retry
        self.error('rate-limited', reply, retryAfter=retry)
        self.server.log({'event': 'relay.rate-limited', 'kid': self.kid, 'type': kind, 'retryAfter': retry})
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

    # ---- Delivery -------------------------------------------------------------------------------------------------

    def flush(self) -> None:
        """Delivers what this machine's mailbox holds past what this connection was sent, in `seq` order."""
        if self.kid is None or self.ended:
            return
        with self.flush_lock:
            if not self.stands():
                return
            for message in self.server.mailboxes.pending(self.kid, self.server.now_ms(), self.delivered):
                try:
                    self.send(dict({'type': 'deliver'}, **message))
                except (OSError, PeerClosed):
                    return
                self.delivered = message['seq']


# ---- The server -----------------------------------------------------------------------------------------------------

class RelayServer:
    """The listening relay: `start()` binds and serves on threads of its own, `stop()` ends every session (1001) and
    stops. `origin` is what machines sign into `relay-auth/1`: its loopback `http://` address, or the `https://` origin
    of the TLS terminator in front of it."""

    def __init__(self, *, origin: str, admission: Admission, mailboxes: Mailboxes, host: str = '127.0.0.1',
                 port: int = 0, limits: Optional[dict] = None, clock: Callable[[], float] = time.time,
                 log: Callable[[dict], None] = _default_log, max_connections: int = MAX_CONNECTIONS):
        if not relay.is_relay_origin(origin):
            raise ValueError('not a relay origin: https on a host name, or http on a loopback literal')
        self.origin, self.admission, self.mailboxes = origin, admission, mailboxes
        self.host, self.requested_port = host, port
        self.limits = {name: value for name, value in dict(DEFAULT_LIMITS, **(limits or {})).items() if value}
        self.clock, self.log, self.max_connections = clock, log, max_connections
        self._lock = threading.Lock()
        self._sessions: Dict[str, set] = {}
        self._open: set = set()
        self._active = 0
        self._last_seen: Dict[str, int] = {}
        self._stop = threading.Event()
        self._listener: Optional[socket.socket] = None
        self.port: Optional[int] = None

    def now_ms(self) -> int:
        return _now_ms(self.clock)

    # ---- Lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> 'RelayServer':
        family = socket.AF_INET6 if ':' in self.host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.requested_port))
        listener.listen(64)
        self._listener = listener
        self.port = listener.getsockname()[1]
        threading.Thread(target=self._accept, name='relay-accept', daemon=True).start()
        threading.Thread(target=self._watch, name='relay-watch', daemon=True).start()
        self.log({'event': 'relay.listening', 'host': self.host, 'port': self.port, 'origin': self.origin,
                  'protocol': relay.RELAY_PROTOCOL})
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            sockets = list(self._open)
        for ws in sockets:
            ws.close(CLOSE_GOING_AWAY)
        self.log({'event': 'relay.stopped', 'sessions': len(sockets)})

    def _accept(self) -> None:
        """One thread per connection, at most `max_connections` at once counting those still sending their request
        head: one more is answered 503 here and closed, so a flood of idle sockets cannot grow the threads."""
        while not self._stop.is_set():
            try:
                sock, address = self._listener.accept()
            except OSError:
                return
            with self._lock:
                full = self._active >= self.max_connections
                if not full:
                    self._active += 1
            if full:
                self.log({'event': 'relay.refused', 'reason': 'too-many-connections'})
                try:
                    sock.settimeout(1.0)
                    _respond(sock, 503, _error_body('rate-limited', 'too many connections'))
                finally:
                    sock.close()
                continue
            threading.Thread(target=self._connection, args=(sock, address[0]), name='relay-conn',
                             daemon=True).start()

    def _watch(self) -> None:
        """Every two seconds: every open session still stands, or is closed 4403; every minute, expired mail goes."""
        purged = time.monotonic()
        while not self._stop.wait(REVALIDATE_SECONDS):
            with self._lock:
                sessions = [s for group in self._sessions.values() for s in group]
            for session in sessions:
                try:
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

    def _connection(self, sock: socket.socket, peer: str) -> None:
        ws = None
        try:
            sock.settimeout(REQUEST_HEAD_SECONDS)
            head, rest = self._read_head(sock)
            if head is None:
                return
            method, target, headers = head
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
            ws = ServerSocket(sock, rest)
            with self._lock:
                self._open.add(ws)
            accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + GUID).encode('ascii'))
                                      .digest()).decode('ascii')
            sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          'Sec-WebSocket-Accept: %s\r\nSec-WebSocket-Protocol: %s\r\n\r\n'
                          % (accept, relay.RELAY_SUBPROTOCOL)).encode('ascii'))
            Session(self, ws, peer).run()
        except (OSError, PeerClosed):
            pass
        except Exception as error:                         # noqa: BLE001 - one connection's fault is logged, not fatal
            self.log({'event': 'relay.connection.fault', 'error': type(error).__name__})
            if ws is not None:
                ws.close(CLOSE_INTERNAL)
        finally:
            with self._lock:
                self._active -= 1
                if ws is not None:
                    self._open.discard(ws)
            if ws is not None:
                ws.close(CLOSE_GOING_AWAY)
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_head(sock: socket.socket):
        data = b''
        while b'\r\n\r\n' not in data:
            if len(data) > MAX_REQUEST_HEAD_BYTES:
                _respond(sock, 431, _error_body('too-large', 'the request head is over 8 KiB'))
                return None, b''
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

    def joined(self, session: Session) -> None:
        with self._lock:
            group = self._sessions.setdefault(session.kid, set())
            first = not group
            group.add(session)
            self._last_seen[session.kid] = self.now_ms()
            others = [s for kid, g in self._sessions.items() if kid != session.kid for s in g]
        self.log({'event': 'relay.session.admitted', 'kid': session.kid, 'session': session.session_id})
        if first:
            self._announce(session.kid, 'online', others)

    def left(self, session: Session) -> None:
        with self._lock:
            group = self._sessions.get(session.kid, set())
            group.discard(session)
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
                try:
                    other.send(dict(frame))
                except (OSError, PeerClosed):
                    pass

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
        with self._lock:
            sessions = list(self._sessions.get(kid, ()))
        for session in sessions:
            session.flush()

    def snapshot(self) -> dict:
        """What `status` reports: counts only."""
        with self._lock:
            return {'connections': len(self._open), 'machinesOnline': len(self._sessions),
                    'queued': self.mailboxes.count()}


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
          memory: bool = False, limits: Optional[dict] = None,
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
                       log=log)
