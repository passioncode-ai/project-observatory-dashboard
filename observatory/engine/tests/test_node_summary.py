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
        summary, omitted, _ = self.build()
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
        summary, _, _ = self.build()
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
        summary, _, _ = self.build(baseline=baseline)
        self.assertEqual(summary['drift'], {'policyRevision': 7, 'items': [
            {'kind': 'coding-agent', 'name': 'codex', 'state': 'outdated'},
            {'kind': 'skill', 'name': 'tidy-notes', 'state': 'changed'},
            {'kind': 'service', 'name': 'inbox-agent.default', 'state': 'missing'}]})
        self.assertEqual(ns.summary_findings({'summary': summary, 'baseline': baseline, 'org': 'org-7f3a'}), [])

    def test_services_are_copied_by_allow_list(self):
        summary, omitted, _ = self.build(services=[
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
        summary, omitted, _ = ns.build(device_id='dev-1', org_id='org-1', sequence=1, at='2026-10-22T09:00:00Z',
                                    harness_scan={'skills': 'no', 'hooks': None, 'plugins': [1, 'x']},
                                    mcp_scan=['not', 'an', 'object'])
        self.assertEqual(summary['harness'], {'codingAgents': [], 'mcpServers': [], 'skills': [], 'plugins': [],
                                              'hooks': {'count': 0}})
        self.assertEqual(omitted, {})

    def test_the_persons_exclusion_list_keeps_names_back_before_anything_is_checked(self):
        harness, mcp = scans()
        harness['plugins'].append({'name': 'someone@example', 'source': 'other'})   # FAC-SEM-128 would refuse it
        summary, omitted, excluded = ns.build(device_id='dev-1', org_id='org-1', sequence=1,
                                              at='2026-10-22T09:00:00Z', harness_scan=harness, mcp_scan=mcp,
                                              exclude=['tidy-*', 'someone@*', 'plugin:*', 'codex'])
        said = json.dumps(summary)
        for gone in ('tidy-notes', 'someone@example', 'plugin:switchboard:switchboard', '"codex"'):
            self.assertNotIn(gone, said)
        self.assertEqual(excluded, {'skills': 1, 'plugins': 1, 'mcpServers': 1, 'codingAgents': 1})
        self.assertEqual(omitted, {'mcpServers': 1, 'skills': 1})

    def test_a_dashed_home_folder_in_a_name_is_left_out(self):
        harness, mcp = scans()
        dashed = '-'.join(('', 'Users', 'alice', 'x'))
        harness['skills'].append({'name': 'notes.' + dashed, 'source': 'plain'})
        harness['plugins'].append({'name': 'tool@' + '-'.join(('', 'home', 'alice')), 'source': 'other'})
        self.assertEqual(ns.document_findings({'harness': {'skills': [{'name': 'notes.' + dashed}]}}), [],
                         'the contract does not catch it after a dot: the node does')
        summary, omitted, _ = ns.build(device_id='dev-1', org_id='org-1', sequence=1, at='2026-10-22T09:00:00Z',
                                       harness_scan=harness, mcp_scan=mcp)
        self.assertNotIn('alice', json.dumps(summary))
        self.assertEqual((omitted['skills'], omitted['plugins']), (2, 1))
        for name in ('smart-home-mcp', 'end-users-guide', 'my-home-page', 'Users-guide'):   # names, not folders
            self.assertIsNone(ns.DASHED_HOME.search(name), name)
        for name in ('notes.-Users-alice-x', 'x@-home-alice', 'a-Users-alice-projects-x', 'p:-home-bob'):
            self.assertIsNotNone(ns.DASHED_HOME.search(name), name)

    def test_the_drift_is_capped_at_the_schemas_bound(self):
        baseline = {'revision': 3, 'items': [{'kind': 'skill', 'name': 's%d' % i} for i in range(1100)]}
        summary, omitted, _ = self.build(baseline=baseline)
        self.assertEqual(len(summary['drift']['items']), 1024)
        self.assertEqual(omitted['drift'], 76)
        check = validator()
        if check is not None:
            self.assertEqual(list(check.iter_errors(summary)), [])

    def test_the_digest_ignores_the_sequence_and_the_time(self):
        a, _, _ = self.build()
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


class _Enrolled(unittest.TestCase):
    """One enrollment against the fake organization server, the person's consent given."""

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
        self.exclusions = ([], None)
        self.transport = self.server
        self.make = lambda: Enrollment(self.node, ORG, transport=lambda *a: self.transport(*a), clock=self.clock,
                                       harness=lambda: tuple(self.inputs), exclusions=lambda: self.exclusions)
        self.make().enroll('https://org.example', 'et_1.' + 'S' * 32)
        self.make().set_summary_consent(True)

    def sent(self) -> list:
        return [body.get('records', {}).get('nodeSummary') for path, body in self.server.requests
                if path == '/v1/devices/check-in']


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class CheckIn(_Enrolled):
    """An enrollment sends its summary in a check-in only to a server that lists `node-summary/1`."""

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
                                             'drift': None, 'consent': {'on': True, 'at': org.iso(self.clock())},
                                             'state': 'pending'})
        self.make().check_in()
        sent = len(self.server.log)
        (notice,) = org.notices(self.node, now=self.clock())
        self.assertEqual((notice['summary']['sequence'], notice['summary']['counts']['hooks']), (1, 3))
        self.assertEqual(len(self.server.log), sent, 'Health reads the saved state, never the network')
        source = (ROOT / 'dashboard' / 'build_dashboard.py').read_text(encoding='utf-8')
        rows = source[source.index('Array.isArray(H.organizations)'):]
        for said in ('S.listed', 'S.sentAt', 'S.drift', 'org summary --org', 'node-summary-', '"consent-off"',
                     '--consent on', 'S.refused'):
            self.assertIn(said, rows)

    def test_the_preview_shows_the_next_summary_and_changes_nothing(self):
        before = self.make().state()
        preview = self.make().summary_preview()
        self.assertEqual(preview['summary']['sequence'], 1)
        self.assertEqual((preview['listed'], preview['baseline'], preview['lastSent']), (True, 'none', None))
        self.assertEqual(self.make().state(), before)
        self.assertEqual(self.sent(), [])


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Consent(_Enrolled):
    """Off until the person turns it on, per organization (until the contract's CT-92 answers the other-contexts
    question): nothing is sent, the person is told, and the dry run still shows what would be shared."""

    def setUp(self):
        super().setUp()
        self.make().set_summary_consent(False)

    def test_off_sends_nothing_and_says_so(self):
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.sent(), [None])
        status = self.make().describe()
        self.assertEqual((status['nodeSummary']['state'], status['nodeSummary']['said']),
                         ('consent-off', 'harness summary: not shared (consent off)'))
        self.assertFalse(status['nodeSummary']['consent']['on'])
        preview = self.make().summary_preview()
        self.assertEqual(preview['summary']['type'], ns.TYPE, 'the dry run works before consent')
        self.assertEqual(preview['command'], 'project-observatory full org summary --org %s --consent on' % self.org)

    def test_the_terminal_turns_it_on_for_one_organization_with_its_time(self):
        import contextlib
        import io
        from unittest import mock
        sys.path.insert(0, str(ROOT / 'tools'))
        import org as org_tool
        self.now[0] += 120
        with mock.patch.object(org_tool, '_enrollment', lambda org_id: self.make()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(org_tool.main(['summary', '--org', self.org, '--consent', 'on']), 0)
        (row,) = json.loads(out.getvalue())['organizations']
        from node.org import iso
        self.assertEqual(row['consent'], {'on': True, 'at': iso(self.clock())})
        self.assertEqual(self.make().state()['summaryConsent']['at'], iso(self.clock()))
        self.make().check_in()
        self.assertEqual(self.sent()[-1]['sequence'], 1)
        with mock.patch.object(org_tool, '_enrollment', lambda org_id: self.make()), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(org_tool.main(['summary', '--consent', 'off']), 1)
        self.assertEqual(json.loads(err.getvalue())['error'], 'org-required')


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Refusal(_Enrolled):
    """A server that refuses the summary never stops check-ins: the check-in goes again without it at once, the
    refusal is recorded, and the summary waits a day."""

    def refusing(self, status=422, code='schema-invalid', pointer='/records/nodeSummary', always=False):
        def transport(url, headers, body, timeout):
            doc = json.loads(body)
            if url.endswith('/v1/devices/check-in') and (always or 'records' in doc):
                self.server.requests.append(('/v1/devices/check-in', doc))
                return status, json.dumps({'error': {'code': code, 'pointer': pointer}}).encode()
            return self.server(url, headers, body, timeout)
        self.transport = transport

    def test_the_check_in_goes_on_without_the_summary(self):
        self.refusing()
        result = self.make().check_in()
        self.assertTrue(result['ok'], result)
        self.assertEqual([s and s['sequence'] for s in self.sent()], [1, None])
        state = self.make().state()
        self.assertEqual((state['summaryRefused']['code'], state['summaryRefused']['status']), ('schema-invalid', 422))
        self.assertIn('org:node-summary-refused', self.make().describe()['degraded'])
        self.make().check_in()                         # within the day: no summary, no second refusal
        self.assertEqual(self.sent()[2:], [None])
        self.now[0] += 86400 + 1
        self.transport = self.server
        self.make().check_in()
        self.assertEqual(self.sent()[-1]['sequence'], 2, 'tried again after a day, and taken')
        self.assertNotIn('summaryRefused', self.make().state())

    def test_a_refusal_that_names_the_summary_under_another_status(self):
        self.refusing(status=403, code='node-summary-not-accepted', pointer=None)
        self.assertTrue(self.make().check_in()['ok'])
        self.assertEqual(self.make().state()['summaryRefused']['status'], 403)

    def test_a_refusal_of_the_check_in_itself_is_not_blamed_on_the_summary(self):
        self.refusing(status=422, always=True)
        self.assertFalse(self.make().check_in()['ok'])
        self.assertNotIn('summaryRefused', self.make().state())
        sent = len(self.sent())
        self.assertFalse(self.make().check_in()['ok'])
        self.assertEqual(self.sent()[sent:], [None], 'held until a check-in is taken: one request, not two')
        self.transport = self.server
        self.assertTrue(self.make().check_in()['ok'])              # taken bare: the summary may ride again
        self.make().check_in()
        self.assertEqual([s and s['sequence'] for s in self.sent()[sent + 1:]], [None, 2])
        before = len(self.sent())
        self.inputs[0]['hooks'] = []                          # a changed summary is due again
        self.refusing(status=401, code='signature-invalid', pointer=None, always=True)
        self.assertFalse(self.make().check_in()['ok'])
        self.assertEqual(len(self.sent()) - before, 1, 'a signature refusal is not retried bare')


@unittest.skipUnless(HAVE_CRYPTO, 'needs the engine full extra (cryptography)')
class Lifetime(_Enrolled):
    """A renewal or a re-enrollment writes the state afresh; the consent, the rising sequence and (across a renewal)
    the last summary taken survive it."""

    def test_a_renewal_by_the_key_on_record_keeps_consent_sequence_and_the_last_summary(self):
        from node.relay import strict_time
        self.make().check_in()
        before = self.make().state()
        self.server.renewal = True
        self.now[0] = strict_time(before['certificate']['notAfter']) / 1000 + 3600
        result = self.make().check_in()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['renewed']['by'], 'key-on-record')
        after = self.make().state()
        self.assertEqual(after['summaryConsent'], before['summaryConsent'])
        self.assertEqual(after['nodeSummary']['digest'], before['nodeSummary']['digest'])
        self.assertEqual(self.sent()[-1]['sequence'], 2, 'the day passed: sent again, the sequence still rising')

    def test_a_re_enrollment_keeps_the_sequence_rising_and_the_consent_of_the_same_organization(self):
        self.make().check_in()
        self.make().check_in()
        self.inputs[0]['hooks'] = []
        self.make().check_in()                                # sequences 1 and 2 sent
        self.make().enroll('https://org.example', 'et_2.' + 'T' * 32)
        state = self.make().state()
        self.assertEqual((state['summarySequence'], state['summaryConsent']['on']), (2, True))
        self.assertNotIn('nodeSummary', state, 'a new enrollment sends the summary again')
        self.make().check_in()
        self.assertEqual(self.sent()[-1]['sequence'], 3)

    def test_consent_is_not_carried_from_another_organization(self):
        from node.org import _summary_carried
        previous = {'org': 'org_other', 'summarySequence': 4, 'summaryConsent': {'on': True, 'at': 'x'},
                    'nodeSummary': {'digest': 'sha256:x'}}
        self.assertEqual(_summary_carried(previous, self.org, renewal=False), {'summarySequence': 4})
        self.assertEqual(_summary_carried(None, self.org, renewal=True), {})

    def test_a_tick_reads_the_scans_only_when_a_check_in_is_due(self):
        reads = []
        harness = lambda: (reads.append(1), tuple(self.inputs))[1]       # noqa: E731
        from node.org import Enrollment
        make = lambda: Enrollment(self.node, self.org, transport=self.server, clock=self.clock,   # noqa: E731
                                  harness=harness, exclusions=lambda: self.exclusions)
        make().tick()
        self.assertEqual(len(reads), 1)
        make().tick()                                        # the next check-in is 300 s away
        self.assertEqual(len(reads), 1)
        self.now[0] += 301
        make().tick()
        self.assertEqual(len(reads), 2)


class Scans(unittest.TestCase):
    """`read_harness_scans`: a scan whose integration is off, older than 48 h, or written before the summary existed
    is never sent; `read_exclusions`: the person's list, owner-only or nothing is sent."""

    NOW = 1792396800.0                                   # 2026-10-19T08:00:00Z

    def setUp(self):
        import tmp as tmpdir
        self.folder = Path(tmpdir.mkdtemp()).resolve()
        harness, mcp = scans()
        self.harness = dict(harness, generated_at='2026-10-19T07:30:00Z')
        self.mcp = dict(mcp, scanned_at='2026-10-19T07:30:00Z')
        self.on = {'harness': True, 'mcp': True}

    def read(self):
        from node.org import read_harness_scans
        (self.folder / 'harness.json').write_text(json.dumps(self.harness))
        (self.folder / 'mcp.json').write_text(json.dumps(self.mcp))
        return read_harness_scans(self.NOW, scratch=self.folder, enabled=lambda name: self.on[name])

    def test_fresh_scans_are_read(self):
        harness, mcp, missing = self.read()
        self.assertIsNone(missing)
        self.assertEqual(len(harness['skills']), 4)

    def test_a_switched_off_integration_leaves_its_old_scan_unsent(self):
        self.on['mcp'] = False
        self.assertIn('integrations.mcp is off', self.read()[2])

    def test_a_scan_older_than_two_days_is_refused(self):
        self.harness['generated_at'] = '2026-10-17T07:59:00Z'
        self.assertIn('past the 48 h', self.read()[2])
        self.harness['generated_at'] = 'not a time'
        self.assertIn('carries no time', self.read()[2])

    def test_a_harness_scan_written_before_the_summary_is_refused(self):
        del self.harness['coding_agents']
        self.assertIn('predates the node summary', self.read()[2])

    def test_the_exclusion_list_is_owner_only_or_nothing_is_sent(self):
        import os
        from node.org import read_exclusions
        path = self.folder / 'node-summary.json'
        self.assertEqual(read_exclusions(path), ([], None))
        path.write_text(json.dumps({'exclude': ['private-*', 'plugin:*:secret']}))
        path.chmod(0o600)
        self.assertEqual(read_exclusions(path), (['private-*', 'plugin:*:secret'], None))
        if os.name == 'posix':
            path.chmod(0o644)
            self.assertIn('owner-only', read_exclusions(path)[1])
            path.chmod(0o600)
        path.write_text(json.dumps({'exclude': 'private-*'}))
        self.assertIn('must be {"exclude"', read_exclusions(path)[1])

    @unittest.skipUnless(__import__('os').name == 'posix', 'links and modes as POSIX keeps them')
    def test_the_exclusion_list_under_a_linked_folder_is_read_and_failures_are_named(self):
        import os
        from node.org import read_exclusions
        real = self.folder / 'real-config'
        real.mkdir(mode=0o700)
        (real / 'node-summary.json').write_text(json.dumps({'exclude': ['private-*']}))
        (real / 'node-summary.json').chmod(0o600)
        linked = self.folder / 'linked'
        linked.symlink_to(real)                               # a workspace under a linked folder, as /tmp is
        self.assertEqual(read_exclusions(linked / 'node-summary.json'), (['private-*'], None))
        os.symlink(real / 'node-summary.json', real / 'other.json')
        self.assertIn('symbolic link', read_exclusions(real / 'other.json')[1])
        real.chmod(0o777)
        self.assertIn('folder others may write to', read_exclusions(real / 'node-summary.json')[1])
        real.chmod(0o700)


if __name__ == '__main__':
    unittest.main()
