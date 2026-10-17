#!/usr/bin/env python3
"""The revocation delta and the timestamp heartbeat (node/revocation.py, contract DEC-0054), run over the contract's
own vectors (`fabric/node-vectors/revocation-delta.json`, vendored at a named commit): every apply, heartbeat,
freshness and reconciliation case gives the reference's answer, the hashes and the genesis are the reference's bytes,
and the Ed25519 signatures reproduce from their seeds. Then what a machine does with them: the 72-hour rule over a
state the delta and a heartbeat built, the revocations a fork keeps in force, and exact matching of revoked refs.
"""
from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import jcs, p256                              # noqa: E402
from node import revocation as R                        # noqa: E402

VECTORS = json.loads((ROOT / 'fabric' / 'node-vectors' / 'revocation-delta.json').read_text(encoding='utf-8'))
RECORD = VECTORS['record']
E1, E2, E3 = (VECTORS['entries'][name] for name in ('e1', 'e2', 'e3'))
ESTATE = RECORD['estate']
AT = R.delta_time('2026-10-12T12:00:00Z')
TRUST = {'record': RECORD, 'at': AT}
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def trust_of(case: dict) -> dict:
    """A case's trust: `trust.at` is a string a port parses; an unreadable one is no clock."""
    at = R.delta_time(case['trust']['at'])
    return {'record': case['trust']['record'], 'at': None if math.isnan(at) else at}


def held(entries, **extra) -> dict:
    return dict({'estate': ESTATE, 'entries': list(entries)}, **extra)


def resign(document: dict, kid: str = None) -> dict:
    seed = bytes.fromhex(VECTORS['keys'][kid or document['kid']]['seed'])
    body = {key: value for key, value in document.items() if key != 'sig'}
    return dict(body, sig=p256.b64url_encode(Ed25519PrivateKey.from_private_bytes(seed).sign(R.signing_input(body))))


class Documents(unittest.TestCase):
    def test_genesis_and_entry_hashes(self):
        self.assertEqual(R.delta_genesis(ESTATE), VECTORS['genesis']['sha256'])
        self.assertEqual(jcs.rfc8785({'type': R.DELTA_TYPE, 'estate': ESTATE, 'sequence': 0}), VECTORS['genesis']['input'])
        for entry in (E1, E2, E3):
            self.assertEqual(R.signing_input(entry['entry']).decode('utf-8'), entry['signingInput'])
            self.assertEqual(R.delta_entry_hash(entry['entry']), entry['hash'])
        self.assertEqual(E1['entry']['prev'], VECTORS['genesis']['sha256'])
        self.assertEqual(E2['entry']['prev'], E1['hash'])
        self.assertEqual(E3['entry']['prev'], E2['hash'])

    @needs_crypto
    def test_every_signature_reproduces_from_its_seed(self):
        for entry in (E1, E2, E3):
            self.assertEqual(resign(entry['entry'])['sig'], entry['entry']['sig'])
        for case in VECTORS['heartbeats']:
            if case['expect']['ok'] is True:
                self.assertEqual(resign(case['heartbeat'])['sig'], case['heartbeat']['sig'], case['name'])

    def test_constants(self):
        self.assertEqual(R.HEARTBEAT_STALE_SECONDS, 72 * 3600)
        self.assertEqual(R.MAX_FUTURE_SECONDS, 60)
        self.assertEqual(R.DELTA_KINDS, ('machine', 'device-key', 'hub-key', 'publisher-key', 'package', 'grant'))


@needs_crypto
class Vectors(unittest.TestCase):
    def test_apply(self):
        for case in VECTORS['apply']:
            with self.subTest(case['name']):
                verdict = R.apply_delta(copy.deepcopy(case['state']), copy.deepcopy(case['page']), trust_of(case))
                expect = case['expect']
                if expect['ok']:
                    self.assertEqual((verdict['ok'], verdict['applied']), (True, expect['applied']))
                    self.assertEqual(R.delta_head(verdict['state']), expect['head'])
                else:
                    self.assertEqual((verdict['ok'], verdict['problem'], verdict['index']),
                                     (False, expect['problem'], expect['index']))
                    if 'head' in expect:
                        self.assertEqual(R.delta_head(verdict['state']), expect['head'])
                    self.assertEqual(verdict['state'].get('broken') is True, expect.get('broken') is True)

    def test_heartbeats(self):
        for case in VECTORS['heartbeats']:
            with self.subTest(case['name']):
                verdict = R.accept_heartbeat(copy.deepcopy(case['state']), copy.deepcopy(case['heartbeat']), trust_of(case))
                expect = case['expect']
                if expect['ok']:
                    self.assertEqual((verdict['ok'], verdict['newer']), (True, expect['newer']))
                else:
                    self.assertEqual((verdict['ok'], verdict['problem']), (False, expect['problem']))
                    self.assertEqual(verdict['state'].get('broken') is True, expect.get('broken') is True)

    def test_freshness(self):
        for case in VECTORS['freshness']:
            given = case['input']
            with self.subTest(case['name']):
                answer = R.freshness_answer(R.delta_time(given['at']), given['applied'], given.get('heartbeat'),
                                            given.get('broken'))
                self.assertEqual(answer['answer'], case['answer'])

    def test_reconciliation(self):
        for case in VECTORS['reconciliation']:
            with self.subTest(case['name']):
                self.assertEqual(R.reconcile_revision(held(case['entries']), case['revision']), case['expect'])


@needs_crypto
class WhatAMachineDoes(unittest.TestCase):
    def test_the_72_hour_rule_over_a_built_state(self):
        applied = R.apply_delta(R.empty_delta_state(ESTATE), {'head': {}, 'entries': [E1['entry'], E2['entry']]}, TRUST)
        self.assertTrue(applied['ok'])
        self.assertEqual(R.delta_freshness(applied['state'], TRUST)['remote'], 'refuse')
        heartbeat = VECTORS['heartbeats'][0]['heartbeat']
        accepted = R.accept_heartbeat(applied['state'], heartbeat, TRUST)
        self.assertTrue(accepted['ok'])
        self.assertEqual(R.delta_freshness(accepted['state'], TRUST),
                         dict(R.delta_freshness(accepted['state'], TRUST), answer='current', remote='accept'))
        later = R.delta_time(heartbeat['at']) + R.HEARTBEAT_STALE_SECONDS * 1000 + 1
        self.assertEqual(R.delta_freshness(accepted['state'], {'record': RECORD, 'at': later})['answer'], 'stale')
        self.assertEqual(R.delta_freshness(accepted['state'], {'record': RECORD, 'at': None})['answer'], 'stale')
        self.assertEqual(R.delta_freshness(accepted['state'], {'record': dict(RECORD, revision=2), 'at': AT})['answer'],
                         'behind')
        self.assertEqual(R.delta_freshness(dict(accepted['state'], broken=True), TRUST)['answer'], 'broken')
        ahead = R.accept_heartbeat(applied['state'], VECTORS['heartbeats'][1]['heartbeat'], TRUST)
        self.assertEqual(R.delta_freshness(ahead['state'], TRUST)['answer'], 'behind')
        for corrupt in (dict(heartbeat, sequence='nine'), dict(heartbeat, at='not a time'), dict(heartbeat, deltaHead='x'),
                        None):
            verdict = R.accept_heartbeat(dict(accepted['state'], heartbeat=corrupt), VECTORS['heartbeats'][1]['heartbeat'],
                                         TRUST)
            self.assertEqual((verdict['ok'], verdict['problem']), (False, 'malformed'))
        self.assertEqual(R.freshness_answer(R.delta_time(heartbeat['at']), {'sequence': '3', 'revision': 1}, heartbeat,
                                            False)['answer'], 'behind')

    def test_a_fork_keeps_every_revocation_in_force(self):
        fork = next(c for c in VECTORS['apply'] if c['expect'].get('problem') == 'fork')
        verdict = R.apply_delta(copy.deepcopy(fork['state']), copy.deepcopy(fork['page']), TRUST)
        revoked = R.effective_revocations(RECORD, verdict['state'])
        self.assertTrue(R.is_revoked(revoked, 'hub-key', 'hub-m-9a07-1'))
        self.assertTrue(R.is_revoked(revoked, 'package', E2['entry']['revocation']['ref']))
        self.assertTrue(R.is_revoked(revoked, 'machine', 'm-9a07'))
        self.assertEqual(R.delta_revocations(verdict['state']),
                         [E1['entry']['revocation'], E2['entry']['revocation'], fork['page']['entries'][0]['revocation']])
        self.assertEqual(R.effective_revocations({'revocations': [{'kind': 'grant', 'ref': 'g-1'}]}, None),
                         [{'kind': 'grant', 'ref': 'g-1'}])

    def test_revoked_refs_match_exactly_and_grants_by_normal_form(self):
        grants = [{'kind': 'grant', 'ref': 'urn:grant:estate:g-1'}, {'kind': 'grant', 'ref': 9}]
        self.assertTrue(R.is_revoked(grants, 'grant', 'URN:grant:estate:g-1'))
        self.assertFalse(R.is_revoked(grants, 'grant', 'urn:grant:estate:G-1'))
        self.assertFalse(R.is_revoked(grants, 'grant', '9'))
        self.assertFalse(R.is_revoked([{'kind': 'machine', 'ref': 'Dev:m1'}], 'machine', 'dev:m1'))
        self.assertTrue(R.is_revoked([{'kind': 'machine', 'ref': 'Dev:m1'}], 'machine', 'Dev:m1'))
        numbers = [{'kind': 'machine', 'ref': '5'}, {'kind': 'hub-key', 'ref': 7}]
        self.assertTrue(R.is_revoked(numbers, 'machine', '5'))
        self.assertFalse(R.is_revoked(numbers, 'machine', 5))
        self.assertFalse(R.is_revoked(numbers, 'hub-key', '7'))
        self.assertFalse(R.is_revoked(numbers, 'Machine', '5'))
        numbered = dict(RECORD, machines=[dict(RECORD['machines'][0], id=5)])
        self.assertIn('key', R.estate_signer(numbered, RECORD['machines'][0]['hubKey']['kid'],
                                              [{'kind': 'machine', 'ref': '5'}]))
        revision = {'issuedAt': '2026-10-12T10:00:00Z',
                    'revocations': [dict(E1['entry']['revocation'], ref=[E1['entry']['revocation']['ref']])]}
        self.assertEqual(R.reconcile_revision(held([E1['entry']]), revision)['problem'], 'unreconciled')

    def test_no_relay_breaks_a_machine(self):
        other = dict(E2['entry'], revocation=dict(E2['entry']['revocation'], ref='grant-x'))
        verdict = R.apply_delta(held([E1['entry'], E2['entry']]), {'head': {}, 'entries': [other]}, TRUST)
        self.assertEqual(verdict['problem'], 'bad-signature')
        self.assertNotIn('broken', verdict['state'])

    def test_entries_before_a_refused_one_stay_applied(self):
        verdict = R.apply_delta(R.empty_delta_state(ESTATE),
                                {'head': {}, 'entries': [E1['entry'], E2['entry'], dict(E3['entry'], sig='x')]}, TRUST)
        self.assertEqual((verdict['problem'], verdict['index']), ('malformed', 2))
        self.assertEqual(R.delta_head(verdict['state']), {'sequence': 2, 'sha256': E2['hash']})

    def test_a_revision_leaving_out_an_applied_entry_is_refused(self):
        state = held([E1['entry']])
        self.assertEqual(R.reconcile_revision(state, {'issuedAt': '2026-10-12T10:00:00Z', 'revocations': []})['problem'],
                         'unreconciled')
        self.assertEqual(R.reconcile_revision(state, {'issuedAt': '2026-10-12T10:00:00Z',
                                                      'revocations': [E1['entry']['revocation']]}), {'ok': True})

    def test_times_are_instants_in_any_offset(self):
        self.assertEqual(R.delta_time('2026-10-15T20:00:00+09:00'), R.delta_time('2026-10-15T11:00:00Z'))
        self.assertTrue(math.isnan(R.delta_time('2026-10-12T10:00:00')))

    def test_a_datetime_clock(self):
        import datetime
        at = datetime.datetime(2026, 10, 12, 12, tzinfo=datetime.timezone.utc)
        self.assertTrue(R.apply_delta(R.empty_delta_state(ESTATE), {'head': {}, 'entries': [E1['entry']]},
                                      {'record': RECORD, 'at': at})['ok'])
        naive = R.apply_delta(R.empty_delta_state(ESTATE), {'head': {}, 'entries': [E1['entry']]},
                              {'record': RECORD, 'at': at.replace(tzinfo=None)})
        self.assertEqual(naive['problem'], 'no-clock')


if __name__ == '__main__':
    unittest.main()
