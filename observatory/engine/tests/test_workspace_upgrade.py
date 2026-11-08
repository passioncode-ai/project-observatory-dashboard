#!/usr/bin/env python3
"""Private synthetic workspace snapshots, forward upgrades and safe restores."""
from __future__ import annotations
import contextlib
import importlib
import io
import json
from pathlib import PurePosixPath
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import configuration as config
import workspace
import workspace_upgrade as upgrade
sys.path.insert(0,str(Path(__file__).resolve().parent))
from secret_store_fixture import VALUES, all_values, make_external_store  # noqa: E402

PASSPHRASE='correct horse battery staple'

#: The machine a snapshot is taken on, and another one it is restored on (OBS-284).
ALPHA,BETA='m-machine-alpha-0001','m-machine-beta-0002'

class WorkspaceUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name).resolve()
        self.home=self.base/'private workspace'
        self.env=patch.dict(os.environ,{'OBSERVATORY_HOME':str(self.home),'OBSERVATORY_MACHINE_ID':ALPHA,'OBSERVATORY_SYSTEM_SETUP':'0'},clear=True)
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

    # OBS-389: an explicit --output is a plaintext folder; a workspace that holds secrets refuses it
    # unless the caller asks for the plaintext copy by name.
    def test_an_explicit_output_refuses_a_workspace_that_holds_secrets(self):
        out=self.base/'plain copy'
        with self.assertRaises(config.ConfigurationError) as caught:
            upgrade.snapshot(self.home,out,writers_stopped=True)
        message=str(caught.exception)
        self.assertIn('secrets',message)
        self.assertIn('without --output',message,'the encrypted way forward is named')
        self.assertIn('--plaintext',message,'the explicit opt-in is named')
        self.assertNotIn('synthetic opaque private value',message)
        self.assertFalse(out.exists(),'nothing is written before the refusal')
        self.assertEqual(list((self.home/'backups').iterdir()),[])

    def test_an_update_snapshot_needs_the_opt_in_too(self):
        out=self.home/'backups'/'before-update-refused'
        with self.assertRaises(config.ConfigurationError):
            upgrade.snapshot(self.home,out,writers_stopped=True,update=True)
        self.assertFalse(out.exists())

    def test_the_plaintext_opt_in_keeps_the_copy_and_says_so(self):
        out=self.base/'plain copy'
        result=upgrade.snapshot(self.home,out,writers_stopped=True,plaintext=True)
        self.assertEqual((result['encrypted'],result['plaintext']),(False,True))
        self.assertTrue(any('secrets' in line and 'plaintext' in line for line in result['warnings']),result)
        self.assertEqual((out/'data/secrets/demo-slot').read_text(),'synthetic opaque private value')
        self.assertEqual(stat.S_IMODE((out/'data/secrets/demo-slot').stat().st_mode),0o600)
        self.assertNotIn('synthetic opaque private value',json.dumps(result))

    def test_without_secrets_an_explicit_output_is_as_before(self):
        (self.home/'secrets/demo-slot').unlink()
        (self.home/'secrets/.vault.lock').write_text('')   # a lock or a temporary is not a secret
        out=self.base/'plain copy'
        result=upgrade.snapshot(self.home,out,writers_stopped=True)
        self.assertEqual(result['encrypted'],False)
        self.assertNotIn('warnings',result)
        self.assertTrue((out/'manifest.json').is_file())

    def test_the_nodes_private_keys_count_with_no_vault_inside(self):
        # Review of OBS-389: with the vault outside the home, the device, organization and owner keys
        # under node/ were still copied in plaintext with no refusal.
        (self.home/'secrets/demo-slot').unlink()
        for rel in ('node/device/sig.pem','node/orgs/demo-org/keys/kem.pem','node/shares/owner/sig.pem',
                    'node/shares/sent/share-1/kem.pem','node/shares/received/share-2/sig.pem',
                    'node/relay/spool.key'):
            key=self.home/rel
            key.parent.mkdir(parents=True,exist_ok=True)
            key.write_text('synthetic key material')
            self.assertIn('node',upgrade.secret_stores_inside(self.home),rel)
            key.unlink()
        for name in upgrade.STATE_SECRET_FILES:
            key=self.home/'store'/name
            existed=key.exists()
            if not existed:
                key.write_text('synthetic key material')
            self.assertIn(f'store/{name}',upgrade.secret_stores_inside(self.home),name)
            if not existed:
                key.unlink()
        (self.home/'node/shares/owner/sig.pem').write_text('synthetic key material')
        out=self.base/'plain copy'
        with self.assertRaisesRegex(config.ConfigurationError,'node'):
            upgrade.snapshot(self.home,out,writers_stopped=True)
        self.assertFalse(out.exists())

    def test_unreadable_settings_fail_closed(self):
        (self.home/'secrets/demo-slot').unlink()
        (self.home/'config/settings.json').write_text('{not json')
        self.assertTrue(any('unreadable' in s for s in upgrade.secret_stores_inside(self.home)))

    def test_a_store_the_settings_put_inside_the_workspace_counts(self):
        (self.home/'secrets/demo-slot').unlink()
        inner=self.home/'vault-inside'/'projects'/'alpha'/'prod'
        inner.mkdir(parents=True)
        (inner/'API_KEY').write_text('synthetic opaque private value')
        file=self.home/'config/settings.json'
        doc=config.read_json(file)
        doc.setdefault('sources',{})['secret_store']=str(self.home/'vault-inside')
        workspace.write_json(file,doc)
        with self.assertRaisesRegex(config.ConfigurationError,'vault-inside'):
            upgrade.snapshot(self.home,self.base/'plain copy',writers_stopped=True)

    def test_the_cli_refuses_and_takes_the_opt_in(self):
        out=self.base/'cli copy'
        err=io.StringIO()
        with patch.object(sys,'stderr',err),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upgrade.main(['backup','--output',str(out),'--writers-stopped']),2)
        self.assertIn('--plaintext',err.getvalue())
        self.assertFalse(out.exists())
        printed=io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(upgrade.main(['backup','--output',str(out),'--writers-stopped','--plaintext']),0)
        self.assertTrue(json.loads(printed.getvalue())['plaintext'])
        err=io.StringIO()
        with patch.object(sys,'stderr',err),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upgrade.main(['backup','--writers-stopped','--plaintext']),2)
        self.assertIn('--output',err.getvalue())
        backup=upgrade.parser()._subparsers._group_actions[0].choices['backup']
        self.assertIn('--plaintext',backup.format_help())

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
        for source in ('tools/record_turn.py', 'tools/session_start.py', 'companion_faults.py', 'why_asks.py'):
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
            with self.assertRaises(config.ConfigurationError) as refused:
                upgrade.upgrade(self.home,apply=True,writers_stopped=True)
        self.assertFalse((self.home/upgrade.JOURNAL).exists())
        # The refusal names what moved, so the writer can be found (0.22.0, 2026-10-11).
        self.assertIn('secrets/demo-slot', str(refused.exception))
        self.assertNotIn('a different synthetic value', str(refused.exception))

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
        import oslocks
        fd=os.open(self.home/'store/tick.lock',os.O_RDWR|os.O_CREAT,0o600)
        try:
            oslocks.flock(fd,oslocks.LOCK_EX|oslocks.LOCK_NB)
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

    # --- OBS-284: a snapshot restored on another machine ------------------------------------

    def identity(self):
        """This machine's agent-network identity, enrollment, verified estate state and schedule."""
        for rel,text in (('node/device/device.json','{"device": "synthetic"}'),
                         ('node/orgs/org_example/enrollment.json','{"org": "synthetic"}'),
                         ('node/orgs/org_example/keys/sig.pem','synthetic key material'),
                         ('node/records/estate-record.json','{"revision": 1}'),
                         ('node/shares/owner/note.txt','kept'),
                         ('store/maintenance.json','{"last": "synthetic"}')):
            path=self.home/rel
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            path.write_text(text)
            path.chmod(0o600)
        settings=config.read_json(self.home/'config/settings.json')
        settings.setdefault('features',{})['scheduler']=True
        workspace.write_json(self.home/'config/settings.json',settings)

    def test_a_snapshot_names_the_machine_it_was_taken_on(self):
        self.assertEqual(config.read_json(self.snapshot()/'manifest.json')['machine_id'],ALPHA)

    def test_another_machine_never_takes_the_node_identity_or_the_schedules(self):
        self.identity()
        source=self.snapshot()
        dest=self.base/'second machine'
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA}):
            result=upgrade.restore(source,dest)
        for rel in ('node/device','node/orgs','node/records','store/maintenance.json'):
            self.assertFalse((dest/rel).exists(),rel)
        self.assertFalse((dest/'node/shares').exists(),'shares are sealed to the machine that holds them')
        self.assertEqual((dest/'secrets/demo-slot').read_text(),'synthetic opaque private value','the vault moves')
        self.assertFalse(config.read_json(dest/'config/settings.json')['features']['scheduler'])
        self.assertNotEqual(config.read_json(dest/'workspace.json')['instance_id'],
                            config.read_json(self.home/'workspace.json')['instance_id'],
                            'its own backup label, so the two machines never rotate each other\'s backups')
        machine=result['machine']
        self.assertEqual((machine['mode'],machine['why'],machine['taken_on'],machine['this_machine']),
                         ('second-machine','another-machine',ALPHA,BETA))
        self.assertEqual(machine['left_out'],['node/device/','node/orgs/','node/records/','node/shares/','store/maintenance.json'])
        self.assertIn('turned off',machine['scheduler'])
        self.assertIn('project-observatory full node --init',machine['next'][0])
        manifest=config.read_json(source/'manifest.json')
        self.assertEqual(result['files'],len(manifest['files'])-6)
        for file in dest.rglob('*'):
            self.assertEqual(file.stat().st_mode & 0o777,0o700 if file.is_dir() else 0o600,str(file))
        upgrade.preflight(dest)

    def test_the_mailbox_and_always_on_apps_stay_with_their_machine(self):
        self.identity()
        workspace.write_json(self.home/'config/cloud.json',{'mailbox':'synthetic'})
        workspace.write_json(self.home/upgrade.LIFECYCLE,{'targets':{
            'app.always':{'desiredState':'always_on','policyRevision':3},
            'app.ondemand':{'desiredState':'on_demand','policyRevision':1}}})
        source=self.snapshot()
        dest=self.base/'second machine'
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA}):
            result=upgrade.restore(source,dest)
        self.assertFalse((dest/'config/cloud.json').exists(),'the second machine deploys its own mailbox')
        self.assertIn('config/cloud.json',result['machine']['left_out'])
        targets=config.read_json(dest/upgrade.LIFECYCLE)['targets']
        self.assertEqual(targets['app.always'],{'desiredState':'held_off','policyRevision':4},
                         'held off with a new revision, so the supervisor does not start it on two machines')
        self.assertEqual(targets['app.ondemand'],{'desiredState':'on_demand','policyRevision':1})
        self.assertEqual(result['machine']['held_off'],['app.always'])
        self.assertIn('project-observatory full lifecycle policy app.always --desired always_on',
                      ' '.join(result['machine']['next']))

    def test_second_machine_is_asked_anywhere(self):
        self.identity()
        source=self.snapshot()
        result=upgrade.restore(source,self.base/'asked',second_machine=True)
        self.assertEqual((result['machine']['mode'],result['machine']['why']),('second-machine','asked'))
        self.assertFalse((self.base/'asked'/'node/device').exists())
        import io
        dest=self.base/'asked by flag'
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(dest)}),patch.object(sys,'stdin',io.StringIO('')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upgrade.main(['restore',str(source),'--second-machine']),0,'no terminal needed')
        self.assertFalse((dest/'node/device').exists())

    def test_init_restores_a_snapshot_naming_no_machine_whole(self):
        self.identity()
        source=self.snapshot()
        manifest=config.read_json(source/'manifest.json')
        del manifest['machine_id']
        workspace.write_json(source/'manifest.json',manifest)
        dest=self.base/'init'
        result=upgrade.restore(source,dest,unknown='keep')
        self.assertEqual((result['machine']['mode'],result['machine']['why']),('unknown','names-no-machine'))
        self.assertTrue((dest/'node/device/device.json').is_file(),'an older snapshot keeps its identity at init')

    def test_then_node_init_makes_this_machines_own_device_id(self):
        try:
            from node.device_keys import DeviceKeys
            first=DeviceKeys.load_or_create(self.home/'node'/'device')
        except RuntimeError as exc:
            self.skipTest(f'the full extra is not installed: {exc}')
        source=self.snapshot()
        dest=self.base/'second machine'
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA}):
            upgrade.restore(source,dest)
        own=DeviceKeys.load_or_create(dest/'node'/'device')
        self.assertNotEqual(own.device_id,first.device_id)

    def test_another_machine_never_takes_the_analytics_install_id_or_queue(self):
        """OBS-324: a second machine makes its own install id and never resends the first one's queue; the
        person's switch (the file `analytics` in the home) travels with the workspace."""
        for rel,text in (('store/analytics/install-id','00000000-0000-4000-8000-000000000000\n'),
                         ('store/analytics/queue.jsonl','{"n": "command_run"}\n'),
                         ('analytics','off\n')):
            path=self.home/rel
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            path.write_text(text)
            path.chmod(0o600)
        source=self.snapshot()
        dest=self.base/'second machine'
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA}):
            result=upgrade.restore(source,dest)
        self.assertFalse((dest/'store/analytics').exists())
        self.assertIn('store/analytics/',result['machine']['left_out'])
        self.assertEqual((dest/'analytics').read_text(),'off\n','the person\'s choice moves with the workspace')

    def test_the_same_machine_restores_the_whole_snapshot_as_before(self):
        self.identity()
        source=self.snapshot()
        dest=self.base/'same machine'
        result=upgrade.restore(source,dest)
        for rel in ('node/device/device.json','node/orgs/org_example/keys/sig.pem','node/records/estate-record.json',
                    'store/maintenance.json'):
            self.assertTrue((dest/rel).is_file(),rel)
        self.assertTrue(config.read_json(dest/'config/settings.json')['features']['scheduler'])
        self.assertEqual(config.read_json(dest/'workspace.json'),config.read_json(self.home/'workspace.json'))
        self.assertEqual((result['machine']['mode'],result['files']),
                         ('same-machine',len(config.read_json(source/'manifest.json')['files'])))
        self.assertNotIn('left_out',result['machine'])

    def test_a_snapshot_naming_no_machine_is_another_machines_unless_asked(self):
        self.identity()
        source=self.snapshot()
        manifest=config.read_json(source/'manifest.json')
        del manifest['machine_id']
        workspace.write_json(source/'manifest.json',manifest)
        dest=self.base/'old snapshot'
        result=upgrade.restore(source,dest)
        self.assertEqual((result['machine']['mode'],result['machine']['why']),('second-machine','names-no-machine'))
        self.assertFalse((dest/'node/device').exists())
        whole=self.base/'old snapshot, whole'
        result=upgrade.restore(source,whole,same_machine=True)
        self.assertEqual((result['machine']['mode'],result['machine']['why']),('same-machine','asked'))
        self.assertTrue((whole/'node/device/device.json').is_file())

    def test_same_machine_is_a_choice_at_a_terminal(self):
        source=self.snapshot()
        dest=self.base/'refused'
        import io
        err=io.StringIO()
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(dest)}),patch.object(sys,'stdin',io.StringIO('')), \
                contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upgrade.main(['restore',str(source),'--same-machine']),2)
        self.assertIn('a terminal',err.getvalue())
        self.assertFalse(dest.exists())
        with patch.object(upgrade.osprivacy,'is_terminal',return_value=True):
            with patch.dict(os.environ,{'OBSERVATORY_HOME':str(dest),'OBSERVATORY_MACHINE_ID':BETA}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(upgrade.main(['restore',str(source),'--same-machine']),0)
        self.assertTrue((dest/'workspace.json').is_file())

    def test_a_second_machine_never_takes_the_snapshots_backup_passphrase(self):
        """OBS-348 (D2): each machine keeps its own backup passphrase; a second machine makes its own."""
        import backup_vault
        backup_vault.set_passphrase(self.home,'a synthetic passphrase of the first machine')
        source=self.base/'explicit snapshot'
        upgrade.snapshot(self.home,source,writers_stopped=True,plaintext=True)
        dest=self.base/'second machine'
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA}):
            result=upgrade.restore(source,dest)
        self.assertFalse((dest/'secrets/backup-passphrase').exists())
        self.assertIn('secrets/backup-passphrase',result['machine']['left_out'])
        # Until this machine makes its own, every snapshot stays unencrypted: the answer names the step.
        self.assertTrue(any(step.startswith('project-observatory full maintain ensure ')
                            for step in result['machine']['next']),result['machine']['next'])
        # The first machine's passphrase no longer travels: a fixed sentence says to keep it, never a value.
        self.assertEqual(result['machine']['notes'],[upgrade.OTHER_PASSPHRASE_NOTE])
        self.assertNotIn('a synthetic passphrase of the first machine',json.dumps(result))
        with patch.dict(os.environ,{'OBSERVATORY_BACKUPS':str(self.base/'roots')}):
            report=backup_vault.status(dest)
        self.assertEqual((report['encrypted'],report['passphrase']),(False,'missing'))
        self.assertIn('project-observatory full maintain ensure',' '.join(report['warnings']))
        self.assertEqual((dest/'secrets/demo-slot').read_text(),'synthetic opaque private value','the vault moves')
        same=self.base/'same machine'
        upgrade.restore(source,same)
        self.assertEqual((same/'secrets/backup-passphrase').read_bytes(),(self.home/'secrets/backup-passphrase').read_bytes(),
                         'a same-machine restore keeps the snapshot\'s passphrase as before')

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


class HomeRelativeSources(unittest.TestCase):
    """OBS-347: a workspace restored under another home folder still finds its sources, its backups
    root and its declared folders, and the answer says what it rewrote and what it could not find."""
    setUp=WorkspaceUpgrade.setUp
    tearDown=WorkspaceUpgrade.tearDown
    snapshot=WorkspaceUpgrade.snapshot

    def homes(self):
        self.ann,self.bob=self.base/'Users'/'ann',self.base/'Users'/'bob'
        for home in (self.ann,self.bob):
            home.mkdir(parents=True)
        return self.ann,self.bob

    def settings(self,sources=None,backups=None):
        file=self.home/'config/settings.json'
        doc=config.read_json(file)
        doc.setdefault('sources',{}).update(sources or {})
        if backups:
            doc.setdefault('storage',{})['backups']=backups
        workspace.write_json(file,doc)

    def snapshot_as(self,home):
        with patch.dict(os.environ,{'HOME':str(home),'USERPROFILE':str(home)}):
            return self.snapshot()

    def restore_as(self,home,source,dest):
        with patch.dict(os.environ,{'HOME':str(home),'USERPROFILE':str(home)}):
            return upgrade.restore(source,dest)

    def test_a_snapshot_names_its_home(self):
        ann,_=self.homes()
        self.assertEqual(config.read_json(self.snapshot_as(ann)/'manifest.json')['home_path'],str(ann))

    def test_restore_under_another_home_rewrites_settings_backup_and_tracked_paths(self):
        ann,bob=self.homes()
        self.settings({'secret_store':str(ann/'.config/example/secrets'),'projects':str(ann/'code'),
                       'harness_root':str(ann),'wiki':'~/notes'},backups=str(ann/'Backups'))
        workspace.write_json(self.home/'config/machine-backup.json',
                             {'include':[str(ann/'.zshrc'),'~/.gitconfig'],'roots':[str(ann/'code')],'extra':'kept'})
        workspace.write_json(self.home/'config/tracked.json',
                             {'schema_version':1,'local_folders':[{'path':str(ann/'src/alpha'),'by':'operator','future':1}],
                              'dismissed_local_folders':[{'path':str(ann/'src/old')}]})
        source=self.snapshot_as(ann)
        for rel in ('.config/example/secrets','src/alpha','Backups','notes'):
            (bob/rel).mkdir(parents=True)
        dest=self.base/'restored for bob'
        result=self.restore_as(bob,source,dest)
        paths=result['paths']
        self.assertEqual((paths['home_from'],paths['home_to']),(str(ann),str(bob)))
        doc=config.read_json(dest/'config/settings.json')
        self.assertEqual(doc['sources'],{'secret_store':'~/.config/example/secrets','projects':'~/code',
                                         'harness_root':'~','wiki':'~/notes'})
        self.assertEqual(doc['storage']['backups'],'~/Backups')
        machine=config.read_json(dest/'config/machine-backup.json')
        self.assertEqual(machine,{'include':['~/.zshrc','~/.gitconfig'],'roots':['~/code'],'extra':'kept'})
        tracked=config.read_json(dest/'config/tracked.json')
        self.assertEqual(tracked['local_folders'],[{'path':str(bob/'src/alpha'),'by':'operator','future':1}],
                         'a declared folder stays absolute, under the new home, with its other fields')
        self.assertEqual(tracked['dismissed_local_folders'],[{'path':str(bob/'src/old')}])
        rewritten={(row['file'],row['setting']):row['to'] for row in paths['rewritten']}
        self.assertEqual(rewritten[('config/settings.json','sources.secret_store')],'~/.config/example/secrets')
        self.assertEqual(rewritten[('config/settings.json','storage.backups')],'~/Backups')
        self.assertEqual(rewritten[('config/machine-backup.json','roots[0]')],'~/code')
        self.assertEqual(rewritten[('config/tracked.json','local_folders[0].path')],str(bob/'src/alpha'))
        self.assertNotIn(('config/settings.json','sources.wiki'),rewritten,'a home-relative value is left as it is')
        with patch.dict(os.environ,{'HOME':str(bob)}):
            for row in paths['rewritten']:
                if row['file']=='config/settings.json':
                    self.assertTrue(str(Path(row['to']).expanduser()).startswith(str(bob)),row)
        # `code` was not made under bob's home: it is named, with the step that points it elsewhere.
        self.assertEqual([(r['setting'],r['reason'],r['fix']) for r in paths['unresolved']],
                         [('sources.projects','missing-after-restore',
                           'project-observatory full configure sources projects PATH')])
        for file in dest.rglob('*'):
            self.assertEqual(file.stat().st_mode & 0o777,0o700 if file.is_dir() else 0o600,str(file))

    def test_a_value_outside_the_old_home_is_kept_and_named_when_missing(self):
        ann,bob=self.homes()
        volume=self.base/'Volumes'/'Work'/'src'
        self.settings({'projects':str(volume)},backups=str(self.base/'Volumes'/'Backups'))
        source=self.snapshot_as(ann)
        result=self.restore_as(bob,source,self.base/'restored')
        self.assertEqual(result['paths']['rewritten'],[])
        doc=config.read_json(self.base/'restored/config/settings.json')
        self.assertEqual(doc['sources']['projects'],str(volume))
        unresolved={r['setting']:r for r in result['paths']['unresolved']}
        self.assertEqual(unresolved['sources.projects'],
                         {'file':'config/settings.json','setting':'sources.projects','value':str(volume),'exists':False,
                          'reason':'missing-after-restore','fix':'project-observatory full configure sources projects PATH'})
        self.assertEqual(unresolved['storage.backups']['fix'],'project-observatory full configure storage backups PATH')

    def test_a_prefix_by_string_only_is_not_rewritten(self):
        ann,bob=self.homes()
        anna=self.base/'Users'/'anna'
        self.settings({'projects':str(anna/'src')})
        result=self.restore_as(bob,self.snapshot_as(ann),self.base/'restored')
        self.assertEqual(config.read_json(self.base/'restored/config/settings.json')['sources']['projects'],str(anna/'src'))
        self.assertEqual(result['paths']['rewritten'],[])

    def test_the_same_home_restores_settings_byte_for_byte(self):
        ann,_=self.homes()
        self.settings({'projects':str(ann/'src')})
        (ann/'src').mkdir()
        before=(self.home/'config/settings.json').read_bytes()
        result=self.restore_as(ann,self.snapshot_as(ann),self.base/'restored')
        self.assertEqual((self.base/'restored/config/settings.json').read_bytes(),before)
        self.assertEqual((result['paths']['rewritten'],result['paths']['unresolved']),([],[]))

    def test_an_older_snapshot_without_a_home_rewrites_nothing_and_names_what_is_missing(self):
        ann,bob=self.homes()
        self.settings({'projects':str(ann/'src')})
        source=self.snapshot_as(ann)
        manifest=config.read_json(source/'manifest.json')
        del manifest['home_path']
        workspace.write_json(source/'manifest.json',manifest)
        result=self.restore_as(bob,source,self.base/'restored')
        self.assertIsNone(result['paths']['home_from'])
        self.assertEqual(result['paths']['rewritten'],[])
        self.assertEqual(config.read_json(self.base/'restored/config/settings.json')['sources']['projects'],str(ann/'src'))
        self.assertEqual([r['setting'] for r in result['paths']['unresolved']],['sources.projects'])

    def test_an_unreadable_declaration_is_named_and_the_restore_completes(self):
        ann,bob=self.homes()
        (self.home/'config/tracked.json').write_text('{not json')
        result=self.restore_as(bob,self.snapshot_as(ann),self.base/'restored')
        self.assertEqual(result['status'],'restored')
        self.assertIn({'file':'config/tracked.json','reason':'unreadable'},result['paths']['unresolved'])
        self.assertEqual((self.base/'restored/config/tracked.json').read_text(),'{not json','left as it is')

    def test_without_a_home_a_snapshot_and_a_restore_still_work(self):
        # Windows has no home without USERPROFILE (a service account, a task without a profile):
        # `Path.home()` raises there, and a snapshot or a restore must not fail for it.
        ann,_=self.homes()
        self.settings({'projects':str(ann/'code')})
        with patch.object(upgrade.Path,'home',side_effect=RuntimeError('Could not determine home directory.')):
            source=self.snapshot()
            self.assertNotIn('home_path',config.read_json(source/'manifest.json'))
            result=upgrade.restore(source,self.base/'restored')
        self.assertEqual((result['paths']['home_from'],result['paths']['home_to'],result['paths']['rewritten']),
                         (None,None,[]))
        self.assertEqual([r['setting'] for r in result['paths']['unresolved']],['sources.projects'])
        self.assertEqual(upgrade.rewrite_home_paths(self.base/'restored',old_home=str(ann),new_home=None)['rewritten'],[],
                         'with no home to rewrite into, nothing is rewritten')

    def test_a_windows_home_is_compared_part_by_part(self):
        rewrite=upgrade.relative_to_home
        self.assertEqual(rewrite('C:\\Users\\ann\\src\\alpha','C:\\Users\\ann'),'~/src/alpha')
        self.assertEqual(rewrite('c:\\users\\ANN','C:\\Users\\ann'),'~')
        self.assertIsNone(rewrite('C:\\Users\\anna\\src','C:\\Users\\ann'))
        self.assertIsNone(rewrite('D:\\Work','C:\\Users\\ann'))
        self.assertEqual(rewrite('/srv/people/ann/src','/srv/people/ann'),'~/src')
        self.assertIsNone(rewrite('/srv/people/anna/src','/srv/people/ann'))
        self.assertIsNone(rewrite('~/src','/srv/people/ann'),'already home-relative')
        self.assertIsNone(rewrite('/anything','/'),'a root is no home')
        # Normalised lexically first: a value that leaves the old home through `..` is outside it.
        self.assertIsNone(rewrite('/srv/people/ann/../bob/x','/srv/people/ann'))
        self.assertIsNone(rewrite('/srv/people/ann/..','/srv/people/ann'))
        self.assertEqual(rewrite('/srv/people/ann/src/../x','/srv/people/ann'),'~/x')
        self.assertIsNone(rewrite('C:\\Users\\ann\\..\\bob','C:\\Users\\ann'))
        self.assertEqual(rewrite('/srv/linked/ann/x','/srv/people/ann','/srv/linked/ann'),'~/x','either form of the home')

    @unittest.skipIf(os.name=='nt','a home behind a symbolic link is a POSIX layout')
    def test_a_home_behind_a_link_matches_the_resolved_paths_tracked_json_keeps(self):
        real_ann,real_bob=self.base/'real'/'ann',self.base/'real'/'bob'
        for home in (real_ann,real_bob):
            home.mkdir(parents=True)
        (self.base/'Users').mkdir()
        ann,bob=self.base/'Users'/'ann',self.base/'Users'/'bob'
        ann.symlink_to(real_ann,target_is_directory=True)
        bob.symlink_to(real_bob,target_is_directory=True)
        self.settings({'projects':str(ann/'code')})
        # `full track local-folder` writes resolved paths (`tracked.canonical`).
        workspace.write_json(self.home/'config/tracked.json',
                             {'schema_version':1,'local_folders':[{'path':str(real_ann/'src/alpha')},
                                                                   {'path':str(self.base/'elsewhere/beta')}],
                              'dismissed_local_folders':[]})
        source=self.snapshot_as(ann)
        manifest=config.read_json(source/'manifest.json')
        self.assertEqual((manifest['home_path'],manifest['home_path_resolved']),(str(ann),str(real_ann)))
        (real_bob/'src/alpha').mkdir(parents=True)
        (real_bob/'code').mkdir()
        result=self.restore_as(bob,source,self.base/'restored')
        tracked=config.read_json(self.base/'restored/config/tracked.json')
        self.assertEqual([r['path'] for r in tracked['local_folders']],
                         [str(real_bob/'src/alpha'),str(self.base/'elsewhere/beta')],
                         'written in tracked.json\'s canonical form, under the new home')
        self.assertEqual(config.read_json(self.base/'restored/config/settings.json')['sources']['projects'],'~/code')
        self.assertEqual([(r['setting'],r['value'],r['fix']) for r in result['paths']['unresolved']],
                         [('local_folders[1].path',str(self.base/'elsewhere/beta'),
                           'project-observatory full track local-folder PATH')])



class ExternalSecretStore(unittest.TestCase):
    """OBS-350: a secret store the settings put outside the workspace travels inside the encrypted
    snapshot, beside `data/`, and never in a plaintext one."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name).resolve()
        self.home=self.base/'private workspace'
        self.root=self.base/'cloud'/'Backups'
        self.env=patch.dict(os.environ,{'OBSERVATORY_HOME':str(self.home),'OBSERVATORY_MACHINE_ID':ALPHA,
                                        'OBSERVATORY_SYSTEM_SETUP':'0','OBSERVATORY_BACKUPS':str(self.root)},clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        import backup_vault
        self.vault=backup_vault
        cheap=patch.dict(backup_vault.SCRYPT,{'n':2**12})
        cheap.start()
        self.addCleanup(cheap.stop)
        self.store=make_external_store(self.base/'external-store')
        file=self.home/'config/settings.json'
        doc=config.read_json(file)
        doc.setdefault('sources',{})['secret_store']=str(self.store)
        workspace.write_json(file,doc)
        self.outputs=[]

    def tearDown(self):
        text='\n'.join(self.outputs)
        for value in all_values():
            self.assertNotIn(value,text,'a value reached a manifest, an answer or a record')
        self.env.stop()
        self.tmp.cleanup()

    def encrypted(self):
        self.vault.set_passphrase(self.home,PASSPHRASE)
        result=upgrade.snapshot(self.home,writers_stopped=True,kind='daily')
        self.outputs.append(json.dumps(result))
        self.assertTrue(result['encrypted'],result)
        opened=self.vault.extract_tree(Path(result['snapshot']),self.base/'opened',PASSPHRASE)
        manifest=config.read_json(opened/'manifest.json')
        self.outputs.append(json.dumps(manifest))
        return result,opened,manifest

    def record(self):
        file=self.home/'store/raw/secret-store-carry.json'
        self.outputs.append(file.read_text())
        return json.loads(file.read_text())

    def test_an_external_store_is_carried_into_an_encrypted_snapshot(self):
        result,opened,manifest=self.encrypted()
        [entry]=manifest['externals']
        self.assertEqual((entry['name'],entry['settings_keys'],entry['state'],entry['reason']),
                         ('secret_store',['secret_store'],'carried',None))
        self.assertTrue(manifest['external_sources_included'])
        carried=opened/'external/secret_store'
        for rel in VALUES:
            self.assertEqual((carried/rel).read_bytes(),(self.store/rel).read_bytes(),rel)
        self.assertEqual((carried/'projects/movements.jsonl').read_bytes(),(self.store/'projects/movements.jsonl').read_bytes())
        listed={row['path'] for row in entry['files']}
        on_disk={p.relative_to(carried).as_posix() for p in carried.rglob('*') if p.is_file()}
        self.assertEqual(listed,on_disk,'nothing under external/ is unlisted')
        self.assertEqual({r['path']:r['mode'] for r in entry['files']}['projects/alpha/prod/API_KEY'],0o600)
        self.assertEqual(entry['vault_slots'],3)
        self.assertEqual(entry['kept_here'],0)
        self.assertFalse((opened/'data'/'external').exists(),'never inside data/')
        self.assertEqual(result['secret_store']['state'],'carried')
        self.assertEqual(result['secret_store']['files'],len(entry['files']))
        upgrade.verify_snapshot(opened)
        record=self.record()
        self.assertEqual((record['state'],record['kind'],record['file']),('carried','daily',Path(result['snapshot']).name))
        self.assertEqual(stat.S_IMODE((self.home/'store/raw/secret-store-carry.json').stat().st_mode),0o600)
        # Doctor reads that record and stops warning.
        codes=[i['code'] for i in self.vault.status(self.home)['warning_items']]
        self.assertNotIn('backups-vault-store-not-carried',codes)

    def test_lock_files_and_temporaries_are_not_carried(self):
        _,opened,manifest=self.encrypted()
        listed={row['path'] for row in manifest['externals'][0]['files']}
        for name in ('projects/.vault.lock','projects/.vault-write-abc123','.openrouter.lock','stash.tmp'):
            self.assertNotIn(name,listed)
            self.assertFalse((opened/'external/secret_store'/name).exists(),name)

    def test_a_plaintext_output_never_carries_the_store(self):
        out=self.base/'plain-snapshot'
        result=upgrade.snapshot(self.home,out,writers_stopped=True)
        manifest=config.read_json(out/'manifest.json')
        self.outputs+= [json.dumps(result),json.dumps(manifest)]
        self.assertEqual((manifest['externals'][0]['state'],manifest['externals'][0]['reason']),('not-carried','plaintext-output'))
        self.assertFalse(manifest['external_sources_included'])
        self.assertFalse((out/'external').exists())
        self.assertEqual(result['secret_store']['reason'],'plaintext-output')
        self.assertFalse((self.home/'store/raw/secret-store-carry.json').exists(),
                         'a copy by hand never stands for the daily encrypted one')

    def test_without_a_passphrase_the_store_stays_where_it_is(self):
        result=upgrade.snapshot(self.home,writers_stopped=True)
        self.outputs.append(json.dumps(result))
        self.assertFalse(result['encrypted'])
        local=Path(result['snapshot'])
        self.assertFalse((local/'external').exists())
        for path in (self.home/'backups').rglob('*'):
            if path.is_file():
                self.assertNotIn(b'fixture-value-001',path.read_bytes(),path)
        self.assertEqual((self.record()['state'],self.record()['reason']),('not-carried','no-passphrase'))

    def test_a_failed_export_keeps_no_store_in_the_plaintext_copy(self):
        self.vault.set_passphrase(self.home,PASSPHRASE)
        with patch.object(self.vault,'export_snapshot',side_effect=OSError('the root went away')):
            result=upgrade.snapshot(self.home,writers_stopped=True)
        self.outputs.append(json.dumps(result))
        self.assertFalse(result['encrypted'])
        local=Path(result['snapshot'])
        self.assertFalse((local/'external').exists())
        manifest=config.read_json(local/'manifest.json')
        self.assertEqual((manifest['externals'][0]['state'],manifest['externals'][0]['reason']),('not-carried','export-failed'))
        upgrade.verify_snapshot(local)
        self.assertEqual(self.record()['reason'],'export-failed')

    def test_the_vault_lock_is_held_while_the_store_is_copied(self):
        import oslocks
        import secret_store_carry
        held=[]
        real=secret_store_carry._copy_file

        def probe(source,target):
            fd=os.open(self.store/'projects/.vault.lock',os.O_RDWR)
            try:
                oslocks.flock(fd,oslocks.LOCK_EX|oslocks.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return real(source,target)
        with patch.object(secret_store_carry,'_copy_file',probe):
            self.encrypted()
        self.assertTrue(held and all(held),'a vault write could have run during the copy')

    def test_a_store_that_changes_during_the_copy_is_named_not_carried(self):
        import secret_store_carry
        real=secret_store_carry._copy_file
        stash=self.store/'openrouter-admin'

        def moving(source,target):
            out=real(source,target)
            if source==stash:
                stash.write_text(stash.read_text()+'x')
            return out
        with patch.object(secret_store_carry,'_copy_file',moving):
            result,opened,manifest=self.encrypted()
        self.assertEqual((manifest['externals'][0]['state'],manifest['externals'][0]['reason']),
                         ('not-carried','changed-during-copy'))
        self.assertFalse((opened/'external').exists())
        self.assertEqual(result['secret_store']['state'],'not-carried')

    def test_one_change_is_retried_and_the_second_pass_carries(self):
        import secret_store_carry
        real=secret_store_carry._copy_file
        stash=self.store/'openrouter-admin'
        changed=[]

        def once(source,target):
            out=real(source,target)
            if source==stash and not changed:
                changed.append(1)
                stash.write_text('fixture-value-005\n')
                os.utime(stash,ns=(1,1))
                stash.write_text('fixture-value-005 \n')
            return out
        with patch.object(secret_store_carry,'_copy_file',once):
            _,opened,manifest=self.encrypted()
        self.assertEqual(manifest['externals'][0]['state'],'carried')
        self.assertEqual((opened/'external/secret_store/openrouter-admin').read_bytes(),stash.read_bytes())

    def test_a_link_inside_the_store_is_recorded_and_one_outside_is_named(self):
        (self.store/'current').symlink_to('projects')
        (self.store/'outside').symlink_to(self.base)
        special=[]
        if hasattr(os,'mkfifo'):
            os.mkfifo(self.store/'pipe')
            special.append({'path':'pipe','reason':'special-file'})
        _,opened,manifest=self.encrypted()
        entry=manifest['externals'][0]
        self.assertEqual(entry['state'],'carried')
        self.assertEqual(entry['links'],[{'path':'current','target':'projects'}])
        self.assertIn({'path':'outside','reason':'link-outside-store'},entry['not_carried'])
        for row in special:
            self.assertIn(row,entry['not_carried'])
        self.assertFalse(os.path.lexists(opened/'external/secret_store/current'),
                         'a link is recorded, never written: the encrypted format and older readers refuse links')
        upgrade.verify_snapshot(opened)

    def test_a_store_over_the_cap_is_named_too_large(self):
        import secret_store_carry
        with patch.object(secret_store_carry,'SECRET_STORE_MAX_MB',40/(1024*1024)):
            result,opened,manifest=self.encrypted()
        entry=manifest['externals'][0]
        self.assertEqual((entry['state'],entry['reason']),('not-carried','too-large'))
        self.assertGreater(entry['size'],40)
        self.assertFalse((opened/'external').exists())

    def test_verify_refuses_an_unlisted_or_changed_store_file(self):
        import secret_store_carry
        _,opened,_=self.encrypted()
        upgrade.verify_snapshot(opened)
        # Each damage on its own copy of the opened snapshot.
        cases={'changed':lambda c,o: (c/'projects/alpha/prod/API_KEY').write_text('fixture-value-009\n'),
               'unlisted':lambda c,o: (c/'unlisted').write_text('x'),
               'link':lambda c,o: (c/'a-link').symlink_to('projects'),
               'stray folder':lambda c,o: (o/'external/other').mkdir(),
               'missing':lambda c,o: (c/'openrouter-admin').unlink()}
        for name,damage in cases.items():
            with self.subTest(name):
                copy=self.base/('damaged-'+name.replace(' ','-'))
                shutil.copytree(opened,copy,symlinks=True)
                damage(copy/'external/secret_store',copy)
                with self.assertRaises(secret_store_carry.CarryRefused) as caught:
                    upgrade.verify_snapshot(copy)
                self.assertEqual(caught.exception.code,'secret-store-manifest-mismatch')
                self.assertIsInstance(caught.exception,config.ConfigurationError)

    def test_a_restore_by_this_release_restores_the_home_and_leaves_the_store_alone(self):
        result,_,_=self.encrypted()
        new=self.base/'restored'
        with patch.dict(os.environ,{'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}):
            restored=upgrade.restore(Path(result['snapshot']),new)
        self.outputs.append(json.dumps(restored))
        self.assertEqual(restored['status'],'restored')
        self.assertFalse((new/'external').exists(),'the store is never written into the home')
        self.assertEqual(config.read_json(new/'config/settings.json')['sources']['secret_store'],str(self.store))

    def test_a_store_inside_the_workspace_travels_in_data_as_before(self):
        file=self.home/'config/settings.json'
        doc=config.read_json(file)
        doc['sources'].pop('secret_store')
        workspace.write_json(file,doc)
        (self.home/'secrets/demo-slot').write_text('synthetic opaque private value')
        result,opened,manifest=self.encrypted()
        self.assertEqual((manifest['externals'],manifest['external_sources_included']),([],False))
        self.assertEqual(result['secret_store'],{'state':'not-applicable'})
        self.assertTrue((opened/'data/secrets/demo-slot').is_file())
        self.assertFalse((opened/'external').exists())
        self.assertFalse((self.home/'store/raw/secret-store-carry.json').exists())

    def test_the_update_snapshot_never_carries_the_store(self):
        # The pre-update snapshot (`update=True`, an explicit output) and `upgrade`'s own `_snapshot`
        # are named for what they are, not as a plaintext copy taken by hand.
        out=self.home/'backups'/'before-update-test'
        receipt=upgrade.snapshot(self.home,out,writers_stopped=True,update=True,plaintext=True)
        manifest=config.read_json(out/'manifest.json')
        self.outputs+= [json.dumps(receipt),json.dumps(manifest)]
        self.assertEqual(manifest['externals'][0]['reason'],'update-snapshot')
        self.assertFalse((out/'external').exists())
        out=self.home/'backups'/'before-upgrade-test'
        upgrade._snapshot(self.home,out,not_carried='update-snapshot')
        self.assertEqual(config.read_json(out/'manifest.json')['externals'][0]['reason'],'update-snapshot')
        self.assertIn("not_carried='update-snapshot'",Path(upgrade.__file__).read_text(),
                      'upgrade takes its snapshot as an update snapshot')

    def test_the_environment_override_is_named_in_the_manifest(self):
        with patch.dict(os.environ,{'OBSERVATORY_VAULT_DIR':str(self.base/'elsewhere')}):
            _,_,manifest=self.encrypted()
        self.assertEqual([w['code'] for w in manifest['external_warnings']],['secret-store-env-override'])
        self.assertEqual(manifest['externals'][0]['state'],'carried','the settings\' store is still carried')

    def test_a_setting_that_holds_the_workspace_is_never_copied_into_its_own_snapshot(self):
        file=self.home/'config/settings.json'
        doc=config.read_json(file)
        doc['sources']['secret_store']=str(self.base)
        workspace.write_json(file,doc)
        _,opened,manifest=self.encrypted()
        self.assertEqual((manifest['externals'][0]['state'],manifest['externals'][0]['reason']),
                         ('not-carried','contains-workspace'))
        self.assertFalse((opened/'external').exists())

    def test_the_daily_maintenance_record_says_whether_the_store_was_carried(self):
        import datetime
        import maintenance
        self.vault.set_passphrase(self.home,PASSPHRASE)
        state={}
        at=datetime.datetime.now(datetime.timezone.utc)
        report=maintenance._snapshot_with_jobs_stopped(self.home,state,at,None,sleep=lambda seconds: None)
        self.assertEqual(report['result'],'taken',report)
        self.outputs.append(json.dumps(state))
        self.assertEqual(state['snapshot']['secret_store'],{'state':'carried','reason':None})

    def test_an_interrupt_inside_the_encryption_leaves_no_store_in_plaintext(self):
        # SIGTERM (SystemExit) in a maintenance run, ^C, a library that does not import, a bug: each is
        # raised past `after_snapshot`, and none may leave the store in the plaintext copy that stays.
        self.vault.set_passphrase(self.home,PASSPHRASE)
        for error in (KeyboardInterrupt(),SystemExit(143),ImportError('cryptography'),ValueError('bug')):
            with self.subTest(type(error).__name__):
                with patch.object(self.vault,'encrypt_tree',side_effect=error):
                    with self.assertRaises(type(error)):
                        upgrade.snapshot(self.home,writers_stopped=True,kind='daily')
                left=[p for p in (self.home/'backups').iterdir() if p.name.startswith('daily-')]
                self.assertTrue(left,'the plaintext copy stays, as before')
                for copy in left:
                    self.assertFalse((copy/'external').exists(),copy)
                    rows=config.read_json(copy/'manifest.json')['externals']
                    self.assertEqual({r['state'] for r in rows},{'not-carried'})
                for path in (self.home/'backups').rglob('*'):
                    if path.is_file():
                        self.assertNotIn(b'fixture-value-001',path.read_bytes(),path)
                for copy in left:   # so the next case proves its own path, not the next run's sweep
                    shutil.rmtree(copy)

    def test_a_snapshot_first_sweeps_stores_and_dead_stages_from_the_workspace_backups(self):
        import secret_store_carry
        backups=self.home/'backups'
        killed=backups/'daily-killed'
        (killed/'external/secret_store/projects').mkdir(parents=True)
        (killed/'external/secret_store/projects/K').write_text('fixture-value-001')
        workspace.write_json(killed/'manifest.json',{'externals':[{'name':'secret_store','state':'carried'}],
                                                     'external_sources_included':True})
        dead=backups/'.snapshot-dead'
        (dead/'external').mkdir(parents=True)
        os.utime(dead,(1,1))
        fresh=backups/'.snapshot-fresh'
        fresh.mkdir()
        upgrade.snapshot(self.home,self.base/'by-hand',writers_stopped=True)
        self.assertFalse((killed/'external').exists())
        manifest=config.read_json(killed/'manifest.json')
        self.assertEqual((manifest['externals'][0]['state'],manifest['externals'][0]['reason'],
                          manifest['external_sources_included']),('not-carried','export-failed',False))
        self.assertFalse(dead.exists(),'a stage older than an hour is a dead run\'s')
        self.assertTrue(fresh.exists(),'a recent stage may be a live run\'s')
        # A copy whose manifest is gone or broken still loses its store.
        for broken in ('missing','garbage'):
            copy=backups/('daily-'+broken)
            (copy/'external/secret_store').mkdir(parents=True)
            if broken=='garbage':
                (copy/'manifest.json').write_text('{not json')
            self.assertTrue(secret_store_carry.strip(copy,'export-failed'))
            self.assertFalse((copy/'external').exists())

    def test_a_failed_carry_record_is_said_not_swallowed(self):
        real=workspace.write_json

        def refuse(path,value):
            if Path(path).name=='secret-store-carry.json':
                raise OSError('read-only')
            return real(path,value)
        err=io.StringIO()
        with patch.object(workspace,'write_json',refuse),patch.object(sys,'stderr',err):
            result,_,_=self.encrypted()
        self.assertIn('carry record was not written',result['carry_record_error'])
        self.assertIn('carry record was not written',err.getvalue())

    def test_the_carry_record_counts_what_stayed_behind(self):
        (self.store/'outside').symlink_to(self.base)
        result,_,manifest=self.encrypted()
        count=len(manifest['externals'][0]['not_carried'])
        self.assertGreaterEqual(count,1)
        self.assertEqual((self.record()['not_carried'],result['secret_store']['not_carried']),(count,count))
        self.assertEqual(self.vault.status(self.home)['secret_store']['last_carry']['not_carried'],count)

    def test_an_undeletable_leftover_is_named_and_the_snapshot_goes_on(self):
        stuck=self.home/'backups'/'daily-stuck'
        locked=stuck/'external/secret_store/projects'
        locked.mkdir(parents=True)
        (locked/'K').write_text('fixture-value-001')
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o700))
        if os.access(locked,os.W_OK):
            self.skipTest('this account can delete inside a 0500 folder')
        result,_,_=self.encrypted()
        self.assertEqual(result['secret_store']['state'],'carried','the daily backup is not stopped')
        self.assertEqual(result['secret_store_sweep'],{'failed':[{'path':'daily-stuck','error':'PermissionError'}]})
        self.assertEqual(self.record()['sweep_failed'],1)
        self.assertEqual(self.vault.status(self.home)['secret_store']['last_carry']['sweep_failed'],1)
        by_hand=upgrade.snapshot(self.home,self.base/'by-hand-2',writers_stopped=True,plaintext=True)
        self.assertEqual(by_hand['secret_store_sweep']['failed'][0]['path'],'daily-stuck')

    def test_a_failing_strip_never_hides_the_original_interrupt(self):
        import secret_store_carry
        self.vault.set_passphrase(self.home,PASSPHRASE)
        with patch.object(self.vault,'encrypt_tree',side_effect=SystemExit(143)), \
                patch.object(secret_store_carry,'strip',side_effect=OSError('cannot delete')):
            with self.assertRaises(SystemExit) as caught:
                upgrade.snapshot(self.home,writers_stopped=True,kind='daily')
        self.assertEqual(caught.exception.code,143)
        # The next snapshot's sweep cleans what this one could not.
        upgrade.snapshot(self.home,self.base/'by-hand-3',writers_stopped=True,plaintext=True)
        for copy in (self.home/'backups').iterdir():
            self.assertFalse((copy/'external').exists(),copy)

    def test_the_sweep_touches_only_snapshot_folders(self):
        backups=self.home/'backups'
        notes=backups/'my-notes'
        (notes/'external').mkdir(parents=True)
        (notes/'external/plan.txt').write_text('a person\'s own file')
        odd=backups/'odd-name'
        (odd/'external/secret_store').mkdir(parents=True)
        workspace.write_json(odd/'manifest.json',{'externals':[{'name':'secret_store','state':'carried'}]})
        upgrade.snapshot(self.home,self.base/'by-hand-4',writers_stopped=True)
        self.assertTrue((notes/'external/plan.txt').is_file(),'a folder that is not a snapshot is never touched')
        self.assertFalse((odd/'external').exists(),'a folder whose manifest lists externals is a snapshot')


def _tree(root: Path) -> dict:
    """Every entry under `root` with its bytes or its link target: a store compared before and after."""
    out={}
    if not os.path.lexists(root):
        return out
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel]=('link',os.readlink(path))
        elif path.is_file():
            out[rel]=('file',path.read_bytes())
        else:
            out[rel]=('dir',None)
    return out


@unittest.skipIf(os.name=='nt','OBS-278: a workspace snapshot and its restore stop at a bad file descriptor on '
                                 'Windows before their own checks; this class runs once that row lands')
class RestoreSecretStore(unittest.TestCase):
    """OBS-350 part 2: the restore writes a carried store where the restored settings point, under a
    policy for a store already there, before the home, and never with a value in an answer."""

    setUp=ExternalSecretStore.setUp
    tearDown=ExternalSecretStore.tearDown
    encrypted=ExternalSecretStore.encrypted

    def snapshot_and_move_store_away(self):
        """A snapshot carrying the store, then the store moved off this path: a second machine's view."""
        result,opened,manifest=self.encrypted()
        self.opened=opened
        self.original=_tree(self.store)
        os.rename(self.store,self.base/'first-machine-store')
        return Path(result['snapshot']),manifest

    def restore(self,snapshot,new=None,**kw):
        new=new or self.base/'restored'
        with patch.dict(os.environ,{'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}):
            result=upgrade.restore(snapshot,new,**kw)
        self.outputs.append(json.dumps(result))
        return result

    def movements(self):
        file=self.store/'projects/movements.jsonl'
        text=file.read_text()
        self.outputs.append(text)
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def assert_store_is_the_snapshot(self,root):
        for rel,value in VALUES.items():
            self.assertEqual((root/rel).read_text(),value+'\n',rel)
        for name in ('projects/.vault.lock','projects/.vault-write-abc123','.openrouter.lock','stash.tmp'):
            self.assertFalse(os.path.lexists(root/name),name)

    def test_restore_writes_the_store_where_the_restored_settings_point(self):
        if hasattr(os,'symlink') and os.name!='nt':
            (self.store/'current').symlink_to('projects')
        snapshot,_=self.snapshot_and_move_store_away()
        result=self.restore(snapshot)
        self.assertEqual(result['status'],'restored')
        block=result['secret_store']
        self.assertEqual((block['state'],block['conflicts']),('written',0))
        self.assertGreaterEqual(block['files'],len(VALUES))
        self.assertNotIn('conflict_paths',block,'names only on request')
        self.assert_store_is_the_snapshot(self.store)
        if os.name!='nt':
            self.assertEqual(stat.S_IMODE(self.store.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((self.store/'projects/alpha').stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((self.store/'projects/alpha/prod/API_KEY').stat().st_mode),0o600)
            self.assertEqual(os.readlink(self.store/'current'),'projects')
        rows=self.movements()
        self.assertEqual(rows[0],{'event':'put','secret':'alpha/prod/API_KEY'},'the carried journal comes first')
        [line]=[r for r in rows if r.get('event')=='restore-store']
        self.assertEqual((line['secret'],line['tool'],line['state'],line['snapshot'],line['taken_on']),
                         ('store/*','workspace_upgrade.py','written',snapshot.name,ALPHA))
        self.assertNotIn('value',line)
        self.assertEqual(block['journal'],'written')
        self.assertFalse([p for p in self.base.iterdir() if '.restore-' in p.name],'no stage is left')

    def test_the_parents_of_an_absent_target_are_created_owner_only(self):
        file=self.home/'config/settings.json'
        doc=config.read_json(file)
        doc['sources']['secret_store']=str(self.base/'deep'/'er'/'store')
        workspace.write_json(file,doc)
        shutil.copytree(self.store,self.base/'deep'/'er'/'store')
        self.store=self.base/'deep'/'er'/'store'
        snapshot,_=self.snapshot_and_move_store_away()
        shutil.rmtree(self.base/'deep')
        self.assertEqual(self.restore(snapshot)['secret_store']['state'],'written')
        self.assert_store_is_the_snapshot(self.store)
        if os.name!='nt':
            self.assertEqual(stat.S_IMODE((self.base/'deep').stat().st_mode),0o700)

    def test_auto_refuses_a_different_existing_store_before_writing_anything(self):
        import secret_store_carry
        snapshot,_=self.snapshot_and_move_store_away()
        shutil.copytree(self.base/'first-machine-store',self.store,symlinks=True)
        (self.store/'projects/alpha/prod/API_KEY').write_text('fixture-value-010\n')
        before=_tree(self.store)
        new=self.base/'restored'
        with self.assertRaises(secret_store_carry.CarryRefused) as caught:
            self.restore(snapshot,new)
        self.assertEqual(caught.exception.code,'secret-store-not-empty')
        self.assertIsInstance(caught.exception,config.ConfigurationError)
        self.assertIn('--secret-store',str(caught.exception))
        self.assertNotIn('fixture-value',str(caught.exception))
        self.assertFalse(new.exists(),'the home is not created')
        self.assertEqual(_tree(self.store),before,'the target is byte-identical')

    def test_keep_merge_replace_and_skip_follow_the_table(self):
        import secret_store_carry
        snapshot,_=self.snapshot_and_move_store_away()
        first=self.base/'first-machine-store'

        def differing():
            if os.path.lexists(self.store):
                shutil.rmtree(self.store)
            shutil.copytree(first,self.store,symlinks=True)
            (self.store/'projects/alpha/prod/API_KEY').write_text('fixture-value-010\n')   # a conflict
            (self.store/'openrouter-admin').unlink()                                       # missing here
            (self.store/'only-here').write_text('fixture-value-011\n')                      # only here
            return _tree(self.store)

        for n,policy in enumerate(('keep','merge','replace','skip')):
            with self.subTest(policy):
                before=differing()
                new=self.base/f'restored-{policy}'
                result=self.restore(snapshot,new,secret_store=policy,secret_store_names=True)
                block=result['secret_store']
                self.assertEqual(result['status'],'restored')
                if policy=='keep':
                    self.assertEqual(block['state'],'kept-existing')
                    self.assertEqual(_tree(self.store),before)
                elif policy=='merge':
                    self.assertEqual((block['state'],block['files'],block['conflicts'],block['only_here']),
                                     ('merged',1,1,1))
                    self.assertEqual(block['conflict_paths'],['projects/alpha/prod/API_KEY'])
                    self.assertEqual((self.store/'openrouter-admin').read_text(),'fixture-value-005\n')
                    self.assertEqual((self.store/'projects/alpha/prod/API_KEY').read_text(),'fixture-value-010\n',
                                     'a conflict is never overwritten')
                    self.assertTrue((self.store/'only-here').is_file())
                elif policy=='replace':
                    self.assertEqual(block['state'],'replaced')
                    self.assert_store_is_the_snapshot(self.store)
                    self.assertFalse((self.store/'only-here').exists())
                    [aside]=[p for p in self.base.iterdir() if p.name.startswith('external-store.before-restore-')]
                    self.assertEqual(_tree(aside),before,'the old store is moved aside, never deleted')
                    if os.name!='nt':
                        self.assertEqual(stat.S_IMODE(aside.stat().st_mode),0o700)
                    shutil.rmtree(aside)
                else:
                    self.assertEqual(block['state'],'skipped')
                    self.assertEqual(_tree(self.store),before)
        # Absent, or an empty folder: every policy but skip writes it whole.
        for policy in ('auto','keep','merge','replace'):
            with self.subTest('empty '+policy):
                shutil.rmtree(self.store)
                (self.store/'projects').mkdir(parents=True)
                (self.store/'projects/.vault.lock').write_text('')
                result=self.restore(snapshot,self.base/f'restored-empty-{policy}',secret_store=policy)
                self.assertEqual(result['secret_store']['state'],'written')
                self.assert_store_is_the_snapshot(self.store)
        shutil.rmtree(self.store)
        result=self.restore(snapshot,self.base/'restored-absent-skip',secret_store='skip')
        self.assertEqual(result['secret_store']['state'],'skipped')
        self.assertFalse(os.path.lexists(self.store))
        # Present and the same: nothing written, whatever the policy, and no journal line.
        shutil.copytree(first,self.store,symlinks=True)
        for policy in ('auto','keep','merge','replace'):
            with self.subTest('same '+policy):
                result=self.restore(snapshot,self.base/f'restored-same-{policy}',secret_store=policy)
                self.assertEqual(result['secret_store']['state'],'same')
        self.assertFalse([r for r in self.movements() if r.get('event')=='restore-store'])
        with self.assertRaises(config.ConfigurationError):
            self.restore(snapshot,self.base/'restored-bad',secret_store='overwrite')
        self.assertTrue(secret_store_carry.POLICIES)

    def test_a_journal_that_grew_since_the_snapshot_reads_the_same(self):
        snapshot,_=self.snapshot_and_move_store_away()
        self.assertEqual(self.restore(snapshot,self.base/'r1')['secret_store']['state'],'written')
        # The restore appended its own movement line: a rerun (an interrupted restore) still reads same.
        self.assertEqual(self.restore(snapshot,self.base/'r2')['secret_store']['state'],'same')

    def test_the_store_is_written_before_the_home_and_a_failed_store_write_leaves_both_unchanged(self):
        import secret_store_carry
        snapshot,_=self.snapshot_and_move_store_away()
        new=self.base/'restored'
        seen=[]
        real=secret_store_carry._copy_file

        def watch(source,target):
            seen.append(new.exists())
            return real(source,target)
        with patch.object(secret_store_carry,'_copy_file',watch):
            self.restore(snapshot,new)
        self.assertTrue(seen and not any(seen),'every store file is written before the home appears')
        shutil.rmtree(new)
        shutil.rmtree(self.store)
        calls=[]

        def fail_third(source,target):
            calls.append(1)
            if len(calls)==3:
                raise OSError('disk full')
            return real(source,target)
        with patch.object(secret_store_carry,'_copy_file',fail_third):
            with self.assertRaises(secret_store_carry.CarryFailed) as caught:
                self.restore(snapshot,new)
        self.assertEqual(caught.exception.code,'secret-store-write-failed')
        self.assertFalse(os.path.lexists(self.store),'the target is left as it was')
        self.assertFalse(new.exists(),'the home is not written')
        self.assertFalse([p for p in self.base.iterdir() if '.restore-' in p.name],'the stages are removed')
        # The same failure under replace puts the old store back.
        shutil.copytree(self.base/'first-machine-store',self.store,symlinks=True)
        (self.store/'projects/alpha/prod/API_KEY').write_text('fixture-value-010\n')
        before=_tree(self.store)
        calls.clear()
        with patch.object(secret_store_carry,'_copy_file',fail_third):
            with self.assertRaises(secret_store_carry.CarryFailed):
                self.restore(snapshot,new,secret_store='replace')
        self.assertEqual(_tree(self.store),before)
        self.assertFalse([p for p in self.base.iterdir() if 'before-restore' in p.name])

    def test_secret_store_only_writes_the_store_into_the_current_settings_target(self):
        snapshot,_=self.snapshot_and_move_store_away()
        new=self.base/'restored'
        result=self.restore(snapshot,new,secret_store='skip')
        self.assertEqual(result['secret_store']['state'],'skipped')
        self.assertFalse(os.path.lexists(self.store))
        file=new/'config/settings.json'
        doc=config.read_json(file)
        target=self.base/'pointed-here-now'
        doc['sources']['secret_store']=str(target)
        workspace.write_json(file,doc)
        home_before=_tree(new)
        with patch.dict(os.environ,{'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}):
            only=upgrade.restore_store_only(snapshot,new)
        self.outputs.append(json.dumps(only))
        self.assertEqual((only['status'],only['secret_store']['state']),('store-restored','written'))
        self.assert_store_is_the_snapshot(target)
        self.assertFalse(os.path.lexists(self.store),'the snapshot\'s own setting is not used')
        self.assertEqual({k:v for k,v in _tree(new).items() if not k.endswith('.lock')},
                         {k:v for k,v in home_before.items() if not k.endswith('.lock')},'the home is not touched')
        # A second run reads same; a folder that is no workspace is refused.
        with patch.dict(os.environ,{'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}):
            self.assertEqual(upgrade.restore_store_only(snapshot,new)['secret_store']['state'],'same')
            with self.assertRaises(config.ConfigurationError):
                upgrade.restore_store_only(snapshot,self.base/'no-workspace-here')

    def test_a_target_that_cannot_be_resolved_or_lies_in_the_home_is_refused_under_auto(self):
        import secret_store_carry
        snapshot,_=self.snapshot_and_move_store_away()
        # The store's own path as the new home: the target lies inside it.
        with self.assertRaises(secret_store_carry.CarryRefused) as caught:
            self.restore(snapshot,self.store)
        self.assertEqual(caught.exception.code,'secret-store-target-inside-home')
        self.assertFalse(os.path.lexists(self.store))
        result=self.restore(snapshot,self.store,secret_store='keep')
        self.assertEqual((result['secret_store']['state'],result['secret_store']['reason']),
                         ('skipped','secret-store-target-inside-home'))
        shutil.rmtree(self.store)
        # A snapshot whose settings no longer name the store.
        opened=self.opened
        settings=opened/'data/config/settings.json'
        doc=config.read_json(settings)
        doc['sources'].pop('secret_store')
        workspace.write_json(settings,doc)
        manifest=config.read_json(opened/'manifest.json')
        for row in manifest['files']:
            if row['path']=='config/settings.json':
                row['size']=settings.stat().st_size
                row['sha256']=upgrade.digest(settings)
        workspace.write_json(opened/'manifest.json',manifest)
        with self.assertRaises(secret_store_carry.CarryRefused) as caught:
            upgrade.restore(opened,self.base/'restored-unresolved')
        self.assertEqual(caught.exception.code,'secret-store-unresolved')
        self.assertIn('configure sources secret_store',str(caught.exception))
        self.assertIn('--secret-store-only',str(caught.exception))
        for policy in ('merge','replace'):
            with self.assertRaises(secret_store_carry.CarryRefused):
                upgrade.restore(opened,self.base/'restored-unresolved',secret_store=policy)
        result=upgrade.restore(opened,self.base/'restored-unresolved',secret_store='keep')
        self.outputs.append(json.dumps(result))
        self.assertEqual(result['secret_store']['reason'],'secret-store-unresolved')

    def test_a_snapshot_that_carried_nothing_says_so(self):
        out=self.base/'plain-snapshot'
        upgrade.snapshot(self.home,out,writers_stopped=True,plaintext=True)
        result=self.restore(out,self.base/'r-plain')
        self.assertEqual(result['secret_store']['state'],'not-in-snapshot')
        self.assertEqual(result['secret_store']['reason'],'plaintext-output')
        manifest=config.read_json(out/'manifest.json')
        manifest.pop('externals')
        workspace.write_json(out/'manifest.json',manifest)
        self.assertEqual(self.restore(out,self.base/'r-older')['secret_store'],{'state':'not-in-snapshot'})

    def test_a_leftover_stage_older_than_an_hour_is_removed_by_the_next_restore(self):
        snapshot,_=self.snapshot_and_move_store_away()
        dead=self.base/'.external-store.restore-0123456789ab'
        (dead/'projects').mkdir(parents=True)
        (dead/'projects/K').write_text('fixture-value-001')
        os.utime(dead,(1,1))
        fresh=self.base/'.external-store.restore-ba9876543210'
        fresh.mkdir()
        other=self.base/'.unrelated.restore-0123456789ab'
        other.mkdir()
        os.utime(other,(1,1))
        result=self.restore(snapshot)
        self.assertEqual(result['secret_store']['stages_removed'],1)
        self.assertFalse(dead.exists())
        self.assertTrue(fresh.exists(),'a recent stage may be a live restore\'s')
        self.assertTrue(other.exists(),'another folder\'s stage is not this target\'s')

    def test_the_first_machines_digest_manifest_becomes_a_peer_on_a_second_machine(self):
        digests=self.home/'store/vault-digests'
        digests.mkdir(parents=True)
        local={'schema':'vault-digests/1','machine':'first-mac','generated_at':'2026-11-01T00:00:00Z',
               'key_id':'0123456789abcdef','slots':{}}
        workspace.write_json(digests/'local.json',local)
        snapshot,_=self.snapshot_and_move_store_away()
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA,'OBSERVATORY_MACHINE_NAME':'second-mac'}):
            result=self.restore(snapshot,self.base/'second')
        self.assertEqual(result['machine']['mode'],'second-machine')
        moved=self.base/'second/store/vault-digests'
        self.assertFalse((moved/'local.json').exists())
        self.assertEqual(config.read_json(moved/'peers/first-mac.json'),local)
        self.assertEqual(result['digest_manifest'],{'state':'moved-to-peers','file':'peers/first-mac.json'})
        # The same name as this machine's: a peer under another file name, never this machine's own.
        shutil.rmtree(self.store)
        with patch.dict(os.environ,{'OBSERVATORY_MACHINE_ID':BETA,'OBSERVATORY_MACHINE_NAME':'first-mac'}):
            result=self.restore(snapshot,self.base/'third')
        [peer]=list((self.base/'third/store/vault-digests/peers').iterdir())
        self.assertRegex(peer.name,r'^peer-[0-9a-f]{8}\.json$')
        # The same machine keeps its own manifest.
        shutil.rmtree(self.store)
        result=self.restore(snapshot,self.base/'same')
        self.assertTrue((self.base/'same/store/vault-digests/local.json').is_file())
        self.assertNotIn('digest_manifest',result)

    def test_an_update_rollback_never_touches_the_external_store(self):
        import engine_update
        seen={}

        def fake(snapshot,stage,**kw):
            seen.update(kw)
            raise RuntimeError('stop here')
        with patch.object(upgrade,'restore',fake):
            with self.assertRaises(RuntimeError):
                engine_update.restore_in_place(self.home,self.base/'snap')
        self.assertEqual(seen.get('secret_store'),'skip')

    def test_init_on_an_empty_home_keeps_a_surviving_store(self):
        import maintenance
        snapshot,_=self.snapshot_and_move_store_away()
        shutil.copytree(self.base/'first-machine-store',self.store,symlinks=True)
        (self.store/'projects/alpha/prod/API_KEY').write_text('fixture-value-010\n')
        before=_tree(self.store)
        shutil.rmtree(self.home)   # a reinstall: the home is gone, its backups and the store are not
        with patch.dict(os.environ,{'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}):
            result=maintenance.restore_latest(self.home,unknown='keep',secret_store='keep')
        self.outputs.append(json.dumps(result))
        self.assertEqual(result['status'],'restored',result)
        self.assertEqual(result['secret_store']['state'],'kept-existing')
        self.assertEqual(_tree(self.store),before)
        self.assertIn('secret_store="keep"',(ROOT/'workspace.py').read_text(),'init passes keep')
        # Under auto the refusal is said, never swallowed into "try an older backup".
        import secret_store_carry
        shutil.rmtree(self.home)
        with patch.dict(os.environ,{'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}):
            with self.assertRaises(secret_store_carry.CarryRefused):
                maintenance.restore_latest(self.home)
        self.assertFalse(self.home.exists() and any(self.home.iterdir()))

    def test_the_command_line_takes_the_policy_and_answers_a_refusal_as_json(self):
        snapshot,_=self.snapshot_and_move_store_away()
        shutil.copytree(self.base/'first-machine-store',self.store,symlinks=True)
        (self.store/'projects/alpha/prod/API_KEY').write_text('fixture-value-010\n')
        new=self.base/'restored'
        out,err=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(new),'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}), \
                contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=upgrade.main(['restore',str(snapshot),'--json'])
        self.outputs+= [out.getvalue(),err.getvalue()]
        self.assertEqual(code,2)
        self.assertEqual(json.loads(out.getvalue())['error']['code'],'secret-store-not-empty')
        self.assertFalse(new.exists())
        out=io.StringIO()
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(new),'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}), \
                contextlib.redirect_stdout(out):
            code=upgrade.main(['restore',str(snapshot),'--secret-store','merge','--secret-store-names'])
        self.outputs.append(out.getvalue())
        self.assertEqual(code,0)
        block=json.loads(out.getvalue())['secret_store']
        self.assertEqual((block['state'],block['conflict_paths']),('merged',['projects/alpha/prod/API_KEY']))
        out,err=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(new),'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}), \
                contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=upgrade.main(['restore',str(snapshot),'--secret-store-only'])
        self.outputs+= [out.getvalue(),err.getvalue()]
        self.assertIn('Observatory: ',err.getvalue(),'without --json a refusal stays one line on stderr')
        self.assertEqual(code,2,'the merged store differs, so auto refuses')
        out=io.StringIO()
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(new),'OBSERVATORY_BACKUP_PASSPHRASE':PASSPHRASE}), \
                contextlib.redirect_stdout(out):
            code=upgrade.main(['restore',str(snapshot),'--secret-store-only','--secret-store','keep','--json'])
        self.outputs.append(out.getvalue())
        self.assertEqual((code,json.loads(out.getvalue())['secret_store']['state']),(0,'kept-existing'))

    def test_links_outside_and_special_files_are_named_and_links_are_not_restored_on_windows(self):
        if os.name=='nt':
            self.skipTest('links need POSIX here')
        import secret_store_carry
        (self.store/'current').symlink_to('projects')
        (self.store/'outside').symlink_to(self.base)
        snapshot,_=self.snapshot_and_move_store_away()
        result=self.restore(snapshot,secret_store_names=True)
        block=result['secret_store']
        self.assertEqual(block['state'],'written')
        self.assertIn({'path':'outside','reason':'link-outside-store'},block['not_restored_paths'])
        self.assertFalse(os.path.lexists(self.store/'outside'),'a link out of the store is never recreated')
        shutil.rmtree(self.store)
        with patch.object(secret_store_carry.osprivacy,'WINDOWS',True):
            result=self.restore(snapshot,self.base/'as-on-windows',secret_store_names=True)
        self.assertIn({'path':'current','reason':'link-not-supported'},result['secret_store']['not_restored_paths'])
        self.assertFalse(os.path.lexists(self.store/'current'))
        self.assert_store_is_the_snapshot(self.store)

    def test_the_target_is_read_after_the_home_rewrite(self):
        source=self.base/'fake-snapshot'
        (source/'data/config').mkdir(parents=True)
        old_home,new_home=self.base/'homes'/'ann',self.base/'homes'/'bob'
        elsewhere=str(self.base/'code')
        workspace.write_json(source/'data/config/settings.json',
                             {'sources':{'secret_store':str(old_home/'.config/example/secrets'),'projects':elsewhere}})
        with patch.object(upgrade,'this_home',lambda: new_home):
            sources=upgrade.restored_sources(source,{'home_path':str(old_home)})
            same=upgrade.restored_sources(source,{'home_path':str(new_home)})
        self.assertEqual(sources,{'secret_store':'~/.config/example/secrets','projects':elsewhere})
        self.assertEqual(same['secret_store'],str(old_home/'.config/example/secrets'),'a same-home restore keeps it')


if __name__=='__main__':
    unittest.main()
