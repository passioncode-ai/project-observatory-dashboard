#!/usr/bin/env python3
"""The node's relay session (OBS-108, RM-21 P5 slice 2): the stdlib WebSocket client and `RelaySession`, against a
relay stub on a loopback literal (relay.md allows `http://127.0.0.1:<port>` for a relay on the same machine).

The stub keeps the relay's rules that a node depends on — it admits by the signed `relay-auth/1`, answers with
`replyTo`, keeps a mailbox in `seq` order, deletes only what is acknowledged, closes a revoked session 4403 — and lets a
test misbehave as a relay may: name another origin, deliver a forged or repeated body.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import p256, relay                           # noqa: E402
from node.blobs import BlobRefused, digest_of, get_blob, put_blob  # noqa: E402
from node import ws as ws_module                       # noqa: E402
from node.ws import GUID, Closed, WebSocket, WebSocketError  # noqa: E402

if HAVE_CRYPTO:
    from node.device_keys import DeviceKeys            # noqa: E402
    from node import session as session_module         # noqa: E402
    from node.session import RelayError, RelayRefused, RelaySession, SessionClosed, connect_url, keep_connected  # noqa: E402

ESTATE = 'urn:fabric:estate:test'
WAIT = 5.0


# ---- A relay stub ------------------------------------------------------------------------------------------------

class Conn:
    def __init__(self, sock):
        self.sock, self.buffer, self.lock, self.kid, self.sent = sock, b'', threading.Lock(), None, set()

    def read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('closed')
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def frame(self):
        first, second = self.read(2)
        n = second & 0x7F
        if n == 126:
            n = struct.unpack('!H', self.read(2))[0]
        elif n == 127:
            n = struct.unpack('!Q', self.read(8))[0]
        mask = self.read(4) if second & 0x80 else b'\0\0\0\0'
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.read(n)))
        return first & 0x0F, data

    def raw(self, opcode, data):
        n = len(data)
        header = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else
                                           bytes([126]) + struct.pack('!H', n) if n < 65536 else
                                           bytes([127]) + struct.pack('!Q', n))
        with self.lock:
            self.sock.sendall(header + data)

    def send(self, frame):
        self.raw(0x1, json.dumps(frame).encode())

    def close(self, code):
        try:
            self.raw(0x8, struct.pack('!H', code))
        finally:
            self.sock.close()


class RelayStub:
    def __init__(self, admitted, *, subprotocol='fabric-relay.v1', hello_relay=None):
        self.admitted = dict(admitted)                      # kid -> SEC 1 octets
        self.subprotocol, self.hello_relay = subprotocol, hello_relay
        self.tamper = {}                                    # digest -> other bytes a misbehaving relay returns
        self.ignore_pings = False                           # a half-open link: frames go in, nothing comes back
        self.no_hello = False                               # upgrade, then silence
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen()
        self.origin = 'http://127.0.0.1:%d' % self.server.getsockname()[1]
        self.sessions, self.mailbox, self.seq, self.tombstones = {}, {}, 0, {}
        self.auths, self.acks, self.blobs = [], [], {}
        self.lock = threading.Lock()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(Conn(sock),), daemon=True).start()

    def upgrade(self, conn):
        while b'\r\n\r\n' not in conn.buffer:
            conn.buffer += conn.sock.recv(4096)
        head, conn.buffer = conn.buffer.split(b'\r\n\r\n', 1)
        lines = head.decode().split('\r\n')
        headers = {l.split(':', 1)[0].lower(): l.split(':', 1)[1].strip() for l in lines[1:]}
        if lines[0].split(' ')[1].startswith('/v1/blobs/'):
            self.blob(conn, lines[0].split(' ')[0], lines[0].split(' ')[1][len('/v1/blobs/'):], headers)
            return False
        assert lines[0] == 'GET /v1/connect HTTP/1.1', lines[0]
        accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + GUID).encode()).digest()).decode()
        conn.sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Accept: %s\r\nSec-WebSocket-Protocol: %s\r\n\r\n'
                           % (accept, self.subprotocol)).encode())
        return True

    def blob(self, conn, method, digest, headers):
        """relay.md §Blobs in its order, with the reference's checks over the node's header."""
        def answer(status, body=b'', error=None):
            if error:
                body = json.dumps({'error': {'code': error, 'message': error}}).encode()
            conn.sock.sendall(b'HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (status, len(body))
                              + body)
        parsed = relay.parse_relay_request(headers.get('fabric-relay-request'))
        if not parsed['ok']:
            return answer(parsed['status'], error=parsed['code'])
        request = parsed['request']
        iat = relay.instant(request['iat'])
        key = self.admitted.get(request['kid'])
        text = relay.blob_signing_input(relay=self.origin, method=method, digest=digest,
                                        recipients=request['recipients'], iat=request['iat'])
        if iat is None or abs(iat - time.time() * 1000) > 60_000 or key is None \
                or p256.verify_es256(key, text, request['sig']) is not True:
            return answer(401, error='unauthenticated')
        stored = self.blobs.get(digest)
        if method == 'PUT':
            if not all(r in self.admitted for r in request['recipients']):
                return answer(403, error='unknown-recipient')
            data = conn.read(int(headers['content-length']))
            if digest_of(data) != digest:
                return answer(422, error='digest-mismatch')
            if stored is not None and stored[0] != request['recipients']:
                return answer(409, error='recipients-mismatch')
            self.blobs[digest] = (request['recipients'], data)
            return answer(200 if stored is not None else 201)
        if stored is None or stored[0] != request['recipients'] or request['kid'] not in stored[0]:
            return answer(403, error='not-admitted')
        return answer(200, self.tamper.get(digest, stored[1]))

    def serve(self, conn):
        try:
            if not self.upgrade(conn):
                return
            if self.no_hello:
                time.sleep(10)
                return
            nonce = p256.b64url_encode(os.urandom(32))
            conn.send({'v': 1, 'type': 'hello', 'id': 'h', 'protocol': 'fabric-relay/0.1',
                       'relay': self.hello_relay or self.origin, 'nonce': nonce, 'time': '2026-10-15T00:00:00Z'})
            op, data = conn.frame()
            auth = json.loads(data)
            self.auths.append(auth)
            text = relay.auth_signing_input(relay=self.origin, nonce=nonce, estate=auth['estate'], kid=auth['kid'],
                                            iat=auth['iat'])
            key = self.admitted.get(auth['kid'])
            iat = relay.instant(auth['iat'])
            if key is None or auth['estate'] != ESTATE or iat is None or abs(iat - time.time() * 1000) > 60_000 \
                    or p256.verify_es256(key, text, auth['sig']) is not True:
                conn.send({'v': 1, 'type': 'error', 'id': 'e', 'code': 'unauthenticated', 'message': 'no'})
                conn.close(4401)
                return
            conn.kid = auth['kid']
            with self.lock:
                self.sessions[conn.kid] = conn
            conn.send({'v': 1, 'type': 'welcome', 'id': 'w', 'session': 's-' + conn.kid[-12:], 'estateRevision': 4,
                       'deltaHead': {'sequence': 0, 'sha256': '0' * 64}, 'heartbeat': None})
            self.flush(conn.kid)
            while True:
                op, data = conn.frame()
                if op == 0x8:
                    return
                if op == 0xA or op == 0x9:
                    continue
                self.handle(conn, json.loads(data))
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            with self.lock:
                if conn.kid and self.sessions.get(conn.kid) is conn:
                    del self.sessions[conn.kid]
            conn.sock.close()

    def handle(self, conn, frame):
        kind, reply = frame['type'], frame['id']
        if kind == 'send':
            if frame['to'] not in self.admitted:
                conn.send({'v': 1, 'type': 'error', 'id': 'x', 'replyTo': reply, 'code': 'unknown-recipient',
                           'message': 'no such recipient'})
                return
            with self.lock:
                key = (conn.kid, frame['msgId'])
                if key not in self.tombstones:
                    self.seq += 1
                    self.tombstones[key] = self.seq
                    self.mailbox.setdefault(frame['to'], []).append(
                        {'from': conn.kid, 'msgId': frame['msgId'], 'seq': self.seq, 'body': frame['body'],
                         'sentAt': '2026-10-15T00:00:00Z'})
                seq = self.tombstones[key]
            conn.send({'v': 1, 'type': 'accepted', 'id': 'a', 'replyTo': reply, 'msgId': frame['msgId'], 'seq': seq})
            self.flush(frame['to'])
        elif kind == 'ack':
            with self.lock:
                self.acks.append((conn.kid, frame['seq']))
                box = self.mailbox.get(conn.kid, [])
                self.mailbox[conn.kid] = [m for m in box if m['seq'] != frame['seq']]
        elif kind == 'ping':
            if not self.ignore_pings:
                conn.send({'v': 1, 'type': 'pong', 'id': 'p', 'replyTo': reply})
        elif kind == 'presence-query':
            with self.lock:
                known = [k for k in frame['kids'] if k in self.admitted]
                presences = [{'kid': k, 'state': 'online' if k in self.sessions else 'offline',
                              'lastSeen': '2026-10-15T00:00:00Z'} for k in known]
            conn.send({'v': 1, 'type': 'presence-result', 'id': 'r', 'replyTo': reply, 'presences': presences,
                       'unknown': [k for k in frame['kids'] if k not in known]})
        else:
            conn.send({'v': 1, 'type': 'error', 'id': 'x', 'replyTo': reply, 'code': 'unknown-type', 'message': kind})

    def flush(self, kid):
        with self.lock:
            conn = self.sessions.get(kid)
            # Each message once per connection; a new connection gets what is still unacknowledged.
            box = [m for m in self.mailbox.get(kid, []) if conn is not None and m['seq'] not in conn.sent]
            if conn is not None:
                conn.sent.update(m['seq'] for m in box)
        for message in box:
            conn.send(dict({'v': 1, 'type': 'deliver', 'id': 'd%d' % message['seq']}, **message))

    def deliver_raw(self, kid, **message):
        with self.lock:
            self.seq += 1
            message = dict(message, seq=self.seq, sentAt='2026-10-15T00:00:00Z')
            self.mailbox.setdefault(kid, []).append(message)
        self.flush(kid)
        return message['seq']

    def revoke(self, kid):
        with self.lock:
            self.admitted.pop(kid, None)
            conn = self.sessions.pop(kid, None)
        if conn is not None:
            conn.send({'v': 1, 'type': 'error', 'id': 'n', 'code': 'not-admitted', 'message': 'revoked'})
            conn.close(4403)

    def send_to(self, kid, frame=None, raw=None):
        with self.lock:
            conn = self.sessions.get(kid)
        if raw is not None:
            conn.raw(0x1, raw)
        else:
            conn.send(frame)

    def shutdown(self, kid, code=1001):
        """The relay going away: a close with no error frame."""
        with self.lock:
            conn = self.sessions.pop(kid, None)
        if conn is not None:
            conn.close(code)

    def stop(self):
        self.server.close()


def eventually(predicate, timeout=WAIT):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()

# ---- Tests ----------------------------------------------------------------------------------------------------------


class Urls(unittest.TestCase):
    @unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra')
    def test_connect_url(self):
        self.assertEqual(connect_url('https://relay.example'), 'wss://relay.example/v1/connect')
        self.assertEqual(connect_url('https://relay.example:8443'), 'wss://relay.example:8443/v1/connect')
        self.assertEqual(connect_url('http://127.0.0.1:9000'), 'ws://127.0.0.1:9000/v1/connect')
        for origin in ('http://relay.example', 'https://relay.example/', 'http://localhost:9000', 'wss://relay.example'):
            with self.assertRaises(ValueError):
                connect_url(origin)

    def test_plain_ws_only_on_loopback(self):
        with self.assertRaises(WebSocketError):
            WebSocket.connect('ws://relay.example/v1/connect', subprotocol='fabric-relay.v1')


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Session(unittest.TestCase):
    def setUp(self):
        self.a, self.b = DeviceKeys.generate(), DeviceKeys.generate()
        self.relay = RelayStub({self.a.sig_kid: self.a.sig_public, self.b.sig_kid: self.b.sig_public})
        self.peers = {self.a.sig_kid: self.a.sig_public, self.b.sig_kid: self.b.sig_public}
        self.opened = []

    def tearDown(self):
        for session in self.opened:
            session.close()
        self.relay.stop()

    def session(self, keys, **kw):
        received = kw.pop('received', [])
        refused = kw.pop('refused', [])
        session = RelaySession(self.relay.origin, keys, ESTATE, self.peers.get,
                               on_message=kw.pop('on_message', received.append),
                               on_refused=lambda reason, frame: refused.append(reason), **kw)
        session.open()
        self.opened.append(session)
        return session

    def test_a_message_reaches_the_other_machine_and_is_acknowledged(self):
        received = []
        b = self.session(self.b, received=received)
        a = self.session(self.a)
        self.assertEqual(b.welcome.estate_revision, 4)
        seq = a.send(self.b.sig_kid, {'command': 'run.start', 'run': 'r-1'}, msg_id='m-1')
        self.assertTrue(eventually(lambda: received))
        self.assertEqual(received[0]['payload'], {'command': 'run.start', 'run': 'r-1'})
        self.assertEqual((received[0]['from'], received[0]['to']), (self.a.sig_kid, self.b.sig_kid))
        self.assertTrue(eventually(lambda: (self.b.sig_kid, seq) in self.relay.acks))
        self.assertEqual(self.relay.mailbox[self.b.sig_kid], [])
        # The same msgId again answers the first seq, and nothing is delivered twice.
        self.assertEqual(a.send(self.b.sig_kid, {'command': 'run.start', 'run': 'r-1'}, msg_id='m-1'), seq)
        time.sleep(0.2)
        self.assertEqual(len(received), 1)

    def test_mail_waits_for_an_offline_machine(self):
        a = self.session(self.a)
        a.send(self.b.sig_kid, {'n': 1}, msg_id='m-1')
        a.send(self.b.sig_kid, {'n': 2}, msg_id='m-2')
        received = []
        self.session(self.b, received=received)
        self.assertTrue(eventually(lambda: len(received) == 2))
        self.assertEqual([m['payload']['n'] for m in received], [1, 2])

    def test_a_message_the_handler_could_not_take_is_delivered_again(self):
        a = self.session(self.a)
        a.send(self.b.sig_kid, {'n': 1}, msg_id='m-1')
        calls = []

        def failing(message):
            calls.append(message)
            raise RuntimeError('busy')
        first = self.session(self.b, on_message=failing)
        self.assertTrue(eventually(lambda: calls))
        self.assertEqual(self.relay.acks, [])
        first.close()
        received = []
        self.session(self.b, received=received)
        self.assertTrue(eventually(lambda: received))
        self.assertEqual(received[0]['msgId'], 'm-1')

    def test_a_forged_or_misdirected_body_is_refused_and_acknowledged(self):
        received, refused = [], []
        self.session(self.b, received=received, refused=refused)
        stranger = DeviceKeys.generate()
        now = int(time.time()) * 1000
        forged = relay.message_body(stranger.sign, sender=self.a.sig_kid, to=self.b.sig_kid, msg_id='f-1',
                                    iat=relay.rfc3339(now), exp=relay.rfc3339(now + 60_000), payload={'x': 1})
        elsewhere = relay.message_body(self.a.sign, sender=self.a.sig_kid, to=self.a.sig_kid, msg_id='f-2',
                                       iat=relay.rfc3339(now), exp=relay.rfc3339(now + 60_000), payload={'x': 2})
        unknown = relay.message_body(stranger.sign, sender=stranger.sig_kid, to=self.b.sig_kid, msg_id='f-3',
                                     iat=relay.rfc3339(now), exp=relay.rfc3339(now + 60_000), payload={'x': 3})
        # The relay even asserts a `to` matching the misdirected body: the node checks its own kid, never the relay's.
        seqs = [self.relay.deliver_raw(self.b.sig_kid, **{'from': sender, 'msgId': msg, 'to': self.a.sig_kid,
                                                          'body': p256.b64url_encode(body.encode())})
                for sender, msg, body in ((self.a.sig_kid, 'f-1', forged), (self.a.sig_kid, 'f-2', elsewhere),
                                          (stranger.sig_kid, 'f-3', unknown))]
        self.assertTrue(eventually(lambda: len(refused) == 3))
        self.assertEqual(refused, ['bad-signature', 'mismatch', 'unknown-sender'])
        self.assertEqual(received, [])
        # A forged or misdirected body is acknowledged; a sender the record does not list yet is left in the mailbox,
        # because a newer revision may list it.
        self.assertTrue(eventually(lambda: all((self.b.sig_kid, s) in self.relay.acks for s in seqs[:2])))
        time.sleep(0.2)
        self.assertNotIn((self.b.sig_kid, seqs[2]), self.relay.acks)

    def test_a_repeated_body_is_acted_on_once(self):
        received, refused = [], []
        self.session(self.b, received=received, refused=refused)
        now = int(time.time()) * 1000
        body = relay.message_body(self.a.sign, sender=self.a.sig_kid, to=self.b.sig_kid, msg_id='m-1',
                                  iat=relay.rfc3339(now), exp=relay.rfc3339(now + 60_000), payload={'n': 1})
        for _ in range(2):
            self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'm-1',
                                                      'body': p256.b64url_encode(body.encode())})
        self.assertTrue(eventually(lambda: refused == ['replay']))
        self.assertEqual(len(received), 1)

    def test_an_unknown_recipient_is_the_relays_refusal(self):
        a = self.session(self.a)
        with self.assertRaises(RelayError) as caught:
            a.send('m-nobody#sig', {'n': 1})
        self.assertEqual(caught.exception.code, 'unknown-recipient')

    def test_presence(self):
        a = self.session(self.a)
        answer = a.presence([self.a.sig_kid, self.b.sig_kid, 'm-nobody#sig'])
        self.assertEqual({p['kid']: p['state'] for p in answer['presences']},
                         {self.a.sig_kid: 'online', self.b.sig_kid: 'offline'})
        self.assertEqual(answer['unknown'], ['m-nobody#sig'])
        a.ping()

    def test_a_machine_outside_the_record_is_refused(self):
        stranger = DeviceKeys.generate()
        with self.assertRaises(RelayRefused) as caught:
            RelaySession(self.relay.origin, stranger, ESTATE, self.peers.get, on_message=print).open()
        self.assertEqual(caught.exception.code, 'unauthenticated')
        self.assertIn('current revision', str(caught.exception))

    def test_a_relay_naming_another_origin_gets_no_signature(self):
        self.relay.hello_relay = 'https://other-relay.example'
        with self.assertRaises(RelayRefused):
            RelaySession(self.relay.origin, self.a, ESTATE, self.peers.get, on_message=print).open()
        self.assertEqual(self.relay.auths, [])

    def test_a_revoked_machine_is_closed_4403(self):
        b = self.session(self.b)
        self.relay.revoke(self.b.sig_kid)
        self.assertTrue(b.wait_closed(WAIT))
        self.assertEqual(b.close_code, 4403)
        with self.assertRaises(SessionClosed):
            b.send(self.a.sig_kid, {'n': 1})

    def test_a_relay_without_the_subprotocol_is_refused(self):
        self.relay.subprotocol = 'other.v1'
        with self.assertRaises(WebSocketError):
            RelaySession(self.relay.origin, self.a, ESTATE, self.peers.get, on_message=print).open()

    def test_large_messages_cross_the_extended_lengths(self):
        received = []
        self.session(self.b, received=received)
        a = self.session(self.a)
        text = 'x' * 70_000                                # 64-bit length on the wire
        a.send(self.b.sig_kid, {'text': text}, msg_id='big')
        self.assertTrue(eventually(lambda: received))
        self.assertEqual(received[0]['payload']['text'], text)


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Blobs(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.c = DeviceKeys.generate(), DeviceKeys.generate(), DeviceKeys.generate()
        self.relay = RelayStub({k.sig_kid: k.sig_public for k in (self.a, self.b, self.c)})

    def tearDown(self):
        self.relay.stop()

    def test_put_then_get_by_the_recipient(self):
        data = os.urandom(200_000)
        digest, repeat = put_blob(self.relay.origin, self.a, data, [self.b.sig_kid])
        self.assertEqual((digest, repeat), (digest_of(data), False))
        self.assertEqual(put_blob(self.relay.origin, self.a, data, [self.b.sig_kid]), (digest, True))
        self.assertEqual(get_blob(self.relay.origin, self.b, digest, [self.b.sig_kid]), data)

    def test_what_is_refused(self):
        data = b'sealed bytes'
        digest, _ = put_blob(self.relay.origin, self.a, data, [self.b.sig_kid])
        cases = [
            (lambda: get_blob(self.relay.origin, self.c, digest, [self.b.sig_kid]), 403, 'not-admitted'),
            (lambda: get_blob(self.relay.origin, self.b, digest, [self.b.sig_kid, self.c.sig_kid]), 403, 'not-admitted'),
            (lambda: put_blob(self.relay.origin, self.a, data, [self.c.sig_kid]), 409, 'recipients-mismatch'),
            (lambda: put_blob(self.relay.origin, self.a, b'x', ['m-nobody#sig']), 403, 'unknown-recipient'),
            (lambda: put_blob(self.relay.origin, DeviceKeys.generate(), b'y', [self.b.sig_kid]), 401, 'unauthenticated'),
        ]
        for call, status, code in cases:
            with self.assertRaises(BlobRefused) as caught:
                call()
            self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_bytes_that_are_not_the_named_blob_are_refused(self):
        digest, _ = put_blob(self.relay.origin, self.a, b'the real bytes', [self.b.sig_kid])
        self.relay.tamper[digest] = b'other bytes'
        with self.assertRaises(BlobRefused) as caught:
            get_blob(self.relay.origin, self.b, digest, [self.b.sig_kid])
        self.assertEqual(caught.exception.code, 'digest-mismatch')

    def test_an_error_body_of_another_shape_still_refuses(self):
        from node import blobs
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.addCleanup(server.close)

        def serve():
            sock, _ = server.accept()
            sock.recv(65536)
            body = b'{"error": "x"}'
            sock.sendall(b'HTTP/1.1 400 Bad\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            sock.close()
        threading.Thread(target=serve, daemon=True).start()
        with self.assertRaises(BlobRefused) as caught:
            get_blob('http://127.0.0.1:%d' % server.getsockname()[1], self.b, 'sha256:' + 'ab' * 32, [self.b.sig_kid])
        self.assertEqual((caught.exception.status, caught.exception.code), (400, 'unknown'))

    def test_a_blob_larger_than_the_limit_is_refused_after_reading_no_more(self):
        from node import blobs
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.addCleanup(server.close)

        def serve(endless):
            sock, _ = server.accept()
            sock.recv(65536)
            if endless:                                     # no length, never ends: only a bounded read returns
                sock.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n')
                try:
                    while True:
                        sock.sendall(b'z' * 4096)
                except OSError:
                    pass
            else:
                sock.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2000\r\nConnection: close\r\n\r\n' + b'z' * 2000)
            sock.close()
        for endless in (False, True):
            threading.Thread(target=serve, args=(endless,), daemon=True).start()
            errors = []

            def fetch():
                try:
                    get_blob('http://127.0.0.1:%d' % server.getsockname()[1], self.b, 'sha256:' + 'ab' * 32,
                             [self.b.sig_kid])
                except Exception as error:
                    errors.append(error)
            with mock.patch.multiple(blobs, MAX_BLOB_BYTES=1000, TIMEOUT_SECONDS=3):
                reader = threading.Thread(target=fetch, daemon=True)
                reader.start()
                reader.join(10)                             # an unbounded read of an endless body never returns
            self.assertFalse(reader.is_alive(), 'the read is not bounded')
            self.assertIsInstance(errors[0], BlobRefused)
            self.assertEqual(errors[0].code, 'too-large')

    def slow_server(self, status_line, body_stream):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.addCleanup(server.close)

        def serve():
            sock, _ = server.accept()
            sock.recv(65536)
            sock.sendall(status_line)
            try:
                for chunk in body_stream():
                    sock.sendall(chunk)
            except OSError:
                pass
            sock.close()
        threading.Thread(target=serve, daemon=True).start()
        return 'http://127.0.0.1:%d' % server.getsockname()[1]

    def fetch_within(self, origin, seconds):
        from node import blobs
        errors = []

        def fetch():
            try:
                get_blob(origin, self.b, 'sha256:' + 'ab' * 32, [self.b.sig_kid])
            except Exception as error:
                errors.append(error)
        with mock.patch.multiple(blobs, DEADLINE_SECONDS=0.5, TIMEOUT_SECONDS=2):
            reader = threading.Thread(target=fetch, daemon=True)
            reader.start()
            reader.join(seconds)
        self.assertFalse(reader.is_alive(), 'the read has no total limit')
        return errors[0]

    def test_a_trickled_blob_hits_the_deadline(self):
        def trickle():
            for _ in range(100):
                time.sleep(0.05)
                yield b'z'
        origin = self.slow_server(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n', trickle)
        error = self.fetch_within(origin, 4)
        self.assertEqual(error.code, 'timeout')

    def test_an_endless_error_body_is_read_no_further_than_needed(self):
        def endless():
            yield b'{"error": {"code": "not-admitted", "message": "' + b'x' * 70_000
            while True:
                time.sleep(0.01)
                yield b'x' * 4096
        origin = self.slow_server(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n', endless)
        error = self.fetch_within(origin, 6)
        self.assertIsInstance(error, BlobRefused)
        self.assertEqual(error.status, 403)

    def test_arguments_are_checked_before_anything_is_sent(self):
        for call in (lambda: get_blob(self.relay.origin, self.b, 'sha256:XYZ', [self.b.sig_kid]),
                     lambda: put_blob('http://localhost:1', self.a, b'x', [self.b.sig_kid]),
                     lambda: put_blob(self.relay.origin, self.a, b'x', [])):
            with self.assertRaises(ValueError):
                call()


# ---- RFC 6455 on a raw loopback server ------------------------------------------------------------------------------

class RawServer:
    """One connection: the upgrade (its answer configurable), then `script(conn)` in the server thread."""

    def __init__(self, script, *, accept=None, protocol='fabric-relay.v1'):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen()
        self.url = 'ws://127.0.0.1:%d/v1/connect' % self.server.getsockname()[1]
        self.received, self.done = [], threading.Event()
        self.accept, self.protocol, self.script = accept, protocol, script
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        sock, _ = self.server.accept()
        conn = Conn(sock)
        try:
            while b'\r\n\r\n' not in conn.buffer:
                conn.buffer += sock.recv(4096)
            head, conn.buffer = conn.buffer.split(b'\r\n\r\n', 1)
            headers = {l.split(':', 1)[0].lower(): l.split(':', 1)[1].strip() for l in head.decode().split('\r\n')[1:]}
            accept = self.accept or base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + GUID).encode()).digest()).decode()
            sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          'Sec-WebSocket-Accept: %s\r\nSec-WebSocket-Protocol: %s\r\n\r\n' % (accept, self.protocol)).encode())
            self.script(conn, self)
        except (OSError, ConnectionError):
            pass
        finally:
            self.done.set()
            sock.close()
            self.server.close()

    def read_raw(self, conn):
        """A client frame as sent: (first byte, mask bit, mask, unmasked payload)."""
        first, second = conn.read(2)
        n = second & 0x7F
        if n == 126:
            n = struct.unpack('!H', conn.read(2))[0]
        elif n == 127:
            n = struct.unpack('!Q', conn.read(8))[0]
        mask = conn.read(4) if second & 0x80 else b''
        data = conn.read(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return first, bool(second & 0x80), mask, data


def header(first, n):
    return bytes([first]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack('!H', n) if n < 65536
                             else bytes([127]) + struct.pack('!Q', n))


class Frames(unittest.TestCase):
    def client(self, script, **kw):
        server = RawServer(script, **kw)
        ws = WebSocket.connect(server.url, subprotocol='fabric-relay.v1', timeout=5)
        ws.settimeout(5)
        self.addCleanup(ws.close)
        return server, ws

    def test_client_frames_are_masked_with_fresh_random_keys(self):
        seen = []

        def script(conn, server):
            for _ in range(3):
                seen.append(server.read_raw(conn))
        calls = []
        real = os.urandom
        with mock.patch.object(ws_module.os, 'urandom', side_effect=lambda n: calls.append(n) or real(n)):
            server, ws = self.client(script)
            for text in ('one', 'two', 'three'):
                ws.send_text(text)
            server.done.wait(5)
        self.assertEqual([d for _, _, _, d in seen], [b'one', b'two', b'three'])
        self.assertTrue(all(masked for _, masked, _, _ in seen))
        self.assertEqual(len({m for _, _, m, _ in seen}), 3)
        self.assertGreaterEqual(calls.count(4), 3)

    def refused(self, frames: bytes):
        _, ws = self.client(lambda conn, server: conn.sock.sendall(frames) or time.sleep(1))
        with self.assertRaises((WebSocketError, Closed)) as caught:
            ws.recv_text()
        return caught.exception

    def test_what_a_client_refuses(self):
        cases = {
            'a frame over 1 MiB': header(0x81, ws_module.MAX_MESSAGE_BYTES + 1),
            'a message over 1 MiB in two fragments': header(0x01, 600_000) + b'a' * 600_000 + header(0x80, 600_000) + b'b' * 600_000,
            'too many fragments': header(0x01, 1) + b'a' + header(0x00, 0) * (ws_module.MAX_FRAGMENTS + 1) + header(0x80, 0),
            'text that is not UTF-8': header(0x81, 2) + b'\xc3\x28',
            'a control frame over 125 bytes': header(0x89, 126) + b'p' * 126,
            'a reserved bit': header(0xC1, 1) + b'x',
            'an unknown opcode': header(0x83, 1) + b'x',
            'an unknown opcode in a message': header(0x01, 1) + b'x' + header(0x83, 1) + b'y',
            'a new message inside a fragmented one': header(0x01, 1) + b'x' + header(0x81, 1) + b'y',
            'a binary message': header(0x82, 1) + b'x',
            'a masked server frame': bytes([0x81, 0x81]) + b'\0\0\0\0x',
        }
        for name, frames in cases.items():
            with self.subTest(name):
                error = self.refused(frames)
                self.assertIsInstance(error, WebSocketError)
                if 'unknown opcode' in name:
                    self.assertIn('unknown opcode', str(error))

    def test_a_ping_is_answered_with_its_payload(self):
        got = []

        def script(conn, server):
            conn.sock.sendall(header(0x89, 4) + b'ping' + header(0x81, 2) + b'ok')
            got.append(server.read_raw(conn))
        server, ws = self.client(script)
        self.assertEqual(ws.recv_text(), 'ok')
        server.done.wait(5)
        self.assertEqual((got[0][0] & 0x0F, got[0][3]), (0xA, b'ping'))

    def test_close_frames(self):
        for payload, code, echoed in ((b'\x03', 1002, struct.pack('!H', 1002)),
                                      (struct.pack('!H', 1000) + b'\xff', 1007, struct.pack('!H', 1007)),
                                      (b'', 1005, b''),
                                      (struct.pack('!H', 4403) + b'gone', 4403, struct.pack('!H', 4403)),
                                      (struct.pack('!H', 1006), 1006, b''),           # never sendable: not echoed
                                      (struct.pack('!H', 2999), 2999, b'')):
            got = []

            def script(conn, server, payload=payload):
                conn.sock.sendall(header(0x88, len(payload)) + payload)
                got.append(server.read_raw(conn))
            server, ws = self.client(script)
            with self.assertRaises(Closed) as caught:
                ws.recv_text()
            server.done.wait(5)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(got[0][3], echoed)

    def test_the_upgrade_must_prove_the_key_and_the_subprotocol(self):
        for kw in ({'accept': base64.b64encode(b'x' * 20).decode()}, {'protocol': 'other.v1'}):
            server = RawServer(lambda conn, server: None, **kw)
            with self.assertRaises(WebSocketError):
                WebSocket.connect(server.url, subprotocol='fabric-relay.v1', timeout=5)

    @unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra')
    def test_tls_verifies_the_certificate(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__('ipaddress').ip_address('127.0.0.1'))]), False)
                .sign(key, hashes.SHA256()))
        folder = Path(__import__('tempfile').mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in folder.iterdir()] and folder.rmdir())
        (folder / 'c.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (folder / 'k.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(folder / 'c.pem', folder / 'k.pem')
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.addCleanup(server.close)

        def serve():
            try:
                sock, _ = server.accept()
                with context.wrap_socket(sock, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass
        threading.Thread(target=serve, daemon=True).start()
        with self.assertRaises(ssl.SSLCertVerificationError):
            WebSocket.connect('wss://127.0.0.1:%d/v1/connect' % server.getsockname()[1], subprotocol='fabric-relay.v1',
                              timeout=5)


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Hardening(unittest.TestCase):
    """Findings of the #293 review: nothing a relay sends kills the reader; a dead link closes; the node reconnects."""

    def setUp(self):
        self.a, self.b = DeviceKeys.generate(), DeviceKeys.generate()
        self.relay = RelayStub({self.a.sig_kid: self.a.sig_public, self.b.sig_kid: self.b.sig_public})
        self.peers = {self.a.sig_kid: self.a.sig_public, self.b.sig_kid: self.b.sig_public}
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.relay.stop()

    def open(self, keys, **kw):
        session = RelaySession(self.relay.origin, keys, ESTATE, self.peers.get, on_message=kw.pop('on_message', print),
                               **kw)
        session.open()
        self.sessions.append(session)
        return session

    def body(self, payload_overrides=None, depth=0):
        now = int(time.time()) * 1000
        payload = {'type': relay.MESSAGE_TYPE, 'from': self.a.sig_kid, 'to': self.b.sig_kid, 'msgId': 'p-1',
                   'iat': relay.rfc3339(now), 'exp': relay.rfc3339(now + 60_000), 'payload': None}
        payload.update(payload_overrides or {})
        head = p256.b64url_encode(b'{"alg":"ES256","typ":"fabric-relay-message+jwt"}')
        text = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        if depth:
            text = '[' * depth + ']' * depth
        part = p256.b64url_encode(text.encode())
        return head + '.' + part + '.' + self.a.sign(head + '.' + part)

    def test_poison_bodies_are_refused_acknowledged_and_survived(self):
        refused = []
        b = self.open(self.b, on_refused=lambda reason, frame: refused.append(reason))
        seqs = [self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'p-%d' % i,
                                                          'body': p256.b64url_encode(body.encode())})
                for i, body in enumerate([self.body({'iat': '0000-01-01T00:00:00Z'}), self.body(depth=900),
                                          self.body(depth=100_000)])]
        self.assertTrue(eventually(lambda: len(refused) == 3))
        self.assertTrue(eventually(lambda: all((self.b.sig_kid, s) in self.relay.acks for s in seqs)))
        b.ping()                                            # the reader is alive

    def test_a_local_failure_leaves_the_body_for_the_next_connection(self):
        refused, received, calls = [], [], []

        def flaky_lookup(kid):
            calls.append(kid)
            if len(calls) == 1:
                raise OSError('the record store is busy')
            return self.peers.get(kid)
        first = RelaySession(self.relay.origin, self.b, ESTATE, flaky_lookup, on_message=received.append,
                             on_refused=lambda reason, frame: refused.append(reason))
        first.open()
        self.sessions.append(first)
        seq = self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'p-1',
                                                        'body': p256.b64url_encode(self.body().encode())})
        self.assertTrue(first.wait_closed(5))                # the node could not check it: it does not pretend to
        self.assertEqual((refused, received), ([], []))
        self.assertNotIn((self.b.sig_kid, seq), self.relay.acks)
        second = RelaySession(self.relay.origin, self.b, ESTATE, flaky_lookup, on_message=received.append)
        second.open()
        self.sessions.append(second)
        self.assertTrue(eventually(lambda: len(received) == 1))
        self.assertTrue(eventually(lambda: (self.b.sig_kid, seq) in self.relay.acks))

    def test_a_check_that_raises_on_the_relays_input_refuses_the_body(self):
        refused = []
        b = self.open(self.b, on_refused=lambda reason, frame: refused.append(reason))
        with mock.patch.object(session_module.relay, 'verify_message_body', side_effect=RuntimeError('parser')):
            seq = self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'p-1',
                                                            'body': p256.b64url_encode(self.body().encode())})
            self.assertTrue(eventually(lambda: refused == ['malformed']))
        self.assertTrue(eventually(lambda: (self.b.sig_kid, seq) in self.relay.acks))
        b.ping()

    def test_a_refused_body_is_acknowledged_before_its_callback_runs(self):
        holder = {}
        b = self.open(self.b, on_refused=lambda reason, frame: holder['session'].close())
        holder['session'] = b
        seq = self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'x', 'body': 'AAAA'})
        self.assertTrue(eventually(lambda: (self.b.sig_kid, seq) in self.relay.acks))

    def test_an_unexpected_failure_closes_the_session_with_its_reason(self):
        class Broken(session_module.ReplayStore):
            def add(self, *args):
                raise RuntimeError('store')
        received = []
        b = RelaySession(self.relay.origin, self.b, ESTATE, self.peers.get, on_message=received.append,
                         replay=Broken())
        b.open()
        self.sessions.append(b)
        self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'p-1',
                                                  'body': p256.b64url_encode(self.body().encode())})
        self.assertTrue(b.wait_closed(5))
        self.assertEqual(b.close_reason, 'relay.reader.RuntimeError')

    def test_a_frame_nested_too_deep_is_skipped(self):
        b = self.open(self.b)
        self.relay.send_to(self.b.sig_kid, raw=('[' * 5000 + ']' * 5000).encode())
        self.relay.send_to(self.b.sig_kid, raw=b'{"v":1,"type":' + b'[' * 70 + b']' * 70 + b',"id":"x"}')
        b.ping()

    def test_callbacks_that_raise_stop_nothing(self):
        def boom(*args):
            raise RuntimeError('callback')
        b = self.open(self.b, on_presence=boom, on_refused=boom)
        self.relay.send_to(self.b.sig_kid, {'v': 1, 'type': 'presence', 'id': 'q', 'kid': self.a.sig_kid,
                                            'state': 'online', 'lastSeen': '2026-10-15T00:00:00Z'})
        seq = self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'x', 'body': 'AAAA'})
        self.assertTrue(eventually(lambda: (self.b.sig_kid, seq) in self.relay.acks))
        b.ping()

    def test_a_relay_that_never_says_hello_times_out(self):
        self.relay.no_hello = True
        errors = []

        def attempt():
            try:
                RelaySession(self.relay.origin, self.a, ESTATE, self.peers.get, on_message=print).open()
            except Exception as error:
                errors.append(error)
        with mock.patch.object(session_module, 'HELLO_TIMEOUT_SECONDS', 0.3):
            thread = threading.Thread(target=attempt, daemon=True)
            thread.start()
            thread.join(3)                                  # well before the stub's 10 s silence ends
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], socket.timeout)

    def test_a_dead_link_is_closed(self):
        with mock.patch.multiple(session_module, PING_SECONDS=0.1, ANSWER_TIMEOUT_SECONDS=0.1):
            b = self.open(self.b)
            self.relay.ignore_pings = True
            self.assertTrue(b.wait_closed(5))
        self.assertEqual(b.close_reason, 'relay.session.dead')

    def test_keep_connected_reopens_and_stops_on_4403(self):
        states, stop = [], threading.Event()
        thread = threading.Thread(target=keep_connected, args=(
            lambda replay: RelaySession(self.relay.origin, self.b, ESTATE, self.peers.get, on_message=print,
                                        replay=replay), stop),
            kwargs={'on_state': lambda state, detail: states.append((state, detail)), 'min_backoff': 0.05},
            daemon=True)
        thread.start()
        self.assertTrue(eventually(lambda: [s for s, _ in states].count('open') == 1))
        self.relay.shutdown(self.b.sig_kid, 1001)            # the relay restarts
        self.assertTrue(eventually(lambda: [s for s, _ in states].count('open') == 2))
        self.relay.revoke(self.b.sig_kid)                     # this machine leaves the estate
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(states[-1], ('stopped', {'code': 4403}))
        stop.set()


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Reconnects(unittest.TestCase):
    """The #293 adversarial round: what survives a dropped link, and how the node waits before trying again."""

    def setUp(self):
        self.a, self.b = DeviceKeys.generate(), DeviceKeys.generate()
        self.relay = RelayStub({self.a.sig_kid: self.a.sig_public, self.b.sig_kid: self.b.sig_public})
        self.peers = {self.a.sig_kid: self.a.sig_public, self.b.sig_kid: self.b.sig_public}

    def tearDown(self):
        self.relay.stop()

    def run_loop(self, make, **kw):
        states, stop = [], threading.Event()
        thread = threading.Thread(target=keep_connected, args=(make, stop),
                                  kwargs=dict({'on_state': lambda state, detail: states.append((state, detail)),
                                               'min_backoff': 0.05}, **kw), daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(5)))
        return states, stop, thread

    def test_a_message_handled_before_the_link_dropped_is_acted_on_once(self):
        handled = []

        def handler(message):
            handled.append(message['msgId'])
            if len(handled) == 1:
                self.relay.shutdown(self.b.sig_kid, 1001)   # the link drops before the ack leaves
        make = lambda replay: RelaySession(self.relay.origin, self.b, ESTATE, self.peers.get, on_message=handler,
                                           replay=replay)
        states, stop, thread = self.run_loop(make)
        self.assertTrue(eventually(lambda: [s for s, _ in states].count('open') == 1))
        now = int(time.time()) * 1000
        body = relay.message_body(self.a.sign, sender=self.a.sig_kid, to=self.b.sig_kid, msg_id='once',
                                  iat=relay.rfc3339(now), exp=relay.rfc3339(now + 60_000), payload=None)
        self.relay.deliver_raw(self.b.sig_kid, **{'from': self.a.sig_kid, 'msgId': 'once',
                                                  'body': p256.b64url_encode(body.encode())})
        self.assertTrue(eventually(lambda: [s for s, _ in states].count('open') >= 2))
        self.assertTrue(eventually(lambda: self.relay.mailbox.get(self.b.sig_kid) == []))
        self.assertEqual(handled, ['once'])

    def test_refusals_no_retry_can_change_stop_the_loop(self):
        stranger = DeviceKeys.generate()
        states, stop, thread = self.run_loop(
            lambda replay: RelaySession(self.relay.origin, stranger, ESTATE, self.peers.get, on_message=print))
        thread.join(5)
        self.assertEqual(states[-1], ('stopped', {'refusal': 'unauthenticated'}))
        self.assertEqual([s for s, _ in states].count('retry'), 0)

    def test_a_4403_during_open_is_terminal(self):
        def refuse_open(replay):
            session = RelaySession(self.relay.origin, self.b, ESTATE, self.peers.get, on_message=print)
            session.open = mock.Mock(side_effect=RelayRefused('not-admitted'))
            return session
        states, stop, thread = self.run_loop(refuse_open)
        thread.join(5)
        self.assertEqual(states[-1], ('stopped', {'refusal': 'not-admitted'}))

    def test_backoff_doubles_with_jitter_and_honours_retry_after(self):
        attempts = []

        def failing(replay):
            session = RelaySession(self.relay.origin, self.b, ESTATE, self.peers.get, on_message=print)
            error = RelayError('rate-limited', retry_after=0.4) if len(attempts) == 2 else OSError('down')
            attempts.append(error)
            session.open = mock.Mock(side_effect=error)
            return session
        with mock.patch.object(session_module.random, 'random', return_value=1.0):
            states, stop, thread = self.run_loop(failing, min_backoff=0.01, max_backoff=10)
            self.assertTrue(eventually(lambda: [s for s, _ in states].count('retry') >= 4, timeout=10))
        waits = [d['seconds'] for s, d in states if s == 'retry'][:4]
        self.assertEqual(waits[:2], [0.01, 0.02])            # doubling, from the minimum
        self.assertEqual(waits[2], 0.4)                       # the relay's retryAfter outranks the backoff
        with mock.patch.object(session_module.random, 'random', return_value=0.0):
            states2, stop2, thread2 = self.run_loop(failing, min_backoff=0.2, max_backoff=10)
            self.assertTrue(eventually(lambda: [s for s, _ in states2].count('retry') >= 1))
        self.assertEqual([d['seconds'] for s, d in states2 if s == 'retry'][0], 0.1)   # jitter: half the backoff

    def test_seventy_five_seconds_of_silence_close_the_link(self):
        with mock.patch.multiple(session_module, RECV_TIMEOUT_SECONDS=0.3, PING_SECONDS=60):
            session = RelaySession(self.relay.origin, self.b, ESTATE, self.peers.get, on_message=print)
            session.open()
            self.assertTrue(session.wait_closed(5))
        self.assertEqual(session.close_reason, 'relay.session.dead')

    def test_one_deadline_covers_the_whole_opening(self):
        server = RawServer(lambda conn, srv: [conn.raw(0x9, b'p') or time.sleep(0.1) for _ in range(40)])
        origin = server.url[len('ws://'):-len('/v1/connect')]
        with mock.patch.object(session_module, 'HELLO_TIMEOUT_SECONDS', 0.5):
            started = time.monotonic()
            with self.assertRaises(socket.timeout):
                RelaySession('http://' + origin, self.b, ESTATE, self.peers.get, on_message=print).open()
        self.assertLess(time.monotonic() - started, 2.0)     # pings every 0.1 s did not hold it open


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class ScopedRound(unittest.TestCase):
    """The #293 scoped adversarial round: a stranger's hello cannot end the node's reconnect loop."""

    def setUp(self):
        self.b = DeviceKeys.generate()
        self.relay = RelayStub({self.b.sig_kid: self.b.sig_public})

    def tearDown(self):
        self.relay.stop()

    def hello_with(self, nonce):
        server = RawServer(lambda conn, srv: conn.send({'v': 1, 'type': 'hello', 'id': 'h', 'protocol': 'fabric-relay/0.1',
                                                         'relay': 'http://' + srv.url[5:-11], 'nonce': nonce,
                                                         'time': '2026-10-15T00:00:00Z'}) or time.sleep(1))
        return 'http://' + server.url[5:-11]

    def test_a_nonce_that_is_not_32_bytes_of_base64url_is_refused_before_signing(self):
        for nonce in ('\ud800', 'short', 'A' * 44, '+' * 43, 'A' * 42 + 'B'):
            with self.subTest(repr(nonce)):
                origin = self.hello_with(nonce)
                with self.assertRaises(RelayRefused) as caught:
                    RelaySession(origin, self.b, ESTATE, {}.get, on_message=print).open()
                self.assertEqual(caught.exception.code, 'protocol-unsupported')

    def test_any_failure_is_reported_and_retried(self):
        states, stop, calls = [], threading.Event(), []

        def make(replay):
            calls.append(1)
            if len(calls) == 1:
                raise KeyError('a bug anywhere in opening')
            stop.set()
            raise OSError('down')
        thread = threading.Thread(target=keep_connected, args=(make, stop),
                                  kwargs={'on_state': lambda s, d: states.append((s, d)), 'min_backoff': 0.01},
                                  daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(states[0], ('closed', {'error': 'KeyError'}))
        self.assertEqual(len(calls), 2)

    def test_retry_after_is_a_finite_wait_or_none(self):
        for value, expected in ((5, 5.0), (0.25, 0.25), (-3, 0.0), (1e12, 60.0), (float('inf'), 0.0),
                                (float('nan'), 0.0), ('abc', 0.0), ([1], 0.0), (True, 0.0), (None, 0.0)):
            self.assertEqual(session_module.retry_after_seconds(value, 60.0), expected, value)

    def test_an_error_answer_to_a_ping_keeps_the_link(self):
        with mock.patch.multiple(session_module, PING_SECONDS=0.05, ANSWER_TIMEOUT_SECONDS=0.5):
            b = RelaySession(self.relay.origin, self.b, ESTATE, {}.get, on_message=print)
            b.open()
            with mock.patch.object(b, 'ping', side_effect=RelayError('rate-limited')):
                time.sleep(0.3)
            self.assertFalse(b.wait_closed(0.3))
            b.ping()
            b.close()


class Redirects(unittest.TestCase):
    """A blob request follows no redirect, through either opener: the signed header never reaches another origin."""

    def test_no_redirect_is_followed(self):
        from node import blobs
        hits = []
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.addCleanup(server.close)
        port = server.getsockname()[1]

        def serve():
            for _ in range(4):
                try:
                    sock, _ = server.accept()
                except OSError:
                    return
                request = sock.recv(65536).decode('latin-1')
                hits.append(request.split(' ')[1])
                sock.sendall(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:%d/elsewhere\r\nContent-Length: 0\r\n'
                             b'Connection: close\r\n\r\n' % port)
                sock.close()
        threading.Thread(target=serve, daemon=True).start()
        import urllib.request
        import urllib.error
        for opener in (blobs._OPENER, blobs._LOCAL_OPENER):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(urllib.request.Request('http://127.0.0.1:%d/v1/blobs/x' % port), timeout=5)
            self.assertEqual(caught.exception.code, 302)
            caught.exception.close()
        self.assertNotIn('/elsewhere', hits)


if __name__ == '__main__':
    unittest.main()
