#!/usr/bin/env python3
"""The node's estate record (OBS-109, RM-21 P5 slice 3a): identifier equality, Ed25519 small-order refusal, offline
passkey assertions and `verify_estate_record`, run over the contract's vectors (`identifiers.json`,
`estate-record.json`), then each of the record's own rules on a revision re-signed with the vectors' EdDSA test
passkey. A node trusts its peers only from a revision this accepts, so the last test feeds an accepted record to
`relay.peer_keys`.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    HAVE_CRYPTO = True
except ImportError:                                      # the engine's `full` extra
    HAVE_CRYPTO = False

from node import ed25519, estate, p256, relay, webauthn  # noqa: E402
from node.identifiers import normal_identifier, same_identifier  # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors(name: str) -> dict:
    return json.loads((VECTORS / (name + '.json')).read_text(encoding='utf-8'))


def verify(record, trust):
    kw = {'at_ms': relay.strict_time(trust['at'])}
    if 'parent' in trust:
        kw['parent'] = trust['parent']
    else:
        kw['authorities'] = trust.get('authorities')
    return estate.verify_estate_record(record, **kw)


class Identifiers(unittest.TestCase):
    def test_vectors(self):
        data = vectors('identifiers')
        for case in data['normal'] + data['laxPorts']:
            with self.subTest(case['name']):
                self.assertEqual(normal_identifier(case['input']), case['expected'])
        for a, b in data['same']:
            self.assertTrue(same_identifier(a, b), (a, b))
        for a, b in data['different']:
            self.assertFalse(same_identifier(a, b), (a, b))
        self.assertIsNone(normal_identifier(7))


class SmallOrder(unittest.TestCase):
    def test_every_small_order_encoding_and_its_sign_bit_is_refused(self):
        for encoding in ed25519.SMALL_ORDER_ENCODINGS:
            raw = bytes.fromhex(encoding)
            self.assertTrue(ed25519.is_small_order_key(raw))
            self.assertTrue(ed25519.is_small_order_key(raw[:31] + bytes([raw[31] | 0x80])))
        self.assertTrue(ed25519.is_small_order_key(b'\x01' * 31))
        self.assertFalse(ed25519.is_small_order_key(webauthn.node_b64url(vectors('estate-record')['keys']['hub']['x'])))


@needs_crypto
class EstateVectors(unittest.TestCase):
    def test_digest_and_challenge(self):
        data = vectors('estate-record')
        self.assertEqual(estate.estate_record_digest(data['digest']['record']), data['digest']['expected'])
        challenge = data['challenge']
        self.assertEqual(estate.estate_challenge_input(challenge['digest']).decode(), challenge['input'])
        self.assertEqual(p256.b64url_encode(estate.estate_challenge(challenge['digest'])), challenge['challenge'])

    def test_every_verdict(self):
        for case in vectors('estate-record')['cases']:
            with self.subTest(case['name']):
                self.assertEqual(verify(case['record'], case['trust']), case['expected'])


def ed_passkey():
    keys = vectors('estate-record')['keys']['passkeyEdDSA']
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(keys['seed'])), keys['authority']


def resign(record: dict, *, rp_id='sign.example.org', origin='https://sign.example.org', flags=0x05,
           client_extra=None) -> dict:
    """The record with its digest recomputed and an EdDSA assertion over its challenge, as a signer makes one."""
    key, authority = ed_passkey()
    body = {k: v for k, v in record.items() if k not in ('digest', 'signature')}
    digest = estate.estate_record_digest(body)
    auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + (7).to_bytes(4, 'big')
    client = json.dumps({'type': 'webauthn.get', 'challenge': p256.b64url_encode(estate.estate_challenge(digest)),
                         'origin': origin, 'crossOrigin': False, **(client_extra or {})},
                        separators=(',', ':')).encode()
    sig = key.sign(auth_data + hashlib.sha256(client).digest())
    return dict(body, digest=digest, signature={
        'format': 'webauthn-assertion', 'credentialId': authority['credentialId'], 'alg': 'EdDSA',
        'authenticatorData': p256.b64url_encode(auth_data), 'clientDataJSON': p256.b64url_encode(client),
        'sig': p256.b64url_encode(sig)})


@needs_crypto
class RecordRules(unittest.TestCase):
    """Each rule on a revision 1 re-signed by the pinned EdDSA passkey: the signature holds, the rule refuses."""

    def setUp(self):
        self.base = copy.deepcopy(vectors('estate-record')['cases'][0]['record'])
        _, self.authority = ed_passkey()
        self.base['authorities'] = [self.authority]
        self.trust = {'at': '2026-10-13T00:00:00Z', 'authorities': [self.authority]}

    def verdict(self, mutate):
        record = copy.deepcopy(self.base)
        mutate(record)
        return verify(resign(record), self.trust)

    def test_the_base_revision_is_accepted(self):
        answer = verify(resign(self.base), self.trust)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual((answer['revision'], answer['signedBy'], answer['signCount']),
                         (1, self.authority['credentialId'], 7))

    def test_each_rule(self):
        machine = lambda r: r['machines'][0]
        cases = {
            'FAC-SEM-073 two signing keys': (lambda r: machine(r)['deviceKeys'].append(
                dict(machine(r)['deviceKeys'][0])), 'FAC-SEM-073'),
            'FAC-SEM-073 a key named for another machine': (lambda r: machine(r)['deviceKeys'][0].update(
                kid='m-other#sig'), 'FAC-SEM-073'),
            'FAC-SEM-073 an off-curve device key': (lambda r: machine(r)['deviceKeys'][0].update(
                publicKey=base64.b64encode(b'\x04' + b'\x00' * 64).decode()), 'FAC-SEM-073'),
            'FAC-SEM-073 a small-order hub key': (lambda r: machine(r)['hubKey'].update(x='A' * 43), 'FAC-SEM-073'),
            'FAC-SEM-074 an unpinned publisher key': (lambda r: machine(r)['packages'][0].update(
                publisherKid='nobody'), 'FAC-SEM-074'),
            'FAC-SEM-075 a secret value in a placement': (lambda r: machine(r)['secrets'][0].update(
                slot='sk-' + 'a1B2' * 6), 'FAC-SEM-075'),
            'FAC-SEM-076 a grant listed twice': (lambda r: r['shareGrants'].append(copy.deepcopy(r['shareGrants'][0])),
                                                 'FAC-SEM-076'),
            'FAC-SEM-110 a share grant used in the personal context': (lambda r: r['shareGrants'][0].update(
                context='personal'), 'FAC-SEM-110'),
            'FAC-SEM-111 an organization instance without its grant': (lambda r: machine(r)['packages'][0].update(
                context='org:acme'), 'FAC-SEM-111'),
            # DEC-0090: a revision that scopes the key of a personal placement to an organization is refused whole.
            'FAC-SEM-122 a partner key outside its scope': (lambda r: r.update(publisherScopes=[{
                'kid': machine(r)['packages'][0]['publisherKid'], 'partner': 'org:acme_partners',
                'contexts': ['org:org_7kq2m4xw']}]), 'FAC-SEM-122'),
            'FAC-SEM-122 a scope naming a key the record does not pin': (lambda r: r.update(publisherScopes=[{
                'kid': 'partner-unpinned', 'partner': 'org:acme_partners', 'contexts': ['org:org_7kq2m4xw']}]),
                'FAC-SEM-122'),
            'FAC-SEM-092 a pinned origin outside the signing RP': (lambda r: r['authorities'][0].update(
                origins=['https://sign.example.org', 'https://evil.example']), 'FAC-SEM-092'),
        }
        for name, (mutate, code) in cases.items():
            with self.subTest(name):
                answer = self.verdict(mutate)
                self.assertEqual(answer.get('problem'), 'rule-violation', answer)
                self.assertIn(code, answer['codes'])

    def test_assertions_refused_by_their_ceremony(self):
        record = copy.deepcopy(self.base)
        self.assertEqual(verify(resign(record, flags=0x01), self.trust)['problem'], 'no-user-verification')
        self.assertEqual(verify(resign(record, origin='https://example.org'), self.trust)['problem'], 'wrong-origin')
        self.assertEqual(verify(resign(record, rp_id='example.org'), self.trust)['problem'], 'wrong-rp')
        # A ceremony inside a frame names its top origin even with crossOrigin false: refused all the same.
        framed = resign(record, client_extra={'topOrigin': 'https://sign.example.org'})
        self.assertEqual(verify(framed, self.trust)['problem'], 'cross-origin')

    def test_client_data_nested_too_deep_is_malformed(self):
        record = resign(copy.deepcopy(self.base))
        deep = '{"type":"webauthn.get","x":' + '[' * 5000 + ']' * 5000 + '}'
        record['signature']['clientDataJSON'] = p256.b64url_encode(deep.encode())
        self.assertEqual(verify(record, self.trust), {'ok': False, 'problem': 'malformed'})

    def test_a_revision_chain(self):
        first = resign(self.base)
        second = copy.deepcopy(first)
        second.update(revision=2, parent=first['digest'], issuedAt='2026-10-13T09:00:00Z',
                      validUntil='2026-11-12T09:00:00Z',
                      revocations=[{'kind': 'machine', 'ref': 'm-gone', 'at': '2026-10-13T09:00:00Z'}])
        signed = resign(second)
        self.assertTrue(verify(signed, {'at': '2026-10-14T00:00:00Z', 'parent': first})['ok'])
        self.assertEqual(estate.revision_chain({'parent': first, 'next': signed}), [])
        # A third revision that drops the machine revocation is refused, by the verifier and by the rule.
        third = copy.deepcopy(signed)
        third.update(revision=3, parent=signed['digest'], revocations=[])
        refused = resign(third)
        self.assertEqual(verify(refused, {'at': '2026-10-14T00:00:00Z', 'parent': signed})['problem'], 'broken-chain')
        self.assertEqual([f['code'] for f in estate.revision_chain({'parent': signed, 'next': refused})],
                         ['FAC-SEM-072'])

    def test_peers_come_only_from_an_accepted_revision(self):
        record = resign(self.base)
        self.assertTrue(verify(record, self.trust)['ok'])
        peers = relay.peer_keys(record, relay.strict_time('2026-10-13T00:00:00Z'))
        self.assertEqual(sorted(peers['keys']), ['m-4c1e#sig'])
        self.assertIsNone(p256.key_refusal(peers['keys']['m-4c1e#sig']))
        self.assertEqual(estate.placement_trust(record, 'm-4c1e', 'urn:fabric:agent:example:inbox-digest'),
                         {'publisher': record['publisherKeys']})
        self.assertIsNone(estate.placement_trust(record, 'm-4c1e', 'urn:fabric:agent:example:inbox-digest',
                                                 'org:acme'))


if __name__ == '__main__':
    unittest.main()
