#!/usr/bin/env python3
"""The node's secret envelope (OBS-110, RM-21 P5 slice 4): HPKE base mode (RFC 9180, suite 16/1/1), sealing and
opening `fabric-secret-envelope/1` in the reference's order of refusals, and the open context a target derives from its
applied estate-record revisions, run over the contract's `secret-envelope.json`. A round trip between two device-key
sets shows that what one node seals another opens once, and refuses a second envelope at the same sequence.
"""
from __future__ import annotations

import base64
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

from node import envelope, p256, relay                 # noqa: E402

import json                                            # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors() -> dict:
    return json.loads((VECTORS / 'secret-envelope.json').read_text(encoding='utf-8'))


def key_of(value):
    return base64.b64decode(value) if isinstance(value, str) else value


def context_of(ctx: dict) -> dict:
    """A vector's context as the port's callables."""
    holders = {(h['sender'], h['agentRef'], h['slot']) for h in ctx.get('holders', [])}
    placements = {(p['agentRef'], p['slot']): p['since'] for p in ctx.get('placements', [])}
    marks = {(m['agentRef'], m['slot']): m for m in ctx.get('marks', [])}
    return {
        'own_kem_kid': ctx['ownKemKid'],
        'own_kem_private_key': bytes.fromhex(ctx['ownKemPrivateKey']),
        'sender_key': lambda kid: key_of(ctx.get('senders', {}).get(kid)) if kid in ctx.get('senders', {}) else None,
        'may_hold': lambda sender, agent, slot: (sender, agent, slot) in holders,
        'current_placement': lambda agent, slot: placements.get((agent, slot)),
        'applied_revision': lambda: ctx.get('appliedRevision'),
        'last_opened': lambda agent, slot: marks.get((agent, slot)),
        'now_ms': relay.instant(ctx['now']),
    }


def answer_of(result: dict) -> dict:
    if result['ok']:
        return dict(result, secret=result['secret'].decode('utf-8'))
    return result


@needs_crypto
class Vectors(unittest.TestCase):
    def test_digest(self):
        data = vectors()
        self.assertEqual(envelope.envelope_digest(data['envelope']), data['digest'])

    def test_every_case(self):
        for case in vectors()['cases']:
            with self.subTest(case['name']):
                self.assertEqual(answer_of(envelope.open_envelope(case['envelope'], context_of(case['context']))),
                                 case['expect'])

    def test_sealing_reproduces_the_vector_bytes(self):
        """Sealed with RFC 9180 §A.3.1's ephemeral key, the header and ciphertext equal the vector's byte for byte."""
        data = vectors()
        header = data['envelope']['header']
        # The vector's ephemeral private key is RFC 9180 §A.3.1's skEm.
        sk_e = bytes.fromhex('4995788ef4b9d6132b249ce59a77281493eb39af373d236a1fe415cb0c2d7beb')
        sealed = envelope.seal_envelope(
            recipient_kid=header['recipient'], recipient_key=data['recipientKey'], sender_kid=header['sender'],
            signer=lambda data_bytes: 'A' * 86, agent_ref=header['agentRef'], slot=header['slot'],
            placement=header['placement'], envelope_sequence=header['envelopeSequence'],
            iat_ms=relay.instant(header['iat']), exp_ms=relay.instant(header['exp']),
            secret=data['open']['secret'].encode(), ephemeral_private_key=sk_e)
        self.assertEqual(sealed['header'], header)
        self.assertEqual(sealed['ct'], data['envelope']['ct'])

    def test_placement_context_cases(self):
        for case in vectors()['placement']['cases']:
            with self.subTest(case['name']):
                ctx = envelope.estate_envelope_context(case['applied'], case['machine'])
                for hold in case['expect']['holds']:
                    self.assertEqual(ctx['may_hold'](hold['sender'], case['agentRef'], case['slot']), hold['mayHold'])
                self.assertEqual(ctx['current_placement'](case['agentRef'], case['slot']),
                                 case['expect']['currentPlacement'])


@needs_crypto
class RoundTrip(unittest.TestCase):
    def test_a_secret_sealed_by_one_node_opens_once_on_another(self):
        from node.device_keys import DeviceKeys
        laptop, gpu = DeviceKeys.generate(), DeviceKeys.generate()
        agent, slot, now = 'urn:fabric:agent:example:inbox-digest', 'MAIL_TOKEN', relay.instant('2026-10-15T10:00:00Z')

        def seal(sequence, secret=b'not-a-real-secret'):
            return envelope.seal_envelope(recipient_kid=gpu.kem_kid, recipient_key=gpu.kem_public,
                                          sender_kid=laptop.sig_kid, signer=laptop.sign, agent_ref=agent, slot=slot,
                                          placement=3, envelope_sequence=sequence, iat_ms=now, exp_ms=now + 3_600_000,
                                          secret=secret)
        marks = {}
        ctx = {'own_kem_kid': gpu.kem_kid, 'own_kem_private_key': gpu.kem_private(),
               'sender_key': {laptop.sig_kid: laptop.sig_public}.get,
               'may_hold': lambda sender, a, s: sender == laptop.sig_kid,
               'current_placement': lambda a, s: 3, 'applied_revision': lambda: 3,
               'last_opened': lambda a, s: marks.get((a, s)), 'now_ms': now + 1000}
        first = seal(1)
        opened = envelope.open_envelope(first, ctx)
        self.assertEqual(opened['secret'], b'not-a-real-secret')
        marks[(agent, slot)] = opened['mark']
        self.assertTrue(envelope.open_envelope(first, ctx)['ok'])            # a repeat delivery of the same envelope
        self.assertEqual(envelope.open_envelope(seal(1, b'other'), ctx)['refusal'], 'sequence-reused')
        self.assertTrue(envelope.open_envelope(seal(2), ctx)['ok'])
        # Another machine's #kem key cannot open it, and a laptop's #kem is no recipient for a #sig signer.
        self.assertEqual(envelope.open_envelope(first, dict(ctx, own_kem_private_key=laptop.kem_private()))['refusal'],
                         'cannot-open')
        for bad in ({'recipient_kid': gpu.sig_kid}, {'sender_kid': laptop.kem_kid}, {'envelope_sequence': 0}):
            with self.assertRaises(ValueError):
                envelope.seal_envelope(**dict(dict(recipient_kid=gpu.kem_kid, recipient_key=gpu.kem_public,
                                                   sender_kid=laptop.sig_kid, signer=laptop.sign, agent_ref=agent,
                                                   slot=slot, placement=3, envelope_sequence=1, iat_ms=now,
                                                   exp_ms=now + 1000, secret=b'x'), **bad))
        with self.assertRaises(ValueError):                  # a life over 24 hours is refused before sealing
            envelope.seal_envelope(
                recipient_kid=gpu.kem_kid, recipient_key=gpu.kem_public, sender_kid=laptop.sig_kid, signer=laptop.sign,
                agent_ref=agent, slot=slot, placement=3, envelope_sequence=1, iat_ms=now,
                exp_ms=now + 86_401_000, secret=b'x')


if __name__ == '__main__':
    unittest.main()
