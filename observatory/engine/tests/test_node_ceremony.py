#!/usr/bin/env python3
"""The ceremony surface (OBS-197, NODE slice 5): the screen a desktop app draws for one pairing session, read through
the ceremony checks of OBS-133 (`node/pairing.py`, contract DEC-0084), and the steps a person takes on it.

The screens are driven by the contract's own `pairing.json`: every rendezvous case that carries this machine's
commitment gives the screen its verdict implies, every join case gives `confirmed` or the refusal it names, and the
code shown is the vectors' checked code. The steps run on a temporary workspace folder; nothing reaches a network,
because no hub runs pairing yet and the engine says so (`hub-unreachable`, never a pretend wait or success).
"""
from __future__ import annotations

import copy
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

from node import ceremony, pairing, relay              # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')


def vectors() -> dict:
    return json.loads((VECTORS / 'pairing.json').read_text(encoding='utf-8'))


def complete() -> list:
    """Both commitments, then both reveals: the rendezvous every screen below is cut from."""
    return copy.deepcopy(vectors()['rendezvous'][0]['messages'])


def side(role: str) -> dict:
    """A role's side as it committed, read back from its reveal in the complete rendezvous."""
    reveal = next(m for m in complete() if m['step'] == 'reveal' and m['role'] == role)
    return {k: v for k, v in reveal.items() if k not in ('schema', 'step')}


def session(role: str, messages: list, opened_at: str = '2026-10-15T09:00:00Z', **extra) -> dict:
    return dict({'schema': ceremony.SESSION_SCHEMA, 'role': role, 'own': side(role), 'openedAt': opened_at,
                 'messages': messages}, **extra)


def ms(text: str) -> int:
    return relay.strict_time(text)


AT = ms('2026-10-15T09:02:00Z')
GENESIS = side('trusted')['genesis']
CODE = vectors()['checkedCodes'][0]['answer']['code']


@needs_crypto
class ScreensFromTheVectors(unittest.TestCase):
    def test_no_session_is_not_paired(self):
        self.assertEqual(ceremony.pairing_screen(None, AT), {'schema': 'node-ceremony/1', 'state': 'not-paired'})

    def test_every_rendezvous_that_carries_this_machines_commitment(self):
        ran = 0
        for case in vectors()['rendezvous']:
            for role in pairing.PAIRING_ROLES:
                mine = {'schema': pairing.PAIRING_SCHEMA, 'step': 'commit', 'role': role,
                        'commit': pairing.pairing_commit(side(role))['commit']}
                if not isinstance(case['messages'], list) or mine not in case['messages'] \
                        or relay.strict_time(case['openedAt']) is None:
                    continue
                with self.subTest(case['name'], role=role):
                    ran += 1
                    s = session(role, case['messages'], case['openedAt'])
                    at = ms(case['at'])
                    answer = case['answer']
                    for hub in ceremony.HUB_STATES:
                        screen = ceremony.pairing_screen(s, at, hub)
                        if answer['state'] == 'complete':
                            self.assertEqual(screen['state'], 'code-shown')
                            self.assertEqual(screen['code'], CODE)
                        elif answer['state'] == 'open':
                            self.assertEqual(screen['state'], 'waiting-for-hub' if hub == 'ready' else 'hub-unreachable')
                            self.assertEqual(screen['step'], 'other-side')
                            if hub != 'ready':
                                self.assertEqual(screen['hub'], hub)
                        elif answer['reason'] == 'expired':
                            self.assertEqual(screen['state'], 'expired')
                        else:
                            own_abort = answer['reason'] == 'aborted' and \
                                case['messages'][answer['index']].get('role') == role
                            self.assertEqual(screen['state'], 'refused')
                            self.assertEqual(screen['reason'], 'codes-differ' if own_abort else answer['reason'])
                            self.assertEqual(screen['index'], answer['index'])
        self.assertGreaterEqual(ran, 30, 'the vectors exercised the screens')

    def test_the_code_is_shown_with_the_estate_fingerprint_and_the_time_left(self):
        screen = ceremony.pairing_screen(session('new', complete()), AT)
        self.assertEqual(screen['state'], 'code-shown')
        self.assertEqual(screen['code'], CODE)
        self.assertEqual(screen['fingerprint'], pairing.estate_fingerprint(GENESIS)['fingerprint'])
        self.assertEqual(screen['secondsLeft'], 480)
        self.assertEqual(screen['expiresAt'], '2026-10-15T09:10:00Z')

    def test_a_revision_without_the_persons_confirmation_is_never_confirmed(self):
        new, trusted = complete()[2], complete()[3]
        case = next(c for c in vectors()['join']
                    if c['answer']['ok'] and c['new'] == new and c['trusted'] == trusted)
        for role in pairing.PAIRING_ROLES:
            with self.subTest(role=role):
                s = session(role, complete(), record=case['record'], recordAt='2026-10-15T09:03:00Z')
                s.pop('confirmed', None)
                if role == 'new':
                    s['genesis'] = case['genesis']
                screen = ceremony.pairing_screen(s, ms('2026-10-15T11:00:00Z'))
                self.assertNotEqual(screen['state'], 'confirmed')

    def test_every_join_case_gives_confirmed_or_its_refusal(self):
        new, trusted = complete()[2], complete()[3]
        ran = 0
        for case in vectors()['join']:
            if case['new'] != new or case['trusted'] != trusted:
                continue                                 # the swapped reveals are another rendezvous, not this one
            for role in pairing.PAIRING_ROLES:
                with self.subTest(case['name'], role=role):
                    ran += 1
                    s = session(role, complete(), confirmed=True, record=case['record'],
                                recordAt='2026-10-15T09:03:00Z')
                    if role == 'new':
                        s['genesis'] = case['genesis']
                    elif case['genesis'] != GENESIS:
                        continue                         # a trusted machine judges by its own revision 1
                    # A revision that arrived inside the rendezvous stands after its 600 s.
                    screen = ceremony.pairing_screen(s, ms('2026-10-15T11:00:00Z'))
                    if case['answer']['ok']:
                        self.assertEqual(screen['state'], 'confirmed')
                        self.assertEqual(screen['machine'], pairing.machine_of_keys(new['keys']))
                        self.assertEqual(screen['fingerprint'], pairing.estate_fingerprint(GENESIS)['fingerprint'])
                    else:
                        self.assertEqual(screen['state'], 'refused')
                        self.assertEqual(screen['reason'], case['answer']['problem'])
        self.assertGreaterEqual(ran, 15)

    def test_each_refusal_has_a_group_a_screen_words(self):
        groups = {'codes-differ', 'aborted', 'mismatch', 'other-estate', 'not-listed', 'trusted-not-listed'}
        for reason in ('commit-mismatch', 'repeated', 'out-of-order', 'closed', 'malformed', 'other-keys',
                       'not-listed', 'other-estate', 'trusted-not-listed', 'aborted', 'codes-differ'):
            with self.subTest(reason):
                self.assertIn(ceremony._refused(reason)['group'], groups)
        self.assertEqual(ceremony._refused('other-keys')['group'], 'not-listed')
        self.assertEqual(ceremony._refused('repeated')['group'], 'mismatch')


@needs_crypto
class ScreensAfterTheCodes(unittest.TestCase):
    def test_the_trusted_side_sees_what_the_passkey_would_sign_before_it_signs(self):
        new_keys = side('new')['keys']
        for hub in ceremony.HUB_STATES:
            with self.subTest(hub):
                screen = ceremony.pairing_screen(session('trusted', complete(), confirmed=True), AT, hub)
                self.assertEqual(screen['state'], 'passkey')
                self.assertEqual(screen['machine'], pairing.machine_of_keys(new_keys))
                self.assertEqual([k['kid'] for k in screen['keys']], [k['kid'] for k in new_keys])
                for shown, key in zip(screen['keys'], new_keys):
                    self.assertEqual(shown['fingerprint'], ceremony.device_key_fingerprint(key))
                    self.assertRegex(shown['fingerprint'], r'^sha256:[0-9a-f]{64}$')
                self.assertEqual(screen['canSign'], hub == 'ready')
                self.assertEqual(screen['code'], CODE)

    def test_the_new_side_waits_for_its_revision_and_says_when_the_hub_is_the_wait(self):
        s = session('new', complete(), confirmed=True)
        ready = ceremony.pairing_screen(s, AT, 'ready')
        self.assertEqual((ready['state'], ready['step']), ('waiting-for-hub', 'revision'))
        for hub in ('not-ready', 'unreachable'):
            screen = ceremony.pairing_screen(s, AT, hub)
            self.assertEqual((screen['state'], screen['step'], screen['hub']), ('hub-unreachable', 'revision', hub))

    def test_an_unknown_hub_state_is_unreachable_never_ready(self):
        screen = ceremony.pairing_screen(session('new', complete()[:2]), AT, 'maybe')
        self.assertEqual((screen['state'], screen['hub']), ('hub-unreachable', 'unreachable'))

    def test_confirmed_codes_still_expire_without_a_revision(self):
        s = session('new', complete(), confirmed=True)
        self.assertEqual(ceremony.pairing_screen(s, ms('2026-10-15T09:10:01Z'), 'ready')['state'], 'expired')

    def test_a_revision_judged_outside_its_rendezvous_is_refused(self):
        record = vectors()['join'][0]['record']
        s = session('new', complete(), confirmed=True, record=record, recordAt='2026-10-15T09:11:00Z', genesis=GENESIS)
        self.assertEqual(ceremony.pairing_screen(s, AT)['reason'], 'malformed')

    def test_a_malformed_session_is_refused_never_raised(self):
        good = session('new', complete())
        broken = [
            'not a session',
            dict(good, schema='another/1'),
            dict(good, role='observer'),
            dict(good, messages='none'),
            dict(good, openedAt='2026-10-15 09:00:00'),
            dict(good, own=side('trusted')),                     # a side of another role
            dict(good, own=dict(side('new'), nonce='short')),    # a side the commit refuses
            dict(good, messages=complete()[1:]),                 # this machine's commitment is missing
            {'schema': 'unreadable'},
        ]
        for value in broken:
            with self.subTest(str(value)[:60]):
                screen = ceremony.pairing_screen(value, AT)
                self.assertEqual((screen['state'], screen['reason'], screen['group']),
                                 ('refused', 'malformed', 'mismatch'))
        self.assertEqual(ceremony.pairing_screen(good, True)['reason'], 'malformed')
        self.assertEqual(ceremony.pairing_screen(good, None)['reason'], 'malformed')

    def test_every_state_is_one_the_screens_draw(self):
        seen = {ceremony.pairing_screen(None, AT)['state']}
        for s, at, hub in ((session('new', complete()[:2]), AT, 'not-ready'),
                           (session('new', complete()[:2]), AT, 'ready'),
                           (session('new', complete()), AT, 'ready'),
                           (session('trusted', complete(), confirmed=True), AT, 'ready'),
                           (session('new', complete(), confirmed=True, record=vectors()['join'][0]['record'],
                                    recordAt='2026-10-15T09:03:00Z', genesis=GENESIS), AT, 'ready'),
                           (session('new', complete() + [{'schema': pairing.PAIRING_SCHEMA, 'step': 'abort',
                                                          'role': 'new'}]), AT, 'ready'),
                           (session('new', complete()), ms('2026-10-15T09:20:00Z'), 'ready')):
            seen.add(ceremony.pairing_screen(s, at, hub)['state'])
        self.assertEqual(seen, set(ceremony.STATES))


@needs_crypto
class Steps(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name).resolve() / 'node'
        self.device = self.folder / 'device'
        self.now = ms('2026-10-15T09:02:00Z')

    def step(self, op, role=None, hub=None, at=None):
        return ceremony.step(self.folder, self.device, op, role, at or self.now, hub)

    def refused(self, op, role=None, hub=None):
        with self.assertRaises(ceremony.CeremonyRefused) as caught:
            self.step(op, role, hub)
        return str(caught.exception)

    def plant(self, value):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / ceremony.SESSION_FILE).write_text(json.dumps(value), encoding='utf-8')

    def test_with_no_session_the_screen_is_not_paired_and_the_hub_is_not_ready(self):
        answer = self.step('status')
        self.assertEqual(answer, {'screen': {'schema': 'node-ceremony/1', 'state': 'not-paired'}, 'hub': 'not-ready'})
        self.assertFalse(self.folder.exists(), 'a status wrote something')

    def test_start_commits_to_this_machines_keys_and_says_the_hub_is_the_wait(self):
        answer = self.step('start', 'new')
        self.assertEqual(answer['screen']['state'], 'hub-unreachable')
        self.assertEqual((answer['screen']['step'], answer['screen']['hub']), ('other-side', 'not-ready'))
        self.assertEqual(answer['screen']['secondsLeft'], 600)
        path = self.folder / ceremony.SESSION_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(path.read_text())
        from node.device_keys import DeviceKeys
        self.assertEqual(stored['own']['keys'], DeviceKeys.load(self.device).enrollment_keys())
        self.assertRegex(stored['own']['nonce'], r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(stored['messages'], [{'schema': pairing.PAIRING_SCHEMA, 'step': 'commit', 'role': 'new',
                                               'commit': pairing.pairing_commit(stored['own'])['commit']}])
        self.assertEqual(self.refused('start', 'new'), 'ceremony-in-progress')
        self.assertEqual(self.refused('confirm'), 'not-now')
        self.assertEqual(self.refused('differ'), 'not-now')
        self.assertEqual(self.refused('sign'), 'not-now')

    def test_two_starts_never_reuse_a_nonce(self):
        first = self.step('start', 'new')
        nonce = json.loads((self.folder / ceremony.SESSION_FILE).read_text())['own']['nonce']
        self.step('cancel')
        self.step('start', 'new')
        self.assertNotEqual(json.loads((self.folder / ceremony.SESSION_FILE).read_text())['own']['nonce'], nonce)
        self.assertEqual(first['screen']['state'], 'hub-unreachable')

    def test_a_machine_with_no_estate_cannot_be_the_trusted_side(self):
        self.assertEqual(self.refused('start', 'trusted'), 'no-estate')
        self.assertFalse((self.folder / ceremony.SESSION_FILE).exists())
        self.assertEqual(self.refused('start', 'observer'), 'invalid-input')
        self.assertEqual(self.refused('start'), 'invalid-input')
        self.assertEqual(self.refused('open-the-door'), 'invalid-input')

    def test_a_session_that_ran_out_may_be_started_again(self):
        self.step('start', 'new')
        later = ms('2026-10-15T09:13:00Z')
        self.assertEqual(self.step('status', at=later)['screen']['state'], 'expired')
        self.assertEqual(self.step('start', 'new', at=later)['screen']['state'], 'hub-unreachable')

    def test_the_codes_match_the_new_side_waits_for_its_revision(self):
        self.plant(session('new', complete()))
        self.assertEqual(self.step('status', hub='ready')['screen']['code'], CODE)
        answer = self.step('confirm', hub='ready')
        self.assertEqual((answer['screen']['state'], answer['screen']['step']), ('waiting-for-hub', 'revision'))
        self.assertTrue(json.loads((self.folder / ceremony.SESSION_FILE).read_text())['confirmed'])
        self.assertEqual(self.refused('confirm', hub='ready'), 'not-now')

    def test_the_codes_differ_this_side_aborts_and_the_pairing_stops(self):
        self.plant(session('new', complete()))
        answer = self.step('differ', hub='ready')
        self.assertEqual((answer['screen']['state'], answer['screen']['reason'], answer['screen']['group']),
                         ('refused', 'codes-differ', 'codes-differ'))
        stored = json.loads((self.folder / ceremony.SESSION_FILE).read_text())
        self.assertEqual(stored['messages'][-1], {'schema': pairing.PAIRING_SCHEMA, 'step': 'abort', 'role': 'new'})
        self.assertEqual(self.step('start', 'new')['screen']['state'], 'hub-unreachable')

    def test_the_passkey_never_signs_while_the_hub_serves_no_page(self):
        self.plant(session('trusted', complete()))
        self.assertEqual(self.step('confirm', hub='ready')['screen']['state'], 'passkey')
        for hub in ('ready', 'not-ready'):
            self.assertEqual(self.refused('sign', hub=hub), 'hub-not-ready')
        self.assertEqual(self.step('status')['screen']['canSign'], False)

    def test_a_damaged_session_is_shown_and_can_be_cancelled(self):
        self.folder.mkdir(parents=True)
        (self.folder / ceremony.SESSION_FILE).write_text('{', encoding='utf-8')
        self.assertEqual(self.step('status')['screen']['reason'], 'malformed')
        self.assertEqual(self.step('cancel')['screen']['state'], 'not-paired')
        self.assertFalse((self.folder / ceremony.SESSION_FILE).exists())
        self.assertEqual(self.step('cancel')['screen']['state'], 'not-paired')


@needs_crypto
class OverTheAppProtocol(unittest.TestCase):
    """`project-observatory full assistant ceremony`: what the desktop apps call, JSON in and out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {'HOME': str(self.home), 'OBSERVATORY_HOME': str(self.home / 'ws')}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        import workspace
        workspace.initialize(self.home / 'ws')
        import paths
        importlib.reload(paths)
        from agent import assistant
        self.a = importlib.reload(assistant)

    def call(self, doc) -> tuple[int, dict]:
        out = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(doc).encode('utf-8')))
        with patch.object(sys, 'stdin', stdin), patch('sys.stdout', out):
            code = self.a.main(['ceremony'])
        return code, json.loads(out.getvalue())

    def test_status_start_and_cancel_over_the_protocol(self):
        self.assertEqual(self.call({'op': 'status'}), (0, {'screen': {'schema': 'node-ceremony/1',
                                                                       'state': 'not-paired'}, 'hub': 'not-ready'}))
        code, answer = self.call({'op': 'start', 'role': 'new'})
        self.assertEqual((code, answer['screen']['state']), (0, 'hub-unreachable'))
        self.assertTrue((self.home / 'ws' / 'node' / ceremony.SESSION_FILE).exists())
        self.assertTrue((self.home / 'ws' / 'node' / 'device' / 'device.json').exists())
        self.assertEqual(self.call({'op': 'start', 'role': 'new'}), (1, {'error': 'ceremony-in-progress'}))
        self.assertEqual(self.call({'op': 'cancel'})[1]['screen']['state'], 'not-paired')

    def test_unknown_members_and_kinds_are_refused(self):
        for doc in ({'op': 'start', 'role': 'new', 'shell': 'rm'}, {'op': 3}, {'op': 'start', 'role': ['new']}, {}):
            with self.subTest(doc):
                self.assertEqual(self.call(doc), (1, {'error': 'invalid-input'}))


if __name__ == '__main__':
    unittest.main()
