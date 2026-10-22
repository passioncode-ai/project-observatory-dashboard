#!/usr/bin/env python3
"""The node's harness summary, `node-summary/1`, and its drift (contract DEC-0101, DEC-0102; engine OBS-192).

The port runs over the contract's own vectors (`fabric/node-vectors/summary/`, vendored at the engine's contract pin):
FAC-SEM-128 answers every semantic fixture as the contract does, `harness_drift`, `version_below` and
`harness_baseline` answer the contract kit's own cases, and the schema admits the positive fixture and refuses every
negative one. Then the builder is held to the schema and to what never leaves (a path, a command, an argument, an
environment value, a hook's command), and an enrollment sends a summary in its check-in only to a server that lists
`node-summary/1`, when it changed and at least once a day.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from node import node_summary as ns                    # noqa: E402

SUMMARY = ROOT / 'fabric' / 'node-vectors' / 'summary'
DIGEST_A = 'sha256:' + 'a1' * 32


def load(name: str):
    return json.loads((SUMMARY / name).read_text(encoding='utf-8'))


def codes(value) -> list:
    return list(dict.fromkeys(f['code'] for f in ns.summary_findings(value)))


def validator():
    """The vendored schema with the two it refers to, or None without `jsonschema` (the engine's `full` extra)."""
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return None
    schemas = {name: load(name) for name in ('node-summary.schema.json', 'common.schema.json',
                                             'activity-common.schema.json')}
    registry = Registry().with_resources((doc['$id'], Resource.from_contents(doc)) for doc in schemas.values())
    return jsonschema.Draft202012Validator(schemas['node-summary.schema.json'], registry=registry,
                                           format_checker=jsonschema.FormatChecker())


class Vectors(unittest.TestCase):
    """FAC-SEM-128 and the schema over the contract's fixtures."""

    def test_every_semantic_fixture_answers_as_the_contract(self):
        files = sorted((SUMMARY / 'semantic').glob('node-summary-*.json'))
        self.assertEqual(len(files), 6)
        self.assertEqual(sum(1 for f in files if json.loads(f.read_text())['expect']), 4)
        for file in files:
            fixture = json.loads(file.read_text(encoding='utf-8'))
            with self.subTest(file.name):
                self.assertEqual(fixture['kind'], 'node-summary')
                self.assertEqual(codes(fixture['input']), fixture['expect'])

    def test_the_positive_document_has_no_finding_and_the_schema_admits_it(self):
        positive = load('positive/node-summary.json')
        self.assertEqual(ns.document_findings(positive), [])
        check = validator()
        if check is None:
            self.skipTest('needs jsonschema')
        self.assertEqual(list(check.iter_errors(positive)), [])

    def test_the_schema_refuses_every_negative_fixture(self):
        check = validator()
        if check is None:
            self.skipTest('needs jsonschema')
        files = sorted((SUMMARY / 'negative').glob('node-summary-*.json'))
        self.assertEqual(len(files), 7)
        for file in files:
            with self.subTest(file.name):
                self.assertTrue(list(check.iter_errors(json.loads(file.read_text(encoding='utf-8')))))
        # The contract's eighth, `node-summary-path-in-name.json`, rebuilt from the positive: it names a home folder,
        # which the public-release gate refuses in a file, so it is not vendored.
        path_in_name = load('positive/node-summary.json')
        path_in_name['harness']['mcpServers'][0]['name'] = '/' + '/'.join(('Users', 'example', 'servers', 'x'))
        self.assertTrue(list(check.iter_errors(path_in_name)))


class Drift(unittest.TestCase):
    """`harnessDrift`, `versionBelow` and `harnessBaseline`, the contract kit's own cases (test/node-summary.test.ts)."""

    def setUp(self):
        fixture = load('semantic/node-summary-ok.json')['input']
        self.summary, self.baseline = fixture['summary'], fixture['baseline']

    def test_each_item_missing_stopped_changed_or_outdated_in_the_baselines_order(self):
        self.assertEqual(ns.harness_drift(self.baseline['items'], self.summary), [
            {'kind': 'plugin', 'name': 'switchboard@passioncode', 'state': 'outdated'},
            {'kind': 'service', 'name': 'inbox-agent.default', 'state': 'missing'}])

    def test_one_state_per_item_the_first_that_holds(self):
        items = [{'kind': 'service', 'name': 'observatory-node.default'},
                 {'kind': 'mcp-server', 'name': 'fabric', 'minVersion': '1'},
                 {'kind': 'skill', 'name': 'task-pipeline', 'digest': 'sha256:' + 'b' * 64},
                 {'kind': 'coding-agent', 'name': 'codex', 'minVersion': '0.48.1'},
                 {'kind': 'coding-agent', 'name': 'claude-code', 'minVersion': '2.1.296'},
                 {'kind': 'widget', 'name': 'x'}, {'kind': 'skill'}]
        stopped = copy.deepcopy(self.summary)
        stopped['services'] = [{'key': 'observatory-node.default', 'state': 'stopped'}]
        stopped['harness']['mcpServers'] = [dict(self.summary['harness']['mcpServers'][0], health='down')]
        self.assertEqual(ns.harness_drift(items, stopped), [
            {'kind': 'service', 'name': 'observatory-node.default', 'state': 'stopped'},
            {'kind': 'mcp-server', 'name': 'fabric', 'state': 'stopped'},
            {'kind': 'skill', 'name': 'task-pipeline', 'state': 'changed'},
            {'kind': 'coding-agent', 'name': 'codex', 'state': 'outdated'}])
        self.assertEqual(ns.harness_drift('not a list', self.summary), [])
        self.assertEqual([i['state'] for i in ns.harness_drift(items, None)], ['missing'] * 5)

    def test_only_what_a_kind_carries_is_compared_and_changed_comes_before_outdated(self):
        items = [{'kind': 'mcp-server', 'name': 'fabric', 'minVersion': '9', 'digest': 'sha256:' + 'c' * 64},
                 {'kind': 'service', 'name': 'observatory-node.default', 'minVersion': '9'},
                 {'kind': 'coding-agent', 'name': 'claude-code', 'digest': 'sha256:' + 'c' * 64},
                 {'kind': 'plugin', 'name': 'switchboard@passioncode', 'minVersion': '9',
                  'digest': 'sha256:' + 'c' * 64}]
        self.assertEqual(ns.harness_drift(items, self.summary),
                         [{'kind': 'plugin', 'name': 'switchboard@passioncode', 'state': 'changed'}])
        unknown = dict(self.summary, services=[{'key': 'observatory-node.default', 'state': 'unknown'}])
        self.assertEqual(ns.harness_drift([{'kind': 'service', 'name': 'observatory-node.default'}], unknown),
                         [{'kind': 'service', 'name': 'observatory-node.default', 'state': 'stopped'}])

    def test_versions_are_compared_number_by_number(self):
        self.assertFalse(ns.version_below('2.10.0', '2.9'))
        self.assertTrue(ns.version_below('2.9', '2.10'))
        self.assertFalse(ns.version_below('2', '2.0.0'))
        self.assertTrue(ns.version_below('2.0.1-beta', '1'))
        self.assertTrue(ns.version_below(None, '1'))
        self.assertFalse(ns.version_below('1', '1.x'), 'a part that is no number is below nothing, as in JavaScript')

    def test_the_baseline_is_read_from_signed_layers_only(self):
        items = [{'kind': 'plugin', 'name': 'x'}]
        server = {'source': 'server', 'revision': 7,
                  'keys': {'harness.baseline': {'value': {'items': items}}, 'repair.packages': {'value': []}}}
        user = {'source': 'user', 'revision': 1, 'keys': {'harness.baseline': {'value': {'items': []}},
                                                         'repair.packages': {'value': [{'agentRef': 'a', 'digest': 'b'}]}}}
        self.assertEqual(ns.harness_baseline([server, user]), {'revision': 7, 'items': items})
        self.assertIsNone(ns.harness_baseline([user]))
        self.assertIsNone(ns.harness_baseline('no'))
        mdm_items = [{'kind': 'skill', 'name': 'y'}]
        mdm = {'source': 'mdm', 'revision': 3, 'keys': {'harness.baseline': {'value': {'items': mdm_items}}}}
        self.assertEqual(ns.harness_baseline([mdm, server]), {'revision': 3, 'items': mdm_items})
        locked = dict(server, keys={'harness.baseline': {'value': {'items': items}, 'locked': True}})
        self.assertEqual(ns.harness_baseline([mdm, locked]), {'revision': 7, 'items': items})
        mdm_other = {'source': 'mdm', 'revision': 3, 'keys': {'repair.packages': {'value': []}}}
        self.assertEqual(ns.harness_baseline([mdm_other, server]), {'revision': 7, 'items': items})
        self.assertEqual(ns.signed_policy_value([server, user], 'repair.packages'), [])
        resolved = ns.resolve_policy([server, dict(user, keys=dict(user['keys'], **{'x-example.banner':
                                                                                    {'value': 'mine'}}))])
        self.assertNotIn('harness.baseline', {k for k, v in resolved.items() if v['from'] == 'user'})
        self.assertEqual(resolved['x-example.banner'], {'value': 'mine', 'from': 'user'})

    def test_the_contracts_policy_with_a_baseline(self):
        policy = load('device-policy-harness-baseline.json')
        baseline = ns.harness_baseline([policy])
        self.assertEqual(baseline['revision'], 12)
        self.assertEqual([i['kind'] for i in baseline['items']],
                         ['coding-agent', 'mcp-server', 'skill', 'plugin', 'service'])


class Findings(unittest.TestCase):
    """FAC-SEM-128 beyond the fixtures."""

    def setUp(self):
        fixture = load('semantic/node-summary-ok.json')['input']
        self.summary, self.baseline = fixture['summary'], fixture['baseline']

    def paths(self, value):
        return [f['instancePath'] for f in ns.document_findings(value)]

    def test_a_credential_a_path_and_drift_against_the_wrong_baseline(self):
        key = '_'.join(('ghp', '0123456789abcdefghij' + 'ABCDEFGHIJ'))     # never one literal: the release gate
        with_secret = copy.deepcopy(self.summary)
        with_secret['harness']['plugins'] = [{'name': key, 'source': 'local'}]
        self.assertIn('FAC-SEM-128', codes({'summary': with_secret, 'baseline': self.baseline}))
        with_path = copy.deepcopy(self.summary)
        with_path['services'][0]['mcpEndpoint'] = 'https://host.example/' + 'Users/example/x'
        self.assertEqual([f['instancePath'] for f in ns.summary_findings({'summary': with_path,
                                                                          'baseline': self.baseline})],
                         ['/summary/services/0/mcpEndpoint'])
        self.assertEqual(codes({'summary': self.summary}), ['FAC-SEM-128'])
        other = dict(self.summary, drift=dict(self.summary['drift'], policyRevision=6))
        self.assertEqual(codes({'summary': other, 'baseline': self.baseline}), ['FAC-SEM-128'])
        undrifted = {k: v for k, v in self.summary.items() if k != 'drift'}
        self.assertEqual(codes({'summary': undrifted, 'baseline': self.baseline}), ['FAC-SEM-128'])
        self.assertEqual(codes({'summary': 'no', 'baseline': 'no'}), [])

    def test_the_context_is_compared_as_written(self):
        self.assertEqual(codes({'summary': dict(self.summary, context='org:ORG-7F3A'), 'baseline': self.baseline,
                                'org': 'org-7f3a'}), ['FAC-SEM-128'])
        contextless = {k: v for k, v in self.summary.items() if k != 'context'}
        self.assertEqual(codes({'summary': contextless, 'baseline': self.baseline, 'org': 'org-7f3a'}),
                         ['FAC-SEM-128'])

    def test_duplicates_in_each_list_and_pointers_that_escape_member_names(self):
        for member, field in (('skills', 'name'), ('mcpServers', 'name'), ('codingAgents', 'id')):
            doubled = copy.deepcopy(self.summary)
            doubled['harness'][member] = [doubled['harness'][member][0]] * 2
            self.assertEqual(self.paths(doubled), ['/harness/%s/1/%s' % (member, field)])
        services = dict(self.summary, services=[self.summary['services'][0]] * 2)
        self.assertEqual(self.paths(services), ['/services/1/key'])
        self.assertEqual(self.paths(dict(self.summary, **{'a/b~c': 'someone@example.com'})), ['/a~1b~0c'])

    def test_depth_never_raises(self):
        deep = 'x'
        for _ in range(20000):
            deep = [deep]
        harness = dict(self.summary['harness'], hooks=deep)
        self.assertEqual(ns.document_findings(dict(self.summary, harness=harness)), [])


def scans():
    """A harness scan and an MCP scan as the collectors write them, with everything that must never leave planted in
    fields the summary does not copy."""
    harness = {
        'skills': [{'name': 'task-pipeline', 'source': 'plugin:task-pipeline@family', 'digest': 'a1' * 32,
                    'key': 'plugin:task-pipeline@family/task-pipeline', 'files': 3},
                   {'name': 'tidy-notes', 'source': 'plain', 'digest': 'b2' * 32, 'key': 'plain/tidy-notes'},
                   {'name': 'task-pipeline', 'source': 'plain', 'digest': 'c3' * 32},
                   {'name': 'has space', 'source': 'plain', 'digest': 'd4' * 32}],
        'plugins': [{'name': 'switchboard@passioncode', 'source': 'marketplace', 'version': '0.6.61',
                     'installed': True},
                    {'name': 'odd@market', 'source': 'somewhere', 'version': 'not a version'}],
        'coding_agents': [{'id': 'claude-code', 'version': '2.1.296'}, {'id': 'codex'}],
        'hooks': [{'event': 'PreToolUse', 'program': 'python3', 'command_sha': 'abc'}] * 3,
        'instructions': [{'path': '~/.claude/CLAUDE.md', 'lines': 3}],
        'tool_plaintext': [{'file': '~/.codex/config.toml', 'name': 'OPENROUTER_API_KEY'}],
    }
    mcp = {'servers': [
        {'name': 'fabric', 'agent': 'claude', 'scope': 'user', 'wire': 'stdio', 'liveness': 'connected',
         'declared_in': '~/.claude.json', 'command': 'deno-launch', 'target': None, 'args_count': 2},
        {'name': 'fabric', 'agent': 'codex', 'scope': 'user', 'wire': 'stdio', 'liveness': 'failed',
         'declared_in': '~/.codex/config.toml'},
        {'name': 'plugin:switchboard:switchboard', 'agent': 'claude', 'scope': 'plugin:switchboard',
         'wire': 'streamable-http', 'liveness': 'needs-auth', 'target': 'https://h.example/mcp'},
        {'name': 'alpha-local', 'agent': 'claude', 'scope': 'project:~/projects/alpha-web', 'wire': 'sse'},
        {'name': 'claude.ai Example Mail', 'agent': 'claude', 'scope': 'claude.ai', 'liveness': 'connected'},
        {'name': 'switched-off', 'agent': 'claude', 'scope': 'user', 'wire': 'stdio', 'disabled': True},
        {'name': None, 'agent': 'cursor', 'error': 'JSONDecodeError'}]}
    return harness, mcp


class Build(unittest.TestCase):
    """The summary from this machine's scans: the schema's shape, an allow-list of fields, nothing guessed."""

    def build(self, **extra):
        harness, mcp = scans()
        return ns.build(device_id='dev-01j9a', org_id='org-7f3a', sequence=3, at='2026-10-22T09:00:00Z',
                        harness_scan=harness, mcp_scan=mcp, **extra)

    def test_the_harness_from_the_scans(self):
        summary, omitted = self.build()
        harness = summary['harness']
        self.assertEqual(summary['context'], 'org:org-7f3a')
        self.assertEqual(harness['codingAgents'], [{'id': 'claude-code', 'version': '2.1.296'}, {'id': 'codex'}])
        self.assertEqual(harness['mcpServers'], [
            {'name': 'alpha-local', 'source': 'local', 'transport': 'sse', 'health': 'unknown'},
            {'name': 'fabric', 'source': 'user', 'transport': 'stdio', 'health': 'ok'},
            {'name': 'plugin:switchboard:switchboard', 'source': 'plugin', 'transport': 'http',
             'health': 'degraded'}])
        self.assertEqual(harness['skills'], [
            {'name': 'task-pipeline', 'source': 'plugin', 'digest': 'sha256:' + 'a1' * 32},
            {'name': 'tidy-notes', 'source': 'user', 'digest': 'sha256:' + 'b2' * 32}])
        self.assertEqual(harness['plugins'], [{'name': 'switchboard@passioncode', 'source': 'marketplace',
                                               'version': '0.6.61'},
                                              {'name': 'odd@market', 'source': 'other'}])
        self.assertEqual(harness['hooks'], {'count': 3})
        self.assertEqual(summary['services'], [])
        self.assertNotIn('drift', summary)
        self.assertEqual(omitted, {'mcpServers': 1, 'skills': 1})

    def test_it_matches_the_schema_and_carries_nothing_it_never_carries(self):
        summary, _ = self.build()
        check = validator()
        if check is not None:
            self.assertEqual(list(check.iter_errors(summary)), [])
        said = json.dumps(summary)
        for never in ('~/', 'projects/alpha-web', '.claude.json', 'config.toml', 'deno-launch', 'h.example',
                      'OPENROUTER', 'python3', 'command', 'declared_in', 'args'):
            self.assertNotIn(never, said)

    def test_a_baseline_brings_the_drift_under_its_revision(self):
        baseline = {'revision': 7, 'items': [{'kind': 'coding-agent', 'name': 'codex', 'minVersion': '0.1'},
                                             {'kind': 'skill', 'name': 'tidy-notes', 'digest': DIGEST_A},
                                             {'kind': 'mcp-server', 'name': 'fabric'},
                                             {'kind': 'service', 'name': 'inbox-agent.default'}]}
        summary, _ = self.build(baseline=baseline)
        self.assertEqual(summary['drift'], {'policyRevision': 7, 'items': [
            {'kind': 'coding-agent', 'name': 'codex', 'state': 'outdated'},
            {'kind': 'skill', 'name': 'tidy-notes', 'state': 'changed'},
            {'kind': 'service', 'name': 'inbox-agent.default', 'state': 'missing'}]})
        self.assertEqual(ns.summary_findings({'summary': summary, 'baseline': baseline, 'org': 'org-7f3a'}), [])

    def test_services_are_copied_by_allow_list(self):
        summary, omitted = self.build(services=[
            {'key': 'inbox-agent.default', 'state': 'ok', 'protocols': ['fabric-service/0.1'],
             'mcpEndpoint': 'https://inbox.example/mcp', 'token': 'x', 'origin': 'http://127.0.0.1:9'},
            {'key': 'Bad Key', 'state': 'ok'},
            {'key': 'q-agent.one', 'state': 'weird', 'mcpEndpoint': 'https://q.example/mcp?k=v'}])
        self.assertEqual(summary['services'], [
            {'key': 'inbox-agent.default', 'state': 'ok', 'protocols': ['fabric-service/0.1'],
             'mcpEndpoint': 'https://inbox.example/mcp'},
            {'key': 'q-agent.one', 'state': 'unknown'}])
        self.assertEqual(omitted['services'], 1)

    def test_a_summary_that_would_break_fac_sem_128_is_refused_not_sent(self):
        harness, mcp = scans()
        harness['plugins'] = [{'name': 'someone@example', 'source': 'other', 'version': '1'},
                              {'name': '_'.join(('ghp', '0123456789abcdefghij' + 'ABCDEFGHIJ')), 'source': 'other'}]
        with self.assertRaises(ns.SummaryRefused) as refused:
            ns.build(device_id='dev-1', org_id='org-1', sequence=1, at='2026-10-22T09:00:00Z',
                     harness_scan=harness, mcp_scan=mcp)
        self.assertIn('/summary/harness/plugins/1/name', str(refused.exception))

    def test_malformed_scans_are_an_empty_harness_never_an_exception(self):
        summary, omitted = ns.build(device_id='dev-1', org_id='org-1', sequence=1, at='2026-10-22T09:00:00Z',
                                    harness_scan={'skills': 'no', 'hooks': None, 'plugins': [1, 'x']},
                                    mcp_scan=['not', 'an', 'object'])
        self.assertEqual(summary['harness'], {'codingAgents': [], 'mcpServers': [], 'skills': [], 'plugins': [],
                                              'hooks': {'count': 0}})
        self.assertEqual(omitted, {})

    def test_the_digest_ignores_the_sequence_and_the_time(self):
        a, _ = self.build()
        b = dict(a, sequence=9, at='2026-10-23T09:00:00Z')
        self.assertEqual(ns.content_digest(a), ns.content_digest(b))
        c = copy.deepcopy(a)
        c['harness']['hooks']['count'] = 4
        self.assertNotEqual(ns.content_digest(a), ns.content_digest(c))


try:
    import cryptography                                 # noqa: F401  the engine's `full` extra
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class CheckIn(unittest.TestCase):
    """An enrollment sends its summary in a check-in only to a server that lists `node-summary/1`."""

    def setUp(self):
        import tmp as tmpdir
        from test_node_org import ORG, T0, FakeServer
        from node.org import Enrollment
        self.org = ORG
        self.now = [float(T0 + 3600)]
        self.clock = lambda: self.now[0]
        self.server = FakeServer(self.clock, protocols=('fabric-device/0.1', 'fabric-activity/0.1', ns.TYPE))
        self.node = Path(tmpdir.mkdtemp()).resolve() / 'node'
        self.inputs = list(scans()) + [None]
        self.make = lambda: Enrollment(self.node, ORG, transport=self.server, clock=self.clock,
                                       harness=lambda: tuple(self.inputs))
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)

    def sent(self) -> list:
        return [body.get('records', {}).get('nodeSummary') for path, body in self.server.requests
                if path == '/v1/devices/check-in']

    def test_the_first_check_in_carries_the_summary(self):
        self.assertTrue(self.make().check_in()['ok'])
        (summary,) = self.sent()
        self.assertEqual((summary['context'], summary['sequence']), ('org:' + self.org, 1))
        self.assertEqual(summary['device'], {'id': self.make().keys.device_id})
        check = validator()
        if check is not None:
            self.assertEqual(list(check.iter_errors(summary)), [])
        status = self.make().describe()
        self.assertEqual(status['nodeSummary']['sequence'], 1)
        self.assertEqual(status['nodeSummary']['counts'], {'codingAgents': 2, 'mcpServers': 3, 'skills': 2,
                                                           'plugins': 2, 'hooks': 3, 'services': 0})
        self.assertIsNone(status['nodeSummary']['drift'])
        self.assertNotIn('org:node-summary-not-listed', status['degraded'])

    def test_resent_when_it_changed_and_at_least_once_a_day(self):
        self.make().check_in()
        self.make().check_in()
        self.assertEqual([s and s['sequence'] for s in self.sent()], [1, None])
        self.inputs[0]['hooks'] = self.inputs[0]['hooks'] + [{'event': 'Stop'}]
        self.make().check_in()
        self.now[0] += 86400
        self.make().check_in()
        self.assertEqual([s and s['sequence'] for s in self.sent()], [1, None, 2, 3])

    def test_a_check_in_that_failed_spends_its_sequence_and_sends_again(self):
        self.server.refuse = (503, 'unavailable')
        self.assertFalse(self.make().check_in()['ok'])
        self.server.refuse = None
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual([s['sequence'] for s in self.sent()], [1, 2])
        self.assertNotIn('note', self.make().describe()['nodeSummary'])

    def test_nothing_to_a_server_that_does_not_list_it(self):
        self.server.checkin_protocols = ['fabric-device/0.1', 'fabric-activity/0.1']
        self.make().check_in()                         # the check-in that tells the node: the protocol is no longer read
        self.make().check_in()
        self.assertEqual(self.sent()[1:], [None])
        status = self.make().describe()
        self.assertIn('org:node-summary-not-listed', status['degraded'])
        self.assertFalse(status['nodeSummary']['listed'])

    def test_without_the_scans_nothing_is_sent_and_the_person_is_told(self):
        self.inputs = [None, None, 'no harness scan on this machine yet (integrations.harness off, or ...)']
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.sent(), [None])
        status = self.make().describe()
        self.assertIn('org:node-summary-unavailable', status['degraded'])
        self.assertEqual(status['nodeSummary']['note']['reason'], 'node-summary-unavailable')
        preview = self.make().summary_preview()
        self.assertEqual((preview['error'], preview['command']), ('node-summary-unavailable',
                                                                  'project-observatory full doctor'))

    def test_the_terminal_prints_the_summary_it_would_send(self):
        import contextlib
        import io
        from unittest import mock
        sys.path.insert(0, str(ROOT / 'tools'))
        import org as org_tool
        out = io.StringIO()
        with mock.patch.object(org_tool, '_enrollment', lambda org_id: self.make()), \
                mock.patch.object(org_tool, '_orgs', lambda args: [self.org]), contextlib.redirect_stdout(out):
            code = org_tool.main(['summary', '--org', self.org])
        self.assertEqual(code, 0)
        answer = json.loads(out.getvalue())
        self.assertEqual(answer['schema'], 'org-summary/1')
        (row,) = answer['organizations']
        self.assertEqual((row['summary']['type'], row['omitted']), (ns.TYPE, {'mcpServers': 1, 'skills': 1}))
        self.assertEqual(self.sent(), [])

    def test_health_and_doctor_read_the_summary_from_the_saved_state(self):
        from node import org
        (notice,) = org.notices(self.node, now=self.clock())
        self.assertEqual(notice['summary'], {'listed': True, 'sentAt': None, 'sequence': None, 'counts': None,
                                             'drift': None})
        self.make().check_in()
        sent = len(self.server.log)
        (notice,) = org.notices(self.node, now=self.clock())
        self.assertEqual((notice['summary']['sequence'], notice['summary']['counts']['hooks']), (1, 3))
        self.assertEqual(len(self.server.log), sent, 'Health reads the saved state, never the network')
        source = (ROOT / 'dashboard' / 'build_dashboard.py').read_text(encoding='utf-8')
        rows = source[source.index('Array.isArray(H.organizations)'):]
        for said in ('S.listed', 'S.sentAt', 'S.drift', 'org summary --org', 'node-summary-'):
            self.assertIn(said, rows)

    def test_the_preview_shows_the_next_summary_and_changes_nothing(self):
        before = self.make().state()
        preview = self.make().summary_preview()
        self.assertEqual(preview['summary']['sequence'], 1)
        self.assertEqual((preview['listed'], preview['baseline'], preview['lastSent']), (True, 'none', None))
        self.assertEqual(self.make().state(), before)
        self.assertEqual(self.sent(), [])


if __name__ == '__main__':
    unittest.main()
