#!/usr/bin/env python3
"""The node's relay session (OBS-108, RM-21 P5 slice 2): the stdlib WebSocket client and `RelaySession`, against a
relay stub on a loopback literal (relay.md allows `http://127.0.0.1:<port>` for a relay on the same machine).

The stub keeps the relay's rules that a node depends on — it admits by the signed `relay-auth/1`, answers with
`replyTo`, keeps a mailbox in `seq` order, deletes only what is acknowledged, closes a revoked session 4403 — and lets a
test misbehave as a relay may: name another origin, deliver a forged or repeated body.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import p256, relay                           # noqa: E402
from node.blobs import BlobRefused, digest_of, get_blob, put_blob  # noqa: E402
from node.ws import GUID, Closed, WebSocket, WebSocketError  # noqa: E402

if HAVE_CRYPTO:
    from node.device_keys import DeviceKeys            # noqa: E402
    from node.session import RelayError, RelayRefused, RelaySession, SessionClosed, connect_url  # noqa: E402

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
        self.assertTrue(eventually(lambda: all((self.b.sig_kid, s) in self.relay.acks for s in seqs)))

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

    def test_arguments_are_checked_before_anything_is_sent(self):
        for call in (lambda: get_blob(self.relay.origin, self.b, 'sha256:XYZ', [self.b.sig_kid]),
                     lambda: put_blob('http://localhost:1', self.a, b'x', [self.b.sig_kid]),
                     lambda: put_blob(self.relay.origin, self.a, b'x', [])):
            with self.assertRaises(ValueError):
                call()


if __name__ == '__main__':
    unittest.main()
