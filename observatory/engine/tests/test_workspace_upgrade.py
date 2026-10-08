#!/usr/bin/env python3
"""Private synthetic workspace snapshots, forward upgrades and safe restores."""
from __future__ import annotations
import contextlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import configuration as config
import workspace
import workspace_upgrade as upgrade

class WorkspaceUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name).resolve()
        self.home=self.base/'private workspace'
        self.env=patch.dict(os.environ,{'OBSERVATORY_HOME':str(self.home)},clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        from store import db
        importlib.reload(db)
        self.db=db
        conn=db.connect()
        conn.execute("INSERT INTO events(id,kind,occurred_at,actor) VALUES ('synthetic-event','session','2026-01-01T00:00:00Z','fixture')")
        conn.commit()
        conn.close()
        (self.home/'secrets'/'demo-slot').write_text('synthetic opaque private value')
        (self.home/'store/logs/test.log').write_text('synthetic log')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def snapshot(self):
        result=upgrade.snapshot(self.home,writers_stopped=True)
        return Path(result['snapshot'])

    def test_missing_sqlite_extension_support_refuses_before_writes(self):
        class NoExtensions:
            def close(self):
                pass
        before = upgrade.inventory(self.home)
        untouched = self.base / 'refused-new-home'
        with patch.object(workspace.sqlite3, 'connect', return_value=NoExtensions()):
            for operation in (
                lambda: workspace.initialize(untouched),
                lambda: workspace.migrate_local(self.base / 'absent-source', untouched, True),
                lambda: workspace.doctor(self.home),
                lambda: upgrade.upgrade(self.home, apply=True, writers_stopped=True),
            ):
                with self.assertRaisesRegex(config.ConfigurationError, 'loadable extensions'):
                    operation()
        self.assertFalse(untouched.exists())
        self.assertEqual(before, upgrade.inventory(self.home))
        self.assertFalse((self.base / ('.' + self.home.name + '.observatory-operation.lock')).exists())

    def test_a_missing_full_extra_is_named_instead_of_the_interpreter(self):
        # A wheel installed without `[full]` on a perfectly good interpreter used to
        # hear "use Homebrew Python 3.14", which sends the user after the wrong fix.
        untouched = self.base / 'refused-new-home'
        absent = ['observatory-example-absent', 'mcp']
        with patch.object(workspace, 'FULL_DISTRIBUTIONS', absent):
            with self.assertRaises(config.ConfigurationError) as caught:
                workspace.initialize(untouched)
        message = str(caught.exception)
        self.assertIn('[full]', message)
        self.assertIn('missing: observatory-example-absent)', message, 'an installed distribution is not reported')
        self.assertIn('-m pip install', message)
        self.assertIn('requirements-full.lock', message)
        self.assertIn(sys.executable, message)
        self.assertNotIn('Homebrew', message)
        self.assertFalse(untouched.exists())

    def test_a_module_on_the_path_does_not_count_as_the_installed_extra(self):
        # The engine's own `mcp/` package is on sys.path; an import-based check found it and
        # called the absent MCP SDK installed (seen in a fresh venv without `[full]`).
        shadow = self.base / 'shadow'
        (shadow / 'observatory_shadow_example').mkdir(parents=True)
        (shadow / 'observatory_shadow_example' / '__init__.py').write_text('')
        with patch.object(sys, 'path', [str(shadow), *sys.path]), \
                patch.object(workspace, 'FULL_DISTRIBUTIONS', ['observatory_shadow_example']):
            with self.assertRaisesRegex(config.ConfigurationError, r'missing: observatory_shadow_example\)'):
                workspace.require_runtime()

    def test_an_interpreter_without_extensions_keeps_the_interpreter_advice(self):
        class NoExtensions:
            def close(self):
                pass
        with patch.object(workspace.sqlite3, 'connect', return_value=NoExtensions()), \
                patch.object(workspace, 'FULL_DISTRIBUTIONS', ['observatory-example-absent']):
            with self.assertRaises(config.ConfigurationError) as caught:
                workspace.doctor(self.home)
        message = str(caught.exception)
        self.assertIn('loadable extensions', message)
        self.assertIn('Homebrew', message)
        # Both faults are real; the second is named too, so one fix is not followed by another refusal.
        self.assertIn('observatory-example-absent', message)

    def test_preview_does_not_write(self):
        before=upgrade.inventory(self.home)
        result=upgrade.upgrade(self.home)
        self.assertEqual(result['status'],'preview')
        self.assertEqual(upgrade.inventory(self.home),before)
        self.assertFalse((self.base/('.'+self.home.name+'.observatory-operation.lock')).exists())

    def test_snapshot_requires_stopped_writers(self):
        with self.assertRaises(config.ConfigurationError):
            upgrade.snapshot(self.home)
        self.assertEqual(list((self.home/'backups').iterdir()),[])

    def test_snapshot_restore_preserves_private_files_and_modes(self):
        source=self.snapshot()
        dest=self.base/'restored instance'
        result=upgrade.restore(source,dest)
        self.assertEqual(result['status'],'restored')
        self.assertEqual((dest/'secrets/demo-slot').read_text(),'synthetic opaque private value')
        self.assertEqual((dest/'store/logs/test.log').read_text(),'synthetic log')
        self.assertFalse((dest/'backups').exists())
        for file in dest.rglob('*'):
            self.assertEqual(file.stat().st_mode & 0o777,0o700 if file.is_dir() else 0o600)
        with contextlib.closing(sqlite3.connect(dest/'store/observatory.db')) as conn:
            self.assertEqual(conn.execute('SELECT id FROM events').fetchone()[0],'synthetic-event')

    def test_backup_does_not_recurse(self):
        first=self.snapshot()
        second=self.snapshot()
        files=config.read_json(second/'manifest.json')['files']
        self.assertTrue(first.exists())
        self.assertFalse(any('backups/' in row['path'] for row in files))
        self.assertFalse(any(row['path'].endswith('.lock') for row in files))

    def test_restore_refuses_existing_data(self):
        source=self.snapshot()
        before=upgrade.inventory(self.home)
        with self.assertRaises(config.ConfigurationError):
            upgrade.restore(source,self.home)
        self.assertEqual(upgrade.inventory(self.home),before)

    def test_modified_snapshot_refused_before_destination_creation(self):
        source=self.snapshot()
        (source/'data/secrets/demo-slot').write_text('changed')
        dest=self.base/'never-created'
        with self.assertRaises(config.ConfigurationError):
            upgrade.restore(source,dest)
        self.assertFalse(dest.exists())

    def test_manifest_traversal_refused(self):
        source=self.snapshot()
        manifest=config.read_json(source/'manifest.json')
        manifest['files'][0]['path']='../../escape'
        workspace.write_json(source/'manifest.json',manifest)
        with self.assertRaises(config.ConfigurationError):
            upgrade.restore(source,self.base/'never-created')

    def test_snapshot_symlink_refused(self):
        (self.home/'secrets/link').symlink_to(self.base/'outside')
        with self.assertRaises(config.ConfigurationError):
            self.snapshot()

    def test_future_workspace_refused_before_writes(self):
        marker=config.read_json(self.home/'workspace.json')
        marker['minimum_writer']='99.0.0'
        workspace.write_json(self.home/'workspace.json',marker)
        before=upgrade.inventory(self.home)
        with self.assertRaises(config.ConfigurationError):
            upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertEqual(before,upgrade.inventory(self.home))
        self.assertFalse((self.base/('.'+self.home.name+'.observatory-operation.lock')).exists())

    def test_future_database_refused_before_writes(self):
        with contextlib.closing(sqlite3.connect(self.home/'store/observatory.db')) as conn:
            conn.execute('PRAGMA user_version=999')
        before=upgrade.inventory(self.home)
        with self.assertRaises(RuntimeError):
            self.snapshot()
        self.assertEqual(before,upgrade.inventory(self.home))

    def test_upgrade_adds_defaults_preserves_custom_values(self):
        setting=config.read_json(self.home/'config/settings.json')
        setting['features']['scheduler']=False
        setting['custom_extension']={'value':9}
        setting.pop('must_understand')
        workspace.write_json(self.home/'config/settings.json',setting)
        result=upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertEqual(result['status'],'upgraded')
        current=config.read_json(self.home/'config/settings.json')
        self.assertEqual(current['custom_extension'],{'value':9})
        self.assertFalse(current['features']['scheduler'])
        self.assertEqual(current['must_understand'],[])
        self.assertTrue(Path(result['snapshot']).exists())
        self.assertFalse((self.home/upgrade.JOURNAL).exists())

    def test_a_write_while_the_upgrade_is_staged_is_kept_not_refused(self):
        # 2026-10-07: twenty Claude Code sessions' Stop hooks and MCP servers write to the
        # store all the time; one write while the staged copy was migrated refused 0.19.0
        # ("Workspace changed while upgrade was staged") and the store would have been
        # replaced by the older copy had it gone through. The store is migrated in place.
        real = upgrade.prepare_upgrade

        def staged_while_a_session_writes(stage):
            out = real(stage)
            conn = self.db.connect()
            conn.execute("INSERT INTO events(id,kind,occurred_at,actor) VALUES "
                         "('written-during-staging','session','2026-01-02T00:00:00Z','fixture')")
            conn.commit()
            conn.close()
            with open(self.home/'store/logs/test.log','a') as log:
                log.write('a line appended while staging\n')
            return out
        with patch.object(upgrade,'prepare_upgrade',side_effect=staged_while_a_session_writes):
            result=upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertEqual(result['status'],'upgraded')
        self.assertTrue(result['store_migrated_in_place'])
        conn=self.db.connect()
        try:
            ids={r[0] for r in conn.execute("SELECT id FROM events")}
        finally:
            conn.close()
        self.assertIn('written-during-staging',ids,'a write made while staging survives')
        self.assertIn('synthetic-event',ids)

    def test_the_session_hooks_receipts_may_change_while_staged(self):
        # The Stop hook rewrites store/raw/record-turn.json on every agent turn, the
        # SessionStart hook appends to sessions-seen.jsonl, a hook fault to
        # companion-faults.jsonl — none of them stops for an update. Before 0.19.2
        # any of these refused the update as "Workspace changed while staged".
        real = upgrade.prepare_upgrade

        def staged_while_hooks_run(stage):
            out = real(stage)
            raw = self.home / 'store/raw'
            raw.mkdir(parents=True, exist_ok=True)
            for name in sorted(upgrade.SESSION_RECEIPTS):
                with open(self.home / name, 'a') as receipt:
                    receipt.write('{"written": "while staging"}\n')
            return out
        with patch.object(upgrade, 'prepare_upgrade', side_effect=staged_while_hooks_run):
            result = upgrade.upgrade(self.home, apply=True, writers_stopped=True)
        self.assertEqual(result['status'], 'upgraded')
        for name in upgrade.SESSION_RECEIPTS:
            self.assertIn('while staging', (self.home / name).read_text(), f'{name} kept its write')

    def test_every_receipt_a_session_hook_writes_is_exempt(self):
        import re
        engine = Path(upgrade.__file__).resolve().parent
        written = set()
        for source in ('tools/record_turn.py', 'tools/session_start.py', 'companion_faults.py'):
            text = (engine / source).read_text(encoding='utf-8')
            written |= {'store/raw/' + n for n in re.findall(r'paths\.SCRATCH / "([^"]+)"', text)}
        # record_turn READS vault.json (the tick writes it); everything else named is written.
        written.discard('store/raw/vault.json')
        self.assertTrue(written)
        self.assertEqual(written - set(upgrade.SESSION_RECEIPTS), set())

    def test_a_settings_file_changed_while_staged_still_refuses(self):
        # What must hold still still must: only the databases and the logs are exempt.
        real = upgrade.prepare_upgrade

        def staged_while_settings_change(stage):
            out = real(stage)
            (self.home/'secrets'/'demo-slot').write_text('a different synthetic value')
            return out
        with patch.object(upgrade,'prepare_upgrade',side_effect=staged_while_settings_change):
            with self.assertRaises(config.ConfigurationError):
                upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertFalse((self.home/upgrade.JOURNAL).exists())

    def test_a_failed_live_migration_puts_the_files_back(self):
        setting=config.read_json(self.home/'config/settings.json')
        setting.pop('must_understand')
        workspace.write_json(self.home/'config/settings.json',setting)
        before=config.read_json(self.home/'config/settings.json')
        calls=[]
        real=upgrade._migrate

        def fail_live(home):
            calls.append(home)
            return real(home) if home != self.home else 'synthetic live migration failure'
        with patch.object(upgrade,'_migrate',side_effect=fail_live):
            with self.assertRaises(config.ConfigurationError):
                upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertEqual(calls[-1],self.home)
        self.assertEqual(config.read_json(self.home/'config/settings.json'),before)
        self.assertFalse((self.home/upgrade.JOURNAL).exists())

    def test_staging_failure_leaves_original_unchanged(self):
        before=upgrade.inventory(self.home)
        with patch.object(upgrade,'prepare_upgrade',side_effect=RuntimeError('synthetic migration failure')):
            with self.assertRaises(RuntimeError):
                upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertEqual(before,upgrade.inventory(self.home))
        self.assertFalse((self.home/upgrade.JOURNAL).exists())
        self.assertEqual(len(list((self.home/'backups').glob('before-upgrade-*'))),1)

    def test_commit_failure_rolls_back_previous_files(self):
        setting=config.read_json(self.home/'config/settings.json')
        setting.pop('must_understand')
        workspace.write_json(self.home/'config/settings.json',setting)
        before=upgrade.inventory(self.home)
        original=upgrade.os.replace
        fired=[]
        def fault(source,destination):
            if Path(destination)==self.home/'workspace.json' and '.upgrade-' in str(source) and not fired:
                fired.append(True)
                raise OSError('synthetic publish fault')
            return original(source,destination)
        with patch.object(upgrade.os,'replace',side_effect=fault):
            with self.assertRaises(OSError):
                upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertTrue(fired)
        self.assertEqual(before,upgrade.inventory(self.home))
        self.assertFalse((self.home/upgrade.JOURNAL).exists())

    def test_workspace_lock_refuses_backup(self):
        with workspace.lock(self.home):
            with self.assertRaises(config.ConfigurationError):
                self.snapshot()

    def test_snapshot_retains_committed_wal_data(self):
        conn=sqlite3.connect(self.home/'store/observatory.db')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            conn.execute("UPDATE events SET actor='committed-wal'")
            conn.commit()
            source=self.snapshot()
            with contextlib.closing(sqlite3.connect(source/'data/store/observatory.db')) as copy:
                self.assertEqual(copy.execute('SELECT actor FROM events').fetchone()[0],'committed-wal')
        finally:
            conn.close()

    def test_future_snapshot_floor_refused(self):
        source=self.snapshot()
        manifest=config.read_json(source/'manifest.json')
        manifest['minimum_writer']='99.0.0'
        workspace.write_json(source/'manifest.json',manifest)
        dest=self.base/'future-refused'
        with self.assertRaises(config.ConfigurationError):
            upgrade.restore(source,dest)
        self.assertFalse(dest.exists())

    def test_compatible_newer_producer_is_not_an_automatic_break(self):
        source=self.snapshot()
        manifest=config.read_json(source/'manifest.json')
        manifest['application_version']='0.2.1'
        workspace.write_json(source/'manifest.json',manifest)
        upgrade.restore(source,self.base/'compatible-restore')

    def test_snapshot_extra_file_refused(self):
        source=self.snapshot()
        (source/'data/unlisted').write_text('unexpected')
        with self.assertRaises(config.ConfigurationError):
            upgrade.verify_snapshot(source)

    def test_snapshot_symlink_target_refused(self):
        source=self.snapshot()
        file=source/'data/secrets/demo-slot'
        file.unlink()
        file.symlink_to(self.home/'secrets/demo-slot')
        with self.assertRaises(config.ConfigurationError):
            upgrade.verify_snapshot(source)

    def test_external_runtime_override_refused(self):
        with patch.dict(os.environ,{'OBSERVATORY_DB':str(self.base/'external.db')}):
            with self.assertRaises(config.ConfigurationError):
                upgrade.upgrade(self.home)
        self.assertFalse((self.base/'external.db').exists())

    def test_interrupted_upgrade_marker_refuses_further_work(self):
        workspace.write_json(self.home/upgrade.JOURNAL,{'application_version':config.VERSION})
        with self.assertRaises(config.ConfigurationError):
            upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertTrue((self.home/upgrade.JOURNAL).exists())

    def test_tick_lock_refuses_backup(self):
        import fcntl
        fd=os.open(self.home/'store/tick.lock',os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(config.ConfigurationError):
                self.snapshot()
        finally:
            os.close(fd)

    def test_empty_runtime_directories_survive_restore(self):
        (self.home/'store/empty-runtime').mkdir()
        source=self.snapshot()
        dest=self.base/'restore-empty-dirs'
        upgrade.restore(source,dest)
        self.assertTrue((dest/'store/empty-runtime').is_dir())
        self.assertTrue((dest/'docs/dashboard').is_dir())

    def moving_copy(self, change):
        original=workspace.copy_private
        mutated=[]
        def moving(source,destination):
            result=original(source,destination)
            if not mutated:
                mutated.append(True)
                change()
            return result
        return mutated, patch.object(workspace,'copy_private',side_effect=moving)

    def test_source_movement_aborts_snapshot(self):
        mutated, copying = self.moving_copy(lambda: (self.home/'secrets'/'demo-slot').write_text('a writer moved this'))
        with copying:
            with self.assertRaises(config.ConfigurationError):
                self.snapshot()
        self.assertTrue(mutated)
        self.assertEqual(list((self.home/'backups').iterdir()),[])

    def test_logs_and_databases_written_during_a_snapshot_do_not_abort_it(self):
        # Audit A11: session MCP servers and the Stop hook append to logs and write the
        # store while the tick and server are stopped. Both are copied safely (the store
        # through the backup API, a log up to the line being written), so they need not hold still.
        def writers():
            with open(self.home/'store/logs/test.log','a') as f:
                f.write('\nappended by a session')
            (self.home/'store/logs/test.log.1').write_text('a rotated generation')
            conn=self.db.connect()
            conn.execute("INSERT INTO events(id,kind,occurred_at,actor) VALUES ('during','session','2026-01-01T00:00:01Z','fixture')")
            conn.commit()
            conn.close()
        mutated, copying = self.moving_copy(writers)
        with copying:
            snap=self.snapshot()
        self.assertTrue(mutated)
        self.assertTrue(snap.exists())

    def test_what_counts_as_volatile(self):
        dbs={'store/observatory.db'}
        for name, expected in (('store/observatory.db', True), ('store/observatory.db-wal', True),
                               ('store/observatory.db-shm', True), ('store/logs/update.jsonl', True),
                               ('store/logs/tick.log.3', True), ('store/logs/update-apply.err', True),
                               ('store/other.db-wal', False), ('config/settings.json', False),
                               ('store/logs/notes.txt', False), ('store/logs.log', False),
                               ('secrets/demo-slot', False)):
            self.assertEqual(upgrade.volatile(name, dbs), expected, name)

    def test_vector_database_integrity_when_extension_installed(self):
        try:
            import sqlite_vec
        except ImportError:
            self.skipTest('sqlite-vec unavailable in this interpreter')
        source=self.home/'store/vector-fixture.db'
        with contextlib.closing(sqlite3.connect(source)) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute('CREATE VIRTUAL TABLE vectors USING vec0(embedding float[2])')
            conn.execute("INSERT INTO vectors(rowid,embedding) VALUES (1,'[1.0,2.0]')")
            conn.commit()
        snapshot=self.snapshot()
        upgrade.verify_snapshot(snapshot)
        self.assertTrue((snapshot/'data/store/vector-fixture.db').exists())

if __name__=='__main__':
    unittest.main()
