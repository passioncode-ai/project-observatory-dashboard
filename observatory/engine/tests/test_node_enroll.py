#!/usr/bin/env python3
"""`enroll-token/1` (DEC-0084, RM-21 P5): the headless join. The node's port of the contract's verifier and single-use
redemption runs over every case of `enroll-token.json`; the join proof a node makes is redeemed by the reference port;
the token is read from stdin or an owner-only file and never from the command line, a URL or the environment; and
FAC-SEM-113 names what is wrong with a token's statement."""
from __future__ import annotations

import copy
import io
import json
import os
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

from node import enroll_token, p256, relay, webauthn   # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors(name: str = 'enroll-token') -> dict:
    return json.loads((VECTORS / (name + '.json')).read_text(encoding='utf-8'))


def first_record() -> dict:
    """`fixtures/positive/estate-record-first.json`, which `estate-record.json` carries as its digest case."""
    return vectors('estate-record')['digest']['record']


def ms(text: str) -> int:
    value = relay.instant(text)
    assert value is not None, text
    return value


def valid_token() -> dict:
    return vectors()['verify'][0]['token']


@needs_crypto
class Verify(unittest.TestCase):
    def test_every_verify_case(self):
        data = vectors()
        self.assertEqual(len(data['verify']), 30)
        for case in data['verify']:
            with self.subTest(case['name']):
                record = case.get('record', first_record())
                answer = enroll_token.verify_enroll_token(case['token'], record, data['genesis'], ms(case['at']))
                self.assertEqual(answer, case['answer'])

    def test_the_statement_bytes_challenge_and_join_input(self):
        data = vectors()
        fields = {k: v for k, v in data['statement']['fields'].items() if k != 'type'}
        self.assertEqual(webauthn.signing_statement('enroll-token/1', fields).decode(), data['statement']['bytes'])
        self.assertEqual(p256.b64url_encode(webauthn.signing_challenge('enroll-token/1', fields)),
                         data['statement']['challenge'])
        self.assertEqual(enroll_token.enroll_join_input(valid_token()).decode(), data['statement']['join'])

    def test_no_clock_is_checked_first(self):
        for at in (None, '2026-10-15T09:01:00Z', True, 1.5e12):
            self.assertEqual(enroll_token.verify_enroll_token(None, None, None, at), {'ok': False, 'problem': 'no-clock'})

    def test_a_record_without_an_estate_is_malformed(self):
        genesis = vectors()['genesis']
        for record in (None, [], {'estate': 1}, {}):
            self.assertEqual(enroll_token.verify_enroll_token(valid_token(), record, genesis, ms('2026-10-15T09:01:00Z')),
                             {'ok': False, 'problem': 'malformed'})

    def test_uses_one_spelled_as_a_boolean_is_no_register_statement(self):
        token = copy.deepcopy(valid_token())
        token['statement']['uses'] = True
        self.assertIsNone(enroll_token.enroll_join_input(token))
        token['statement']['uses'] = 1.0
        self.assertIsNotNone(enroll_token.enroll_join_input(token))

    def test_a_digest_with_a_trailing_newline_is_no_digest(self):
        token = copy.deepcopy(valid_token())
        token['statement']['genesis'] += '\n'
        self.assertIsNone(enroll_token.enroll_join_input(token))
        self.assertIsNone(webauthn.signing_statement('estate-revision/1', {'digest': 'sha256:' + 'a' * 64 + '\n'}))


@needs_crypto
class Redeem(unittest.TestCase):
    def test_every_redeem_case(self):
        data = vectors()
        self.assertEqual(len(data['redeem']), 12)
        for case in data['redeem']:
            with self.subTest(case['name']):
                ledger = {nonce: entry for nonce, entry in case['ledger']}
                before = copy.deepcopy(ledger)
                answer = enroll_token.redeem_enroll_token(case['token'], case['machineKey'], case['proof'], ledger)
                self.assertEqual(answer, case['answer'])
                self.assertEqual(ledger, before)             # the hub writes its ledger, never this function

    def test_no_ledger_map_is_malformed(self):
        case = vectors()['redeem'][0]
        for ledger in (None, [], 'ledger'):
            self.assertEqual(enroll_token.redeem_enroll_token(case['token'], case['machineKey'], case['proof'], ledger),
                             {'ok': False, 'problem': 'malformed'})

    def test_a_proof_in_another_spelling_is_no_proof(self):
        case = vectors()['redeem'][0]
        for proof in (case['proof'] + '=', case['proof'][:-1] + 'B', case['proof'][:40] + '.' + case['proof'][41:], None, 7):
            self.assertEqual(enroll_token.redeem_enroll_token(case['token'], case['machineKey'], proof, {}),
                             {'ok': False, 'problem': 'bad-proof'}, proof)

    def test_a_null_ledger_entry_is_a_use(self):
        case = vectors()['redeem'][0]
        nonce = case['answer']['record']['nonce']
        answer = enroll_token.redeem_enroll_token(case['token'], case['machineKey'], case['proof'], {nonce: None})
        self.assertEqual(answer['problem'], 'reused')
        self.assertEqual(answer['log']['boundTo'], 'null')

    def test_a_node_proof_redeems_once_and_then_never(self):
        from node.device_keys import DeviceKeys
        keys = DeviceKeys.generate()
        token = valid_token()
        proof = enroll_token.join_proof(keys, token)
        sig_key = keys.enrollment_keys()[0]
        ledger: dict = {}
        first = enroll_token.redeem_enroll_token(token, sig_key, proof, ledger)
        self.assertTrue(first['ok'])
        ledger[first['record']['nonce']] = {k: v for k, v in first['record'].items() if k != 'nonce'}
        second = enroll_token.redeem_enroll_token(token, sig_key, proof, ledger)
        self.assertEqual(second['problem'], 'reused')
        self.assertEqual(second['log']['boundTo'], first['record']['fingerprint'])
        # The #kem key of the same machine is not the key a join binds.
        self.assertEqual(enroll_token.redeem_enroll_token(token, keys.enrollment_keys()[1], proof, {})['problem'],
                         'bad-key')
        with self.assertRaises(ValueError):
            enroll_token.join_proof(keys, {'schema': 'enroll-token/1'})


@needs_crypto
class ReadToken(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tmpdir.mkdtemp()).resolve()
        self.text = json.dumps(valid_token())

    def write(self, name: str, mode: int, text: str = None) -> Path:
        path = self.folder / name
        path.write_text(self.text if text is None else text, encoding='utf-8')
        os.chmod(path, mode)
        return path

    def test_from_an_owner_only_file(self):
        for mode in (0o600, 0o400):
            self.assertEqual(enroll_token.read_token(str(self.write('t-%o.json' % mode, mode))), valid_token())

    def test_from_stdin(self):
        stream = io.BytesIO(self.text.encode() + b'\n')
        self.assertEqual(enroll_token.read_token('-', stdin=stream), valid_token())

    def test_a_file_others_may_read_is_refused(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            with self.assertRaisesRegex(ValueError, 'read by others'):
                enroll_token.read_token(str(self.write('t-%o.json' % mode, mode)))

    def test_a_file_of_another_user_is_refused(self):
        from unittest import mock
        path = str(self.write('theirs.json', 0o600))
        with mock.patch.object(enroll_token.osprivacy, 'owned_by_me', return_value=False):
            with self.assertRaisesRegex(ValueError, 'another user'):
                enroll_token.read_token(path)

    def test_a_symlink_or_a_folder_is_refused(self):
        target = self.write('real.json', 0o600)
        link = self.folder / 'link.json'
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            enroll_token.read_token(str(link))
        with self.assertRaises(ValueError):
            enroll_token.read_token(str(self.folder))
        with self.assertRaises(ValueError):
            enroll_token.read_token(str(self.folder / 'absent.json'))

    def test_the_token_itself_is_never_the_source(self):
        os.environ['OBSERVATORY_TEST_ENROLL_TOKEN'] = self.text
        try:
            for source in (self.text, ' ' + self.text, 'https://example.org/token', 'file:///tmp/x',
                           'env:OBSERVATORY_TEST_ENROLL_TOKEN', '$OBSERVATORY_TEST_ENROLL_TOKEN',
                           'OBSERVATORY_TEST_ENROLL_TOKEN', '', None):
                with self.assertRaises(ValueError) as caught:
                    enroll_token.read_token(source)
                self.assertNotIn(valid_token()['statement']['nonce'], str(caught.exception))
        finally:
            del os.environ['OBSERVATORY_TEST_ENROLL_TOKEN']

    def test_what_is_not_a_token_is_refused(self):
        for text in ('', 'not json', '{"schema": "enroll-token/1"}', '[' * 70 + ']' * 70,
                     json.dumps(dict(valid_token(), extra=1)), ' ' * (enroll_token.MAX_TOKEN_BYTES + 1)):
            with self.assertRaises(ValueError):
                enroll_token.read_token(str(self.write('bad.json', 0o600, text)))
        with self.assertRaises(ValueError):
            enroll_token.read_token('-', stdin=io.BytesIO(b'{}'))


class Findings(unittest.TestCase):
    """FAC-SEM-113 over the shapes the verifier refuses as malformed, and none over a token it accepts."""

    def codes(self, statement) -> list:
        return [(f['code'], f['instancePath']) for f in enroll_token.token_findings({'statement': statement})]

    def test_a_register_statement_has_no_findings(self):
        self.assertEqual(self.codes(valid_token()['statement']), [])
        self.assertEqual(enroll_token.enroll_token_rules('enroll-token', valid_token()), [])
        self.assertIsNone(enroll_token.enroll_token_rules('estate-record', valid_token()))

    def test_each_rule_names_its_member(self):
        good = valid_token()['statement']
        cases = [
            ({}, None, [('FAC-SEM-113', '/statement/type'), ('FAC-SEM-113', '/statement'),
                        ('FAC-SEM-113', '/statement/purpose'), ('FAC-SEM-113', '/statement/uses'),
                        ('FAC-SEM-113', '/statement/notBefore')]),
            (dict(good, type='limits/1'), None, [('FAC-SEM-113', '/statement/type')]),
            (dict(good, purpose='admin'), None, [('FAC-SEM-113', '/statement/purpose')]),
            (dict(good, uses=2), None, [('FAC-SEM-113', '/statement/uses')]),
            (dict(good, uses=True), None, [('FAC-SEM-113', '/statement/uses')]),
            ({k: v for k, v in good.items() if k != 'ceiling'}, None, [('FAC-SEM-113', '/statement')]),
            (dict(good, extra=1), None, [('FAC-SEM-113', '/statement')]),
            (dict(good, nonce=good['nonce'][:-1]), None, [('FAC-SEM-113', '/statement')]),
            (dict(good, scope='organization:x'), None, [('FAC-SEM-113', '/statement')]),
            (dict(good, expiresAt='2026-10-15T09:15:00'), None, [('FAC-SEM-113', '/statement/expiresAt')]),
            (dict(good, notBefore='2026-02-30T09:00:00Z'), None, [('FAC-SEM-113', '/statement/notBefore')]),
            (dict(good, expiresAt='2026-10-15T09:15:01Z'), None, [('FAC-SEM-113', '/statement/expiresAt')]),
            (dict(good, expiresAt=good['notBefore']), None, [('FAC-SEM-113', '/statement/expiresAt')]),
        ]
        for statement, _, expect in cases:
            with self.subTest(statement):
                self.assertEqual(self.codes(statement), expect)
        self.assertEqual(enroll_token.token_findings({'statement': 'x'}), [{
            'code': 'FAC-SEM-113', 'instancePath': '/statement', 'message': 'an enroll token carries its signed statement'}])


if __name__ == '__main__':
    unittest.main()
