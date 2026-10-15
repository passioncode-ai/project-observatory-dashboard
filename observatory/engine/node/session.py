"""One node's connection to a relay (contract `fabric-relay/0.1`, relay.md §Transport, §Admission, §Messages).

`RelaySession.open` connects to `wss://<relay>/v1/connect` with the subprotocol `fabric-relay.v1`, reads `hello`,
checks that the relay names the origin this node dialled (a node never signs a nonce for another relay), answers `auth`
signed by the device's `#sig` key and waits for `welcome`. A refusal is `RelayRefused` with the relay's code; a 4401
carries no reason by design, so the error says what the person can check.

After `welcome` one reader thread owns the socket's input:

- an answer (`accepted`, `pong`, `presence-result`, an `error` with `replyTo`) wakes the call that sent its frame;
- a `deliver` is checked end to end (`verify_message_body` against this machine's own kid, then `check_replay`),
  handed to `on_message`, and acknowledged only after the handler returned, so a message the handler could not take is
  delivered again (at least once). A refused or repeated body is acknowledged without acting on it;
- `presence`, `heartbeat` and `delta` go to their callbacks unchanged (the estate record's checks are slice 3);
- a closing `error` and the close code end the session: 4403 means this machine left the estate's admission set or
  the record expired.

Logs carry event names, kids, codes and counts; never a body, a payload or a signature.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from . import p256, relay
from .ws import Closed, WebSocket, WebSocketError

LOG = logging.getLogger('observatory.node.session')
HELLO_TIMEOUT_SECONDS = 10.0
ANSWER_TIMEOUT_SECONDS = 15.0
PING_SECONDS = 30.0
DEFAULT_TTL_SECONDS = 3600


class RelayError(Exception):
    """The relay answered a frame with `error`; `code` is its code, `retry_after` its hint for `rate-limited`."""

    def __init__(self, code: str, message: str = '', retry_after: Optional[int] = None):
        super().__init__(code + (': ' + message if message else ''))
        self.code = code
        self.retry_after = retry_after


class RelayRefused(RelayError):
    """The relay did not admit this machine."""

    ADVICE = ('the relay names no reason by design: check that this machine is in the estate\'s current revision '
              '(not revoked, its device key the one the record lists), then that this client is current')


class SessionClosed(Exception):
    def __init__(self, code: int, reason: str = ''):
        super().__init__(f'the relay session closed ({code})')
        self.code = code
        self.reason = reason


class ReplayStore:
    """Each accepted `(from, msgId)` until its signed `exp` (relay.md §Messages, the replay window). In memory; a node
    that restarts within a message's life may act on a redelivery once more, which the payload's own idempotency key
    (the task's) absorbs."""

    def __init__(self):
        self._seen: Dict[tuple, int] = {}
        self._lock = threading.Lock()

    def seen(self, sender: str, msg_id: str) -> bool:
        with self._lock:
            return (sender, msg_id) in self._seen

    def add(self, sender: str, msg_id: str, exp_ms: int, now_ms: int) -> None:
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if v > now_ms}
            self._seen[(sender, msg_id)] = exp_ms


@dataclass
class Welcome:
    session: str
    estate_revision: int
    delta_head: Any
    heartbeat: Any
    limits: dict = field(default_factory=dict)


def connect_url(origin: str) -> str:
    """The WebSocket URL of a relay origin: `https://h` → `wss://h/v1/connect`, `http://127.0.0.1:p` → `ws://...`."""
    if not relay.is_relay_origin(origin):
        raise ValueError('not a relay origin')
    return ('wss://' + origin[len('https://'):] if origin.startswith('https://')
            else 'ws://' + origin[len('http://'):]) + relay.RELAY_PATH


def _frame_id() -> str:
    return secrets.token_urlsafe(9)


class RelaySession:
    def __init__(self, origin: str, keys, estate: str, sender_key: Callable[[str], Any], *,
                 on_message: Callable[[dict], None], on_presence: Callable[[dict], None] = lambda frame: None,
                 on_record: Callable[[dict], None] = lambda frame: None,
                 on_refused: Callable[[str, dict], None] = lambda reason, frame: None,
                 clock: Callable[[], float] = time.time, replay: Optional[ReplayStore] = None,
                 connector: Callable[..., WebSocket] = WebSocket.connect):
        connect_url(origin)
        self.origin = origin
        self.keys = keys
        self.estate = estate
        self.sender_key = sender_key
        self.on_message, self.on_presence, self.on_record, self.on_refused = on_message, on_presence, on_record, on_refused
        self.clock = clock
        self.replay = replay or ReplayStore()
        self._connector = connector
        self._ws: Optional[WebSocket] = None
        self._waiters: Dict[str, list] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self.close_code: Optional[int] = None
        self.welcome: Optional[Welcome] = None

    # ---- Opening ---------------------------------------------------------------------------------------------------

    def open(self) -> Welcome:
        ws = self._connector(connect_url(self.origin), subprotocol=relay.RELAY_SUBPROTOCOL)
        try:
            ws.settimeout(HELLO_TIMEOUT_SECONDS)
            hello = self._json(ws.recv_text())
            if hello.get('type') != 'hello' or hello.get('protocol') != relay.RELAY_PROTOCOL \
                    or not isinstance(hello.get('nonce'), str):
                raise RelayRefused('protocol-unsupported', 'the relay did not say hello in fabric-relay/0.1')
            if hello.get('relay') != self.origin:
                # The signed origin is what keeps a nonce signed for one relay from admitting this machine to another.
                raise RelayRefused('unauthenticated', 'the relay names another origin than the one dialled')
            now_ms = int(self.clock() * 1000)
            frame = relay.auth_frame(self.keys.sign, frame_id=_frame_id(), relay=self.origin, nonce=hello['nonce'],
                                     estate=self.estate, kid=self.keys.sig_kid, iat=relay.rfc3339(now_ms))
            ws.send_text(json.dumps(frame, separators=(',', ':')))
            answer = self._json(ws.recv_text())
            if answer.get('type') == 'error':
                raise RelayRefused(str(answer.get('code')), RelayRefused.ADVICE)
            if answer.get('type') != 'welcome' or not isinstance(answer.get('session'), str):
                raise RelayRefused('malformed', 'the relay answered auth with ' + str(answer.get('type')))
            self.welcome = Welcome(answer['session'], answer.get('estateRevision'), answer.get('deltaHead'),
                                   answer.get('heartbeat'), answer.get('limits') or {})
        except Closed as closed:
            ws.close()
            raise RelayRefused('unauthenticated' if closed.code == 4401 else 'closed-%d' % closed.code,
                               RelayRefused.ADVICE) from None
        except BaseException:
            ws.close()
            raise
        ws.settimeout(None)
        self._ws = ws
        LOG.info('relay.session.open', extra={'kid': self.keys.sig_kid, 'revision': self.welcome.estate_revision})
        threading.Thread(target=self._read, name='relay-reader', daemon=True).start()
        threading.Thread(target=self._keepalive, name='relay-ping', daemon=True).start()
        return self.welcome

    @staticmethod
    def _json(text: str) -> dict:
        try:
            value = json.loads(text)
        except ValueError:
            raise RelayError('malformed', 'a frame that is not JSON') from None
        if not isinstance(value, dict) or value.get('v') != 1:
            raise RelayError('malformed', 'a frame that is not {v: 1, type, id}')
        return value

    # ---- Calls -----------------------------------------------------------------------------------------------------

    def _call(self, frame: dict, timeout: float = ANSWER_TIMEOUT_SECONDS) -> dict:
        if self._ws is None or self._closed.is_set():
            raise SessionClosed(self.close_code or 1006)
        done, box = threading.Event(), {}
        with self._lock:
            self._waiters[frame['id']] = [done, box]
        try:
            self._ws.send_text(json.dumps(frame, separators=(',', ':')))
            if not done.wait(timeout):
                raise TimeoutError('the relay did not answer ' + frame['type'])
        finally:
            with self._lock:
                self._waiters.pop(frame['id'], None)
        answer = box.get('frame')
        if answer is None:
            raise SessionClosed(self.close_code or 1006)
        if answer.get('type') == 'error':
            raise RelayError(str(answer.get('code')), str(answer.get('message', '')), answer.get('retryAfter'))
        return answer

    def send(self, to: str, payload: Any, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
             msg_id: Optional[str] = None) -> int:
        """Signs `payload` to the machine `to` (its `#sig` kid), queues it at the relay and returns its mailbox `seq`.
        `exp` is signed at `now + ttl`; the relay keeps it no longer than `ttl`. A repeat with the same `msg_id` answers
        the first `seq`."""
        if not 1 <= ttl_seconds <= relay.MAX_MESSAGE_TTL_SECONDS:
            raise ValueError('ttl is 1 s to 7 days')
        now_ms = int(self.clock() * 1000)
        msg_id = msg_id or secrets.token_hex(16)
        body = relay.message_body(self.keys.sign, sender=self.keys.sig_kid, to=to, msg_id=msg_id,
                                  iat=relay.rfc3339(now_ms), exp=relay.rfc3339(now_ms + ttl_seconds * 1000),
                                  payload=payload)
        answer = self._call({'v': 1, 'type': 'send', 'id': _frame_id(), 'to': to, 'msgId': msg_id,
                             'body': p256.b64url_encode(body.encode('ascii')), 'ttl': ttl_seconds})
        if answer.get('type') != 'accepted' or answer.get('msgId') != msg_id or not isinstance(answer.get('seq'), int):
            raise RelayError('malformed', 'the relay answered send with ' + str(answer.get('type')))
        return answer['seq']

    def presence(self, kids) -> dict:
        kids = list(kids)
        if len(kids) > 256:
            raise ValueError('at most 256 kids a query')
        answer = self._call({'v': 1, 'type': 'presence-query', 'id': _frame_id(), 'kids': kids})
        if answer.get('type') != 'presence-result':
            raise RelayError('malformed', 'the relay answered presence-query with ' + str(answer.get('type')))
        return {'presences': answer.get('presences', []), 'unknown': answer.get('unknown', [])}

    def ping(self) -> None:
        self._call({'v': 1, 'type': 'ping', 'id': _frame_id()})

    def close(self) -> None:
        self._closed.set()
        if self._ws is not None:
            self._ws.close(1000)

    def wait_closed(self, timeout: Optional[float] = None) -> bool:
        return self._closed.wait(timeout)

    # ---- The reader ------------------------------------------------------------------------------------------------

    def _read(self) -> None:
        code, reason = 1006, ''
        try:
            while True:
                try:
                    frame = self._json(self._ws.recv_text())
                except RelayError:
                    LOG.warning('relay.frame.malformed')
                    continue
                self._dispatch(frame)
        except Closed as closed:
            code, reason = closed.code, closed.reason
        except (WebSocketError, OSError) as error:
            reason = type(error).__name__
        finally:
            self.close_code = code
            self._closed.set()
            with self._lock:
                waiters = list(self._waiters.values())
            for done, _ in waiters:
                done.set()
            LOG.info('relay.session.closed', extra={'code': code})

    def _dispatch(self, frame: dict) -> None:
        reply = frame.get('replyTo')
        if isinstance(reply, str):
            with self._lock:
                waiter = self._waiters.get(reply)
            if waiter is not None:
                waiter[1]['frame'] = frame
                waiter[0].set()
            return
        kind = frame.get('type')
        if kind == 'deliver':
            self._deliver(frame)
        elif kind == 'presence':
            self.on_presence(frame)
        elif kind in ('heartbeat', 'delta'):
            self.on_record(frame)
        elif kind == 'error':
            # An unsolicited error precedes a close: 4403 not-admitted or expired, 4401, 1009, 1008, 1011.
            LOG.warning('relay.session.error', extra={'code': frame.get('code')})

    def _ack(self, seq: Any) -> None:
        if isinstance(seq, int) and seq >= 0:
            self._ws.send_text(json.dumps({'v': 1, 'type': 'ack', 'id': _frame_id(), 'seq': seq},
                                          separators=(',', ':')))

    def _deliver(self, frame: dict) -> None:
        now_ms = int(self.clock() * 1000)
        raw = p256.b64url_decode(frame.get('body')) if isinstance(frame.get('body'), str) else None
        try:
            body = raw.decode('ascii') if raw is not None else None
        except UnicodeDecodeError:
            body = None
        expect = {'from': frame.get('from'), 'to': self.keys.sig_kid, 'msgId': frame.get('msgId')}
        verdict = relay.verify_message_body(body, expect, self.sender_key, now_ms) if body else {'ok': False,
                                                                                                'reason': 'malformed'}
        if verdict['ok']:
            verdict = dict(relay.check_replay(self.replay.seen, verdict['payload'], now_ms), payload=verdict['payload'])
        if not verdict['ok']:
            LOG.warning('relay.deliver.refused', extra={'reason': verdict['reason'], 'from': frame.get('from')})
            self.on_refused(verdict['reason'], frame)
            self._ack(frame.get('seq'))
            return
        payload = verdict['payload']
        try:
            self.on_message(payload)
        except Exception:                                  # not acknowledged: the relay delivers it again
            LOG.exception('relay.deliver.handler-failed')
            return
        self.replay.add(payload['from'], payload['msgId'], relay.instant(payload['exp']), now_ms)
        self._ack(frame.get('seq'))

    def _keepalive(self) -> None:
        while not self._closed.wait(PING_SECONDS):
            try:
                self.ping()
            except (RelayError, SessionClosed, TimeoutError, OSError):
                LOG.warning('relay.ping.failed')
