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


def org_iso(seconds: float) -> str:
    return dt.datetime.fromtimestamp(int(seconds), dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def vectors(name: str) -> dict:
    return json.loads((VECTORS / (name + '.json')).read_text(encoding='utf-8'))


@needs_crypto
class TransportVectors(unittest.TestCase):
    """DEC-0056 against `http-signature.json`."""

    def setUp(self):
        self.data = vectors('http-signature')
        self.certs = {c['serial']: c for c in self.data['certificates']}

    def result(self, case, certificates=None):
        held = {(s['serial'], s['nonce']) for s in case.get('seen', [])}
        return hs.verify_request(case['request'], public_base_url=self.data['publicBaseUrl'],
                                 certificate=(certificates or self.certs).get, at_ms=strict_time(self.data['at']),
                                 seen=lambda serial, nonce: (serial, nonce) in held,
                                 client_certificate_serial=case.get('clientCertificateSerial'))

    def verdict(self, case):
        result = self.result(case)
        return 'accepted' if result['ok'] else result['code']

    def test_every_case_answers_as_the_contract(self):
        # A lower bound (DEC-0103's Compatibility): the contract adds cases, and every one it adds must still answer.
        self.assertGreaterEqual(len(self.data['cases']), 56)
        for case in self.data['cases']:
            with self.subTest(case['name']):
                result = self.result(case)
                self.assertEqual('accepted' if result['ok'] else result['code'], case['expect'])
                if 'expectStatus' in case:
                    self.assertEqual(result.get('status'), case['expectStatus'])
                if 'expectClosedAt' in case:
                    self.assertEqual(result.get('closedAt'), case['expectClosedAt'])

    def test_a_closed_organization_is_read_before_revocation(self):
        """DEC-0103 step 4: both vector cases carry a revoked certificate on purpose; an unreadable or null closing
        time is the receiver's own fault, never an open organization."""
        cases = [c for c in self.data['cases'] if c.get('fault') == 'organization']
        self.assertEqual(len(cases), 2)
        for case in cases:
            serial = case.get('clientCertificateSerial') or hs._parsed(case['request']['headers'])['keyid']
            self.assertTrue(self.certs[serial]['revoked'])
            for bad in (None, 'yesterday', '2026-10-10', 7):
                broken = dict(self.certs, **{serial: dict(self.certs[serial], organizationClosedAt=bad)})
                with self.subTest(case['name'], closedAt=bad):
                    self.assertEqual(self.result(case, broken), {'ok': False, 'code': 'context-invalid'})
            reopened = dict(self.certs, **{serial: {k: v for k, v in self.certs[serial].items()
                                                    if k != 'organizationClosedAt'}})
            self.assertEqual(self.result(case, reopened)['code'], 'certificate-revoked')

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
        self.closed = False                                # a server's own `unknown-org` on every request
        self.closed_at = None                              # DEC-0103: the organization ended at this time
        self.closed_body = None                            # a 410 body of the server's choosing instead
        self.acknowledged = None                           # DEC-0102: the notice version the person acknowledged
        self.user = None                                   # the person a certificate binds (none: unassigned)
        self.role = None                                   # `org-cloud` (DEC-0099): a machine no person uses
        self.refuse = None                                 # (status, code): every signed request answers that
        # DEC-0100: a server at or after it renews a device on record by its key; one pinned before refuses the
        # request at its schema. `devices` is its record per device id, `renew_answer` a refusal of its own policy.
        self.renewal, self.devices, self.renew_answer, self.renewed = False, {}, None, []
        self._hashes = hashes

    def __call__(self, url, headers, body, timeout):
        path = url[len('https://org.example'):]
        self.log.append(path)
        self.requests.append((path, json.loads(body)))
        handler = {'/v1/devices/enroll': self.enroll, '/v1/devices/check-in': self.check_in,
                   '/v1/devices/rotate': self.rotate, '/v1/activity/batches': self.batch,
                   '/v1/devices/share-registrations': self.share}[path]
        if self.closed:                                    # the first organization server's own code
            return 404, json.dumps({'error': {'code': 'unknown-org'}}).encode()
        if (self.closed_at or self.closed_body) and path != '/v1/devices/enroll':   # DEC-0103: an enrollment as today
            return 410, json.dumps(self.closed_body or {
                'error': {'code': 'organization-closed', 'message': 'This organization closed; this machine no '
                                                                    'longer reports to it.'},
                'closedAt': self.closed_at}).encode()
        if self.refuse and path != '/v1/devices/enroll':
            return self.refuse[0], json.dumps({'error': {'code': self.refuse[1]}}).encode()
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
        now = dt.datetime.fromtimestamp(self.clock() + self.skew, dt.timezone.utc)   # on the server's own clock
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
                'binding': dict({'org': {'id': self.org}, 'device': {'id': device}},
                                **({'user': {'id': self.user}} if self.user else {}),
                                **({'role': self.role} if self.role else {})), 'issuer': 'test-ca'}

    def _csr(self, text):
        from cryptography import x509
        csr = x509.load_der_x509_csr(base64.b64decode(text))
        assert csr.is_signature_valid
        return csr

    def enroll(self, path, headers, body):
        doc = json.loads(body)
        if doc.get('auth') == {'method': 'key-on-record'}:
            return self.renew(headers, doc)
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
        self.devices[doc['device']['id']] = {'keySha256': doc['key']['publicKeySha256']}
        return 200, self._enrolled(csr, doc['device']['id'])

    def _enrolled(self, csr, device):
        self.epoch += 1
        return {'protocol': 'fabric-device/0.1', 'kind': 'enrollment-response', 'protocols': self.protocols,
                'certificate': self._issue(csr, device), 'streamEpoch': self.epoch,
                'policyRootKey': {'keyId': 'root', 'alg': 'ed25519', 'publicKey': 'x'},
                'checkIn': {'intervalSeconds': 300}, 'collectionStart': '2026-10-10T00:00:00Z'}

    def renew(self, headers, doc):
        """DEC-0100 as FAC-SEM-044 holds a server to it: a device on record, not revoked, whose last certificate ended
        at most 30 days ago on the server's clock, with a CSR from its key on record and no other proof."""
        assert 'authorization' not in headers             # the CSR's signature is the only proof
        assert set(doc['auth']) == {'method'}
        if not self.renewal:                               # a server pinned before DEC-0100: refused at its schema
            return 422, {'error': {'code': 'schema-invalid', 'pointer': '/auth/method', 'message': 'not in enum'}}
        if self.renew_answer:
            return self.renew_answer[0], {'error': {'code': self.renew_answer[1]}}
        device = doc['device']['id']
        record = self.devices.get(device)
        if record is None:
            return 401, {'error': {'code': 'certificate-unknown'}}
        if record.get('revoked'):
            return 401, {'error': {'code': 'certificate-revoked'}}
        csr = self._csr(doc['csr'])
        from cryptography.hazmat.primitives import serialization
        spki = csr.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if 'sha256:' + hashlib.sha256(spki).hexdigest() != doc['key']['publicKeySha256'] \
                or doc['key']['publicKeySha256'] != record['keySha256']:
            return 403, {'error': {'code': 'key-not-on-record'}}
        ended = max(strict_time(c['notAfter']) for c in self.certs.values() if c['device'] == device) / 1000
        if self.clock() + self.skew - ended > 30 * 86400:
            return 403, {'error': {'code': 'renewal-window-closed'}}
        self.renewed.append(device)
        return 200, self._enrolled(csr, device)

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
        if self.acknowledged is not None:
            out['acknowledgedNoticeVersion'] = self.acknowledged
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

    # -- OBS-199: the certificate's expiry, named before and after it passes ------------------------------------
    def test_status_names_the_expiry_date_and_warns_three_days_before(self):
        enrolled = self.enroll()
        not_after = enrolled['certificate']['notAfter']
        self.assertEqual(enrolled['certificate']['expiresOn'], not_after[:10])
        self.assertEqual(enrolled['certificate']['state'], 'valid')
        self.assertNotIn('org:certificate-expiring', enrolled['degraded'])
        self.assertIsNone(enrolled.get('nextStep'))
        self.now[0] = strict_time(not_after) / 1000 - 3 * 86400 - 60          # a minute before the warning
        self.assertEqual(self.make().describe()['certificate']['state'], 'valid')
        self.now[0] += 120                                                      # inside the last three days
        status = self.make().describe()
        self.assertEqual(status['certificate']['state'], 'expiring')
        self.assertIn('org:certificate-expiring', status['degraded'])
        self.assertIn(not_after, status['nextStep'])
        self.assertEqual(status['command'], 'project-observatory full org rotate --org ' + ORG)
        self.assertFalse(status['needsEnrollment'])

    def test_a_check_in_on_an_expired_certificate_names_it_and_the_exact_step(self):
        from node.org import OrgError
        not_after = self.enroll()['certificate']['notAfter']
        self.now[0] = strict_time(not_after) / 1000 + 30 * 86400 + 1          # past the renewal window (DEC-0100)
        sent = len(self.server.log)
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        error = raised.exception
        self.assertEqual(error.code, 'certificate-expired')
        self.assertIn(not_after, str(error))
        self.assertEqual(error.command, 'project-observatory full org enroll --server https://org.example --org '
                         + ORG + ' --token-slot PROJECT/ENV/NAME')
        self.assertIn('new enrollment token', error.next_step)
        self.assertEqual(len(self.server.log), sent)                            # nothing went to the server
        status = self.make().describe()
        self.assertEqual(status['certificate']['state'], 'expired')
        self.assertEqual(status['command'], error.command)
        self.assertIn('org:certificate-expired', status['degraded'])
        tick = self.make().tick()
        self.assertEqual((tick['result'], tick['command']), ('needs-enrollment', error.command))

    def test_the_terminal_says_an_expired_certificate_and_its_step_and_fails(self):
        import contextlib
        import io
        from unittest import mock
        sys.path.insert(0, str(ROOT / 'tools'))
        import org as org_tool
        not_after = self.enroll()['certificate']['notAfter']
        self.now[0] = strict_time(not_after) / 1000 + 1
        out = io.StringIO()
        with mock.patch.object(org_tool, '_enrollment', lambda org_id: self.make()), \
                mock.patch.object(org_tool, '_orgs', lambda args: [ORG]), contextlib.redirect_stdout(out):
            code = org_tool.main(['check-in', '--org', ORG])
        self.assertEqual(code, 1)
        (row,) = json.loads(out.getvalue())['organizations']
        self.assertEqual(row['error'], 'certificate-expired')
        self.assertIn(not_after, row['message'])
        self.assertIn('full org enroll --server https://org.example', row['command'])
        self.assertIn('new enrollment token', row['nextStep'])

    def test_health_lists_each_enrollments_expiry_without_the_network(self):
        from node import org
        not_after = self.enroll()['certificate']['notAfter']
        sent = len(self.server.log)
        (notice,) = org.notices(self.node, now=self.clock())
        self.assertEqual((notice['org'], notice['state'], notice['expiresOn']), (ORG, 'valid', not_after[:10]))
        (notice,) = org.notices(self.node, now=strict_time(not_after) / 1000 - 86400)
        self.assertEqual((notice['state'], notice['command']), ('expiring', 'project-observatory full org rotate --org '
                                                                + ORG))
        (notice,) = org.notices(self.node, now=strict_time(not_after) / 1000 + 1)
        self.assertEqual(notice['state'], 'expired')
        self.assertEqual(org.notices(self.node.parent / 'no-node', now=self.clock()), [])
        self.assertEqual(len(self.server.log), sent)

    def test_doctor_and_health_read_the_same_notices(self):
        from unittest import mock
        import workspace
        self.enroll()
        with mock.patch.dict(os.environ, {'OBSERVATORY_NODE': str(self.node)}):
            (row,) = workspace._organizations(self.node.parent / 'elsewhere')
        self.assertEqual((row['org'], row['server']), (ORG, 'https://org.example'))
        self.assertIn(row['state'], ('valid', 'expiring', 'expired'))
        with mock.patch.dict(os.environ, {'OBSERVATORY_NODE': ''}):
            self.assertEqual(len(workspace._organizations(self.node.parent)), 1)      # <base>/node, as `full org`
            self.assertEqual(workspace._organizations(self.node.parent / 'elsewhere'), [])
        source = (ROOT / 'dashboard' / 'build_dashboard.py').read_text(encoding='utf-8')
        self.assertIn('node_org.notices(node_org.node_folder(', source)              # the Health data
        for state in ('removed', 'expired', 'needs-enrollment', 'expiring'):          # each state has its row
            self.assertIn('"%s"' % state, source[source.index('Array.isArray(H.organizations)'):])
        self.assertIn('O.renewUntil', source[source.index('Array.isArray(H.organizations)'):])   # DEC-0100

    # -- OBS-204: a machine removed from its organization ------------------------------------------------------
    def _pending(self):
        with self.make()._db() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM events WHERE state = 'pending'").fetchone()['n']

    def assert_removed(self, code):
        from node.org import OrgError
        state = self.make().state()
        self.assertEqual((state['removed']['code'], state['removed']['droppedEvents']), (code, 3))
        self.assertEqual(self._pending(), 0)
        status = self.make().describe()
        self.assertEqual(status['state'], 'removed')
        self.assertEqual(status['removed']['code'], code)
        self.assertIn('org:removed', status['degraded'])
        self.assertIn('removed from', status['nextStep'])
        self.assertEqual(status['command'], 'project-observatory full org leave --org ' + ORG)
        sent = len(self.server.log)
        for step in ('check_in', 'rotate', 'flush', 'register_shares', 'collect_grant_usage'):
            with self.subTest(step), self.assertRaises(OrgError) as raised:
                getattr(self.make(), step)()
            self.assertEqual(raised.exception.code, 'removed-from-organization')
        with self.assertRaises(OrgError):
            self.make().enqueue('meter.call', {'n': 9}, occurred='2026-10-18T00:00:00Z')
        self.now[0] += 86400
        self.assertEqual(self.make().tick()['result'], 'removed')
        self.assertEqual(len(self.server.log), sent)                            # no retry reaches the server
        (notice,) = __import__('node.org', fromlist=['notices']).notices(self.node, now=self.clock())
        self.assertEqual((notice['state'], notice['removed']['code']), ('removed', code))

    def _three_events(self):
        for n in range(3):
            self.make().enqueue('meter.call', {'n': n}, occurred='2026-10-18T00:00:00Z')

    def test_a_revoked_certificate_removes_the_machine_and_drops_what_it_buffered(self):
        self.enroll()
        self._three_events()
        self.server.certs[self.make().state()['certificate']['serial']]['revoked'] = True
        result = self.make().check_in()
        self.assertFalse(result['ok'])
        self.assertEqual((result['code'], result['removed']['code']), ('certificate-revoked', 'certificate-revoked'))
        self.assertEqual(result['command'], 'project-observatory full org leave --org ' + ORG)
        self.assert_removed('certificate-revoked')

    def test_unknown_org_on_a_device_request_is_retried_and_never_ends_the_enrollment(self):
        # DEC-0103: `unknown-org` is a server's own enrollment refusal, not a contract code; the provisional rule that
        # ended an enrollment on a second sighting an hour on is retired. On a signed request it is any failure.
        self.enroll()
        self._three_events()
        self.server.closed = True
        self.assertEqual(self.make().flush()['held'], 'unknown-org')            # retried as usual, nothing dropped
        for _ in range(3):
            self.now[0] += 3601
            self.assertEqual(self.make().tick()['checkIn']['code'], 'unknown-org')
        state = self.make().state()
        self.assertNotIn('removed', state)
        self.assertNotIn('unknownOrg', state)
        self.assertNotIn('tickSeq', state)
        self.assertEqual(self._pending(), 3)
        status = self.make().describe()
        self.assertNotIn('org:unknown-org', status['degraded'])
        self.assertNotIn('unknownOrg', status)
        self.server.closed = False
        self.now[0] += 3601
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().flush()['accepted'], 3)

    def test_an_enrollment_the_server_refuses_unknown_org_names_the_organization(self):
        from node.org import OrgError
        self.server.closed = True
        with self.assertRaises(OrgError) as raised:
            self.enroll()
        self.assertEqual(raised.exception.code, 'enroll-refused')
        self.assertIn('does not know organization ' + ORG, str(raised.exception))
        self.assertIsNone(self.make().state())

    def test_a_state_from_the_provisional_rule_is_dropped_by_the_next_check_in(self):
        self.enroll()
        state = self.make().state()
        state.update(unknownOrg={'since': org_iso(self.now[0]), 'at': int(self.now[0]), 'status': 404, 'tick': 1},
                     tickSeq=4)
        self.make()._save(state)
        self.assertNotIn('warning', self.make().describe())
        self.assertTrue(self.make().check_in()['ok'])
        self.assertNotIn('unknownOrg', self.make().state())

    # -- DEC-0103: an organization that ended -----------------------------------------------------------------------
    CLOSED_AT = '2026-10-18T00:00:00Z'

    def assert_closed(self, events=3):
        """The enrollment ended on `organization-closed`: what it buffered, its registrations, policy root, collection
        start and stream are gone; every step is refused before the network; status says so with the date."""
        from node.org import OrgError
        state = self.make().state()
        self.assertEqual({k: state['removed'][k] for k in ('code', 'status', 'closedAt', 'droppedEvents')},
                         {'code': 'organization-closed', 'status': 410, 'closedAt': self.CLOSED_AT,
                          'droppedEvents': events})
        for gone in ('collectionStart', 'policyRootKey'):
            self.assertNotIn(gone, state)
        with self.make()._db() as conn:
            for table in ('events', 'registrations', 'meta'):
                self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM %s' % table).fetchone()['n'], 0, table)
        status = self.make().describe()
        self.assertEqual((status['state'], status['removed']['code']), ('removed', 'organization-closed'))
        self.assertIn('org:removed', status['degraded'])
        self.assertIn('closed on 2026-10-18', status['nextStep'])
        self.assertEqual(status['command'], 'project-observatory full org leave --org ' + ORG)
        sent = len(self.server.log)
        for step in ('check_in', 'rotate', 'flush', 'register_shares', 'collect_grant_usage'):
            with self.subTest(step), self.assertRaises(OrgError) as raised:
                getattr(self.make(), step)()
            self.assertEqual(raised.exception.code, 'removed-from-organization')
        self.now[0] += 86400
        self.assertEqual(self.make().tick()['result'], 'removed')
        self.assertEqual(len(self.server.log), sent)                            # one answer was final: no retry

    def test_a_closed_organization_ends_the_enrollment_on_one_check_in(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'send'})
        self._three_events()
        self.server.closed_at = self.CLOSED_AT
        result = self.make().check_in()
        self.assertEqual((result['ok'], result['code'], result['status']), (False, 'organization-closed', 410))
        self.assertEqual(result['removed']['closedAt'], self.CLOSED_AT)
        self.assertEqual(self.make().state()['removed']['droppedRegistrations'], 1)
        self.assert_closed()

    def test_a_batch_answered_410_is_the_enrollments_end_not_a_rejection(self):
        self.enroll()
        self._three_events()
        self.server.closed_at = self.CLOSED_AT
        result = self.make().flush()
        self.assertEqual((result['held'], result['rejected'], result['batches']), ('removed', 0, 0))
        self.assertEqual(result['removed']['code'], 'organization-closed')
        self.assert_closed()

    def test_a_share_registration_answered_410_stops_for_that_organization(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self._three_events()
        self.server.closed_at = self.CLOSED_AT
        result = self.make().register_shares()
        self.assertEqual(result['held'], 'removed')
        self.assertEqual(result['removed']['code'], 'organization-closed')
        self.assert_closed()

    def test_a_rotation_answered_410_ends_it_too(self):
        from node.org import OrgError
        self.enroll()
        self._three_events()
        self.server.closed_at = self.CLOSED_AT
        try:
            self.make().rotate()
        except OrgError:
            pass
        self.assert_closed()

    def test_a_410_that_is_not_the_contracts_body_is_retried(self):
        # Ending an enrollment cannot be undone, so only `device-organization-closed` ends it: a 410 without a
        # readable closedAt, with another code, a flat body or a path in its message is any failure.
        self.enroll()
        self._three_events()
        bodies = [json.loads((VECTORS / 'device' / ('device-organization-closed-%s.json' % name)).read_text())
                  for name in ('bad-time', 'error-extra', 'extra', 'flat', 'no-time', 'other-code')]
        path = json.loads((VECTORS / 'device' / 'device-organization-closed.json').read_text())
        path['error']['message'] = 'Closed; see %s for your data.' % '/'.join(('', 'Users', 'example', 'org-export'))
        for body in bodies + [path]:                    # the contract's `-path`, rebuilt: the gate refuses the file
            with self.subTest(body=body):
                self.server.closed_body = body
                self.assertEqual(self.make().check_in()['status'], 410)
                self.make().flush()
                self.assertNotIn('removed', self.make().state())
                self.assertEqual(self._pending(), 3)
                self.now[0] += 3601
        self.server.closed_body = None
        self.assertTrue(self.make().check_in()['ok'])

    def test_the_closed_body_is_read_as_the_contracts_schema(self):
        from node.org import organization_closed_at
        positive = json.loads((VECTORS / 'device' / 'device-organization-closed.json').read_text())
        self.assertEqual(organization_closed_at(410, positive), positive['closedAt'])
        for status in (400, 401, 404, 409, 503):
            self.assertIsNone(organization_closed_at(status, positive))
        negatives = sorted((VECTORS / 'device').glob('device-organization-closed-*.json'))
        self.assertEqual(len(negatives), 6)
        for file in negatives:
            with self.subTest(file.name):
                self.assertIsNone(organization_closed_at(410, json.loads(file.read_text())))

    def test_a_closed_organization_leaves_the_machines_other_enrollments_alone(self):
        from node.org import Enrollment
        other_server = FakeServer(self.clock)
        other = lambda: Enrollment(self.node, 'org_other', transport=other_server, clock=self.clock)   # noqa: E731
        other().enroll('https://org.example', 'et_1.' + 'S' * 32)
        other().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        self.enroll()
        self._three_events()
        self.server.closed_at = self.CLOSED_AT
        self.make().check_in()
        self.assert_closed()
        self.assertNotIn('removed', other().state())
        self.assertTrue(other().check_in()['ok'])
        self.assertEqual(other().flush()['accepted'], 1)

    def test_a_closed_organization_left_and_joined_again_with_a_new_token_starts_over(self):
        self.enroll()
        self._three_events()
        self.server.closed_at = self.CLOSED_AT
        self.make().check_in()
        self.server.closed_at, self.server.epoch = None, 0                       # the organization is created again
        again = self.enroll('et_2.' + 'T' * 32)
        self.assertEqual((again['epoch'], again['state']), (1, 'enrolled'))
        self.assertTrue(self.make().check_in()['ok'])

    def test_a_refused_batch_that_names_the_org_or_the_certificate_is_held_not_rejected(self):
        # A 400 or 404 `unknown-org` and a 400 `certificate-unknown` are not refusals of the batch's content: the
        # events stay pending and the flush stops for this tick, retried with backoff.
        self.enroll()
        self._three_events()
        for status, code in ((400, 'unknown-org'), (404, 'unknown-org'), (400, 'certificate-unknown')):
            with self.subTest(status=status, code=code):
                self.server.refuse = (status, code)
                result = self.make().flush()
                self.assertEqual((result['held'], result['rejected'], result['batches']), (code, 0, 0))
                self.assertEqual(self._pending(), 3)
                self.assertNotIn('removed', self.make().state())
                self.assertEqual(self.make().flush()['held'], 'backoff')        # retried later, not resent now
                self.now[0] += 120
        self.server.refuse = None
        self.now[0] += 3601
        self.assertEqual(self.make().flush()['accepted'], 3)

    def test_the_fallback_judges_the_previous_certificate_on_the_servers_clock(self):
        self.server.skew = -250                                                 # this clock runs ahead
        self.enroll()
        self.assertTrue(self.make().check_in()['ok'])
        first = self.make().state()['certificate']
        self.make().rotate()
        del self.server.certs[self.make().state()['certificate']['serial']]
        self.now[0] = strict_time(first['notAfter']) / 1000 + 60                # past it here, not on the server
        self.assertTrue(self.make().check_in()['ok'])
        state = self.make().state()
        self.assertEqual(state['certificate']['serial'], first['serial'])
        self.assertNotIn('removed', state)

    def test_only_a_401_ends_the_enrollment_and_a_5xx_never(self):
        # devices.md step table: certificate-unknown and certificate-revoked come with HTTP 401.
        self.enroll()
        self._three_events()
        for status, code in ((503, 'certificate-unknown'), (403, 'certificate-unknown'), (502, 'unknown-org'),
                             (503, 'certificate-revoked'), (500, 'unknown-org')):
            with self.subTest(status=status, code=code):
                self.server.refuse = (status, code)
                result = self.make().check_in()
                self.assertEqual((result['ok'], result['code']), (False, code))
                self.make().flush()
                state = self.make().state()
                self.assertNotIn('removed', state)
                self.assertFalse(state['reEnroll'])
                self.assertEqual(self._pending(), 3)
                self.now[0] += 3601                                             # past any backoff and any hour
        self.server.refuse = None
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().flush()['accepted'], 3)

    def test_a_401_certificate_unknown_falls_back_first_and_ends_only_when_that_is_refused(self):
        self.enroll()
        self._three_events()
        first = self.make().state()['certificate']['serial']
        self.make().rotate()
        second = self.make().state()['certificate']['serial']
        del self.server.certs[second]
        self.assertTrue(self.make().check_in()['ok'])                           # fallen back, not removed
        self.assertEqual(self.make().state()['certificate']['serial'], first)
        self.make().rotate()
        third = self.make().state()['certificate']['serial']
        for serial in (first, third):
            self.server.certs.pop(serial, None)                                 # both unknown to the server
        self.assertEqual(self.make().check_in()['code'], 'certificate-unknown')
        self.assert_removed('certificate-unknown')

    def test_a_certificate_the_server_forgot_with_no_previous_one_removes_the_machine(self):
        self.enroll()
        self._three_events()
        del self.server.certs[self.make().state()['certificate']['serial']]
        self.assertEqual(self.make().check_in()['code'], 'certificate-unknown')
        self.assert_removed('certificate-unknown')

    def test_a_removed_machine_leaves_and_a_new_token_starts_over(self):
        self.enroll()
        self._three_events()
        self.server.certs[self.make().state()['certificate']['serial']]['revoked'] = True
        self.make().check_in()
        self.assertEqual(self.make().state()['removed']['code'], 'certificate-revoked')
        self.server.epoch = 0                                                   # the organization is created again
        again = self.enroll('et_2.' + 'T' * 32)
        self.assertEqual((again['epoch'], again['state']), (1, 'enrolled'))
        self.assertNotIn('removed', again)
        self.assertTrue(self.make().check_in()['ok'])
        self.make().enqueue('meter.call', {'n': 'new'}, occurred='2026-10-18T00:00:00Z')
        with self.make()._db() as conn:
            self.assertEqual([r['seq'] for r in conn.execute('SELECT seq FROM events')], [1])  # a new stream
        self.assertEqual(self.make().leave(), {'org': ORG, 'left': True})
        self.assertFalse((self.node / 'orgs' / ORG).exists())

    def test_expiry_is_judged_on_the_servers_clock(self):
        # A local clock running ahead must not refuse a certificate the server still takes, nor one running behind
        # keep using a certificate the server has seen expire (the fake server allows 300 s of skew in a signature).
        self.server.skew = -250                                                 # the server's clock is behind
        not_after = strict_time(self.enroll()['certificate']['notAfter']) / 1000
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().state()['skewSeconds'], -250)
        self.now[0] = not_after + 60                                            # past it here, not on the server
        self.assertEqual(self.make().describe()['certificate']['state'], 'expiring')
        self.assertTrue(self.make().check_in()['ok'])
        self.assertNotEqual(self.make().tick().get('result'), 'needs-enrollment')
        not_after = strict_time(self.make().state()['certificate']['notAfter']) / 1000   # the tick rotated it
        self.server.skew = 250
        self.now[0] = not_after - 3600
        self.assertEqual(self.make().check_in()['code'], 'signature-stale')    # the jump is measured again
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().state()['skewSeconds'], 250)
        self.now[0] = not_after - 60                                            # not yet here, past on the server
        self.assertEqual(self.make().describe()['certificate']['state'], 'expired')
        from node.org import OrgError
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'certificate-expired')

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'needs a folder this account cannot list')
    def test_an_unreadable_node_folder_is_named_and_doctor_still_answers(self):
        from unittest import mock
        import workspace
        from node import org
        self.enroll()
        orgs = self.node / 'orgs'
        orgs.chmod(0)
        try:
            (row,) = org.notices(self.node, now=self.clock())
            self.assertEqual((row['state'], row['reason']), ('unreadable', 'PermissionError'))
            with mock.patch.dict(os.environ, {'OBSERVATORY_NODE': str(self.node)}):
                answer = workspace._organizations(self.node.parent)
            self.assertEqual(answer[0]['state'] if isinstance(answer, list) else answer['state'],
                             'unreadable' if isinstance(answer, list) else 'unknown')
        finally:
            orgs.chmod(0o700)

    # -- OBS-199, contract DEC-0100: renewal by the key on record within 30 days of the certificate's end --------
    def _expired_by(self, seconds):
        """Enrolled, then `seconds` past the certificate's end; returns that end."""
        not_after = strict_time(self.enroll()['certificate']['notAfter']) / 1000
        self.now[0] = not_after + seconds
        return not_after

    def _renewals(self):
        return [doc for path, doc in self.server.requests
                if path == '/v1/devices/enroll' and doc['auth'] == {'method': 'key-on-record'}]

    def test_a_check_in_past_expiry_renews_by_the_key_on_record_without_a_token(self):
        self.server.renewal = True
        old = self.enroll()
        enrolled_at = self.make().state()['enrolledAt']
        self.now[0] = strict_time(old['certificate']['notAfter']) / 1000 + 20 * 86400    # back from a holiday
        result = self.make().check_in()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['renewed']['by'], 'key-on-record')
        (request,) = self._renewals()
        self.assertNotIn('tokenId', request['auth'])
        self.assertEqual(request['key']['publicKeySha256'],
                         self.server.devices[request['device']['id']]['keySha256'])
        self.assertEqual(strict_time(request['requestedAt']) / 1000, self.now[0])
        state = self.make().state()
        self.assertEqual(state['epoch'], old['epoch'] + 1)                      # a new epoch, as every re-enrollment
        self.assertNotEqual(state['certificate']['serial'], old['certificate']['serial'])
        self.assertFalse(state['reEnroll'])
        self.assertEqual(state['enrolledAt'], enrolled_at)
        self.assertEqual(state['renewedAt'], state['certificate']['notBefore'])  # the server's clock (DEC-0102)
        self.assertEqual(state['renewedAt'], org_iso(self.now[0]))
        status = self.make().describe()
        self.assertEqual((status['state'], status['certificate']['state']), ('enrolled', 'valid'))
        self.assertNotIn('org:certificate-expired', status['degraded'])
        self.assertEqual(self.server.used, {'et_1'})                            # no second token was spent
        journal = (self.node / 'orgs' / ORG / 'journal.jsonl').read_text(encoding='utf-8')
        self.assertIn('"event": "renewed"', journal)

    def test_a_renewal_is_dated_and_measured_on_the_servers_clock(self):
        # DEC-0102 item 6: the server's clock is the new certificate's notBefore, never this machine's.
        self.server.renewal = True
        old = self.enroll()
        self.server.skew = -1800                                                 # this clock runs half an hour ahead
        self.now[0] = strict_time(old['certificate']['notAfter']) / 1000 + 5 * 86400
        self.assertTrue(self.make().check_in()['ok'])
        state = self.make().state()
        self.assertEqual(state['renewedAt'], org_iso(self.now[0] - 1800))
        self.assertEqual(state['skewSeconds'], -1800)

    def test_the_terminal_check_in_renews_and_succeeds(self):
        import contextlib
        import io
        from unittest import mock
        sys.path.insert(0, str(ROOT / 'tools'))
        import org as org_tool
        self.server.renewal = True
        self._expired_by(86400)
        out = io.StringIO()
        with mock.patch.object(org_tool, '_enrollment', lambda org_id: self.make()), \
                mock.patch.object(org_tool, '_orgs', lambda args: [ORG]), contextlib.redirect_stdout(out):
            code = org_tool.main(['check-in', '--org', ORG])
        self.assertEqual(code, 0)
        (row,) = json.loads(out.getvalue())['organizations']
        self.assertTrue(row['ok'])
        self.assertEqual(row['renewed']['epoch'], 2)

    def test_the_renewal_request_is_the_contracts(self):
        """The request matches the vendored DEC-0100 fixture member for member, carries no token id (the negative
        fixture), and its `auth` is the schema's key-on-record branch exactly."""
        fixture = json.loads((VECTORS / 'device' / 'device-enrollment-request-key-on-record.json').read_text())
        negative = json.loads((VECTORS / 'device' / 'device-enrollment-key-on-record-with-token.json').read_text())
        self.server.renewal = True
        self._expired_by(86400)
        self.make().check_in()
        (request,) = self._renewals()
        self.assertEqual(set(request) - {'deviceKeys'}, set(fixture))
        self.assertEqual(request['auth'], fixture['auth'])
        self.assertNotEqual(request['auth'], negative['auth'])
        schema = json.loads((VECTORS / 'device-enrollment.schema.json').read_text(encoding='utf-8'))
        found = []

        def walk(node):
            if isinstance(node, dict):
                method = (node.get('properties') or {}).get('method') if isinstance(node.get('properties'), dict) \
                    else None
                if isinstance(method, dict) and method.get('const') == 'key-on-record':
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(schema)
        (branch,) = found
        self.assertEqual(set(request['auth']), set(branch['required']))
        self.assertIs(branch['additionalProperties'], False)

    def test_a_tick_past_expiry_renews_and_then_does_what_is_due(self):
        self.server.renewal = True
        self._expired_by(3 * 86400)
        report = self.make().tick()
        self.assertEqual(report['renewed']['by'], 'key-on-record')
        self.assertTrue(report['checkIn']['ok'], report)
        self.assertEqual(len(self._renewals()), 1)
        self.assertEqual(self.make().tick()['checkIn'], 'not-due')              # renewed once, not every tick

    def test_the_window_is_thirty_days_on_the_servers_clock(self):
        from node.org import OrgError
        self.server.renewal = True
        not_after = self._expired_by(30 * 86400)                                # the last second it is taken
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(len(self._renewals()), 1)
        # Past the 30 days: refused before the network, with #339's step (a new token).
        not_after = strict_time(self.make().state()['certificate']['notAfter']) / 1000
        self.now[0] = not_after + 30 * 86400 + 1
        sent = len(self.server.log)
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'certificate-expired')
        self.assertIn('new enrollment token', raised.exception.next_step)
        self.assertIn('full org enroll --server', raised.exception.command)
        self.assertEqual(len(self.server.log), sent)
        self.assertEqual(self.make().tick()['result'], 'needs-enrollment')
        self.assertEqual(len(self.server.log), sent)
        # The window is judged on the server's clock: a server an hour behind still takes it.
        state = self.make().state()
        state['skewSeconds'] = -3600
        self.make()._save(state)
        self.server.skew = -3600
        self.assertTrue(self.make().check_in()['ok'])

    def test_inside_the_window_status_says_it_renews_by_itself(self):
        from node import org
        self.server.renewal = True
        not_after = self._expired_by(86400)
        (said,) = org.notices(self.node, now=self.clock())
        self.assertEqual(said['state'], 'expired')
        self.assertEqual(said['renewUntil'], org.iso(not_after + 30 * 86400))
        self.assertEqual(said['command'], 'project-observatory full org check-in --org ' + ORG)
        self.assertIn('key on record', said['nextStep'])
        self.assertIn(said['renewUntil'], said['nextStep'])
        status = self.make().describe()
        self.assertFalse(status['needsEnrollment'])
        self.assertNotIn('org:needs-enrollment', status['degraded'])
        self.assertIn('org:certificate-expired', status['degraded'])
        (said,) = org.notices(self.node, now=not_after + 30 * 86400 + 1)          # past it: #339's step
        self.assertNotIn('renewUntil', said)
        self.assertIn('new enrollment token', said['nextStep'])

    def test_a_server_that_refuses_the_renewal_is_asked_once_then_a_token_is_needed(self):
        from node.org import OrgError
        self._expired_by(86400)                                                 # pinned before DEC-0100: 422
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'certificate-expired')
        self.assertIn('schema-invalid', str(raised.exception))
        self.assertIn('new enrollment token', raised.exception.next_step)
        sent = len(self.server.log)
        with self.assertRaises(OrgError):
            self.make().check_in()
        self.assertEqual(self.make().tick()['result'], 'needs-enrollment')
        self.assertEqual(len(self.server.log), sent)                            # never asked again for that certificate
        status = self.make().describe()
        self.assertEqual(status['renewalRefused']['code'], 'schema-invalid')
        self.assertIn('org:needs-enrollment', status['degraded'])
        self.assertNotIn('renewUntil', status)
        # A new token starts over, and a later expiry may renew again.
        self.server.renewal = True
        self.enroll('et_2.' + 'T' * 32)
        self.assertNotIn('renewal', self.make().state())

    def test_a_policy_refusal_is_named(self):
        from node.org import OrgError
        self.server.renewal, self.server.renew_answer = True, (403, 'key-storage-refused')
        self._expired_by(86400)
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'certificate-expired')
        self.assertIn('key-storage-refused', str(raised.exception))
        self.assertIn('new enrollment token', raised.exception.next_step)

    def test_an_unanswered_renewal_is_retried_with_backoff(self):
        from node.org import OrgError
        self.server.renewal, self.server.renew_answer = True, (503, 'unavailable')
        self._expired_by(86400)
        report = self.make().tick()
        self.assertEqual(report['result'], 'renewal-pending')
        self.assertEqual(report['renewal']['attempts'], 1)
        self.assertEqual(report['command'], 'project-observatory full org check-in --org ' + ORG)
        sent = len(self.server.log)
        self.assertEqual(self.make().tick()['result'], 'renewal-pending')       # the backoff holds the next tick
        self.assertEqual(len(self.server.log), sent)
        with self.assertRaises(OrgError) as raised:                             # a person's check-in asks now
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'renewal-failed')
        self.assertEqual(len(self.server.log), sent + 1)
        self.server.renew_answer = None
        self.now[0] += 3600
        self.assertEqual(self.make().tick()['renewed']['by'], 'key-on-record')
        self.assertNotIn('renewal', self.make().state())

    def test_a_revoked_device_does_not_renew_and_is_removed(self):
        from node.org import OrgError
        self.server.renewal = True
        self._expired_by(86400)
        self.server.devices[self.make().state()['device']]['revoked'] = True
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'removed-from-organization')
        self.assertEqual(self.make().state()['removed']['code'], 'certificate-revoked')
        self.assertEqual(self.server.renewed, [])

    def test_a_removed_enrollment_never_renews(self):
        from node.org import OrgError
        self.server.renewal = True
        self.enroll()
        self.server.refuse = (401, 'certificate-revoked')
        self.make().check_in()                                                  # OBS-204: removed
        self.server.refuse = None
        self.now[0] = strict_time(self.make().state()['certificate']['notAfter']) / 1000 + 86400
        sent = len(self.server.log)
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'removed-from-organization')
        self.assertEqual(self.make().tick()['result'], 'removed')
        self.assertEqual(len(self.server.log), sent)
        self.assertNotIn('renewUntil', self.make().describe())

    def test_a_server_asked_re_enrollment_is_not_a_renewal(self):
        self.server.renewal = True
        self.enroll()
        state = self.make().state()
        state['reEnroll'] = True                                                # the server said `re-enroll`
        self.make()._save(state)
        self.now[0] = strict_time(state['certificate']['notAfter']) / 1000 + 86400
        self.assertEqual(self.make().tick()['result'], 'needs-enrollment')
        self.assertEqual(self._renewals(), [])

    def test_a_server_that_saw_the_certificate_expire_lets_it_renew(self):
        # The local clock is behind: the server refuses the check-in as expired before this machine sees it.
        self.server.renewal = True
        not_after = strict_time(self.enroll()['certificate']['notAfter']) / 1000
        self.server.refuse = (401, 'certificate-expired')
        self.now[0] = not_after - 600
        self.assertEqual(self.make().check_in()['code'], 'certificate-expired')
        self.server.refuse = None
        self.assertEqual(self.make().tick()['renewed']['by'], 'key-on-record')

    def test_a_renewal_that_changes_the_binding_is_refused(self):
        from node.org import OrgError
        self.server.renewal = True
        self._expired_by(86400)
        issue = self.server._issue

        def with_user(csr, device):
            cert = issue(csr, device)
            cert['binding']['user'] = {'id': 'someone'}
            return cert
        self.server._issue = with_user
        old = self.make().state()
        with self.assertRaises(OrgError) as raised:
            self.make().check_in()
        self.assertEqual(raised.exception.code, 'certificate-expired')
        self.assertIn('binding-changed', str(raised.exception))
        self.assertEqual(self.make().state()['certificate'], old['certificate'])

    def test_the_renewal_holds_the_enrollments_lock(self):
        import threading
        self.server.renewal = True
        self._expired_by(86400)
        started, release = threading.Event(), threading.Event()
        self.server.pause = ('/v1/devices/enroll', started, release)
        renewed, left = [], []
        tick = self._thread(lambda: self.make().tick(), renewed)
        self.assertTrue(started.wait(10))
        leave = self._thread(lambda: self.make().leave(), left)
        leave.join(0.5)
        self.assertTrue(leave.is_alive())                                       # leave waits for the renewal
        release.set()
        tick.join(10)
        leave.join(10)
        self.assertEqual(renewed[0]['renewed']['by'], 'key-on-record')
        self.assertEqual(left, [{'org': ORG, 'left': True}])

    def test_a_notice_refusal_holds_without_degrading_and_waits_for_the_weekly_resend(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self.server.share_answer = (403, {'error': {'code': 'purpose-not-collected', 'message': 'notice'}})
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'hold-notice'})
        self.assertEqual(self.make().register_shares()['sent'], 0)              # not every tick
        self.assertEqual(self.make().collect_grant_usage()['enqueued'], 0)      # the sums wait in the meter
        self.assertNotIn('org:share-registration-refused', self.make().describe()['degraded'])
        self.server.share_answer = None
        with self.make()._db() as conn:                                         # a week after it was sent
            conn.execute('UPDATE registrations SET sent_at = sent_at - ?', (7 * 86400 + 1,))
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'send'})

    # -- DEC-0100, DEC-0102: a purpose rejection ---------------------------------------------------------------------
    def reject_first(self, reason):
        """The server rejects the batch's first event with `reason` and acknowledges only that seq, so what follows
        stays pending; later batches are taken as usual."""
        def answer(doc):
            if self.server.batch_answer is None:
                return None
            self.server.batch_answer = None
            first = doc['events'][0]
            return 200, {'protocol': 'fabric-activity/0.1', 'device': doc['device'], 'batchId': doc['batchId'],
                         'received': '2026-10-19T00:00:00Z', 'accepted': 0, 'duplicates': 0,
                         'rejected': [{'eventId': first['eventId'], 'reason': reason}],
                         'acks': [{'collector': first['collector'], 'seq': first['seq']}]}
        self.server.batch_answer = answer

    def held(self):
        return set((self.make().state().get('purposeHolds') or {}))

    def bound(self, acknowledged='v1'):
        """An enrollment bound to a person who acknowledged a notice: the only one a release signal reaches."""
        self.server.user = 'm-4821'
        self.enroll()
        if acknowledged:
            self.server.acknowledged = acknowledged
            self.assertTrue(self.make().check_in()['ok'])

    def test_a_purpose_rejection_holds_that_kind_until_the_person_acknowledges_another_notice(self):
        self.bound()
        for kind in ('meter.call', 'meter.call', 'meter.other'):
            self.make().enqueue(kind, {'n': 1}, occurred='2026-10-18T00:00:00Z')
        self.reject_first('purpose')
        self.assertEqual(self.make().flush()['rejected'], 1)
        self.assertEqual(self.held(), {'meter.call'})
        self.assertEqual(self.make().state()['purposeHolds']['meter.call']['acknowledged'], 'v1')
        self.assertIsNone(self.make().enqueue('meter.call', {'n': 2}, occurred='2026-10-18T00:00:00Z'))
        self.assertIsNotNone(self.make().enqueue('meter.other', {'n': 2}, occurred='2026-10-18T00:00:00Z'))
        self.make().flush()                             # the pending meter.call is named dropped, never sent
        kinds = [k for _, _, ks in self.server.batches[1:] for k in ks]
        self.assertNotIn('meter.call', kinds)
        self.assertIn('buffer.overflow', kinds)
        self.assertEqual(kinds.count('meter.other'), 2)
        for _ in range(3):                              # never on a timer, nor on a response without the member
            self.now[0] += 86400
            self.server.acknowledged = None
            self.make().tick()
        self.assertEqual(self.held(), {'meter.call'})
        self.server.acknowledged = '2026-11'
        self.now[0] += 301
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.held(), set())
        self.assertIsNotNone(self.make().enqueue('meter.call', {'n': 3}, occurred='2026-10-18T00:00:00Z'))
        self.make().flush()
        self.assertEqual(self.server.batches[-1][2], ['meter.call'])

    def test_only_another_acknowledged_version_than_at_the_rejection_resumes(self):
        self.bound()
        self._three_events()
        self.reject_first('purpose')
        self.make().flush()
        self.assertEqual(self.make().state()['purposeHolds']['meter.call']['acknowledged'], 'v1')
        for said in ('v1', None):                       # the same version, then a value that becomes absent
            self.server.acknowledged = said
            self.now[0] += 301
            self.assertTrue(self.make().check_in()['ok'])
            self.assertEqual(self.held(), {'meter.call'}, said)
        self.server.acknowledged = 'v2'
        self.now[0] += 301
        self.make().check_in()
        self.assertEqual(self.held(), set())

    def assert_not_held(self, why):
        """A rejection no release signal could ever end is consumed like `before-purpose`: nothing held, the kind
        still collected and sent, and the refusal logged once per kind per day."""
        self._three_events()
        self.reject_first('purpose')
        self.assertEqual(self.make().flush()['rejected'], 1, why)
        self.assertEqual(self.held(), set(), why)
        self.assertIsNotNone(self.make().enqueue('meter.call', {'n': 9}, occurred='2026-10-18T00:00:00Z'))
        self.reject_first('purpose')
        self.make().flush()
        journal = (self.node / 'orgs' / ORG / 'journal.jsonl').read_text(encoding='utf-8')
        self.assertEqual(journal.count('"event": "purpose-refused"'), 1, why)   # once per kind per day
        self.assertNotIn('org:purpose-held', self.make().describe()['degraded'])

    def test_an_org_cloud_machine_never_holds(self):
        self.server.role = 'org-cloud'                  # DEC-0099: no person, no acknowledgement ever arrives
        self.enroll()
        self.assert_not_held('org-cloud')

    def test_a_machine_bound_to_no_person_never_holds(self):
        self.enroll()
        self.server.acknowledged = 'v1'                 # even a stray value: no person's acknowledgement
        self.make().check_in()
        self.assert_not_held('unassigned')

    def test_a_person_who_never_acknowledged_is_not_held(self):
        # A managed machine collecting under the mdm deadline before any acknowledgement: no signal has arrived, so
        # none is known to come.
        self.bound(acknowledged=None)
        self.assert_not_held('no acknowledgement yet')

    def test_a_buffer_overflow_is_never_held_and_adds_no_overflow(self):
        self.bound()
        with self.make()._db() as conn:
            self.make()._insert(conn, self.make().state()['epoch'], 'buffer.overflow',
                                {'dropped': {'events': 1, 'ranges': [{'fromSeq': 1, 'toSeq': 1}], 'byKind': []}},
                                occurred='2026-10-18T00:00:00Z')
        self.reject_first('purpose')
        self.make().flush()
        self.assertEqual(self.held(), set())
        with self.make()._db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = 'buffer.overflow' AND "
                                          "state = 'pending'").fetchone()['n'], 0)

    def test_status_doctor_and_health_show_every_held_kind(self):
        from node import org
        self.bound()
        self._three_events()
        self.reject_first('purpose')
        self.make().flush()
        status = self.make().describe()
        self.assertIn('org:purpose-held', status['degraded'])
        (row,) = status['purposeHolds']
        self.assertEqual(row['kind'], 'meter.call')
        self.assertEqual(row['waitsFor'], {'acknowledgedNoticeVersion': 'v1', 'noticeVersion': None})
        self.assertTrue(row['since'])
        (said,) = org.notices(self.node, now=self.clock())                      # doctor and Health read this
        self.assertEqual(said['purposeHolds'], status['purposeHolds'])
        source = (ROOT / 'dashboard' / 'build_dashboard.py').read_text(encoding='utf-8')
        self.assertIn('O.purposeHolds', source)

    def test_a_purpose_hold_survives_a_renewal_by_the_key_on_record(self):
        self.server.renewal, self.server.user = True, 'm-4821'
        old = self.enroll()
        self.server.acknowledged = 'v1'
        self.make().check_in()
        self._three_events()
        self.reject_first('purpose')
        self.make().flush()
        self.now[0] = strict_time(old['certificate']['notAfter']) / 1000 + 86400
        self.assertTrue(self.make().check_in()['ok'])                            # renewed, then checked in on v1
        state = self.make().state()
        self.assertIn('renewedAt', state)
        self.assertEqual((self.held(), state['acknowledgedNoticeVersion']), ({'meter.call'}, 'v1'))

    def test_a_later_notice_version_in_the_applied_policy_resumes(self):
        from unittest import mock
        from node.org import Enrollment
        self.bound()
        self._three_events()
        self.reject_first('purpose')
        self.make().flush()
        self.assertEqual(self.held(), {'meter.call'})
        self.assertIsNone(self.make().state()['purposeHolds']['meter.call']['noticeVersion'])
        with mock.patch.object(Enrollment, 'policy_layers',
                               lambda _self: [{'source': 'server', 'revision': 9, 'noticeVersion': '2026-12'}]):
            self.make().flush()
        self.assertEqual(self.held(), set())

    def test_a_notice_refusal_is_sent_again_when_the_applied_policy_names_another_notice(self):
        from unittest import mock
        from node.org import Enrollment
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self.server.share_answer = (403, {'error': {'code': 'purpose-not-collected', 'message': 'notice'}})
        self.make().register_shares()
        self.server.share_answer = None
        with mock.patch.object(Enrollment, 'policy_layers',
                               lambda _self: [{'source': 'mdm', 'revision': 2, 'noticeVersion': '2026-12'}]):
            self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().state()['noticeVersion'], '2026-12')
        with self.make()._db() as conn:
            self.assertEqual(conn.execute('SELECT gate FROM registrations').fetchone()['gate'], 'send')

    def test_before_purpose_and_a_reason_this_node_does_not_know_are_consumed_and_hold_nothing(self):
        fixture = json.loads((VECTORS / 'device' / 'activity-batch-ack-before-purpose.json').read_text())
        self.assertEqual(fixture['rejected'][0]['reason'], 'before-purpose')
        self.enroll()
        for reason in ('before-purpose', 'not-yet-defined', None, 7):
            with self.subTest(reason=reason):
                self.make().enqueue('meter.call', {'r': repr(reason)}, occurred='2026-10-18T00:00:00Z')
                self.reject_first(reason)
                self.assertEqual(self.make().flush()['rejected'], 1)
                self.assertEqual(self.held(), set())
        with self.make()._db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM events WHERE state = 'rejected'").fetchone()['n'],
                             4)

    def test_the_purpose_fixture_is_consumed_as_the_contract_sends_it(self):
        fixture = json.loads((VECTORS / 'device' / 'activity-batch-ack-purpose.json').read_text())
        self.assertEqual(fixture['rejected'], [{'eventId': fixture['rejected'][0]['eventId'], 'reason': 'purpose'}])
        self.bound()
        event = self.make().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        ack = dict(fixture, rejected=[dict(fixture['rejected'][0], eventId=event['eventId'])],
                   acks=[{'collector': event['collector'], 'seq': event['seq']}])
        self.server.batch_answer = lambda doc: (200, dict(ack, batchId=doc['batchId']))
        self.assertEqual(self.make().flush()['rejected'], 1)
        self.assertEqual(self.held(), {'meter.call'})

    def test_the_acknowledged_version_is_read_as_the_contracts_schema(self):
        good = json.loads((VECTORS / 'device' / 'device-check-in-response-acknowledged.json').read_text())
        bad = json.loads((VECTORS / 'device' / 'device-check-in-response-acknowledged-bad.json').read_text())
        self.enroll()
        self.server.acknowledged = bad['acknowledgedNoticeVersion']
        self.assertTrue(self.make().check_in()['ok'])
        self.assertNotIn('acknowledgedNoticeVersion', self.make().state())
        self.server.acknowledged = good['acknowledgedNoticeVersion']
        self.now[0] += 301
        self.make().check_in()
        self.assertEqual(self.make().state()['acknowledgedNoticeVersion'], good['acknowledgedNoticeVersion'])

    def test_a_notice_refusal_is_sent_again_when_the_acknowledged_notice_moves(self):
        self.server.protocols.append('share-registration/1')
        self.enroll()
        self.apply(share_record())
        self.server.share_answer = (403, {'error': {'code': 'purpose-not-collected', 'message': 'notice'}})
        self.assertEqual(self.make().register_shares()['answers'], {GRANT: 'hold-notice'})
        self.server.share_answer = None
        self.make().check_in()                          # no acknowledgement: nothing is sent again
        self.assertEqual(self.make().register_shares()['sent'], 0)
        self.server.acknowledged = '2026-11'
        sent = len([p for p in self.server.log if p == '/v1/devices/share-registrations'])
        self.now[0] += 301
        self.assertTrue(self.make().check_in()['ok'])   # sent again at once, not at the weekly resend
        self.assertEqual(len([p for p in self.server.log if p == '/v1/devices/share-registrations']), sent + 1)
        with self.make()._db() as conn:
            self.assertEqual(conn.execute('SELECT gate FROM registrations').fetchone()['gate'], 'send')

    # -- OBS-201: the device key and the enrollment are owner-only on every system -----------------------------
    def test_the_device_keys_and_the_enrollment_are_owner_only_on_this_system(self):
        import osprivacy
        self.enroll()
        self.make().check_in()
        self.make().enqueue('meter.call', {'n': 1}, occurred='2026-10-18T00:00:00Z')
        folder = self.node / 'orgs' / ORG
        files = [folder / 'keys' / name for name in ('sig.pem', 'kem.pem', 'device.json')] + \
            [folder / name for name in ('enrollment.json', 'outbox.sqlite', 'journal.jsonl')]
        for path in files + [folder, folder / 'keys']:
            with self.subTest(path.name):
                self.assertTrue(path.exists())
                self.assertTrue(osprivacy.private(path), osprivacy.explain(path))
                self.assertFalse(osprivacy.others_may_write(path), osprivacy.explain(path))
                self.assertTrue(osprivacy.owned_by_me(path), osprivacy.explain(path))
                if os.name == 'nt':                    # Windows keeps no mode bits: the access list is the proof
                    self.assertEqual(osprivacy.describe(path), 'owner-only')
                else:
                    want = 0o700 if path.is_dir() else 0o600
                    self.assertEqual(path.stat().st_mode & 0o777, want, oct(path.stat().st_mode))
        self.assertNotEqual(self.make().keys.sign(b'x'), '')                    # the key loads back from its file


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


    # -- the contract pin (CT-65, DEC-0094; the commit is node-vectors/README.md's OBS-161 pin) --------------------
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
