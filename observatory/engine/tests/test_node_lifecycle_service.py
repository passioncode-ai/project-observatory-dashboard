#!/usr/bin/env python3
"""node.lifecycle served (OBS-79 step L3, docs/design/LIFECYCLE.md): the MCP surface on the local server, the
access-binding authority, the event log, and the operator's `full lifecycle` verbs.

Ported from the private broker's tests (mechanics only): its HTTP and MCP surface, its admin CLI, its event log and
its host credential. Where the engine works differently the case says so: callers prove themselves with an access
binding instead of a token file of the broker's own, the operator's authority is a terminal instead of an owner
token, and the server's one-copy rule, its own log rotation and its quiet handling of a client that went away are
already the server's (tests/test_fabric_service.py, tests/test_serverd.py), so they are not tested twice here. The
five `unenroll` cases wait for that verb (step L5, with the import).

Fixtures are neutral: principals `agent:alpha`, `agent:beta`; targets `alpha.app`, `beta.app`; signing team
`ABCDE12345`; every file in a temporary folder; no installed application is touched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

import access_binding as AB                            # noqa: E402
import lifecycle_cli                                   # noqa: E402
from node.lifecycle import broker, macos               # noqa: E402
from node.lifecycle import service as S                # noqa: E402
from test_node_lifecycle_broker import FileAdapter, ProcessAdapter, app_identity, target  # noqa: E402

AUDIENCE = 'observatory:test'
ALPHA, BETA = 'agent:alpha', 'agent:beta'


def bearer(name: str) -> str:
    """A fixture bearer composed rather than written out, so the secret scan has no key-shaped literal."""
    return name.replace(':', '-') + '-' + 'b' * 32


def stamp(at: datetime) -> str:
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def binding(principal: str, n: int, *, revoked: bool = False, days: int = 30, audience: str = AUDIENCE,
            token: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    return {'bindingId': f'bnd_fixture{n:04d}', 'principal': principal, 'channel': 'http', 'audience': audience,
            'credentialRef': {'kind': 'bearer-sha256', 'digest': AB.bearer_digest(token or bearer(principal))},
            'projects': ['project:alpha-web'], 'workflows': None, 'scopes': ['memory.read'],
            'classCeiling': 'public', 'effectCeiling': 'read', 'issuedAt': stamp(now - timedelta(days=1)),
            'expiresAt': stamp(now + timedelta(days=days)), 'revokedAt': stamp(now) if revoked else None,
            'issuedBy': 'operator', 'via': 'terminal'}


def grant(tools=broker.TOOLS, targets=('alpha.app',), revision=1, binding_id='bnd_fixture0001', **extra) -> dict:
    return {'bindingId': binding_id, 'revision': revision, 'revoked': False, 'allowedTools': list(tools),
            'allowedTargets': list(targets), **extra}


def lifecycle_target(key='alpha.app', **extra) -> dict:
    return target(key, callers=(ALPHA, BETA)) | extra


FABRIC_IDENTITY = app_identity(broker.FABRIC_BUNDLE_ID)
#: A second bearer of agent:alpha, for a second binding of the same principal (one issued to read memory, say).
SECOND = bearer(ALPHA) + '-second'


class Workspace:
    """A config document, a state folder and a bindings registry, all in one temporary folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name).resolve()
        self.config = self.data / 'config' / S.CONFIG_NAME
        self.state = self.data / 'state'
        self.bindings = [binding(ALPHA, 1), binding(BETA, 2)]
        self.doc = {'schemaVersion': 1, 'targets': {'alpha.app': lifecycle_target()},
                    'callers': {ALPHA: grant()}}
        self.save()
        self.adapter = ProcessAdapter(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        S.write_config(self.doc, self.config)

    def registry(self) -> AB.Registry:
        return AB.parse({'schema': AB.SCHEMA, 'revision': 1, 'bindings': self.bindings})

    def service(self, adapter=None) -> S.Service:
        return S.Service(config=self.config, state=self.state, adapter=adapter or self.adapter,
                         bindings=self.registry, audience=lambda: AUDIENCE, version='0.0.0')

    def events(self):
        path = self.state / S.EVENTS_FILE
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def rpc(service, principal, method, params=None, rid=1):
        return service.handle(principal, {'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}})

    @staticmethod
    def call(service, principal, name, arguments=None):
        response = Workspace.rpc(service, principal, 'tools/call', {'name': name, 'arguments': arguments or {}})
        return response['result']


# -- the MCP surface ---------------------------------------------------------------------------------------------------

class ServiceTests(Workspace, unittest.TestCase):
    def test_initialize_tells_every_agent_the_rules(self):
        service = self.service()
        try:
            principal = service.authenticate('Bearer ' + bearer(ALPHA))
            text = self.rpc(service, principal, 'initialize', {'protocolVersion': '2025-06-18'})['result']['instructions']
        finally:
            service.close()
        for phrase in ('ensure_running', 'renews your lease', 'every 10 minutes', 'in_use_by_person', 'held_off',
                       'Never launch, quit or kill', 'full lifecycle allow'):
            self.assertIn(phrase, text)
        self.assertLessEqual(len(text), 1800, 'the instructions an agent reads at every initialize stay short')
        self.assertNotIn('\u2014', text, 'text for people carries no em dash')

    def test_stop_and_restart_are_served_and_marked_destructive(self):
        service = self.service()
        try:
            principal = service.authenticate('Bearer ' + bearer(ALPHA))
            tools = {t['name']: t for t in self.rpc(service, principal, 'tools/list')['result']['tools']}
        finally:
            service.close()
        self.assertEqual(sorted(tools), sorted(broker.TOOLS))
        for name in ('lifecycle.stop', 'lifecycle.restart'):
            self.assertTrue(tools[name]['annotations']['destructiveHint'], name)
        self.assertFalse(tools['lifecycle.ensure_running']['annotations']['destructiveHint'])
        self.assertTrue(tools['lifecycle.status']['annotations']['readOnlyHint'])

    def test_a_caller_is_listed_only_the_tools_its_grant_names(self):
        # Adapted: the source widened its owner's tools at every start. Here no bearer is an owner, so nothing is
        # widened by a start, and a caller sees exactly what `full lifecycle allow` gave it.
        self.doc['callers'][BETA] = grant(tools=('lifecycle.list',), binding_id='bnd_fixture0002')
        self.save()
        before = self.config.read_bytes()
        service = self.service()
        try:
            beta = service.authenticate('Bearer ' + bearer(BETA))
            tools = [t['name'] for t in self.rpc(service, beta, 'tools/list')['result']['tools']]
            stop = self.call(service, beta, 'lifecycle.stop', {'targetId': 'alpha.app', 'requestId': 'r-1',
                                                               'expectedPolicyRevision': 1, 'grantRevision': 1})
        finally:
            service.close()
        self.assertEqual(tools, ['lifecycle.list'])
        self.assertEqual(stop['structuredContent']['error'], 'denied')
        self.assertIn('full lifecycle allow', stop['structuredContent']['hint'])
        self.assertEqual(self.config.read_bytes(), before, 'a start changes no grant')

    def test_events_name_the_app_and_refusals_wake_nobody(self):
        service = self.service()
        try:
            service.operation_finished({'state': 'ready'}, None,
                                       {'targetId': 'alpha.app', 'kind': 'ensure', 'reason': 'always_on'})
            service.operation_finished({'state': 'stopped'}, None,
                                       {'targetId': 'alpha.app', 'kind': 'stop', 'reason': 'idle'})
            service.operation_finished({'state': 'in_use_by_person'}, None,
                                       {'targetId': 'alpha.app', 'kind': 'stop', 'reason': None})
            service.operation_finished({'state': 'stop_timeout'}, None,
                                       {'targetId': 'alpha.app', 'kind': 'stop', 'reason': None})
        finally:
            service.close()
        events = self.events()[-4:]
        self.assertEqual([e['kind'] for e in events], ['lifecycle.supervisor_started', 'lifecycle.idle_stopped',
                                                       'lifecycle.refused', 'lifecycle.stop_failed'])
        self.assertIn('Alpha', events[0]['text'])
        self.assertEqual([bool(e.get('notify')) for e in events], [False, False, False, True])

    def test_one_ensure_through_the_wire_starts_once_and_reads_back(self):
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            listed = self.call(service, alpha, 'lifecycle.list')['structuredContent']
            self.assertEqual([t['targetId'] for t in listed['targets']], ['alpha.app'])
            args = {'targetId': 'alpha.app', 'requestId': 'r-1', 'expectedPolicyRevision': 1,
                    'grantRevision': listed['grantRevision']}
            receipt = self.call(service, alpha, 'lifecycle.ensure_running', args)['structuredContent']
            end = time.monotonic() + 10
            got = receipt
            while got['state'] == 'running' and time.monotonic() < end:
                time.sleep(0.02)
                got = self.call(service, alpha, 'lifecycle.operation.get',
                                {'operationId': receipt['operationId']})['structuredContent']
            again = self.call(service, alpha, 'lifecycle.ensure_running', args)['structuredContent']
        finally:
            service.close()
        self.assertEqual(got['state'], 'ready')
        self.assertEqual(again['operationId'], receipt['operationId'], 'one request id means one operation')
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app')])

    def test_a_malformed_argument_is_an_error_and_a_refusal_is_a_typed_answer(self):
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            bad = self.call(service, alpha, 'lifecycle.status', {'targetId': 'alpha.app', 'shell': 'rm -rf /'})
            unknown = self.call(service, alpha, 'lifecycle.status', {'targetId': 'nope.app'})
            tool = self.call(service, alpha, 'shell.exec')
        finally:
            service.close()
        self.assertTrue(bad['isError'])
        self.assertEqual(bad['structuredContent']['error'], 'invalid_argument')
        self.assertFalse(unknown['isError'])
        self.assertEqual(unknown['structuredContent']['error'], 'denied')
        self.assertEqual(tool['structuredContent']['error'], 'unknown-tool')


class AuthorityTests(Workspace, unittest.TestCase):
    def test_only_a_binding_of_this_workspace_proves_a_caller(self):
        self.bindings = [binding(ALPHA, 1, audience='observatory:other')]
        service = self.service()
        try:
            for header in (None, 'Basic x', 'Bearer ' + bearer(ALPHA), 'Bearer ' + bearer('agent:nobody')):
                with self.subTest(header=header and header[:12]):
                    with self.assertRaisesRegex(broker.LifecycleError, 'unauthorized'):
                        service.authenticate(header)
        finally:
            service.close()

    def test_only_the_bound_binding_opens_the_tools_not_another_of_the_same_principal(self):
        # A second binding of agent:alpha, issued (say) to read memory, shares the principal but not the grant.
        self.bindings.append(binding(ALPHA, 3, token=SECOND))
        service = self.service()
        try:
            with self.assertRaisesRegex(broker.LifecycleError, 'denied'):
                service.authenticate('Bearer ' + SECOND)
        finally:
            service.close()

    def test_revoking_the_bound_binding_refuses_even_with_another_in_force(self):
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            self.bindings = [binding(ALPHA, 1, revoked=True), binding(ALPHA, 3, token=SECOND)]
            self.assertEqual(self.call(service, alpha, 'lifecycle.list')['structuredContent']['error'], 'denied')
            with self.assertRaisesRegex(broker.LifecycleError, 'unauthorized'):
                service.authenticate('Bearer ' + bearer(ALPHA))
        finally:
            service.close()

    def test_a_grant_rebound_to_another_binding_refuses_the_old_one(self):
        self.bindings.append(binding(ALPHA, 3, token=SECOND))
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            self.doc['callers'][ALPHA] = grant(revision=2, binding_id='bnd_fixture0003')
            self.save()
            alpha['grantRevision'] = 2
            self.assertEqual(self.call(service, alpha, 'lifecycle.list')['structuredContent']['error'], 'denied')
        finally:
            service.close()

    def test_a_binding_without_a_lifecycle_grant_is_denied(self):
        service = self.service()
        try:
            with self.assertRaisesRegex(broker.LifecycleError, 'denied'):
                service.authenticate('Bearer ' + bearer(BETA))
        finally:
            service.close()

    def test_revoking_the_binding_or_the_grant_refuses_the_next_call(self):
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            self.assertNotIn('error', self.call(service, alpha, 'lifecycle.list')['structuredContent'])
            self.bindings = [binding(ALPHA, 1, revoked=True)]
            self.assertEqual(self.call(service, alpha, 'lifecycle.list')['structuredContent']['error'], 'denied')
            self.assertEqual(self.rpc(service, alpha, 'tools/list')['result']['tools'], [])
            self.bindings = [binding(ALPHA, 1)]
            self.doc['callers'][ALPHA] = grant(revision=2) | {'revoked': True}
            self.save()
            self.assertEqual(self.call(service, alpha, 'lifecycle.list')['structuredContent']['error'], 'denied')
        finally:
            service.close()

    def test_a_grant_revoked_between_admission_and_effect_launches_nothing(self):
        adapter = FileAdapter(self.data, delay=0)
        service = self.service(adapter)

        def revoke():
            self.doc['callers'][ALPHA] = grant(revision=2) | {'revoked': True}
            self.save()

        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            adapter.identity = True
            with mock.patch.object(adapter, 'identity_matches', side_effect=lambda t, deadline=None: revoke() or True):
                receipt = service.broker.ensure(alpha, {'targetId': 'alpha.app', 'requestId': 'r-1',
                                                        'expectedPolicyRevision': 1, 'grantRevision': 1})
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    with service.broker.db_lock:
                        row = service.broker.db.execute('SELECT state FROM operations WHERE id=?',
                                                        (receipt['operationId'],)).fetchone()
                    if row['state'] != 'running':
                        break
                    time.sleep(0.02)
        finally:
            service.close()
        self.assertEqual(row['state'], 'denied')
        self.assertFalse((self.data / 'effects.log').exists(), 'nothing was launched')

    def test_unsupported_platform_is_said_never_a_start(self):
        service = self.service(macos.UnsupportedAdapter('linux'))
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            status = self.call(service, alpha, 'lifecycle.status', {'targetId': 'alpha.app'})['structuredContent']
            receipt = self.call(service, alpha, 'lifecycle.ensure_running', {
                'targetId': 'alpha.app', 'requestId': 'r-1', 'expectedPolicyRevision': 1,
                'grantRevision': 1})['structuredContent']
            end = time.monotonic() + 10
            while receipt['state'] == 'running' and time.monotonic() < end:
                time.sleep(0.02)
                receipt = self.call(service, alpha, 'lifecycle.operation.get',
                                    {'operationId': receipt['operationId']})['structuredContent']
        finally:
            service.close()
        self.assertEqual(status['state'], 'unsupported_platform')
        self.assertEqual(receipt['state'], 'unsupported_platform')


class ConfigTests(Workspace, unittest.TestCase):
    def test_no_bearer_is_the_owner(self):
        # Adapted from "the owner token still does everything": the operator acts at a terminal, so a grant that
        # names an owner, a token or its hash is refused whole.
        for extra in ({'owner': True}, {'tokenHash': 'a' * 64}, {'operatorProxy': False}):
            with self.subTest(extra=sorted(extra)):
                doc = json.loads(json.dumps(self.doc))
                doc['callers'][ALPHA].update(extra)
                with self.assertRaisesRegex(broker.LifecycleError, 'invalid_config'):
                    S.validate_config(doc)
        doc = json.loads(json.dumps(self.doc))
        doc['callers'][ALPHA]['allowedTargets'] = ['*']
        with self.assertRaisesRegex(broker.LifecycleError, 'invalid_config'):
            S.validate_config(doc)

    def test_a_document_others_may_read_or_a_link_is_refused_whole(self):
        os.chmod(self.config, 0o644)
        with self.assertRaisesRegex(broker.LifecycleError, 'unsafe_config'):
            S.read_config(self.config)
        os.chmod(self.config, 0o600)
        link = self.data / 'config' / 'linked.json'
        link.symlink_to(self.config)
        with self.assertRaisesRegex(broker.LifecycleError, 'unsafe_config'):
            S.read_config(link)
        self.assertEqual(S.read_config(self.data / 'absent.json'), S.empty_config())


class HostTests(Workspace, unittest.TestCase):
    def test_nothing_is_built_or_created_until_something_is_enrolled(self):
        absent = self.data / 'absent' / S.CONFIG_NAME
        built = []
        host = S.Host(factory=lambda: built.append(1), config=absent)
        host.start()
        self.assertIsNone(host.service())
        self.assertEqual((built, host.entry()['state']), ([], 'inert'))
        self.assertFalse((self.data / 'absent').exists())

    def test_the_entry_names_the_node_as_lifecycle_manager_and_a_failed_start_is_degraded(self):
        host = S.Host(factory=lambda: (_ for _ in ()).throw(broker.LifecycleError('already_running')),
                      config=self.config)
        host.start()
        entry = host.entry()
        self.assertEqual((entry['service'], entry['lifecycle'], entry['path']),
                         ('node.lifecycle', {'manager': 'node'}, '/mcp'))
        self.assertEqual(entry['capabilities'], list(broker.TOOLS))
        self.assertEqual(entry['state'], 'failed')
        self.assertEqual(host.degraded(), [{'source': 'node-lifecycle',
                                            'reason': 'lifecycle: unavailable, already_running'}])
        host.close()

    def corrupt_store(self):
        self.state.mkdir(mode=0o700)
        store = self.state / broker.STORE_FILE
        store.write_bytes(b'this is not a database ' * 512)
        os.chmod(store, 0o600)
        return store

    def test_a_damaged_store_is_a_degraded_row_and_a_repaired_one_starts(self):
        store = self.corrupt_store()
        host = S.Host(factory=self.service, config=self.config)
        host.start()                                    # never raises
        self.assertIsNone(host.service())
        self.assertEqual(host.entry()['state'], 'failed')
        self.assertEqual(host.degraded(), [{'source': 'node-lifecycle',
                                            'reason': 'lifecycle: unavailable, DatabaseError'}])
        # The failed broker let go of its lock: once the store is repaired the next attempt starts, never
        # `already_running` against a broker that never existed.
        store.unlink()
        try:
            self.assertIsNotNone(host.service())
            self.assertEqual((host.degraded(), host.entry()['state']), ([], 'serving'))
        finally:
            host.close()

    def test_deleting_the_document_after_a_failed_start_clears_the_fault(self):
        host = S.Host(factory=lambda: (_ for _ in ()).throw(sqlite3.DatabaseError('malformed')), config=self.config)
        host.start()
        self.assertEqual(host.entry()['state'], 'failed')
        self.config.unlink()                            # the operator un-enrolls everything
        self.assertIsNone(host.service())
        self.assertEqual((host.degraded(), host.entry()['state']), ([], 'inert'))
        host.start()                                    # the heartbeat after the deletion says the same
        self.assertEqual(host.degraded(), [])

    def test_a_store_that_fails_during_recovery_releases_the_lock(self):
        with mock.patch.object(broker.Broker, 'prune', side_effect=sqlite3.DatabaseError('database disk image is '
                                                                                            'malformed')):
            with self.assertRaises(sqlite3.DatabaseError):
                self.service()
        self.service().close()                          # not `already_running`


class SupervisionTests(Workspace, unittest.TestCase):
    """OBS-79 step L4: the local server's Host starts the supervisor with the service and stops it first."""

    def test_the_host_starts_supervision_and_close_stops_it_before_the_broker(self):
        host = S.Host(factory=self.service, config=self.config)
        host.start()
        service = host.service()
        self.assertTrue(service.supervisor.running)
        self.assertFalse(service.start_supervision(), 'one supervisor per service')
        order = []
        real_close = service.broker.close
        service.broker.close = lambda *a, **k: (order.append(service.supervisor.running), real_close(*a, **k))
        host.close()
        self.assertEqual(order, [False], 'the supervisor had stopped when the broker closed')
        self.assertTrue(any(json.loads(line)['event'] == 'supervision.started'
                            for line in (self.state / 'logs' / S.LOG_FILE).read_text().splitlines()))

    def test_a_service_built_outside_the_server_runs_no_supervisor(self):
        service = self.service()
        try:
            self.assertFalse(service.supervisor.running)
        finally:
            service.close()

    def test_two_services_over_one_state_folder_never_run_two_supervisors(self):
        first = S.Host(factory=self.service, config=self.config)
        first.start()
        second = S.Host(factory=self.service, config=self.config)
        second.start()
        try:
            self.assertTrue(first.service().supervisor.running)
            self.assertIsNone(second.service())
            self.assertEqual(second.degraded(), [{'source': 'node-lifecycle',
                                                  'reason': 'lifecycle: unavailable, already_running'}])
        finally:
            first.close()
            second.close()

    def test_a_supervisor_that_cannot_start_is_a_degraded_row_and_the_tools_still_serve(self):
        service = self.service()

        def refuse():
            raise RuntimeError("can't start new thread")
        service.start_supervision = refuse
        host = S.Host(factory=lambda: service, config=self.config)
        host.start()
        try:
            self.assertIs(host.service(), service)
            self.assertEqual(host.entry()['state'], 'serving')
            self.assertEqual(host.degraded(), [{'source': 'node-lifecycle',
                                                'reason': 'lifecycle: supervisor unavailable, RuntimeError'}])
        finally:
            host.close()

    def test_a_failed_pass_is_one_notify_event_and_every_failure_a_log_line(self):
        service = self.service()
        try:
            with mock.patch.object(service.broker, 'supervise', side_effect=sqlite3.OperationalError('locked')):
                service.supervisor.tick()
                service.supervisor.tick()
        finally:
            service.close()
        failed = [e for e in self.events() if e['kind'] == 'lifecycle.supervision_failed']
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]['notify'])
        self.assertNotIn('locked', failed[0]['text'], 'never the raw error text')
        lines = [json.loads(x) for x in (self.state / 'logs' / S.LOG_FILE).read_text().splitlines()]
        rows = [x for x in lines if x['event'] == 'supervision.pass_failed']
        self.assertEqual([(r['error'], r['first']) for r in rows], [('OperationalError', True),
                                                                    ('OperationalError', False)])

    def test_a_watcher_that_ended_is_a_log_line(self):
        service = self.service()
        try:
            service.watch_ended()
        finally:
            service.close()
        lines = [json.loads(x) for x in (self.state / 'logs' / S.LOG_FILE).read_text().splitlines()]
        self.assertIn('supervision.watch_ended', [x['event'] for x in lines])

    def test_off_macos_the_supervisor_only_keeps_the_store_bounded(self):
        service = self.service(adapter=macos.UnsupportedAdapter('linux'))
        try:
            self.assertFalse(service.supervisor.supported)
            with mock.patch.object(service.broker, 'supervise') as supervise:
                service.supervisor.step(woke=False)
            supervise.assert_not_called()
        finally:
            service.close()


# -- the local server ----------------------------------------------------------------------------------------------------

class HTTPTests(Workspace, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import serverd
        self.serverd = serverd
        self.svc = self.service()
        self.host = S.Host(factory=lambda: self.svc, config=self.config)
        self.host.start()
        self.token = 'service-token-' + 'c' * 32
        self.saved = (serverd.RUNTIME, serverd.LIFECYCLE)
        serverd.RUNTIME = serverd.Runtime(self.token)
        serverd.LIFECYCLE = self.host
        self.server = serverd.LoopbackServer(('127.0.0.1', 0), serverd.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.host.close()
        self.serverd.RUNTIME, self.serverd.LIFECYCLE = self.saved
        super().tearDown()

    def request(self, path='/.well-known/fabric-service', body=None, headers=None, method=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read()
                return exc.code, json.loads(raw) if raw else None

    def mcp(self, message, who=ALPHA, headers=None):
        auth = {'Authorization': 'Bearer ' + bearer(who)} if who else {}
        return self.request('/mcp', message, auth | (headers or {}))

    LIST = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'lifecycle.list', 'arguments': {},
                       '_meta': {'traceparent': '00-' + 'a' * 32 + '-' + 'b' * 16 + '-01'}}}

    def test_guards_apply_to_well_known_and_mcp(self):
        for path, body in (('/.well-known/fabric-service', None), ('/mcp', self.LIST)):
            for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                            {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(path=path, headers=sorted(headers)):
                    auth = {'Authorization': 'Bearer ' + bearer(ALPHA)} if body else {}
                    self.assertEqual(self.request(path, body, auth | headers)[0], 403)
        status, doc = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn('node-lifecycle', [d['source'] for d in doc['degraded']])

    def test_well_known_declares_its_mcp_surface_authenticates_on_its_own(self):
        # Contract DEC-0024: the callers bring credentials of their own (access bindings), the descriptor's token is
        # the host's, so the surface says `auth: own`; the entry says this node is the targets' lifecycle manager.
        status, doc = self.request()
        self.assertEqual(status, 200)
        # No capability list on the surface: the manifest is that list's authority (the probe's
        # interop.well-known-capabilities), and the six tools are named in the node.lifecycle entry instead.
        self.assertEqual(doc['surfaces']['mcp'], {'path': '/mcp', 'transport': 'streamable-http', 'auth': 'own'})
        entry = doc['extensions'][S.EXTENSION_KEY]
        self.assertEqual((entry['service'], entry['lifecycle'], entry['state'], entry['capabilities']),
                         ('node.lifecycle', {'manager': 'node'}, 'serving', list(broker.TOOLS)))

    def test_mcp_auth_scopes_and_trace(self):
        status, refused = self.mcp(self.LIST, who=None)
        self.assertEqual((status, refused['error']), (401, 'unauthorized'))
        self.assertIn('full access-binding issue', refused['hint'])
        status, response = self.mcp(self.LIST)
        self.assertEqual(status, 200)
        self.assertEqual([t['targetId'] for t in response['result']['structuredContent']['targets']], ['alpha.app'])
        self.assertIn('a' * 32, response['result']['_meta']['traceparent'])
        self.assertEqual(self.mcp(self.LIST, who=BETA)[0], 403, 'a binding without a lifecycle grant')
        self.assertEqual(self.request('/mcp')[0], 405)
        self.assertEqual(self.mcp({'jsonrpc': '2.0', 'method': 'notifications/initialized'})[0], 202)

    def test_malformed_rpc_returns_400(self):
        for params in ([], {'_meta': []}, {'arguments': []}):
            with self.subTest(params=params):
                status, _ = self.mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params})
                self.assertEqual(status, 400)
        status, _ = self.request('/mcp', None, {'Authorization': 'Bearer ' + bearer(ALPHA)}, method='POST')
        self.assertEqual(status, 413, 'no body')

    def test_s3_the_descriptor_token_is_not_an_mcp_credential(self):
        # The source gave a Fabric host its own token that could read events and act on nothing. Here the
        # descriptor's token is the service token: it opens the events feed and the usage report, never a tool.
        status, refused = self.request('/mcp', self.LIST, {'Authorization': 'Bearer ' + self.token})
        self.assertEqual((status, refused['error']), (401, 'unauthorized'))

    def test_s3_the_service_token_reads_events_but_cannot_act(self):
        status, _ = self.request('/fabric/v1/events', headers={'Authorization': 'Bearer ' + self.token})
        self.assertNotIn(status, (401, 403), 'the token still opens the feed (200, or 503 with no store yet)')
        status, _ = self.request('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}},
                                 {'Authorization': 'Bearer ' + self.token})
        self.assertEqual(status, 401)

    def test_a_damaged_lifecycle_store_keeps_the_server_up(self):
        class Broken(S.Host):
            def start(self):
                raise sqlite3.DatabaseError('database disk image is malformed')
        self.serverd.LIFECYCLE = Broken(config=self.config)
        with mock.patch.object(self.serverd, 'heartbeat', return_value={}):
            self.serverd.beat_once(self.serverd.RUNTIME)    # the heartbeat thread survives
        status, doc = self.request()
        self.assertEqual(status, 200, 'the well-known document still answers')
        self.assertIn({'source': 'node-lifecycle-start', 'reason': 'lifecycle: unavailable, DatabaseError'},
                      doc['degraded'])
        store = self.state / broker.STORE_FILE
        self.host.close()
        store.unlink()
        for suffix in ('-wal', '-shm'):
            (self.state / (broker.STORE_FILE + suffix)).unlink(missing_ok=True)
        store.write_bytes(b'not a database ' * 512)
        os.chmod(store, 0o600)
        self.serverd.LIFECYCLE = S.Host(factory=self.service, config=self.config)
        self.serverd.LIFECYCLE.start()
        status, refused = self.mcp(self.LIST)
        self.assertEqual((status, refused['error']), (503, 'unavailable'))
        self.assertEqual(self.request('/health')[0], 200)
        self.serverd.LIFECYCLE.close()

    def test_a_fault_cleared_by_deleting_the_document_is_403_not_503(self):
        self.serverd.LIFECYCLE = S.Host(factory=lambda: (_ for _ in ()).throw(sqlite3.DatabaseError('malformed')),
                                        config=self.config)
        self.serverd.LIFECYCLE.start()
        self.assertEqual(self.mcp(self.LIST)[0], 503)
        self.config.unlink()
        status, refused = self.mcp(self.LIST)
        self.assertEqual((status, refused['error']), (403, 'denied'))
        self.assertIn('nothing is enrolled', refused['detail'])

    def test_post_elsewhere_is_still_405_and_nothing_enrolled_is_refused(self):
        self.assertEqual(self.request('/health', {})[0], 405)
        self.serverd.LIFECYCLE = S.Host(config=self.data / 'absent.json')
        status, refused = self.mcp(self.LIST)
        self.assertEqual((status, refused['error']), (403, 'denied'))
        self.assertIn('nothing is enrolled', refused['detail'])


# -- what happened ---------------------------------------------------------------------------------------------------

class ObservabilityTests(Workspace, unittest.TestCase):
    """An event says what and why; the machine detail is in a bounded, private log."""

    def test_obs1_a_failure_event_names_the_target_and_the_reason(self):
        service = self.service()
        try:
            service.operation_finished({'state': 'capability_missing', 'reason': 'gui_owner_offline'}, None,
                                       {'targetId': 'alpha.app', 'kind': 'restart', 'reason': None,
                                        'operationId': 'op-1', 'caller': ALPHA})
            service.operation_finished({'state': 'timeout'}, None,
                                       {'targetId': 'alpha.app', 'kind': 'ensure', 'reason': None,
                                        'operationId': 'op-2'})
        finally:
            service.close()
        restart, ensure = self.events()[-2:]
        self.assertEqual(restart['subject'], {'kind': 'target', 'id': 'alpha.app'})
        self.assertIn('Alpha', restart['text'])
        self.assertIn('capability missing', restart['text'])
        self.assertEqual(ensure['kind'], 'lifecycle.failed')
        self.assertIn('Alpha', ensure['text'])
        self.assertIn('timeout', ensure['text'])

    def test_obs1_admitted_is_logged_once_per_new_operation_including_the_supervisors(self):
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            args = {'targetId': 'alpha.app', 'requestId': 's-1', 'grantRevision': 1, 'expectedPolicyRevision': 1}
            service.broker.ensure(alpha, args)
            service.broker.ensure(alpha, args)          # a replay is not a new admission
            service.broker._admit_internal(service.broker._target(broker.SUPERVISOR, 'x', 'alpha.app'), 'ensure',
                                           'always_on')
            time.sleep(0.3)
        finally:
            service.close()
        admitted = [e for e in self.events() if e['kind'] == 'lifecycle.admitted']
        self.assertEqual(len(admitted), 2, [e['text'] for e in admitted])
        self.assertTrue(all(e.get('subject', {}).get('id') == 'alpha.app' for e in admitted))

    def test_f10_concurrent_appends_lose_no_event(self):
        self.state.mkdir(mode=0o700)
        log = S.EventLog(self.state / S.EVENTS_FILE)
        threads = [threading.Thread(target=lambda: [log.append('test.event', 'info', 'x') for _ in range(10)])
                   for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [e['id'] for e in self.events()]
        self.assertEqual((len(ids), len(set(ids))), (120, 120))

    def test_obs2_the_machine_log_is_private_rotated_and_structured(self):
        service = self.service()
        try:
            self.assertEqual((service.log.max_bytes, service.log.backups), (5 * 1024 * 1024, 5), 'LC-12: 5 x 5 MB')
            service.note('operation.finished', targetId='alpha.app', operationId='op-1', state='ready')
        finally:
            service.close()
        path = self.state / 'logs' / S.LOG_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        line = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual((line['event'], line['operationId'], line['state']), ('operation.finished', 'op-1', 'ready'))

    def test_obs2_a_disposable_service_never_logs_into_the_real_log(self):
        # Adapted: the engine's state is the workspace's, so a test workspace (or any other) logs into its own.
        import paths
        self.assertEqual(S.state_dir(), Path(paths.STATE) / 'node-lifecycle')
        self.service().close()
        self.assertTrue((self.state / 'logs' / S.LOG_FILE).is_file())

    def test_one_notify_event_in_words_when_supervision_gives_up(self):
        self.doc['targets'] = {'fabric.desktop': lifecycle_target('fabric.desktop', identity=FABRIC_IDENTITY,
                                                                  path='/Applications/Fabric.app',
                                                                  desiredState='always_on')}
        self.doc['callers'] = {}
        self.save()
        service = self.service()
        try:
            service.supervision_gave_up({'targetId': 'fabric.desktop', 'failedStarts': 3, 'windowSeconds': 1800})
        finally:
            service.close()
        [event] = [e for e in self.events() if e['kind'] == 'lifecycle.supervisor_gave_up']
        self.assertTrue(event['notify'])
        self.assertEqual(event['text'], 'Fabric keeps failing to start, so the node stopped retrying; '
                                        'open it to see why.')
        self.assertEqual(event['subject'], {'kind': 'target', 'id': 'fabric.desktop'})


# -- the operator's verbs -------------------------------------------------------------------------------------------

class CliTests(Workspace, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.doc['callers'] = {ALPHA: grant(tools=('lifecycle.list',))}
        self.save()
        patches = [mock.patch.object(S, 'config_path', return_value=self.config),
                   mock.patch.object(S, 'state_dir', return_value=self.state),
                   mock.patch.object(lifecycle_cli, '_is_terminal', return_value=True),
                   # OBS-79 L2r: `status` never looks at an installed app here (tests/test_node_lifecycle_helper.py).
                   mock.patch.object(lifecycle_cli, '_helper_choice', return_value={
                       'inUse': None, 'path': None, 'app': {'path': None, 'state': 'not_installed',
                                                            'reason': 'app_not_installed'}}),
                   mock.patch.object(lifecycle_cli, '_bindings_registry',
                                     side_effect=lambda: (self.registry(), AUDIENCE))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *argv) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return lifecycle_cli.main(list(argv))
            except SystemExit as exc:
                return 1 if exc.code not in (0, None) else 0

    def now(self) -> dict:
        return S.read_config(self.config)

    def test_every_change_needs_a_terminal(self):
        before = self.config.read_bytes()
        with mock.patch.object(lifecycle_cli, '_is_terminal', return_value=False):
            for argv in (('allow', ALPHA, '--tool', 'lifecycle.stop'), ('revoke', ALPHA), ('hold', 'alpha.app'),
                         ('policy', 'alpha.app', '--idle-minutes', '15'),
                         ('enroll', 'beta.app', '--kind', 'app', '--path', '/Applications/Beta.app',
                          '--caller', ALPHA, '--startup-effect', 'opens a window')):
                with self.subTest(verb=argv[0]):
                    self.assertEqual(self.cli(*argv), 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_allow_widens_one_caller_and_bumps_its_revision(self):
        self.assertEqual(self.cli('allow', ALPHA, '--tool', 'lifecycle.stop', '--tool', 'lifecycle.restart'), 0)
        g = self.now()['callers'][ALPHA]
        self.assertEqual((g['allowedTools'][-2:], g['revision']), (['lifecycle.stop', 'lifecycle.restart'], 2))
        self.assertEqual(self.cli('allow', ALPHA, '--tool', 'lifecycle.stop'), 0)
        self.assertEqual(self.now()['callers'][ALPHA]['revision'], 2, 'no change, no new revision')
        self.assertEqual(self.cli('allow', ALPHA, '--tool', 'shell.exec'), 1)
        self.assertEqual(self.cli('allow', ALPHA, '--tool', 'lifecycle.stop', '--remove'), 0)
        self.assertNotIn('lifecycle.stop', self.now()['callers'][ALPHA]['allowedTools'])
        self.assertEqual(self.cli('allow', BETA, '--tool', 'lifecycle.list'), 1,
                         'a new grant names the binding that proves its caller')
        self.assertEqual(self.cli('allow', BETA, '--binding', 'bnd_fixture0001', '--tool', 'lifecycle.list'), 1,
                         'a binding of another principal')
        self.assertEqual(self.cli('allow', BETA, '--binding', 'bnd_fixture0002', '--tool', 'lifecycle.list'), 0,
                         'a new caller gets a grant tied to its binding')
        g = self.now()['callers'][BETA]
        self.assertEqual((g['revision'], g['bindingId']), (1, 'bnd_fixture0002'))

    def test_allow_refuses_an_expired_binding(self):
        now = datetime.now(timezone.utc)
        self.bindings.append(binding(BETA, 3, token=bearer(BETA) + '-third') | {'issuedAt': stamp(now - timedelta(days=3)),
                                                 'expiresAt': stamp(now - timedelta(hours=1))})
        self.assertEqual(self.cli('allow', BETA, '--binding', 'bnd_fixture0003', '--tool', 'lifecycle.list'), 1,
                         'the runtime refuses an expired binding, so a grant naming it would open nothing')
        self.assertNotIn(BETA, self.now()['callers'])
        self.assertEqual(self.cli('allow', BETA, '--binding', 'bnd_fixture0002', '--tool', 'lifecycle.list'), 0)

    def test_allow_target_adds_an_enrolled_target_and_bumps_the_revision(self):
        self.assertEqual(self.cli('allow', ALPHA, '--target', 'alpha.app'), 0)
        g = self.now()['callers'][ALPHA]
        self.assertEqual((g['allowedTargets'], g['revision']), (['alpha.app'], 1), 'already granted')
        self.doc = self.now()
        self.doc['targets']['beta.app'] = lifecycle_target('beta.app')
        self.save()
        self.assertEqual(self.cli('allow', ALPHA, '--target', 'beta.app'), 0)
        g = self.now()['callers'][ALPHA]
        self.assertEqual((g['allowedTargets'], g['revision']), (['alpha.app', 'beta.app'], 2))
        self.assertEqual(g['allowedTools'], ['lifecycle.list'], 'tools untouched')

    def test_allow_target_refuses_an_unenrolled_target_but_removes_a_stale_one(self):
        self.assertEqual(self.cli('allow', ALPHA, '--target', 'alpha.typo'), 1, 'a grant to nothing is a typo')
        self.assertEqual(self.now()['callers'][ALPHA]['allowedTargets'], ['alpha.app'])
        self.doc = self.now()
        self.doc['callers'][ALPHA]['allowedTargets'].append('alpha.gone')
        self.save()
        self.assertEqual(self.cli('allow', ALPHA, '--target', 'alpha.gone', '--remove'), 0)
        self.assertEqual(self.now()['callers'][ALPHA]['allowedTargets'], ['alpha.app'])

    def test_allow_never_grants_the_wildcard_and_needs_something_to_change(self):
        self.assertEqual(self.cli('allow', ALPHA, '--target', '*'), 1)
        self.assertEqual(self.cli('allow', ALPHA), 1, 'neither --tool nor --target')
        self.assertEqual(self.now()['callers'][ALPHA]['allowedTargets'], ['alpha.app'])

    def test_s1_operator_proxy_is_set_and_cleared_by_allow(self):
        self.assertEqual(self.cli('allow', ALPHA, '--operator-proxy', 'on'), 0)
        g = self.now()['callers'][ALPHA]
        self.assertEqual((g['operatorProxy'], g['revision']), (True, 2))
        self.assertEqual(self.cli('allow', ALPHA, '--operator-proxy', 'off'), 0)
        self.assertNotIn('operatorProxy', self.now()['callers'][ALPHA])

    def test_s5_reserved_caller_names_cannot_be_granted(self):
        before = self.config.read_bytes()
        for name in ('owner', 'supervisor', 'fabric-host', 'agent:', 'agent:*'):
            with self.subTest(name=name):
                self.assertEqual(self.cli('allow', name, '--tool', 'lifecycle.list'), 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_revoke_refuses_the_caller_and_a_later_allow_is_a_fresh_grant(self):
        self.assertEqual(self.cli('revoke', ALPHA), 0)
        g = self.now()['callers'][ALPHA]
        self.assertEqual((g['revoked'], g['revision']), (True, 2))
        self.assertEqual(self.cli('revoke', BETA), 1, 'no grant to revoke')
        self.assertEqual(self.cli('allow', ALPHA, '--tool', 'lifecycle.status'), 0)
        g = self.now()['callers'][ALPHA]
        self.assertEqual((g['revoked'], g['revision']), (False, 3))

    def test_policy_sets_always_on_and_idle(self):
        self.assertEqual(self.cli('policy', 'alpha.app', '--idle-minutes', '15'), 0)
        t = self.now()['targets']['alpha.app']
        self.assertEqual((t['idleStopMinutes'], t['policyRevision']), (15, 2))
        self.assertEqual(self.cli('policy', 'alpha.app', '--desired', 'always_on'), 0)
        t = self.now()['targets']['alpha.app']
        self.assertEqual((t['desiredState'], 'idleStopMinutes' in t), ('always_on', False))
        self.assertEqual(self.cli('policy', 'alpha.app', '--idle-minutes', '15'), 1,
                         'idle on an always_on target is refused, nothing written')
        self.assertEqual(self.now()['targets']['alpha.app']['policyRevision'], 3)
        self.assertEqual(self.cli('policy', 'nope.app', '--idle-minutes', '15'), 1)
        self.assertEqual(self.cli('policy', 'alpha.app'), 1, 'nothing to change')

    def test_policy_switches_background_launch(self):
        self.assertEqual(self.cli('policy', 'alpha.app', '--background-launch', 'on'), 0)
        self.assertIs(self.now()['targets']['alpha.app']['backgroundLaunch'], True)
        self.assertEqual(self.cli('policy', 'alpha.app', '--background-launch', 'off'), 0)
        self.assertNotIn('backgroundLaunch', self.now()['targets']['alpha.app'])

    def test_policy_readiness_switches_in_place_and_bumps_the_revision(self):
        self.doc = self.now()
        self.doc['targets']['fabric.desktop'] = lifecycle_target('fabric.desktop', identity=FABRIC_IDENTITY,
                                                                 idleStopMinutes=15, policyRevision=11)
        self.save()
        before = self.now()['targets']['fabric.desktop']
        callers = self.now()['callers']
        self.assertEqual(self.cli('policy', 'fabric.desktop', '--readiness', 'fabric-hub'), 0)
        after = self.now()['targets']['fabric.desktop']
        self.assertEqual((after['readiness'], after['policyRevision']), ({'kind': 'fabric-hub'}, 12))
        for key in ('desiredState', 'idleStopMinutes', 'allowedCallers', 'identity', 'timeoutSeconds'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.now()['callers'], callers, 'grants untouched')
        self.assertEqual(self.cli('policy', 'fabric.desktop', '--readiness', 'process'), 0)
        self.assertEqual(self.now()['targets']['fabric.desktop']['readiness'], {'kind': 'process'})

    def test_fabric_hub_on_another_app_is_refused_and_nothing_changes(self):
        before = self.config.read_bytes()
        self.assertEqual(self.cli('policy', 'alpha.app', '--readiness', 'fabric-hub'), 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_hold_and_release_bump_the_revision_and_an_agent_gets_held_off(self):
        self.assertEqual(self.cli('hold', 'alpha.app'), 0)
        t = self.now()['targets']['alpha.app']
        self.assertEqual((t['desiredState'], t['policyRevision']), ('held_off', 2))
        self.doc = self.now()
        self.doc['callers'][ALPHA] = grant()
        self.save()
        service = self.service()
        try:
            alpha = service.authenticate('Bearer ' + bearer(ALPHA))
            status = self.call(service, alpha, 'lifecycle.status', {'targetId': 'alpha.app'})['structuredContent']
        finally:
            service.close()
        self.assertEqual(status['state'], 'held_off')
        self.assertEqual(self.adapter.effects, [])
        self.assertEqual(self.cli('hold', 'alpha.app', '--release'), 0)
        self.assertEqual(self.now()['targets']['alpha.app']['desiredState'], 'on_demand')

    def test_enroll_verifies_the_named_path_and_records_what_starting_it_does(self):
        fake = mock.Mock()
        fake.fingerprint.return_value = app_identity('com.example.beta')
        with mock.patch.object(lifecycle_cli, '_adapter', return_value=fake):
            self.assertEqual(self.cli('enroll', 'beta.app', '--kind', 'app', '--path', 'Beta.app', '--caller', ALPHA,
                                      '--startup-effect', 'opens a window'), 1, 'a relative path is refused')
            fake.fingerprint.assert_not_called()
            self.assertEqual(self.cli('enroll', 'beta.app', '--kind', 'app', '--path', '/Applications/Beta.app',
                                      '--caller', ALPHA, '--startup-effect', 'opens a window'), 0)
        fake.fingerprint.assert_called_once_with('app', '/Applications/Beta.app')
        t = self.now()['targets']['beta.app']
        self.assertEqual((t['identity']['bundleId'], t['startupEffects'], t['allowedCallers'], t['desiredState']),
                         ('com.example.beta', ['opens a window'], [ALPHA], 'on_demand'))
        refuse = mock.Mock()
        refuse.fingerprint.side_effect = broker.LifecycleError('identity_mismatch')
        before = self.config.read_bytes()
        with mock.patch.object(lifecycle_cli, '_adapter', return_value=refuse):
            self.assertEqual(self.cli('enroll', 'gamma.app', '--kind', 'app', '--path', '/Applications/Gamma.app',
                                      '--caller', ALPHA, '--startup-effect', 'opens a window'), 1)
        self.assertEqual(self.config.read_bytes(), before, 'a bundle that fails its signature is not enrolled')

    def test_a_re_enrolled_id_never_reuses_a_policy_revision(self):
        # Adapted: a policy revision names one decision; enrolling the same id again moves past every revision it
        # had. (Across an unenroll, the source's retired revisions, comes with the unenroll verb.)
        self.doc = self.now()
        self.doc['targets']['alpha.app']['policyRevision'] = 4
        self.save()
        fake = mock.Mock()
        fake.fingerprint.return_value = app_identity()
        with mock.patch.object(lifecycle_cli, '_adapter', return_value=fake):
            self.assertEqual(self.cli('enroll', 'alpha.app', '--kind', 'app', '--path', '/Applications/Alpha.app',
                                      '--caller', ALPHA, '--startup-effect', 'opens a window'), 0)
        self.assertEqual(self.now()['targets']['alpha.app']['policyRevision'], 5)

    def test_status_reads_and_writes_nothing(self):
        self.bindings = [binding(ALPHA, 1)]
        with mock.patch.object(lifecycle_cli, '_adapter', return_value=self.adapter):
            doc = lifecycle_cli.status()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lifecycle_cli.main(['status']), 0)
        self.assertEqual([t['targetId'] for t in doc['targets']], ['alpha.app'])
        self.assertEqual(doc['targets'][0]['observed']['state'], 'stopped')
        self.assertEqual((doc['callers'][0]['binding'], doc['callers'][0]['bindingState']),
                         ('bnd_fixture0001', 'in force'))
        self.assertIn('agent:alpha rev 1 · binding bnd_fixture0001 in force', out.getvalue())
        self.assertFalse(self.state.exists(), 'status creates no state')
        self.assertEqual(self.adapter.effects, [])

    def test_status_names_a_helper_built_from_an_older_source(self):
        # OBS-222: the helper line says why apps answer setup_required, and names the command that repairs it.
        for state, line in (('outdated', 'built from an older native_app.swift'), ('not_built', 'is not built'),
                            ('unsafe', 'others may change'), ('source_missing', 'native_app.swift is missing'),
                            ('current', None)):
            with mock.patch.object(macos, 'helper_state', return_value=state), \
                    mock.patch.object(lifecycle_cli.sys, 'platform', 'darwin'):
                doc = lifecycle_cli.status(probe=False)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(lifecycle_cli.main(['status', '--no-probe']), 0)
            self.assertEqual(doc['helper']['state'], state)
            self.assertEqual(doc['helper']['built'], state not in ('not_built', 'unsafe'))
            self.assertEqual(doc['helper']['expectedVersion'], macos.expected_helper_version())
            if line:
                self.assertIn(line, out.getvalue())
                self.assertIn('full lifecycle build-helper', out.getvalue())
            else:
                self.assertNotIn('build-helper', out.getvalue())

    def test_build_helper_is_the_adapters_own_build(self):
        with mock.patch.object(macos, 'main', return_value=0) as build:
            self.assertEqual(self.cli('build-helper', '--dest', str(self.data / 'bin')), 0)
        build.assert_called_once_with(['build-helper', '--dest', str(self.data / 'bin')])


class DispatcherTests(unittest.TestCase):
    def test_the_full_cli_hands_lifecycle_to_its_parser_and_describes_it(self):
        import observatory as cli
        self.assertIn('lifecycle', cli.OWN_PARSER_COMMANDS)
        self.assertIn('  lifecycle status|enroll|allow|policy|hold|revoke|import|build-helper', cli.PUBLIC_HELP)
        self.assertEqual(cli.refusal(['lifecycle', 'status']), '')
        self.assertNotEqual(cli.refusal(['lifecycle', 'launch']), '')


if __name__ == '__main__':
    unittest.main()
