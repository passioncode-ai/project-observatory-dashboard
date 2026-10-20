#!/usr/bin/env python3
"""Organization enrollment (OBS-161; contract DEC-0031, DEC-0056, DEC-0082, DEC-0086).

The transport signature and the share-registration derivation run over the contract's own vectors
(`fabric/node-vectors/http-signature.json`, `share-registration.json`): the receiver's check answers every case as the
contract does, the signer reproduces the vectors' digests and signature inputs byte for byte, and the node's derivation
and sender gate answer every `derive`, `grantId` and `gate` case. The client then runs end to end against a fake
organization server that verifies what a real one verifies: the CSR's signature and key digest, the single-use token,
every request's `fabric-device` signature and nonce, and a `grant.usage`'s context against the certificate's org."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import http_signature as hs, share_registration as sr   # noqa: E402
from node.relay import strict_time                     # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
ORG = 'org_7kq2m4xw'
GRANT = 'urn:fabric:share:3f9c2a7e5b1d4c608e2f71a9d0b6c4e8'
OTHER_GRANT = 'urn:fabric:share:0123456789abcdef0123456789abcdef'
AGENT = 'urn:fabric:agent:example:inbox-digest'
T0 = 1792396800                                        # 2026-10-19T08:00:00Z (its UTC day began at T0 - 28800)


def pinned_key_admits(key: dict) -> bool:
    """Whether the vendored device-enrollment schema (the contract commit this engine is pinned to) admits a request's
    `key`: `storage` in its enum, and every const or forbidden property of the schema's top level and of its
    if/then/else branch (CT-65: `file` with `exportable: true` and no attestation; any other store `exportable: false`)."""
    doc = json.loads((VECTORS / 'device-enrollment.schema.json').read_text(encoding='utf-8'))
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('properties'), dict) and 'storage' in node['properties'] \
                    and 'exportable' in node['properties']:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(doc)
    (rules,) = found
    if key.get('storage') not in rules['properties']['storage']['enum'] or set(key) - set(rules['properties']):
        return False
    branches = [rules['properties']]
    if 'if' in rules:
        matched = key.get('storage') == rules['if']['properties']['storage']['const']
        branches.append(rules['then' if matched else 'else'].get('properties', {}))
    for properties in branches:
        for name, rule in properties.items():
            if rule is False and name in key:
                return False
            if isinstance(rule, dict) and 'const' in rule and name in key and key[name] != rule['const']:
                return False
    return True


def vectors(name: str) -> dict:
    return json.loads((VECTORS / (name + '.json')).read_text(encoding='utf-8'))


@needs_crypto
class TransportVectors(unittest.TestCase):
    """DEC-0056 against `http-signature.json`."""

    def setUp(self):
        self.data = vectors('http-signature')
        self.certs = {c['serial']: c for c in self.data['certificates']}

    def verdict(self, case):
        held = {(s['serial'], s['nonce']) for s in case.get('seen', [])}
        result = hs.verify_request(case['request'], public_base_url=self.data['publicBaseUrl'],
                                   certificate=self.certs.get, at_ms=strict_time(self.data['at']),
                                   seen=lambda serial, nonce: (serial, nonce) in held,
                                   client_certificate_serial=case.get('clientCertificateSerial'))
        return 'accepted' if result['ok'] else result['code']

    def test_every_case_answers_as_the_contract(self):
        self.assertEqual(len(self.data['cases']), 54)
        for case in self.data['cases']:
            with self.subTest(case['name']):
                self.assertEqual(self.verdict(case), case['expect'])

    def test_the_signature_base_is_the_vectors_byte_for_byte(self):
        built = 0
        for case in self.data['cases']:
            parsed = hs._parsed(case['request']['headers'])
            if parsed is None or case.get('signatureBase') is None or case['expect'] != 'accepted' \
                    or 'Host' in case['name']:
                continue
            body = case['request']['body'].encode('utf-8')
            base = hs.signature_base(case['request']['method'],
                                     hs.target_uri(self.data['publicBaseUrl'], case['request']['target']),
                                     hs.content_digest(body), parsed['params'])
            self.assertEqual(base, case['signatureBase'], case['name'])
            built += 1
        self.assertGreaterEqual(built, 2)

    def test_the_signer_writes_the_vectors_fields(self):
        """Given a vector's created and nonce, the signer writes its Content-Digest and Signature-Input exactly."""
        from node.device_keys import DeviceKeys
        keys = DeviceKeys.generate()
        case = self.data['cases'][0]
        parsed = hs._parsed(case['request']['headers'])
        headers = hs.sign_request(keys.sign, method='POST', origin=self.data['publicBaseUrl'],
                                  target=case['request']['target'], body=case['request']['body'].encode('utf-8'),
                                  serial=parsed['keyid'], created=parsed['created'], nonce=parsed['nonce'])
        self.assertEqual(headers['Content-Digest'], case['request']['headers']['content-digest'])
        self.assertEqual(headers['Signature-Input'], case['request']['headers']['signature-input'])
        # And it verifies under this machine's key, as the receiver checks it.
        cert = dict(self.certs[parsed['keyid']], key={'alg': 'es256',
                                                     'publicKey': base64.b64encode(keys.sig_public).decode()})
        request = {'method': 'POST', 'target': case['request']['target'], 'body': case['request']['body'],
                   'headers': {k.lower(): v for k, v in headers.items()}}
        result = hs.verify_request(request, public_base_url=self.data['publicBaseUrl'],
                                   certificate={parsed['keyid']: cert}.get, at_ms=strict_time(self.data['at']))
        self.assertTrue(result['ok'], result)

    def test_rfc_9530_digest(self):
        rfc = self.data['rfc9530']
        self.assertEqual(hs.content_digest(rfc['body'].encode('utf-8')), rfc['contentDigest'])

    def test_a_retry_is_signed_afresh(self):
        from node.device_keys import DeviceKeys
        keys = DeviceKeys.generate()
        one = hs.sign_request(keys.sign, method='POST', origin='https://org.example', target='/x', body=b'{}',
                              serial='0a0b', created=1)
        two = hs.sign_request(keys.sign, method='POST', origin='https://org.example', target='/x', body=b'{}',
                              serial='0a0b', created=1)
        self.assertNotEqual(one['Signature-Input'], two['Signature-Input'])

    def test_public_origin(self):
        self.assertEqual(hs.public_origin('https://ORG.example:443/'), 'https://org.example')
        for bad in ('http://org.example', 'https://org.example/path', 'https://u@org.example', 'org.example', None):
            self.assertIsNone(hs.public_origin(bad), bad)


class ShareRegistrationVectors(unittest.TestCase):
    """DEC-0086 against `share-registration.json`: the node's half."""

    def setUp(self):
        self.data = vectors('share-registration')

    def test_grant_ids(self):
        for case in self.data['grantId']:
            with self.subTest(case['name']):
                self.assertEqual(sr.is_share_grant_id(case['value']), case['ok'])

    def test_derive(self):
        self.assertEqual(len(self.data['derive']), 51)
        for case in self.data['derive']:
            with self.subTest(case['name']):
                self.assertEqual(sr.derive(case['record'], case['entries'], case['orgId'],
                                           case['previouslyRegistered']), case['expected'])

    def test_gate(self):
        for case in self.data['gate']:
            with self.subTest(case['name']):
                self.assertEqual(sr.gate(case.get('answer')), case['expected'])

    def test_digest_is_the_receivers(self):
        checked = 0
        for case in self.data['intake']:
            receipt = (case['expected'] or {}).get('receipt') if isinstance(case['expected'], dict) else None
            if receipt and receipt.get('state') == 'registered':
                self.assertEqual(sr.digest(case['registration']), receipt['digest'], case['name'])
                checked += 1
        self.assertGreater(checked, 0)


# ---- A fake organization server -------------------------------------------------------------------------------

class FakeServer:
    """What a receiver verifies, and nothing it need not: enough to hold the client to the wire."""

    def __init__(self, clock, protocols=('fabric-device/0.1', 'fabric-activity/0.1')):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        self.clock, self.protocols = clock, list(protocols)
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        self.ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test-ca')])
        self.tokens = {'et_1': 'S' * 32, 'et_2': 'T' * 32}
        self.used, self.certs, self.nonces, self.events, self.registrations = set(), {}, set(), {}, {}
        self.batches, self.sequences, self.requests = [], [], []
        self.epoch, self.fail_batches, self.log, self.share_answer = 0, 0, [], None
        self.refuse_storage, self.skew, self.org, self.strict_schema = (), 0, ORG, False
        self.checkin_protocols, self.batch_answer, self.lose_rotation, self.rotations = None, None, False, {}
        self.pause = None                                  # (path, started Event, release Event): hold one request
        self._hashes = hashes

    def __call__(self, url, headers, body, timeout):
        path = url[len('https://org.example'):]
        self.log.append(path)
        self.requests.append((path, json.loads(body)))
        handler = {'/v1/devices/enroll': self.enroll, '/v1/devices/check-in': self.check_in,
                   '/v1/devices/rotate': self.rotate, '/v1/activity/batches': self.batch,
                   '/v1/devices/share-registrations': self.share}[path]
        if self.pause and self.pause[0] == path:
            pause, self.pause = self.pause, None
            pause[1].set()
            assert pause[2].wait(10)
        status, doc = handler(path, {k.lower(): v for k, v in headers.items()}, body)
        if path == '/v1/devices/rotate' and self.lose_rotation:
            self.lose_rotation = False
            raise OSError('the answer was lost on the way back')
        return status, json.dumps(doc).encode()

    def _issue(self, csr, device):
        from cryptography import x509
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        serial = os.urandom(8).hex()
        cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.ca_name)
                .public_key(csr.public_key()).serial_number(int(serial, 16)).not_valid_before(now)
                .not_valid_after(now + dt.timedelta(days=7)).sign(self.ca_key, self._hashes.SHA256()))
        from cryptography.hazmat.primitives import serialization
        point = csr.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        fmt = lambda t: t.strftime('%Y-%m-%dT%H:%M:%SZ')
        self.certs[serial] = {'serial': serial, 'notBefore': fmt(now), 'notAfter': fmt(now + dt.timedelta(days=7)),
                              'key': {'alg': 'es256', 'publicKey': base64.b64encode(point).decode()},
                              'device': device, 'org': self.org, 'epoch': self.epoch}
        return {'der': base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode(), 'serial': serial,
                'notBefore': self.certs[serial]['notBefore'], 'notAfter': self.certs[serial]['notAfter'],
                'binding': {'org': {'id': self.org}, 'device': {'id': device}}, 'issuer': 'test-ca'}

    def _csr(self, text):
        from cryptography import x509
        csr = x509.load_der_x509_csr(base64.b64decode(text))
        assert csr.is_signature_valid
        return csr

    def enroll(self, path, headers, body):
        doc = json.loads(body)
        auth = headers.get('authorization', '')
        token_id, _, secret = auth[len('EnrollmentToken '):].partition('.')
        if not auth.startswith('EnrollmentToken ') or self.tokens.get(token_id) != secret or token_id in self.used \
                or doc['auth'] != {'method': 'enrollment-token', 'tokenId': token_id}:
            return 401, {'error': {'code': 'token-invalid', 'message': 'no'}}
        assert secret not in body.decode()                # the proof travels in Authorization only
        csr = self._csr(doc['csr'])
        from cryptography.hazmat.primitives import serialization
        spki = csr.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if 'sha256:' + hashlib.sha256(spki).hexdigest() != doc['key']['publicKeySha256']:
            return 422, {'error': {'code': 'key-mismatch'}}
        assert [k['kid'].split('#')[1] for k in doc['deviceKeys']] == ['sig', 'kem']
        assert (doc['key']['storage'], doc['key']['exportable']) in (('file', True), ('platform-keystore', False))
        if self.strict_schema and not pinned_key_admits(doc['key']):
            return 422, {'error': {'code': 'schema-invalid', 'pointer': '/key/storage', 'message': 'not in enum'}}
        if doc['key']['storage'] in self.refuse_storage:
            return 403, {'error': {'code': 'key-storage-refused', 'message': 'policy'}}
        self.used.add(token_id)
        self.org = doc['org']['id']
        self.epoch += 1
        return 200, {'protocol': 'fabric-device/0.1', 'kind': 'enrollment-response', 'protocols': self.protocols,
                     'certificate': self._issue(csr, doc['device']['id']), 'streamEpoch': self.epoch,
                     'policyRootKey': {'keyId': 'root', 'alg': 'ed25519', 'publicKey': 'x'},
                     'checkIn': {'intervalSeconds': 300}, 'collectionStart': '2026-10-10T00:00:00Z'}

    def _verify(self, path, headers, body):
        result = hs.verify_request({'method': 'POST', 'target': path, 'headers': headers, 'body': body},
                                   public_base_url='https://org.example', certificate=self.certs.get,
                                   at_ms=int((self.clock() + self.skew) * 1000),
                                   seen=lambda s, n: (s, n) in self.nonces)
        if result['ok']:
            self.nonces.add((result['serial'], result['nonce']))
            replaced = self.certs[result['serial']].pop('replaces', None)
            if replaced:                                   # CT-65: the old certificate ends when the new one is used
                self.certs[replaced]['revoked'] = True
        return result

    def check_in(self, path, headers, body):
        v = self._verify(path, headers, body)
        if not v['ok']:
            return 401, {'error': {'code': v['code']}}
        cert = self.certs[v['serial']]
        doc = json.loads(body)
        assert doc['device']['id'] == cert['device'] and doc['certificate']['serial'] == v['serial']
        self.sequences.append(doc['sequenceNum'])
        out = {'protocol': 'fabric-device/0.1', 'kind': 'check-in-response', 'device': doc['device'],
               'at': dt.datetime.fromtimestamp(self.clock() + self.skew, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
               'nextCheckInSeconds': 300, 'flags': []}
        if self.checkin_protocols is not None:
            out['protocols'] = self.checkin_protocols
        if self.clock() >= strict_time(cert['notBefore']) / 1000 + 7 * 86400 * 2 / 3:
            out['certificate'] = {'action': 'rotate'}
        return 200, out

    def rotate(self, path, headers, body):
        v = self._verify(path, headers, body)
        if not v['ok']:
            return 401, {'error': {'code': v['code']}}
        doc = json.loads(body)
        old = self.certs[v['serial']]
        if (v['serial'], doc['csr']) in self.rotations:     # CT-65: the same request, the same certificate
            return 200, self.rotations[(v['serial'], doc['csr'])]
        csr = self._csr(doc['csr'])
        answer = {'protocol': 'fabric-device/0.1', 'kind': 'rotation-response',
                  'certificate': self._issue(csr, old['device']), 'streamEpoch': old['epoch']}
        self.certs[answer['certificate']['serial']]['replaces'] = v['serial']
        self.rotations[(v['serial'], doc['csr'])] = answer
        return 200, answer

    def batch(self, path, headers, body):
        v = self._verify(path, headers, body)
        if not v['ok']:
            return 401, {'error': {'code': v['code']}}
        if self.fail_batches:
            self.fail_batches -= 1
            return 503, {'error': {'code': 'unavailable'}}
        doc = json.loads(body)
        self.batches.append((doc['batchId'], [e['seq'] for e in doc['events']], [e['kind'] for e in doc['events']]))
        if self.batch_answer:
            answer = self.batch_answer(doc)
            if answer:
                return answer
        cert = self.certs[v['serial']]
        accepted = duplicates = 0
        rejected, top = [], {}
        for event in doc['events']:
            stream = event['collector']['epoch']
            if event['kind'] == 'grant.usage' and event['data']['context'] != 'org:' + cert['org']:
                rejected.append({'eventId': event['eventId'], 'reason': 'attribution'})
            elif event['eventId'] in self.events:
                duplicates += 1
            else:
                self.events[event['eventId']] = event
                accepted += 1
            top[stream] = max(top.get(stream, 0), event['seq'])
        return 200, {'protocol': 'fabric-activity/0.1', 'device': doc['device'], 'batchId': doc.get('batchId'),
                     'received': '2026-10-19T00:00:00Z', 'accepted': accepted, 'duplicates': duplicates,
                     'rejected': rejected, 'acks': [{'collector': {'id': 'observatory-node', 'epoch': e}, 'seq': s}
                                                    for e, s in top.items()]}

    def share(self, path, headers, body):
        v = self._verify(path, headers, body)
        if not v['ok']:
            return 401, {'error': {'code': v['code']}}
        doc = json.loads(body)
        if self.share_answer:
            return self.share_answer
        held = self.registrations.get(doc['grantId'])
        digest = sr.digest(doc)
        state = 'unchanged' if held == digest else ('withdrawn' if doc['state'] == 'withdrawn' else 'registered')
        self.registrations[doc['grantId']] = digest
        return 200, {'type': 'share-registration-receipt/1', 'grantId': doc['grantId'], 'state': state,
                     'digest': digest}


def share_record(grants=((GRANT, 'org:' + ORG),), revision=7, digest='sha256:' + 'a' * 64):
    return {'schema': 'estate-record/1', 'estate': 'estate-5d0f1a', 'revision': revision, 'digest': digest,
            'issuedAt': '2026-10-14T09:00:00Z',
            'machines': [{'id': 'm-1', 'packages': [{'agentRef': AGENT, 'context': ctx, 'grant': gid}
                                                    for gid, ctx in grants]}],
            'shareGrants': [{'kind': 'standing-grant', 'meta': {'id': gid, 'revision': 3},
                             'startsAt': '2026-10-12T08:00:00Z', 'expiresAt': '2026-10-26T08:00:00Z',
                             'principalKind': 'external', 'context': ctx} for gid, ctx in grants],
            'revocations': []}


@needs_crypto
class EnrollmentEndToEnd(unittest.TestCase):

    def setUp(self):
        import private_io
        from node.org import Enrollment
        self.now = [float(T0 + 3600)]
        self.clock = lambda: self.now[0]
        self.server = FakeServer(self.clock)
        self.node = Path(tmpdir.mkdtemp()).resolve() / 'node'
        self.meter = self.node.parent / 'meter.sqlite'
        conn = sqlite3.connect(self.meter)
        conn.execute('CREATE TABLE reservations (receipt TEXT PRIMARY KEY, grant_ref TEXT, grant_id TEXT, project TEXT,'
                     ' task_key TEXT, key_ref TEXT, token_jti TEXT, amount REAL, created_at INTEGER, expires_at INTEGER,'
                     ' settled_usd REAL, settled_at INTEGER, purpose TEXT)')
        rows = [('r1', GRANT, T0 - 86400 + 10, 0.25), ('r2', GRANT, T0 - 86400 + 20, 0.5),
                ('r3', OTHER_GRANT, T0 - 86400 + 30, 1.0), ('r4', GRANT, T0 - 20 * 86400, 2.0),
                ('r5', GRANT, T0 + 60, 9.0)]
        for receipt, grant, at, usd in rows:
            conn.execute('INSERT INTO reservations(receipt, grant_id, created_at, expires_at, settled_usd, amount) '
                         'VALUES (?, ?, ?, ?, ?, ?)', (receipt, grant, at, at + 600, usd, usd))
        conn.commit()
        conn.close()
        self.make = lambda: Enrollment(self.node, ORG, transport=self.server, clock=self.clock, meter_db=self.meter)
        self.private_io = private_io

    def open_gate(self, grant, gate='send'):
        """A registration answered as `gate`, written as register_shares would."""
        with self.make()._db() as conn:
            conn.execute("INSERT INTO registrations(grant_id, doc, digest, gate, active_registered) VALUES(?, ?, ?, ?, 1)"
                         " ON CONFLICT(grant_id) DO UPDATE SET gate = excluded.gate",
                         (grant, json.dumps({'state': 'active'}), 'sha256:x', gate))

    def enroll(self, token='et_1.' + 'S' * 32):
        return self.make().enroll('https://org.example', token)

    def apply(self, record):
        folder = self.node / 'records'
        self.private_io.write(folder / 'estate-record.json', json.dumps(record))

    def test_enroll_check_in_and_no_secret_at_rest(self):
        status = self.enroll()
        self.assertTrue(status['enrolled'])
        self.assertEqual(status['epoch'], 1)
        self.assertEqual(status['protocols'], ['fabric-device/0.1', 'fabric-activity/0.1'])
        self.assertIn('org:share-registration-not-listed', status['degraded'])
        result = self.make().check_in()
        self.assertTrue(result['ok'], result)
        for path in (p for p in (self.node / 'orgs' / ORG).rglob('*') if p.is_file() and p.suffix != '.pem'):
            self.assertNotIn('S' * 32, path.read_bytes().decode('utf-8', 'replace'), path.name)
        if os.name == 'posix':                          # Windows has no owner-only mode bits
            self.assertEqual(oct((self.node / 'orgs' / ORG / 'enrollment.json').stat().st_mode & 0o777), '0o600')

    def test_a_used_token_is_refused_and_leaves_nothing(self):
        from node.org import OrgError
        self.enroll()
        other = FakeServer(self.clock)
        other.used.add('et_1')
        from node.org import Enrollment
        with self.assertRaises(OrgError) as raised:
            Enrollment(self.node, 'org_other', transport=other, clock=self.clock).enroll('https://org.example',
                                                                                         'et_1.' + 'S' * 32)
        self.assertEqual(raised.exception.code, 'enroll-refused')
        self.assertFalse((self.node / 'orgs' / 'org_other' / 'enrollment.json').exists())

    def test_a_captured_check_in_cannot_be_replayed(self):
        self.enroll()
        captured = []
        original = self.server.__call__

        def recording(url, headers, body, timeout):
            captured.append((url, headers, body))
            return original(url, headers, body, timeout)
        enrollment = self.make()
        enrollment.transport = recording
        self.assertTrue(enrollment.check_in()['ok'])
        status, raw = self.server(*captured[-1], 30)
        self.assertEqual((status, json.loads(raw)['error']['code']), (401, 'signature-replayed'))

    def test_rotation_at_two_thirds_keeps_the_key(self):
        first = self.enroll()['certificate']['serial']
        self.now[0] += 5 * 86400
        report = self.make().tick()
        self.assertEqual(report['rotate'], {'result': 'done'})
        after = self.make().describe()
        self.assertNotEqual(after['certificate']['serial'], first)
        self.assertEqual(self.server.certs[after['certificate']['serial']]['key'],
                         self.server.certs[first]['key'])
        self.assertTrue(self.make().check_in()['ok'])

    def test_nothing_of_shares_or_grant_usage_before_the_protocol_is_listed(self):
        self.enroll()
        self.apply(share_record())
        report = self.make().tick()
        self.assertEqual(report['shares'], {'sent': 0, 'held': 'protocol-not-listed'})
        self.assertEqual(report['grantUsage']['enqueued'], 0)
        self.assertNotIn('/v1/devices/share-registrations', self.server.log)
        self.assertEqual(self.server.events, {})

    def test_registration_then_grant_usage_for_this_org_only(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record(grants=((GRANT, 'org:' + ORG), (OTHER_GRANT, 'org:another_org'))))
        report = self.make().tick()
        self.assertEqual(report['shares']['answers'], {GRANT: 'send'})
        self.assertEqual(report['grantUsage'], {'enqueued': 1, 'heldGrants': 0, 'waitingDays': 0})
        self.assertEqual(report['outbox']['accepted'], 1)
        (event,) = self.server.events.values()
        self.assertEqual(event['data'], {'day': '2026-10-18', 'grantId': GRANT, 'context': 'org:' + ORG,
                                         'calls': 2, 'unpricedCalls': 0, 'costUsd': 0.75})
        self.assertEqual((event['occurred'], event['monotonicNs'], event['seq']), ('2026-10-18T00:00:00Z', '0', 1))
        # Idempotent: a second tick sends no event again and no registration within the week.
        again = self.make().tick()
        self.assertEqual((again['shares']['sent'], again['grantUsage']['enqueued']), (0, 0))
        self.assertEqual(len(self.server.events), 1)
        # A week later the registration is sent again and answered unchanged.
        self.now[0] += 7 * 86400 + 1
        before = self.server.log.count('/v1/devices/share-registrations')
        self.enroll('et_2.' + 'T' * 32)                  # the certificate would have expired: re-enroll
        # The enrollment itself sends the registrations again (on change, not at the next tick).
        self.assertEqual(self.server.log.count('/v1/devices/share-registrations'), before + 1)
        self.assertEqual(self.make().register_shares()['sent'], 0)

    def test_a_typed_refusal_holds_until_the_record_changes(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self.server.share_answer = (409, {'error': {'code': 'agent-mismatch', 'message': 'no'}})
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'hold-degraded'})
        self.assertEqual(self.make().register_shares()['sent'], 0)
        self.assertEqual(self.make().collect_grant_usage(), {'enqueued': 0, 'heldGrants': 1, 'waitingDays': 0})
        self.assertIn('org:share-registration-refused', self.make().describe()['degraded'])
        self.server.share_answer = None
        self.apply(share_record(revision=8, digest='sha256:' + 'b' * 64))
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'send'})

    def test_the_outbox_retries_under_one_batch_id_and_survives_a_restart(self):
        self.enroll()
        for day in ('2026-10-16', '2026-10-17'):
            self.make().enqueue('meter.call', {'day': day}, occurred=day + 'T00:00:00Z', key=day)
        self.assertIsNone(self.make().enqueue('meter.call', {}, occurred='2026-10-16T00:00:00Z', key='2026-10-16'))
        self.server.fail_batches = 1
        first = self.make().flush()
        self.assertEqual(first['held'], 'unavailable')
        self.assertEqual(self.make().describe()['outbox']['pending'], 2)
        self.assertIn('org:outbox-retrying', self.make().describe()['degraded'])
        self.assertEqual(self.make().flush()['held'], 'backoff')
        self.now[0] += 3600
        second = self.make().flush()
        self.assertEqual((second['batches'], second['accepted']), (1, 2))
        self.assertEqual(self.make().describe()['outbox']['pending'], 0)

    def test_a_foreign_context_is_consumed_as_rejected(self):
        self.enroll()
        self.open_gate(GRANT)
        self.make().enqueue('grant.usage', {'day': '2026-10-16', 'grantId': GRANT, 'context': 'org:someone_else',
                                            'calls': 1, 'unpricedCalls': 0, 'costUsd': 1.0},
                            occurred='2026-10-16T00:00:00Z')
        self.assertEqual(self.make().flush()['rejected'], 1)
        self.assertEqual(self.make().describe()['outbox']['rejected'], 1)

    def test_leave_removes_only_the_enrollment(self):
        self.enroll()
        self.assertEqual(self.make().leave(), {'org': ORG, 'left': True})
        self.assertFalse((self.node / 'orgs' / ORG).exists())   # its own keys went with it
        self.assertEqual(self.make().tick(), {'org': ORG, 'result': 'not-enrolled'})
        self.assertFalse((self.node / 'orgs' / ORG).exists())   # and nothing recreates it

    def test_an_unverified_revision_is_never_applied(self):
        from node import org
        with self.assertRaises(org.OrgError) as raised:
            org.apply_record(self.node, share_record(), T0 * 1000, authorities=[{'credentialId': 'x'}])
        self.assertEqual(raised.exception.code, 'record-refused')
        self.assertEqual(org.applied_records(self.node), (None, []))

    def test_an_expired_certificate_asks_for_enrollment(self):
        self.enroll()
        self.now[0] += 8 * 86400
        self.assertEqual(self.make().tick()['result'], 'needs-enrollment')
        self.assertIn('org:needs-enrollment', self.make().describe()['degraded'])


    # -- fix cycle 1 (adversarial round 1 on PR #308) ----------------------------------------------------------
    def _thread(self, fn, out):
        import threading

        def body():
            try:
                out.append(fn())
            except Exception as error:                 # noqa: BLE001 — surfaced by the assertion below
                out.append(error)
        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        return thread

    def test_a_check_in_waits_for_a_rotation_in_flight(self):
        import threading
        self.enroll()
        old = self.make().state()['certificate']['serial']
        started, release = threading.Event(), threading.Event()
        self.server.pause = ('/v1/devices/rotate', started, release)
        rotated, checked = [], []
        rotation = self._thread(lambda: self.make().rotate(), rotated)
        self.assertTrue(started.wait(10))
        check_in = self._thread(lambda: self.make().check_in(), checked)
        check_in.join(0.5)
        self.assertTrue(check_in.is_alive())           # it waits for the lock, not for the network
        release.set()
        rotation.join(10)
        check_in.join(10)
        self.assertTrue(rotated[0]['enrolled'], rotated)
        self.assertTrue(checked[0]['ok'], checked)
        state = self.make().state()
        self.assertNotEqual(state['certificate']['serial'], old)   # the rotation's certificate survived the check-in
        self.assertTrue(self.make().check_in()['ok'])               # and the next one is not certificate-revoked
        self.assertEqual(self.server.sequences, [1, 2])

    def test_two_check_ins_spend_two_sequence_numbers(self):
        import threading
        self.enroll()
        started, release = threading.Event(), threading.Event()
        self.server.pause = ('/v1/devices/check-in', started, release)
        results = []
        first = self._thread(lambda: self.make().check_in(), results)
        self.assertTrue(started.wait(10))
        second = self._thread(lambda: self.make().check_in(), results)
        second.join(0.5)
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual([r['ok'] for r in results], [True, True])
        self.assertEqual(self.server.sequences, [1, 2])
        self.assertEqual(self.make().state()['sequenceNum'], 2)

    def test_a_tick_in_flight_finishes_before_leave_and_nothing_comes_back(self):
        import threading
        self.enroll()
        started, release = threading.Event(), threading.Event()
        self.server.pause = ('/v1/devices/check-in', started, release)
        ticked, left = [], []
        tick = self._thread(lambda: self.make().tick(), ticked)
        self.assertTrue(started.wait(10))
        leave = self._thread(lambda: self.make().leave(), left)
        leave.join(0.5)
        self.assertTrue(leave.is_alive())
        release.set()
        tick.join(10)
        leave.join(10)
        self.assertEqual(left, [{'org': ORG, 'left': True}])
        self.assertEqual(self.make().tick(), {'org': ORG, 'result': 'not-enrolled'})
        self.assertFalse((self.node / 'orgs' / ORG).exists())

    def test_a_rotation_lost_on_the_way_back_is_sent_again_unchanged(self):
        from node.org import OrgError
        self.enroll()
        old = self.make().state()['certificate']['serial']
        self.server.lose_rotation = True
        with self.assertRaises(OrgError):
            self.make().rotate()
        self.assertEqual(self.make().state()['pendingRotation']['serial'], old)
        self.make().rotate()
        sent = [doc for path, doc in self.server.requests if path == '/v1/devices/rotate']
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])             # the same request, so the same certificate (CT-65)
        state = self.make().state()
        self.assertNotIn('pendingRotation', state)
        self.assertEqual(state['previousCertificate']['serial'], old)
        self.assertFalse(self.server.certs[old].get('revoked'))     # valid until the new serial is first presented
        self.assertTrue(self.make().check_in()['ok'])
        self.assertNotIn('previousCertificate', self.make().state())
        self.assertTrue(self.server.certs[old].get('revoked'))

    def test_the_check_in_replaces_the_protocol_list_and_registrations_follow(self):
        self.enroll()
        self.apply(share_record())
        self.server.checkin_protocols = ['fabric-device/0.1', 'fabric-activity/0.1', 'share-registration/1']
        self.assertTrue(self.make().check_in()['ok'])
        self.assertIn('share-registration/1', self.make().state()['protocols'])
        self.assertIn('/v1/devices/share-registrations', self.server.log)   # sent on the change, not at a tick
        self.server.checkin_protocols = ['fabric-device/0.1']
        self.make().check_in()
        self.assertEqual(self.make().state()['protocols'], ['fabric-device/0.1'])

    def test_a_file_held_key_is_declared_and_each_organization_has_its_own(self):
        from node.org import Enrollment, OrgError
        self.enroll()
        (request,) = [doc for path, doc in self.server.requests if path == '/v1/devices/enroll']
        self.assertEqual((request['key']['storage'], request['key']['exportable']), ('file', True))
        self.assertTrue((self.node / 'orgs' / ORG / 'keys' / 'device.json').exists())
        refusing = FakeServer(self.clock)
        refusing.refuse_storage = ('file',)
        with self.assertRaises(OrgError) as raised:
            Enrollment(self.node, 'org_policy', transport=refusing, clock=self.clock).enroll(
                'https://org.example', 'et_1.' + 'S' * 32)
        self.assertEqual(raised.exception.code, 'key-storage-refused')
        self.assertFalse((self.node / 'orgs' / 'org_policy' / 'enrollment.json').exists())
        other = FakeServer(self.clock)
        Enrollment(self.node, 'org_other', transport=other, clock=self.clock).enroll('https://org.example',
                                                                                     'et_1.' + 'S' * 32)
        (theirs,) = [doc for path, doc in other.requests if path == '/v1/devices/enroll']
        self.assertNotEqual(theirs['device']['id'], request['device']['id'])
        self.assertNotEqual(theirs['key']['publicKeySha256'], request['key']['publicKeySha256'])

    def test_a_day_is_summed_only_once_none_of_its_calls_can_still_settle(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        conn = sqlite3.connect(self.meter)
        conn.execute('INSERT INTO reservations(receipt, grant_id, created_at, expires_at, amount) VALUES (?, ?, ?, ?, ?)',
                     ('r6', GRANT, T0 - 86400 + 40, T0 + 7200, 1.0))   # r1's ended day, still open now
        conn.commit()
        report = self.make().tick()
        self.assertEqual(report['grantUsage'], {'enqueued': 0, 'heldGrants': 0, 'waitingDays': 1})
        conn.execute("UPDATE reservations SET settled_usd = 1.0, settled_at = ? WHERE receipt = 'r6'", (T0 + 3600,))
        conn.commit()
        conn.close()
        self.assertEqual(self.make().tick()['grantUsage']['enqueued'], 1)
        (event,) = self.server.events.values()
        self.assertEqual((event['data']['calls'], event['data']['costUsd']), (3, 1.75))

    def test_a_grant_registered_then_held_is_still_withdrawn(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'send'})
        with self.make()._db() as conn:                # the weekly resend is due, and the server is away
            conn.execute('UPDATE registrations SET sent_at = sent_at - 700000')
        self.server.share_answer = (503, {'error': {'code': 'unavailable', 'message': 'away'}})
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'hold'})
        self.apply(share_record(grants=(), revision=8, digest='sha256:' + 'c' * 64))   # the grant is gone
        self.now[0] += 7200                            # past the backoff
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'hold'})    # the withdrawal, held too
        self.server.share_answer = None
        self.now[0] += 7200
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'send'})    # retried, taken
        (last,) = [doc for path, doc in self.server.requests if path == '/v1/devices/share-registrations'][-1:]
        self.assertEqual(last['state'], 'withdrawn')
        self.assertEqual(self.make().register_shares()['sent'], 0)          # final once taken
        with self.make()._db() as conn:
            self.assertEqual(conn.execute('SELECT active_registered FROM registrations').fetchone()[0], 0)

    def test_a_batch_refused_for_what_it_is_is_not_retried(self):
        self.enroll()
        self.make().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        self.server.batch_answer = lambda doc: (422, {'error': {'code': 'batch-invalid', 'message': 'no'}})
        self.assertEqual(self.make().flush()['rejected'], 1)
        self.assertEqual(self.make().describe()['outbox']['pending'], 0)
        self.assertEqual(self.make().flush()['batches'], 0)
        self.assertEqual(len(self.server.batches), 1)
        self.make().enqueue('meter.call', {'n': 2}, occurred='2026-10-18T00:00:00Z')
        self.server.batch_answer = lambda doc: (409, {'error': {'code': 'activation-closed', 'message': 'later'}})
        self.assertEqual(self.make().flush()['held'], 'activation-closed')
        self.assertEqual(self.make().describe()['outbox']['pending'], 1)    # kept for a retry

    def test_a_413_halves_the_batch(self):
        self.enroll()
        for n in range(4):
            self.make().enqueue('meter.call', {'n': n}, occurred='2026-10-18T00:00:00Z')
        self.server.batch_answer = lambda doc: (413, {'error': {'code': 'payload-too-large'}}) \
            if len(doc['events']) > 2 else None
        result = self.make().flush()
        self.assertEqual((result['accepted'], result['rejected']), (4, 0))
        self.assertEqual([len(seqs) for _, seqs, _ in self.server.batches], [4, 2, 2])

    def test_an_answer_that_acknowledges_nothing_is_held_under_the_same_batch_id(self):
        self.enroll()
        self.make().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        self.server.batch_answer = lambda doc: (200, {'protocol': 'fabric-activity/0.1', 'acks': []})
        self.assertEqual(self.make().flush()['held'], 'not-acknowledged')
        self.assertEqual(len(self.server.batches), 1)                       # not twenty fresh batch ids
        enrollment = self.make()
        self.assertEqual(enrollment.check_in_document(enrollment.state())['health']['state'], 'degraded')
        self.server.batch_answer = None
        self.now[0] += 3600
        self.assertEqual(self.make().flush()['accepted'], 1)
        self.assertEqual(self.server.batches[0][0], self.server.batches[1][0])

    def test_a_burst_past_the_bound_ends_in_one_overflow(self):
        from unittest import mock
        from node import org
        self.enroll()
        with mock.patch.object(org, 'MAX_PENDING', 10), mock.patch.object(org, 'OVERFLOW_HEADROOM', 4):
            for n in range(20):
                self.make().enqueue('meter.call', {'n': n}, occurred='2026-10-18T00:00:00Z')
        with self.make()._db() as conn:
            overflows = [json.loads(r['body']) for r in conn.execute(
                "SELECT body FROM events WHERE kind = 'buffer.overflow'")]
            dropped = conn.execute("SELECT COUNT(*) FROM events WHERE state = 'dropped'").fetchone()[0]
        self.assertEqual(len(overflows), 1)
        ranges = overflows[0]['data']['dropped']['ranges']
        self.assertEqual(overflows[0]['data']['dropped']['events'], dropped)
        self.assertEqual(sum(r['toSeq'] - r['fromSeq'] + 1 for r in ranges), dropped)   # the overflow's own seq is not
        enrollment = self.make()
        collector = enrollment.check_in_document(enrollment.state())['collectors'][0]
        self.assertEqual((collector['dropped'], collector['healthy']), (ranges, False))
        self.make().flush()
        enrollment = self.make()
        self.assertEqual(enrollment.check_in_document(enrollment.state())['collectors'][0]['dropped'], [])

    def test_grant_usage_is_gated_again_at_send(self):
        self.enroll()
        self.open_gate(GRANT)
        key = 'grant.usage|2026-10-18|' + GRANT
        data = {'day': '2026-10-18', 'grantId': GRANT, 'context': 'org:' + ORG, 'calls': 1, 'unpricedCalls': 0,
                'costUsd': 1.0}
        self.make().enqueue('grant.usage', data, occurred='2026-10-18T00:00:00Z', key=key)
        self.open_gate(GRANT, 'hold')                  # the latest answer no longer lets it
        self.make().flush()
        self.assertEqual([kinds for _, _, kinds in self.server.batches], [['buffer.overflow']])
        self.assertEqual(self.make().describe()['outbox']['dropped'], 1)
        self.assertIsNotNone(self.make().enqueue('grant.usage', data, occurred='2026-10-18T00:00:00Z', key=key))

    def test_requests_are_signed_on_the_servers_clock(self):
        self.enroll()
        self.server.skew = 200
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().state()['skewSeconds'], 200)
        self.server.skew = 450                         # 450 s from this clock, 250 s from the measured one
        self.make().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        self.assertEqual(self.make().flush()['accepted'], 1)
        self.server.skew = 1200
        self.make().enqueue('meter.call', {'n': 2}, occurred='2026-10-18T00:00:00Z')
        self.assertEqual(self.make().flush()['held'], 'signature-stale')
        state = self.make().state()
        self.assertEqual((state['skewSeconds'], state['nextCheckInAt']), (0, 0))   # measure again at once


    # -- fix cycle 2 (confirming round on PR #308) -------------------------------------------------------------
    def test_a_forgotten_rotation_falls_back_and_the_enrollment_goes_on(self):
        self.enroll()
        first = self.make().state()['certificate']['serial']
        self.make().rotate()
        second = self.make().state()['certificate']['serial']
        del self.server.certs[second]                  # the server lost the rotated certificate
        self.assertTrue(self.make().check_in()['ok'])  # resent once under the previous one
        state = self.make().state()
        self.assertEqual((state['certificate']['serial'], state['reEnroll']), (first, False))
        self.assertNotIn('previousCertificate', state)
        self.now[0] += 301
        self.assertNotEqual(self.make().tick().get('result'), 'needs-enrollment')

    def test_only_a_2xx_lets_the_previous_certificate_go(self):
        self.enroll()
        self.make().rotate()
        self.make().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        self.server.fail_batches = 1
        self.assertEqual(self.make().flush()['held'], 'unavailable')        # a 503 under the new serial
        self.assertIn('previousCertificate', self.make().state())
        self.assertTrue(self.make().check_in()['ok'])
        self.assertNotIn('previousCertificate', self.make().state())

    def test_a_schema_refusal_of_the_key_is_named_until_the_contract_admits_it(self):
        from node.org import Enrollment, OrgError
        strict = FakeServer(self.clock)
        strict.strict_schema = True
        enrollment = Enrollment(self.node, 'org_strict', transport=strict, clock=self.clock)
        if pinned_key_admits({'storage': 'file', 'exportable': True}):   # pinned at CT-65 (DEC-0094) or later
            self.assertTrue(enrollment.enroll('https://org.example', 'et_1.' + 'S' * 32)['enrolled'])
            return
        with self.assertRaises(OrgError) as raised:   # pinned before it: what a current server answers
            enrollment.enroll('https://org.example', 'et_1.' + 'S' * 32)
        self.assertEqual(raised.exception.code, 'key-storage-refused')
        self.assertFalse((self.node / 'orgs' / 'org_strict' / 'enrollment.json').exists())

    def test_status_after_leave_creates_nothing(self):
        self.enroll()
        self.make().leave()
        self.assertEqual(self.make().describe(), {'org': ORG, 'enrolled': False})
        self.assertFalse((self.node / 'orgs' / ORG).exists())

    def test_one_object_shared_by_two_threads_still_excludes(self):
        import threading
        self.enroll()
        shared = self.make()
        started, release = threading.Event(), threading.Event()
        self.server.pause = ('/v1/devices/check-in', started, release)
        results = []
        first = self._thread(shared.check_in, results)
        self.assertTrue(started.wait(10))
        second = self._thread(shared.check_in, results)
        second.join(0.5)
        self.assertTrue(second.is_alive())             # re-entry is per thread, not per object
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(self.server.sequences, [1, 2])


    # -- the contract pin (CT-65, DEC-0094, at de9eae4d) --------------------------------------------------------
    def device_fixture(self, name):
        return json.loads((VECTORS / 'device' / name).read_text(encoding='utf-8'))

    def test_the_device_fixtures_are_the_pinned_ones(self):
        readme = (VECTORS / 'README.md').read_text(encoding='utf-8')
        listed = dict(re.findall(r'\| `device/([a-z0-9][a-z0-9.-]*\.json)` \| `([0-9a-f]{64})` \|', readme))
        self.assertEqual(set(listed), {p.name for p in (VECTORS / 'device').glob('*.json')})
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((VECTORS / 'device' / name).read_bytes()).hexdigest(), digest, name)

    def test_the_nodes_key_declaration_is_what_the_pinned_schema_admits(self):
        from node.org import Enrollment
        key = Enrollment(self.node, ORG, transport=self.server, clock=self.clock).enrollment_request('et_1')['key']
        self.assertEqual((key['storage'], key['exportable']), ('file', True))
        self.assertTrue(pinned_key_admits(key))
        self.assertTrue(pinned_key_admits(self.device_fixture('device-enrollment-request-file-key.json')['key']))
        for name in ('device-enrollment-file-key-not-exportable.json', 'device-enrollment-file-key-attested.json',
                     'device-enrollment-hardware-key-exportable.json'):
            self.assertFalse(pinned_key_admits(self.device_fixture(name)['key']), name)

    def test_the_contracts_check_in_answer_with_protocols_replaces_the_list(self):
        from node import org
        answer = self.device_fixture('device-check-in-response-protocols.json')
        self.assertEqual(org._protocols(answer), answer['protocols'])
        self.enroll()
        self.server.checkin_protocols = answer['protocols']
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().state()['protocols'], answer['protocols'])

    def test_only_a_schema_refusal_of_the_key_storage_is_named(self):
        from node.org import _key_schema_refusal
        refusal = lambda pointer: {'error': {'code': 'schema-invalid', 'pointer': pointer}}
        self.assertTrue(_key_schema_refusal(422, 'schema-invalid', refusal('/key/storage')))
        self.assertTrue(_key_schema_refusal(422, 'schema-invalid', refusal('/key/exportable')))
        for pointer in ('/key/publicKeySha256', '/device/platform', '/csr', '/storage'):
            self.assertFalse(_key_schema_refusal(422, 'schema-invalid', refusal(pointer)), pointer)
        self.assertFalse(_key_schema_refusal(400, 'schema-invalid', refusal('/key/storage')))
        from node.org import Enrollment, OrgError
        for pointer, code in (('/key/storage', 'key-storage-refused'), ('/csr', 'enroll-refused')):
            answer = json.dumps(refusal(pointer)).encode()
            with self.assertRaises(OrgError) as raised:
                Enrollment(self.node, 'org_older', transport=lambda *a: (422, answer), clock=self.clock).enroll(
                    'https://org.example', 'et_1.' + 'S' * 32)
            self.assertEqual(raised.exception.code, code, pointer)


class MaintenanceStep(unittest.TestCase):
    """The maintenance pass runs `full org tick` only on a machine with an enrollment, and records its failure."""

    def test_nothing_runs_without_an_enrollment_and_a_failure_is_recorded(self):
        import maintenance

        class Commands:
            def __init__(self, code, doc):
                self.calls, self.code, self.doc = [], code, doc

            def full(self, *args, timeout=0, env=None):
                self.calls.append(args)
                self.env = env
                return self.code, self.doc, 'tail line'
        base = Path(tmpdir.mkdtemp()).resolve()
        none = Commands(0, {})
        self.assertEqual(maintenance.step_org(base, none), {'result': 'none'})
        self.assertEqual(none.calls, [])
        (base / 'node' / 'orgs' / ORG).mkdir(parents=True)
        (base / 'node' / 'orgs' / ORG / 'enrollment.json').write_text('{}')
        ran = Commands(0, {'organizations': [{'org': ORG, 'checkIn': {'ok': True}}]})
        self.assertEqual(maintenance.step_org(base, ran), {'result': 'ran', 'organizations': [{'org': ORG,
                                                                                                'result': 'ran'}]})
        self.assertEqual(ran.calls, [('org', 'tick')])
        self.assertEqual(ran.env, {'OBSERVATORY_NODE': str(base / 'node')})   # the CLI reads the same folder
        failed = maintenance.step_org(base, Commands(1, {}))
        self.assertEqual((failed['result'], failed['detail']), ('error', 'tail line'))


class TokenSlot(unittest.TestCase):
    def test_a_malformed_slot_is_refused_before_the_vault(self):
        from node import org
        with self.assertRaises(org.OrgError):
            org.read_token('only-two/parts')


if __name__ == '__main__':
    unittest.main()
