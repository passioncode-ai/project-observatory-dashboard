#!/usr/bin/env python3
"""Encrypted backups outside the workspace: one root, one passphrase, one format.

Every test runs in a synthetic workspace with OBSERVATORY_BACKUPS pointed at a
temporary directory, so no test can reach the operator's real Documents folder.
"""
from __future__ import annotations
import importlib
import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration as config
import workspace
import workspace_upgrade as upgrade
import backup_vault as vault

PASS = 'correct horse battery staple'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'private workspace'
        self.root = self.base / 'cloud' / 'Backups'
        self.env = patch.dict(os.environ, {'OBSERVATORY_HOME': str(self.home),
                                           'OBSERVATORY_BACKUPS': str(self.root)}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        from store import db
        importlib.reload(db)
        conn = db.connect()
        conn.execute("INSERT INTO events(id,kind,occurred_at,actor) VALUES ('synthetic-event','session','2026-01-01T00:00:00Z','fixture')")
        conn.commit()
        conn.close()
        (self.home / 'secrets' / 'demo-slot').write_text('synthetic opaque private value')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def target(self) -> Path:
        return vault.root_info(self.home)['path']


class Format(Base):
    def roundtrip(self, payload: bytes, chunk: int) -> bytes:
        src, enc = self.base / 'plain.bin', self.base / 'out.obsdb'
        src.write_bytes(payload)
        with patch.object(vault, 'CHUNK', chunk):
            vault.encrypt_file(src, enc, PASS, kind='observatory-db')
        out = io.BytesIO()
        vault.decrypt_to(enc, out, PASS)
        return out.getvalue()

    def test_roundtrip_multi_chunk_and_empty(self):
        payload = os.urandom(10_000)
        self.assertEqual(self.roundtrip(payload, 1024), payload)
        self.assertEqual(self.roundtrip(b'', 1024), b'')

    def test_ciphertext_does_not_contain_plaintext(self):
        src, enc = self.base / 'plain.txt', self.base / 'out.obsdb'
        src.write_bytes(b'synthetic opaque private value' * 50)
        vault.encrypt_file(src, enc, PASS, kind='observatory-db')
        self.assertNotIn(b'synthetic opaque private value', enc.read_bytes())
        self.assertEqual(stat.S_IMODE(enc.stat().st_mode), 0o600)

    def encrypted(self) -> Path:
        src, enc = self.base / 'plain.bin', self.base / 'out.obsdb'
        src.write_bytes(os.urandom(5000))
        with patch.object(vault, 'CHUNK', 1024):
            vault.encrypt_file(src, enc, PASS, kind='observatory-db')
        return enc

    def test_wrong_passphrase_refused(self):
        with self.assertRaisesRegex(vault.BackupError, 'passphrase'):
            vault.decrypt_to(self.encrypted(), io.BytesIO(), 'another passphrase entirely')

    def test_truncation_tamper_and_append_refused(self):
        enc = self.encrypted()
        data = enc.read_bytes()
        cases = {'truncated': data[:-1100], 'tampered': data[:-10] + bytes([data[-10] ^ 1]) + data[-9:],
                 'appended': data + b'extra', 'header': data[:12] + b'X' + data[13:]}
        for name, blob in cases.items():
            with self.subTest(name):
                enc.write_bytes(blob)
                with self.assertRaises(vault.BackupError):
                    vault.decrypt_to(enc, io.BytesIO(), PASS)

    def test_rewritten_header_field_refused(self):
        """A header edit that stays valid JSON must fail authentication, not parsing."""
        enc = self.encrypted()
        data = enc.read_bytes()
        forged = data.replace(b'"kind": "observatory-db"', b'"kind": "observatory-dc"', 1)
        self.assertNotEqual(forged, data)
        enc.write_bytes(forged)
        with self.assertRaisesRegex(vault.BackupError, 'authentication'):
            vault.decrypt_to(enc, io.BytesIO(), PASS)

    def test_dropping_whole_final_chunks_refused(self):
        """The case the final-chunk flag exists for: a file cut exactly at a frame
        boundary reads as a complete, shorter stream unless the flag is checked."""
        enc = self.encrypted()
        data = enc.read_bytes()
        # 5000 bytes in 1024-byte chunks: 5 frames of (4 + len + 16); drop the last one.
        last = 4 + (5000 - 4 * 1024) + 16
        enc.write_bytes(data[:-last])
        with self.assertRaisesRegex(vault.BackupError, 'authentication'):
            vault.decrypt_to(enc, io.BytesIO(), PASS)

    def test_failed_encryption_leaves_no_file(self):
        src, enc = self.base / 'plain.bin', self.base / 'out.obsdb'
        src.write_bytes(b'x' * 100)
        with patch.object(vault, 'verify_file', side_effect=vault.BackupError('planted')):
            with self.assertRaises(vault.BackupError):
                vault.encrypt_file(src, enc, PASS, kind='observatory-db')
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.endswith(('.obsdb', '.tmp'))], [])


class Root(Base):
    def test_environment_wins_and_is_per_workspace(self):
        info = vault.root_info(self.home)
        self.assertEqual(info['source'], 'environment')
        self.assertEqual(info['path'].parent, self.root)
        self.assertTrue(info['path'].name.startswith('private workspace-'))

    def test_relative_environment_refused(self):
        with patch.dict(os.environ, {'OBSERVATORY_BACKUPS': 'relative/dir'}):
            with self.assertRaisesRegex(config.ConfigurationError, 'absolute'):
                vault.root_info(self.home)

    def test_settings_then_documents_then_beside_the_workspace(self):
        del os.environ['OBSERVATORY_BACKUPS']
        docs, data = self.base / 'Documents', self.base / 'xdg-data'
        with patch.object(vault, 'documents_dir', return_value=docs), patch.object(vault.sys, 'platform', 'darwin'), \
                patch.object(vault, 'data_dir', return_value=data):
            info = vault.root_info(self.home)  # no Documents yet: beside the workspace, never inside it
            self.assertEqual(info['source'], 'default-data')
            self.assertEqual(info['path'].parent, data / 'project-observatory-backups')
            self.assertFalse(info['inside_workspace'])
            docs.mkdir()
            info = vault.root_info(self.home)
            self.assertEqual(info['source'], 'default-documents')
            self.assertEqual(info['path'].parent, docs / 'Project Observatory' / 'Backups')
            chosen = self.base / 'chosen'
            self.assertEqual(workspace.main(['configure', 'storage', 'backups', str(chosen)]), 0)
            self.assertEqual(vault.root_info(self.home)['source'], 'settings')
            self.assertEqual(vault.root_info(self.home)['path'].parent, chosen)
        with patch.object(vault, 'documents_dir', return_value=docs), patch.object(vault.sys, 'platform', 'linux'), \
                patch.object(vault, 'data_dir', return_value=data):
            doc = config.load(self.home); doc.pop('storage')
            workspace.write_json(self.home / 'config/settings.json', doc)
            # Off macOS the copies leave the workspace too, so deleting it or reinstalling
            # into it leaves them behind (decision D5, docs/runs/2026-10-05-auto-update).
            info = vault.root_info(self.home)
            self.assertEqual(info['source'], 'default-data')
            self.assertEqual(info['path'].parent, data / 'project-observatory-backups')
            self.assertNotIn(self.home, info['path'].parents)

    def test_storage_setting_validated(self):
        self.assertEqual(workspace.main(['configure', 'storage', 'backups', 'relative']), 2)
        self.assertEqual(workspace.main(['configure', 'storage', 'elsewhere', str(self.base)]), 2)
        doc = config.load(self.home)
        doc['storage'] = {'backups': 'relative'}
        workspace.write_json(self.home / 'config/settings.json', doc)
        with self.assertRaises(config.ConfigurationError):
            config.load(self.home)


class Passphrase(Base):
    def test_set_status_and_modes(self):
        self.assertIsNone(vault.passphrase(self.home))
        with self.assertRaisesRegex(vault.BackupError, 'at least'):
            vault.set_passphrase(self.home, 'short')
        vault.set_passphrase(self.home, PASS)
        file = vault.passphrase_file(self.home)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(vault.passphrase(self.home), PASS)
        file.chmod(0o644)
        with self.assertRaisesRegex(vault.BackupError, 'readable'):
            vault.passphrase(self.home)

    def test_environment_passphrase(self):
        with patch.dict(os.environ, {'OBSERVATORY_BACKUP_PASSPHRASE': PASS}):
            self.assertEqual(vault.passphrase(self.home), PASS)

    def test_show_refuses_without_terminal(self):
        vault.set_passphrase(self.home, PASS)
        out = io.StringIO()
        with patch.object(sys, 'stdout', out):
            code = workspace.main(['backup-passphrase', 'show'])
        self.assertEqual(code, 2)
        self.assertNotIn(PASS, out.getvalue())

    def test_set_from_stdin(self):
        with patch.object(sys, 'stdin', io.StringIO(PASS + '\n')):
            self.assertEqual(workspace.main(['backup-passphrase', 'set']), 0)
        self.assertEqual(vault.passphrase(self.home), PASS)


class Snapshots(Base):
    def test_without_passphrase_stays_local_and_rotates(self):
        made = [upgrade.snapshot(self.home, writers_stopped=True) for _ in range(5)]
        self.assertTrue(all(not r['encrypted'] for r in made))
        local = sorted(p.name for p in (self.home / 'backups').iterdir() if p.name.startswith('snapshot-'))
        self.assertEqual(len(local), vault.KEEP)
        self.assertIn(Path(made[-1]['snapshot']).name, local)
        self.assertFalse(self.root.exists())

    def test_encrypted_snapshot_restores_identically(self):
        vault.set_passphrase(self.home, PASS)
        result = upgrade.snapshot(self.home, writers_stopped=True)
        self.assertTrue(result['encrypted'])
        file = Path(result['snapshot'])
        self.assertEqual(file.parent, self.target())
        self.assertTrue(file.name.endswith('.obsnap'))
        self.assertEqual([p for p in (self.home / 'backups').iterdir() if p.name.startswith('snapshot-')], [])
        self.assertNotIn(b'synthetic opaque private value', file.read_bytes())
        new = self.base / 'restored'
        with patch.dict(os.environ, {'OBSERVATORY_BACKUP_PASSPHRASE': PASS}):
            upgrade.restore(file, new)
        self.assertEqual((new / 'secrets/demo-slot').read_text(), 'synthetic opaque private value')
        conn = sqlite3.connect(new / 'store/observatory.db')
        self.assertEqual(conn.execute("SELECT id FROM events WHERE id='synthetic-event'").fetchone()[0], 'synthetic-event')
        conn.close()

    def test_decrypt_takes_the_bare_name_backups_status_prints(self):
        # `backups status` names the newest copy by file name; that name, typed
        # back, used to end in a raw `[Errno 2]`.
        vault.set_passphrase(self.home, PASS)
        name = Path(upgrade.snapshot(self.home, writers_stopped=True)['snapshot']).name
        out = self.base / 'decrypted'
        with patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(workspace.main(['backups', 'decrypt', name, str(out)]), 0)
        self.assertTrue(out.is_dir() and any(out.rglob('*')), list(out.rglob('*'))[:5])
        err = io.StringIO()
        with patch.object(sys, 'stderr', err), patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(workspace.main(['backups', 'decrypt', 'snapshot-absent.obsnap', str(self.base / 'x')]), 2)
        self.assertIn('backups status', err.getvalue())
        self.assertNotIn('Errno', err.getvalue())

    def test_backup_commands_speak_like_every_other_command(self):
        # workspace-backup/upgrade/restore printed `ConfigurationError: …` and exit 1
        # where every other command prints `Observatory: …`; a refusal is exit 2.
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            self.assertEqual(upgrade.main(['backup']), 2)
        self.assertTrue(err.getvalue().startswith('Observatory: '), err.getvalue())
        self.assertNotIn('ConfigurationError', err.getvalue())

    def test_encrypted_rotation_keeps_newest(self):
        vault.set_passphrase(self.home, PASS)
        names = [Path(upgrade.snapshot(self.home, writers_stopped=True)['snapshot']).name for _ in range(5)]
        kept = sorted(p.name for p in self.target().glob('snapshot-*.obsnap'))
        self.assertEqual(len(kept), vault.KEEP)
        self.assertIn(names[-1], kept)

    def test_restore_without_passphrase_refused_cleanly(self):
        vault.set_passphrase(self.home, PASS)
        file = Path(upgrade.snapshot(self.home, writers_stopped=True)['snapshot'])
        new = self.base / 'restored'
        with patch.object(vault, 'passphrase', return_value=None), patch.object(vault.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(vault.BackupError, 'OBSERVATORY_BACKUP_PASSPHRASE'):
                upgrade.restore(file, new)
        self.assertFalse(new.exists())

    def test_export_failure_keeps_local_copy(self):
        vault.set_passphrase(self.home, PASS)
        with patch.object(vault, 'encrypt_tree', side_effect=vault.BackupError('planted')):
            result = upgrade.snapshot(self.home, writers_stopped=True)
        self.assertFalse(result['encrypted'])
        self.assertIn('planted', result['export_error'])
        self.assertTrue(Path(result['snapshot']).is_dir())

    def test_upgrade_exports_before_upgrade_snapshot(self):
        vault.set_passphrase(self.home, PASS)
        result = upgrade.upgrade(self.home, apply=True, writers_stopped=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertTrue(result['backup']['encrypted'])
        self.assertEqual(len(list(self.target().glob('before-upgrade-*.obsnap'))), 1)
        self.assertEqual([p for p in (self.home / 'backups').iterdir() if p.name.startswith('before-upgrade-')], [])

    def test_upgrade_survives_export_failure(self):
        vault.set_passphrase(self.home, PASS)
        with patch.object(vault, 'encrypt_tree', side_effect=vault.BackupError('planted')):
            result = upgrade.upgrade(self.home, apply=True, writers_stopped=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertIn('planted', result['backup']['export_error'])
        self.assertTrue(Path(result['snapshot']).is_dir())


    def test_latest_under_another_home_names_the_backups_it_found_elsewhere(self):
        # OBS-347: the owner file names the first machine's home, so the exact-path rule finds
        # nothing under another user name; the backups are named, never chosen.
        import maintenance
        vault.set_passphrase(self.home, PASS)
        made = Path(upgrade.snapshot(self.home, writers_stopped=True)['snapshot'])
        ann, bob = self.base / 'Users' / 'ann', self.base / 'Users' / 'bob'
        folder = self.root / 'obs-0badc0de'
        folder.mkdir(parents=True)
        (folder / made.name).write_bytes(made.read_bytes())
        (folder / vault.OWNER_FILE).write_text(str(ann / '.local/share/obs') + '\n')
        unrelated = self.root / 'obs-1badc0de'
        unrelated.mkdir()
        (unrelated / made.name).write_bytes(made.read_bytes())
        (unrelated / vault.OWNER_FILE).write_text(str(ann / 'elsewhere/obs') + '\n')
        with patch.dict(os.environ, {'HOME': str(bob), 'USERPROFILE': str(bob)}):
            out = maintenance.restore_latest(bob / '.local/share/obs', store=FakeStore())
        self.assertEqual(out['status'], 'no-backup')
        self.assertEqual(out['elsewhere'], [{'label': 'obs-0badc0de', 'owner_home_relative': '~/.local/share/obs',
                                             'newest': str(folder / made.name), 'snapshots': 1}])
        self.assertEqual(out['next'], f"project-observatory full restore '{folder / made.name}'")
        self.assertNotIn(str(ann), json.dumps(out), 'only the home-relative tail of the owner file is shown')
        self.assertFalse((bob / '.local/share/obs').exists(), 'nothing is chosen or restored')


class StoreCopies(Base):
    def run_backup(self, *args):
        sys.path.insert(0, str(ROOT / 'tools'))
        import backup_store
        importlib.reload(backup_store)
        return backup_store.main(['backup_store.py', *args])

    def test_encrypted_daily_copy_and_rotation(self):
        vault.set_passphrase(self.home, PASS)
        # Each run names its own minute through `--stamp`, the CLI's path (ENG-008),
        # rather than a patched `backup_vault.stamp`, which `encrypted()` no longer calls.
        for i in range(5):
            self.assertEqual(self.run_backup('--stamp', f'2026-01-01T00{i:02d}Z'), 0)
        self.assertEqual(self.run_backup('--stamp', '2026-01-01T0004Z'), 0)  # the same minute again: nothing taken
        kept = sorted(self.target().glob('observatory-db-*.obsdb'))
        self.assertEqual(len(kept), vault.KEEP)
        self.assertEqual([p.name for p in kept],
                         [f'observatory-db-20260101T00{i:02d}00Z.obsdb' for i in (2, 3, 4)])
        self.assertEqual(list((self.home / 'store').glob('observatory.db.backup-*')), [])
        out = self.base / 'copy.db'
        with out.open('wb') as stream:
            vault.decrypt_to(kept[-1], stream, PASS)
        conn = sqlite3.connect(out)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], 'ok')
        conn.close()

    def test_encrypted_stamp_older_than_the_kept_window_is_refused(self):
        """Round-1 seam finding F-1 on #250: a stale `--stamp` used to encrypt a copy,
        rotate it away and then delete the plaintext legacy copies as if it were proven."""
        vault.set_passphrase(self.home, PASS)
        for i in range(3):
            self.assertEqual(self.run_backup('--stamp', f'2026-01-01T00{i:02d}Z'), 0)
        before = sorted(p.name for p in self.target().glob('observatory-db-*.obsdb'))
        legacy = self.home / 'store' / 'observatory.db.backup-2025-12-01T0000Z'
        legacy.write_bytes(b'x')
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            self.assertEqual(self.run_backup('--stamp', '2025-12-31T2359Z'), 2)
        self.assertIn('older than', err.getvalue())
        self.assertEqual(sorted(p.name for p in self.target().glob('observatory-db-*.obsdb')), before)
        self.assertTrue(legacy.exists())

    def test_if_due_skips_a_fresh_copy(self):
        vault.set_passphrase(self.home, PASS)
        self.assertEqual(self.run_backup('--if-due'), 0)
        self.assertEqual(self.run_backup('--if-due'), 0)
        self.assertEqual(len(list(self.target().glob('observatory-db-*.obsdb'))), 1)

    def test_unreachable_root_falls_back_and_says_so(self):
        """macOS privacy controls refuse a background job ~/Documents: the day still
        gets a local copy, and the exit code still says the off-disk copy failed."""
        vault.set_passphrase(self.home, PASS)
        err = io.StringIO()
        with patch.object(vault, 'encrypt_file', side_effect=PermissionError('Operation not permitted')), \
                patch.object(sys, 'stderr', err):
            self.assertEqual(self.run_backup(), 1)
        self.assertIn('local unencrypted copy', err.getvalue())
        self.assertEqual(len(list((self.home / 'store').glob('observatory.db.backup-2*'))), 1)
        self.assertEqual(list((self.home / 'store').glob('observatory.db.backup-encrypting-*')), [])

    def test_without_passphrase_legacy_copy(self):
        self.assertEqual(self.run_backup(), 0)
        self.assertEqual(len(list((self.home / 'store').glob('observatory.db.backup-*'))), 1)
        self.assertFalse(self.root.exists())


class Migration(Base):
    def test_moves_newest_legacy_and_removes_the_rest(self):
        for _ in range(5):
            upgrade._snapshot(self.home, self.home / 'backups' / ('snapshot-' + os.urandom(8).hex()))
        broken = self.home / 'backups' / 'snapshot-broken'
        broken.mkdir()
        vault.set_passphrase(self.home, PASS)
        report = vault.migrate(self.home)
        self.assertEqual(report['exported'], vault.KEEP)
        self.assertEqual(len(list(self.target().glob('snapshot-*.obsnap'))), vault.KEEP)
        left = sorted(p.name for p in (self.home / 'backups').iterdir() if p.name.startswith('snapshot-'))
        self.assertEqual(left, ['snapshot-broken'])  # an unverifiable one is reported, never deleted
        self.assertEqual(report['skipped'][0]['path'], str(broken))

    def test_a_local_before_update_snapshot_is_exported_verified_and_removed(self):
        # `full update` writes `backups/before-update-<hex>`: migrate exports it, verified, and the
        # doctor warning clears (OBS-157; the kind list itself was fixed in #303).
        local = self.home / 'backups' / ('before-update-' + os.urandom(8).hex())
        upgrade._snapshot(self.home, local)
        vault.set_passphrase(self.home, PASS)
        self.assertIn('1 unencrypted snapshot(s)', ' '.join(vault.status(self.home)['warnings']))
        report = vault.migrate(self.home)
        self.assertEqual(report['exported'], 1)
        self.assertFalse(local.exists())
        exported = list(self.target().glob('before-update-*.obsnap'))
        self.assertEqual(len(exported), 1)
        self.assertEqual(vault.verify_file(exported[0], PASS)['kind'], 'before-update')
        status = vault.status(self.home)
        self.assertEqual(status['local_unencrypted_snapshots'], 0)
        self.assertFalse(any('unencrypted snapshot' in w for w in status['warnings']))
        self.assertEqual(status['artifacts']['before-update']['count'], 1)
        self.assertIn('machine', status['artifacts'])

    def test_every_kind_a_snapshot_is_written_as_is_rotated(self):
        # Every kind `workspace_upgrade.snapshot` accepts is one `migrate` and `status` read.
        self.assertIn('before-update', vault.SNAPSHOT_KINDS)
        for _ in range(vault.KEEP + 2):
            upgrade._snapshot(self.home, self.home / 'backups' / ('before-update-' + os.urandom(8).hex()))
        vault.rotate_local(self.home, 'before-update')
        self.assertEqual(len(vault.local_snapshots(self.home, 'before-update')), vault.KEEP)

    def test_requires_passphrase(self):
        with self.assertRaisesRegex(vault.BackupError, 'passphrase'):
            vault.migrate(self.home)


class BeforeUpdateInventory(Base):
    def make_update(self, suffix='fixture'):
        directory = self.home / 'backups' / ('before-update-' + suffix)
        upgrade._snapshot(self.home, directory)
        return directory

    def test_local_update_is_counted_with_and_without_passphrase(self):
        directory = self.make_update()
        vault.after_snapshot(self.home, directory, 'before-update')
        self.assertEqual(vault.status(self.home)['local_unencrypted_snapshots'], 1)
        vault.set_passphrase(self.home, PASS)
        report = vault.status(self.home)
        self.assertEqual(report['local_unencrypted_snapshots'], 1)
        self.assertTrue(any('1 unencrypted snapshot(s)' in w for w in report['warnings']))
        self.assertTrue(directory.is_dir())  # status is read-only

    def test_update_migrates_encrypted_and_restores_while_foreign_kind_stays(self):
        directory = self.make_update()
        foreign = self.home / 'backups' / 'foreign-kind-fixture'
        upgrade._snapshot(self.home, foreign)
        before = {p.relative_to(foreign): p.read_bytes() for p in foreign.rglob('*') if p.is_file()}
        vault.set_passphrase(self.home, PASS)
        report = vault.migrate(self.home)
        self.assertEqual(report['exported'], 1)
        self.assertEqual(report['removed'], 0)
        self.assertFalse(directory.exists())
        self.assertEqual(before, {p.relative_to(foreign): p.read_bytes() for p in foreign.rglob('*') if p.is_file()})
        artifacts = vault.artifacts(self.target(), 'before-update', vault.SNAPSHOT_SUFFIX)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(vault.verify_file(artifacts[0], PASS)['kind'], 'before-update')
        restored = vault.extract_tree(artifacts[0], self.base / 'restored-update', PASS)
        upgrade.verify_snapshot(restored)
        self.assertEqual(vault.status(self.home)['local_unencrypted_snapshots'], 0)
        self.assertEqual(vault.status(self.home)['artifacts']['before-update']['count'], 1)
        self.assertIn(artifacts[0], [row['snapshot'] for row in vault.candidates(self.home)])

    def test_invalid_update_is_reported_and_preserved(self):
        directory = self.home / 'backups' / 'before-update-unverified'
        directory.mkdir(parents=True)
        (directory / 'only-copy').write_bytes(b'synthetic unverified data')
        vault.set_passphrase(self.home, PASS)
        report = vault.migrate(self.home)
        self.assertEqual(report['exported'], 0)
        self.assertEqual(report['removed'], 0)
        self.assertEqual([row['path'] for row in report['skipped']], [str(directory)])
        self.assertEqual((directory / 'only-copy').read_bytes(), b'synthetic unverified data')


class Doctor(Base):
    def test_reports_backup_state_without_values(self):
        report = workspace.doctor(self.home)['backups']
        self.assertFalse(report['encrypted'])
        self.assertTrue(any('passphrase' in w for w in report['warnings']))
        vault.set_passphrase(self.home, PASS)
        report = workspace.doctor(self.home)['backups']
        self.assertTrue(report['encrypted'])
        self.assertNotIn(PASS, repr(report))

    def test_a_root_inside_the_workspace_is_named_as_sharing_the_disk_with_its_key(self):
        # With no ~/Documents (or off macOS) the root falls back to <home>/backups, and the
        # encrypted copies then sit in the same tree as secrets/backup-passphrase.
        vault.set_passphrase(self.home, PASS)
        with patch.dict(os.environ, {'OBSERVATORY_BACKUPS': str(self.home / 'nested')}):
            report = workspace.doctor(self.home)['backups']
        self.assertTrue(report['inside_workspace'])
        joined = ' '.join(report['warnings'])
        self.assertIn('inside the workspace', joined)
        self.assertIn('backup-passphrase', joined)
        self.assertIn('configure storage backups', joined)
        del os.environ['OBSERVATORY_BACKUPS']
        # With no ~/Documents the default is beside the workspace now, so only an explicit
        # root inside it (above) earns the warning.
        with patch.object(vault, 'documents_dir', return_value=self.base / 'no-documents'), \
                patch.object(vault, 'data_dir', return_value=self.base / 'xdg-data'):
            report = vault.status(self.home)
        self.assertEqual(report['root_source'], 'default-data')
        self.assertFalse(report['inside_workspace'])
        self.assertNotIn('inside the workspace', ' '.join(report['warnings']))
        # A root elsewhere carries no such warning.
        with patch.dict(os.environ, {'OBSERVATORY_BACKUPS': str(self.root)}):
            report = vault.status(self.home)
        self.assertFalse(report['inside_workspace'])
        self.assertEqual(report['warnings'], [])

    # OBS-349 part 1: the secret store the settings name, and a code on every warning.

    def configure(self, **sources):
        file = self.home / 'config' / 'settings.json'
        doc = json.loads(file.read_text())
        doc.setdefault('sources', {}).update({k: str(v) for k, v in sources.items()})
        file.write_text(json.dumps(doc))

    def carry(self, **record):
        file = self.home / 'store' / 'raw' / 'secret-store-carry.json'
        file.parent.mkdir(parents=True, exist_ok=True)
        at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        file.write_text(json.dumps({'at': at, 'kind': 'daily', 'file': 'daily-20261106T000000Z.obsnap',
                                    'state': 'carried', 'reason': None, 'files': 3, 'size': 120, **record}))

    def codes(self, report):
        return [item['code'] for item in report['warning_items']]

    def item(self, report, code):
        return next(i for i in report['warning_items'] if i['code'] == code)

    def test_a_store_inside_the_workspace_is_not_external(self):
        self.assertEqual(vault.status(self.home)['secret_store'], {'external': False})
        self.configure(secret_store=self.home / 'secrets')
        self.assertEqual(vault.status(self.home)['secret_store'], {'external': False})

    def test_an_external_store_not_carried_is_a_coded_warning(self):
        store = self.base / 'external-store'
        store.mkdir()
        self.configure(secret_store=store)
        report = vault.status(self.home)
        section = report['secret_store']
        self.assertTrue(section['external'])
        self.assertTrue(section['exists'])
        self.assertFalse(section['env_override'])
        self.assertEqual(section['settings_keys'], ['secret_store'])
        self.assertIsNone(section['last_carry'])
        item = self.item(report, 'backups-vault-store-not-carried')
        self.assertIn(str(store), item['text'])
        self.assertIn('another machine restored from these backups would not get these keys', item['text'])
        self.assertTrue(item['step'])
        self.assertIn(item['text'], report['warnings'])
        # A snapshot that could not encrypt names the passphrase as the step.
        self.carry(state='not-carried', reason='no-passphrase')
        item = self.item(vault.status(self.home), 'backups-vault-store-not-carried')
        self.assertIn('no-passphrase', item['text'])
        self.assertIn('backup-passphrase set', item['step'])
        # A carry older than 48 hours proves nothing about the store as it is now.
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=49)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.carry(at=old)
        self.assertIn('backups-vault-store-not-carried', self.codes(vault.status(self.home)))
        # A record that does not read cannot prove the store was carried.
        (self.home / 'store' / 'raw' / 'secret-store-carry.json').write_text('{not json')
        report = vault.status(self.home)
        self.assertEqual(report['secret_store']['last_carry'], {'state': 'unknown'})
        self.assertIn('backups-vault-store-not-carried', self.codes(report))

    def test_without_a_carrier_the_step_says_the_store_is_copied_by_hand(self):
        # A release whose snapshots never carry an external store must not send a person to take
        # another snapshot: that snapshot would not carry it either.
        store = self.base / 'external-store'
        store.mkdir()
        self.configure(secret_store=store)
        with patch.object(vault, 'secret_store_carrier', return_value=False):
            item = self.item(vault.status(self.home), 'backups-vault-store-not-carried')
        self.assertNotIn('workspace-backup', item['step'])
        self.assertIn(str(store), item['step'])
        with patch.object(vault, 'secret_store_carrier', return_value=True):
            item = self.item(vault.status(self.home), 'backups-vault-store-not-carried')
        self.assertIn('workspace-backup --writers-stopped', item['step'])

    def test_a_carried_store_is_not(self):
        store = self.base / 'external-store'
        store.mkdir()
        self.configure(secret_store=store)
        self.carry()
        report = vault.status(self.home)
        self.assertEqual(report['secret_store']['last_carry']['state'], 'carried')
        self.assertEqual(report['secret_store']['last_carry']['files'], 3)
        self.assertNotIn('backups-vault-store-not-carried', self.codes(report))

    def test_a_missing_configured_store_is_named_with_its_configure_step(self):
        store = self.base / 'gone-store'
        self.configure(secrets=store)
        report = vault.status(self.home)
        self.assertFalse(report['secret_store']['exists'])
        self.assertEqual(report['secret_store']['settings_keys'], ['secrets'])
        self.assertNotIn('backups-vault-store-not-carried', self.codes(report), 'the missing store says more')
        item = self.item(report, 'backups-secret-store-missing')
        self.assertIn(str(store), item['text'])
        self.assertIn('configure sources secrets PATH', item['step'])

    def test_nested_settings_name_the_outer_store_once(self):
        outer = self.base / 'outer-store'
        (outer / 'inner').mkdir(parents=True)
        self.configure(secret_store=outer / 'inner', secrets=outer)
        section = vault.status(self.home)['secret_store']
        self.assertEqual(section['path'], str(outer))
        self.assertEqual(section['settings_keys'], ['secret_store', 'secrets'])

    def test_existing_backup_warnings_keep_their_text_and_gain_codes(self):
        report = vault.status(self.home)
        self.assertEqual(self.codes(report), ['backups-no-passphrase'])
        self.assertEqual([i['text'] for i in report['warning_items']], report['warnings'])
        vault.set_passphrase(self.home, PASS)
        (self.home.parent / f'{self.home.name}.failed-update-20261101T000000Z').mkdir()
        (self.home / 'backups' / 'daily-20261101T000000Z').mkdir(parents=True)
        with patch.dict(os.environ, {'OBSERVATORY_BACKUPS': str(self.home / 'nested')}):
            report = vault.status(self.home)
        self.assertEqual(self.codes(report), ['backups-root-inside-workspace', 'backups-failed-update-copy',
                                              'backups-unencrypted-local'])
        self.assertEqual([i['text'] for i in report['warning_items']], report['warnings'])
        steps = {i['code']: i['step'] for i in report['warning_items']}
        self.assertIn('configure storage backups', steps['backups-root-inside-workspace'])
        self.assertIn('backups migrate', steps['backups-unencrypted-local'])
        self.assertNotIn(PASS, repr(report))

    def test_the_environment_override_is_named(self):
        store = self.base / 'external-store'
        (store / 'projects').mkdir(parents=True)
        self.configure(secret_store=store)
        self.carry()
        # The environment repeating what the settings name moves nothing.
        with patch.dict(os.environ, {'OBSERVATORY_VAULT_DIR': str(store / 'projects')}):
            report = vault.status(self.home)
        self.assertFalse(report['secret_store']['env_override'])
        self.assertNotIn('backups-secret-store-env-override', self.codes(report))
        with patch.dict(os.environ, {'OBSERVATORY_VAULT_DIR': str(self.base / 'elsewhere')}):
            report = vault.status(self.home)
        self.assertTrue(report['secret_store']['env_override'])
        item = self.item(report, 'backups-secret-store-env-override')
        self.assertIn('OBSERVATORY_VAULT_DIR', item['text'])
        self.assertIn('configure sources secret_store PATH', item['step'])
        # A store inside the workspace is carried whole, but a slot folder the environment names is not.
        self.configure(secret_store=self.home / 'secrets')
        with patch.dict(os.environ, {'OBSERVATORY_VAULT_DIR': str(self.base / 'elsewhere')}):
            report = vault.status(self.home)
        self.assertEqual(report['secret_store'], {'external': False})
        self.assertIn('backups-secret-store-env-override', self.codes(report))

    def test_observatory_secrets_moves_nothing_while_secret_store_is_set(self):
        # The vault's root is `sources.secret_store` first; OBSERVATORY_SECRETS is only its fallback.
        store = self.base / 'external-store'
        store.mkdir()
        self.configure(secret_store=store)
        self.carry()
        with patch.dict(os.environ, {'OBSERVATORY_SECRETS': str(self.base / 'elsewhere')}):
            report = vault.status(self.home)
        self.assertFalse(report['secret_store']['env_override'])
        self.assertEqual(self.codes(report), ['backups-no-passphrase'])

    def test_observatory_secrets_moves_the_vault_when_only_secrets_is_set(self):
        store = self.base / 'external-store'
        store.mkdir()
        self.configure(secrets=store)
        self.carry()
        with patch.dict(os.environ, {'OBSERVATORY_SECRETS': str(store)}):
            self.assertNotIn('backups-secret-store-env-override', self.codes(vault.status(self.home)))
        with patch.dict(os.environ, {'OBSERVATORY_SECRETS': str(self.base / 'elsewhere')}):
            report = vault.status(self.home)
        self.assertTrue(report['secret_store']['env_override'])
        self.assertIn('OBSERVATORY_SECRETS', self.item(report, 'backups-secret-store-env-override')['text'])

    def test_a_vault_dir_elsewhere_is_named(self):
        store = self.base / 'external-store'
        store.mkdir()
        self.configure(secret_store=store)
        self.carry()
        with patch.dict(os.environ, {'OBSERVATORY_VAULT_DIR': str(self.base / 'other-slots')}):
            report = vault.status(self.home)
        self.assertTrue(report['secret_store']['env_override'])
        self.assertIn('OBSERVATORY_VAULT_DIR', self.item(report, 'backups-secret-store-env-override')['names'])

    def test_a_store_only_the_environment_names_is_reported_as_external(self):
        # No setting names a store, OBSERVATORY_SECRETS points outside the workspace: the vault's keys
        # live there, no snapshot carries them, and doctor says where they came from.
        store = self.base / 'env-store'
        store.mkdir()
        with patch.dict(os.environ, {'OBSERVATORY_SECRETS': str(store)}):
            report = vault.status(self.home)
        section = report['secret_store']
        self.assertTrue(section['external'])
        self.assertEqual(section['path'], str(store))
        self.assertEqual(section['settings_keys'], [])
        self.assertEqual(section['environment'], 'OBSERVATORY_SECRETS')
        self.assertTrue(section['env_override'])
        self.assertIn('backups-secret-store-env-override', self.codes(report))
        self.assertNotIn('backups-vault-store-not-carried', self.codes(report), 'one row for one cause')
        # Inside the workspace it is carried with it.
        with patch.dict(os.environ, {'OBSERVATORY_SECRETS': str(self.home / 'other-secrets')}):
            report = vault.status(self.home)
        self.assertEqual(report['secret_store'], {'external': False})


class FakeStore:
    """The OS credential store, in memory: what a restore reads, and proof it never writes."""

    def __init__(self, items=None):
        self.items, self.puts = dict(items or {}), []

    def get(self, label):
        return self.items.get(label)

    def put(self, label, value, replace=False):
        self.puts.append(label)
        self.items[label] = value
        return 'file'

    def kind(self):
        return 'file'


def stdin_of(data: bytes):
    return io.TextIOWrapper(io.BytesIO(data), encoding='utf-8')


class PassphraseStdin(unittest.TestCase):
    """OBS-348: what `--passphrase-stdin` accepts and refuses. No workspace and no backup, so it runs
    wherever Python does, Windows included."""

    def test_stdin_is_refused_when_it_is_a_terminal_empty_or_too_long(self):
        read = vault.read_passphrase_stdin
        self.assertEqual(read(stdin_of(b'a value from a file\n')), 'a value from a file')
        self.assertEqual(read(stdin_of(b'a value from windows\r\n')), 'a value from windows')
        self.assertEqual(read(stdin_of(b' kept as typed \n\n')), ' kept as typed \n', 'only one newline goes')
        self.assertEqual(read(stdin_of(b'x' * vault.STDIN_LIMIT + b'\n')), 'x' * vault.STDIN_LIMIT)
        for data, code in ((b'', 'passphrase-stdin-empty'), (b'\n', 'passphrase-stdin-empty'),
                           (b'x' * (vault.STDIN_LIMIT + 1), 'passphrase-stdin-too-long'),
                           (b'\xff\xfe', 'passphrase-stdin-not-text')):
            with self.subTest(code=code), self.assertRaises(vault.BackupError) as caught:
                read(stdin_of(data))
            self.assertEqual(caught.exception.code, code)
        # A terminal: `osprivacy.is_terminal` decides (a real console on Windows, `isatty` elsewhere).
        with patch.object(vault.osprivacy, 'is_terminal', return_value=True), \
                self.assertRaises(vault.BackupError) as caught:
            read(stdin_of(b'typed\n'))
        self.assertEqual(caught.exception.code, 'passphrase-stdin-terminal')
        closed = stdin_of(b'never read\n')
        closed.close()
        for stream in (None, closed):   # no standard input at all, or one already closed: a typed refusal
            with self.subTest(stream=stream), patch.object(sys, 'stdin', None), \
                    self.assertRaises(vault.BackupError) as caught:
                read(stream)
            self.assertEqual(caught.exception.code, 'passphrase-stdin-empty')


class ForeignPassphrase(Base):
    """OBS-348 (D2): another machine's backup opens with its passphrase given once, on stdin or
    at a terminal; it is never stored and never replaces this machine's own."""
    OTHER = 'a synthetic passphrase of the second machine'

    def setUp(self):
        super().setUp()
        cheap = patch.dict(vault.SCRYPT, {'n': 2 ** 12})
        cheap.start()
        self.addCleanup(cheap.stop)
        vault.set_passphrase(self.home, PASS)
        self.file = Path(upgrade.snapshot(self.home, writers_stopped=True)['snapshot'])
        # The second machine: its own workspace, its own passphrase, its own store.
        self.second = self.base / 'second workspace'
        workspace.initialize(self.second)
        vault.set_passphrase(self.second, self.OTHER)
        self.store = FakeStore({vault._workspace_label(self.second): self.OTHER})

    def sealed(self):
        """The second machine's passphrase file and store, as digests: compared, never printed."""
        import hashlib
        return (hashlib.sha256(vault.passphrase_file(self.second).read_bytes()).hexdigest(),
                hashlib.sha256(json.dumps(self.store.items, sort_keys=True).encode()).hexdigest())

    def test_a_snapshot_of_another_workspace_opens_with_a_value_on_stdin_and_nothing_is_stored(self):
        before = self.sealed()
        dest = self.base / 'restored on the second machine'
        with patch.object(vault, 'SecretStore', return_value=self.store):
            result = upgrade.restore(self.file, dest, stdin_value=PASS, second_machine=True)
        self.assertEqual(result['passphrase_source'], 'stdin')
        self.assertEqual(self.sealed(), before)
        self.assertEqual(self.store.puts, [])
        self.assertFalse((dest / 'secrets/backup-passphrase').exists(), 'the second machine makes its own')
        self.assertEqual((dest / 'secrets/demo-slot').read_text(), 'synthetic opaque private value')
        self.assertNotIn(PASS, json.dumps(result))

    def test_the_prompt_is_asked_once_and_only_after_every_other_candidate_failed(self):
        asked = []

        def typed(prompt):
            asked.append(prompt)
            return PASS

        wrong = FakeStore({self.file.parent.name: 'a stored value that is not it'})
        with patch.object(vault, 'SecretStore', return_value=wrong), \
                patch.object(vault.osprivacy, 'is_terminal', return_value=True), \
                patch('getpass.getpass', side_effect=typed), \
                patch.dict(os.environ, {vault.PASS_ENV: 'an environment value that is not it'}):
            result = upgrade.restore(self.file, self.base / 'asked', stdin_value='a piped value that is not it')
        self.assertEqual(result['passphrase_source'], 'prompt')
        self.assertEqual(asked, [f'Backup passphrase for {self.file.parent.name}: '])
        asked.clear()
        with patch.object(vault, 'SecretStore', return_value=FakeStore()), \
                patch.object(vault.osprivacy, 'is_terminal', return_value=True), \
                patch('getpass.getpass', side_effect=lambda prompt: asked.append(prompt) or 'typed wrong'):
            with self.assertRaises(vault.BackupError) as caught:
                upgrade.restore(self.file, self.base / 'typed wrong')
        self.assertEqual(caught.exception.code, 'passphrase-refused')
        self.assertEqual(len(asked), 1, 'one prompt per run, so a script cannot loop on it')
        self.assertFalse((self.base / 'typed wrong').exists())
        with patch.object(vault, 'SecretStore', return_value=FakeStore({self.file.parent.name: PASS})), \
                patch.object(vault.osprivacy, 'is_terminal', return_value=True), \
                patch('getpass.getpass', side_effect=AssertionError('no prompt when a stored value opens it')):
            self.assertEqual(upgrade.restore(self.file, self.base / 'stored')['passphrase_source'], 'stored')

    def test_without_a_terminal_and_without_stdin_the_refusal_names_the_sources_tried(self):
        wrong = FakeStore({self.file.parent.name: 'a stored value that is not it',
                           self.file.parent.name + vault.PREVIOUS: 'an earlier stored value that is not it'})
        with patch.object(vault, 'SecretStore', return_value=wrong), \
                patch.object(vault.osprivacy, 'is_terminal', return_value=False), \
                patch.dict(os.environ, {vault.PASS_ENV: 'an environment value that is not it'}):
            with self.assertRaises(vault.BackupError) as caught:
                upgrade.restore(self.file, self.base / 'refused')
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, 'passphrase-refused')
        for source in ('environment', 'stored', 'stored-previous'):
            self.assertIn(source, message)
        self.assertIn('--passphrase-stdin', message)
        self.assertNotIn('not it', message, 'a value is never in a refusal')
        self.assertFalse((self.base / 'refused').exists())

    def test_a_wrong_candidate_costs_one_chunk_not_a_whole_file(self):
        src, enc = self.base / 'plain.bin', self.base / 'many chunks' / 'out.obsdb'
        src.write_bytes(os.urandom(20_000))
        with patch.object(vault, 'CHUNK', 1024):
            vault.encrypt_file(src, enc, PASS, kind='observatory-db')
        opened = []
        original = vault._Reader._open_next

        def counting(reader):
            opened.append(1)
            return original(reader)

        with patch.object(vault._Reader, '_open_next', counting):
            self.assertFalse(vault.opens(enc, 'not the passphrase at all'))
            self.assertEqual(len(opened), 1)
            opened.clear()
            secret, header, source = vault.open_with(enc, base=None, label=None, given='not the passphrase at all',
                                                     stdin_value=PASS)
        self.assertEqual((secret == PASS, source, header['kind']), (True, 'stdin', 'observatory-db'))
        self.assertEqual(len(opened), 1 + 1 + 20, 'one chunk for the wrong one, then the winner once in full')
        with open(enc, 'r+b') as f:   # a damaged header is not a passphrase problem
            f.write(b'NOTOURS\n')
        with self.assertRaises(vault.BackupError) as caught:
            vault.open_with(enc, base=None, label=None, given=PASS)
        self.assertIsNone(caught.exception.code)

    def test_the_command_line_refuses_an_empty_stdin_and_restores_with_a_value(self):
        # From the command line: refused before any write, exit 2, the code named.
        dest = self.base / 'never made'
        err = io.StringIO()
        with patch.dict(os.environ, {'OBSERVATORY_HOME': str(dest)}), patch.object(sys, 'stdin', stdin_of(b'')), \
                patch.object(sys, 'stderr', err), patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(upgrade.main(['restore', str(self.file), '--passphrase-stdin']), 2)
        self.assertIn('passphrase-stdin-empty', err.getvalue())
        self.assertFalse(dest.exists())
        out = io.StringIO()
        with patch.dict(os.environ, {'OBSERVATORY_HOME': str(dest), 'OBSERVATORY_SYSTEM_SETUP': '0'}), \
                patch.object(sys, 'stdin', stdin_of(PASS.encode() + b'\n')), \
                patch.object(sys, 'stderr', io.StringIO()), patch.object(sys, 'stdout', out):
            self.assertEqual(upgrade.main(['restore', str(self.file), '--passphrase-stdin']), 0)
        self.assertEqual(json.loads(out.getvalue())['passphrase_source'], 'stdin')

    def test_restore_latest_asks_once_per_foreign_label(self):
        import maintenance
        upgrade.snapshot(self.home, writers_stopped=True)   # two snapshots under one label
        import shutil
        shutil.rmtree(self.home)
        asked = []
        foreign = FakeStore()   # this machine's store never held the other machine's passphrase
        with patch.object(vault.osprivacy, 'is_terminal', return_value=False):
            out = maintenance.restore_latest(self.home, store=foreign, ask=True)
        self.assertEqual(out['status'], 'backups-found-locked')
        self.assertIn('--passphrase-stdin < FILE', out['next'])
        self.assertFalse(self.home.exists() and any(self.home.iterdir()))
        with patch.object(vault.osprivacy, 'is_terminal', return_value=True), \
                patch('getpass.getpass', side_effect=lambda prompt: asked.append(prompt) or PASS):
            self.assertEqual(maintenance.restore_latest(self.home, store=foreign)['status'], 'backups-found-locked',
                             'only `restore --latest` asks; `init` names the backups and the steps')
            self.assertEqual(asked, [])
            out = maintenance.restore_latest(self.home, store=foreign, ask=True)
        self.assertEqual((out['status'], out['passphrase_source']), ('restored', 'prompt'))
        self.assertEqual(len(asked), 1)
        self.assertEqual(foreign.puts, [], 'nothing is stored')
        shutil.rmtree(self.home)
        with patch.object(vault.osprivacy, 'is_terminal', return_value=False):
            out = maintenance.restore_latest(self.home, store=foreign, stdin_value=PASS)
        self.assertEqual((out['status'], out['passphrase_source']), ('restored', 'stdin'))



class ThisMachineOnly(Base):
    """OBS-350 (spec 3.2a): a slot marked with `full vault-digests keep-here` stays on this machine; the
    encrypted snapshot carries the rest of the store and names it."""

    SLOT = 'projects/alpha/prod/API_KEY'

    def setUp(self):
        super().setUp()
        cheap = patch.dict(vault.SCRYPT, {'n': 2 ** 12})
        cheap.start()
        self.addCleanup(cheap.stop)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from secret_store_fixture import make_external_store, all_values
        self.values = all_values()
        self.store = make_external_store(self.base / 'external-store')
        file = self.home / 'config' / 'settings.json'
        doc = json.loads(file.read_text())
        doc.setdefault('sources', {})['secret_store'] = str(self.store)
        file.write_text(json.dumps(doc))
        vault.set_passphrase(self.home, PASS)
        self.outputs = []

    def tearDown(self):
        text = '\n'.join(self.outputs)
        for value in self.values:
            self.assertNotIn(value, text, 'a value reached an answer, a manifest or a command\'s output')
        super().tearDown()

    def tool(self, name, *args, value=None):
        env = {**os.environ, 'OBSERVATORY_VAULT_DIR': str(self.store / 'projects')}
        run = subprocess.run([sys.executable, str(ROOT / 'tools' / name), *args], input=value, text=True,
                             capture_output=True, env=env, timeout=60)
        self.outputs.append(run.stdout + run.stderr)
        return run

    def carried(self):
        result = upgrade.snapshot(self.home, writers_stopped=True, kind='daily')
        opened = vault.extract_tree(Path(result['snapshot']), self.base / 'opened', PASS)
        self.addCleanup(__import__('shutil').rmtree, opened, True)
        manifest = config.read_json(opened / 'manifest.json')
        self.outputs += [json.dumps(result), json.dumps(manifest)]
        return opened / 'external' / 'secret_store', manifest['externals'][0]

    def test_a_marked_slot_is_left_out_and_named(self):
        run = self.tool('vault_digests.py', 'keep-here', 'alpha', 'prod', 'API_KEY', '--why', 'root seed, one home')
        self.assertEqual(run.returncode, 0, run.stderr)
        meta = json.loads((self.store / (self.SLOT + '.meta.json')).read_text())
        self.assertEqual(meta['this_machine_only']['why'], 'root seed, one home')
        store, entry = self.carried()
        self.assertEqual(entry['state'], 'carried', 'a slot kept here does not make the store not carried')
        self.assertFalse((store / self.SLOT).exists())
        self.assertFalse((store / (self.SLOT + '.meta.json')).exists())
        self.assertIn({'path': self.SLOT, 'reason': 'this-machine-only'}, entry['not_carried'])
        self.assertNotIn(self.SLOT, {f['path'] for f in entry['files']})
        self.assertEqual((entry['kept_here'], entry['vault_slots']), (1, 2))
        self.assertTrue((store / 'projects/alpha/stage/DB_PASSWORD').is_file(), 'the other slots travel')
        moves = (self.store / 'projects' / 'movements.jsonl').read_text()
        self.assertIn('mark-this-machine-only', moves)

    def test_unmarking_carries_the_slot_in_the_next_snapshot(self):
        self.assertEqual(self.tool('vault_digests.py', 'keep-here', 'alpha', 'prod', 'API_KEY',
                                   '--why', 'root seed, one home').returncode, 0)
        run = self.tool('vault_digests.py', 'unkeep-here', 'alpha', 'prod', 'API_KEY')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('unmark-this-machine-only', (self.store / 'projects' / 'movements.jsonl').read_text())
        store, entry = self.carried()
        self.assertEqual((store / self.SLOT).read_bytes(), (self.store / self.SLOT).read_bytes())
        self.assertEqual(entry['kept_here'], 0)
        # Clearing a mark that is not there is refused.
        self.assertEqual(self.tool('vault_digests.py', 'unkeep-here', 'alpha', 'prod', 'API_KEY').returncode, 2)

    def test_marking_a_slot_this_machine_does_not_hold_is_refused(self):
        run = self.tool('vault_digests.py', 'keep-here', 'alpha', 'prod', 'ABSENT', '--why', 'root seed, one home')
        self.assertEqual(run.returncode, 2)
        run = self.tool('vault_digests.py', 'keep-here', 'alpha', 'prod', 'API_KEY', '--why', 'short')
        self.assertEqual(run.returncode, 2, 'a reason of at least 8 characters, as mark asks')

    def test_the_mark_survives_a_new_value(self):
        self.assertEqual(self.tool('vault_digests.py', 'keep-here', 'alpha', 'prod', 'API_KEY',
                                   '--why', 'root seed, one home').returncode, 0)
        run = self.tool('vault.py', 'put', 'alpha', 'prod', 'API_KEY', '--force', value='fixture-value-008')
        self.assertEqual(run.returncode, 0, run.stderr)
        meta = json.loads((self.store / (self.SLOT + '.meta.json')).read_text())
        self.assertIn('this_machine_only', meta)
        store, entry = self.carried()
        self.assertFalse((store / self.SLOT).exists())


if __name__ == '__main__':
    unittest.main()
