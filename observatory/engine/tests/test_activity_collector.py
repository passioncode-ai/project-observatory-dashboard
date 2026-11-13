#!/usr/bin/env python3
"""The device's activity collector (OBS-76 OE-01a): synthetic transcripts of both runtimes, the contract's telemetry
schema and event id, a personal session never enqueued, a line written in two halves read once, a rotated file read
from the start without counting a call twice."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import activity_collector as ac  # noqa: E402

VECTORS = ROOT / 'fabric' / 'node-vectors'
T0 = 1_800_000_000.0          # 2027-01-15, any fixed moment
KEY = bytes(range(32))


def iso(seconds: float) -> str:
    return ac._iso(seconds).replace('Z', '.123Z')


def claude(at, *, kind='user', request=None, output=0, session='sess-1', cwd='/work/demo', side=False):
    doc = {'type': kind, 'timestamp': iso(at), 'sessionId': session, 'cwd': cwd, 'version': '2.1.300',
           'gitBranch': 'main', 'isSidechain': side, 'uuid': 'u-%d' % at,
           'message': {'role': kind, 'content': 'a secret the collector must never keep'}}
    if request:
        doc['requestId'] = request
        doc['message'].update(model='claude-opus-5-5', usage={'input_tokens': 10, 'output_tokens': output,
                                                             'cache_read_input_tokens': 100,
                                                             'cache_creation_input_tokens': 5})
    return json.dumps(doc) + '\n'


class Emit:
    """The enrollment's `enqueue` as far as the collector sees it: a key or a derived id is stored once."""

    def __init__(self):
        self.events, self.keys = [], set()

    def __call__(self, kind, data, *, occurred, key=None, source='meter', envelope=None, event_id=None):
        if key in self.keys or (event_id and event_id in self.keys):
            return None
        self.keys.update(k for k in (key, event_id) if k)
        event = {**(envelope or {}), 'protocol': 'fabric-activity/0.1', 'eventId': event_id or '01J000000000000000000000A0',
                 'kind': kind, 'source': source, 'collector': {'id': 'observatory-node', 'epoch': 1},
                 'seq': len(self.events) + 1, 'occurred': occurred, 'bootId': 'boot-test', 'monotonicNs': '0',
                 'data': data}
        self.events.append(event)
        return event

    def of(self, kind):
        return [e for e in self.events if e['kind'] == kind]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='activity-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.claude_root = self.tmp / 'claude' / 'projects'
        self.codex_root = self.tmp / 'codex' / 'sessions'
        self.state = self.tmp / 'state'
        self.emit = Emit()
        self.context = ('org:acme', 'project:demo')

    def homes(self):
        return [{'agent': 'claude', 'trees': [(self.claude_root, ('.jsonl',))]},
                {'agent': 'codex', 'trees': [(self.codex_root, ('.jsonl', '.jsonl.zst'))]}]

    def write(self, path: Path, text: str, mode='a'):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding='utf-8') as stream:
            stream.write(text)
        os.utime(path, (T0, T0))

    def run_pass(self, now, start=T0 - 3600):
        run = ac.Pass('acme', self.state, self.emit, KEY, enrolled=['acme'], start=start,
                      resolve=lambda folder: self.context, now=now)
        return run.run(ac.transcript_files(self.homes()))


class Identifiers(unittest.TestCase):
    def test_event_id_is_the_contracts(self):
        # fabric-agent-contract fixtures/positive/telemetry-event-usage.json at the engine's pin
        self.assertEqual(ac.event_id('claude_code.otel', 'req_011CTexampleRequest0001'),
                         'sha256:143ffca5ff867511f008058ab8dd756d46acecede30385b604841ad2e5a4959e')

    def test_path_key_is_keyed_and_never_a_path(self):
        key = ac.path_source_key(KEY, '-Users-someone-work/sess.jsonl', 42)
        self.assertRegex(key, r'^hmac-sha256:[0-9a-f]{64}$')
        self.assertNotEqual(key, ac.path_source_key(bytes(32), '-Users-someone-work/sess.jsonl', 42))


class Claude(Base):
    def setUp(self):
        super().setUp()
        self.file = self.claude_root / '-work-demo' / 'sess-1.jsonl'
        self.write(self.file, ''.join([
            claude(T0),
            claude(T0 + 10, kind='assistant', request='req_A', output=3),
            claude(T0 + 11, kind='assistant', request='req_A', output=40),
            claude(T0 + 60),
            claude(T0 + 70, kind='assistant', request='req_B', output=7),
            claude(T0 + 1000),
        ]))

    def test_one_usage_line_per_call_with_its_largest_counts(self):
        self.run_pass(now=T0 + 5000)
        usage = self.emit.of('usage.line')
        self.assertEqual([e['data']['dedupeKey'] for e in usage], ['req_A', 'req_B'])
        self.assertEqual(usage[0]['data']['tokens'], {'input': 10, 'output': 40, 'cacheRead': 100, 'cacheWrite': 5})
        self.assertEqual(usage[0]['eventId'], ac.event_id('claude_code.transcript', 'req_A'))
        self.assertEqual(usage[0]['sourceKey'], 'req_A')
        self.assertEqual(usage[0]['data']['cost'], {'usd': None})
        self.assertEqual(usage[0]['session'], {'id': 'sess-1'})
        self.assertEqual(usage[0]['project'], {'id': 'project:demo'})
        self.assertEqual(usage[0]['runtime'], {'name': 'claude_code', 'version': '2.1.300'})
        self.assertRegex(usage[0]['git']['branch'], r'^hmac-sha256:')

    def test_intervals_split_at_the_idle_threshold(self):
        self.run_pass(now=T0 + 5000)
        intervals = [(e['data']['state'], e['data']['start'], e['data']['end']) for e in self.emit.of('session.interval')]
        self.assertEqual(intervals, [('agent_working', ac._iso(T0), ac._iso(T0 + 70)),
                                     ('idle', ac._iso(T0 + 70), ac._iso(T0 + 1000))])
        for event in self.emit.of('session.interval'):
            self.assertEqual(event['data']['method'], 'transcript')
            self.assertNotIn('sourceKey', event)

    def test_no_content_reaches_an_event(self):
        self.run_pass(now=T0 + 5000)
        text = json.dumps(self.emit.events)
        for word in ('secret', 'content', 'cwd', '/work/demo', '"main"'):
            self.assertNotIn(word, text)

    def test_events_pass_the_contracts_schema(self):
        try:
            import jsonschema
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest('jsonschema is in the [full] extra; the base install cannot run this check')
        registry = Registry()
        for name in ('summary/common.schema.json', 'summary/activity-common.schema.json',
                     'activity/telemetry-event.schema.json'):
            doc = json.loads((VECTORS / name).read_text(encoding='utf-8'))
            registry = registry.with_resource(doc['$id'], Resource.from_contents(doc))
        schema = json.loads((VECTORS / 'activity' / 'telemetry-event.schema.json').read_text(encoding='utf-8'))
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
        self.run_pass(now=T0 + 5000)
        self.assertTrue(self.emit.events)
        for event in self.emit.events:
            self.assertEqual([e.message for e in validator.iter_errors(event)], [], event['kind'])

    def test_a_second_pass_adds_nothing(self):
        self.run_pass(now=T0 + 5000)
        count = len(self.emit.events)
        receipt = self.run_pass(now=T0 + 6000)
        self.assertEqual(len(self.emit.events), count)
        self.assertEqual(receipt['readBytes'], 0)

    def test_a_personal_session_is_counted_and_never_enqueued(self):
        self.context = ('personal', None)
        receipt = self.run_pass(now=T0 + 5000)
        self.assertEqual(self.emit.events, [])
        self.assertGreater(receipt['keptPersonal'], 0)

    def test_another_organizations_session_is_left_to_it(self):
        self.context = ('org:other', 'project:theirs')
        receipt = self.run_pass(now=T0 + 5000)
        self.assertEqual(self.emit.events, [])
        self.assertGreater(receipt['otherContext'], 0)

    def test_nothing_before_the_collection_start(self):
        self.run_pass(now=T0 + 5000, start=T0 + 500)
        self.assertEqual(self.emit.of('usage.line'), [])
        self.assertEqual([e['data']['start'] for e in self.emit.of('session.interval')], [])

    def test_a_subagents_file_gives_usage_and_no_interval(self):
        side = self.claude_root / '-work-demo' / 'sess-1' / 'subagents' / 'agent-1.jsonl'
        self.write(side, claude(T0 + 20, kind='assistant', request='req_S', output=2, side=True) + claude(T0 + 30))
        self.run_pass(now=T0 + 5000)
        self.assertIn('req_S', [e['data']['dedupeKey'] for e in self.emit.of('usage.line')])
        self.assertEqual(len(self.emit.of('session.interval')), 2)


class Halves(Base):
    def test_a_line_written_in_two_halves_is_read_once(self):
        file = self.claude_root / '-work-demo' / 'sess-1.jsonl'
        line = claude(T0 + 10, kind='assistant', request='req_A', output=4)
        self.write(file, claude(T0) + line[:40])
        self.run_pass(now=T0 + 20)
        self.assertEqual(self.emit.of('usage.line'), [])
        self.write(file, line[40:] + claude(T0 + 30))
        self.run_pass(now=T0 + 5000)
        usage = self.emit.of('usage.line')
        self.assertEqual([e['data']['dedupeKey'] for e in usage], ['req_A'])
        self.assertEqual(usage[0]['data']['tokens']['output'], 4)


class Rotation(Base):
    def test_a_replaced_file_is_read_from_the_start_and_counts_a_call_once(self):
        file = self.claude_root / '-work-demo' / 'sess-1.jsonl'
        self.write(file, claude(T0 + 10, kind='assistant', request='req_A', output=4) + claude(T0 + 20))
        self.run_pass(now=T0 + 5000)
        replaced = file.with_suffix('.new')
        self.write(replaced, claude(T0 + 10, kind='assistant', request='req_A', output=4) + claude(T0 + 20)
                   + claude(T0 + 30, kind='assistant', request='req_B', output=1) + claude(T0 + 40))
        os.replace(replaced, file)
        self.run_pass(now=T0 + 9000)
        self.assertEqual([e['data']['dedupeKey'] for e in self.emit.of('usage.line')], ['req_A', 'req_B'])


class Codex(Base):
    def test_a_rollout_gives_usage_lines_and_intervals(self):
        file = self.codex_root / '2027' / '01' / '15' / 'rollout-1.jsonl'
        lines = [
            {'type': 'session_meta', 'timestamp': iso(T0), 'payload': {'id': 'thread-1', 'cwd': '/work/demo',
                                                                       'cli_version': '0.164.0', 'git': {'branch': 'main'},
                                                                       'base_instructions': 'never kept'}},
            {'type': 'turn_context', 'timestamp': iso(T0 + 1), 'payload': {'cwd': '/work/demo', 'model': 'gpt-6'}},
            {'type': 'token_usage_record', 'timestamp': iso(T0 + 30),
             'payload': {'response_id': 'resp_123', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 800,
                                                              'cache_write_input_tokens': 0, 'output_tokens': 50,
                                                              'reasoning_output_tokens': 20}}},
            {'type': 'response_item', 'timestamp': iso(T0 + 40), 'payload': {'type': 'message', 'content': 'hidden'}},
        ]
        self.write(file, ''.join(json.dumps(line) + '\n' for line in lines))
        self.run_pass(now=T0 + 5000)
        usage = self.emit.of('usage.line')
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]['source'], 'codex.rollout')
        self.assertEqual(usage[0]['data']['model'], 'gpt-6')
        self.assertEqual(usage[0]['data']['tokens'], {'input': 200, 'output': 50, 'cacheRead': 800, 'cacheWrite': 0})
        self.assertEqual(usage[0]['session'], {'id': 'thread-1'})
        self.assertEqual(usage[0]['runtime'], {'name': 'codex', 'version': '0.164.0'})
        self.assertEqual([e['data']['state'] for e in self.emit.of('session.interval')], ['agent_working'])
        self.assertNotIn('hidden', json.dumps(self.emit.events))


if __name__ == '__main__':
    unittest.main()
