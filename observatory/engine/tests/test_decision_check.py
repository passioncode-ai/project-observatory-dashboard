#!/usr/bin/env python3
"""One operator-decision check (OBS-611): `node/actions.decided`, and the five wrappers that call it.

The configuration door, the key acts, the organization selection, the secret prompt's twins and the own-share acts
each checked the operator's relayed words (contract DEC-0121 AP-2) with their own wrapper around `action_decision`.
They differed on the refusal code's spelling, on a decision that names no object, on a null decision, on words that
carry something shaped like a credential, on words that are not valid Unicode, and on a person's caller that also
carries a decision. `MATRIX` pins each wrapper's answer to the same inputs; every row that changed with OBS-611 keeps
the answer before (`was`) and why it changed (`why`).
"""
from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from node import actions as A  # noqa: E402  before tools/ joins the path: tools/node.py would shadow the package

sys.path.insert(0, str(ROOT / 'tools'))

FAKE_KEY = 'sk-or-v1-' + 'FAKE' * 16
DIGEST = 'sha256:' + 'a' * 64
REQUEST = 'sreq_' + 'f' * 24
SHARE = '0123456789abcdef' * 2
NO = object()                                                      # no decision at all
NOW = '2026-11-17T10:00:00Z'


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def words(action: str, target=NO, instruction: str = 'Go ahead.', at=None) -> dict:
    out = {'by': 'operator', 'via': 'relayed-instruction', 'action': action, 'instruction': instruction,
           'at': at or stamp()}
    if target is not NO:
        out['target'] = target
    return out


class TheOneCheck(unittest.TestCase):
    """`decided()`: one rule for every relayed act of this engine."""

    ACT = {'id': 'configure.apply', 'title': 'Apply', 'tool': 'observatory.configure', 'gate': 'relayed'}

    def check(self, decision=NO, *, caller='agent:mcp', target=DIGEST, action=None):
        kwargs = {} if decision is NO else {'decision': decision}
        return A.decided(action=action or self.ACT, target=target, caller=caller, now=NOW, **kwargs)

    def ok(self, **extra) -> dict:
        return words('configure.apply', DIGEST, at='2026-11-17T09:59:00Z', **extra)

    def test_the_operators_words_for_this_act_and_object(self):
        answer = self.check(self.ok())
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['record'], {**self.ok(), 'caller': 'agent:mcp'})
        self.assertEqual(answer['recordAs'], {'by': 'operator', 'via': 'relayed-instruction', 'caller': 'agent:mcp'})

    def test_no_decision_and_a_null_one_are_both_no_decision(self):
        for decision in (NO, None):
            self.assertEqual(self.check(decision), {'ok': False, 'code': A.OPERATOR_DECISION_REQUIRED,
                                                    'why': 'absent'})

    def test_a_person_without_words_is_the_persons_act(self):
        for caller, via in (('person:cli', 'cli'), ('person:page', 'page')):
            self.assertEqual(self.check(caller=caller), {'ok': True, 'person': True,
                                                         'record': {'by': 'person', 'via': via}})

    def test_words_a_person_carries_are_read_like_any_others(self):
        self.assertEqual(self.check('yes', caller='person:cli')['code'], A.DECISION_INVALID)
        self.assertEqual(self.check(self.ok(), caller='person:page')['record']['caller'], 'person:page')

    def test_every_refusal_is_decision_invalid_with_its_reason(self):
        cases = {'shape': 'yes', 'unicode': self.ok(instruction='bad \ud800'),
                 'contract': {**self.ok(), 'by': 'agent'}, 'target': words('configure.apply', at=NOW),
                 'credential': self.ok(instruction='use ' + FAKE_KEY)}
        for why, decision in cases.items():
            with self.subTest(why):
                self.assertEqual(self.check(decision), {'ok': False, 'code': A.DECISION_INVALID, 'why': why})
        other = self.check(words('configure.apply', 'sha256:' + 'b' * 64, at=NOW))
        self.assertEqual((other['code'], other['why']), (A.DECISION_INVALID, 'contract'))

    def test_a_digest_or_the_object_itself_in_the_words_is_no_credential(self):
        self.assertTrue(self.check(self.ok(instruction=f'apply {DIGEST}'))['ok'])
        act = dict(A.SECRET_DECLINE)
        decision = words('secret.decline', REQUEST, f'decline {REQUEST}', at=NOW)
        self.assertTrue(self.check(decision, action=act, target=REQUEST)['ok'])
        self.assertFalse(self.check({**decision, 'instruction': f'decline x{REQUEST}y {FAKE_KEY}'}, action=act,
                                    target=REQUEST)['ok'])

    def test_an_object_carried_in_the_words_needs_no_target(self):
        link = 'https://mailbox.example/s/' + SHARE + '#c=' + 'A' * 22 + '&k=' + 'E' * 43
        answer = self.check(words('share.open', instruction=f'Open this share: {link}', at=NOW),
                            action=A.SHARE_OPEN, target=SHARE)
        self.assertTrue(answer['ok'], answer)

    def test_a_node_fault_is_never_a_decision(self):
        answer = A.decided(action=self.ACT, target=DIGEST, caller='agent:mcp', decision=self.ok(), now='soon')
        self.assertEqual(answer, {'ok': False, 'code': 'unavailable', 'why': 'fault', 'fault': 'now-unreadable'})

    def test_both_spellings_are_read_and_one_is_answered(self):
        self.assertEqual(A.refusal_code('operator_decision_required'), 'operator-decision-required')
        self.assertEqual(A.refusal_code('decision_invalid'), 'decision-invalid')
        self.assertEqual(A.refusal_code('plan_stale'), 'plan_stale')


# --- the five wrappers -------------------------------------------------------------------------------------------

def _configure(decision, caller='agent:mcp'):
    import configuration_door as C
    return C._decided('apply', DIGEST, caller, None if decision is NO else decision, ['features.auto_cleanup'])


def _key_acts(decision, caller=None):
    import key_acts as K
    return K.decided('keys.mint', 'Mint an OpenRouter key', 'observatory.key_act', 'alpha-key',
                     None if decision is NO else decision, label='mint')


def _org(decision, caller='agent:mcp'):
    import org_selection as O
    return O.decided('apply', 'example-org', None if decision is NO else decision, caller=caller)


def _secret(decision, caller=None):
    import secret_prompt as S
    return S._decided(A.SECRET_DECLINE, 'beta', S._NO_DECISION if decision is NO else decision, REQUEST)


def _own_share(decision, caller=None):
    import own_share as W
    with mock.patch.object(W, 'personal_node', lambda node: True):
        return W.gate(A.SHARE_ACCEPT_OWN, A.SHARE_ACCEPT, agent='claude-code',
                      decision=None if decision is NO else decision, share_id=SHARE, node=None, step='accept it')


WRAPPERS = {'configure': (_configure, 'configure.apply', DIGEST), 'key_acts': (_key_acts, 'keys.mint', 'alpha-key'),
            'org': (_org, 'org.select.apply', 'example-org'), 'secret': (_secret, 'secret.decline', REQUEST),
            'own_share': (_own_share, 'share.accept-own', SHARE)}
REFUSED = {'configure': 'Refusal', 'key_acts': 'Refusal', 'org': 'Refusal', 'secret': 'Refused',
           'own_share': 'Outside'}

ODR, INV, HUMAN = 'operator-decision-required', 'decision-invalid', 'human-step-required'

#: (wrapper, case) -> (answer now, answer before OBS-611 or None when unchanged, why it changed).
#: An answer is `operator`, `person` or the refusal code. The own-share acts answer every refusal
#: `human-step-required` (OM-2), so only their message names the code.
MATRIX = {
    ('configure', 'absent'): (ODR, 'operator_decision_required', 'the contract spelling, as every other door'),
    ('configure', 'null'): (ODR, 'operator_decision_required', 'the contract spelling'),
    ('configure', 'not-a-dict'): (INV, 'decision_invalid', 'the contract spelling'),
    ('configure', 'no-target'): (INV, 'decision_invalid', 'the contract spelling'),
    ('configure', 'other-target'): (INV, 'decision_invalid', 'the contract spelling'),
    ('configure', 'credential'): (INV, 'decision_invalid', 'the contract spelling'),
    ('configure', 'surrogate'): (INV, 'operator', 'words the journal cannot write are no decision'),
    ('configure', 'person-valid'): ('operator', 'person', 'words a caller carries are read, never ignored'),
    ('configure', 'person-bad'): (INV, 'person', 'words a caller carries are read, never ignored'),
    ('key_acts', 'digest-in-words'): ('operator', INV, 'a digest is not a credential'),
    ('key_acts', 'surrogate'): (INV, 'operator', 'words the journal cannot write are no decision'),
    ('secret', 'null'): (ODR, INV, 'null is no decision, as at every other door'),
    ('secret', 'no-target'): (INV, 'operator', 'a decision names the request it declines'),
    ('secret', 'credential'): (INV, 'operator', 'the audit record keeps the words: none shaped like a credential'),
    ('secret', 'surrogate'): (INV, 'operator', 'words the audit record cannot write are no decision'),
    ('own_share', 'no-target'): (HUMAN, 'operator', 'OM-1.3: the decision names this share id as its target'),
    ('own_share', 'credential'): (HUMAN, 'operator', 'the journal keeps the words: none shaped like a credential'),
    ('own_share', 'surrogate'): (HUMAN, 'operator', 'words the journal cannot write are no decision'),
}
#: What every wrapper answers when MATRIX names no row for it.
DEFAULT = {'absent': ODR, 'null': ODR, 'not-a-dict': INV, 'valid': 'operator', 'no-target': INV,
           'other-target': INV, 'credential': INV, 'digest-in-words': 'operator', 'target-in-words': 'operator',
           'surrogate': INV}
OWN_SHARE_DEFAULT = {**{k: HUMAN for k in DEFAULT}, 'valid': 'operator', 'digest-in-words': 'operator',
                     'target-in-words': 'operator'}


def cases(action: str, target: str) -> dict:
    return {'absent': NO, 'null': None, 'not-a-dict': 'yes', 'valid': words(action, target),
            'no-target': words(action), 'other-target': words(action, 'other-object'),
            'credential': words(action, target, 'use ' + FAKE_KEY),
            'digest-in-words': words(action, target, 'apply sha256:' + 'b' * 64),
            'target-in-words': words(action, target, f'decide {target}'),
            'surrogate': words(action, target, 'bad \ud800')}


def outcome(call) -> str:
    try:
        record = call()
    except Exception as exc:  # noqa: BLE001 - each wrapper's own refusal type
        code = getattr(exc, 'code', None) or getattr(exc, 'answer', {}).get('error')
        return f'{type(exc).__name__}:{code}'
    return record.get('by') if isinstance(record, dict) else repr(record)


class TheWrappers(unittest.TestCase):
    def expected(self, name: str, case: str) -> str:
        row = MATRIX.get((name, case))
        answer = row[0] if row else (OWN_SHARE_DEFAULT if name == 'own_share' else DEFAULT)[case]
        return answer if answer in ('operator', 'person') else f'{REFUSED[name]}:{answer}'

    def test_every_wrapper_answers_the_same_inputs_alike(self):
        for name, (call, action, target) in WRAPPERS.items():
            for case, decision in cases(action, target).items():
                with self.subTest(wrapper=name, case=case):
                    self.assertEqual(outcome(lambda: call(decision)), self.expected(name, case))

    def test_a_person_at_the_terminal_or_the_page(self):
        expected = {('configure', 'person:page'): 'person', ('configure', 'person:cli'): 'person',
                    ('org', 'cli'): 'person'}
        for (name, caller), answer in expected.items():
            call, action, target = WRAPPERS[name]
            with self.subTest(wrapper=name, caller=caller):
                self.assertEqual(outcome(lambda: call(NO, caller=caller)), answer)
                self.assertEqual(outcome(lambda: call(words(action, target), caller=caller)), 'operator')
                self.assertEqual(outcome(lambda: call('yes', caller=caller)), f'Refusal:{INV}')

    def test_the_own_share_message_names_the_code(self):
        for decision, code in ((NO, ODR), (None, ODR), (words('share.accept-own'), INV)):
            try:
                _own_share(decision)
            except Exception as exc:  # noqa: BLE001
                self.assertIn(code, exc.answer['why'])
            else:
                self.fail('accepted')

    def test_every_changed_row_says_why(self):
        for key, (now, was, why) in MATRIX.items():
            self.assertNotEqual(now, was, key)
            self.assertTrue(why and '—' not in why, key)


if __name__ == '__main__':
    unittest.main()
