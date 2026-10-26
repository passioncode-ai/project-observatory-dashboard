#!/usr/bin/env python3
"""The repair runner (contract DEC-0101, DEC-0102, DEC-0117; FAC-SEM-068, FAC-SEM-129; engine OBS-193).

The decision runs over the contract's own vectors (`fabric/node-vectors/repair/repair-decision.json`, every case), the
settings canonical form over the contract's canonical vectors, the request and result shapes against the vendored
schemas and fixtures, and FAC-SEM-068's remote-execution case. Then the book is held to what the node owes: the person
told before anything starts and able to decline, a decision taken again at the moment it would start, only an
allow-listed lifecycle action, one run and one stored result per id, every step journalled without a secret. Last, an
enrollment against the fake organization server: with no signed policy applied (today, until OE-06) every repair is
refused `bad-signature`, recorded, and answered to a server listing `repair-result/1`, and the check-in does not list
`repair-request/1`; with a policy and an allow-list injected, the whole run goes through to `succeeded`.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import sqlite3
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

from node import repair                                # noqa: E402
from node.relay import strict_time                     # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
REPAIR = VECTORS / 'repair'
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
AGENT = 'urn:fabric:agent:example:observatory-repair'
DIGEST = 'sha256:' + 'b2' * 32
KEY_ID = 'example-org.policy-2026-10'


def load(name: str):
    return json.loads((REPAIR / name).read_text(encoding='utf-8'))


def validators():
    """The vendored request and result schemas with what they refer to, or None without `jsonschema`."""
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return None
    docs = [load(n) for n in ('repair-request.schema.json', 'repair-result.schema.json', 'device-common.schema.json',
                              'fabric-package.schema.json')]
    docs += [json.loads((VECTORS / 'summary' / n).read_text(encoding='utf-8'))
             for n in ('common.schema.json', 'activity-common.schema.json')]
    registry = Registry().with_resources((d['$id'], Resource.from_contents(d)) for d in docs)
    make = lambda d: jsonschema.Draft202012Validator(d, registry=registry,             # noqa: E731
                                                     format_checker=jsonschema.FormatChecker())
    return make(docs[0]), make(docs[1])


def signing_key(seed_hex: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))


def public_b64(private) -> str:
    from cryptography.hazmat.primitives import serialization
    return base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw,
                                                              serialization.PublicFormat.Raw)).decode()


def sign(request: dict, private, key_id: str = KEY_ID) -> dict:
    body = {k: v for k, v in request.items() if k != 'signature'}
    value = private.sign(repair.policy_signing_input(body))
    return dict(body, signature={'alg': 'ed25519', 'keyId': key_id, 'value': base64.b64encode(value).decode()})


def iso(seconds: float) -> str:
    return repair._iso(seconds)


class Vectors(unittest.TestCase):
    """FAC-SEM-129: the port answers every case of the contract's shared vectors."""

    @needs_crypto
    def test_every_case_answers_as_the_contract(self):
        data = load('repair-decision.json')
        self.assertGreaterEqual(len(data['cases']), 49)          # a lower bound: the contract adds cases
        for case in data['cases']:
            with self.subTest(case['name']):
                self.assertEqual(repair.repair_decision(case['input']), case['expect'])

    def test_the_signing_input_is_the_vectors_byte_for_byte(self):
        data = load('repair-decision.json')
        request = data['cases'][0]['input']['request']
        self.assertEqual(repair.policy_signing_input(request).decode('utf-8'), data['signingInput'])

    @needs_crypto
    def test_the_vector_keys_are_the_seeds(self):
        data = load('repair-decision.json')
        for name in ('policy', 'other'):
            self.assertEqual(public_b64(signing_key(data['keys'][name]['seed'])), data['keys'][name]['publicKey'])

    def test_every_reason_is_in_the_contract_order(self):
        data = load('repair-decision.json')
        seen = {case['expect'].get('reason') for case in data['cases']} - {None}
        self.assertEqual(seen, set(repair.REASONS))
        self.assertNotIn('already-answered', repair.RESULT_REASONS)          # never sent in a result (DEC-0102)


class Canonical(unittest.TestCase):
    """The settings form over the contract's canonical vectors (DEC-0043)."""

    def setUp(self):
        self.data = json.loads((VECTORS / 'canonical.json').read_text(encoding='utf-8'))['settings']

    def test_every_vector(self):
        for case in self.data['vectors']:
            value = case['input'] if 'input' in case else json.loads(case['inputJson'])
            with self.subTest(case['name']):
                self.assertEqual(repair.settings_canonical_json(value), case['expected'])

    def test_every_refusal(self):
        for case in self.data['refuses']:
            with self.subTest(case['name']):
                with self.assertRaises(repair.CanonicalError):
                    repair.settings_canonical_json(json.loads(case['inputJson']))

    def test_a_whole_float_is_the_integer_javascript_reads(self):
        self.assertEqual(repair.settings_canonical_json([1.0, -0.0, 100.0]), '[1, 0, 100]')
        self.assertEqual(repair.settings_canonical_json([True, False, 1]), '[true, false, 1]')


class Shapes(unittest.TestCase):
    """The request and result shapes agree with the vendored schemas over every fixture and the mutations."""

    def setUp(self):
        self.request = load('positive/repair-request.json')

    def mutations(self):
        r = self.request
        yield 'positive', r
        yield 'two-line task', load('negative/repair-request-task-two-lines.json')
        yield 'an extra member', dict(r, extra=1)
        yield 'no signature', {k: v for k, v in r.items() if k != 'signature'}
        yield 'another type', dict(r, type='repair-request/2')
        yield 'an id with a space', dict(r, id='rep 1')
        yield 'an e-mail id', dict(r, id='a@b.example')
        yield 'a short task', dict(r, task='')
        yield 'a task of 301', dict(r, task='x' * 301)
        yield 'a task of 300', dict(r, task='x' * 300)
        yield 'a line separator', dict(r, task='a b')
        yield 'a version with a leading zero', dict(r, package=dict(r['package'], version='01.2.0'))
        yield 'a digest in capitals', dict(r, package=dict(r['package'], digest='sha256:' + 'B2' * 32))
        yield 'an agent ref of another kind', dict(r, package=dict(r['package'], agentRef='urn:fabric:pkg:a:b'))
        yield 'a package member more', dict(r, package=dict(r['package'], path='x'))
        yield 'an alg not allowed', dict(r, signature=dict(r['signature'], alg='rs256'))
        yield 'a short signature', dict(r, signature=dict(r['signature'], value='AAAA'))
        yield 'a time without a zone', dict(r, issuedAt='2026-10-22T09:00:00')
        yield 'a device member more', dict(r, device={'id': 'dev-01j9a', 'name': 'x'})
        yield 'an org without id', dict(r, org={})
        yield 'not an object', ['repair-request/1']

    def test_request_problems_agree_with_the_schema(self):
        checks = validators()
        for name, doc in self.mutations():
            with self.subTest(name):
                problems = repair.request_problems(doc)
                if name == 'a time without a zone':
                    # `format: date-time` asserts only where the validator asserts formats (the contract loads
                    # ajv-formats; Python's jsonschema needs another package): the port reads it as the contract does.
                    self.assertTrue(problems)
                elif checks is not None:
                    self.assertEqual(bool(problems), bool(list(checks[0].iter_errors(doc))), problems)
        self.assertEqual(repair.request_problems(self.request), [])
        self.assertTrue(repair.request_problems(load('negative/repair-request-task-two-lines.json')))

    def test_result_fixtures(self):
        checks = validators()
        for name in ('repair-result', 'repair-result-refused', 'repair-result-org-cloud'):
            doc = load('positive/%s.json' % name)
            self.assertEqual(repair.result_problems(doc), [], name)
            if checks is not None:
                self.assertEqual(list(checks[1].iter_errors(doc)), [], name)
        for name in ('repair-result-already-answered', 'repair-result-refused-without-reason',
                     'repair-result-succeeded-unnotified'):
            doc = load('negative/%s.json' % name)
            self.assertTrue(repair.result_problems(doc), name)
            if checks is not None:
                self.assertTrue(list(checks[1].iter_errors(doc)), name)

    def test_build_result_refuses_what_the_schema_refuses(self):
        built = repair.build_result('rep-1', 'succeeded', '2026-10-22T09:07:30Z', notified_at='2026-10-22T09:05:00Z',
                                    started_at='2026-10-22T09:06:00Z')
        self.assertEqual(built, load('positive/repair-result.json') | {'id': 'rep-1'})
        for bad in ({'outcome': 'succeeded'}, {'outcome': 'refused'}, {'outcome': 'refused', 'reason': 'already-answered'}):
            with self.subTest(bad), self.assertRaises(ValueError):
                repair.build_result('rep-1', bad['outcome'], '2026-10-22T09:07:30Z', reason=bad.get('reason'))


class RemoteExecution(unittest.TestCase):
    """FAC-SEM-068's remote execution: what `policy-expired` is decided by."""

    def test_the_contract_case(self):
        case = load('semantic/device-action-expired-remote-execution.json')['input']
        allowed = repair.remote_execution_allowed(case['layers'], strict_time(case['at']))
        # The fixture is a wrong decision (`allow` after the server layer expired) that FAC-SEM-068 flags.
        self.assertEqual(case['decision'], 'allow')
        self.assertFalse(allowed)

    def test_the_bounds(self):
        layers = [{'source': 'server', 'expiresAt': '2026-10-19T08:00:00Z'}]
        at = strict_time('2026-10-19T08:00:00Z')
        self.assertTrue(repair.remote_execution_allowed(layers, at))             # at the instant itself: not after it
        self.assertFalse(repair.remote_execution_allowed(layers, at + 1))
        self.assertTrue(repair.remote_execution_allowed([{'source': 'server'}], at))     # no expiresAt: no expiry
        self.assertTrue(repair.remote_execution_allowed([{'source': 'user', 'expiresAt': '2020-01-01T00:00:00Z'}], at))
        self.assertTrue(repair.remote_execution_allowed([], at))

    def test_an_unreadable_expiry_stops_it(self):
        """The safe reading: the contract's `Date.parse` makes an unreadable bound never expire; here it has."""
        for bad in ('next week', '2026-10-19 08:00:00Z', 7, None):
            with self.subTest(bad):
                self.assertFalse(repair.remote_execution_allowed([{'source': 'server', 'expiresAt': bad}], 0))
        self.assertFalse(repair.remote_execution_allowed([], float('nan')))


@needs_crypto
class Signatures(unittest.TestCase):

    def test_es256_raw_point_and_r_s(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        private = ec.generate_private_key(ec.SECP256R1())
        point = private.public_key().public_bytes(serialization.Encoding.X962,
                                                  serialization.PublicFormat.UncompressedPoint)
        r, s = decode_dss_signature(private.sign(b'body', ec.ECDSA(hashes.SHA256())))
        sig = base64.b64encode(r.to_bytes(32, 'big') + s.to_bytes(32, 'big')).decode()
        key = {'alg': 'es256', 'publicKey': base64.b64encode(point).decode()}
        self.assertTrue(repair.verify_with(key, b'body', sig))
        self.assertFalse(repair.verify_with(key, b'other', sig))
        self.assertFalse(repair.verify_with(dict(key, alg='ed25519'), b'body', sig))
        self.assertFalse(repair.verify_with(key, b'body', 'AAAA'))

    def test_a_small_order_key_never_verifies(self):
        from node import ed25519
        for raw in ed25519.SMALL_ORDER_ENCODINGS:
            raw = bytes.fromhex(raw) if isinstance(raw, str) else raw
            key = {'publicKey': base64.b64encode(raw).decode()}
            self.assertFalse(repair.verify_with(key, b'x', base64.b64encode(b'\0' * 64).decode()))


class FakeExecutor:
    """The lifecycle door, faked: what was started, what each poll answers."""

    def __init__(self):
        self.started, self.polled = [], []
        self.answers = ['running', 'succeeded']
        self.refuse = None

    def start(self, action, request_id):
        if self.refuse:
            raise repair.ExecutorError(self.refuse)
        self.started.append((action['action'], action['targetId'], request_id))
        return {'operationId': 'op-%d' % len(self.started)}

    def poll(self, operation_id):
        self.polled.append(operation_id)
        state = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {'state': state, 'code': 'ready' if state == 'succeeded' else 'failed'}


@needs_crypto
class Book(unittest.TestCase):
    """The runner over one in-memory book, its decision inputs injected."""

    T = 1792396800.0                                     # 2026-10-19T08:00:00Z

    def setUp(self):
        self.private = signing_key('11' * 32)
        self.key = {'keyId': KEY_ID, 'alg': 'ed25519', 'publicKey': public_b64(self.private)}
        self.layers = [{'source': 'server', 'revision': 7, 'expiresAt': iso(self.T + 30 * 86400),
                        'keys': {'repair.packages': {'value': [{'agentRef': AGENT, 'digest': DIGEST}]}}}]
        self.executor = FakeExecutor()
        self.agents = {AGENT: {'action': 'restart', 'targetId': 'example-service'}}
        self.lines = []
        self.now = self.T
        self.role = None
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(repair.SCHEMA)
        self.addCleanup(self.conn.close)

    def ctx(self, **over):
        values = dict(org='example-org', device='dev-1', role=self.role, keys=[self.key], layers=self.layers,
                      agents=self.agents, executor=self.executor, now=self.now,
                      journal=lambda event, **f: self.lines.append(dict(f, event=event)))
        values.update(over)
        return repair.Context(**values)

    def request(self, rid='rep-1', **over):
        doc = {'type': 'repair-request/1', 'org': {'id': 'example-org'}, 'id': rid, 'device': {'id': 'dev-1'},
               'package': {'agentRef': AGENT, 'version': '1.2.0', 'digest': DIGEST},
               'task': 'Restart the example service', 'issuedAt': iso(self.T - 60), 'expiresAt': iso(self.T + 86400)}
        doc.update(over)
        return sign(doc, self.private)

    def row(self, rid='rep-1'):
        return self.conn.execute('SELECT * FROM repairs WHERE id = ?', (rid,)).fetchone()

    def result(self, rid='rep-1'):
        return json.loads(self.row(rid)['result'])

    def test_no_key_set_applied_is_refused_bad_signature_and_recorded(self):
        counts = repair.receive(self.conn, self.ctx(keys=[], layers=[]), [self.request()])
        self.assertEqual(counts['refused'], 1)
        self.assertEqual(self.result(), {'type': 'repair-result/1', 'id': 'rep-1', 'outcome': 'refused',
                                         'reason': 'bad-signature', 'at': iso(self.T)})
        self.assertEqual([l['event'] for l in self.lines], ['repair-received', 'repair-answered'])
        self.assertEqual(self.executor.started, [])

    def test_a_package_the_policy_does_not_list(self):
        repair.receive(self.conn, self.ctx(), [self.request(package={'agentRef': AGENT, 'version': '1.2.0',
                                                                      'digest': 'sha256:' + 'c3' * 32})])
        self.assertEqual(self.result()['reason'], 'not-allowlisted')

    def test_another_device(self):
        repair.receive(self.conn, self.ctx(), [self.request(device={'id': 'dev-2'})])
        self.assertEqual(self.result()['reason'], 'wrong-device')

    def test_no_local_action_fails_before_the_person_is_told(self):
        repair.receive(self.conn, self.ctx(agents={}), [self.request()])
        result = self.result()
        self.assertEqual((result['outcome'], result['reason']), ('failed', 'run-failed'))
        self.assertNotIn('notifiedAt', result)
        self.assertEqual(self.row()['note'], 'no-local-action')
        self.assertNotIn('repair-notified', [l['event'] for l in self.lines])

    def test_the_person_is_told_first_and_it_runs_after_the_notice(self):
        repair.receive(self.conn, self.ctx(), [self.request()])
        self.assertEqual(self.row()['state'], 'notified')
        shown = repair.report(self.conn)['pending']
        self.assertEqual([(p['id'], p['task'], p['agentRef'], p['runsAfter']) for p in shown],
                         [('rep-1', 'Restart the example service', AGENT, iso(self.T + repair.NOTICE_SECONDS))])
        self.now = self.T + repair.NOTICE_SECONDS - 1
        self.assertEqual(repair.advance(self.conn, self.ctx())['waiting'], 1)
        self.assertEqual(self.executor.started, [])
        self.now = self.T + repair.NOTICE_SECONDS
        self.assertEqual(repair.advance(self.conn, self.ctx())['started'], 1)
        self.assertEqual(self.executor.started,
                         [('restart', 'example-service', repair.lifecycle_request_id('example-org', 'rep-1'))])
        self.assertEqual(self.row()['state'], 'running')
        self.now += 60
        self.assertEqual(repair.advance(self.conn, self.ctx())['succeeded'], 1)
        self.assertEqual(self.result(), {'type': 'repair-result/1', 'id': 'rep-1', 'outcome': 'succeeded',
                                         'notifiedAt': iso(self.T), 'startedAt': iso(self.T + repair.NOTICE_SECONDS),
                                         'at': iso(self.T + repair.NOTICE_SECONDS + 60)})
        self.assertEqual(repair.report(self.conn)['pending'], [])
        self.assertEqual(len(self.executor.started), 1)

    def test_a_declined_repair_never_runs(self):
        repair.receive(self.conn, self.ctx(), [self.request()])
        self.now += 30
        self.assertEqual(repair.decline(self.conn, self.ctx(), 'rep-1'), 'notified')
        self.assertEqual((self.result()['outcome'], self.result()['reason']), ('refused', 'person-declined'))
        self.assertEqual(self.result()['notifiedAt'], iso(self.T))
        self.now += repair.NOTICE_SECONDS
        repair.advance(self.conn, self.ctx())
        self.assertEqual(self.executor.started, [])
        self.assertIsNone(repair.decline(self.conn, self.ctx(), 'rep-none'))
        self.assertEqual(repair.decline(self.conn, self.ctx(), 'rep-1'), 'answered')

    def test_decided_again_when_it_would_start(self):
        """A request that expired, or a policy that expired, while the notice stood is refused, never started."""
        repair.receive(self.conn, self.ctx(), [self.request('rep-1', expiresAt=iso(self.T + 600))])
        self.layers[0]['expiresAt'] = iso(self.T + 700)
        repair.receive(self.conn, self.ctx(), [self.request('rep-2')])
        self.now = self.T + repair.NOTICE_SECONDS
        repair.advance(self.conn, self.ctx())
        self.assertEqual(self.result('rep-1')['reason'], 'expired')
        self.assertEqual(self.result('rep-2')['reason'], 'policy-expired')
        self.assertEqual(self.executor.started, [])

    def test_a_refused_start_and_a_failed_run(self):
        repair.receive(self.conn, self.ctx(), [self.request('rep-1'), self.request('rep-2')])
        self.now = self.T + repair.NOTICE_SECONDS
        self.executor.refuse = 'in_use_by_person'
        repair.advance(self.conn, self.ctx())
        self.assertEqual((self.result('rep-1')['outcome'], self.row('rep-1')['note']), ('failed', 'in_use_by_person'))
        self.executor.refuse = None
        repair.receive(self.conn, self.ctx(), [self.request('rep-3')])
        self.now += repair.NOTICE_SECONDS
        self.executor.answers = ['held_off']
        repair.advance(self.conn, self.ctx())
        self.now += 60
        repair.advance(self.conn, self.ctx())
        result = self.result('rep-3')
        self.assertEqual((result['outcome'], result['reason'], self.row('rep-3')['note']),
                         ('failed', 'run-failed', 'failed'))
        self.assertIn('startedAt', result)

    def test_a_run_that_never_ends_times_out(self):
        repair.receive(self.conn, self.ctx(), [self.request()])
        self.now = self.T + repair.NOTICE_SECONDS
        self.executor.answers = ['running']
        repair.advance(self.conn, self.ctx())
        self.now += repair.RUN_TIMEOUT_SECONDS + 1
        repair.advance(self.conn, self.ctx())
        self.assertEqual((self.result()['outcome'], self.row()['note']), ('failed', 'timeout'))

    def test_once_per_id_the_same_result_again(self):
        repair.receive(self.conn, self.ctx(keys=[]), [self.request()])
        first = self.result()
        repair.mark_sent(self.conn, ['rep-1'], self.now)
        self.assertEqual(repair.due_results(self.conn), [])
        self.now += 3600
        counts = repair.receive(self.conn, self.ctx(), [self.request()])         # now it would pass: still refused
        self.assertEqual(counts['redelivered'], 1)
        self.assertEqual(repair.due_results(self.conn), [first])
        self.assertEqual(self.executor.started, [])

    def test_a_crash_after_starting_is_started_again_under_the_same_request_id(self):
        repair.receive(self.conn, self.ctx(), [self.request()])
        self.conn.execute("UPDATE repairs SET state = 'starting', started_at = ? WHERE id = 'rep-1'",
                          (iso(self.T + repair.NOTICE_SECONDS),))
        self.now = self.T + repair.NOTICE_SECONDS + 5
        repair.advance(self.conn, self.ctx())
        self.assertEqual(self.executor.started[0][2], repair.lifecycle_request_id('example-org', 'rep-1'))
        self.assertEqual(self.row()['started_at'], iso(self.T + repair.NOTICE_SECONDS))

    def test_an_org_cloud_machine_runs_at_once_its_notice_the_verification(self):
        self.role = 'org-cloud'
        repair.receive(self.conn, self.ctx(), [self.request()])
        repair.advance(self.conn, self.ctx())
        self.now += 60
        repair.advance(self.conn, self.ctx())
        result = self.result()
        self.assertEqual((result['outcome'], result['notifiedAt'], result['startedAt']),
                         ('succeeded', iso(self.T), iso(self.T)))

    def test_a_malformed_request_is_never_shown_or_run(self):
        two_lines = self.request(task='run this\nthen that')
        repair.receive(self.conn, self.ctx(), [two_lines, {'id': 'a b'}, 'x', None])
        self.assertEqual((self.result()['reason'], self.row()['note']), ('bad-signature', 'shape'))
        self.assertIsNone(self.row()['task'])
        self.assertEqual(repair.report(self.conn)['pending'], [])
        self.assertEqual(sum(1 for l in self.lines if l['event'] == 'repair-malformed'), 3)

    def test_at_most_sixteen_requests_per_response(self):
        counts = repair.receive(self.conn, self.ctx(keys=[]), [self.request('rep-%d' % i) for i in range(20)])
        self.assertEqual(counts['received'], 16)
        self.assertEqual(self.lines[0]['event'], 'repair-over-limit')

    def test_ended_enrollment_drops_what_is_pending_and_keeps_answered_ids(self):
        repair.receive(self.conn, self.ctx(keys=[]), [self.request('rep-1')])
        repair.receive(self.conn, self.ctx(), [self.request('rep-2')])
        self.assertEqual(repair.drop_pending(self.conn), 1)
        self.assertIsNotNone(self.row('rep-1'))
        self.assertIsNone(self.row('rep-2'))

    def test_every_result_reads_against_the_schema_and_no_line_holds_a_secret(self):
        repair.receive(self.conn, self.ctx(), [self.request('rep-1'), self.request('rep-2', device={'id': 'x'})])
        self.now += repair.NOTICE_SECONDS
        repair.advance(self.conn, self.ctx())
        self.now += 60
        repair.advance(self.conn, self.ctx())
        checks = validators()
        for row in self.conn.execute("SELECT result FROM repairs WHERE result IS NOT NULL"):
            doc = json.loads(row['result'])
            self.assertEqual(repair.result_problems(doc), [])
            if checks is not None:
                self.assertEqual(list(checks[1].iter_errors(doc)), [])
        text = json.dumps(self.lines)
        self.assertNotIn(self.request()['signature']['value'], text)
        self.assertNotIn('Restart the example service', text)


class Hostile(Book):
    """The review of PR #366: what a hostile organization server sends never stops a check-in, never fills the disk,
    and never skips the person."""

    def test_a_lone_surrogate_task_is_refused_and_never_raises(self):
        """MEDIUM 1: JSON `"\\ud800"` passed the task's character check and the SQLite insert raised."""
        doc = self.request()
        doc['task'] = 'fix \ud800 now'
        counts = repair.receive(self.conn, self.ctx(), [doc])
        self.assertEqual(counts['refused'], 1)
        self.assertEqual((self.result()['reason'], self.row()['note'], self.row()['task']),
                         ('bad-signature', 'lone-surrogate', None))
        self.assertIn('/ lone-surrogate', repair.request_problems(doc))
        self.assertTrue(repair.request_problems({'\udc00': 1}))

    def test_a_deeply_nested_request_never_raises(self):
        """MEDIUM 1: a request nested about 700 deep raised RecursionError in the canonical form."""
        deep = 'x'
        for _ in range(5000):
            deep = [deep]
        doc = dict(self.request(), package=deep)
        counts = repair.receive(self.conn, self.ctx(), [doc])
        self.assertEqual(counts['refused'], 1)
        self.assertEqual(self.row()['note'], 'too-deep')
        self.assertEqual(repair.request_problems(doc), ['/ too-deep'])

    def test_any_failure_on_one_request_is_a_code_and_the_rest_go_on(self):
        original = repair.request_problems

        def broken(doc):
            if doc.get('id') == 'rep-1':
                raise RuntimeError('a secret-looking text that must not be written')
            return original(doc)
        repair.request_problems = broken
        self.addCleanup(setattr, repair, 'request_problems', original)
        counts = repair.receive(self.conn, self.ctx(keys=[]), [self.request('rep-1'), self.request('rep-2')])
        self.assertEqual((counts['malformed'], counts['refused']), (1, 1))
        self.assertIsNone(self.row('rep-1'))
        self.assertEqual(self.result('rep-2')['reason'], 'bad-signature')
        line = next(l for l in self.lines if l['event'] == 'repair-unreadable')
        self.assertEqual(line, {'event': 'repair-unreadable', 'repair': 'rep-1', 'error': 'RuntimeError'})
        self.assertNotIn('secret-looking', json.dumps(self.lines))

    def test_bodies_are_kept_only_while_a_repair_is_pending(self):
        """LOW 4: a refused request keeps no body, an answered one drops it, one larger than 64 KB is refused."""
        repair.receive(self.conn, self.ctx(), [dict(self.request('rep-1'), extra=1)])
        self.assertEqual(self.row('rep-1')['request'], '{}')
        repair.receive(self.conn, self.ctx(), [dict(self.request('rep-2'), extra='x' * (70 * 1024))])
        self.assertEqual((self.row('rep-2')['note'], self.row('rep-2')['request']), ('too-large', '{}'))
        repair.receive(self.conn, self.ctx(), [self.request('rep-3')])
        self.assertNotEqual(self.row('rep-3')['request'], '{}')            # notified: its body is read to start it
        repair.decline(self.conn, self.ctx(), 'rep-3')
        self.assertEqual(self.row('rep-3')['request'], '{}')

    def test_the_book_is_bounded(self):
        """LOW 4: at most `MAX_PENDING` repairs wait and `MAX_BOOK_ROWS` rows are kept; past them a request is not
        recorded (the server sends it again later); an answered id is forgotten only after any request could expire."""
        self.addCleanup(setattr, repair, 'MAX_PENDING', repair.MAX_PENDING)
        self.addCleanup(setattr, repair, 'MAX_BOOK_ROWS', repair.MAX_BOOK_ROWS)
        repair.MAX_PENDING, repair.MAX_BOOK_ROWS = 2, 4
        repair.receive(self.conn, self.ctx(), [self.request('rep-%d' % i) for i in range(3)])
        self.assertEqual(len(repair.report(self.conn)['pending']), 2)
        self.assertIsNone(self.row('rep-2'))
        repair.receive(self.conn, self.ctx(keys=[]), [self.request('rep-%d' % i) for i in range(10, 14)])
        total = self.conn.execute('SELECT COUNT(*) AS n FROM repairs').fetchone()['n']
        self.assertEqual(total, 4)
        self.assertIn('repair-book-full', [l['event'] for l in self.lines])
        repair.mark_sent(self.conn, ['rep-10', 'rep-11'], self.now)
        self.now += (repair.MAX_REPAIR_DAYS + 1) * 86400 + 1
        repair.advance(self.conn, self.ctx())                               # prunes what can no longer run
        self.assertIsNone(self.row('rep-10'))
        self.assertIsNotNone(self.row('rep-0'))                              # not yet taken by a server: kept


class Forgetting(Book):
    """The review of #366 at 661ac7f9: an answered id is forgotten only after its own request expired, never by when it
    was answered (contract CONTEXT.md: one result per id)."""

    def test_a_request_that_arrived_early_is_kept_until_it_expired(self):
        early = self.request(issuedAt=iso(self.T + 3 * 86400), expiresAt=iso(self.T + 10 * 86400))
        repair.receive(self.conn, self.ctx(), [early])
        self.assertEqual(self.result()['reason'], 'expired')                 # before its issuedAt
        repair.mark_sent(self.conn, ['rep-1'], self.now)
        self.now = self.T + 9 * 86400                                        # inside its window, 9 days on
        repair.advance(self.conn, self.ctx())
        self.assertIsNotNone(self.row())
        counts = repair.receive(self.conn, self.ctx(), [early])
        self.assertEqual(counts['redelivered'], 1)                          # the same result again, never run
        self.assertEqual(self.executor.started, [])
        self.assertEqual(self.result()['reason'], 'expired')
        repair.mark_sent(self.conn, ['rep-1'], self.now)
        self.now = self.T + 10 * 86400 + repair.PRUNE_MARGIN_SECONDS + 1
        repair.advance(self.conn, self.ctx())
        self.assertIsNone(self.row())

    def test_a_row_without_a_readable_expiry_is_kept(self):
        repair.receive(self.conn, self.ctx(), [dict(self.request(), expiresAt='not a time')])
        self.assertIsNone(self.row()['expires_at'])
        repair.mark_sent(self.conn, ['rep-1'], self.now)
        self.now += 400 * 86400
        repair.advance(self.conn, self.ctx())
        self.assertIsNotNone(self.row())


class Config(unittest.TestCase):

    def setUp(self):
        self.folder = Path(tmpdir.mkdtemp()).resolve()
        os.chmod(self.folder, 0o700)
        self.path = self.folder / repair.CONFIG_NAME

    def write(self, doc, mode=0o600):
        self.path.write_text(json.dumps(doc), encoding='utf-8')
        os.chmod(self.path, mode)

    def test_absent_is_nothing_to_run(self):
        self.assertEqual(repair.read_config(self.path), (repair.empty_config(), None))

    def test_a_valid_allow_list(self):
        doc = {'schemaVersion': 1, 'agents': {AGENT: {'action': 'ensure_running', 'targetId': 'example-app'}},
               'lifecycle': {'bearerSlot': 'node/prod/REPAIR_BINDING'}}
        self.write(doc)
        config, problem = repair.read_config(self.path)
        self.assertIsNone(problem)
        self.assertEqual(config['agents'], doc['agents'])
        self.assertIsInstance(repair.executor_from(config), repair.LifecycleExecutor)
        self.assertIsNone(repair.executor_from(dict(config, lifecycle=None)))

    def test_what_runs_nothing(self):
        cases = [
            ({'schemaVersion': 1, 'agents': {AGENT: {'action': 'stop', 'targetId': 'x'}}}, 'agents'),
            ({'schemaVersion': 1, 'agents': {AGENT: {'action': 'restart', 'targetId': 'x', 'command': 'sh'}}},
             'agents'),
            ({'schemaVersion': 1, 'agents': {'not-a-ref': {'action': 'restart', 'targetId': 'x'}}}, 'agents'),
            ({'schemaVersion': 1, 'agents': {AGENT: {'action': 'restart', 'targetId': '../x'}}}, 'agents'),
            ({'schemaVersion': 1, 'agents': {}, 'lifecycle': {'bearerSlot': 'one'}}, 'lifecycle'),
            ({'schemaVersion': 2, 'agents': {}}, 'schemaVersion'),
        ]
        for doc, word in cases:
            with self.subTest(doc):
                self.write(doc)
                config, problem = repair.read_config(self.path)
                self.assertEqual(config, repair.empty_config())
                self.assertIn(word, problem)
        self.write({'schemaVersion': 1, 'agents': {}}, mode=0o644)
        self.assertIn('owner-only', repair.read_config(self.path)[1])
        self.path.unlink()
        other = self.folder / 'other.json'
        other.write_text('{}')
        self.path.symlink_to(other)
        self.assertIn('symbolic link', repair.read_config(self.path)[1])


class LifecycleDoor(unittest.TestCase):
    """The executor speaks to `node.lifecycle`'s own door as any caller does; the bearer goes in one header only."""

    def setUp(self):
        self.calls = []
        self.answers = {}

    def transport(self, url, headers, body, timeout):
        doc = json.loads(body)
        self.calls.append((url, headers, doc))
        name = doc['params']['name']
        answer = self.answers.get(name)
        if callable(answer):
            answer = answer(doc['params']['arguments'])
        if isinstance(answer, tuple):
            return answer
        return 200, json.dumps({'jsonrpc': '2.0', 'id': doc['id'],
                                'result': {'structuredContent': answer, 'isError': False}}).encode()

    def test_list_then_restart_then_poll(self):
        self.answers = {'lifecycle.list': {'targets': [{'targetId': 'example-service', 'policyRevision': 4}],
                                           'grantRevision': 2},
                        'lifecycle.restart': {'operationId': 'op-9', 'state': 'running'},
                        'lifecycle.operation.get': {'operationId': 'op-9', 'state': 'ready'}}
        door = repair.LifecycleExecutor(lambda: 'bearer-value', port=47999, transport=self.transport)
        self.assertEqual(door.start({'action': 'restart', 'targetId': 'example-service'}, 'repair-abc'),
                         {'operationId': 'op-9', 'state': 'running'})
        self.assertEqual(door.poll('op-9'), {'state': 'succeeded', 'code': 'ready'})
        self.assertEqual([c[2]['params']['name'] for c in self.calls],
                         ['lifecycle.list', 'lifecycle.restart', 'lifecycle.operation.get'])
        self.assertEqual(self.calls[1][2]['params']['arguments'],
                         {'targetId': 'example-service', 'requestId': 'repair-abc', 'expectedPolicyRevision': 4,
                          'grantRevision': 2})
        for url, headers, doc in self.calls:
            self.assertEqual(url, 'http://127.0.0.1:47999/mcp')
            self.assertEqual(headers['Authorization'], 'Bearer bearer-value')
            self.assertNotIn('bearer-value', json.dumps(doc))

    def test_refusals_are_codes(self):
        door = repair.LifecycleExecutor(lambda: 'b', port=1, transport=self.transport)
        self.answers = {'lifecycle.list': {'targets': [], 'grantRevision': 1}}
        with self.assertRaises(repair.ExecutorError) as caught:
            door.start({'action': 'ensure_running', 'targetId': 'example-app'}, 'repair-x')
        self.assertEqual(caught.exception.code, 'target-not-granted')
        self.answers = {'lifecycle.list': (403, json.dumps({'error': 'denied'}).encode())}
        with self.assertRaises(repair.ExecutorError) as caught:
            door.start({'action': 'restart', 'targetId': 'a'}, 'repair-x')
        self.assertEqual(caught.exception.code, 'denied')
        self.answers = {'lifecycle.operation.get': {'error': 'not_found'}}
        with self.assertRaises(repair.ExecutorError) as caught:
            door.poll('op')
        self.assertEqual(caught.exception.code, 'not_found')
        self.answers = {'lifecycle.operation.get': {'state': 'in_use_by_person'}}
        self.assertEqual(door.poll('op'), {'state': 'failed', 'code': 'in_use_by_person'})
        unreachable = repair.LifecycleExecutor(lambda: 'b', port=1, transport=lambda *a: (0, b''))
        with self.assertRaises(repair.ExecutorError) as caught:
            unreachable.poll('op')
        self.assertEqual(caught.exception.code, 'lifecycle-unreachable')


if HAVE_CRYPTO:
    from test_node_org import ORG, T0, FakeServer                    # noqa: E402

    class RepairServer(FakeServer):
        """The fake organization server, sending `records.repairs` and taking `records.repairResults`."""

        def __init__(self, clock, protocols=('fabric-device/0.1', 'fabric-activity/0.1', 'repair-result/1')):
            super().__init__(clock, protocols=protocols)
            self.repairs, self.results, self.refuse_results = None, [], 0

        def check_in(self, path, headers, body):
            doc = json.loads(body)
            carried = (doc.get('records') or {}).get('repairResults')
            if carried and self.refuse_results:
                self.refuse_results -= 1
                return 422, {'error': {'code': 'schema-invalid', 'pointer': '/records/repairResults'}}
            status, out = super().check_in(path, headers, body)
            if status == 200:
                self.results.extend(carried or [])
                if self.repairs is not None:
                    out['records'] = {'repairs': self.repairs}
            return status, out


@needs_crypto
class Enrolled(unittest.TestCase):
    """One enrollment against the fake organization server."""

    def setUp(self):
        from node.org import Enrollment
        self.now = [float(T0 + 3600)]
        self.clock = lambda: self.now[0]
        self.server = RepairServer(self.clock)
        self.node = Path(tmpdir.mkdtemp()).resolve() / 'node'
        self.private = signing_key('22' * 32)
        self.keys, self.layers = [], []
        self.config = (repair.empty_config(), None)
        self.executor = FakeExecutor()
        outer = self

        class Policy(Enrollment):                     # what OE-06 will apply, injected
            def policy_keys(self):
                return outer.keys

            def policy_layers(self):
                return outer.layers

        self.make = lambda: Policy(self.node, ORG, transport=self.server, clock=self.clock,
                                   harness=lambda: (None, None, 'no scans in this test'),
                                   exclusions=lambda: ([], None), repair_config=lambda: self.config,
                                   executor=lambda config: self.executor if config.get('lifecycle') else None)
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        self.device = self.make().keys.device_id

    def enable(self):
        self.keys = [{'keyId': KEY_ID, 'alg': 'ed25519', 'publicKey': public_b64(self.private)}]
        self.layers = [{'source': 'server', 'revision': 3, 'expiresAt': iso(T0 + 30 * 86400),
                        'keys': {'repair.packages': {'value': [{'agentRef': AGENT, 'digest': DIGEST}]}}}]
        self.config = ({'schemaVersion': 1, 'agents': {AGENT: {'action': 'restart', 'targetId': 'example-service'}},
                        'lifecycle': {'bearerSlot': 'node/prod/REPAIR_BINDING'}}, None)

    def request(self, rid='rep-1'):
        return sign({'type': 'repair-request/1', 'org': {'id': ORG}, 'id': rid, 'device': {'id': self.device},
                     'package': {'agentRef': AGENT, 'version': '1.2.0', 'digest': DIGEST},
                     'task': 'Restart the example service', 'issuedAt': iso(self.now[0] - 60),
                     'expiresAt': iso(self.now[0] + 86400)}, self.private)

    def check_ins(self):
        return [body for path, body in self.server.requests if path == '/v1/devices/check-in']

    def test_with_no_policy_applied_the_node_asks_for_none_and_reads_none(self):
        """MEDIUM 2: repairs a check-in did not ask for are dropped, counted in the journal, never recorded or run."""
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.check_ins()[-1]['protocols'], ['fabric-device/0.1'])
        self.assertEqual(self.make().repairs()['enabled']['reason'], 'policy-not-applied')
        self.server.repairs = [self.request(), self.request('rep-2')]      # a server that sends them anyway
        self.assertTrue(self.make().check_in()['ok'])
        self.server.repairs = None
        status = self.make().describe()
        self.assertEqual((status['repairs']['recent'], status['repairs']['pending']), ([], []))
        self.make().check_in()
        self.assertEqual(self.server.results, [])
        journal = (self.node / 'orgs' / ORG / 'journal.jsonl').read_text(encoding='utf-8')
        (line,) = [json.loads(l) for l in journal.splitlines() if '"repair-unasked"' in l]
        self.assertEqual(line['repairs'], 2)
        self.assertNotIn('"repair-received"', journal)

    def test_a_refusal_is_recorded_answered_once_and_again_for_a_redelivery(self):
        self.enable()
        self.make().check_in()
        bad = sign(dict(self.request(), device={'id': 'another-device'}), self.private)
        self.server.repairs = [bad]
        self.assertTrue(self.make().check_in()['ok'])
        self.server.repairs = None
        (recent,) = self.make().describe()['repairs']['recent']
        self.assertEqual((recent['outcome'], recent['reason']), ('refused', 'wrong-device'))
        self.make().check_in()
        (result,) = self.server.results
        self.assertEqual((result['id'], result['outcome'], result['reason']), ('rep-1', 'refused', 'wrong-device'))
        self.make().check_in()
        self.assertEqual(len(self.server.results), 1)            # taken once
        self.server.repairs = [bad]                              # redelivered: the same stored result again
        self.make().check_in()
        self.server.repairs = None
        self.make().check_in()
        self.assertEqual(self.server.results, [result, result])
        journal = (self.node / 'orgs' / ORG / 'journal.jsonl').read_text(encoding='utf-8')
        self.assertIn('"repair-answered"', journal)
        self.assertNotIn(bad['signature']['value'], journal)

    def test_a_check_in_keeps_what_its_response_said_whatever_the_repairs_do(self):
        """MEDIUM 1: a repair that cannot be read never loses the check-in's own state."""
        self.enable()
        self.make().check_in()
        original = repair.receive

        def explode(*args, **kwargs):
            raise RecursionError('maximum recursion depth exceeded')
        repair.receive = explode
        self.addCleanup(setattr, repair, 'receive', original)
        self.server.repairs = [self.request()]
        self.server.checkin_protocols = ['fabric-device/0.1', 'repair-result/1', 'node-summary/1']
        self.now[0] += 10
        self.assertTrue(self.make().check_in()['ok'])
        state = self.make().state()
        self.assertEqual(state['nextCheckInAt'], int(self.now[0]) + 300)
        self.assertIn('node-summary/1', state['protocols'])
        self.assertEqual(state['lastCheckIn']['result'], 'ok')
        journal = (self.node / 'orgs' / ORG / 'journal.jsonl').read_text(encoding='utf-8')
        self.assertIn('"repair-receive-failed"', journal)

    def test_results_go_only_to_a_server_that_lists_repair_result(self):
        self.enable()
        self.server.protocols = ['fabric-device/0.1', 'fabric-activity/0.1']
        self.make().enroll('https://org.example', 'et_2.' + 'T' * 32)
        self.device = self.make().keys.device_id
        self.make().check_in()
        self.server.repairs = [sign(dict(self.request(), device={'id': 'another-device'}), self.private)]
        self.make().check_in()
        self.server.repairs = None
        self.make().check_in()
        self.assertEqual(self.server.results, [])
        for body in self.check_ins():
            self.assertNotIn('repairResults', body.get('records') or {})
        self.assertEqual(self.make().describe()['repairs']['unsent'], 1)

    def test_enabled_the_whole_run_and_the_person_first(self):
        self.enable()
        self.make().check_in()
        self.assertEqual(self.check_ins()[-1]['protocols'], ['fabric-device/0.1', 'repair-request/1'])
        self.server.repairs = [self.request()]
        self.make().check_in()
        self.server.repairs = None
        from node import org
        (said,) = org.notices(self.node, now=self.clock())        # doctor and Health read this
        (pending,) = said['repairs']['pending']
        self.assertEqual((pending['task'], pending['agentRef'], pending['state']),
                         ('Restart the example service', AGENT, 'notified'))
        self.assertEqual(pending['declineCommand'],
                         'project-observatory full org repair --org %s --decline rep-1' % ORG)
        self.make().tick()
        self.assertEqual(self.executor.started, [])               # the notice stands first
        self.now[0] += repair.NOTICE_SECONDS
        self.make().tick()
        self.assertEqual(len(self.executor.started), 1)
        self.now[0] += 300
        self.make().tick()                                        # the run ends and the result rides the next check-in
        self.now[0] += 300
        self.make().tick()
        (result,) = self.server.results
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertLess(strict_time(result['notifiedAt']), strict_time(result['startedAt']))
        self.assertEqual(self.make().describe()['repairs']['pending'], [])

    def test_a_person_declines(self):
        self.enable()
        self.make().check_in()
        self.server.repairs = [self.request()]
        self.make().check_in()
        self.server.repairs = None
        answer = self.make().decline_repair('rep-1')
        self.assertEqual(answer['recent'][0]['reason'], 'person-declined')
        self.now[0] += repair.NOTICE_SECONDS
        self.make().tick()
        self.assertEqual(self.executor.started, [])
        from node.org import OrgError
        with self.assertRaises(OrgError) as caught:
            self.make().decline_repair('rep-1')
        self.assertEqual(caught.exception.code, 'repair-not-pending')
        with self.assertRaises(OrgError) as caught:
            self.make().decline_repair('rep-none')
        self.assertEqual(caught.exception.code, 'no-such-repair')

    def test_a_server_that_refuses_the_results_never_stops_check_ins(self):
        self.enable()
        self.make().check_in()
        self.server.repairs = [sign(dict(self.request(), device={'id': 'another-device'}), self.private)]
        self.make().check_in()
        self.server.repairs = None
        self.server.refuse_results = 5
        self.assertTrue(self.make().check_in()['ok'])             # sent again at once without them
        status = self.make().describe()
        self.assertIn('org:repair-results-refused', status['degraded'])
        self.assertEqual(status['repairs']['refused']['code'], 'schema-invalid')
        self.make().check_in()
        self.assertEqual(self.server.results, [])                 # held a day
        self.server.refuse_results = 0
        self.now[0] += 86400 + 1
        self.make().check_in()
        self.assertEqual(len(self.server.results), 1)
        self.assertNotIn('org:repair-results-refused', self.make().describe()['degraded'])

    def test_a_removed_machine_runs_nothing_more(self):
        self.enable()
        self.make().check_in()
        self.server.repairs = [self.request()]
        self.make().check_in()
        self.server.repairs = None
        self.server.refuse = (401, 'certificate-revoked')
        self.make().check_in()
        self.now[0] += repair.NOTICE_SECONDS
        self.assertEqual(self.make().run_repairs(), {'result': 'removed'})
        self.assertEqual(self.executor.started, [])
        self.assertNotIn('repairs', self.make().describe())        # nothing shown for an organization that left

    def test_the_cli_names_the_commands(self):
        from tools import org as org_tool
        args = org_tool.parser().parse_args(['repair', '--org', ORG, '--decline', 'rep-1'])
        self.assertEqual((args.command, args.org, args.decline), ('repair', ORG, 'rep-1'))
        self.assertIn('org repair --org ID --decline REPAIR_ID', org_tool.__doc__)


@needs_crypto
class OrgCloud(unittest.TestCase):
    """MEDIUM 3: `org-cloud` skips the person's notice, so it counts only when this machine enrolled with it and binds
    no person; a rotation or renewal that adds, drops or changes a role is refused, and a person-bound `org-cloud`
    certificate is refused whole."""

    def setUp(self):
        from node.org import Enrollment
        self.now = [float(T0 + 3600)]
        self.clock = lambda: self.now[0]
        self.server = RepairServer(self.clock)
        self.node = Path(tmpdir.mkdtemp()).resolve() / 'node'
        self.make = lambda: Enrollment(self.node, ORG, transport=self.server, clock=self.clock,
                                       harness=lambda: (None, None, 'no scans in this test'),
                                       exclusions=lambda: ([], None))

    def role(self):
        enrollment = self.make()
        return enrollment._repair_context(enrollment.state()).role

    def test_enrolled_as_org_cloud_it_counts(self):
        self.server.role = 'org-cloud'
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        self.assertEqual(self.make().state()['enrolledRole'], 'org-cloud')
        self.assertEqual(self.role(), 'org-cloud')

    def test_a_state_without_the_enrolled_role_is_a_persons_machine(self):
        self.server.role = 'org-cloud'
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        enrollment = self.make()
        with enrollment._locked():
            state = enrollment.state()
            state.pop('enrolledRole')
            enrollment._save(state)
        self.assertIsNone(self.role())
        from node.org import OrgError
        self.server.repairs = None
        with self.assertRaises(OrgError) as caught:
            self.make().decline_repair('rep-1')
        self.assertEqual(caught.exception.code, 'no-such-repair')            # a person may decline here

    def test_a_person_bound_org_cloud_certificate_is_refused(self):
        from node.org import OrgError
        self.server.role, self.server.user = 'org-cloud', 'member-1'
        with self.assertRaises(OrgError) as caught:
            self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        self.assertEqual(caught.exception.code, 'response-malformed')

    def test_a_rotation_that_adds_a_role_is_refused_and_the_certificate_kept(self):
        from node.org import OrgError
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        serial = self.make().state()['certificate']['serial']
        self.server.role = 'org-cloud'
        with self.assertRaises(OrgError) as caught:
            self.make().rotate()
        self.assertEqual(caught.exception.code, 'binding-changed')
        state = self.make().state()
        self.assertEqual(state['certificate']['serial'], serial)
        self.assertIsNone(state['certificate']['role'])
        self.assertIsNone(self.role())

    def test_a_renewal_or_rotation_may_not_add_drop_or_change_a_role(self):
        from node.org import _same_binding
        self.assertFalse(_same_binding({'user': False, 'role': None}, {'user': False, 'role': 'org-cloud'}))
        self.assertFalse(_same_binding({'user': False, 'role': 'org-cloud'}, {'user': False}))
        self.assertFalse(_same_binding({'user': False, 'role': 'org-cloud'}, {'user': False, 'role': None}))
        self.assertFalse(_same_binding({'user': False, 'role': 'org-cloud'}, {'user': False, 'role': 'other'}))
        self.assertTrue(_same_binding({'user': False, 'role': 'org-cloud'}, {'user': False, 'role': 'org-cloud'}))
        self.assertTrue(_same_binding({'user': False, 'role': None}, {'user': False, 'role': None}))
        # A record saved before 0.29.0 never stored the role: its renewal or rotation keeps whatever role the server
        # names (and `enrolledRole`, absent there too, keeps the notice).
        self.assertTrue(_same_binding({'user': False}, {'user': False, 'role': 'org-cloud'}))
        self.assertTrue(_same_binding({'user': False}, {'user': False, 'role': None}))

    def test_a_certificate_without_a_role_records_none(self):
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        self.assertIsNone(self.make().state()['certificate']['role'])

    def legacy(self):
        """An org-cloud enrollment as 0.27.0 and 0.28.0 saved it: no role on the certificate, no `enrolledRole`."""
        self.server.role = 'org-cloud'
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        enrollment = self.make()
        with enrollment._locked():
            state = enrollment.state()
            state['certificate'].pop('role')
            state.pop('enrolledRole')
            enrollment._save(state)

    def test_an_old_record_without_a_role_rotates_and_never_skips_the_notice(self):
        self.legacy()
        serial = self.make().state()['certificate']['serial']
        self.make().rotate()
        state = self.make().state()
        self.assertNotEqual(state['certificate']['serial'], serial)
        self.assertEqual(state['certificate']['role'], 'org-cloud')
        self.assertIsNone(self.role())

    def test_an_old_record_without_a_role_renews_and_never_skips_the_notice(self):
        self.server.renewal = True
        self.legacy()
        not_after = strict_time(self.make().state()['certificate']['notAfter']) / 1000
        self.now[0] = not_after + 5 * 86400
        result = self.make().check_in()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['renewed']['by'], 'key-on-record')
        self.assertEqual(self.make().state()['certificate']['role'], 'org-cloud')
        self.assertIsNone(self.role())


class HealthAndDocs(unittest.TestCase):
    """Both readers see it: Health renders the pending repair with its decline command, in both languages."""

    def test_health_renders_repairs(self):
        source = (ROOT / 'dashboard' / 'build_dashboard.py').read_text(encoding='utf-8')
        self.assertIn('O.repairs', source)
        self.assertIn('P.declineCommand', source)
        ru = json.loads((ROOT / 'dashboard' / 'locales' / 'ru.json').read_text(encoding='utf-8'))
        for message in ('the organization asks to run a repair of {agent}: {task}; it starts after {time} unless you '
                        'decline', 'a repair of {agent} is running: {task}',
                        'repair {outcome} on {date} ({reason}): {task}'):
            self.assertIn(message, source)
            self.assertIn(message, ru)


if __name__ == '__main__':
    unittest.main()
