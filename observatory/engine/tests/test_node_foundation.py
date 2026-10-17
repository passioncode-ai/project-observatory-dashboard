#!/usr/bin/env python3
"""The agent-network node's foundation (OBS-107, RM-21 P5): RFC 8785, P-256 keys and ES256, and fabric-relay/0.1's
signing and checks, run over the contract's own vectors (`fabric/node-vectors/`, vendored at a named commit).

A port that agrees with the contract kit on every vector gives one answer for one body; the round-trip tests then hold
that what this node signs is what a relay and a peer accept. The device-key store is tested in a temporary folder:
nothing here writes a Keychain item or schedules a job.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

import tmp as tmpdir                                   # noqa: E402  a temporary folder that goes away
from node import jcs, p256, relay                      # noqa: E402
from node.device_keys import DeviceKeys, new_device_id  # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors(name: str) -> dict:
    return json.loads((VECTORS / (name + '.json')).read_text(encoding='utf-8'))


def ms(text: str) -> int:
    value = relay.instant(text)
    assert value is not None, text
    return value


def key_of(value):
    """A vector key: a JWK object, or base64 SEC 1 octets as a string."""
    return base64.b64decode(value) if isinstance(value, str) else value


class VendoredVectors(unittest.TestCase):
    def test_digests_match_the_readme(self):
        readme = (VECTORS / 'README.md').read_text(encoding='utf-8')
        listed = dict(re.findall(r'\| `([a-z0-9-]+\.json)` \| `([0-9a-f]{64})` \|', readme))
        on_disk = {path.name for path in VECTORS.glob('*.json')}
        self.assertEqual(set(listed), on_disk)
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((VECTORS / name).read_bytes()).hexdigest(), digest, name)


class Rfc8785(unittest.TestCase):
    def test_samples(self):
        for case in vectors('canonical')['rfc8785']['vectors']:
            value = json.loads(case['inputJson']) if 'inputJson' in case else case['input']
            with self.subTest(case['name']):
                self.assertEqual(jcs.rfc8785(value), case['expected'])
                if 'utf8Hex' in case:
                    self.assertEqual(jcs.canonical_bytes(value).hex(), case['utf8Hex'])

    def test_numbers(self):
        for case in vectors('canonical')['rfc8785']['numbers']:
            value = struct.unpack('>d', bytes.fromhex(case['ieee754']))[0]
            with self.subTest(case['ieee754']):
                self.assertEqual(jcs.rfc8785(value), case['expected'])

    def test_refusals(self):
        for case in vectors('canonical')['rfc8785']['refuses']:
            value = json.loads(case['inputJson']) if 'inputJson' in case \
                else struct.unpack('>d', bytes.fromhex(case['ieee754']))[0]
            with self.subTest(case['name']), self.assertRaises(jcs.CanonicalError):
                jcs.rfc8785(value)

    def test_integers_are_their_doubles_and_types_outside_json_refuse(self):
        self.assertEqual(jcs.rfc8785({'b': 2 ** 53, 'a': True}), '{"a":true,"b":9007199254740992}')
        for value in (object(), {1: 'x'}, 10 ** 400, b'raw'):
            with self.assertRaises(jcs.CanonicalError):
                jcs.rfc8785(value)


class P256Keys(unittest.TestCase):
    def test_sec1_octets(self):
        data = vectors('p256')
        for case in data['valid']:
            with self.subTest(case['name']):
                self.assertIsNone(p256.key_refusal(base64.b64decode(case['publicKey'])))
        for case in data['invalid']:
            with self.subTest(case['name']):
                self.assertEqual(p256.key_refusal(base64.b64decode(case['publicKey'])), case['refusal'])

    def test_jwks(self):
        for case in vectors('p256')['jwk']:
            with self.subTest(case['name']):
                self.assertEqual(p256.key_refusal(case['jwk']), case['refusal'])

    def test_one_point_in_both_forms(self):
        data = vectors('relay')
        octets = base64.b64decode(data['machineKeyOctets'])
        self.assertEqual(p256.point(data['machineKey']), octets)
        self.assertEqual(p256.jwk(octets), data['machineKey'])

    def test_low_s(self):
        sig = p256.b64url_decode(vectors('relay')['auth']['frame']['sig'])
        self.assertTrue(p256.is_low_s(sig))
        high = sig[:32] + (p256.N - int.from_bytes(sig[32:], 'big')).to_bytes(32, 'big')
        self.assertFalse(p256.is_low_s(high))
        self.assertEqual(p256.to_low_s(high), sig)
        self.assertEqual(p256.to_low_s(sig), sig)
        with self.assertRaises(ValueError):
            p256.to_low_s(sig[:63])


class Times(unittest.TestCase):
    def test_instants(self):
        for case in vectors('relay')['instant']['cases']:
            with self.subTest(case['name']):
                value = relay.instant(case['value'])
                self.assertEqual(None if value is None else relay.iso_ms(value), case['expect'])

    def test_strict_time_takes_offsets_and_refuses_impossible_days(self):
        self.assertEqual(relay.strict_time('2026-10-12T02:00:00+02:00'), ms('2026-10-12T00:00:00Z'))
        self.assertEqual(relay.strict_time('2026-10-12t00:00:00z'), ms('2026-10-12T00:00:00Z'))
        for text in ('2026-02-30T00:00:00Z', '2026-10-12 00:00:00Z', '2026-10-12T00:00:00', None, 7):
            self.assertIsNone(relay.strict_time(text))


class Origins(unittest.TestCase):
    def test_relay_origins(self):
        for case in vectors('relay')['relayOrigin']['cases']:
            with self.subTest(case['name']):
                self.assertEqual(relay.is_relay_origin(case['value']), case['expect'])


@needs_crypto
class Signatures(unittest.TestCase):
    """What this node signs equals, byte for byte, what the contract's signer produced, and verifies there."""

    def test_auth_signing_input_and_signature(self):
        data = vectors('relay')
        auth, frame = data['auth'], data['auth']['frame']
        text = relay.auth_signing_input(relay=auth['context']['relay'], nonce=auth['context']['nonce'],
                                        estate=frame['estate'], kid=frame['kid'], iat=frame['iat'])
        self.assertEqual(text, auth['signingInput'])
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), auth['sha256'])
        self.assertIs(p256.verify_es256(data['machineKey'], text, frame['sig']), True)
        self.assertEqual(p256.verify_es256(data['ed25519']['key'], text, frame['sig']), 'unsupported-alg')

    def test_admit_cases_through_the_node_signing_input(self):
        """The relay's `admit`, in its order, over the node's signing input: every case gives the reference answer."""
        for case in vectors('relay')['admit']['cases']:
            frame, ctx = case['frame'], case['context']
            with self.subTest(case['name']):
                self.assertEqual(admit(frame, ctx), case['expect'])

    def test_blob_cases_through_the_node_parser_and_signing_input(self):
        data = vectors('relay')['blobRequest']
        for case in data['cases']:
            with self.subTest(case['name']):
                self.assertEqual(blob_request(case, data), case['expect'])

    def test_a_signature_the_node_makes_verifies_low_s(self):
        keys = DeviceKeys.generate()
        for i in range(64):
            sig = keys.sign('message %d' % i)
            raw = p256.b64url_decode(sig)
            self.assertEqual(len(raw), 64)
            self.assertTrue(p256.is_low_s(raw))
            self.assertIs(p256.verify_es256(keys.sig_public, 'message %d' % i, sig), True)
            self.assertIs(p256.verify_es256(keys.sig_public, 'message %d!' % i, sig), False)

    def test_an_auth_frame_the_node_makes_is_admitted(self):
        keys = DeviceKeys.generate()
        now = '2026-10-12T00:00:30Z'
        frame = relay.auth_frame(keys.sign, frame_id='a1', relay='https://relay.example', nonce='bm9uY2U',
                                 estate='urn:fabric:estate:example', kid=keys.sig_kid, iat='2026-10-12T00:00:00Z')
        ctx = {'relay': 'https://relay.example', 'nonce': 'bm9uY2U', 'nonceUsed': False, 'now': now,
               'estate': 'urn:fabric:estate:example', 'admitted': {keys.sig_kid: base64.b64encode(keys.sig_public).decode()}}
        self.assertEqual(admit(frame, ctx), {'ok': True, 'kid': keys.sig_kid})
        self.assertEqual(admit(frame, dict(ctx, nonce='b3RoZXI'))['code'], 'unauthenticated')
        with self.assertRaises(ValueError):
            relay.auth_frame(keys.sign, frame_id='a1', relay='https://relay.example/path', nonce='n',
                             estate='e', kid=keys.sig_kid, iat='2026-10-12T00:00:00Z')

    def test_a_blob_request_the_node_makes_is_accepted(self):
        a, b = DeviceKeys.generate(), DeviceKeys.generate()
        digest = 'sha256:' + 'ab' * 32
        header = relay.relay_request_header(a.sign, relay='https://relay.example', method='PUT', digest=digest,
                                            kid=a.sig_kid, recipients=[b.sig_kid], iat='2026-10-12T12:00:00Z')
        case = {'method': 'PUT', 'header': header, 'now': '2026-10-12T12:00:30Z',
                'admitted': {a.sig_kid: p256.jwk(a.sig_public), b.sig_kid: p256.jwk(b.sig_public)}}
        self.assertEqual(blob_request(case, {'relay': 'https://relay.example', 'digest': digest}),
                         {'ok': True, 'kid': a.sig_kid, 'recipients': [b.sig_kid], 'repeat': False})
        for recipients in ([], [b.sig_kid, b.sig_kid], [b.kem_kid]):
            with self.assertRaises(ValueError):
                relay.relay_request_header(a.sign, relay='https://relay.example', method='PUT', digest=digest,
                                           kid=a.sig_kid, recipients=recipients, iat='2026-10-12T12:00:00Z')


@needs_crypto
class Messages(unittest.TestCase):
    def test_message_body_cases(self):
        for case in vectors('relay')['messageBody']['cases']:
            senders = {kid: key_of(key) for kid, key in case['senders'].items()}
            expect = {'from': case['frame']['from'], 'to': case['self'], 'msgId': case['frame']['msgId']}
            with self.subTest(case['name']):
                answer = relay.verify_message_body(case['body'], expect, senders.get, ms(case['now']))
                self.assertEqual(answer, case['expect'])

    def test_the_legacy_message_vector(self):
        data = vectors('relay')
        message = data['message']
        expect = {'from': message['frame']['from'], 'to': message['self'], 'msgId': message['frame']['msgId']}
        answer = relay.verify_message_body(message['body'], expect, {message['frame']['from']: data['machineKey']}.get,
                                           ms(message['now']))
        self.assertTrue(answer['ok'])

    def test_replay_cases(self):
        for case in vectors('relay')['replay']['cases']:
            seen = set()
            with self.subTest(case['name']):
                for step in case['steps']:
                    answer = replay_step(step, seen)
                    self.assertEqual(answer, step['expect'], step)

    def test_a_body_the_node_makes_is_accepted_once(self):
        a, b = DeviceKeys.generate(), DeviceKeys.generate()
        body = relay.message_body(a.sign, sender=a.sig_kid, to=b.sig_kid, msg_id='m-1', iat='2026-10-12T00:00:00Z',
                                  exp='2026-10-12T01:00:00Z', payload={'command': 'run.start', 'n': 1.5})
        keys = {a.sig_kid: a.sig_public}.get
        expect = {'from': a.sig_kid, 'to': b.sig_kid, 'msgId': 'm-1'}
        now = ms('2026-10-12T00:00:10Z')
        answer = relay.verify_message_body(body, expect, keys, now)
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['payload']['payload'], {'command': 'run.start', 'n': 1.5})
        seen = {(a.sig_kid, 'm-1')}
        self.assertEqual(relay.check_replay(lambda f, m: (f, m) in seen, answer['payload'], now)['reason'], 'replay')
        # Delivered to another machine, it is a mismatch: `to` is this machine's own kid, never the relay's word.
        self.assertEqual(relay.verify_message_body(body, dict(expect, to=a.sig_kid), keys, now)['reason'], 'mismatch')
        for exp in ('2026-10-12T00:00:00Z', '2026-10-19T00:00:01Z', '2026-10-12T01:00:00'):
            with self.assertRaises(ValueError):
                relay.message_body(a.sign, sender=a.sig_kid, to=b.sig_kid, msg_id='m', iat='2026-10-12T00:00:00Z',
                                   exp=exp, payload=None)


class PeerKeys(unittest.TestCase):
    def test_admission_set_cases(self):
        for case in vectors('relay')['admissionSet']['cases']:
            with self.subTest(case['name']):
                answer = relay.peer_keys(case.get('held'), ms(case['at']), case.get('extraRevocations', []))
                if case['expect']['ok']:
                    self.assertEqual(answer['revision'], case['expect']['revision'])
                    self.assertEqual(sorted(answer['keys']), case['expect']['kids'])
                    self.assertEqual(answer['excluded'], case['expect']['excluded'])
                else:
                    self.assertEqual(answer, {'ok': False, 'refusal': case['expect']['refusal']})


@needs_crypto
class DeviceKeyStore(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tmpdir.mkdtemp()).resolve() / 'device'

    def test_kids_roles_and_enrollment_form(self):
        keys = DeviceKeys.generate()
        self.assertRegex(keys.device_id, r'^m-[0-9a-f]{32}$')
        self.assertEqual((keys.sig_kid, keys.kem_kid), (keys.device_id + '#sig', keys.device_id + '#kem'))
        listed = keys.enrollment_keys()
        self.assertEqual([(k['use'], k['alg']) for k in listed], [('sig', 'es256'), ('kem', 'ecdh-p256')])
        for entry in listed:
            self.assertRegex(entry['kid'], r'^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}#(sig|kem)$')
            self.assertIsNone(p256.key_refusal(base64.b64decode(entry['publicKey'])))
        self.assertNotEqual(keys.sig_public, keys.kem_public)
        self.assertNotIn('PRIVATE', repr(keys))

    def test_saved_owner_only_and_loaded_back(self):
        keys = DeviceKeys.load_or_create(self.folder)
        for name in ('device.json', 'sig.pem', 'kem.pem'):
            self.assertEqual(stat.S_IMODE(os.stat(self.folder / name).st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)
        again = DeviceKeys.load_or_create(self.folder)
        self.assertEqual((again.device_id, again.sig_public, again.kem_public),
                         (keys.device_id, keys.sig_public, keys.kem_public))
        self.assertIs(p256.verify_es256(keys.sig_public, 'x', again.sign('x')), True)

    def test_existing_keys_are_never_replaced(self):
        DeviceKeys.load_or_create(self.folder)
        with self.assertRaises(FileExistsError):
            DeviceKeys.generate().save(self.folder)

    def test_a_half_written_set_is_not_completed(self):
        keys = DeviceKeys.generate()
        keys.save(self.folder)
        (self.folder / 'device.json').unlink()
        with self.assertRaises(RuntimeError):
            DeviceKeys.load_or_create(self.folder)

    def test_a_symlinked_folder_is_refused(self):
        real = Path(tmpdir.mkdtemp()).resolve()
        link = Path(tmpdir.mkdtemp()).resolve() / 'link'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            DeviceKeys.generate().save(link)

    def test_device_ids_are_opaque(self):
        self.assertNotEqual(new_device_id(), new_device_id())
        with self.assertRaises(ValueError):
            DeviceKeys('person@example', None, None, '')


class AdversarialRound(unittest.TestCase):
    """Findings of the #291 adversarial review (round 2): each input now gets a typed answer, never an exception."""

    def test_year_0000_is_an_instant_as_javascript_reads_it(self):
        self.assertEqual(relay.instant('0000-01-01T00:00:00Z'), -62167219200000)
        self.assertEqual(relay.iso_ms(relay.instant('0000-02-29T12:00:00Z')), '0000-02-29T12:00:00.000Z')
        self.assertEqual(relay.strict_time('0000-01-01T00:00:00+00:00'), -62167219200000)
        self.assertEqual(relay.iso_ms(relay.instant('9999-12-31T23:59:59.999Z')), '9999-12-31T23:59:59.999Z')
        for days in (-719_528, -1, 0, 1, 20_000, 2_900_000):        # 0000-01-01 to year 9909
            ms = days * 86_400_000 + 12_345_000
            self.assertEqual(relay.instant(relay.iso_ms(ms)), ms)

    def test_non_ascii_digits_and_trailing_newlines_are_no_time(self):
        for text in ('２０２６-10-12T00:00:00Z', '2026-10-12T00:00:00Z\n', '٢٠٢٦-10-12T00:00:00Z'):
            self.assertIsNone(relay.instant(text))
            self.assertIsNone(relay.strict_time(text))
        self.assertIsNone(relay.strict_time('2026-10-12T00:00:00+00:00\n'))

    @needs_crypto
    def test_a_body_dated_year_0000_is_refused_not_raised(self):
        keys = DeviceKeys.generate()
        header = p256.b64url_encode(jcs.canonical_bytes({'alg': 'ES256', 'kid': keys.sig_kid, 'typ': relay.MESSAGE_TYP}))
        payload = p256.b64url_encode(jcs.canonical_bytes({
            'type': relay.MESSAGE_TYPE, 'from': keys.sig_kid, 'to': 'm-b#sig', 'msgId': 'x',
            'iat': '0000-01-01T00:00:00Z', 'exp': '0000-01-01T01:00:00Z', 'payload': None}))
        body = header + '.' + payload + '.' + keys.sign(header + '.' + payload)
        expect = {'from': keys.sig_kid, 'to': 'm-b#sig', 'msgId': 'x'}
        self.assertEqual(relay.verify_message_body(body, expect, {keys.sig_kid: keys.sig_public}.get,
                                                   ms('2026-10-12T00:00:00Z')), {'ok': False, 'reason': 'expired'})
        self.assertEqual(relay.check_replay(lambda f, m: False, {'from': 'a', 'msgId': 'b', 'exp': '0000-01-01T00:00:00Z'},
                                            0)['reason'], 'expired')
        self.assertEqual(relay.peer_keys({'issuedAt': '0000-01-01T00:00:00Z', 'validUntil': '0000-01-02T00:00:00Z'},
                                         0), {'ok': False, 'refusal': 'expired'})

    def test_deep_nesting_is_malformed(self):
        for depth in (65, 600, 900, 100_000):
            nested = p256.b64url_encode(('[' * depth + ']' * depth).encode())
            header = p256.b64url_encode(b'{"alg":"ES256","typ":"fabric-relay-message+jwt"}')
            for body in (header + '.' + nested + '.sig', nested + '.' + nested + '.sig'):
                self.assertEqual(relay.verify_message_body(body, {'from': 'a', 'to': 'b', 'msgId': 'c'},
                                                           lambda kid: None, 0), {'ok': False, 'reason': 'malformed'})
        for depth in (65, 3000, 100_000):
            value = p256.b64url_encode(('{"a":' * depth + '1' + '}' * depth).encode())
            answer = relay.parse_relay_request(value)
            self.assertFalse(answer['ok'])
            self.assertIn(answer['code'], ('malformed', 'too-large'))
        self.assertEqual(relay.parse_relay_request(p256.b64url_encode(('[' * 64 + ']' * 64).encode()))['code'],
                         'malformed')
        self.assertFalse(jcs.is_canonical(json.loads('[' * 64 + ']' * 64), b'x'))
        with self.assertRaises(jcs.CanonicalError):
            jcs.rfc8785(json.loads('[' * 66 + ']' * 66))

    def test_peer_keys_are_bound_to_their_machine_and_read_strictly(self):
        good = base64.b64encode(p256.point(vectors('relay')['machineKey'])).decode()
        record = {'issuedAt': '2026-10-12T00:00:00Z', 'validUntil': '2026-11-01T00:00:00Z', 'revocations': [
            {'kind': 'machine', 'ref': {'unhashable': True}}, {'kind': 'device-key', 'ref': ['x']}], 'machines': [
            {'id': 'm-a', 'deviceKeys': [{'kid': 'm-a#sig', 'use': 'sig', 'publicKey': good}]},
            {'id': 'm-b', 'deviceKeys': [{'kid': 'm-a#sig', 'use': 'sig', 'publicKey': good}]},
            {'id': 'm-c', 'deviceKeys': [{'kid': 'm-x#sig', 'use': 'sig', 'publicKey': good}]},
            {'id': 'm-d', 'deviceKeys': [{'kid': 'm-d#sig', 'use': 'sig', 'publicKey': good[:20] + '=' + good[21:]}]},
            {'id': 'm-e', 'deviceKeys': [{'kid': 'm-e#sig', 'use': 'sig', 'publicKey': good + 'A'}]},
            {'id': {'not': 'a string'}, 'deviceKeys': []},
            {'id': 'm-f', 'deviceKeys': [{'kid': 'm-f#sig', 'use': 'sig', 'publicKey': good}]}]}
        answer = relay.peer_keys(record, ms('2026-10-13T00:00:00Z'))
        self.assertEqual(sorted(answer['keys']), ['m-f#sig'])
        self.assertEqual(sorted(e['kid'] for e in answer['excluded']), ['m-a#sig', 'm-d#sig', 'm-e#sig', 'm-x#sig'])

    @needs_crypto
    def test_a_blob_request_no_relay_accepts_is_never_signed(self):
        keys = DeviceKeys.generate()
        base = dict(relay='https://relay.example', method='PUT', digest='sha256:' + 'ab' * 32, kid=keys.sig_kid,
                    recipients=['m-b#sig'], iat='2026-10-12T00:00:00Z')
        for bad in ({'relay': 'https://relay.example/x'}, {'digest': 'sha256:AB'}, {'iat': '2026-02-30T00:00:00Z'},
                    {'method': 'POST'}):
            with self.assertRaises(ValueError):
                relay.relay_request_header(keys.sign, **dict(base, **bad))

    @needs_crypto
    def test_one_key_set_when_processes_start_together(self):
        script = ('import sys; sys.path.insert(0, %r); from pathlib import Path; from node.device_keys import DeviceKeys;'
                  'k = DeviceKeys.load_or_create(Path(sys.argv[1])); print(k.device_id, k.sig_public.hex(), '
                  'k.kem_public.hex())') % str(ROOT)
        for trial in range(6):
            folder = Path(tmpdir.mkdtemp()).resolve() / 'device'
            runs = [subprocess.Popen([sys.executable, '-c', script, str(folder)], stdout=subprocess.PIPE, text=True)
                    for _ in range(4)]
            outputs = {run.communicate(timeout=60)[0].strip() for run in runs}
            disk = DeviceKeys.load(folder)
            self.assertEqual(outputs, {'%s %s %s' % (disk.device_id, disk.sig_public.hex(), disk.kem_public.hex())},
                             trial)
            self.assertEqual(sorted(p.name for p in folder.iterdir() if not p.name.startswith('.')),
                             ['device.json', 'kem.pem', 'sig.pem'])

    @needs_crypto
    def test_a_folder_others_may_write_is_refused(self):
        folder = Path(tmpdir.mkdtemp()).resolve() / 'device'
        DeviceKeys.load_or_create(folder)
        os.chmod(folder, 0o777)
        with self.assertRaises(RuntimeError):
            DeviceKeys.load(folder)
        os.chmod(folder, 0o700)
        self.assertTrue(DeviceKeys.load(folder).device_id)

    def test_device_ids_end_where_they_end(self):
        with self.assertRaises(ValueError):
            DeviceKeys('m-1234\n', None, None, '')


class ConfirmingRound(unittest.TestCase):
    """The #291 confirming round's LOW findings, and the checks its mutants showed untested."""

    def test_a_number_json_cannot_carry_is_refused(self):
        for text in (b'{"a":1e400}', b'[-1e400]'):
            with self.assertRaises(ValueError):
                relay.parse_json(text)
        self.assertEqual(relay.parse_json(b'{"a":1e300}'), {'a': 1e300})

    def test_a_kid_listed_with_mixed_reasons_is_excluded_once(self):
        good = base64.b64encode(p256.point(vectors('relay')['machineKey'])).decode()
        ed = base64.b64encode(bytes(range(1, 33))).decode()
        record = {'issuedAt': '2026-10-12T00:00:00Z', 'validUntil': '2026-11-01T00:00:00Z', 'machines': [
            {'id': 'm-a', 'deviceKeys': [{'kid': 'm-a#sig', 'use': 'sig', 'publicKey': ed},
                                         {'kid': 'm-a#sig', 'use': 'sig', 'publicKey': good}]}]}
        answer = relay.peer_keys(record, ms('2026-10-13T00:00:00Z'))
        self.assertEqual((answer['keys'], answer['excluded']), ({}, [{'kid': 'm-a#sig', 'reason': 'bad-key'}]))

    def test_a_lone_surrogate_header_is_malformed(self):
        self.assertEqual(relay.parse_relay_request('\ud800'), {'ok': False, 'status': 400, 'code': 'malformed'})

    def test_a_kid_named_three_times_is_excluded_once(self):
        good = base64.b64encode(p256.point(vectors('relay')['machineKey'])).decode()
        key = {'kid': 'm-a#sig', 'use': 'sig', 'publicKey': good}
        record = {'issuedAt': '2026-10-12T00:00:00Z', 'validUntil': '2026-11-01T00:00:00Z',
                  'machines': [{'id': 'm-a', 'deviceKeys': [key, key, key]}]}
        answer = relay.peer_keys(record, ms('2026-10-13T00:00:00Z'))
        self.assertEqual((answer['keys'], answer['excluded']), ({}, [{'kid': 'm-a#sig', 'reason': 'bad-key'}]))

    def test_no_bare_module_name_the_privacy_gate_denies(self):
        source = (ROOT / 'node' / 'p256.py').read_text(encoding='utf-8')
        self.assertNotRegex(source, r'import [^\n]*\butils\b|\butils\.')

    def test_offsets_past_23_59_are_no_time(self):
        for text in ('2026-10-12T00:00:00+24:00', '2026-10-12T00:00:00+12:60', '2026-10-12T00:00:00-99:00'):
            self.assertIsNone(relay.strict_time(text))
        self.assertEqual(relay.strict_time('2026-10-12T23:59:00+23:59'), ms('2026-10-12T00:00:00Z'))

    def test_the_signer_and_the_parser_share_one_depth(self):
        for depth in (64, 65):
            text = '[' * depth + ']' * depth
            parsed = relay._depth_ok(text)
            try:
                jcs.rfc8785(json.loads(text))
                signed = True
            except jcs.CanonicalError:
                signed = False
            self.assertEqual((parsed, signed), (depth == 64, depth == 64), depth)

    def test_the_depth_count_skips_strings_and_their_escapes(self):
        self.assertTrue(relay._depth_ok('"' + '[' * 100 + '"'))
        # an escaped quote does not end the string, so the brackets after it are still inside it
        self.assertTrue(relay._depth_ok('["a\\"' + '[' * 100 + '"]'))
        # an escaped backslash does end its escape, so the string ends at the next quote
        self.assertFalse(relay._depth_ok('["a\\\\"' + '[' * 65 + ']' * 65 + ']'))

    def test_the_parser_bounds_size_depth_and_recursion(self):
        with self.assertRaises(ValueError):
            relay.parse_json(b' ' * (relay.MAX_FRAME_BYTES + 1))
        with self.assertRaises(ValueError):
            relay.parse_json(('[' * 65 + ']' * 65).encode())
        with mock.patch.object(relay, 'MAX_JSON_DEPTH', 10 ** 9):    # past the pre-check, the parser's own limit
            with self.assertRaises(ValueError):
                relay.parse_json(('[' * 200_000 + ']' * 200_000).encode())

    def test_device_key_base64_has_one_spelling(self):
        good = base64.b64encode(p256.point(vectors('relay')['machineKey'])).decode()
        self.assertEqual(len(relay._base64(good)), 65)
        for bad in (good[:-1], good + '=', good[:-2] + 'B=', '!' + good[1:], good[:40] + '=' + good[41:]):
            self.assertEqual(relay._base64(bad), b'', bad)

    @needs_crypto
    def test_a_key_file_is_never_replaced(self):
        from node import device_keys
        folder = Path(tmpdir.mkdtemp()).resolve()
        (folder / 'sig.pem').write_text('existing')
        os.chmod(folder / 'sig.pem', 0o600)
        with self.assertRaises(FileExistsError):
            device_keys._write_exclusive(folder / 'sig.pem', 'new')
        self.assertEqual((folder / 'sig.pem').read_text(), 'existing')
        self.assertEqual([p.name for p in folder.iterdir()], ['sig.pem'])

    @needs_crypto
    def test_a_folder_others_may_write_is_refused_before_the_lock(self):
        folder = Path(tmpdir.mkdtemp()).resolve() / 'device'
        DeviceKeys.load_or_create(folder)
        os.chmod(folder, 0o777)
        with self.assertRaises(RuntimeError):
            DeviceKeys.load_or_create(folder)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o777)   # refused, not silently tightened

    @needs_crypto
    def test_a_writer_killed_between_write_and_link_leaves_nothing_behind(self):
        folder = Path(tmpdir.mkdtemp()).resolve() / 'device'
        folder.mkdir(mode=0o700)
        orphan = folder / '.device-key-abc123'
        orphan.write_text('a stale temporary key file')
        keys = DeviceKeys.load_or_create(folder)
        self.assertFalse(orphan.exists())
        self.assertEqual(DeviceKeys.load(folder).device_id, keys.device_id)


# ---- The relay's checks, in the reference's order, as a cross-check of what the node signs -------------------------

def admit(frame: dict, ctx: dict) -> dict:
    """`admit` of the contract (src/relay.ts) without its timing parity, which a relay owes and a test does not."""
    if frame.get('protocol') != relay.RELAY_PROTOCOL:
        return {'ok': False, 'code': 'protocol-unsupported'}
    if frame.get('alg') != relay.RELAY_ALG:
        return {'ok': False, 'code': 'unsupported-alg'}
    iat, now = relay.instant(frame.get('iat')), ms(ctx['now'])
    if iat is None:
        return {'ok': False, 'code': 'unauthenticated'}
    if iat - now > relay.CLOCK_SKEW_SECONDS * 1000:
        return {'ok': False, 'code': 'not-yet-valid'}
    if now - iat > relay.AUTH_WINDOW_SECONDS * 1000:
        return {'ok': False, 'code': 'unauthenticated'}
    if ctx['nonceUsed']:
        return {'ok': False, 'code': 'replay'}
    key = (ctx.get('admitted') or {}).get(frame['kid']) if ctx.get('estate') is not None \
        and frame['estate'] == ctx['estate'] else None
    try:
        text = relay.auth_signing_input(relay=ctx['relay'], nonce=ctx['nonce'], estate=frame['estate'],
                                        kid=frame['kid'], iat=frame['iat'])
    except jcs.CanonicalError:
        text = None
    proven = key is not None and text is not None and p256.verify_es256(key_of(key), text, frame.get('sig')) is True
    return {'ok': True, 'kid': frame['kid']} if proven else {'ok': False, 'code': 'unauthenticated'}


def blob_request(case: dict, data: dict) -> dict:
    """`verifyBlobRequest` then `decideBlobAccess` of the contract, over the node's parser and signing input."""
    parsed = relay.parse_relay_request(case.get('header'))
    if not parsed['ok']:
        return parsed
    request = parsed['request']
    if request['alg'] != relay.RELAY_ALG:
        return {'ok': False, 'status': 401, 'code': 'unsupported-alg'}
    recipients = request['recipients']
    sig_kid = lambda v: isinstance(v, str) and 0 < len(v) <= relay.MAX_KID_CHARS and v.endswith('#sig')
    if not sig_kid(request['kid']) or not 1 <= len(recipients) <= relay.MAX_BLOB_RECIPIENTS \
            or not all(map(sig_kid, recipients)) or len(set(recipients)) != len(recipients):
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    iat, now = relay.instant(request['iat']), ms(case.get('now') or data['now'])
    if iat is None:
        return {'ok': False, 'status': 400, 'code': 'malformed'}
    if iat - now > relay.CLOCK_SKEW_SECONDS * 1000:
        return {'ok': False, 'status': 401, 'code': 'not-yet-valid'}
    if now - iat > relay.AUTH_WINDOW_SECONDS * 1000:
        return {'ok': False, 'status': 401, 'code': 'unauthenticated'}
    admitted = case.get('admitted') or {}
    key = admitted.get(request['kid'])
    text = relay.blob_signing_input(relay=data['relay'], method=case['method'], digest=data['digest'],
                                    recipients=recipients, iat=request['iat'])
    if key is None or p256.verify_es256(key_of(key), text, request['sig']) is not True:
        return {'ok': False, 'status': 401, 'code': 'unauthenticated'}
    if case['method'] == 'PUT' and not all(r in admitted for r in recipients):
        return {'ok': False, 'status': 403, 'code': 'unknown-recipient'}
    stored = case.get('stored')
    same = stored is not None and stored['recipients'] == recipients
    if case['method'] == 'PUT':
        return {'ok': True, 'kid': request['kid'], 'recipients': recipients, 'repeat': same} \
            if stored is None or same else {'ok': False, 'status': 409, 'code': 'recipients-mismatch'}
    return {'ok': True, 'kid': request['kid'], 'recipients': recipients, 'repeat': False} \
        if same and request['kid'] in stored['recipients'] else {'ok': False, 'status': 403, 'code': 'not-admitted'}


def replay_step(step: dict, seen: set) -> dict:
    payload = step.get('payload')
    if payload is None:
        parts = step['body'].split('.')
        payload = json.loads(p256.b64url_decode(parts[1]))
    answer = relay.check_replay(lambda f, m: (f, m) in seen, payload, ms(step['now']))
    if answer['ok']:
        seen.add((payload['from'], payload['msgId']))
    return answer


if __name__ == '__main__':
    unittest.main()
