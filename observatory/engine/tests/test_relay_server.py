#!/usr/bin/env python3
"""The interim relay (OBS-287, the relay project's RLY-04): `node/relay_server.py` and `full relay`.

What is held:
- `admit` gives the reference answer on every case of the contract's vendored `admit` vectors, and does one signature
  verification on every path past the frame's own bytes, whatever the relay holds (DEC-0066);
- the node's own client (`node/session.py`) talks to the server end to end on 127.0.0.1: admission from the estate
  record the node applied, `welcome`, delivery in `seq` order, offline mail, acknowledgement, presence;
- the refusals: a machine outside the record (4401), `unknown-recipient`, `mailbox-full` (messages and bytes),
  `rate-limited` with `retryAfter` and 1008 for a node that keeps sending, `too-large`, `malformed`, `unknown-type`;
- expiry by the ttl the sender signed its `exp` with, a repeat answered with its first `seq` after an ack too, mail
  sealed at rest and kept across a restart, a revoked machine and an expired record closed 4403;
- the transport rules: the auth deadline, anything but `auth` first, a replayed auth, a binary frame (1003), a frame
  over 1 MiB (1009), a missing subprotocol; and logs that carry no estate id, nonce, signature or body;
- `full relay`: the settings it refuses, enable and disable, status, and the relay started from its configuration.
"""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import tmp as tmpdir                                   # noqa: E402  a temporary folder that goes away

_HOME = Path(tmpdir.mkdtemp(prefix='obs-relay-')).resolve()
os.environ['OBSERVATORY_HOME'] = str(_HOME)
os.environ['OBSERVATORY_NODE'] = str(_HOME / 'node')
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import p256, relay                           # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
#: How long a check waits for something that should happen. Only a failing check waits it out, so it is generous: a
#: hosted runner under a full parallel suite runs this file several times slower than a laptop.
WAIT = 15.0
#: The fair answer window these tests run under: longer than any pause a slow runner makes, so a caller that answered
#: keeps its slot because it answered, never because the machine happened to be fast. A test of the window's end sets
#: its own.
WINDOW = 60.0
#: What "at once" means for a check that a stuck reader blocks nobody: a blocked relay never answers (the read gives up
#: at `WAIT`), so the bound only has to sit above a loaded runner's ordinary time.
PROMPT = 5.0
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')

if HAVE_CRYPTO:
    import private_io                                  # noqa: E402
    from node import org, relay_server as rs           # noqa: E402
    from node.device_keys import DeviceKeys            # noqa: E402
    from node.session import RelayError, RelayRefused, RelaySession, SessionClosed  # noqa: E402
    from node.ws import Closed, OP_BINARY, OP_TEXT, WebSocket, WebSocketError  # noqa: E402


def ms(text: str) -> int:
    return relay.instant(text)


def key_of(value):
    """A vector's key: a JWK, or a string of base64 SEC 1 (or Ed25519) octets."""
    return base64.b64decode(value) if isinstance(value, str) else value


def eventually(predicate, timeout=WAIT):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def skeleton():
    spec = importlib.util.spec_from_file_location('relay_test_skeleton', ROOT / 'tools' / 'node_skeleton.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- The contract's vectors ---------------------------------------------------------------------------------------

@needs_crypto
class AdmitVectors(unittest.TestCase):
    def test_every_admit_case_gives_the_reference_answer(self):
        cases = json.loads((VECTORS / 'relay.json').read_text(encoding='utf-8'))['admit']['cases']
        self.assertGreater(len(cases), 20)
        for case in cases:
            ctx = case['context']
            admitted = {kid: key_of(value) for kid, value in (ctx.get('admitted') or {}).items()}
            with self.subTest(case['name']):
                answer = rs.admit(case['frame'], relay_origin=ctx['relay'], estate=ctx.get('estate'),
                                  nonce=ctx['nonce'], nonce_used=ctx['nonceUsed'], admitted_key=admitted.get,
                                  now_ms=ms(ctx['now']))
                self.assertEqual(answer, case['expect'])

    def test_one_verification_on_every_path_past_the_frames_own_bytes(self):
        """DEC-0066: an unknown kid, another estate, no record, a bad, high-S or non-canonical signature, an Ed25519
        or off-curve key all cost one verification, the same as a good signature; a refusal on the frame's own bytes
        costs none."""
        keys = DeviceKeys.generate()
        now = '2026-10-12T00:00:30Z'
        frame = relay.auth_frame(keys.sign, frame_id='a1', relay='https://relay.example', nonce='bm9uY2U',
                                 estate='urn:fabric:estate:example', kid=keys.sig_kid, iat='2026-10-12T00:00:00Z')
        raw = p256.b64url_decode(frame['sig'])
        high = p256.b64url_encode(raw[:32] + (p256.N - int.from_bytes(raw[32:], 'big')).to_bytes(32, 'big'))
        good = {keys.sig_kid: keys.sig_public}
        off_curve = b'\x04' + b'\x01' * 64
        cases = {
            'good': (frame, good, 'urn:fabric:estate:example', True),
            'unknown kid': (frame, {}, 'urn:fabric:estate:example', False),
            'another estate': (frame, good, 'urn:fabric:estate:other', False),
            'no record': (frame, good, None, False),
            'bad signature': (dict(frame, sig=frame['sig'][:10] + ('B' if frame['sig'][10] == 'A' else 'A')
                                   + frame['sig'][11:]), good, 'urn:fabric:estate:example', False),
            'high-S': (dict(frame, sig=high), good, 'urn:fabric:estate:example', False),
            'not canonical': (dict(frame, sig=frame['sig'] + '='), good, 'urn:fabric:estate:example', False),
            'not a string': (dict(frame, sig=7), good, 'urn:fabric:estate:example', False),
            'ed25519 key': (frame, {keys.sig_kid: b'\x07' * 32}, 'urn:fabric:estate:example', False),
            'off-curve key': (frame, {keys.sig_kid: off_curve}, 'urn:fabric:estate:example', False),
        }
        real = p256.es256_verifies
        for name, (f, admitted, estate, ok) in cases.items():
            calls = []
            with self.subTest(name), mock.patch.object(p256, 'es256_verifies',
                                                       side_effect=lambda *a: calls.append(1) or real(*a)):
                answer = rs.admit(f, relay_origin='https://relay.example', estate=estate, nonce='bm9uY2U',
                                  nonce_used=False, admitted_key=admitted.get, now_ms=ms(now))
                self.assertEqual(answer['ok'], ok)
                self.assertEqual(len(calls), 1)
        calls = []
        with mock.patch.object(p256, 'es256_verifies', side_effect=lambda *a: calls.append(1)):
            for wrong in (dict(frame, protocol='fabric-relay/0.2'), dict(frame, alg='EdDSA'),
                          dict(frame, iat='2026-10-11T00:00:00Z')):
                self.assertFalse(rs.admit(wrong, relay_origin='https://relay.example', estate='e', nonce='n',
                                          nonce_used=False, admitted_key=good.get, now_ms=ms(now))['ok'])
            self.assertEqual(rs.admit(frame, relay_origin='https://relay.example', estate='e', nonce='n',
                                      nonce_used=True, admitted_key=good.get, now_ms=ms(now)),
                             {'ok': False, 'code': 'replay'})
        self.assertEqual(calls, [])


# ---- A relay on loopback, admitting from a node's applied record ----------------------------------------------------

class Estate:
    """Two machines in a test estate's revision 1 (signed by the contract vectors' published test passkey), and a
    third machine the record does not list; the record applied to a node folder through the node's own `apply_record`."""

    def __init__(self, folder: Path, valid_seconds: int = 86_400):
        self.sk = skeleton()
        self.laptop, self.desk, self.outsider = DeviceKeys.generate(), DeviceKeys.generate(), DeviceKeys.generate()
        now = int(time.time()) * 1000
        _, authority = self.sk.test_passkey()
        self.name = 'urn:fabric:estate:relay-test-%d' % now
        self.record = self.sk.sign_test_revision({
            'schema': 'estate-record/1', 'estate': self.name, 'revision': 1,
            'issuedAt': relay.rfc3339(now - 60_000), 'validUntil': relay.rfc3339(now + valid_seconds * 1000),
            'authorities': [authority],
            'machines': [{'id': k.device_id, 'deviceKeys': k.enrollment_keys()} for k in (self.laptop, self.desk)],
            'revocations': []})
        self.node = folder / 'node'
        org.apply_record(self.node, self.record, now, authorities=[authority])
        self.peers = relay.peer_keys(self.record, now)['keys']

    def write_delta(self, entries):
        private_io.write(self.node / 'records' / 'delta-state.json',
                         json.dumps({'estate': self.name, 'entries': entries}))


@needs_crypto
class Relay(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tmpdir.mkdtemp(prefix='obs-relay-test-')).resolve()
        self.estate = Estate(self.folder)
        self.offset = [0.0]
        self.logs = []
        self.lock = threading.Lock()
        self.sessions = []
        self.server = None
        for name, value in (('REVALIDATE_SECONDS', 0.1), ('FAIR_ANSWER_SECONDS', WINDOW)):
            patcher = mock.patch.object(rs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for session in self.sessions:
            session.close()
        if self.server is not None:
            self.server.stop()
            self.server.mailboxes.close()
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def log(self, record):
        with self.lock:
            self.logs.append(record)

    def logged(self, event):
        with self.lock:
            return sum(1 for r in self.logs if r['event'] == event)

    def progress(self):
        """The `progress` of every connection the relay holds as pending: 0 sent nothing yet, 1 sent its request."""
        with self.server._lock:
            return sorted(e.progress for e in self.server._pending.values())

    def start(self, limits=None, spool=None, key=None, **server):
        port = free_port()
        origin = 'http://127.0.0.1:%d' % port
        admission = rs.Admission(rs.NodeRecords(self.estate.node).read, log=self.log)
        mailboxes = rs.Mailboxes(spool, key or b'\x01' * 32, limits, log=self.log)
        self.server = rs.RelayServer(origin=origin, admission=admission, mailboxes=mailboxes, port=port,
                                     limits=limits, clock=lambda: time.time() + self.offset[0], log=self.log,
                                     **server).start()
        self.origin = origin
        return self.server

    def open(self, keys, **kw):
        inbox = kw.pop('inbox', [])
        session = RelaySession(self.origin, keys, self.estate.name, self.estate.peers.get,
                               on_message=inbox.append, **kw)
        self.sessions.append(session)
        session.open()
        return session, inbox

    def raw(self):
        ws = WebSocket.connect('ws://127.0.0.1:%d/v1/connect' % self.server.port, subprotocol='fabric-relay.v1')
        hello = json.loads(ws.recv_text(time.monotonic() + WAIT))
        return ws, hello

    def raw_admitted(self, keys):
        ws, hello = self.raw()
        frame = relay.auth_frame(keys.sign, frame_id='a1', relay=self.origin, nonce=hello['nonce'],
                                 estate=self.estate.name, kid=keys.sig_kid, iat=relay.rfc3339(int(time.time() * 1000)))
        ws.send_text(json.dumps(frame))
        welcome = json.loads(ws.recv_text(time.monotonic() + WAIT))
        self.assertEqual(welcome['type'], 'welcome')
        return ws, welcome

    @staticmethod
    def next_frame(ws):
        return json.loads(ws.recv_text(time.monotonic() + WAIT))

    def closed_with(self, ws):
        """The close code after any frames still in flight; the frames read on the way."""
        frames = []
        try:
            while True:
                frames.append(self.next_frame(ws))
        except Closed as closed:
            return closed.code, frames
        finally:
            ws.close()

    # ---- End to end --------------------------------------------------------------------------------------------

    def test_two_machines_exchange_messages_through_the_relay(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        desk, inbox = self.open(self.estate.desk)
        self.assertEqual(laptop.welcome.estate_revision, 1)
        self.assertEqual(laptop.welcome.delta_head['sequence'], 0)
        self.assertIsNone(laptop.welcome.heartbeat)
        self.assertEqual(laptop.welcome.limits, rs.DEFAULT_LIMITS)
        first = laptop.send(self.estate.desk.sig_kid, {'n': 1})
        second = laptop.send(self.estate.desk.sig_kid, {'n': 2})
        self.assertEqual((first, second), (1, 2))
        self.assertTrue(eventually(lambda: len(inbox) == 2))
        self.assertEqual([m['payload'] for m in inbox], [{'n': 1}, {'n': 2}])
        self.assertEqual(inbox[0]['from'], self.estate.laptop.sig_kid)
        # Acknowledged mail leaves the mailbox.
        self.assertTrue(eventually(lambda: self.server.mailboxes.count(self.estate.desk.sig_kid) == 0))
        desk.send(self.estate.laptop.sig_kid, {'back': True})
        presence = laptop.presence([self.estate.desk.sig_kid, self.estate.outsider.sig_kid])
        self.assertEqual([p['state'] for p in presence['presences']], ['online'])
        self.assertEqual(presence['unknown'], [self.estate.outsider.sig_kid])
        text = json.dumps(self.logs)
        self.assertNotIn(self.estate.name, text)
        self.assertNotIn('"sig"', text)
        self.assertNotIn('nonce', text)
        self.assertTrue(any(r['event'] == 'relay.session.admitted' for r in self.logs))

    def test_offline_mail_is_delivered_when_the_machine_connects(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        for n in range(3):
            laptop.send(self.estate.desk.sig_kid, {'n': n})
        _, inbox = self.open(self.estate.desk)
        self.assertTrue(eventually(lambda: len(inbox) == 3))
        self.assertEqual([m['payload']['n'] for m in inbox], [0, 1, 2])

    def test_presence_is_pushed_when_a_machine_comes_and_goes(self):
        self.start()
        seen = []
        laptop, _ = self.open(self.estate.laptop, on_presence=seen.append)
        desk, _ = self.open(self.estate.desk)
        self.assertTrue(eventually(lambda: [f['state'] for f in seen] == ['online']))
        desk.close()
        self.assertTrue(eventually(lambda: [f['state'] for f in seen] == ['online', 'offline']))
        self.assertEqual(seen[0]['kid'], self.estate.desk.sig_kid)
        answer = laptop.presence([self.estate.desk.sig_kid, self.estate.desk.sig_kid])
        self.assertEqual([p['state'] for p in answer['presences']], ['offline'])

    # ---- Refusals ----------------------------------------------------------------------------------------------

    def test_a_machine_outside_the_record_is_refused_4401(self):
        self.start()
        with self.assertRaises(RelayRefused) as refused:
            self.open(self.estate.outsider)
        self.assertEqual(refused.exception.code, 'unauthenticated')
        self.assertTrue(any(r.get('reason') == 'not-proven' for r in self.logs))

    def test_no_record_admits_nobody(self):
        (self.estate.node / 'records' / 'estate-record.json').unlink()
        self.start()
        with self.assertRaises(RelayRefused):
            self.open(self.estate.laptop)
        self.assertTrue(any(r.get('reason') == 'admission-no-record' for r in self.logs))

    def test_a_record_edited_on_disk_admits_nobody(self):
        path = self.estate.node / 'records' / 'estate-record.json'
        doc = json.loads(private_io.read(path))
        doc['validUntil'] = '2099-01-01T00:00:00Z'
        private_io.write(path, json.dumps(doc))
        self.start()
        with self.assertRaises(RelayRefused):
            self.open(self.estate.laptop)
        self.assertTrue(any(r.get('problem') == 'record-unreadable' for r in self.logs))

    def test_unknown_recipient(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        with self.assertRaises(RelayError) as refused:
            laptop.send(self.estate.outsider.sig_kid, {'x': 1})
        self.assertEqual(refused.exception.code, 'unknown-recipient')
        laptop.ping()                                       # the connection stays open

    def test_mailbox_full_by_messages_and_by_bytes(self):
        self.start(limits={'mailboxMessages': 2, 'mailboxBytes': 4000})
        laptop, _ = self.open(self.estate.laptop)
        laptop.send(self.estate.desk.sig_kid, {'n': 1})
        laptop.send(self.estate.desk.sig_kid, {'n': 2})
        with self.assertRaises(RelayError) as full:
            laptop.send(self.estate.desk.sig_kid, {'n': 3})
        self.assertEqual(full.exception.code, 'mailbox-full')
        with self.assertRaises(RelayError) as full:
            laptop.send(self.estate.laptop.sig_kid, {'pad': 'x' * 4000})
        self.assertEqual(full.exception.code, 'mailbox-full')
        self.assertEqual(laptop.send(self.estate.laptop.sig_kid, {'small': 1}), 1)

    def test_rate_limited_then_closed_1008_for_a_node_that_keeps_sending(self):
        self.start(limits={'sendPerMinute': 2})
        laptop, _ = self.open(self.estate.laptop)
        self.assertEqual(laptop.welcome.limits['sendPerMinute'], 2)
        laptop.send(self.estate.desk.sig_kid, {'n': 1})
        laptop.send(self.estate.desk.sig_kid, {'n': 2})
        with self.assertRaises(RelayError) as limited:
            laptop.send(self.estate.desk.sig_kid, {'n': 3})
        self.assertEqual(limited.exception.code, 'rate-limited')
        self.assertTrue(1 <= limited.exception.retry_after <= 60)
        with self.assertRaises((RelayError, SessionClosed, TimeoutError)):
            laptop.send(self.estate.desk.sig_kid, {'n': 4})
        self.assertTrue(laptop.wait_closed(WAIT))
        self.assertEqual(laptop.close_code, 1008)

    def test_a_repeat_answers_its_first_seq_after_an_ack_too(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        _, inbox = self.open(self.estate.desk)
        seq = laptop.send(self.estate.desk.sig_kid, {'n': 1}, msg_id='same-message')
        self.assertTrue(eventually(lambda: len(inbox) == 1))
        self.assertTrue(eventually(lambda: self.server.mailboxes.count() == 0))
        self.assertEqual(laptop.send(self.estate.desk.sig_kid, {'n': 1}, msg_id='same-message'), seq)
        time.sleep(0.2)
        self.assertEqual(len(inbox), 1)

    def test_mail_expires_with_its_ttl(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        laptop.send(self.estate.desk.sig_kid, {'short': True}, ttl_seconds=5)
        laptop.send(self.estate.desk.sig_kid, {'long': True}, ttl_seconds=600)
        self.offset[0] = 6.0
        _, inbox = self.open(self.estate.desk)
        self.assertTrue(eventually(lambda: len(inbox) == 1))
        time.sleep(0.2)
        self.assertEqual([m['payload'] for m in inbox], [{'long': True}])

    def test_a_revoked_machine_is_closed_4403_and_is_no_recipient(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        desk, _ = self.open(self.estate.desk)
        self.estate.write_delta([{'sequence': 1, 'revocation': {'kind': 'machine', 'ref': self.estate.desk.device_id}}])
        self.assertTrue(desk.wait_closed(WAIT))
        self.assertEqual(desk.close_code, 4403)
        with self.assertRaises(RelayError) as refused:
            laptop.send(self.estate.desk.sig_kid, {'x': 1})
        self.assertEqual(refused.exception.code, 'unknown-recipient')
        with self.assertRaises(RelayRefused):
            self.open(self.estate.desk)

    def test_a_forked_entrys_revocation_stays_in_force(self):
        """RD-10: the revocation of a correctly signed entry that forked the chain leaves the admission set too."""
        self.start()
        desk, _ = self.open(self.estate.desk)
        private_io.write(self.estate.node / 'records' / 'delta-state.json', json.dumps({
            'estate': self.estate.name, 'entries': [],
            'forked': [{'sequence': 1, 'revocation': {'kind': 'device-key', 'ref': self.estate.desk.sig_kid}}]}))
        self.assertTrue(desk.wait_closed(WAIT))
        self.assertEqual(desk.close_code, 4403)

    def test_an_expired_record_closes_every_session_4403(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        ws, _ = self.raw_admitted(self.estate.desk)
        self.offset[0] = 2 * 86_400.0
        self.assertTrue(laptop.wait_closed(WAIT))
        self.assertEqual(laptop.close_code, 4403)
        code, frames = self.closed_with(ws)
        self.assertEqual(code, 4403)
        self.assertEqual(frames[-1]['code'], 'expired')

    # ---- Frames ------------------------------------------------------------------------------------------------

    def test_welcome_and_hello(self):
        self.start()
        ws, hello = self.raw()
        self.assertEqual((hello['type'], hello['protocol'], hello['relay']), ('hello', 'fabric-relay/0.1', self.origin))
        self.assertEqual(len(p256.b64url_decode(hello['nonce'])), 32)
        ws.close()
        ws, welcome = self.raw_admitted(self.estate.laptop)
        self.assertNotIn(self.estate.name, json.dumps(welcome))
        self.assertEqual(welcome['estateRevision'], 1)
        ws.close()

    def test_frames_a_node_may_not_send_and_malformed_ones(self):
        self.start()
        ws, _ = self.raw_admitted(self.estate.laptop)
        for kind in ('hello', 'deliver', 'delta', 'heartbeat', 'nonsense'):
            ws.send_text(json.dumps({'v': 1, 'type': kind, 'id': 'f-' + kind}))
            answer = self.next_frame(ws)
            self.assertEqual((answer['code'], answer['replyTo']), ('unknown-type', 'f-' + kind))
        for frame in ({'v': 1, 'type': 'send', 'id': 's1', 'to': self.estate.desk.sig_kid, 'msgId': 'm', 'body': 'QQ',
                       'ttl': 0},
                      {'v': 1, 'type': 'send', 'id': 's2', 'to': self.estate.desk.kem_kid, 'msgId': 'm', 'body': 'QQ',
                       'ttl': 60},
                      {'v': 1, 'type': 'ack', 'id': 'k1', 'seq': -1},
                      {'v': 1, 'type': 'presence-query', 'id': 'p1', 'kids': 'all'},
                      {'v': 1, 'type': 'auth', 'id': 'a2'}):
            ws.send_text(json.dumps(frame))
            answer = self.next_frame(ws)
            self.assertEqual((answer['code'], answer['replyTo']), ('malformed', frame['id']))
        ws.send_text('[1, 2]')
        self.assertEqual(self.next_frame(ws)['code'], 'malformed')
        ws.send_text(json.dumps({'v': 1, 'type': 'ping', 'id': 'still-open'}))
        self.assertEqual(self.next_frame(ws), {'v': 1, 'type': 'pong', 'id': mock.ANY, 'replyTo': 'still-open'})
        ws.close()

    def test_a_body_over_the_limit_is_too_large_and_a_frame_over_1_mib_closes_1009(self):
        self.start()
        ws, _ = self.raw_admitted(self.estate.laptop)
        body = 'A' * 1_045_000                               # 783 750 bytes decoded, in a frame under 1 MiB
        ws.send_text(json.dumps({'v': 1, 'type': 'send', 'id': 'big', 'to': self.estate.desk.sig_kid,
                                 'msgId': 'm', 'body': body, 'ttl': 60}))
        answer = self.next_frame(ws)
        self.assertEqual((answer['code'], answer['replyTo']), ('too-large', 'big'))
        # Only the header: the relay refuses from the declared length before reading (or allocating) the payload.
        ws._sock.sendall(bytes([0x80 | OP_TEXT, 0x80 | 127]) + (relay.MAX_FRAME_BYTES + 1).to_bytes(8, 'big')
                         + b'\0\0\0\0' + b' ' * 1024)
        code, frames = self.closed_with(ws)
        self.assertEqual((code, frames[-1]['code']), (1009, 'too-large'))

    def test_a_binary_frame_closes_1003(self):
        self.start()
        ws, _ = self.raw_admitted(self.estate.laptop)
        ws._send_frame(OP_BINARY, b'\x00\x01')
        self.assertEqual(self.closed_with(ws), (1003, []))

    def test_delta_query_pages_the_entries_the_node_applied(self):
        entries = [{'sequence': n, 'revocation': {'kind': 'grant', 'ref': 'urn:example:grant:%d' % n}}
                   for n in (1, 2, 3)]
        self.estate.write_delta(entries)
        self.start()
        ws, welcome = self.raw_admitted(self.estate.laptop)
        self.assertEqual(welcome['deltaHead']['sequence'], 3)
        with mock.patch.object(rs, 'DELTA_PAGE_ENTRIES', 2):
            ws.send_text(json.dumps({'v': 1, 'type': 'delta-query', 'id': 'q1', 'fromSeq': 1}))
            pages = [self.next_frame(ws) for _ in range(3)]
        self.assertEqual([len(p['entries']) for p in pages], [2, 1, 0])
        self.assertEqual([p['head']['sequence'] for p in pages], [2, 3, 3])
        self.assertTrue(all(p['replyTo'] == 'q1' for p in pages))
        self.assertEqual(pages[2]['head'], welcome['deltaHead'])
        ws.send_text(json.dumps({'v': 1, 'type': 'delta-query', 'id': 'q2', 'fromSeq': 4}))
        self.assertEqual(self.next_frame(ws)['entries'], [])
        ws.close()

    # ---- Transport ---------------------------------------------------------------------------------------------

    def test_no_auth_in_time_closes_4401(self):
        self.start()
        with mock.patch.object(rs, 'AUTH_DEADLINE_SECONDS', 0.3):
            ws, _ = self.raw()
            code, frames = self.closed_with(ws)
        self.assertEqual((code, frames[-1]['code']), (4401, 'unauthenticated'))

    def test_anything_but_auth_first_closes_4401(self):
        self.start()
        ws, _ = self.raw()
        ws.send_text(json.dumps({'v': 1, 'type': 'ping', 'id': 'p'}))
        code, frames = self.closed_with(ws)
        self.assertEqual((code, frames[-1]['code']), (4401, 'unauthenticated'))

    def test_an_auth_frame_replayed_on_another_connection_is_refused(self):
        self.start()
        ws, hello = self.raw()
        frame = relay.auth_frame(self.estate.laptop.sign, frame_id='a1', relay=self.origin, nonce=hello['nonce'],
                                 estate=self.estate.name, kid=self.estate.laptop.sig_kid,
                                 iat=relay.rfc3339(int(time.time() * 1000)))
        ws.send_text(json.dumps(frame))
        self.assertEqual(self.next_frame(ws)['type'], 'welcome')
        other, _ = self.raw()
        other.send_text(json.dumps(frame))
        code, frames = self.closed_with(other)
        self.assertEqual((code, frames[-1]['code']), (4401, 'unauthenticated'))
        ws.close()

    def test_the_upgrade_needs_the_subprotocol_and_other_routes_answer_json(self):
        self.start()
        with self.assertRaises(WebSocketError):
            WebSocket.connect('ws://127.0.0.1:%d/v1/connect' % self.server.port, subprotocol='other.v1')
        with urllib.request.urlopen(self.origin + '/healthz', timeout=WAIT) as answer:
            self.assertEqual(json.loads(answer.read()), {'ok': True, 'protocol': 'fabric-relay/0.1',
                                                          'service': 'observatory-relay'})
        for path, status in (('/v1/blobs/sha256:' + '0' * 64, 501), ('/elsewhere', 404)):
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(self.origin + path, timeout=WAIT)
            self.assertEqual(refused.exception.code, status)
            self.assertEqual(refused.exception.headers['Cache-Control'], 'no-store')
            self.assertIn('error', json.loads(refused.exception.read()))
            refused.exception.close()

    # ---- Review of #400: deadlines, caps, accept, rates, paging, slow readers --------------------------------

    def upgrade(self):
        """A raw socket upgraded to the relay, its hello read; nothing sent after."""
        sock = socket.create_connection(('127.0.0.1', self.server.port))
        sock.sendall(b'GET /v1/connect HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                     b'Sec-WebSocket-Protocol: fabric-relay.v1\r\n\r\n')
        return sock

    @staticmethod
    def dribble_until_closed(sock, data, every, limit):
        """Sends `data` a byte at a time every `every` s; the seconds until the relay closed, or None at `limit`."""
        start = time.monotonic()
        sock.settimeout(every)
        for byte in data:
            if time.monotonic() - start > limit:
                return None
            try:
                sock.send(bytes([byte]))
                while True:
                    if sock.recv(65536) == b'':
                        return time.monotonic() - start
            except socket.timeout:
                continue
            except OSError:
                return time.monotonic() - start
        return None

    def test_the_request_head_has_a_wall_clock_deadline(self):
        """One byte every 0.1 s keeps any per-read timeout satisfied for the 23 s the head takes; only a deadline
        across every byte closes it, at 0.3 s. The outer limit only bounds a relay that never closes it."""
        self.start()
        with mock.patch.object(rs, 'REQUEST_HEAD_SECONDS', 0.3):
            sock = socket.create_connection(('127.0.0.1', self.server.port))
            took = self.dribble_until_closed(sock, b'GET /healthz HTTP/1.1\r\nX-Pad: ' + b'a' * 200, 0.1, WAIT)
            sock.close()
        self.assertIsNotNone(took, 'a client sending one byte at a time kept its slot past the deadline')
        self.assertLess(took, 5.0)

    def test_auth_has_a_wall_clock_deadline_and_closes_4401(self):
        self.start()
        with mock.patch.object(rs, 'AUTH_DEADLINE_SECONDS', 0.3):
            sock = self.upgrade()
            frame = bytes([0x81, 0x80 | 126]) + (200).to_bytes(2, 'big') + b'\0\0\0\0' + b'x' * 200
            took = self.dribble_until_closed(sock, frame, 0.1, WAIT)
            sock.close()
        self.assertIsNotNone(took, 'a client dribbling its auth frame kept its slot past the deadline')
        self.assertLess(took, 5.0)
        self.assertTrue(any(r.get('reason') == 'auth-deadline' and r.get('close') == 4401 for r in self.logs))

    def test_unauthenticated_connections_cannot_starve_machines(self):
        """Idle sockets fill the unauthenticated cap; a machine still gets in (the oldest idle one gives way), and
        admitted sessions have their own cap."""
        self.start(max_pending=2)
        idle = [socket.create_connection(('127.0.0.1', self.server.port)) for _ in range(4)]
        try:
            # Four on a budget of two: the third and the fourth each displaced one, so every slot holds an idle one.
            self.assertTrue(eventually(lambda: self.logged('relay.pending.evicted') == 2 and self.progress() == [0, 0]))
            laptop, _ = self.open(self.estate.laptop)
            laptop.ping()
        finally:
            for sock in idle:
                sock.close()

    def test_a_source_address_has_its_own_cap_loopback_included(self):
        """No address is exempt: a second caller from the address of one still answering inside its fair window is
        refused, and an idle one from that address gives way."""
        self.start(max_pending=8, max_pending_per_source=1)
        first = self.upgrade()
        try:
            self.assertTrue(eventually(lambda: self.progress() == [1]))    # its request read: it is answering
            with self.assertRaises(WebSocketError):
                self.raw()
        finally:
            first.close()
        self.assertTrue(eventually(lambda: self.progress() == []))
        idle = socket.create_connection(('127.0.0.1', self.server.port))
        try:
            self.assertTrue(eventually(lambda: self.progress() == [0]))
            ws, _ = self.raw()                              # the idle one, which sent nothing, gave way
            ws.close()
            self.assertEqual(self.logged('relay.pending.evicted'), 1)
        finally:
            idle.close()

    # ---- Review of #400, second round ----------------------------------------------------------------------

    def flood(self, stop, opened, address_header=None):
        """Idle connections from this machine, one every 5 ms (the reviewer's flood, twice as dense), until
        `stop`."""
        held = []
        while not stop.is_set():
            try:
                sock = socket.create_connection(('127.0.0.1', self.server.port))
                if address_header:
                    sock.sendall(b'GET /v1/connect HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: '
                                 + address_header + b'\r\n')        # an unfinished head
                held.append(sock)
                opened[0] += 1
            except OSError:
                pass
            if len(held) > 200:
                for old in held[:100]:
                    old.close()
                del held[:100]
            time.sleep(0.005)
        for sock in held:
            sock.close()

    def flooding(self):
        """A flood under way in the background: its count of opened connections, and how to end it."""
        stop, opened = threading.Event(), [0]
        flooder = threading.Thread(target=self.flood, args=(stop, opened), daemon=True)
        flooder.start()

        def end():
            stop.set()
            flooder.join(WAIT)
        self.addCleanup(end)
        return opened, end

    def evictions_grow(self, by):
        """Waits until the relay displaced `by` more pending connections: the flood is under way at the relay itself,
        however slow the machine. Counted in connections, never in seconds."""
        target = self.logged('relay.pending.evicted') + by
        self.assertTrue(eventually(lambda: self.logged('relay.pending.evicted') >= target), 'the flood stalled')

    def test_a_flood_from_the_same_address_never_locks_a_machine_out(self):
        """Each of five admissions starts after the flood displaced ten more connections at the relay, and the flood
        goes on while the machine answers, so every admission happens inside the flood."""
        self.start()
        opened, end = self.flooding()
        admitted = 0
        try:
            for _ in range(5):
                self.evictions_grow(10)
                session, _ = self.open(self.estate.laptop)
                admitted += session.welcome is not None
                session.close()
        finally:
            end()
        self.assertEqual(admitted, 5)
        self.assertGreaterEqual(self.logged('relay.pending.evicted'), 50)
        self.assertGreaterEqual(opened[0], 50 + rs.MAX_PENDING_PER_SOURCE)

    def test_a_slow_answer_inside_the_fair_window_survives_a_flood(self):
        """A caller that read its hello owes `auth` while the flood displaces twenty connections of its own address
        around it; it answers late and is still admitted."""
        self.start()
        _, end = self.flooding()
        try:
            self.evictions_grow(10)
            ws, hello = self.raw()
            self.evictions_grow(20)
            ws.send_text(json.dumps(relay.auth_frame(
                self.estate.laptop.sign, frame_id='a1', relay=self.origin, nonce=hello['nonce'],
                estate=self.estate.name, kid=self.estate.laptop.sig_kid, iat=relay.rfc3339(int(time.time() * 1000)))))
            self.assertEqual(self.next_frame(ws)['type'], 'welcome')
            ws.close()
        finally:
            end()

    @mock.patch.object(rs, 'FAIR_ANSWER_SECONDS', 2.0)
    def test_the_address_holding_most_slots_loses_first(self):
        now = 100.0
        entries = [rs._Pending(None, 'A', at=now - 5) for _ in range(3)] + [rs._Pending(None, 'B', at=now - 9)]
        victim = rs.RelayServer.victim(entries, now)
        self.assertEqual((victim.address, victim.at), ('A', now - 5))
        young = [rs._Pending(None, 'A', at=now - 0.1, progress=1), rs._Pending(None, 'B', at=now - 9, progress=1)]
        self.assertEqual(rs.RelayServer.victim(young, now).address, 'B')
        self.assertIsNone(rs.RelayServer.victim(young[:1], now))   # answering inside its fair window
        mixed = [rs._Pending(None, 'A', at=now - 9, progress=1), rs._Pending(None, 'A', at=now - 1)]
        self.assertEqual(rs.RelayServer.victim(mixed, now).progress, 0)   # silence goes before an answer

    def forwarded_upgrade(self, address: bytes):
        sock = socket.create_connection(('127.0.0.1', self.server.port))
        sock.sendall(b'GET /v1/connect HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'X-Forwarded-For: 203.0.113.9, ' + address + b'\r\n'
                     b'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                     b'Sec-WebSocket-Protocol: fabric-relay.v1\r\n\r\n')
        sock.settimeout(WAIT)
        status = sock.recv(64)
        return sock, status.split(b' ')[1] if status else b''

    def test_a_fresh_address_gets_in_past_a_flood_spread_over_addresses(self):
        """Max-min fair: four addresses hold every slot with requests sent (each inside its fair window), and a
        newcomer from a fifth still gets in; more arrivals from the full addresses only displace one another."""
        self.start(max_pending=8, trusted_proxy=True)
        held = [self.forwarded_upgrade(b'192.0.2.%d' % (n % 4 + 1))[0] for n in range(8)]
        try:
            fresh, status = self.forwarded_upgrade(b'198.51.100.7')
            held.append(fresh)
            self.assertEqual(status, b'101')
            for n in range(6):
                held.append(self.forwarded_upgrade(b'192.0.2.%d' % (n % 4 + 1))[0])
            with self.server._lock:
                addresses = [e.address for e in self.server._pending.values()]
            self.assertIn('198.51.100.7', addresses)        # the newcomer kept its place
        finally:
            for sock in held:
                sock.close()
        self.assertTrue(any(r['event'] == 'relay.pending.evicted' and r.get('fair') for r in self.logs))

    def test_one_slot_each_is_already_fair(self):
        self.start(max_pending=4, trusted_proxy=True)
        held = [self.forwarded_upgrade(b'192.0.2.%d' % n)[0] for n in range(1, 5)]
        try:
            fresh, status = self.forwarded_upgrade(b'198.51.100.7')
            held.append(fresh)
            self.assertEqual(status, b'503')                # a swap gains nobody anything
        finally:
            for sock in held:
                sock.close()

    def test_addresses_are_counted_by_their_group(self):
        group = rs.address_group
        self.assertEqual(group('2001:db8:1:2::5'), group('2001:db8:1:2:ffff::1'))
        self.assertNotEqual(group('2001:db8:1:2::5'), group('2001:db8:1:3::5'))
        self.assertEqual(group('::ffff:192.0.2.1'), group('192.0.2.1'))
        self.assertNotEqual(group('192.0.2.1'), group('192.0.2.2'))
        self.assertEqual(group('not an address'), 'not an address')
        self.start(max_pending_per_source=1, trusted_proxy=True)
        first, status = self.forwarded_upgrade(b'2001:db8:1:2::5')
        second, other = self.forwarded_upgrade(b'2001:db8:1:2::6')
        for sock in (first, second):
            sock.close()
        self.assertEqual((status, other), (b'101', b'503'))

    def test_a_forwarded_address_with_a_port_or_malformed(self):
        self.assertEqual(rs.forwarded_for({'x-forwarded-for': '203.0.113.9, 192.0.2.1:5678'}), ('192.0.2.1', False))
        self.assertEqual(rs.forwarded_for({'x-forwarded-for': '[2001:db8::1]:443'}), ('2001:db8::1', False))
        self.assertEqual(rs.forwarded_for({'x-forwarded-for': '2001:db8::1'}), ('2001:db8::1', False))
        self.assertEqual(rs.forwarded_for({}), (None, False))
        self.assertEqual(rs.forwarded_for({'x-forwarded-for': 'unknown'}), (None, True))
        self.start(trusted_proxy=True)
        sock, status = self.forwarded_upgrade(b'not<an>address\x01' + b'x' * 300)
        sock.close()
        self.assertEqual(status, b'101')
        logged = [r for r in self.logs if r['event'] == 'relay.forwarded.unreadable']
        self.assertEqual(len(logged), 1)
        self.assertLessEqual(len(logged[0]['value']), 64)
        self.assertNotIn('<', logged[0]['value'])

    def test_behind_a_trusted_proxy_the_forwarded_address_is_the_callers(self):
        self.start(max_pending_per_source=1, trusted_proxy=True)
        first, status = self.forwarded_upgrade(b'192.0.2.1')
        second, other = self.forwarded_upgrade(b'192.0.2.2')
        third, again = self.forwarded_upgrade(b'192.0.2.1')
        for sock in (first, second, third):
            sock.close()
        self.assertEqual((status, other, again), (b'101', b'101', b'503'))
        self.assertTrue(any(r['event'] == 'relay.refused' and r.get('reason') == 'per-source' for r in self.logs))

    def test_without_the_setting_a_forwarded_header_is_ignored(self):
        self.start(max_pending_per_source=1)
        first, status = self.forwarded_upgrade(b'192.0.2.1')       # answered 101: its request was read
        second, other = self.forwarded_upgrade(b'192.0.2.2')       # refused on arrival, its request never read
        for sock in (first, second):
            sock.close()
        self.assertEqual((status, other), (b'101', b'503'))

    def test_a_refusal_on_arrival_is_read_whole_and_closed_cleanly(self):
        """A connection refused before its request was read gets the whole 503 and a clean close, never a reset: the
        relay half-closes and drains what the caller sends before it closes (a reset makes a Windows caller drop the
        answer unread)."""
        self.start(max_pending_per_source=1)
        first, status = self.forwarded_upgrade(b'192.0.2.1')
        self.assertEqual(status, b'101')
        try:
            sock = socket.create_connection(('127.0.0.1', self.server.port))
            sock.settimeout(WAIT)
            sock.sendall(b'GET /v1/connect HTTP/1.1\r\nHost: x\r\nX-Pad: ' + b'a' * 4000 + b'\r\n\r\n')
            data = b''
            while True:
                chunk = sock.recv(4096)                     # a reset raises ConnectionResetError here
                if not chunk:
                    break
                data += chunk
            sock.close()
        finally:
            first.close()
        self.assertTrue(data.startswith(b'HTTP/1.1 503 '), data[:40])
        self.assertEqual(json.loads(data.split(b'\r\n\r\n', 1)[1])['error']['code'], 'rate-limited')
        self.assertTrue(eventually(lambda: self.server._lingering._value == rs.MAX_LINGERING))   # every slot back

    def test_address_counts_are_dropped_at_zero(self):
        self.start(max_pending=1, trusted_proxy=True)
        with mock.patch.object(rs, 'FAIR_ANSWER_SECONDS', 0.1):
            first, _ = self.forwarded_upgrade(b'192.0.2.1')
            time.sleep(0.15)                                # past its fair window
            second, status = self.forwarded_upgrade(b'192.0.2.2')
            self.assertEqual(status, b'101')
            self.assertTrue(eventually(lambda: '192.0.2.1' not in self.server._per_source))
            first.close()
            second.close()
        self.assertTrue(eventually(lambda: self.server._per_source == {}))

    def test_pongs_are_counted_and_coalesced(self):
        self.start()
        sock = self.upgrade()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sessions = []

        def held():
            # Take the session before the flood: a slow runner may close it for overflow before the
            # flood ends, which is the bound working, so the checks read the object, not the table.
            with self.server._lock:
                sessions[:] = [e.session for e in self.server._pending.values() if e.session is not None]
            return len(sessions) == 1
        self.assertTrue(eventually(held))
        session = sessions[0]
        pings = (bytes([0x89, 0x80 | 125]) + b'\0\0\0\0' + b'p' * 125) * 512
        # 12 288 pings, a fixed count: their pongs (1.5 MB) are far more than the caller's 1 KiB receive buffer and
        # the relay's send buffer hold, so the answers back up whatever the machine's speed.
        sock.settimeout(WAIT)
        for _ in range(24):
            try:
                sock.sendall(pings)
            except OSError:                                 # closed for overflow, or no longer read: the bound
                break
        sock.close()
        self.assertLessEqual(len(session.outbox), 2)
        self.assertLessEqual(session.outbox_bytes, rs.OUTBOX_BYTES)

    def test_a_tombstone_of_a_queued_message_is_never_forgotten(self):
        boxes = rs.Mailboxes(None, b'\x01' * 32)
        now = int(time.time() * 1000)
        with mock.patch.object(rs, 'MAX_TOMBSTONES_PER_SENDER', 2):
            first = boxes.append('b#sig', 'a#sig', 'm0', 'QUFB', 60, now)['seq']
            boxes.append('b#sig', 'a#sig', 'm1', 'QUFB', 60, now)
            self.assertEqual(boxes.append('b#sig', 'a#sig', 'm2', 'QUFB', 60, now), {'full': 'tombstones'})
            self.assertEqual(boxes.append('b#sig', 'a#sig', 'm0', 'QUFB', 60, now), {'seq': first, 'duplicate': True})
            boxes.ack('b#sig', first)
            self.assertFalse(boxes.append('b#sig', 'a#sig', 'm2', 'QUFB', 60, now)['duplicate'])
            self.assertTrue(boxes.append('b#sig', 'a#sig', 'm1', 'QUFB', 60, now)['duplicate'])
        boxes.close()

    def test_a_sender_at_its_tombstone_cap_is_refused_not_forgotten(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        with mock.patch.object(rs, 'MAX_TOMBSTONES_PER_SENDER', 2):
            seq = laptop.send(self.estate.desk.sig_kid, {'n': 0}, msg_id='keep-0')
            laptop.send(self.estate.desk.sig_kid, {'n': 1}, msg_id='keep-1')
            with self.assertRaises(RelayError) as refused:
                laptop.send(self.estate.desk.sig_kid, {'n': 2}, msg_id='keep-2')
            self.assertEqual(refused.exception.code, 'rate-limited')
            self.assertGreater(refused.exception.retry_after, 0)
            self.assertEqual(laptop.send(self.estate.desk.sig_kid, {'n': 0}, msg_id='keep-0'), seq)

    def test_unreadable_rows_never_stall_delivery(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        for n in range(12):
            laptop.send(self.estate.desk.sig_kid, {'n': n})
        boxes = self.server.mailboxes
        with boxes._lock:
            boxes._db.execute('UPDATE mail SET sealed = ? WHERE recipient = ? AND seq BETWEEN 2 AND 7',
                              (b'\0' * 64, self.estate.desk.sig_kid))
        with mock.patch.object(rs, 'FLUSH_PAGE_ROWS', 4):
            _, inbox = self.open(self.estate.desk)
            # Acknowledgements are asynchronous: wait until the mailbox itself is empty, not for the inbox count.
            # The unreadable rows were skipped and dropped, never counted against a page.
            self.assertTrue(eventually(lambda: boxes.count(self.estate.desk.sig_kid) == 0))
        self.assertEqual([m['payload']['n'] for m in inbox], [0, 7, 8, 9, 10, 11])

    def test_admitted_sessions_beyond_the_cap_are_refused(self):
        self.start(max_connections=1)
        laptop, _ = self.open(self.estate.laptop)
        with self.assertRaises(RelayRefused) as refused:
            self.open(self.estate.desk)
        self.assertEqual(refused.exception.code, 'rate-limited')
        laptop.ping()

    def test_a_machine_holds_a_bounded_number_of_connections(self):
        self.start(max_per_kid=2)
        first, _ = self.raw_admitted(self.estate.laptop)
        second, _ = self.raw_admitted(self.estate.laptop)
        third, _ = self.raw_admitted(self.estate.laptop)
        code, _ = self.closed_with(first)
        self.assertEqual(code, 1001)
        self.assertTrue(any(r['event'] == 'relay.session.replaced' for r in self.logs))
        for ws in (second, third):
            ws.send_text(json.dumps({'v': 1, 'type': 'ping', 'id': 'p'}))
            self.assertEqual(self.next_frame(ws)['type'], 'pong')
            ws.close()

    @mock.patch.object(rs, 'ACCEPT_FAILING_SECONDS', 0.2)
    @mock.patch.object(rs, 'ACCEPT_BACKOFF_SECONDS', 0.05)
    def test_accept_survives_errors_and_health_reports_them(self):
        self.start()
        real = self.server._listener
        failing = threading.Event()
        failing.set()

        class Flaky:
            def accept(self):
                if failing.is_set():
                    time.sleep(0.01)
                    raise OSError(24, 'Too many open files')
                return real.accept()

            def close(self):
                real.close()

            def fileno(self):
                return real.fileno()

        self.server._listener = Flaky()
        with contextlib.suppress(OSError):
            urllib.request.urlopen(self.origin + '/healthz', timeout=1).close()   # wakes the real accept once
        self.assertTrue(eventually(lambda: self.server.health() is not None and 'accept' in self.server.health()))
        self.assertTrue(any(r['event'] == 'relay.accept.error' for r in self.logs))
        failing.clear()
        self.assertTrue(eventually(lambda: _answers(self.server.port)))
        self.assertTrue(eventually(lambda: self.server.health() is None))

    def test_health_reports_an_accept_loop_that_ended(self):
        self.start()
        self.assertIsNone(self.server.health())
        self.server._accepting.clear()
        self.assertIn('accept', self.server.health())
        import relay_cli
        self.assertIn('accept loop ended', relay_cli.health(self.server))   # the node's server shows this row

    def test_descriptor_caps_stay_below_the_process_limit(self):
        self.assertEqual(rs.fd_caps(4096, 30), (rs.MAX_PENDING, rs.MAX_SESSIONS))
        pending, sessions = rs.fd_caps(256, 40)
        self.assertLessEqual(pending + sessions, 256 - 40 - rs.FD_RESERVE)
        self.assertGreaterEqual(min(pending, sessions), 1)
        self.assertEqual(rs.fd_caps(None, 0), (rs.MAX_PENDING, rs.MAX_SESSIONS))

    def test_the_rate_limit_is_per_machine_across_its_connections(self):
        self.start(limits={'sendPerMinute': 5})
        accepted, held = 0, []
        for c in range(3):
            ws, _ = self.raw_admitted(self.estate.laptop)
            held.append(ws)
            for n in range(3):
                try:
                    ws.send_text(json.dumps({'v': 1, 'id': 'c%dn%d' % (c, n), 'type': 'send',
                                             'to': self.estate.desk.sig_kid, 'msgId': 'm%d-%d' % (c, n),
                                             'body': 'QUFB', 'ttl': 60}))
                    accepted += self.next_frame(ws)['type'] == 'accepted'
                except (Closed, OSError):
                    break                                   # closed 1008: it kept sending after rate-limited
        for ws in held:
            ws.close()
        self.assertEqual(accepted, 5)

    def test_tombstones_are_bounded_per_sender_and_pruned_on_their_ttl(self):
        boxes = rs.Mailboxes(None, b'\x01' * 32)
        now = int(time.time() * 1000)
        with mock.patch.object(rs, 'MAX_TOMBSTONES_PER_SENDER', 3):
            for n in range(6):
                seq = boxes.append('b#sig', 'a#sig', 'm%d' % n, 'QUFB', 60, now)['seq']
                boxes.ack('b#sig', seq)
        self.assertEqual(boxes.tombstones('a#sig'), 3)
        self.assertEqual(boxes.append('b#sig', 'a#sig', 'm5', 'QUFB', 60, now), {'seq': 6, 'duplicate': True})
        boxes.purge(now + 61_000)
        self.assertEqual(boxes.tombstones('a#sig'), 0)
        boxes.close()

    def test_a_page_is_bounded_in_rows_and_bytes(self):
        boxes = rs.Mailboxes(None, b'\x01' * 32)
        now = int(time.time() * 1000)
        for n in range(10):
            boxes.append('b#sig', 'a#sig', 'm%d' % n, 'A' * 1000, 60, now)
        self.assertEqual([m['seq'] for m in boxes.page('b#sig', now, 0, max_rows=4, max_bytes=10_000)], [1, 2, 3, 4])
        self.assertEqual([m['seq'] for m in boxes.page('b#sig', now, 4, max_rows=8, max_bytes=2500)], [5, 6, 7])
        self.assertEqual([m['seq'] for m in boxes.page('b#sig', now, 9, max_rows=8, max_bytes=1)], [10])
        boxes.close()

    def test_a_full_mailbox_is_delivered_page_by_page_one_flush_per_machine(self):
        self.start()
        laptop, _ = self.open(self.estate.laptop)
        for n in range(25):
            laptop.send(self.estate.desk.sig_kid, {'n': n})
        active, peak, lock = [0], [0], threading.Lock()
        real = self.server.mailboxes.page

        def page(*a, **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            try:
                return real(*a, **kw)
            finally:
                with lock:
                    active[0] -= 1
        self.server.mailboxes.page = page
        with mock.patch.object(rs, 'FLUSH_PAGE_ROWS', 4):
            _, one = self.open(self.estate.desk)
            _, two = self.open(self.estate.desk)
            # An acknowledged message leaves the mailbox, so each arrives once at least, over the two connections.
            self.assertTrue(eventually(lambda: {m['payload']['n'] for m in one + two} == set(range(25))))
        for inbox in (one, two):
            numbers = [m['payload']['n'] for m in inbox]
            self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(peak[0], 1)
        self.assertGreaterEqual(sum(1 for r in self.logs if r['event'] == 'relay.session.admitted'), 3)

    def test_a_recipient_that_stops_reading_blocks_no_other_session(self):
        self.start()
        stuck, _ = self.raw_admitted(self.estate.laptop)
        stuck._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        desk, _ = self.raw_admitted(self.estate.desk)
        body = 'A' * 1_000_000
        for n in range(6):
            started = time.monotonic()
            desk.send_text(json.dumps({'v': 1, 'id': 'i%d' % n, 'type': 'send', 'to': self.estate.laptop.sig_kid,
                                       'msgId': 'big%d' % n, 'body': body, 'ttl': 600}))
            self.assertEqual(self.next_frame(desk)['type'], 'accepted')
            self.assertLess(time.monotonic() - started, PROMPT)
        started = time.monotonic()
        other, _ = self.raw_admitted(self.estate.desk)
        self.assertLess(time.monotonic() - started, PROMPT)
        started = time.monotonic()
        self.server.stop()
        self.assertLess(time.monotonic() - started, PROMPT)
        for ws in (stuck, desk, other):
            ws.close()

    # ---- The spool ---------------------------------------------------------------------------------------------

    def test_mail_is_sealed_at_rest_and_survives_a_restart(self):
        spool = self.folder / 'spool.sqlite'
        key = b'\x07' * 32
        self.start(spool=spool, key=key)
        captured = []
        real = self.server.mailboxes.append
        self.server.mailboxes.append = lambda *a: captured.append(a[3]) or real(*a)
        laptop, _ = self.open(self.estate.laptop)
        laptop.send(self.estate.desk.sig_kid, {'marker': 'readable-only-by-the-desk'})
        self.server.stop()
        self.server.mailboxes.close()
        stored = spool.read_bytes()
        self.assertNotIn(captured[0][:48].encode(), stored)
        self.assertNotIn(b'readable-only-by-the-desk', stored)
        if os.name != 'nt':                                 # owner-only by mode on POSIX; by ACL on Windows
            self.assertEqual(spool.stat().st_mode & 0o077, 0)
        # Another key opens nothing: the row is dropped and logged, never delivered.
        other = rs.Mailboxes(spool, b'\x08' * 32, log=self.log)
        self.assertEqual(other.pending(self.estate.desk.sig_kid, int(time.time() * 1000)), [])
        other.close()
        self.assertTrue(any(r['event'] == 'relay.spool.unreadable' for r in self.logs))

    def test_a_restart_delivers_what_was_queued(self):
        spool = self.folder / 'spool.sqlite'
        self.start(spool=spool, key=b'\x09' * 32)
        laptop, _ = self.open(self.estate.laptop)
        laptop.send(self.estate.desk.sig_kid, {'kept': True})
        laptop.close()
        self.server.stop()
        self.server.mailboxes.close()
        self.start(spool=spool, key=b'\x09' * 32)
        _, inbox = self.open(self.estate.desk)
        self.assertTrue(eventually(lambda: len(inbox) == 1))
        self.assertEqual(inbox[0]['payload'], {'kept': True})

    def test_the_spool_key_is_made_once_and_owner_only(self):
        folder = self.folder / 'relay'
        private_io.parent(folder)
        first = rs.load_or_create_spool_key(folder)
        self.assertEqual(rs.load_or_create_spool_key(folder), first)
        self.assertEqual(len(first), 32)
        if os.name != 'nt':
            self.assertEqual((folder / 'spool.key').stat().st_mode & 0o077, 0)


def _answers(port: int) -> bool:
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/healthz' % port, timeout=1) as answer:
            return answer.status == 200
    except OSError:
        return False


# ---- `full relay` ---------------------------------------------------------------------------------------------------

@needs_crypto
class Cli(unittest.TestCase):
    def setUp(self):
        import relay_cli
        self.cli = relay_cli
        self.cli.CONFIG.unlink(missing_ok=True)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_settings_it_refuses(self):
        checked = self.cli.checked
        self.assertEqual(checked(None, None, None), {'origin': 'http://127.0.0.1:47317', 'host': '127.0.0.1',
                                                     'port': 47317, 'trusted_proxy': False})
        self.assertTrue(checked('https://relay.example', None, 9000, True)['trusted_proxy'])
        with self.assertRaises(self.cli.RelayConfigError):
            checked(None, None, None, True)                 # an http origin has no terminator in front
        self.assertEqual(checked('https://relay.example', '0.0.0.0', 9000)['host'], '0.0.0.0')
        for origin, host, port in (('http://127.0.0.1:9000', '0.0.0.0', 9000),
                                   ('http://127.0.0.1:9001', None, 9000),
                                   ('http://localhost:9000', None, 9000),
                                   ('https://relay.example/path', None, 9000),
                                   ('https://relay.example', 'relay.example', 9000),
                                   (None, None, 70000)):
            with self.subTest(origin=origin, host=host, port=port), self.assertRaises(self.cli.RelayConfigError):
                checked(origin, host, port)
        code, _, err = self.run_cli('enable', '--origin', 'http://relay.example')
        self.assertEqual(code, 2)
        self.assertIn('--origin', err)

    def test_enable_status_disable(self):
        code, out, _ = self.run_cli('status', '--json')
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc['enabled'], doc['admitting'], doc['refusal']), (False, False, 'no-record'))
        port = free_port()
        code, out, _ = self.run_cli('enable', '--origin', 'https://relay.example', '--port', str(port))
        self.assertEqual(code, 0)
        self.assertIn('serverd.py', out)
        self.assertEqual(json.loads(self.cli.CONFIG.read_text())['port'], port)
        self.assertEqual(self.run_cli('enable', '--origin', 'https://relay.example', '--port', str(port),
                                      '--trusted-proxy')[0], 0)
        self.assertIs(json.loads(self.cli.CONFIG.read_text())['trusted_proxy'], True)
        code, out, _ = self.run_cli('status', '--json')
        self.assertEqual(code, 1)                           # enabled, but nothing answers and nobody is admitted
        self.assertEqual((json.loads(out)['answers'], json.loads(out)['enabled']), (False, True))
        code, out, _ = self.run_cli('disable')
        self.assertEqual(code, 0)
        self.assertIsNone(self.cli.start_configured())

    @unittest.skipIf(os.name == 'nt', 'a SIGTERM is how the person or a supervisor stops it on POSIX')
    def test_serve_runs_in_the_foreground_until_sigterm(self):
        import subprocess
        port = free_port()
        node = Path(tmpdir.mkdtemp(prefix='obs-relay-serve-')).resolve()
        process = subprocess.Popen([sys.executable, str(ROOT / 'tools' / 'relay_cli.py'), 'serve', '--memory',
                                    '--port', str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=dict(os.environ, OBSERVATORY_NODE=str(node)))
        try:
            self.assertTrue(eventually(lambda: _answers(port), timeout=20))
        finally:
            process.terminate()
            out, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err.decode()[-2000:])
        events = [json.loads(line)['event'] for line in err.decode().splitlines() if line.startswith('{')]
        self.assertEqual(events[0], 'relay.listening')
        self.assertIn('relay.degraded', events)            # this test workspace's node applied no record
        self.assertEqual(events[-1], 'relay.stopped')
        self.assertEqual(list(node.iterdir()), [])          # --memory: nothing on disk

    def test_the_node_server_starts_the_enabled_relay(self):
        port = free_port()
        self.assertIsNone(self.cli.start_configured())
        self.run_cli('enable', '--origin', 'http://127.0.0.1:%d' % port, '--port', str(port))
        logs = []
        server = self.cli.start_configured(log=logs.append)
        try:
            self.assertEqual(server.port, port)
            with urllib.request.urlopen('http://127.0.0.1:%d/healthz' % port, timeout=WAIT) as answer:
                self.assertTrue(json.loads(answer.read())['ok'])
            self.assertIn('admits nobody', self.cli.health(server))
            code, out, _ = self.run_cli('status')
            self.assertEqual(code, 1)
            self.assertIn('answers; admits nobody (no-record)', out)
            if os.name != 'nt':
                self.assertEqual((Path(os.environ['OBSERVATORY_NODE']) / 'relay' / 'spool.key').stat().st_mode
                                 & 0o077, 0)
        finally:
            server.stop()
            server.mailboxes.close()
            self.run_cli('disable')


if __name__ == '__main__':
    # The slowest cases go into the suite's log (Python 3.12 and later), so a slow runner shows where its time went.
    unittest.main(**({'durations': 8} if sys.version_info >= (3, 12) else {}))
