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
WAIT = 5.0
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
            'bad signature': (dict(frame, sig=frame['sig'][:-2] + 'AA'), good, 'urn:fabric:estate:example', False),
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
        patcher = mock.patch.object(rs, 'REVALIDATE_SECONDS', 0.1)
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

    def start(self, limits=None, spool=None, key=None):
        port = free_port()
        origin = 'http://127.0.0.1:%d' % port
        admission = rs.Admission(rs.NodeRecords(self.estate.node).read, log=self.log)
        mailboxes = rs.Mailboxes(spool, key or b'\x01' * 32, limits, log=self.log)
        self.server = rs.RelayServer(origin=origin, admission=admission, mailboxes=mailboxes, port=port,
                                     limits=limits, clock=lambda: time.time() + self.offset[0], log=self.log).start()
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
        ws._send_frame(OP_TEXT, b' ' * (relay.MAX_FRAME_BYTES + 1))
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

    def test_connections_beyond_the_limit_are_answered_503(self):
        self.start()
        self.server.max_connections = 1
        ws, _ = self.raw()
        with self.assertRaises(WebSocketError):
            self.raw()
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
                                                     'port': 47317})
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
            self.assertEqual((Path(os.environ['OBSERVATORY_NODE']) / 'relay' / 'spool.key').stat().st_mode & 0o077, 0)
        finally:
            server.stop()
            server.mailboxes.close()
            self.run_cli('disable')


if __name__ == '__main__':
    unittest.main()
