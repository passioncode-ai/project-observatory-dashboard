#!/usr/bin/env python3
"""The lifecycle broker (OBS-79 step L1, docs/design/LIFECYCLE.md) against fake adapters.

A file seam stands in for the operating system: `<id>.pid` is a running process, `<id>.windows` a window on
screen, `<id>.active` a frontmost app, `<id>.idle` the seconds since the person last touched input,
`<id>.stubborn` a process that ignores every graceful stop, `<id>.ignores_quit` one that honours only a signal,
`<id>.unready` one that runs but never serves. No installed application is touched, no process is signalled.

The fixtures are neutral: callers `owner`, `caller-a`, `caller-b`; targets `alpha.app`, `beta.app`; signing team
`ABCDE12345`; every file in a temporary folder.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

import osprivacy                                       # noqa: E402
from node.lifecycle import broker                      # noqa: E402

TEAM = 'ABCDE12345'


def app_identity(bundle_id='com.example.alpha', team=TEAM):
    return {
        'scheme': broker.IDENTITY_SCHEME,
        'bundleId': bundle_id,
        'teamId': team,
        'requirement': f'identifier "{bundle_id}" and anchor apple generic and certificate leaf[subject.OU] = "{team}"',
    }


SERVICE_READINESS = {
    'kind': 'fabric-service',
    'origin': 'http://127.0.0.1:47999',
    'serviceId': 'alpha-service',
    'instance': 'default',
    'buildDigest': 'sha256:' + '0' * 64,
}


def write_private(path: Path, doc) -> None:
    """The registry and the authority are owner-only documents; the broker refuses anything else."""
    path = Path(path)
    temp = path.with_name('.' + path.name + '.tmp')
    temp.write_text(json.dumps(doc))
    osprivacy.make_private(temp)
    os.replace(temp, path)


def credential(name):
    """A caller's bearer credential in the fixtures: its name padded to a credential's length, composed rather than
    written out, so the repository's secret scan has no key-shaped literal to argue about."""
    return name + '-' + 'x' * 32


def grant(token, *, owner=False, targets=('alpha.app', 'beta.app'), tools=broker.TOOLS):
    return {
        'tokenHash': hashlib.sha256(token.encode()).hexdigest(),
        'revision': 1,
        'revoked': False,
        'owner': owner,
        'allowedTargets': list(targets),
        'allowedTools': list(tools),
    }


class Clock:
    """Real time plus a jump the test controls: waits inside the broker still elapse."""

    def __init__(self):
        self.offset = 0.0

    @property
    def now(self):
        return time.time() + self.offset

    @now.setter
    def now(self, value):
        self.offset = value - time.time()

    def __call__(self):
        return self.now


class FileAdapter:
    """A start writes `<id>.ready` and appends the target to `effects.log`, fsynced: a durable record of every
    effect, which a crashed process cannot take back."""

    def __init__(self, root, delay=0):
        self.root = Path(root)
        self.delay = delay
        self.identity = True
        self.before_start = None

    def identity_matches(self, target, *, deadline=None):
        return self.identity

    def probe(self, target):
        ready = (self.root / (target['targetId'] + '.ready')).exists()
        return {
            'state': 'ready' if ready else 'stopped',
            'processReady': ready,
            'transportReady': False,
            'capabilityReady': ready,
            'authState': 'not_applicable',
            'observedAt': int(time.time() * 1000),
        }

    def start(self, target):
        if self.before_start:
            self.before_start()
        with (self.root / 'effects.log').open('a') as stream:
            stream.write(target['targetId'] + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        time.sleep(self.delay)
        (self.root / (target['targetId'] + '.ready')).write_text('ready')
        return 'disposable_file_effect'

    def stop(self, target, pid, *, force=False, mode='quit'):
        raise broker.LifecycleError('identity_mismatch')


class ProcessAdapter:
    def __init__(self, root):
        self.root = Path(root)
        self.next_pid = 1000
        self.effects = []
        self.probes = 0

    def _f(self, target_id, suffix):
        return self.root / f'{target_id}.{suffix}'

    def run_as_person(self, target_id, windows=0):
        """A process the person started: the broker has no launch record for it."""
        self.next_pid += 1
        self._f(target_id, 'pid').write_text(str(self.next_pid))
        if windows:
            self._f(target_id, 'windows').write_text(str(windows))
        return self.next_pid

    def crash(self, target_id):
        self._f(target_id, 'pid').unlink(missing_ok=True)

    def identity_matches(self, target, *, deadline=None):
        return True

    def probe(self, target):
        self.probes += 1
        pid_file = self._f(target['targetId'], 'pid')
        if not pid_file.exists():
            return {'state': 'stopped', 'processReady': False, 'observedAt': 0}
        windows = self._f(target['targetId'], 'windows')
        idle = self._f(target['targetId'], 'idle')
        return {
            'state': 'ready',
            'processReady': True,
            'capabilityReady': True,
            'pid': int(pid_file.read_text()),
            'windows': int(windows.read_text()) if windows.exists() else 0,
            'active': self._f(target['targetId'], 'active').exists(),
            'inputIdleSeconds': int(idle.read_text()) if idle.exists() else 3600,
            'observedAt': 0,
        }

    def start(self, target):
        self.effects.append(('start', target['targetId']))
        self.next_pid += 1
        self._f(target['targetId'], 'pid').write_text(str(self.next_pid))
        return 'launch_services_open'

    def stop(self, target, pid, *, force=False, mode='quit'):
        pid_file = self._f(target['targetId'], 'pid')
        if not pid_file.exists() or int(pid_file.read_text()) != pid:
            raise broker.LifecycleError('identity_mismatch')
        step = 'kill' if force else mode
        self.effects.append((step, target['targetId']))
        # `.ignores_quit`: some apps honour only one of the two graceful stops; `.stubborn` ignores both.
        honours = not self._f(target['targetId'], 'stubborn').exists() and not (
            step == 'quit' and self._f(target['targetId'], 'ignores_quit').exists()
        )
        if force or honours:
            pid_file.unlink()
            self._f(target['targetId'], 'windows').unlink(missing_ok=True)
        return {'kill': 'sigkill', 'quit': 'quit_requested', 'term': 'sigterm'}[step]


def target(key='alpha.app', desired='on_demand', idle=None, timeout=2, deps=None,
           callers=('owner', 'caller-a', 'caller-b')):
    t = {
        'targetId': key,
        'kind': 'app',
        'path': '/Applications/Alpha.app',
        'identity': app_identity(),
        'policyRevision': 1,
        'desiredState': desired,
        'dependencies': list(deps or []),
        'readiness': {'kind': 'process'},
        'timeoutSeconds': timeout,
        'startupEffects': ['a disposable effect only'],
        'approved': True,
        'allowedCallers': list(callers),
    }
    if idle is not None:
        t['idleStopMinutes'] = idle
    return t


class Folder:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()


# -- durable operations, receipts, the effect journal, authority (file seam) --------------------------------------

class BrokerTests(Folder, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.token = credential('owner')
        self.peer = credential('caller-b')
        self.grants = {'schemaVersion': 1, 'callers': {
            'owner': grant(self.token, owner=True),
            'caller-b': grant(self.peer),
        }}
        self.save_grants()
        self.policy = {'schemaVersion': 1, 'targets': {'alpha.app': target()}}
        self.save_policy()
        self.authority = broker.Authority(self.data / 'callers.json')
        self.principal = self.authority.authenticate('Bearer ' + self.token)
        self.adapter = FileAdapter(self.data, 0.03)
        self.instance = broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, self.adapter)

    def tearDown(self):
        self.instance.close()
        super().tearDown()

    def save_grants(self):
        write_private(self.data / 'callers.json', self.grants)

    def save_policy(self):
        write_private(self.data / 'registry.json', self.policy)

    def args(self, request='r1', target_id='alpha.app'):
        return {'targetId': target_id, 'requestId': request, 'grantRevision': 1, 'expectedPolicyRevision': 1}

    def wait(self, op, principal=None):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            receipt = self.instance.get(principal or self.principal, op['operationId'])
            if receipt['state'] != 'running':
                return receipt
            time.sleep(0.01)
        self.fail('operation did not terminate')

    def effects(self):
        log = self.data / 'effects.log'
        return log.read_text().splitlines() if log.exists() else []

    def test_ten_concurrent_callers_start_once_and_receive_own_receipts(self):
        principals = []
        for i in range(10):
            name = f'caller-{i}'
            token = name + 'x' * 32
            self.grants['callers'][name] = grant(token, targets=('alpha.app',))
            self.policy['targets']['alpha.app']['allowedCallers'].append(name)
            principals.append((name, token))
        self.save_grants()
        self.save_policy()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            calls = list(pool.map(lambda pair: (self.authority.authenticate('Bearer ' + pair[1]), pair[0]), principals))
            ops = list(pool.map(lambda pair: self.instance.ensure(pair[0], self.args(pair[1])), calls))
        for principal, op in zip(calls, ops, strict=True):
            self.assertIn(self.wait(op, principal[0])['state'], ('ready', 'already_ready'))
        self.assertEqual(self.effects(), ['alpha.app'])
        self.assertEqual(len({op['operationId'] for op in ops}), 10)
        with self.assertRaisesRegex(broker.LifecycleError, 'not_found'):
            self.instance.get(calls[1][0], ops[0]['operationId'])

    def test_duplicate_request_survives_restart(self):
        first = self.instance.ensure(self.principal, self.args())
        self.assertEqual(self.wait(first)['state'], 'ready')
        self.instance.close()
        self.instance = broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, self.adapter)
        second = self.instance.ensure(self.principal, self.args())
        self.assertEqual(first['operationId'], second['operationId'])
        self.assertEqual(self.effects(), ['alpha.app'])

    def test_completed_receipt_replays_after_original_deadline(self):
        args = self.args() | {'deadlineAt': int((time.time() + 0.2) * 1000)}
        first = self.wait(self.instance.ensure(self.principal, args))
        time.sleep(0.25)
        second = self.instance.ensure(self.principal, args)
        self.assertEqual(first['operationId'], second['operationId'])
        self.assertEqual(second['state'], 'ready')
        self.assertEqual(self.effects(), ['alpha.app'])

    def test_idempotency_payload_conflict(self):
        self.wait(self.instance.ensure(self.principal, self.args()))
        with self.assertRaisesRegex(broker.LifecycleError, 'idempotency_conflict'):
            self.instance.ensure(self.principal, self.args() | {'requiredCapability': 'process'})

    def test_held_off_never_starts(self):
        self.policy['targets']['alpha.app']['desiredState'] = 'held_off'
        self.save_policy()
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'held_off')
        self.assertEqual(self.effects(), [])

    def test_stale_policy_and_grant_revision_fail_before_admission(self):
        for args in (self.args() | {'grantRevision': 2}, self.args() | {'expectedPolicyRevision': 2}):
            with self.assertRaises(broker.LifecycleError):
                self.instance.ensure(self.principal, args)
        self.assertEqual(self.effects(), [])

    def test_revoke_between_admission_and_effect(self):
        entered, resume = threading.Event(), threading.Event()
        original = self.adapter.identity_matches

        def check(target, *, deadline=None):
            entered.set()
            resume.wait(2)
            return original(target, deadline=deadline)

        self.adapter.identity_matches = check
        op = self.instance.ensure(self.principal, self.args())
        self.assertTrue(entered.wait(2))
        self.grants['callers']['owner']['revoked'] = True
        self.grants['callers']['owner']['revision'] = 2
        self.save_grants()
        resume.set()
        self.instance.executor.shutdown(wait=True)
        self.assertEqual(self.effects(), [])
        row = self.instance.db.execute('SELECT state FROM operations WHERE id=?', (op['operationId'],)).fetchone()
        self.assertEqual(row[0], 'denied')

    def test_registry_substitution_before_effect_is_denied(self):
        count = 0

        def check(target, *, deadline=None):
            nonlocal count
            count += 1
            if count == 1:
                self.policy['targets']['alpha.app']['path'] = '/Applications/Other.app'
                self.save_policy()
            return True

        self.adapter.identity_matches = check
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'policy_changed')
        self.assertEqual(self.effects(), [])

    def test_wrong_identity_never_starts(self):
        self.adapter.identity = False
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'identity_mismatch')
        self.assertEqual(self.effects(), [])

    def test_closed_request_rejects_shell_and_malformed_deadline(self):
        for changes in ({'argv': ['echo', 'bad']}, {'deadlineAt': False}, {'targetId': '../bad'},
                        {'deadlineAt': float('nan')}, {'requiredCapability': ['process']}):
            with self.assertRaises(broker.LifecycleError, msg=repr(changes)):
                self.instance.ensure(self.principal, self.args() | changes)
        self.assertEqual(self.effects(), [])

    def test_unscoped_peer_denied(self):
        self.grants['callers']['caller-b']['allowedTargets'] = []
        self.save_grants()
        peer = self.authority.authenticate('Bearer ' + self.peer)
        with self.assertRaisesRegex(broker.LifecycleError, 'denied'):
            self.instance.ensure(peer, self.args())
        self.assertEqual(self.effects(), [])

    def test_dead_dependency_and_dag_order(self):
        self.policy['targets']['beta.app'] = target('beta.app')
        self.policy['targets']['alpha.app']['dependencies'] = ['beta.app']
        self.save_policy()
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'ready')
        self.assertEqual(self.effects(), ['beta.app', 'alpha.app'])

    def test_dependency_hold_does_not_start_parent(self):
        self.policy['targets']['beta.app'] = target('beta.app', desired='held_off')
        self.policy['targets']['alpha.app']['dependencies'] = ['beta.app']
        self.save_policy()
        self.assertIn(self.wait(self.instance.ensure(self.principal, self.args()))['state'],
                      ('held_off', 'dependency_failed'))
        self.assertEqual(self.effects(), [])

    def test_cycle_rejected(self):
        self.policy['targets']['alpha.app']['dependencies'] = ['alpha.app']
        self.save_policy()
        with self.assertRaisesRegex(broker.LifecycleError, 'invalid_dependencies'):
            self.instance.ensure(self.principal, self.args())

    def test_crash_marker_never_relaunches_and_adopts_late_readiness(self):
        with self.instance.db:
            self.instance.db.execute('INSERT INTO effects VALUES(?,?,?,?)',
                                     ('alpha.app', 'dispatching', time.time(), 'prior-dead-worker'))
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'delivery_unknown')
        self.assertEqual(self.effects(), [])
        (self.data / 'alpha.app.ready').write_text('late')
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args('r2')))['state'], 'already_ready')
        self.assertEqual(self.effects(), [])

    def test_generation_fence_rejects_late_worker(self):
        with self.instance.db:
            self.instance.db.execute(
                'INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('old', 'alpha.app', 'owner', '{}', json.dumps(self.args()), '{}', 'running', 2,
                 time.time(), time.time(), None),
            )
        self.instance._finish('old', 1, {'state': 'ready'})
        self.assertEqual(self.instance.db.execute("SELECT state FROM operations WHERE id='old'").fetchone()[0], 'running')
        self.instance._finish('old', 2, {'state': 'failed'})
        self.assertEqual(self.instance.db.execute("SELECT state FROM operations WHERE id='old'").fetchone()[0], 'failed')

    def test_auth_failure_is_not_restart(self):
        self.adapter.probe = lambda target: self.instance._observation('auth_required', processReady=True)
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'auth_required')
        self.assertEqual(self.effects(), [])

    def test_circuit_persists_across_restart(self):
        with self.instance.db:
            self.instance.db.executemany('INSERT INTO attempts VALUES(?,?)', [('alpha.app', time.time())] * 2)
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args()))['state'], 'circuit_open')
        self.instance.close()
        self.instance = broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, self.adapter)
        self.assertEqual(self.wait(self.instance.ensure(self.principal, self.args('r2')))['state'], 'circuit_open')
        self.assertEqual(self.effects(), [])

    def test_private_authority_and_no_symlink(self):
        p = self.data / 'callers.json'
        p.chmod(0o644)
        with self.assertRaisesRegex(broker.LifecycleError, 'unsafe_config'):
            self.authority.authenticate('Bearer ' + self.token)
        p.chmod(0o600)
        copy = self.data / 'copy.json'
        p.rename(copy)
        p.symlink_to(copy)
        with self.assertRaisesRegex(broker.LifecycleError, 'unsafe_config'):
            self.authority.authenticate('Bearer ' + self.token)

    # Added here: the store's own boundary and the caller name the broker keeps for itself.

    def test_the_store_is_private_and_lives_where_the_engine_keeps_state(self):
        self.assertTrue(osprivacy.private(self.data / 'state'))
        self.assertTrue(osprivacy.private(self.data / 'state' / broker.STORE_FILE))
        import paths
        self.assertEqual(broker.default_state_dir(), Path(paths.STATE) / 'node-lifecycle')

    def test_a_store_written_by_newer_code_is_refused(self):
        self.instance.close()
        with contextlib.closing(sqlite3.connect(self.data / 'state' / broker.STORE_FILE)) as db, db:
            db.execute('UPDATE meta SET version=2')
        with self.assertRaisesRegex(broker.LifecycleError, 'store_newer_than_code'):
            broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, self.adapter)
        with contextlib.closing(sqlite3.connect(self.data / 'state' / broker.STORE_FILE)) as db, db:
            db.execute('UPDATE meta SET version=1')
        self.instance = broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, self.adapter)

    def test_the_supervisors_name_cannot_be_a_callers(self):
        self.grants['callers']['supervisor'] = grant(credential('supervisor'))
        self.save_grants()
        with self.assertRaisesRegex(broker.LifecycleError, 'invalid_authority'):
            self.authority.authenticate('Bearer ' + credential('supervisor'))

    def test_a_revoked_or_unmarked_grant_does_not_authenticate(self):
        del self.grants['callers']['caller-b']['revoked']
        self.save_grants()
        with self.assertRaisesRegex(broker.LifecycleError, 'unauthorized'):
            self.authority.authenticate('Bearer ' + self.peer)
        for header in (None, 'Basic x', 'Bearer ' + 'x' * 600, 'Bearer wrong-token'):
            with self.assertRaisesRegex(broker.LifecycleError, 'unauthorized'):
                self.authority.authenticate(header)

    def test_a_malformed_trace_context_is_dropped_and_a_valid_one_kept(self):
        trace = '00-' + 'a' * 32 + '-' + 'b' * 16 + '-01'
        kept = self.instance.ensure(self.principal, self.args('t1'), traceparent=trace)
        self.assertEqual(kept['traceparent'], trace)
        dropped = self.instance.ensure(self.principal, self.args('t2'), traceparent='x\n' * 100)
        self.assertIsNone(dropped['traceparent'])
        self.wait(kept)
        self.wait(dropped)


# -- the closed registry --------------------------------------------------------------------------------------

class RegistryTests(Folder, unittest.TestCase):
    def load(self, *targets):
        write_private(self.data / 'registry.json',
                      {'schemaVersion': 1, 'targets': {t['targetId']: t for t in targets}})
        return broker.registry(self.data / 'registry.json')

    def refused(self, t, code='invalid_registry'):
        with self.assertRaisesRegex(broker.LifecycleError, code, msg=repr(t)):
            self.load(t)

    def test_registry_rejects_malformed_new_identity(self):
        self.refused(target() | {'identity': app_identity() | {'teamId': f'{TEAM}" or true'}})

    def test_registry_accepts_only_a_bool_on_an_app(self):
        for value, ok in ((True, True), (False, True), ('yes', False), (1, False)):
            t = target() | {'backgroundLaunch': value}
            if ok:
                self.load(t)
            else:
                self.refused(t)

    # Added here: what the engine's registry refuses beyond the ported cases.

    def test_a_valid_registry_loads_and_a_missing_one_is_empty(self):
        self.assertEqual(broker.registry(self.data / 'absent.json'), {'schemaVersion': 1, 'targets': {}})
        launchd = target('beta.job') | {
            'kind': 'launchd',
            'path': '/Library/LaunchAgents/example.beta.plist',
            'identity': {'label': 'example.beta', 'plistHash': 'a' * 64, 'artifacts': {'/opt/example/run': 'b' * 64}},
        }
        doc = self.load(target(), launchd, target('gamma.app') | {'readiness': dict(SERVICE_READINESS)})
        self.assertEqual(sorted(doc['targets']), ['alpha.app', 'beta.job', 'gamma.app'])

    def test_an_app_identity_must_be_the_requirement_scheme(self):
        for identity in ({'fixture': True}, app_identity() | {'scheme': 'cdhash/1'}, app_identity() | {'extra': 1},
                         app_identity(team='abcde12345'), app_identity(bundle_id='-bad'), app_identity() | {'requirement': ''}):
            self.refused(target() | {'identity': identity})

    def test_unknown_fields_kinds_and_readiness_are_refused(self):
        self.refused(target() | {'command': ['/bin/sh']})
        self.refused(target() | {'kind': 'script'})
        self.refused(target() | {'path': 'Applications/Alpha.app'})
        self.refused(target() | {'approved': 'yes'})
        self.refused(target() | {'timeoutSeconds': 61})
        self.refused(target() | {'startupEffects': []}, 'startup_effects_required')
        # The product readiness profiles come with their probes in step L2.
        self.refused(target() | {'readiness': {'kind': 'fabric-hub'}})
        self.refused(target() | {'readiness': {'kind': 'process', 'origin': 'http://127.0.0.1:1'}})
        for origin in ('http://localhost:47999', 'https://127.0.0.1:47999', 'http://127.0.0.1:47999/path',
                       'http://user@127.0.0.1:47999', 'http://127.0.0.1'):
            self.refused(target() | {'readiness': SERVICE_READINESS | {'origin': origin}})

    def test_a_launchd_identity_is_closed(self):
        base = {'label': 'example.beta', 'plistHash': 'a' * 64, 'artifacts': {'/opt/example/run': 'b' * 64}}
        for identity in (base | {'label': 'x'}, base | {'plistHash': 'nothex'}, base | {'artifacts': {}},
                         base | {'artifacts': {'relative/run': 'b' * 64}}, base | {'extra': True}):
            self.refused(target('beta.job') | {'kind': 'launchd', 'identity': identity})
        self.refused(target('beta.job') | {'kind': 'launchd', 'identity': base, 'backgroundLaunch': True})

    def test_a_registry_is_a_private_regular_file(self):
        self.load(target())
        (self.data / 'registry.json').chmod(0o644)
        with self.assertRaisesRegex(broker.LifecycleError, 'unsafe_config'):
            broker.registry(self.data / 'registry.json')
        (self.data / 'garbled.json').write_text('{')
        osprivacy.make_private(self.data / 'garbled.json')
        with self.assertRaisesRegex(broker.LifecycleError, 'invalid_config'):
            broker.registry(self.data / 'garbled.json')


# -- stop, restart, always_on, idle (process seam) ---------------------------------------------------------------

class BrokerFixture(Folder):
    def setUp(self):
        super().setUp()
        self.tokens = {name: credential(name) for name in ('owner', 'caller-a')}
        self.grants = {'schemaVersion': 1, 'callers': {
            name: grant(token, owner=name == 'owner') for name, token in self.tokens.items()
        }}
        write_private(self.data / 'callers.json', self.grants)
        self.policy = {'schemaVersion': 1, 'targets': {'alpha.app': target()}}
        self.save_policy()
        self.authority = broker.Authority(self.data / 'callers.json')
        self.owner = self.authority.authenticate('Bearer ' + self.tokens['owner'])
        self.agent = self.authority.authenticate('Bearer ' + self.tokens['caller-a'])
        self.adapter = ProcessAdapter(self.data)
        self.clock = Clock()
        self.finished = []
        self.broker = self.make_broker()

    def make_broker(self, adapter=None, **hooks):
        return broker.Broker(
            self.data / 'registry.json',
            self.data / 'state',
            self.authority,
            adapter or self.adapter,
            clock=self.clock,
            on_finish=lambda r, t, c: self.finished.append((r['state'], c)),
            **hooks,
        )

    def tearDown(self):
        self.broker.close()
        super().tearDown()

    def save_policy(self):
        write_private(self.data / 'registry.json', self.policy)

    def args(self, request, target_id='alpha.app', revision=1, **extra):
        return {'targetId': target_id, 'requestId': request, 'grantRevision': 1, 'expectedPolicyRevision': revision} | extra

    def wait(self, receipt, principal):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            got = self.broker.get(principal, receipt['operationId'])
            if got['state'] != 'running':
                return got
            time.sleep(0.01)
        self.fail('operation did not finish')

    def wait_internal(self, op_id):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            got = self.broker.get(broker.SUPERVISOR, op_id)
            if got['state'] != 'running':
                return got
            time.sleep(0.01)
        self.fail('internal operation did not finish')


class ControlTests(BrokerFixture, unittest.TestCase):
    def start_by(self, principal, request='start-1'):
        return self.wait(self.broker.ensure(principal, self.args(request)), principal)

    # -- stop

    def test_agent_stops_what_the_broker_started(self):
        self.assertEqual(self.start_by(self.agent)['state'], 'ready')
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['action'], receipt['kind']), ('stopped', 'quit_requested', 'stop'))
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app'), ('quit', 'alpha.app')])

    def test_sigterm_follows_an_ignored_quit(self):
        self.start_by(self.agent)
        (self.data / 'alpha.app.ignores_quit').write_text('')
        with mock.patch.object(broker, 'QUIT_GRACE_SECONDS', 0.2):
            receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['action']), ('stopped', 'sigterm'))
        self.assertEqual(self.adapter.effects[-2:], [('quit', 'alpha.app'), ('term', 'alpha.app')])

    def test_agent_cannot_stop_an_app_the_person_opened(self):
        self.adapter.run_as_person('alpha.app')
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['reason']), ('in_use_by_person', 'not_started_by_broker'))
        self.assertEqual(self.adapter.effects, [])

    def test_a_window_alone_does_not_make_an_app_the_persons(self):
        # An app opened without activation may still show a window and come forward by itself.
        self.start_by(self.agent)
        (self.data / 'alpha.app.windows').write_text('1')
        (self.data / 'alpha.app.active').write_text('')     # frontmost, but nobody touched input for an hour
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual(receipt['state'], 'stopped')

    def test_agent_cannot_stop_an_app_the_person_is_in_front_of(self):
        self.start_by(self.agent)
        (self.data / 'alpha.app.active').write_text('')
        (self.data / 'alpha.app.idle').write_text('10')
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['reason']), ('in_use_by_person', 'frontmost'))
        self.assertNotIn(('quit', 'alpha.app'), self.adapter.effects)

    def test_an_app_the_person_brought_forward_after_launch_is_theirs(self):
        self.start_by(self.agent)
        pid = self.broker.status(self.agent, 'alpha.app')['pid']
        self.broker.note_activation({'alpha.app'}, pid)    # inside the launch grace: the app itself
        self.assertEqual(self.broker.status(self.agent, 'alpha.app')['startedBy'], 'broker')
        self.clock.now += broker.ADOPT_GRACE_SECONDS + 1
        self.broker.note_activation({'alpha.app'}, pid + 1)  # another process: ignored
        self.assertEqual(self.broker.status(self.agent, 'alpha.app')['startedBy'], 'broker')
        self.broker.note_activation({'alpha.app'}, pid)
        self.assertEqual(self.broker.status(self.agent, 'alpha.app')['startedBy'], 'person')
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['reason']), ('in_use_by_person', 'taken_over_by_person'))

    def test_owner_stops_anything(self):
        self.adapter.run_as_person('alpha.app', windows=2)
        receipt = self.wait(self.broker.stop(self.owner, self.args('stop-1')), self.owner)
        self.assertEqual(receipt['state'], 'stopped')

    def test_already_stopped_is_a_success_without_effect(self):
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual((receipt['state'], self.adapter.effects), ('already_stopped', []))

    def test_graceful_timeout_then_only_the_owner_may_force(self):
        self.start_by(self.agent)
        (self.data / 'alpha.app.stubborn').write_text('')
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual(receipt['state'], 'stop_timeout')
        with self.assertRaises(broker.LifecycleError) as denied:
            self.broker.stop(self.agent, self.args('stop-2', force=True))
        self.assertEqual(denied.exception.code, 'denied')
        policy = self.policy['targets']['alpha.app']
        policy['timeoutSeconds'] = 3
        policy['policyRevision'] = 2
        self.save_policy()
        receipt = self.wait(self.broker.stop(self.owner, self.args('stop-3', revision=2, force=True)), self.owner)
        self.assertEqual((receipt['state'], receipt['action']), ('stopped', 'sigkill'))
        self.assertEqual(self.adapter.effects[-3:], [('quit', 'alpha.app'), ('term', 'alpha.app'), ('kill', 'alpha.app')])

    def test_stop_needs_its_own_grant(self):
        self.grants['callers']['caller-a']['allowedTools'] = ['lifecycle.list', 'lifecycle.ensure_running']
        write_private(self.data / 'callers.json', self.grants)
        with self.assertRaises(broker.LifecycleError) as denied:
            self.broker.stop(self.agent, self.args('stop-1'))
        self.assertEqual(denied.exception.code, 'denied')

    def test_one_request_id_never_means_a_start_and_a_stop(self):
        self.start_by(self.agent, 'same-id')
        with self.assertRaises(broker.LifecycleError) as conflict:
            self.broker.stop(self.agent, self.args('same-id'))
        self.assertEqual(conflict.exception.code, 'idempotency_conflict')
        first = self.broker.stop(self.agent, self.args('stop-1'))
        self.assertEqual(self.broker.stop(self.agent, self.args('stop-1'))['operationId'], first['operationId'])
        self.wait(first, self.agent)

    def test_a_held_off_target_can_still_be_stopped(self):
        self.start_by(self.agent)
        self.policy['targets']['alpha.app']['desiredState'] = 'held_off'
        self.policy['targets']['alpha.app']['policyRevision'] = 2
        self.save_policy()
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1', revision=2)), self.agent)
        self.assertEqual(receipt['state'], 'stopped')

    def test_a_replaced_process_is_not_the_brokers_any_more(self):
        self.start_by(self.agent)
        self.adapter.crash('alpha.app')
        self.adapter.run_as_person('alpha.app')            # the person relaunched it
        self.assertEqual(self.broker.status(self.agent, 'alpha.app')['startedBy'], 'other')
        receipt = self.wait(self.broker.stop(self.agent, self.args('stop-1')), self.agent)
        self.assertEqual(receipt['state'], 'in_use_by_person')

    # -- restart

    def test_restart_stops_then_starts_once(self):
        self.start_by(self.agent)
        receipt = self.wait(self.broker.restart(self.agent, self.args('restart-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['stopState'], receipt['kind']), ('ready', 'stopped', 'restart'))
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app'), ('quit', 'alpha.app'), ('start', 'alpha.app')])
        self.assertEqual(self.broker.status(self.agent, 'alpha.app')['startedBy'], 'broker')

    def test_restart_of_a_stopped_app_just_starts_it(self):
        receipt = self.wait(self.broker.restart(self.agent, self.args('restart-1')), self.agent)
        self.assertEqual((receipt['state'], receipt['stopState']), ('ready', 'already_stopped'))

    def test_restart_obeys_the_same_person_rule(self):
        self.adapter.run_as_person('alpha.app')
        receipt = self.wait(self.broker.restart(self.agent, self.args('restart-1')), self.agent)
        self.assertEqual(receipt['state'], 'in_use_by_person')
        self.assertEqual(self.adapter.effects, [])

    # -- always_on

    def always_on(self):
        self.policy['targets']['alpha.app'] = target(desired='always_on')
        self.save_policy()

    def test_always_on_is_started_by_the_supervisor_and_again_after_a_crash(self):
        self.always_on()
        [op] = self.broker.supervise()
        self.assertEqual(self.wait_internal(op)['state'], 'ready')
        self.assertEqual(self.finished[-1][1]['reason'], 'always_on')
        self.adapter.crash('alpha.app')
        [op] = self.broker.supervise()
        self.assertEqual(self.wait_internal(op)['state'], 'ready')
        self.assertEqual(self.broker.supervise(), [], 'a running target is left alone')

    def test_a_crash_loop_is_bounded_by_the_circuit(self):
        self.always_on()
        for _ in range(2):
            [op] = self.broker.supervise()
            self.wait_internal(op)
            self.adapter.crash('alpha.app')
        [op] = self.broker.supervise()
        self.assertEqual(self.wait_internal(op)['state'], 'circuit_open')
        self.assertEqual(self.broker.supervise(), [], 'an open circuit admits nothing')
        self.clock.now += 301
        self.assertEqual(len(self.broker.supervise()), 1)

    def test_an_explicit_stop_holds_supervision_until_the_next_ensure(self):
        self.always_on()
        [op] = self.broker.supervise()
        self.wait_internal(op)
        self.wait(self.broker.stop(self.owner, self.args('stop-1')), self.owner)
        self.assertEqual(self.broker.supervise(), [])
        self.start_by(self.agent, 'start-again')
        self.adapter.crash('alpha.app')
        self.assertEqual(len(self.broker.supervise()), 1)

    def test_held_off_is_never_supervised(self):
        self.policy['targets']['alpha.app'] = target(desired='held_off')
        self.save_policy()
        self.assertEqual(self.broker.supervise(), [])

    def test_the_supervisor_has_no_owner_rights(self):
        self.always_on()
        with self.assertRaises(broker.LifecycleError):
            self.broker._admit(
                broker.SUPERVISOR,
                {'targetId': 'alpha.app', 'requestId': 'x', 'expectedPolicyRevision': 1, 'grantRevision': 0,
                 'force': True},
                'stop',
            )

    # -- idle

    def idle_target(self, minutes=15):
        self.policy['targets']['alpha.app'] = target(idle=minutes)
        self.save_policy()

    def test_an_idle_broker_app_is_stopped_after_its_timeout(self):
        self.idle_target()
        self.start_by(self.agent)
        self.clock.now += 14 * 60
        self.assertEqual(self.broker.supervise(), [])
        self.clock.now += 2 * 60
        [op] = self.broker.supervise()
        receipt = self.wait_internal(op)
        self.assertEqual((receipt['state'], receipt['kind']), ('stopped', 'stop'))
        self.assertEqual(self.finished[-1][1]['reason'], 'idle')
        self.assertEqual(self.broker.live_launches(), 0)

    def test_ensure_renews_the_lease_and_status_does_not(self):
        self.idle_target()
        self.start_by(self.agent)
        self.clock.now += 10 * 60
        self.wait(self.broker.ensure(self.agent, self.args('renew')), self.agent)
        self.clock.now += 6 * 60
        self.assertEqual(self.broker.supervise(), [], 'renewed 6 min ago')
        self.broker.status(self.agent, 'alpha.app')
        self.clock.now += 10 * 60
        self.assertEqual(len(self.broker.supervise()), 1, 'status is not a renewal')

    def test_idle_never_touches_a_person_app_a_taken_over_app_or_one_in_use(self):
        self.idle_target()
        self.adapter.run_as_person('alpha.app')
        self.clock.now += 60 * 60
        self.assertEqual(self.broker.supervise(), [])
        self.adapter.crash('alpha.app')
        self.start_by(self.agent)
        (self.data / 'alpha.app.active').write_text('')
        (self.data / 'alpha.app.idle').write_text(str(5 * 60))  # the person at the machine 5 min ago
        self.clock.now += 60 * 60
        self.assertEqual(self.broker.supervise(), [], 'frontmost with input inside the idle window')
        (self.data / 'alpha.app.idle').write_text(str(20 * 60))
        self.assertEqual(len(self.broker.supervise()), 1, 'frontmost but nobody there for 20 min')

    def test_idle_skips_an_adopted_app(self):
        self.idle_target()
        self.start_by(self.agent)
        pid = self.broker.status(self.agent, 'alpha.app')['pid']
        self.clock.now += broker.ADOPT_GRACE_SECONDS + 1
        self.broker.note_activation({'alpha.app'}, pid)
        self.clock.now += 60 * 60
        self.assertEqual(self.broker.supervise(), [])

    def test_registry_bounds_idle_and_refuses_idle_on_always_on(self):
        for bad in (0, 4, 1441, '15', 1.5):
            self.policy['targets']['alpha.app'] = target(idle=bad)
            self.save_policy()
            with self.assertRaises(broker.LifecycleError, msg=repr(bad)):
                broker.registry(self.data / 'registry.json')
        self.policy['targets']['alpha.app'] = target(desired='always_on', idle=15)
        self.save_policy()
        with self.assertRaises(broker.LifecycleError):
            broker.registry(self.data / 'registry.json')

    def test_supervision_probes_nothing_when_nothing_needs_it(self):
        # An on-demand target without a broker launch costs no probe at all.
        self.idle_target()
        before = self.adapter.probes
        self.broker.supervise()
        self.assertEqual(self.adapter.probes, before)


# -- the hardened engine ------------------------------------------------------------------------------------------

class Crash(BaseException):
    """Stands for the broker process dying: no `except Exception` in a worker catches it."""


class SlowReadyAdapter(ProcessAdapter):
    """After a start the process exists, but its capability comes up only after N probes (an app whose window is
    up seconds before the service inside it answers)."""

    def __init__(self, root, warmup=3, warmup_state='capability_missing', probe_error_once=False):
        super().__init__(root)
        self.warmup = {}
        self.default_warmup = warmup
        self.warmup_state = warmup_state
        self.probe_error_once = probe_error_once
        self.crash_on_start = False

    def start(self, target):
        action = super().start(target)
        self.warmup[target['targetId']] = self.default_warmup
        if self.crash_on_start:
            raise Crash()                                   # the app was launched; the broker died before anything else
        return action

    def probe(self, target):
        observation = super().probe(target)
        left = self.warmup.get(target['targetId'], 0)
        if observation['state'] == 'ready' and left > 0:
            self.warmup[target['targetId']] = left - 1
            if self.probe_error_once:
                self.probe_error_once = False
                raise broker.LifecycleError('delivery_unknown')
            return observation | {'state': self.warmup_state, 'capabilityReady': False}
        return observation


class EngineTests(BrokerFixture, unittest.TestCase):
    def use_adapter(self, adapter, **policy):
        self.broker.close()
        self.adapter = adapter
        self.policy['targets']['alpha.app'] |= policy
        self.save_policy()
        self.broker = self.make_broker()

    def die(self):
        """The broker's process dies: it dispatches nothing more and its single-instance lock goes with it (the
        operating system releases a dead process's lock). Its connection is closed at the end of the test."""
        self.broker.closed = True
        self.broker._release_instance_lock()
        self.addCleanup(self.broker.close, 0)

    def effects_rows(self):
        with self.broker.db_lock:
            return self.broker.db.execute('SELECT target,state FROM effects').fetchall()

    def operation(self, op_id):
        with self.broker.db_lock:
            return self.broker.db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone()

    # -- start-up states are not final answers

    def test_capability_coming_up_after_a_start_is_waited_for(self):
        self.use_adapter(SlowReadyAdapter(self.data, warmup=3), timeoutSeconds=5)
        got = self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.assertEqual(got['state'], 'ready', got)

    def test_a_probe_timeout_while_coming_up_is_not_delivery_unknown(self):
        self.use_adapter(SlowReadyAdapter(self.data, warmup=2, probe_error_once=True), timeoutSeconds=5)
        got = self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.assertEqual(got['state'], 'ready', got)

    def test_a_capability_that_never_comes_up_reports_the_honest_state_at_the_deadline(self):
        self.use_adapter(SlowReadyAdapter(self.data, warmup=10**6), timeoutSeconds=1)
        got = self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.assertEqual(got['state'], 'capability_missing', got)

    # -- the effect journal never disables supervision for good

    def test_a_start_that_ends_not_ready_with_a_live_process_resolves_the_journal_entry(self):
        self.use_adapter(SlowReadyAdapter(self.data, warmup=10**6), timeoutSeconds=1, desiredState='always_on')
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.assertEqual(self.effects_rows(), [], 'a live pid means the dispatch is known, not unknown')
        self.adapter.crash('alpha.app')
        self.adapter.default_warmup = 0
        ops = self.broker.supervise()
        self.assertEqual(len(ops), 1, 'always_on supervision must still see the target')
        self.assertEqual(self.wait_internal(ops[0])['state'], 'ready')

    def test_a_stale_entry_on_a_stopped_target_expires_after_the_reconcile_window(self):
        with self.broker.db_lock, self.broker.db:
            self.broker.db.execute("INSERT INTO effects VALUES('alpha.app','dispatching',?, 'old-op')", (self.clock(),))
        young = self.wait(self.broker.ensure(self.agent, self.args('s-young')), self.agent)
        self.assertEqual(young['state'], 'delivery_unknown', 'a fresh unknown dispatch is never repeated')
        self.assertEqual(self.adapter.effects, [])
        self.clock.now += broker.DISPATCH_RECONCILE_SECONDS + 1
        got = self.wait(self.broker.ensure(self.agent, self.args('s-old')), self.agent)
        self.assertEqual(got['state'], 'ready', got)
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app')])

    def test_a_stale_entry_does_not_block_always_on_after_the_window(self):
        self.policy['targets']['alpha.app']['desiredState'] = 'always_on'
        self.save_policy()
        with self.broker.db_lock, self.broker.db:
            self.broker.db.execute("INSERT INTO effects VALUES('alpha.app','dispatching',?, 'old-op')", (self.clock(),))
        self.assertEqual(self.broker.supervise(), [], 'inside the window the entry is respected')
        self.clock.now += broker.DISPATCH_RECONCILE_SECONDS + 1
        ops = self.broker.supervise()
        self.assertEqual(len(ops), 1)
        self.assertEqual(self.wait_internal(ops[0])['state'], 'ready')

    # -- a restart gets the doubled budget by default

    def test_a_default_restart_deadline_is_twice_the_target_timeout(self):
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        receipt = self.broker.restart(self.agent, self.args('r-1'))
        args = json.loads(self.operation(receipt['operationId'])['arguments'])
        budget = (args['deadlineAt'] / 1000) - self.operation(receipt['operationId'])['created']
        self.assertGreater(budget, self.policy['targets']['alpha.app']['timeoutSeconds'] * 1.5)
        self.wait(receipt, self.agent)

    # -- deliberate starts do not spend the crash circuit's budget

    def test_stop_and_start_cycles_by_a_caller_never_open_the_circuit(self):
        for i in range(4):
            self.assertEqual(self.wait(self.broker.ensure(self.agent, self.args(f's-{i}')), self.agent)['state'], 'ready')
            self.assertEqual(self.wait(self.broker.stop(self.agent, self.args(f'x-{i}')), self.agent)['state'], 'stopped')

    def test_restarts_never_open_the_circuit(self):
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        for i in range(3):
            got = self.wait(self.broker.restart(self.agent, self.args(f'r-{i}')), self.agent)
            self.assertEqual(got['state'], 'ready', got)

    def test_a_real_crash_loop_still_opens_the_circuit(self):
        states = []
        for i in range(3):
            states.append(self.wait(self.broker.ensure(self.agent, self.args(f's-{i}')), self.agent)['state'])
            self.adapter.crash('alpha.app')
        self.assertEqual(states[-1], 'circuit_open', states)

    # -- an idle stop reads the lease again under the target lock

    def test_a_lease_renewed_while_the_idle_stop_waits_keeps_the_app(self):
        self.policy['targets']['alpha.app']['idleStopMinutes'] = 5
        self.save_policy()
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.clock.now += 6 * 60
        lock = self.broker._lock('alpha.app')
        lock.acquire()
        try:
            ops = self.broker.supervise()
            self.assertEqual(len(ops), 1, 'the idle check admits a stop')
            with self.broker.db_lock, self.broker.db:     # a caller renews the lease right now
                self.broker.db.execute("INSERT OR REPLACE INTO touches VALUES('alpha.app',?)", (self.clock(),))
        finally:
            lock.release()
        got = self.wait_internal(ops[0])
        self.assertEqual(got['state'], 'lease_renewed', got)
        self.assertNotIn(('quit', 'alpha.app'), self.adapter.effects)

    # -- nothing is dispatched once the broker is closing

    def test_a_queued_start_is_not_dispatched_during_shutdown_and_recovers_later(self):
        lock = self.broker._lock('alpha.app')
        lock.acquire()
        receipt = self.broker.ensure(self.agent, self.args('s-1'))
        self.broker.closed = True
        lock.release()
        time.sleep(0.3)
        self.assertEqual(self.adapter.effects, [], 'no start after close')
        self.assertEqual(self.operation(receipt['operationId'])['state'], 'running', 'left for recovery')
        self.broker.close()
        self.broker = broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, self.adapter,
                                    clock=self.clock)
        self.assertEqual(self.wait(receipt, self.agent)['state'], 'ready')
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app')])

    # -- crash recovery resumes a restart after its stop half

    def test_a_restart_recovered_after_its_stop_does_not_stop_again(self):
        adapter = SlowReadyAdapter(self.data, warmup=0)
        self.use_adapter(adapter)
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        adapter.crash_on_start = True
        receipt = self.broker.restart(self.agent, self.args('r-1'))
        end = time.monotonic() + 5
        while ('quit', 'alpha.app') not in adapter.effects and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.2)
        self.die()
        adapter.crash_on_start = False
        stops_before = [e for e in adapter.effects if e[0] in ('quit', 'term', 'kill')]
        self.broker = broker.Broker(self.data / 'registry.json', self.data / 'state', self.authority, adapter,
                                    clock=self.clock)
        got = self.wait(receipt, self.agent)
        self.assertIn(got['state'], ('ready', 'already_ready'), got)
        stops_after = [e for e in adapter.effects if e[0] in ('quit', 'term', 'kill')]
        self.assertEqual(stops_after, stops_before, 'the stop half already happened')
        self.assertEqual([e for e in adapter.effects if e[0] == 'start'], [('start', 'alpha.app')] * 2,
                         'one initial start, one restart start: recovery adds none')

    # -- review round 1 of #342

    def test_a_caller_without_operation_get_still_receives_its_receipt(self):
        self.grants['callers']['caller-a']['allowedTools'] = ['lifecycle.ensure_running']
        write_private(self.data / 'callers.json', self.grants)
        first = self.broker.ensure(self.agent, self.args('s-1'))
        self.assertEqual(first['kind'], 'ensure')
        end = time.monotonic() + 10
        receipt = first
        while receipt['state'] == 'running' and time.monotonic() < end:
            time.sleep(0.02)
            receipt = self.broker.ensure(self.agent, self.args('s-1'))   # polled by retrying, the one tool it holds
        self.assertEqual((receipt['operationId'], receipt['state']), (first['operationId'], 'ready'))
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app')])
        with self.assertRaisesRegex(broker.LifecycleError, 'denied'):
            self.broker.get(self.agent, first['operationId'])          # reading by id is still its own tool

    def test_a_retry_after_a_grant_revision_bump_gets_the_original_operation(self):
        first = self.broker.ensure(self.agent, self.args('s-1'))
        self.wait(first, self.agent)
        self.grants['callers']['caller-a']['revision'] = 2
        write_private(self.data / 'callers.json', self.grants)
        agent = self.authority.authenticate('Bearer ' + self.tokens['caller-a'])
        self.assertEqual(agent['grantRevision'], 2)
        again = self.broker.ensure(agent, self.args('s-1'))              # the original arguments, grantRevision 1
        self.assertEqual(again['operationId'], first['operationId'])
        with self.assertRaisesRegex(broker.LifecycleError, 'denied'):
            self.broker.ensure(agent, self.args('s-2'))                  # a new request still meets the precondition
        self.assertEqual(self.adapter.effects, [('start', 'alpha.app')])

    def test_a_second_broker_on_one_state_folder_is_refused(self):
        with self.assertRaisesRegex(broker.LifecycleError, 'already_running'):
            self.make_broker()
        script = (
            'import sys; sys.path[:0] = [sys.argv[1]]\n'
            'from node.lifecycle import broker\n'
            'try:\n'
            '    broker.Broker(sys.argv[2] + "/registry.json", sys.argv[2] + "/state", None, None)\n'
            'except broker.LifecycleError as e:\n'
            '    print(e.code); sys.exit(3)\n'
        )
        other = subprocess.run([sys.executable, '-c', script, str(ROOT), str(self.data)],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual((other.returncode, other.stdout.strip()), (3, 'already_running'), other.stderr)
        self.broker.close()
        self.broker = self.make_broker()                                 # released by close: the next one opens
        self.assertEqual(self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)['state'], 'ready')

    def test_a_lone_surrogate_in_a_header_is_unauthorized(self):
        with self.assertRaisesRegex(broker.LifecycleError, 'unauthorized'):
            self.authority.authenticate('Bearer \ud800' + 'x' * 32)

    def test_another_operations_dispatch_found_inside_the_journal_transaction_blocks_the_start(self):
        calls = 0
        original = self.adapter.identity_matches

        def check(target, *, deadline=None):
            nonlocal calls
            calls += 1
            if calls == 2:                                  # after the journal was read, before the effect is written
                with self.broker.db_lock, self.broker.db:
                    self.broker.db.execute("INSERT OR REPLACE INTO effects VALUES('alpha.app','dispatching',?,'other-op')",
                                           (self.clock(),))
            return original(target, deadline=deadline)

        self.adapter.identity_matches = check
        got = self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.assertEqual(got['state'], 'delivery_unknown', got)
        self.assertEqual(self.adapter.effects, [])

    def test_a_start_recovered_after_its_dispatch_is_still_the_brokers(self):
        adapter = SlowReadyAdapter(self.data, warmup=0)
        self.use_adapter(adapter)
        adapter.crash_on_start = True
        receipt = self.broker.ensure(self.agent, self.args('s-1'))
        end = time.monotonic() + 5
        while ('start', 'alpha.app') not in adapter.effects and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.2)
        self.die()
        adapter.crash_on_start = False
        self.broker = self.make_broker(adapter)
        got = self.wait(receipt, self.agent)
        self.assertEqual(got['state'], 'already_ready', got)
        self.assertEqual(self.broker.status(self.agent, 'alpha.app')['startedBy'], 'broker',
                         'the recovered launch is recorded: an agent may stop it and it may idle out')
        self.assertEqual(self.wait(self.broker.stop(self.agent, self.args('x-1')), self.agent)['state'], 'stopped')
        self.assertEqual(adapter.effects.count(('start', 'alpha.app')), 1)

    def test_a_restart_recovered_past_its_deadline_still_runs_its_start_half(self):
        adapter = SlowReadyAdapter(self.data, warmup=0)
        self.use_adapter(adapter)
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        lock = self.broker._lock('alpha.app')
        receipt = self.broker.restart(self.agent, self.args('r-1'))
        end = time.monotonic() + 5
        while ('quit', 'alpha.app') not in adapter.effects and time.monotonic() < end:
            time.sleep(0.01)
        lock.acquire()                                      # hold the start half back until the process is gone
        try:
            end = time.monotonic() + 5
            while time.monotonic() < end:
                with self.broker.db_lock:
                    if self.broker.db.execute('SELECT 1 FROM phases WHERE operation=?',
                                              (receipt['operationId'],)).fetchone():
                        break
                time.sleep(0.01)
            self.die()
        finally:
            lock.release()
        self.clock.now += 120                               # recovered long after the original deadline
        self.broker = self.make_broker(adapter)
        got = self.wait(receipt, self.agent)
        self.assertEqual((got['state'], got['stopState']), ('ready', 'stopped'), got)
        self.assertEqual(adapter.effects.count(('start', 'alpha.app')), 2)
        self.assertEqual(len([e for e in adapter.effects if e[0] in ('quit', 'term', 'kill')]), 1)

    # -- idempotency

    def test_a_retry_after_a_policy_bump_gets_the_original_operation(self):
        first = self.broker.ensure(self.agent, self.args('s-1'))
        self.wait(first, self.agent)
        self.policy['targets']['alpha.app']['policyRevision'] = 2
        self.save_policy()
        again = self.broker.ensure(self.agent, self.args('s-1', revision=2))
        self.assertEqual(again['operationId'], first['operationId'])

    def test_a_retry_with_a_new_deadline_is_the_same_request(self):
        first = self.broker.ensure(self.agent, self.args('s-1'))
        later = int((self.clock() + 20) * 1000)
        again = self.broker.ensure(self.agent, self.args('s-1', deadlineAt=later))
        self.assertEqual(again['operationId'], first['operationId'])
        self.wait(first, self.agent)

    def test_the_same_request_id_for_another_target_is_still_a_conflict(self):
        self.policy['targets']['beta.app'] = target('beta.app')
        self.save_policy()
        first = self.broker.ensure(self.agent, self.args('s-1'))
        with self.assertRaises(broker.LifecycleError) as caught:
            self.broker.ensure(self.agent, self.args('s-1', target_id='beta.app'))
        self.assertEqual(caught.exception.code, 'idempotency_conflict')
        self.wait(first, self.agent)

    def test_a_receipt_expires_after_its_ttl(self):
        first = self.broker.ensure(self.agent, self.args('s-1'))
        self.wait(first, self.agent)
        self.adapter.crash('alpha.app')
        self.clock.now += broker.REQUEST_TTL_SECONDS + 1
        self.broker.prune()
        again = self.broker.ensure(self.agent, self.args('s-1'))
        self.assertNotEqual(again['operationId'], first['operationId'])
        self.assertEqual(self.wait(again, self.agent)['state'], 'ready')

    # -- a bounded store

    def test_an_operation_stores_only_its_dependency_closure(self):
        self.policy['targets']['beta.app'] = target('beta.app')
        self.save_policy()
        receipt = self.broker.ensure(self.agent, self.args('s-1'))
        self.wait(receipt, self.agent)
        policy = json.loads(self.operation(receipt['operationId'])['policy'])
        self.assertEqual(sorted(policy['targets']), ['alpha.app'])

    def test_prune_bounds_operations_requests_and_attempts(self):
        for i in range(3):
            self.wait(self.broker.ensure(self.agent, self.args(f's-{i}')), self.agent)
            self.adapter.crash('alpha.app')
        self.clock.now += broker.OPERATION_RETENTION_SECONDS + 1
        self.broker.prune()
        with self.broker.db_lock:
            counts = [self.broker.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                      for t in ('operations', 'requests', 'attempts')]
        self.assertEqual(counts, [0, 0, 0])

    # -- an always_on target is the owner's to stop

    def test_an_agent_cannot_hold_an_always_on_target_off(self):
        self.policy['targets']['alpha.app']['desiredState'] = 'always_on'
        self.save_policy()
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        got = self.wait(self.broker.stop(self.agent, self.args('x-1')), self.agent)
        self.assertEqual(got['state'], 'denied', got)
        self.adapter.crash('alpha.app')
        got = self.wait(self.broker.stop(self.agent, self.args('x-2')), self.agent)
        self.assertEqual(got['state'], 'denied', 'also when it is already stopped')
        self.assertEqual(len(self.broker.supervise()), 1, 'no stop-hold was recorded')

    def test_an_operator_proxy_caller_may_stop_an_always_on_target(self):
        # A caller that relays the operator's own commands is marked `operatorProxy` by the owner.
        self.policy['targets']['alpha.app']['desiredState'] = 'always_on'
        self.save_policy()
        self.grants['callers']['caller-a']['operatorProxy'] = True
        write_private(self.data / 'callers.json', self.grants)
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        self.assertEqual(self.wait(self.broker.stop(self.agent, self.args('x-1')), self.agent)['state'], 'stopped')

    def test_the_owner_still_stops_an_always_on_target(self):
        self.policy['targets']['alpha.app']['desiredState'] = 'always_on'
        self.save_policy()
        self.wait(self.broker.ensure(self.owner, self.args('s-1')), self.owner)
        self.assertEqual(self.wait(self.broker.stop(self.owner, self.args('x-1')), self.owner)['state'], 'stopped')

    # -- a long target id is still supervised

    def test_a_long_target_id_gets_a_valid_supervisor_request_id(self):
        long_id = 'a.' + 'x' * 120
        self.policy['targets'] = {long_id: target(long_id, desired='always_on')}
        self.save_policy()
        ops = self.broker.supervise()
        self.assertEqual(len(ops), 1)
        self.wait_internal(ops[0])

    # -- callers learn whether a person is present, not the machine's idle time

    def test_status_gives_callers_a_presence_flag_not_raw_input_idle(self):
        self.wait(self.broker.ensure(self.agent, self.args('s-1')), self.agent)
        got = self.broker.status(self.agent, 'alpha.app')
        self.assertNotIn('inputIdleSeconds', got)
        self.assertNotIn('active', got)
        self.assertIs(got['personPresent'], False)


# -- the always_on restart budget ---------------------------------------------------------------------------------

class UnreadyAdapter(ProcessAdapter):
    """`<id>.unready`: the process runs but never serves (an app stuck in its startup-failure dialog)."""

    def probe(self, target):
        observation = super().probe(target)
        if observation['state'] == 'ready' and self._f(target['targetId'], 'unready').exists():
            return observation | {'state': 'capability_missing', 'capabilityReady': False, 'reason': 'not_serving'}
        return observation


class RestartBudgetTests(BrokerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.broker.close()
        self.adapter = UnreadyAdapter(self.data)
        self.gave_up = []
        self.broker = self.make_broker(on_gave_up=self.gave_up.append)
        self.policy['targets']['alpha.app'] = target(desired='always_on', timeout=1)
        self.save_policy()

    def supervised_start(self):
        [op] = self.broker.supervise()
        return self.wait_internal(op)

    def fail_once(self, after=200):
        """One start that exits after `after` seconds: a dialog the person closes a few minutes later."""
        receipt = self.supervised_start()
        self.clock.now += after
        self.adapter.crash('alpha.app')
        return receipt

    def test_starts_that_exit_within_minutes_stop_after_three(self):
        # Spaced past the 60 s crash window, so the circuit alone never opens and the loop would run for ever.
        for _ in range(3):
            self.assertEqual(self.fail_once()['state'], 'ready')
        self.assertEqual(self.broker.supervise(), [], 'the third failed start spends the budget')
        self.assertEqual(len(self.gave_up), 1)
        info = self.gave_up[0]
        self.assertEqual((info['targetId'], info['failedStarts']), ('alpha.app', 3))
        self.clock.now += 3600
        self.assertEqual(self.broker.supervise(), [], 'no time-based retry: it waits for someone to act')
        self.assertEqual(len(self.gave_up), 1, 'one notification, not one per pass')
        self.assertEqual(self.adapter.effects.count(('start', 'alpha.app')), 3)
        status = self.broker.status(self.owner, 'alpha.app')
        self.assertEqual(status['supervision']['state'], 'gave_up')
        self.assertEqual(status['supervision']['failedStarts'], 3)

    def test_a_start_that_never_served_counts_however_long_it_ran(self):
        (self.data / 'alpha.app.unready').write_text('')
        for _ in range(3):
            receipt = self.fail_once(after=1200)
            self.assertEqual(receipt['state'], 'capability_missing')
            self.assertTrue(receipt.get('pid'))
            self.clock.now -= 1200 - 300                   # keep the three starts inside one 30-minute window
        self.assertEqual(self.broker.supervise(), [])
        self.assertEqual(len(self.gave_up), 1)

    def test_a_process_that_does_not_exit_is_never_relaunched(self):
        (self.data / 'alpha.app.unready').write_text('')
        self.supervised_start()
        self.clock.now += 600
        self.assertEqual(self.broker.supervise(), [], 'alive but not serving is never a restart reason')

    def test_healthy_runs_that_end_later_do_not_spend_the_budget(self):
        for _ in range(5):
            self.assertEqual(self.fail_once(after=400)['state'], 'ready')
        self.assertEqual(len(self.broker.supervise()), 1)
        self.assertEqual(self.gave_up, [])

    def test_failures_older_than_the_window_do_not_count(self):
        (self.data / 'alpha.app.unready').write_text('')  # never served: counted whenever the exit is seen
        self.fail_once()
        self.fail_once()
        self.clock.now += broker.RESTART_WINDOW_SECONDS
        self.fail_once()
        self.fail_once()
        self.assertEqual(len(self.broker.supervise()), 1)
        self.assertEqual(self.gave_up, [])

    def test_a_dispatch_that_never_produced_a_process_counts(self):
        def refuse(t):
            self.adapter.effects.append(('start', t['targetId']))
            raise broker.LifecycleError('failed')

        self.adapter.start = refuse
        for _ in range(3):
            self.assertEqual(self.supervised_start()['state'], 'delivery_unknown')
            self.clock.now += broker.DISPATCH_RECONCILE_SECONDS + 1
        self.assertEqual(self.broker.supervise(), [])
        self.assertEqual(len(self.gave_up), 1)

    def give_up(self):
        for _ in range(3):
            self.fail_once()
        self.assertEqual(self.broker.supervise(), [])

    def test_a_callers_ensure_rearms_supervision_with_a_fresh_budget(self):
        self.give_up()
        receipt = self.wait(self.broker.ensure(self.agent, self.args('person-asks')), self.agent)
        self.assertEqual(receipt['state'], 'ready')
        self.assertNotIn('supervision', self.broker.status(self.owner, 'alpha.app'))
        self.clock.now += 200
        self.adapter.crash('alpha.app')
        self.assertEqual(len(self.broker.supervise()), 1, 'supervision is back, its budget fresh')

    def test_an_owner_policy_change_rearms_supervision(self):
        self.give_up()
        self.policy['targets']['alpha.app']['policyRevision'] = 2
        self.save_policy()
        self.assertEqual(len(self.broker.supervise()), 1)

    def test_serving_again_rearms_supervision(self):
        # The person opened it after the cause was fixed, and a probe stronger than a pid saw it serve.
        self.policy['targets']['alpha.app']['readiness'] = dict(SERVICE_READINESS)
        self.save_policy()
        self.give_up()
        self.adapter.run_as_person('alpha.app')
        self.assertEqual(self.broker.supervise(), [])
        self.assertNotIn('supervision', self.broker.status(self.owner, 'alpha.app'))
        self.clock.now += 400
        self.adapter.crash('alpha.app')
        self.assertEqual(len(self.broker.supervise()), 1)

    def test_a_running_process_readiness_alone_does_not_rearm(self):
        # With `process` readiness a pid is "ready" even inside a failure dialog: the person opening it to see why
        # must not restart the loop.
        self.give_up()
        self.adapter.run_as_person('alpha.app')
        self.broker.supervise()
        self.adapter.crash('alpha.app')
        self.assertEqual(self.broker.supervise(), [])

    def test_the_sixty_second_circuit_still_applies(self):
        for _ in range(2):
            self.supervised_start()
            self.adapter.crash('alpha.app')
        self.assertEqual(self.supervised_start()['state'], 'circuit_open')

    def test_restart_window_and_budget_are_the_documented_values(self):
        self.assertEqual((broker.RESTART_BUDGET, broker.RESTART_WINDOW_SECONDS, broker.EARLY_EXIT_SECONDS),
                         (3, 1800, 300))


# -- a real process crash ----------------------------------------------------------------------------------------

CRASHING_START = '''
import os, sys, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from node.lifecycle import broker
from test_node_lifecycle_broker import FileAdapter
data = Path(sys.argv[3])
authority = broker.Authority(data / 'callers.json')

class Crashing(FileAdapter):
    def start(self, target):
        super().start(target)
        os._exit(86)                                   # the effect happened; the broker dies before its answer

instance = broker.Broker(data / 'registry.json', data / 'state', authority, Crashing(data))
instance.ensure(authority.authenticate('Bearer ' + 'x' * 32),
                {'targetId': 'alpha.app', 'requestId': 'crash', 'grantRevision': 1, 'expectedPolicyRevision': 1})
time.sleep(5)
'''


class CrashProcessTests(Folder, unittest.TestCase):
    def test_os_process_crash_after_effect_recovers_without_second_effect(self):
        token = 'x' * 32
        write_private(self.data / 'callers.json',
                      {'schemaVersion': 1, 'callers': {'owner': grant(token, owner=True, targets=('alpha.app',))}})
        write_private(self.data / 'registry.json', {'schemaVersion': 1, 'targets': {'alpha.app': target()}})
        process = subprocess.run(
            [sys.executable, '-c', CRASHING_START, str(ROOT), str(Path(__file__).resolve().parent), str(self.data)],
            capture_output=True, timeout=20,
        )
        self.assertEqual(process.returncode, 86, process.stderr.decode())
        authority = broker.Authority(self.data / 'callers.json')
        principal = authority.authenticate('Bearer ' + token)
        instance = broker.Broker(self.data / 'registry.json', self.data / 'state', authority, FileAdapter(self.data))
        try:
            end = time.monotonic() + 3
            while time.monotonic() < end:
                receipt = instance.ensure(principal, {'targetId': 'alpha.app', 'requestId': 'crash',
                                                      'grantRevision': 1, 'expectedPolicyRevision': 1})
                if receipt['state'] != 'running':
                    break
                time.sleep(0.01)
            self.assertEqual(receipt['state'], 'already_ready')
            self.assertEqual(receipt['generation'], 2)
            self.assertEqual((self.data / 'effects.log').read_text().splitlines(), ['alpha.app'])
        finally:
            instance.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
