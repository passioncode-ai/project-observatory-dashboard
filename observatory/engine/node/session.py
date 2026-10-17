"""One node's connection to a relay (contract `fabric-relay/0.1`, relay.md §Transport, §Admission, §Messages).

`RelaySession.open` connects to `wss://<relay>/v1/connect` with the subprotocol `fabric-relay.v1`, reads `hello`,
checks that the relay names the origin this node dialled (a node never signs a nonce for another relay), answers `auth`
signed by the device's `#sig` key and waits for `welcome`. A refusal is `RelayRefused` with the relay's code; a 4401
carries no reason by design, so the error says what the person can check.

After `welcome` one reader thread owns the socket's input:

- an answer (`accepted`, `pong`, `presence-result`, an `error` with `replyTo`) wakes the call that sent its frame;
- a `deliver` is checked end to end (`verify_message_body` against this machine's own kid, then `check_replay`),
  handed to `on_message`, and acknowledged only after the handler returned, so a message the handler could not take
  stays in the mailbox and is delivered again on the next connection (at least once). A refused, repeated or unreadable
  body is acknowledged, then reported to `on_refused`, without acting on it;
- `presence`, `heartbeat` and `delta` go to their callbacks unchanged (the estate record's checks are slice 3);
- a closing `error` and the close code end the session: 4403 means this machine left the estate's admission set or
  the record expired.

Nothing a relay sends ends the reader except a close: a callback that raises, a body no check can read and a frame
nested too deep are logged and skipped. A link that answers nothing (two missed pings, or 75 s of silence) is closed
as dead. `keep_connected` reopens a closed session with a jittered backoff, and stops for good on 4401 or 4403.

Logs carry event names, kids, codes and counts; never a body, a payload or a signature.
"""
from __future__ import annotations

import json
import logging
import random
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from . import jcs, p256, relay
from .ws import Closed, WebSocket, WebSocketError

LOG = logging.getLogger('observatory.node.session')
HELLO_TIMEOUT_SECONDS = 10.0
ANSWER_TIMEOUT_SECONDS = 15.0
PING_SECONDS = 30.0
#: Pings unanswered in a row before the link counts as dead (a half-open TCP connection never reports itself).
DEAD_AFTER_MISSED_PINGS = 2
#: Silence after which the reader gives up: the node pings every 30 s and the relay answers each, so 75 s of nothing
#: is a dead link, closed before the relay's own 90 s idle rule.
RECV_TIMEOUT_SECONDS = 2.5 * PING_SECONDS
DEFAULT_TTL_SECONDS = 3600
#: Close codes after which a node does not reconnect: the relay refused this machine (4401) or it left the estate's
#: admission set (4403). Reconnecting could only be refused again; the person has to look.
TERMINAL_CLOSE_CODES = (4401, 4403)
#: Refusals a relay or an `auth` answer may give that no retry can change until a person acts.
TERMINAL_REFUSALS = ('unauthenticated', 'protocol-unsupported', 'not-admitted', 'expired')
#: Refusals of a delivered body that this machine may outgrow: a sender its estate record does not list yet (a newer
#: revision is on its way) and a body dated ahead of its clock. Such a body is reported and left unacknowledged, so
#: the relay delivers it again on the next connection, until its ttl.
TRANSIENT_REFUSALS = ('unknown-sender', 'not-yet-valid')


_NONCE = re.compile(r'[A-Za-z0-9_-]{43}')


def retry_after_seconds(value: Any, ceiling: float) -> float:
    """A relay's `retryAfter` as a wait: a finite number clamped to [0, ceiling]; anything else is no wait."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        return 0.0
    if seconds != seconds or seconds in (float('inf'), float('-inf')):
        return 0.0
    return min(max(seconds, 0.0), ceiling)


class _LocalFailure(Exception):
    """This node's own lookup or store failed while it checked a body: the body may be authentic."""


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
        self.close_reason: str = ''
        self.welcome: Optional[Welcome] = None

    # ---- Opening ---------------------------------------------------------------------------------------------------

    def open(self) -> Welcome:
        ws = self._connector(connect_url(self.origin), subprotocol=relay.RELAY_SUBPROTOCOL)
        deadline = time.monotonic() + HELLO_TIMEOUT_SECONDS          # one limit for hello, auth and welcome
        try:
            hello = self._json(ws.recv_text(deadline))
            if hello.get('type') != 'hello' or hello.get('protocol') != relay.RELAY_PROTOCOL \
                    or not isinstance(hello.get('nonce'), str):
                raise RelayRefused('protocol-unsupported', 'the relay did not say hello in fabric-relay/0.1')
            if hello.get('relay') != self.origin:
                # The signed origin is what keeps a nonce signed for one relay from admitting this machine to another.
                raise RelayRefused('unauthenticated', 'the relay names another origin than the one dialled')
            nonce = hello['nonce']
            raw_nonce = p256.b64url_canonical(nonce) if _NONCE.fullmatch(nonce) else None
            if raw_nonce is None or len(raw_nonce) != 32:
                # relay.md: 32 random bytes, base64url. Anything else is no relay of this protocol.
                raise RelayRefused('protocol-unsupported', 'the relay sent a nonce that is not 32 bytes of base64url')
            now_ms = int(self.clock() * 1000)
            try:
                frame = relay.auth_frame(self.keys.sign, frame_id=_frame_id(), relay=self.origin, nonce=nonce,
                                         estate=self.estate, kid=self.keys.sig_kid, iat=relay.rfc3339(now_ms))
            except (jcs.CanonicalError, TypeError, ValueError) as error:
                raise RelayError('malformed', 'the auth frame could not be built: ' + type(error).__name__) from None
            ws.send_text(json.dumps(frame, separators=(',', ':')))
            answer = self._json(ws.recv_text(deadline))
            if answer.get('type') == 'error':
                raise RelayRefused(str(answer.get('code')), RelayRefused.ADVICE, answer.get('retryAfter'))
            if answer.get('type') != 'welcome' or not isinstance(answer.get('session'), str):
                raise RelayRefused('malformed', 'the relay answered auth with ' + str(answer.get('type')))
            self.welcome = Welcome(answer['session'], answer.get('estateRevision'), answer.get('deltaHead'),
                                   answer.get('heartbeat'), answer.get('limits') or {})
        except Closed as closed:
            ws.close()
            code = {4401: 'unauthenticated', 4403: 'not-admitted'}.get(closed.code, 'closed-%d' % closed.code)
            raise RelayRefused(code, RelayRefused.ADVICE) from None
        except BaseException:
            ws.close()
            raise
        ws.settimeout(RECV_TIMEOUT_SECONDS)
        self._ws = ws
        LOG.info('relay.session.open', extra={'kid': self.keys.sig_kid, 'revision': self.welcome.estate_revision})
        threading.Thread(target=self._read, name='relay-reader', daemon=True).start()
        threading.Thread(target=self._keepalive, name='relay-ping', daemon=True).start()
        return self.welcome

    @staticmethod
    def _json(text: str) -> dict:
        try:
            value = relay.parse_json(text.encode("utf-8"))   # bounded in size and depth
        except (ValueError, UnicodeError):
            raise RelayError('malformed', 'a frame that is not JSON') from None
        if not isinstance(value, dict) or value.get('v') != 1:
            raise RelayError('malformed', 'a frame that is not {v: 1, type, id}')
        return value

    # ---- Calls -----------------------------------------------------------------------------------------------------

    def _call(self, frame: dict, timeout: Optional[float] = None) -> dict:
        if self._ws is None or self._closed.is_set():
            raise SessionClosed(self.close_code or 1006)
        done, box = threading.Event(), {}
        with self._lock:
            self._waiters[frame['id']] = [done, box]
        try:
            self._ws.send_text(json.dumps(frame, separators=(',', ':')))
            if not done.wait(ANSWER_TIMEOUT_SECONDS if timeout is None else timeout):
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
        """Ends the session; the reader thread frees the socket."""
        self._closed.set()
        if self._ws is not None:
            self._ws.abort(1000)

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
        except socket.timeout:
            reason = 'relay.session.dead'
        except (WebSocketError, OSError) as error:
            reason = type(error).__name__
        except Exception as error:                         # nothing a relay sends may leave a session half alive
            LOG.exception('relay.reader.failed')
            reason = 'relay.reader.' + type(error).__name__
        finally:
            self.close_code = code
            self.close_reason = self.close_reason or reason    # the keepalive may have named it first
            self._closed.set()
            self._ws.close(1000 if code not in TERMINAL_CLOSE_CODES else code)
            with self._lock:
                waiters = list(self._waiters.values())
            for done, _ in waiters:
                done.set()
            LOG.info('relay.session.closed', extra={'code': code})

    def _callback(self, name: str, *args) -> None:
        """A callback's failure is logged and stops nothing: the reader keeps reading."""
        try:
            getattr(self, name)(*args)
        except Exception:
            LOG.exception('relay.callback.failed', extra={'callback': name})

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
            self._callback('on_presence', frame)
        elif kind in ('heartbeat', 'delta'):
            self._callback('on_record', frame)
        elif kind == 'error':
            # An unsolicited error precedes a close: 4403 not-admitted or expired, 4401, 1009, 1008, 1011.
            LOG.warning('relay.session.error', extra={'code': frame.get('code')})

    def _ack(self, seq: Any) -> None:
        if isinstance(seq, int) and seq >= 0:
            self._ws.send_text(json.dumps({'v': 1, 'type': 'ack', 'id': _frame_id(), 'seq': seq},
                                          separators=(',', ':')))

    def _verdict(self, frame: dict, now_ms: int) -> dict:
        body_text = frame.get('body')
        raw = p256.b64url_decode(body_text) if isinstance(body_text, str) and len(body_text) <= relay.MAX_FRAME_BYTES \
            else None
        try:
            body = raw.decode('ascii') if raw is not None else None
        except UnicodeDecodeError:
            body = None
        if not body:
            return {'ok': False, 'reason': 'malformed'}
        expect = {'from': frame.get('from'), 'to': self.keys.sig_kid, 'msgId': frame.get('msgId')}
        verdict = relay.verify_message_body(body, expect, self._local(self.sender_key), now_ms)
        if verdict['ok']:
            verdict = dict(relay.check_replay(self._local(self.replay.seen), verdict['payload'], now_ms),
                           payload=verdict['payload'])
        return verdict

    @staticmethod
    def _local(function):
        """This node's own lookup, with its failure told apart from a fault in what the relay sent."""
        def call(*args):
            try:
                return function(*args)
            except Exception as error:
                raise _LocalFailure(type(error).__name__) from error
        return call

    def _deliver(self, frame: dict) -> None:
        now_ms = int(self.clock() * 1000)
        try:
            verdict = self._verdict(frame, now_ms)
        except _LocalFailure:
            # The node could not check the body; the body may be authentic. It stays unacknowledged and the session
            # ends, so the relay delivers it again on the next connection.
            LOG.exception('relay.deliver.local-failure')
            raise
        except Exception:                                  # a body no check could read is refused, never fatal
            LOG.exception('relay.deliver.unreadable')
            verdict = {'ok': False, 'reason': 'malformed'}
        if not verdict['ok']:
            LOG.warning('relay.deliver.refused', extra={'reason': verdict['reason'], 'from': frame.get('from')})
            if verdict['reason'] not in TRANSIENT_REFUSALS:
                # Acknowledged first: a refused body is never redelivered, whatever its callback does.
                self._ack(frame.get('seq'))
            self._callback('on_refused', verdict['reason'], frame)
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
        missed = 0
        while not self._closed.wait(PING_SECONDS):
            try:
                self.ping()
                missed = 0
            except (TimeoutError, OSError):
                missed += 1
                LOG.warning('relay.ping.failed', extra={'missed': missed})
                if missed >= DEAD_AFTER_MISSED_PINGS:
                    # A half-open link answers nothing and reports nothing: close it so the node reconnects.
                    self.close_reason = 'relay.session.dead'
                    self._ws.abort(1001)
                    return
            except RelayError:
                missed = 0                                     # the relay answered: the link is alive
            except SessionClosed:
                return


def keep_connected(make_session: Callable[[ReplayStore], 'RelaySession'], stop: threading.Event, *,
                   on_state: Callable[[str, dict], None] = lambda state, detail: None,
                   min_backoff: float = 1.0, max_backoff: float = 60.0) -> None:
    """Holds one relay session open until `stop` is set: opens a session, waits for it to close, and opens another.
    Every session gets the same `ReplayStore` (`make_session(replay)` passes it to `RelaySession`), so a message
    handled before a link dropped, and delivered again after it, is acted on once. It opens the next
    after a jittered, doubling backoff (reset by a session that stayed up a minute), at least the relay's `retryAfter`
    (coerced to a finite number up to `max_backoff`). A terminal refusal (`TERMINAL_REFUSALS`) or a close 4401 or 4403
    is terminal: `on_state('stopped', ...)` names it and the loop ends,
    because the relay would refuse every retry until a person acts. `on_state` hears `open`, `closed`, `retry` and
    `stopped`, each with a detail dict that carries no secret. Any other failure is reported as `closed` and retried:
    nothing a relay sends ends the loop."""
    backoff = min_backoff
    replay = ReplayStore()
    while not stop.is_set():
        session = None
        wait_at_least = 0.0
        try:
            session = make_session(replay)
            session.open()
            opened = time.monotonic()
            on_state('open', {'revision': session.welcome.estate_revision})
            while not stop.is_set() and not session.wait_closed(1.0):
                pass
            if stop.is_set():
                session.close()
                break
            on_state('closed', {'code': session.close_code, 'reason': session.close_reason})
            if session.close_code in TERMINAL_CLOSE_CODES:
                on_state('stopped', {'code': session.close_code})
                return
            if time.monotonic() - opened >= 60:
                backoff = min_backoff
        except RelayRefused as refused:
            if refused.code in TERMINAL_REFUSALS:
                on_state('stopped', {'refusal': refused.code})
                return
            wait_at_least = retry_after_seconds(refused.retry_after, max_backoff)
            on_state('closed', {'refusal': refused.code})
        except RelayError as error:
            if error.code in TERMINAL_REFUSALS:
                on_state('stopped', {'refusal': error.code})
                return
            wait_at_least = retry_after_seconds(error.retry_after, max_backoff)   # `rate-limited` names a wait
            on_state('closed', {'refusal': error.code})
        except (WebSocketError, OSError) as error:
            on_state('closed', {'error': type(error).__name__})
        except Exception as error:                         # nothing ends the loop but `stop` and a terminal refusal
            LOG.exception('relay.keep-connected.failed')
            on_state('closed', {'error': type(error).__name__})
        finally:
            if session is not None and stop.is_set():
                session.close()
        delay = max(wait_at_least, backoff * (0.5 + random.random() / 2))
        on_state('retry', {'seconds': round(delay, 2)})
        if stop.wait(delay):
            break
        backoff = min(max_backoff, backoff * 2)
