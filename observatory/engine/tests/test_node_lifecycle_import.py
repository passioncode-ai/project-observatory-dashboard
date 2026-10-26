#!/usr/bin/env python3
"""Importing another lifecycle broker's state (OBS-79 step L5, docs/design/LIFECYCLE.md): `full lifecycle import`.

The source folder is built here from scratch in the format the import reads (`registry.json`, `callers.json`,
`lifecycle.sqlite3`), with neutral fixtures: targets `alpha.app`, `beta.app`, `gamma.app`; callers `caller-a`,
`caller-b`; bundle ids under `com.example`; signing team `ABCDE12345`. No installed application is touched: the
bundle check of the identity migration is an injected function. The seven identity-migration cases the port plan
counted for this step are here (`MigrationTests`), adapted to an import that writes the engine's document instead
of rewriting the source's.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
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
from node.lifecycle import broker, importer            # noqa: E402
from node.lifecycle import service as S                # noqa: E402
from test_node_lifecycle_broker import TEAM, app_identity, target, write_private  # noqa: E402

AUDIENCE = 'observatory:test'
OTHER_TEAM = 'ZYXWV98765'


def bearer(name: str) -> str:
    """A fixture bearer composed rather than written out, so the secret scan has no key-shaped literal."""
    return name + '-' + 'y' * 32


def token_hash(name: str) -> str:
    return hashlib.sha256(bearer(name).encode()).hexdigest()


def stamp(at: datetime) -> str:
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def binding(principal: str, n: int, *, token: str, revoked: bool = False, audience: str = AUDIENCE) -> dict:
    now = datetime.now(timezone.utc)
    return {'bindingId': f'bnd_import{n:04d}', 'principal': principal, 'channel': 'http', 'audience': audience,
            'credentialRef': {'kind': 'bearer-sha256', 'digest': AB.bearer_digest(token)},
            'projects': ['project:alpha-web'], 'workflows': None, 'scopes': ['memory.read'],
            'classCeiling': 'public', 'effectCeiling': 'read', 'issuedAt': stamp(now - timedelta(days=1)),
            'expiresAt': stamp(now + timedelta(days=30)), 'revokedAt': stamp(now) if revoked else None,
            'issuedBy': 'operator', 'via': 'terminal'}


def source_grant(name: str, *, targets=('alpha.app',), tools=broker.TOOLS, revision=2, **extra) -> dict:
    return {'tokenHash': token_hash(name), 'revision': revision, 'revoked': False, 'owner': False,
            'allowedTools': list(tools), 'allowedTargets': list(targets), **extra}


def source_target(key: str, bundle: str, **extra) -> dict:
    t = target(key, callers=('owner', 'caller-a', 'caller-b'))
    t.update(path=f'/Applications/{key.split(".")[0].title()}.app', identity=app_identity(f'com.example.{bundle}'))
    t.update(extra)
    return t


def legacy(key: str, bundle: str, **extra) -> dict:
    """An app enrolled before the requirement scheme: a code hash and a metadata hash, no `scheme`."""
    t = source_target(key, bundle, **extra)
    t['identity'] = {'bundleId': f'com.example.{bundle}', 'version': '1.0.0', 'infoHash': 'info',
                     'executableHash': 'exe', 'codeHash': 'cdhash', 'bundleMetadataHash': f'enrolled-{key}'}
    t['policyRevision'] = 2
    return t


class Folder:
    """A source state folder, an engine document and state folder, and a bindings registry, all temporary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'other-broker'
        self.source.mkdir(mode=0o700)
        self.config = self.root / 'config' / S.CONFIG_NAME
        self.state = self.root / 'state'
        self.targets = {'alpha.app': source_target('alpha.app', 'alpha'),
                        'beta.app': source_target('beta.app', 'beta')}
        self.callers = {
            'owner': {'tokenHash': token_hash('owner'), 'revision': 1, 'revoked': False, 'owner': True,
                      'allowedTargets': ['*'], 'allowedTools': list(broker.TOOLS) + ['service.admin']},
            'fabric-host': {'tokenHash': token_hash('host'), 'revision': 1, 'revoked': False, 'owner': False,
                            'host': True, 'allowedTools': [], 'allowedTargets': []},
            'caller-a': source_grant('caller-a', targets=('alpha.app', 'beta.app')),
            'caller-b': source_grant('caller-b', tools=('lifecycle.list', 'lifecycle.status')),
        }
        self.bindings = [binding('agent:caller-a', 1, token=bearer('caller-a'))]
        self.installed: dict = {}
        self.write_source()

    def tearDown(self):
        self.tmp.cleanup()

    def write_source(self):
        write_private(self.source / importer.SOURCE_REGISTRY, {'schemaVersion': 1, 'targets': self.targets})
        write_private(self.source / importer.SOURCE_CALLERS, {'schemaVersion': 1, 'callers': self.callers})

    def registry(self) -> AB.Registry:
        return AB.parse({'schema': AB.SCHEMA, 'revision': 1, 'bindings': self.bindings})

    def derive(self, path):
        found = self.installed[path]
        if isinstance(found, Exception):
            raise found
        return found

    def run_import(self, *, dry_run=False, **kw) -> dict:
        kw.setdefault('bindings', self.registry())
        kw.setdefault('audience', AUDIENCE)
        kw.setdefault('derive', self.derive)
        return importer.run(self.source, dry_run=dry_run, config=self.config, state=self.state, **kw)

    def engine(self) -> dict:
        return S.read_config(self.config)

    def events(self) -> list:
        path = self.state / S.EVENTS_FILE
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def by_id(out: dict, kind: str) -> dict:
        return {i['id']: i for i in out['items'] if i['kind'] == kind}

    def snapshot(self) -> dict:
        """Every file of the source folder: its bytes, mode and modification time."""
        out = {}
        for p in sorted(self.source.iterdir()):
            info = p.lstat()
            out[p.name] = (p.read_bytes(), info.st_mode, info.st_mtime_ns)
        return out


class SourceFolderTests(Folder, unittest.TestCase):
    def assertRefused(self, code, path=None):
        with self.assertRaises(importer.SourceError) as caught:
            importer.read_source(self.source if path is None else path)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_a_relative_or_missing_folder_is_refused(self):
        self.assertRefused('invalid_argument', 'other-broker')
        self.assertRefused('not_found', self.root / 'absent')

    def test_a_link_to_the_folder_or_among_its_parents_is_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.source)
        self.assertRefused('unsafe_source', link)
        self.assertRefused('unsafe_source', link / '.')

    def test_a_folder_others_may_write_is_refused(self):
        os.chmod(self.source, 0o770)
        self.assertRefused('unsafe_source')

    def test_a_folder_of_another_account_is_refused(self):
        with mock.patch.object(importer.osprivacy, 'owned_by_me', return_value=False):
            self.assertRefused('unsafe_source')

    def test_a_folder_without_a_registry_is_not_a_broker_state(self):
        (self.source / importer.SOURCE_REGISTRY).unlink()
        self.assertRefused('not_a_broker_state')

    def test_a_document_others_may_read_or_a_linked_document_is_refused(self):
        os.chmod(self.source / importer.SOURCE_CALLERS, 0o644)
        self.assertRefused('unsafe_source')
        os.chmod(self.source / importer.SOURCE_CALLERS, 0o600)
        real = self.root / 'elsewhere.json'
        os.replace(self.source / importer.SOURCE_REGISTRY, real)
        (self.source / importer.SOURCE_REGISTRY).symlink_to(real)
        self.assertRefused('unsafe_source')

    def test_a_registry_of_another_shape_refuses_the_whole_source(self):
        write_private(self.source / importer.SOURCE_REGISTRY, {'schemaVersion': 2, 'targets': {}})
        self.assertRefused('invalid_source')
        write_private(self.source / importer.SOURCE_REGISTRY, [1, 2])
        self.assertRefused('invalid_source')

    def test_the_detail_never_carries_a_value_of_the_source(self):
        os.chmod(self.source, 0o777)
        exc = self.assertRefused('unsafe_source')
        self.assertNotIn(str(self.root), exc.detail)


class StoreTests(Folder, unittest.TestCase):
    def make_store(self, mode=0o600, *, live=False, version=1):
        """The other broker's store; `live` keeps its writer open with the rows still in the write-ahead log only."""
        path = self.source / importer.SOURCE_STORE
        db = sqlite3.connect(path)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        if version is not None:
            db.execute('CREATE TABLE meta(version INTEGER NOT NULL)')
            db.execute('INSERT INTO meta VALUES (?)', (version,))
        db.executescript("""
            CREATE TABLE operations(id TEXT PRIMARY KEY, target TEXT, state TEXT);
            CREATE TABLE launches(target TEXT PRIMARY KEY, pid INTEGER, adopted REAL);
            INSERT INTO operations VALUES ('op-1', 'alpha.app', 'running'), ('op-2', 'alpha.app', 'ready');
            INSERT INTO launches VALUES ('alpha.app', 4242, NULL), ('beta.app', 4343, 12.0);
        """)
        db.commit()
        for suffix in ('', '-wal', '-shm'):
            if Path(str(path) + suffix).exists():
                os.chmod(str(path) + suffix, mode)
        if live:
            self.addCleanup(db.close)
        else:
            db.close()

    def test_the_store_is_counted_read_only_and_left_as_it_was(self):
        for live in (False, True):
            with self.subTest(live=live):
                for p in self.source.glob(importer.SOURCE_STORE + '*'):
                    p.unlink()
                self.make_store(live=live)
                if live:
                    self.assertGreater((self.source / (importer.SOURCE_STORE + '-wal')).stat().st_size, 0)
                before = self.snapshot()
                out = self.run_import(dry_run=True)
                self.assertEqual(out['store'], {'runningOperations': 1, 'startedProcesses': 1})
                self.assertEqual(out['degraded'], [])
                self.assertEqual(self.snapshot(), before)    # no -shm, no -wal, no byte of the folder changed

    def test_a_store_of_another_or_no_version_is_not_counted(self):
        for version in (2, None):
            with self.subTest(version=version):
                for p in self.source.glob(importer.SOURCE_STORE + '*'):
                    p.unlink()
                self.make_store(version=version)
                store, degraded = importer.read_store(self.source)
                self.assertIsNone(store)
                self.assertIn('version', degraded[0]['reason'])

    def test_a_write_ahead_log_others_may_read_makes_the_counts_degraded(self):
        self.make_store(live=True)
        os.chmod(self.source / (importer.SOURCE_STORE + '-wal'), 0o644)
        store, degraded = importer.read_store(self.source)
        self.assertIsNone(store)
        self.assertIn('write-ahead log is readable or writable by others', degraded[0]['reason'])

    def test_a_store_that_moved_while_copied_is_copied_again_once(self):
        self.make_store(live=True)
        real = importer._stamp
        calls = []

        def moving(path, *, times):
            calls.append(path)
            stamp = real(path)
            return None if stamp is None else stamp + (len(calls) if len(calls) <= times else 0,)
        # The first copy moves under the reader and the second holds: counted.
        with mock.patch.object(importer, '_stamp', side_effect=lambda p: moving(p, times=2)):
            store, degraded = importer.read_store(self.source)
        self.assertEqual((store, degraded), ({'runningOperations': 1, 'startedProcesses': 1}, []))
        calls.clear()
        with mock.patch.object(importer, '_stamp', side_effect=lambda p: moving(p, times=99)):
            store, degraded = importer.read_store(self.source)
        self.assertIsNone(store)
        self.assertIn('changed while it was copied', degraded[0]['reason'])

    def test_a_store_others_may_read_is_not_opened(self):
        self.make_store(mode=0o644)
        with mock.patch.object(importer.sqlite3, 'connect') as connect:
            store, degraded = importer.read_store(self.source)
        connect.assert_not_called()
        self.assertIsNone(store)
        self.assertEqual(degraded[0]['source'], 'store')

    def test_no_store_is_no_count_and_no_fault(self):
        self.assertEqual(importer.read_store(self.source), (None, []))

    def test_a_store_that_is_not_sqlite_is_degraded_not_fatal(self):
        write_private(self.source / importer.SOURCE_STORE, {'not': 'sqlite'})
        out = self.run_import(dry_run=True)
        self.assertIsNone(out['store'])
        self.assertEqual(out['degraded'][0]['source'], 'store')


class ImportTests(Folder, unittest.TestCase):
    def test_a_dry_run_says_what_would_change_and_writes_nothing(self):
        out = self.run_import(dry_run=True)
        self.assertTrue(out['wouldChange'])
        self.assertFalse(out['changed'])
        self.assertEqual(out['targets'], {'imported': 2})
        self.assertFalse(self.config.exists())
        self.assertEqual(self.events(), [])
        self.assertIn('without --dry-run', out['next'][0])

    def test_targets_and_a_bound_grant_are_imported_in_one_write_and_journaled(self):
        before = self.snapshot()
        with mock.patch.object(S, 'write_config', wraps=S.write_config) as write:
            out = self.run_import()
        write.assert_called_once()
        self.assertTrue(out['changed'])
        doc = self.engine()
        self.assertEqual(set(doc['targets']), {'alpha.app', 'beta.app'})
        alpha = doc['targets']['alpha.app']
        # The source's owner is the operator's terminal here; its callers become principals.
        self.assertEqual(alpha['allowedCallers'], ['agent:caller-a', 'agent:caller-b'])
        self.assertEqual(alpha['identity'], self.targets['alpha.app']['identity'])
        self.assertEqual(alpha['policyRevision'], self.targets['alpha.app']['policyRevision'])
        self.assertEqual(doc['callers'], {'agent:caller-a': {
            'bindingId': 'bnd_import0001', 'revision': 2, 'revoked': False, 'allowedTools': list(broker.TOOLS),
            'allowedTargets': ['alpha.app', 'beta.app']}})
        callers = self.by_id(out, 'caller')
        self.assertEqual(callers['caller-a']['outcome'], 'imported')
        self.assertEqual(callers['owner'], {'kind': 'caller', 'id': 'owner', 'outcome': 'skipped',
                                            'code': 'owner_not_imported'})
        self.assertEqual(callers['fabric-host']['code'], 'host_not_imported')
        [event] = self.events()
        self.assertEqual(event['kind'], 'lifecycle.imported')
        self.assertIn('2 targets and 1 callers', event['text'])
        self.assertEqual(self.snapshot(), before)            # the source is read, never written

    def test_a_caller_without_a_bound_binding_must_be_issued_one(self):
        out = self.run_import()
        b = self.by_id(out, 'caller')['caller-b']
        self.assertEqual((b['outcome'], b['code'], b['principal']), ('reissue_required', 'no_binding', 'agent:caller-b'))
        self.assertNotIn('agent:caller-b', self.engine()['callers'])
        step = next(s for s in out['next'] if 'agent:caller-b' in s)
        self.assertIn('full access-binding issue agent:caller-b', step)
        self.assertIn('full lifecycle allow agent:caller-b --binding BINDING_ID --tool=lifecycle.list '
                      '--tool=lifecycle.status --target=alpha.app', step)

    def test_a_binding_of_another_principal_audience_or_revoked_matches_nothing(self):
        for case in ({'principal': 'agent:caller-b'}, {'audience': 'observatory:other'}, {'revoked': True}):
            with self.subTest(**{k: str(v) for k, v in case.items()}):
                principal = case.pop('principal', 'agent:caller-a')
                self.bindings = [binding(principal, 1, token=bearer('caller-a'), **case)]
                out = self.run_import(dry_run=True)
                self.assertEqual(self.by_id(out, 'caller')['caller-a']['outcome'], 'reissue_required')

    def test_a_second_run_changes_nothing_and_says_so(self):
        self.run_import()
        before = self.config.read_bytes()
        out = self.run_import()
        self.assertFalse(out['changed'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(out['targets'], {'unchanged': 2})
        self.assertEqual(self.by_id(out, 'caller')['caller-a']['outcome'], 'unchanged')

    def test_the_engines_own_records_win_and_are_never_overwritten(self):
        mine = target('alpha.app', callers=('agent:alpha',))
        S.write_config({'schemaVersion': 1, 'targets': {'alpha.app': mine},
                        'callers': {'agent:caller-a': {'bindingId': 'bnd_import0001', 'revision': 7,
                                                       'revoked': False, 'allowedTools': ['lifecycle.list'],
                                                       'allowedTargets': ['alpha.app']}}}, self.config)
        out = self.run_import()
        self.assertEqual(self.by_id(out, 'target')['alpha.app']['code'], 'already_enrolled')
        self.assertEqual(self.by_id(out, 'caller')['caller-a']['code'], 'already_granted')
        doc = self.engine()
        self.assertEqual(doc['targets']['alpha.app'], mine)
        self.assertEqual(doc['callers']['agent:caller-a']['revision'], 7)
        self.assertIn('beta.app', doc['targets'])

    def test_a_refused_record_is_one_item_and_the_rest_is_imported(self):
        self.targets['gamma.app'] = source_target('gamma.app', 'gamma', startupEffects=[])
        self.targets['delta.app'] = source_target('delta.app', 'delta', dependencies=['gamma.app'])
        self.targets['beta.app']['readiness'] = {'kind': 'process', 'unknown': True}
        self.write_source()
        out = self.run_import()
        targets = self.by_id(out, 'target')
        self.assertEqual(targets['gamma.app']['code'], 'startup_effects_required')
        self.assertEqual(targets['delta.app']['code'], 'dependency_refused')
        self.assertEqual(targets['beta.app']['code'], 'invalid_registry')
        self.assertEqual(set(self.engine()['targets']), {'alpha.app'})
        # The grant keeps the target that came and names the one that did not.
        self.assertEqual(self.engine()['callers']['agent:caller-a']['allowedTargets'], ['alpha.app'])
        self.assertEqual(self.by_id(out, 'caller')['caller-a']['notImported'], ['beta.app'])

    def test_grants_the_engine_cannot_hold_are_refused_or_skipped_with_a_code(self):
        self.callers['caller-c'] = source_grant('caller-c', targets=('*',))
        self.callers['caller-d'] = source_grant('caller-d', tools=('lifecycle.list', 'service.admin'))
        self.callers['caller-e'] = source_grant('caller-e', revoked=True)
        self.write_source()
        callers = self.by_id(self.run_import(dry_run=True), 'caller')
        self.assertEqual(callers['caller-c']['code'], 'wildcard_refused')
        self.assertEqual(callers['caller-d']['code'], 'unknown_tool')
        self.assertEqual((callers['caller-e']['outcome'], callers['caller-e']['code']), ('skipped', 'revoked'))

    def test_a_malformed_caller_list_is_one_refusal_not_a_crash(self):
        self.targets['gamma.app'] = source_target('gamma.app', 'gamma', allowedCallers=[{'name': 'x'}])
        self.write_source()
        out = self.run_import(dry_run=True)
        self.assertEqual(self.by_id(out, 'target')['gamma.app']['code'], 'caller_unmapped')
        self.assertEqual(self.by_id(out, 'target')['alpha.app']['outcome'], 'imported')

    def test_a_dependency_cycle_or_a_long_list_refuses_only_the_targets_it_touches(self):
        self.targets['gamma.app'] = source_target('gamma.app', 'gamma', dependencies=['delta.app'])
        self.targets['delta.app'] = source_target('delta.app', 'delta', dependencies=['gamma.app'])
        self.targets['eps.app'] = source_target('eps.app', 'eps', dependencies=['gamma.app'])
        self.targets['zeta.app'] = source_target('zeta.app', 'zeta', dependencies=['alpha.app'] * 17)
        self.write_source()
        out = self.run_import()
        targets = self.by_id(out, 'target')
        self.assertEqual(targets['gamma.app']['code'], 'invalid_dependencies')
        self.assertEqual(targets['delta.app']['code'], 'invalid_dependencies')
        self.assertEqual(targets['eps.app']['code'], 'dependency_refused')
        self.assertEqual(targets['zeta.app']['code'], 'invalid_registry')
        self.assertEqual(set(self.engine()['targets']), {'alpha.app', 'beta.app'})

    def test_an_always_on_target_comes_over_on_demand_unless_kept(self):
        self.targets['alpha.app']['desiredState'] = 'always_on'
        self.write_source()
        out = self.run_import(dry_run=True)
        step = next(s for s in out['next'] if 'always_on' in s)
        self.assertIn('full lifecycle policy TARGET_ID --desired always_on`: alpha.app', step)
        self.run_import()
        self.assertEqual(self.engine()['targets']['alpha.app']['desiredState'], 'on_demand')
        self.assertFalse(self.run_import()['changed'])          # the converted record is the one compared
        self.config.unlink()
        kept = self.run_import(keep_always_on=True)
        self.assertEqual(self.engine()['targets']['alpha.app']['desiredState'], 'always_on')
        self.assertTrue(any('--keep-always-on' in s for s in kept['next']))

    def test_a_grant_target_that_is_no_id_is_counted_never_echoed(self):
        self.callers['caller-a']['allowedTargets'] = ['alpha.app', 'gone.app', '../not an id']
        self.write_source()
        out = self.run_import()
        a = self.by_id(out, 'caller')['caller-a']
        self.assertEqual((a['notImported'], a['notImportedUnnamed']), (['gone.app'], 1))
        self.assertNotIn('not an id', json.dumps(out))

    def test_a_target_whose_only_caller_is_the_owner_is_refused(self):
        self.targets['gamma.app'] = source_target('gamma.app', 'gamma', allowedCallers=['owner'])
        self.write_source()
        out = self.run_import(dry_run=True)
        self.assertEqual(self.by_id(out, 'target')['gamma.app']['code'], 'no_callers')

    def test_the_operator_names_the_principal_a_caller_becomes(self):
        self.bindings = [binding('service:runner', 1, token=bearer('caller-a'))]
        self.run_import(principals={'caller-a': 'service:runner'})
        doc = self.engine()
        self.assertEqual(doc['targets']['alpha.app']['allowedCallers'], ['service:runner', 'agent:caller-b'])
        self.assertEqual(doc['callers']['service:runner']['bindingId'], 'bnd_import0001')

    def test_an_operator_proxy_comes_over_and_nothing_else_is_added(self):
        self.callers['caller-a']['operatorProxy'] = True
        self.write_source()
        self.run_import()
        grant = self.engine()['callers']['agent:caller-a']
        self.assertIs(grant['operatorProxy'], True)
        self.assertEqual(set(grant), set(S.GRANT_FIELDS) | {'operatorProxy'})

    def test_the_report_names_ids_and_counts_never_a_path_a_hash_or_a_team(self):
        self.targets['gamma.app'] = legacy('gamma.app', 'gamma')
        self.installed['/Applications/Gamma.app'] = (app_identity('com.example.gamma'), 'enrolled-gamma.app')
        self.write_source()
        text = json.dumps(self.run_import())
        for value in ('/Applications', TEAM, token_hash('caller-a'), 'enrolled-gamma.app', 'cdhash',
                      str(self.config), str(self.state)):
            self.assertNotIn(value, text)
        self.assertIn(str(self.source), text)                # the one path the operator gave

    def test_a_failed_write_leaves_the_document_and_the_journal_as_they_were(self):
        self.run_import()
        before = self.config.read_bytes()
        self.targets['gamma.app'] = source_target('gamma.app', 'gamma')
        self.write_source()
        with mock.patch.object(S, 'write_config', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_import()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(len(self.events()), 1)


class MigrationTests(Folder, unittest.TestCase):
    """A legacy app record is moved to the requirement scheme inside the import (ported, seven cases)."""

    def setUp(self):
        super().setUp()
        self.targets = {
            'alpha.app': legacy('alpha.app', 'alpha'),
            'switch.app': legacy('switch.app', 'switch', readiness={'kind': 'switchboard-runtime',
                                                                    'capability': 'runtime-online'}),
        }
        self.targets['switch.app']['identity'].update(bundleId=broker.SWITCHBOARD_BUNDLE_ID, readinessCliHash='cli')
        self.write_source()
        switch = app_identity(broker.SWITCHBOARD_BUNDLE_ID)
        switch['readinessCli'] = {'path': broker.SWITCHBOARD_CLI, 'identifier': 'com.example.switch-cli'}
        self.installed = {'/Applications/Alpha.app': (app_identity('com.example.alpha'), 'updated-alpha'),
                          '/Applications/Switch.app': (switch, 'updated-switch')}

    def test_the_source_registry_with_legacy_records_is_read(self):
        self.assertEqual(set(importer.read_source(self.source)['targets']), {'alpha.app', 'switch.app'})

    def test_legacy_records_migrate_under_a_trusted_team_with_a_policy_bump_once(self):
        out = self.run_import(trusted_teams=(TEAM,))
        doc = self.engine()
        self.assertEqual(doc['targets']['alpha.app']['identity'], app_identity('com.example.alpha'))
        self.assertEqual(doc['targets']['alpha.app']['policyRevision'], 3)
        self.assertEqual(doc['targets']['switch.app']['readiness'], {'kind': 'switchboard-runtime'})
        self.assertIn('identity migrated (trusted_team)', self.by_id(out, 'target')['alpha.app']['notes'])
        again = self.run_import(trusted_teams=(TEAM,))
        self.assertFalse(again['changed'])
        self.assertEqual(again['targets'], {'unchanged': 2})

    def test_an_updated_bundle_of_a_team_nobody_named_needs_reenrolment(self):
        out = self.run_import()
        alpha = self.by_id(out, 'target')['alpha.app']
        self.assertEqual((alpha['outcome'], alpha['code'], alpha['reason']),
                         ('refused', 'reenroll_required', 'untrusted_team'))
        self.assertNotIn('alpha.app', self.engine()['targets'])
        self.assertTrue(any('reenroll_required' in s for s in out['next']))

    def test_an_exact_enrolled_bundle_migrates_under_its_own_team(self):
        self.installed['/Applications/Alpha.app'] = (app_identity('com.example.alpha', OTHER_TEAM),
                                                     'enrolled-alpha.app')
        out = self.run_import()
        self.assertEqual(self.engine()['targets']['alpha.app']['identity']['teamId'], OTHER_TEAM)
        self.assertIn('identity migrated (exact_enrolled_bundle)', self.by_id(out, 'target')['alpha.app']['notes'])

    def test_a_changed_bundle_id_is_never_adopted(self):
        self.installed['/Applications/Alpha.app'] = (app_identity('com.example.other'), 'enrolled-alpha.app')
        alpha = self.by_id(self.run_import(trusted_teams=(TEAM,)), 'target')['alpha.app']
        self.assertEqual(alpha['reason'], 'bundle_id_changed')

    def test_an_unverifiable_install_is_refused_and_the_others_continue(self):
        self.installed['/Applications/Alpha.app'] = broker.LifecycleError('identity_mismatch')
        out = self.run_import(trusted_teams=(TEAM,))
        self.assertEqual(self.by_id(out, 'target')['alpha.app']['reason'], 'identity_mismatch')
        self.assertEqual(set(self.engine()['targets']), {'switch.app'})

    def test_a_malformed_derived_identity_is_refused_by_the_registry(self):
        bad = app_identity('com.example.alpha') | {'teamId': f'{TEAM}" or true'}
        self.installed['/Applications/Alpha.app'] = (bad, 'enrolled-alpha.app')
        self.assertEqual(self.by_id(self.run_import(), 'target')['alpha.app']['code'], 'invalid_registry')

    def test_each_bundle_is_verified_once_before_the_lock(self):
        calls = []

        def derive(path):
            calls.append(path)
            return self.derive(path)
        self.run_import(derive=derive, trusted_teams=(TEAM,))
        self.assertEqual(sorted(calls), ['/Applications/Alpha.app', '/Applications/Switch.app'])

    def test_a_legacy_record_without_a_usable_path_is_refused_not_raised(self):
        self.targets['alpha.app']['path'] = ['not', 'a', 'path']
        self.write_source()
        alpha = self.by_id(self.run_import(trusted_teams=(TEAM,)), 'target')['alpha.app']
        self.assertEqual((alpha['code'], alpha['reason']), ('reenroll_required', 'unreadable_bundle'))

    def test_off_macos_a_legacy_record_cannot_be_verified(self):
        derive = importer.default_derive(mock.Mock(platform='linux'))
        alpha = self.by_id(self.run_import(derive=derive), 'target')['alpha.app']
        self.assertEqual(alpha['reason'], 'unsupported_platform')


class CliTests(Folder, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(S, 'config_path', return_value=self.config),
                   mock.patch.object(S, 'state_dir', return_value=self.state),
                   mock.patch.object(lifecycle_cli, '_is_terminal', return_value=True),
                   mock.patch.object(lifecycle_cli, '_bindings_registry',
                                     side_effect=lambda: (self.registry(), AUDIENCE))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *argv) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = lifecycle_cli.main(list(argv))
            except SystemExit as exc:
                code = 1 if exc.code not in (0, None) else 0
        return code, out.getvalue(), err.getvalue()

    def test_an_import_needs_a_terminal_and_a_dry_run_does_not(self):
        with mock.patch.object(lifecycle_cli, '_is_terminal', return_value=False):
            self.assertEqual(self.cli('import', '--from', str(self.source))[0], 1)
            self.assertFalse(self.config.exists())
            code, out, _ = self.cli('import', '--from', str(self.source), '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('dry run, nothing written', out)
        self.assertFalse(self.config.exists())

    def test_the_import_prints_what_came_over_and_what_to_run_next(self):
        code, out, _ = self.cli('import', '--from', str(self.source))
        self.assertEqual(code, 0)
        self.assertIn('target alpha.app: imported', out)
        self.assertIn('caller caller-b: reissue_required (no_binding) as agent:caller-b', out)
        self.assertIn('full lifecycle status', out)
        code, out, _ = self.cli('import', '--from', str(self.source), '--json')
        self.assertEqual((code, json.loads(out)['changed']), (0, False))

    def test_a_refused_item_makes_the_exit_code_one(self):
        self.targets['gamma.app'] = source_target('gamma.app', 'gamma', startupEffects=[])
        self.write_source()
        code, out, _ = self.cli('import', '--from', str(self.source))
        self.assertEqual(code, 1)
        self.assertIn('target gamma.app: refused (startup_effects_required)', out)
        self.assertIn('alpha.app', self.engine()['targets'])

    def test_bad_arguments_and_an_unsafe_source_are_refused(self):
        self.assertEqual(self.cli('import', '--from', str(self.source), '--principal', 'caller-a=alpha')[0], 1)
        self.assertEqual(self.cli('import', '--from', str(self.source), '--trusted-team', 'short')[0], 1)
        os.chmod(self.source, 0o777)
        code, _, err = self.cli('import', '--from', str(self.source))
        self.assertEqual(code, 1)
        self.assertIn('unsafe_source', err)
        self.assertFalse(self.config.exists())

    def test_unreadable_bindings_are_degraded_and_every_grant_waits(self):
        with mock.patch.object(lifecycle_cli, '_bindings_registry', side_effect=AB.BindingError('broken')):
            code, out, _ = self.cli('import', '--from', str(self.source), '--json', '--dry-run')
        doc = json.loads(out)
        self.assertEqual(doc['degraded'][-1]['source'], 'bindings')
        self.assertEqual(self.by_id(doc, 'caller')['caller-a']['outcome'], 'reissue_required')


if __name__ == '__main__':
    unittest.main()
