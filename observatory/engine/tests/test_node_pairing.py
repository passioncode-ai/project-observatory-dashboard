#!/usr/bin/env python3
"""The estate ceremonies on the node (OBS-133, RM-21 P5 slice 5; contract DEC-0084): pairing a new machine with a
trusted one, the comparison code, the estate fingerprint, the one-time rendezvous, the checks a node runs before it
pins an estate and applies a revision that adds or re-keys it, and FAC-SEM-114 (what a revision drops, it revokes).

Every case of the contract's `pairing.json` and every estate fixture of `semantic/` gives the reference answer. Then
the add-a-machine and replace-a-key ceremonies run end to end between two device-key sets, with the revision signed by
the vectors' published EdDSA test passkey (a test estate, never the real ceremony).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import estate, p256, pairing, relay           # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors() -> dict:
    return json.loads((VECTORS / 'pairing.json').read_text(encoding='utf-8'))


@needs_crypto
class PairingVectors(unittest.TestCase):
    def test_commits(self):
        for case in vectors()['commits']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.pairing_commit(case['side']), case['answer'])
                if case.get('input') and case['answer']['ok']:
                    side = case['side']
                    covered = {k: side[k] for k in ('keys', 'nonce', 'genesis') if k in side}
                    text = json.loads(case['input'])
                    self.assertEqual(text, dict(covered, type=pairing.COMMIT_TYPE, role=side['role']))

    def test_reveals(self):
        for case in vectors()['reveals']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.check_reveal(case['commit'], case['reveal'], case.get('expected')),
                                 case['answer'])

    def test_codes_and_their_transcripts(self):
        for case in vectors()['codes']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.comparison_code(case['new'], case['trusted']), case['answer'])
        for case in vectors()['checkedCodes']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.checked_comparison_code(case['newCommit'], case['new'],
                                                                 case['trustedCommit'], case['trusted']),
                                 case['answer'])

    def test_fingerprints(self):
        for case in vectors()['fingerprints']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.estate_fingerprint(case['digest']), case['answer'])

    def test_rendezvous(self):
        for case in vectors()['rendezvous']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.rendezvous_verdict(case['openedAt'], case['messages'],
                                                            relay.instant(case['at'])), case['answer'])

    def test_join_and_own_keys(self):
        for case in vectors()['join']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.join_listed(case['record'], case['new'], case['trusted'], case['genesis']),
                                 case['answer'])
        for case in vectors()['ownKeys']:
            with self.subTest(case['name']):
                self.assertEqual(pairing.own_keys_decision(case['record'], case['machine'], case['held']),
                                 case['answer'])


class SemanticFixtures(unittest.TestCase):
    def test_every_estate_fixture(self):
        fixtures = sorted((VECTORS / 'semantic').glob('*.json'))
        self.assertGreaterEqual(len(fixtures), 74)
        # DEC-0090 (OBS-171): the partner channel's FAC-SEM-122 fixtures are among them.
        self.assertGreaterEqual(len([p for p in fixtures if p.name.startswith('estate-partner-')]), 5)
        for path in fixtures:
            fixture = json.loads(path.read_text(encoding='utf-8'))
            with self.subTest(path.name):
                codes = sorted({f['code'] for f in estate.estate_record_rules(fixture['kind'], fixture['input'])})
                self.assertEqual(codes, sorted(fixture['expect']))

    def test_a_partners_publisher_key_is_trusted_only_in_its_scope(self):
        """DEC-0090 (FAC-SEM-122), the contract's `test/contexts.test.ts` cases: `placement_trust` hands a partner's key
        only for a placement in a context its scope names, and gives no key elsewhere, so `verifyPackage` refuses the
        package `unknown-key` (PK-6); an installation under it outside its scope is refused."""
        partner = json.loads((VECTORS / 'semantic' / 'estate-partner-key-ok.json').read_text(encoding='utf-8'))['input']
        setup, agent, org_a, org_b = ('urn:fabric:agent:acme:setup-desk', 'urn:fabric:agent:example:inbox-digest',
                                      'org:org_7kq2m4xw', 'org:org_3fh9p1zc')
        key = next(k for k in partner['publisherKeys'] if k['kid'] == 'partner-acme-2026')
        self.assertEqual(estate.placement_trust(partner, 'm-4c1e', setup, org_a), {'publisher': [key]})
        moved = copy.deepcopy(partner)
        moved['machines'][0]['packages'][3]['context'] = org_b
        self.assertEqual(estate.placement_trust(moved, 'm-4c1e', setup, org_b), {'publisher': []})
        # A key no scope names keeps DEC-0051's meaning, in any context.
        self.assertEqual(estate.placement_trust(partner, 'm-4c1e', agent, org_a), {'publisher': [partner['publisherKeys'][0]]})
        self.assertEqual(estate.placement_trust(partner, 'm-4c1e', agent), {'publisher': [partner['publisherKeys'][0]]})
        self.assertIsNone(estate.placement_trust(partner, 'm-4c1e', setup, org_b))
        moved['machines'][0]['packages'][3]['grant'] = next(g['meta']['id'] for g in moved['shareGrants']
                                                            if g.get('context') == org_b)
        package = {'agentRef': setup}
        codes = lambda value: {f['code'] for f in estate.estate_record_rules('estate-placement', value)}
        self.assertIn('FAC-SEM-122', codes({'record': moved, 'machine': 'm-4c1e', 'package': package, 'context': org_b}))
        self.assertNotIn('FAC-SEM-122', codes({'record': partner, 'machine': 'm-4c1e', 'package': package,
                                               'context': org_a}))
        paths = [f['instancePath'] for f in estate.estate_record_rules(
            'estate-placement', {'record': moved, 'machine': 'm-4c1e', 'package': package, 'context': org_b})
            if f['code'] == 'FAC-SEM-122']
        self.assertEqual(paths, ['/record/machines/0/packages/3/publisherKid'])

    def test_a_scope_is_read_as_the_reference_reads_it(self):
        """`publisherScopes` entries that are not objects are skipped, a scope whose `contexts` is not a list admits
        no context, a key is compared by value (a boolean never equals a number), and a record without the member reads
        as before DEC-0090."""
        partner = json.loads((VECTORS / 'semantic' / 'estate-partner-key-ok.json').read_text(encoding='utf-8'))['input']
        setup, org_a = 'urn:fabric:agent:acme:setup-desk', 'org:org_7kq2m4xw'
        odd = copy.deepcopy(partner)
        odd['publisherScopes'] = [7, 'x', None] + odd['publisherScopes']
        self.assertEqual(len(estate.placement_trust(odd, 'm-4c1e', setup, org_a)['publisher']), 1)
        odd['publisherScopes'][-1]['contexts'] = org_a
        self.assertEqual(estate.placement_trust(odd, 'm-4c1e', setup, org_a), {'publisher': []})
        scoped_one = {'publisherScopes': [{'kid': 1, 'contexts': []}]}
        self.assertFalse(estate._scope_admits(scoped_one, 1.0, org_a))
        self.assertTrue(estate._scope_admits(scoped_one, True, org_a))
        self.assertTrue(estate._scope_admits(scoped_one, 'k', org_a))
        self.assertTrue(estate._scope_admits({}, 'partner-acme-2026', 'personal'))
        bare = copy.deepcopy(partner)
        del bare['publisherScopes']
        self.assertNotIn('FAC-SEM-122', {f['code'] for f in estate.estate_record_rules('estate-record', bare)})
        twice = estate.estate_record_rules('estate-record', dict(partner, publisherScopes=partner['publisherScopes'] * 2))
        self.assertEqual([f['instancePath'] for f in twice if f['code'] == 'FAC-SEM-122'], ['/publisherScopes/1/kid'])

    def test_a_secret_value_as_slot_name(self):
        """The contract's `estate-secret-value-as-slot` fixture, rebuilt here: its slot is the AWS documentation's example
        access key id, a shape the public-release gate refuses, so that one fixture is not vendored (README)."""
        value = json.loads((VECTORS / 'semantic' / 'estate-record-ok.json').read_text(encoding='utf-8'))['input']
        value = copy.deepcopy(value)
        value['machines'][0]['secrets'][0]['slot'] = 'AKIA' + 'IOSFODNN7EXAMPLE'
        value['digest'] = estate.estate_record_digest(value)
        codes = sorted({f['code'] for f in estate.estate_record_rules('estate-record', value)})
        self.assertEqual(codes, ['FAC-SEM-075'])

    def test_a_slot_name_that_is_not_a_string(self):
        """FAC-SEM-075 compares slot names as the reference's Set does: by value, an object never matching, and never
        raising on a value JSON allows (a fetched package is untrusted)."""
        base = json.loads((VECTORS / 'semantic' / 'estate-placement-ok.json').read_text(encoding='utf-8'))['input']
        index = next(i for i, m in enumerate(base['record']['machines']) if m['id'] == base['machine'])
        for odd in ({'n': 1}, ['x'], 7, True, None):
            value = copy.deepcopy(base)
            value['record']['machines'][index]['secrets'][0]['slot'] = odd
            with self.subTest(odd=odd):
                codes = {f['code'] for f in estate.estate_record_rules('estate-placement', value)}
                self.assertIn('FAC-SEM-075', codes)
        # A number declared and a number placed match by value, as in JavaScript; a boolean never matches a number.
        self.assertTrue(estate._same_value(7, 7.0))
        self.assertFalse(estate._same_value(True, 1))
        self.assertFalse(estate._same_value({'a': 1}, {'a': 1}))

    def test_json_equal_keeps_booleans_apart_from_numbers(self):
        self.assertFalse(pairing.json_equal(True, 1))
        self.assertFalse(pairing.json_equal({'a': False}, {'a': 0}))
        self.assertFalse(pairing.json_equal([1, True], [1, 1]))
        self.assertTrue(pairing.json_equal(1, 1.0))
        self.assertTrue(pairing.json_equal({'a': [1, {'b': None}]}, {'a': [1, {'b': None}]}))

    def test_a_padded_key_with_stray_bits_is_not_canonical(self):
        key = {'kid': 'm-a#sig', 'use': 'sig', 'alg': 'es256',
               'publicKey': 'BGsX0fLhLEJH+Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT+NC4v4af5uO5+tKfA+eFivOM1drMV7Oy7ZAaDe/UfU='}
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        last = key['publicKey'][-2]
        stray = key['publicKey'][:-2] + alphabet[alphabet.index(last) + 1] + '='   # same bytes, a stray low bit set
        self.assertEqual(base64.b64decode(stray), base64.b64decode(key['publicKey']))
        self.assertIsNone(estate.device_key_fingerprint(dict(key, publicKey=stray)))

    def test_node_b64url_is_total(self):
        from node import webauthn
        for text in ('abc!', 'a=b', 'AB=C', 'a b', '\u00e9\u00e9', '', '=='):
            with self.subTest(text=text):
                self.assertIsInstance(webauthn.node_b64url(text), bytes)

    def test_a_package_whose_digest_lies(self):
        """The package's own `digest` must be its digest, even when the record places the bytes it really has."""
        value = json.loads((VECTORS / 'semantic' / 'estate-placement-ok.json').read_text(encoding='utf-8'))['input']
        self.assertEqual(estate.estate_record_rules('estate-placement', value), [])
        lying = copy.deepcopy(value)
        lying['package']['digest'] = 'sha256:' + '0' * 64
        paths = [f['instancePath'] for f in estate.estate_record_rules('estate-placement', lying)]
        self.assertEqual(paths, ['/package/digest'])
        self.assertEqual(estate.package_digest(value['package']), value['package']['digest'])

    def test_key_references(self):
        key = {'kid': 'm-a#sig', 'use': 'sig', 'alg': 'es256',
               'publicKey': 'BGsX0fLhLEJH+Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT+NC4v4af5uO5+tKfA+eFivOM1drMV7Oy7ZAaDe/UfU='}
        fingerprint = estate.device_key_fingerprint(key)
        self.assertRegex(fingerprint, r'^sha256:[0-9a-f]{64}$')
        self.assertEqual(estate.device_key_ref(key), 'm-a#sig@' + fingerprint)
        self.assertTrue(estate.KEY_REFERENCE.fullmatch(estate.device_key_ref(key)))
        for text in (key['publicKey'][:-1], key['publicKey'].rstrip('=') + 'A=', '', 7):
            self.assertIsNone(estate.device_key_fingerprint(dict(key, publicKey=text)))


@needs_crypto
class Ceremonies(unittest.TestCase):
    """Add a machine, then replace its key, between two device-key sets, on a test estate."""

    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from node.device_keys import DeviceKeys
        data = json.loads((VECTORS / 'estate-record.json').read_text(encoding='utf-8'))
        self.passkey = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data['keys']['passkeyEdDSA']['seed']))
        self.authority = data['keys']['passkeyEdDSA']['authority']
        self.trusted, self.new = DeviceKeys.generate(), DeviceKeys.generate()
        self.now = relay.strict_time('2026-10-15T12:00:00Z')

    def sign(self, body: dict) -> dict:
        digest = estate.estate_record_digest(body)
        auth_data = hashlib.sha256(self.authority['rpId'].encode()).digest() + bytes([0x05]) + (1).to_bytes(4, 'big')
        client = json.dumps({'type': 'webauthn.get', 'challenge': p256.b64url_encode(estate.estate_challenge(digest)),
                             'origin': self.authority['origins'][0], 'crossOrigin': False},
                            separators=(',', ':')).encode()
        return dict(body, digest=digest, signature={
            'format': 'webauthn-assertion', 'credentialId': self.authority['credentialId'], 'alg': 'EdDSA',
            'authenticatorData': p256.b64url_encode(auth_data), 'clientDataJSON': p256.b64url_encode(client),
            'sig': p256.b64url_encode(self.passkey.sign(auth_data + hashlib.sha256(client).digest()))})

    def revision(self, number: int, machines: list, parent=None, revocations=()) -> dict:
        body = {'schema': estate.ESTATE_RECORD_SCHEMA, 'estate': 'estate-test', 'revision': number,
                'issuedAt': relay.rfc3339(self.now - 60_000 + number * 1000),
                'validUntil': relay.rfc3339(self.now + 86_400_000), 'authorities': [self.authority],
                'machines': machines, 'revocations': list(revocations)}
        if parent is not None:
            body['parent'] = parent['digest']
        return self.sign(body)

    @staticmethod
    def nonce() -> str:
        import os
        return p256.b64url_encode(os.urandom(32))

    def pair(self, genesis: str, new_keys: list):
        new_side = {'role': 'new', 'keys': new_keys, 'nonce': self.nonce()}
        trusted_side = {'role': 'trusted', 'keys': self.trusted.enrollment_keys(), 'nonce': self.nonce(),
                        'genesis': genesis}
        commits, reveals = {}, {}
        for side in (new_side, trusted_side):
            commits[side['role']] = {'schema': pairing.PAIRING_SCHEMA, 'step': 'commit', 'role': side['role'],
                                     'commit': pairing.pairing_commit(side)['commit']}
        for side in (new_side, trusted_side):
            reveals[side['role']] = dict({'schema': pairing.PAIRING_SCHEMA, 'step': 'reveal'}, **side)
        return commits, reveals

    def test_add_a_machine_end_to_end(self):
        first = self.revision(1, [{'id': self.trusted.device_id, 'deviceKeys': self.trusted.enrollment_keys()}])
        self.assertTrue(estate.verify_estate_record(first, at_ms=self.now, authorities=[self.authority])['ok'])
        genesis = first['digest']
        commits, reveals = self.pair(genesis, self.new.enrollment_keys())
        messages = [commits['new'], commits['trusted'], reveals['new'], reveals['trusted']]
        self.assertEqual(pairing.rendezvous_verdict(relay.rfc3339(self.now), messages, self.now + 5000),
                         {'state': 'complete'})
        code_new = pairing.checked_comparison_code(commits['new'], reveals['new'], commits['trusted'],
                                                   reveals['trusted'])
        code_trusted = pairing.checked_comparison_code(commits['new'], reveals['new'], commits['trusted'],
                                                       reveals['trusted'])
        self.assertTrue(code_new['ok'])
        self.assertEqual(code_new, code_trusted)                 # both screens show one code
        second = self.revision(2, [{'id': self.trusted.device_id, 'deviceKeys': self.trusted.enrollment_keys()},
                                   {'id': self.new.device_id, 'deviceKeys': self.new.enrollment_keys()}],
                               parent=first)
        self.assertTrue(estate.verify_estate_record(second, at_ms=self.now, parent=first)['ok'])
        self.assertEqual(pairing.join_listed(second, reveals['new'], reveals['trusted'], genesis), {'ok': True})
        self.assertEqual(pairing.own_keys_decision(second, self.new.device_id, self.new.enrollment_keys()),
                         {'action': 'apply', 'retire': []})
        # A chain of another estate that copies the trusted key and the public passkey cannot start at this genesis.
        self.assertEqual(pairing.join_listed(second, reveals['new'], reveals['trusted'], 'sha256:' + '0' * 64),
                         {'ok': False, 'problem': 'other-estate'})
        # An attacker in the middle substitutes its own key: the codes differ, and the person signs nothing.
        from node.device_keys import DeviceKeys
        attacker = DeviceKeys.generate()
        forged_commits, forged = self.pair(genesis, attacker.enrollment_keys())
        forged_code = pairing.checked_comparison_code(forged_commits['new'], forged['new'], commits['trusted'],
                                                      reveals['trusted'])
        self.assertNotEqual(forged_code, code_new)
        self.assertRegex(pairing.estate_fingerprint(genesis)['fingerprint'], r'^(\d{5} ){4}\d{5}$')

    def test_replace_a_key_end_to_end(self):
        both = [{'id': self.trusted.device_id, 'deviceKeys': self.trusted.enrollment_keys()},
                {'id': self.new.device_id, 'deviceKeys': self.new.enrollment_keys()}]
        first = self.revision(1, both)
        from node.device_keys import DeviceKeys
        rekeyed = DeviceKeys.generate(self.new.device_id)          # the same machine id, new keys
        replaced = [both[0], {'id': self.new.device_id, 'deviceKeys': rekeyed.enrollment_keys()}]
        unrevoked = self.revision(2, replaced, parent=first)
        answer = estate.verify_estate_record(unrevoked, at_ms=self.now, parent=first)
        self.assertEqual((answer['problem'], answer['codes']), ('rule-violation', ['FAC-SEM-114']))
        old_refs = [{'kind': 'device-key', 'ref': estate.device_key_ref(k)} for k in self.new.enrollment_keys()]
        second = self.revision(2, replaced, parent=first, revocations=old_refs)
        self.assertTrue(estate.verify_estate_record(second, at_ms=self.now, parent=first)['ok'])
        # The machine that still holds the old keys stops; the one holding the new keys applies and retires nothing.
        self.assertEqual(pairing.own_keys_decision(second, self.new.device_id, self.new.enrollment_keys()),
                         {'action': 'stop', 'reason': 'keys-replaced'})
        held = rekeyed.enrollment_keys() + self.new.enrollment_keys()
        decision = pairing.own_keys_decision(second, self.new.device_id, held)
        self.assertEqual(decision['action'], 'apply')
        self.assertEqual(sorted(decision['retire']), sorted(r['ref'] for r in old_refs))
        # Revoking the kid instead would revoke the new key too: FAC-SEM-076 refuses it.
        by_kid = self.revision(2, replaced, parent=first,
                               revocations=[{'kind': 'device-key', 'ref': self.new.sig_kid}])
        self.assertIn('FAC-SEM-076', estate.verify_estate_record(by_kid, at_ms=self.now, parent=first)['codes'])

    def test_remove_a_machine(self):
        both = [{'id': self.trusted.device_id, 'deviceKeys': self.trusted.enrollment_keys()},
                {'id': self.new.device_id, 'deviceKeys': self.new.enrollment_keys()}]
        first = self.revision(1, both)
        dropped = self.revision(2, both[:1], parent=first)
        self.assertEqual(estate.verify_estate_record(dropped, at_ms=self.now, parent=first)['codes'], ['FAC-SEM-114'])
        removed = self.revision(2, both[:1], parent=first, revocations=[{'kind': 'machine', 'ref': self.new.device_id}])
        self.assertTrue(estate.verify_estate_record(removed, at_ms=self.now, parent=first)['ok'])
        self.assertEqual(pairing.own_keys_decision(removed, self.new.device_id, self.new.enrollment_keys()),
                         {'action': 'stop', 'reason': 'removed'})
        self.assertNotIn(self.new.sig_kid, relay.peer_keys(removed, self.now)['keys'])


if __name__ == '__main__':
    unittest.main()
