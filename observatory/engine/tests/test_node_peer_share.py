#!/usr/bin/env python3
"""A credential shared with another person's estate (`fabric-peer-share/1`, contract DEC-0142, OBS-264): the node's port
of the reference run over the contract's `peer-share.json` (keys and thumbprints, the link, the claim and the pin, the
comparison code, one attempt per link, the envelope, usage reports, revocation and deletion), and the seal and sign
functions refusing what no receiver would open.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import p256, peer_share as PS, relay          # noqa: E402

PEER = ROOT / 'fabric' / 'node-vectors' / 'peer'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors() -> dict:
    return json.loads((PEER / 'peer-share.json').read_text(encoding='utf-8'))


def key(name: str) -> dict:
    return next(k for k in vectors()['keys'] if k['name'] == name)


def private(name: str) -> 'PS.SharePrivateKey':
    return PS.SharePrivateKey.from_scalar(p256.b64url_decode(key(name)['d']))


class Keys(unittest.TestCase):
    def test_thumbprints(self):
        for k in vectors()['keys']:
            with self.subTest(k['name']):
                self.assertTrue(PS.peer_jwk(k['jwk']))
                self.assertEqual(PS.peer_thumbprint(k['jwk']), k['thumbprint'])

    def test_refusals(self):
        jwk = key('sSig')['jwk']
        self.assertFalse(PS.peer_jwk({**jwk, 'kid': 'x'}))
        self.assertFalse(PS.peer_jwk({**jwk, 'y': jwk['x']}))
        self.assertIsNone(PS.peer_thumbprint('x'))
        self.assertFalse(PS.peer_keys({'sig': jwk, 'kem': jwk}))
        self.assertTrue(PS.peer_keys({'sig': jwk, 'kem': key('sKem')['jwk']}))

    @needs_crypto
    def test_private_scalar_gives_its_jwk(self):
        for name in ('sSig', 'sKem', 'rSig', 'rKem'):
            self.assertEqual(private(name).jwk(), key(name)['jwk'])


class Link(unittest.TestCase):
    def test_links(self):
        for case in vectors()['links']:
            with self.subTest(case['name']):
                self.assertEqual(PS.parse_share_link(case['link']), case['expect'])
                if case['expect']['ok']:
                    self.assertEqual(PS.parse_share_link(PS.share_link(case['expect']['link'])), case['expect'])

    def test_normalisation_the_reference_names(self):
        """A dot segment and an upper-case host read as the same parts (PS-4, WHATWG URL normalisation)."""
        good = vectors()['links'][0]
        link = good['link'].replace('https://mailbox.example/s/', 'https://MAILBOX.example/x/../s/')
        self.assertEqual(PS.parse_share_link(link), good['expect'])
        self.assertEqual(PS.parse_share_link(good['link'].replace('mailbox.example', 'mailbox.example:443')),
                         good['expect'])


class ClaimAndPin(unittest.TestCase):
    def test_claims(self):
        for case in vectors()['claims']:
            with self.subTest(case['name']):
                self.assertEqual(PS.claim_holds(case['link'], case['frame']), case['holds'])
                self.assertIsNotNone(PS.peer_frame(case['frame']))

    def test_mac(self):
        case = vectors()['claims'][0]
        link = case['link']
        self.assertEqual(PS.claim_mac(link['code'], link['shareId'], case['frame']['keys'], link['fpr']), case['mac'])
        self.assertIsNone(PS.claim_mac('short', link['shareId'], case['frame']['keys'], link['fpr']))
        self.assertFalse(PS.claim_holds(None, case['frame']))

    def test_pins(self):
        for case in vectors()['pins']:
            with self.subTest(case['name']):
                self.assertEqual(PS.pin_holds(case['link'], case['frame']), case['holds'])


class Codes(unittest.TestCase):
    def test_codes(self):
        for case in vectors()['codes']:
            with self.subTest(case['name']):
                self.assertEqual(PS.checked_peer_code(case['senderCommit'], case['sender'],
                                                      case['recipientCommit'], case['recipient']), case['expect'])
                if case['expect']['ok']:
                    self.assertEqual(len(case['expect']['code']), PS.PEER_CODE_DIGITS)
                    self.assertEqual(PS.peer_commit(case['sender']), {'ok': True, 'commit': case['senderCommit']})

    def test_problems(self):
        c = next(v for v in vectors()['codes'] if v['expect']['ok'])
        self.assertEqual(PS.peer_commit('side'), {'ok': False, 'problem': 'malformed'})
        self.assertEqual(PS.peer_commit({**c['sender'], 'role': 'witness'}), {'ok': False, 'problem': 'bad-role'})
        self.assertEqual(PS.peer_commit({**c['sender'], 'shareId': 'NOT-AN-ID'}), {'ok': False, 'problem': 'bad-share'})
        self.assertEqual(PS.checked_peer_code('sha256:short', c['sender'], c['recipientCommit'], c['recipient']),
                         {'ok': False, 'problem': 'malformed'})
        self.assertEqual(PS.peer_comparison_code(c['sender'], {**c['recipient'], 'nonce': 'short'}),
                         {'ok': False, 'problem': 'bad-nonce'})
        self.assertEqual(PS.peer_comparison_code(c['recipient'], c['sender']), {'ok': False, 'problem': 'role-mismatch'})
        self.assertEqual(PS.peer_comparison_code(c['sender'], c['recipient']), {'ok': True, 'code': c['expect']['code']})


class Exchanges(unittest.TestCase):
    def test_exchanges(self):
        for case in vectors()['exchanges']:
            with self.subTest(case['name']):
                self.assertEqual(PS.peer_exchange_verdict(case['shareId'], case['claimDeadline'], case['frames'],
                                                          case['at']), case['expect'])
        self.assertEqual(PS.peer_exchange_verdict('not-an-id', '2026-11-03T00:00:00Z', [], '2026-10-27T00:00:00Z'),
                         {'state': 'burnt', 'reason': 'malformed', 'index': -1})


@needs_crypto
class Envelopes(unittest.TestCase):
    def test_envelopes(self):
        v = vectors()
        for case in v['envelopes']:
            with self.subTest(case['name']):
                self.assertEqual(PS.check_peer_share_envelope(case['envelope'], case['context']), case['expect'])
                if case['expect']['ok']:
                    opened = PS.open_peer_share(case['envelope'], case['context'], private('rKem'))
                    self.assertEqual(opened, {'ok': True, 'secret': v['secret'].encode('utf-8'),
                                              'mark': case['expect']['mark']})

    def test_seal_and_refusals(self):
        share_id = vectors()['envelopes'][0]['envelope']['header']['shareId']
        iat = relay.instant('2026-10-27T09:59:00Z')
        base = dict(recipient_kem=key('rKem')['jwk'], signer_sig=key('sSig')['jwk'], signer=private('sSig'),
                    share_id=share_id, iat_ms=iat, slot='OPENROUTER_API_KEY', provider='openrouter', plaintext=b'v')
        env = PS.seal_peer_share(**base, sequence=4, exp_ms=iat + 86_400_000)
        ctx = {'at': '2026-10-27T10:00:00Z', 'shareId': share_id, 'ownKem': key('rKem')['jwk'],
               'senderSig': key('sSig')['jwk']}
        self.assertEqual(PS.open_peer_share(env, ctx, private('rKem'))['secret'], b'v')
        self.assertEqual(PS.open_peer_share(env, ctx, private('xKem')), {'ok': False, 'refusal': 'cannot-open'})
        with self.assertRaisesRegex(ValueError, 'lifetime-too-long'):
            PS.seal_peer_share(**base, sequence=1, exp_ms=iat + PS.MAX_PEER_ENVELOPE_DAYS * 86_400_000 + 1000)
        with self.assertRaisesRegex(ValueError, 'malformed'):
            PS.seal_peer_share(**{**base, 'slot': 'lower'}, sequence=1, exp_ms=iat + 1000)
        with self.assertRaisesRegex(ValueError, 'bad-key'):
            PS.seal_peer_share(**{**base, 'signer': private('xSig')}, sequence=1, exp_ms=iat + 1000)
        within = relay.instant('2026-10-27T09:59:00.100Z')
        with self.assertRaisesRegex(ValueError, 'lifetime-too-long'):
            PS.seal_peer_share(**{**base, 'iat_ms': within}, sequence=1, exp_ms=within + 800)
        with self.assertRaisesRegex(ValueError, 'bad-key'):
            PS.seal_peer_share(**{**base, 'recipient_kem': {**key('rKem')['jwk'], 'y': key('rKem')['jwk']['x']}},
                               sequence=1, exp_ms=iat + 1000)
        self.assertEqual(PS.check_peer_share_envelope(env, {**ctx, 'marks': 'none'}), {'ok': False, 'refusal': 'malformed'})
        self.assertEqual(PS.check_peer_share_envelope(env, {**ctx, 'marks': None}), {'ok': False, 'refusal': 'malformed'})
        self.assertEqual(PS.open_peer_share(env, {**ctx, 'revoked': True}, private('rKem')),
                         {'ok': False, 'refusal': 'revoked'})
        fixed = bytes([7] * 32)
        self.assertEqual(PS.seal_peer_share(**base, sequence=1, exp_ms=iat + 1000, ephemeral=fixed)['header']['enc'],
                         PS.seal_peer_share(**base, sequence=1, exp_ms=iat + 1000, ephemeral=fixed)['header']['enc'])

    def test_no_value_in_what_travels(self):
        """The sealed envelope carries the value only in its ciphertext: not in the header, not in any member."""
        share_id = vectors()['envelopes'][0]['envelope']['header']['shareId']
        iat = relay.instant('2026-10-27T09:59:00Z')
        value = b'planted-value-0123456789'
        env = PS.seal_peer_share(recipient_kem=key('rKem')['jwk'], signer_sig=key('sSig')['jwk'], signer=private('sSig'),
                                 share_id=share_id, iat_ms=iat, slot='OPENROUTER_API_KEY', provider='openrouter',
                                 plaintext=value, sequence=1, exp_ms=iat + 1000)
        self.assertNotIn(value.decode(), json.dumps(env))


@needs_crypto
class Reports(unittest.TestCase):
    def test_reports(self):
        for case in vectors()['reports']:
            with self.subTest(case['name']):
                self.assertEqual(PS.open_peer_report(case['report'], case['context'], private('sKem')), case['expect'])
                if case['expect']['ok']:
                    self.assertEqual(PS.check_peer_report(case['report'], case['context']),
                                     {'ok': True, 'sequence': case['expect']['sequence']})

    def test_seal_report(self):
        first = vectors()['reports'][0]
        share_id, body = first['report']['header']['shareId'], first['expect']['body']
        rep = PS.seal_peer_report(recipient_kem=key('sKem')['jwk'], signer_sig=key('rSig')['jwk'], signer=private('rSig'),
                                  share_id=share_id, sequence=7, iat_ms=relay.instant('2026-10-27T09:59:00Z'), body=body)
        ctx = {'at': '2026-10-27T10:00:00Z', 'shareId': share_id, 'ownKem': key('sKem')['jwk'],
               'recipientSig': key('rSig')['jwk'], 'lastSequence': 6}
        self.assertEqual(PS.open_peer_report(rep, ctx, private('sKem')), {'ok': True, 'sequence': 7, 'body': body})
        with self.assertRaisesRegex(ValueError, 'bad-body'):
            PS.seal_peer_report(recipient_kem=key('sKem')['jwk'], signer_sig=key('rSig')['jwk'], signer=private('rSig'),
                                share_id=share_id, sequence=8, iat_ms=0, body={**body, 'value': 'sk'})
        with self.assertRaisesRegex(ValueError, 'bad-key'):
            PS.seal_peer_report(recipient_kem=key('sKem')['jwk'], signer_sig=key('rSig')['jwk'], signer=private('xSig'),
                                share_id=share_id, sequence=8, iat_ms=0, body=body)


@needs_crypto
class Records(unittest.TestCase):
    def test_records(self):
        for case in vectors()['records']:
            with self.subTest(case['name']):
                self.assertEqual(PS.peer_record_stands(case['record'], case['type'], case['shareId'], case['signerSig']),
                                 case['expect'])

    def test_sign(self):
        share_id = vectors()['records'][0]['record']['shareId']
        record = PS.sign_peer_record({'type': PS.PEER_REVOCATION_TYPE, 'shareId': share_id, 'at': '2026-10-28T10:00:00Z'},
                                     key('sSig')['jwk'], private('sSig'))
        self.assertEqual(PS.peer_record_stands(record, PS.PEER_REVOCATION_TYPE, share_id, key('sSig')['jwk']), {'ok': True})
        with self.assertRaisesRegex(ValueError, 'bad-key'):
            PS.sign_peer_record({'type': PS.PEER_REVOCATION_TYPE, 'shareId': share_id, 'at': '2026-10-28T10:00:00Z'},
                                key('sSig')['jwk'], private('xSig'))
        with self.assertRaisesRegex(ValueError, 'malformed'):
            PS.sign_peer_record({'type': PS.PEER_REVOCATION_TYPE, 'shareId': share_id, 'at': '2026-10-28T10:00:00Z',
                                 'note': 'x'}, key('sSig')['jwk'], private('sSig'))
        deletion = PS.sign_peer_record({'type': PS.PEER_DELETION_TYPE, 'shareId': share_id, 'at': '2026-10-28T10:05:00Z',
                                        'revocation': 'sha256:' + '0' * 64}, key('rSig')['jwk'], private('rSig'))
        self.assertEqual(PS.peer_record_stands(deletion, PS.PEER_DELETION_TYPE, share_id, key('rSig')['jwk']), {'ok': True})


class Fixtures(unittest.TestCase):
    """The contract's positive and negative fixtures of what crosses between the two estates: each positive one reads
    as its kind, each negative one does not."""

    def test_fixtures(self):
        pos, neg = PEER / 'positive', PEER / 'negative'
        frame = json.loads((pos / 'peer-share-frame-claim.json').read_text())
        self.assertIsNotNone(PS.peer_frame(frame))
        self.assertTrue(PS.peer_sealed_shape(json.loads((pos / 'peer-share-envelope.json').read_text()),
                                             PS.PEER_ENVELOPE_TYPE))
        self.assertTrue(PS.peer_sealed_shape(json.loads((pos / 'peer-share-report.json').read_text()), PS.PEER_REPORT_TYPE))
        self.assertIsNone(PS.peer_frame(json.loads((neg / 'peer-share-frame-claim-code.json').read_text())))
        self.assertFalse(PS.peer_sealed_shape(json.loads((neg / 'peer-share-envelope-value.json').read_text()),
                                              PS.PEER_ENVELOPE_TYPE))


if __name__ == '__main__':
    unittest.main()
