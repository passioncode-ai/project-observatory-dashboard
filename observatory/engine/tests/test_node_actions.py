#!/usr/bin/env python3
"""The relayed decision a button's agent twin carries (contract DEC-0121 AP-2, AP-4; OBS-291): the node's port of
`actionDecision` (`node/actions.py`) run over the contract's `actions.json`, and the audit redaction of a peer share
link (DEC-0147 item 4).

The port reads no mandate (DEC-0125), so a case that hands one is not in `actions.json`'s `cases`; the node's two
twins send none.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from node import actions as A  # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors' / 'actions' / 'actions.json'


def cases() -> list:
    return json.loads(VECTORS.read_text(encoding='utf-8'))['cases']


class Vectors(unittest.TestCase):
    def test_every_case(self):
        ran = 0
        for case in cases():
            with self.subTest(case['name']):
                self.assertNotIn('mandate', case['input'])
                self.assertEqual(A.action_decision(**case['input']), case['expected'])
                ran += 1
        self.assertGreaterEqual(ran, 60)


class Twins(unittest.TestCase):
    NOW = '2026-11-03T10:00:00Z'

    def decision(self, action: str, **extra) -> dict:
        return {'by': 'operator', 'via': 'relayed-instruction', 'action': action,
                'instruction': 'Decline that key request.', 'at': '2026-11-03T09:59:00Z', **extra}

    def test_the_twins_are_relayed(self):
        for action in (A.SECRET_DECLINE, A.SHARE_OPEN):
            self.assertEqual(action['gate'], 'relayed')
            self.assertNotIn('humanStep', action)
            self.assertEqual(A.action_decision(action=action, tier='T0', caller='agent:a', now=self.NOW),
                             {'ok': False, 'code': 'operator-decision-required'})

    def test_a_decision_for_the_twin(self):
        answer = A.action_decision(action=A.SECRET_DECLINE, tier='T0', caller='agent:a', target='sreq_1',
                                   decision=self.decision('secret.decline'), now=self.NOW)
        self.assertEqual(answer, {'ok': True, 'recordAs': {'by': 'operator', 'via': 'relayed-instruction',
                                                           'caller': 'agent:a'}})
        self.assertEqual(A.action_decision(action=A.SECRET_DECLINE, tier='T0', caller='agent:a', target='sreq_1',
                                           decision=self.decision('share.open'), now=self.NOW),
                         {'ok': False, 'code': 'decision-invalid'})

    def test_accept_stays_the_persons(self):
        """The recipient's Accept is the person's consent (PS-5, `humanStep: consent`): no decision makes it the
        agent's."""
        self.assertEqual(A.SHARE_ACCEPT['humanStep'], 'consent')
        self.assertEqual(A.action_decision(action=A.SHARE_ACCEPT, tier='T0', caller='agent:a', now=self.NOW,
                                           decision=self.decision(A.SHARE_ACCEPT['id']), humanStepUrl='/shares'),
                         {'ok': False, 'code': 'human-step-required',
                          'data': {'humanStep': 'consent', 'url': '/shares'}})

    def test_the_link_fragment_is_replaced_by_the_share_id(self):
        share = '1cbbf01dbcb842d86e9b25c6cb286dbd'
        link = f'https://mailbox.example/s/{share}#c=AAAAAAAAAAAAAAAAAAAAAA&k=' + 'B' * 43
        decision = self.decision('share.open', instruction=f'Open this share: {link} please')
        audit = A.redact_link(decision, link, share)
        self.assertNotIn('AAAAAAAAAAAAAAAAAAAAAA', json.dumps(audit))
        self.assertNotIn('B' * 43, json.dumps(audit))
        self.assertIn(f'https://mailbox.example/s/{share}#{share}', audit['instruction'])
        self.assertEqual(decision['instruction'], f'Open this share: {link} please')   # the caller's copy unchanged


if __name__ == '__main__':
    unittest.main()
