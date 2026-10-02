#!/usr/bin/env python3
"""Encrypted backups outside the workspace: one root, one passphrase, one format.

Every test runs in a synthetic workspace with OBSERVATORY_BACKUPS pointed at a
temporary directory, so no test can reach the operator's real Documents folder.
"""
from __future__ import annotations
import importlib
import io
import os
from pathlib import Path
import sqlite3
import stat
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

    def test_settings_then_documents_then_workspace(self):
        del os.environ['OBSERVATORY_BACKUPS']
        docs = self.base / 'Documents'
        with patch.object(vault, 'documents_dir', return_value=docs), patch.object(vault.sys, 'platform', 'darwin'):
            self.assertEqual(vault.root_info(self.home)['source'], 'default-workspace')  # no Documents yet
            docs.mkdir()
            info = vault.root_info(self.home)
            self.assertEqual(info['source'], 'default-documents')
            self.assertEqual(info['path'].parent, docs / 'Project Observatory' / 'Backups')
            chosen = self.base / 'chosen'
            self.assertEqual(workspace.main(['configure', 'storage', 'backups', str(chosen)]), 0)
            self.assertEqual(vault.root_info(self.home)['source'], 'settings')
            self.assertEqual(vault.root_info(self.home)['path'].parent, chosen)
        with patch.object(vault, 'documents_dir', return_value=docs), patch.object(vault.sys, 'platform', 'linux'):
            doc = config.load(self.home); doc.pop('storage')
            workspace.write_json(self.home / 'config/settings.json', doc)
            self.assertEqual(vault.root_info(self.home)['path'], self.home / 'backups')

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


class StoreCopies(Base):
    def run_backup(self, *args):
        sys.path.insert(0, str(ROOT / 'tools'))
        import backup_store
        importlib.reload(backup_store)
        return backup_store.main(['backup_store.py', *args])

    def test_encrypted_daily_copy_and_rotation(self):
        vault.set_passphrase(self.home, PASS)
        for i in range(5):
            with patch.object(vault, 'stamp', return_value=f'20260101T00{i:02d}00Z'):
                self.assertEqual(self.run_backup(), 0)
        kept = sorted(self.target().glob('observatory-db-*.obsdb'))
        self.assertEqual(len(kept), vault.KEEP)
        self.assertEqual(list((self.home / 'store').glob('observatory.db.backup-*')), [])
        out = self.base / 'copy.db'
        with out.open('wb') as stream:
            vault.decrypt_to(kept[-1], stream, PASS)
        conn = sqlite3.connect(out)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], 'ok')
        conn.close()

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

    def test_requires_passphrase(self):
        with self.assertRaisesRegex(vault.BackupError, 'passphrase'):
            vault.migrate(self.home)


class Doctor(Base):
    def test_reports_backup_state_without_values(self):
        report = workspace.doctor(self.home)['backups']
        self.assertFalse(report['encrypted'])
        self.assertTrue(any('passphrase' in w for w in report['warnings']))
        vault.set_passphrase(self.home, PASS)
        report = workspace.doctor(self.home)['backups']
        self.assertTrue(report['encrypted'])
        self.assertNotIn(PASS, repr(report))


if __name__ == '__main__':
    unittest.main()
