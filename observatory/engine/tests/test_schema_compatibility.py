#!/usr/bin/env python3
"""Synthetic-only upgrade, downgrade refusal, WAL backup and concurrency checks."""
from __future__ import annotations
import hashlib
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def open_worker(target: str, home: str, queue) -> None:
    os.environ['OBSERVATORY_HOME'] = home
    os.environ['OBSERVATORY_REGISTRY'] = home + '/registry'
    from store import db
    try:
        conn = db.connect(Path(target))
        try:
            queue.put(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        queue.put(f"{type(exc).__name__}: {exc}")



#: Written out rather than read from `migrate.MIGRATIONS`, so that adding a
#: migration fails here until someone has looked at what the upgrade matrix now
#: covers. 0008 added the agent-memory workflow tables; 0010 the vector-namespace registry.
CURRENT_MIGRATIONS = 10

class SchemaCompatibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            'OBSERVATORY_HOME': str(self.home),
            'OBSERVATORY_REGISTRY': str(self.home / 'registry'),
            'OBSERVATORY_DB': str(self.home / 'data.db'),
            'OBSERVATORY_SCRATCH': str(self.home / 'scratch'),
            'OBSERVATORY_STATE': str(self.home / 'state'),
            'OBSERVATORY_DASHBOARD': str(self.home / 'docs' / 'page.html'),
        })
        self.env.start()
        import importlib, paths
        importlib.reload(paths)
        from store import db, migrate
        importlib.reload(db)
        self.db, self.migrate = db, migrate
        self.target = self.home / 'data.db'

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def legacy(self):
        conn = sqlite3.connect(self.target)
        conn.row_factory = sqlite3.Row
        conn.executescript(self.db.SCHEMA.read_text())
        conn.execute('CREATE TABLE migrations (id TEXT PRIMARY KEY, applied_at TEXT, note TEXT)')
        conn.execute("INSERT INTO events(id, project_id, kind, occurred_at) VALUES ('event-one','project:demo','commit','2026-01-02T10:00:00+02:00')")
        conn.commit()
        return conn

    def digest(self):
        return hashlib.sha256(self.target.read_bytes()).hexdigest()

    def test_fresh_store_and_repeat(self):
        conn = self.db.connect(self.target)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], CURRENT_MIGRATIONS)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], CURRENT_MIGRATIONS)
        for suffix in ('', '-wal', '-shm'):
            file = Path(str(self.target) + suffix)
            if file.exists():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.migrate.apply(conn), [])
        conn.close()
        conn = self.db.connect(self.target)
        conn.close()
        self.assertFalse((self.home / 'migration-backups').exists())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_legacy_upgrade_preserves_event_and_backup(self):
        conn = self.legacy()
        conn.close()
        conn = self.db.connect(self.target)
        row = conn.execute("SELECT id, occurred_at FROM events WHERE id='event-one'").fetchone()
        self.assertEqual(tuple(row), ('event-one', '2026-01-02T08:00:00Z'))
        conn.close()
        copies = list((self.home / 'migration-backups').glob('*.db'))
        self.assertEqual(len(copies), 1)
        old = sqlite3.connect(copies[0])
        self.assertEqual(old.execute("SELECT occurred_at FROM events").fetchone()[0], '2026-01-02T10:00:00+02:00')
        old.close()
        conn = self.db.connect(self.target)
        conn.close()
        self.assertEqual(len(list((self.home / 'migration-backups').glob('*.db'))), 1)
        self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)

    def test_unknown_migration_refused_without_write(self):
        conn = self.legacy()
        conn.execute("INSERT INTO migrations VALUES ('0099-future','2026-01-01','')")
        conn.commit()
        conn.close()
        before = self.digest()
        with self.assertRaises(self.migrate.CompatibilityError):
            self.db.connect(self.target)
        self.assertEqual(before, self.digest())
        self.assertFalse(self.target.with_name(self.target.name + '.upgrade.lock').exists())

    def test_future_version_refused_without_write(self):
        conn = self.legacy()
        conn.execute('PRAGMA user_version=999')
        conn.close()
        before = self.digest()
        with self.assertRaises(self.migrate.CompatibilityError):
            self.db.connect(self.target)
        self.assertEqual(before, self.digest())

    def test_checksum_drift_refused(self):
        conn = self.db.connect(self.target)
        conn.execute("UPDATE migration_checksums SET checksum='wrong'")
        conn.commit()
        conn.close()
        before = self.digest()
        with self.assertRaises(self.migrate.CompatibilityError):
            self.db.connect(self.target)
        self.assertEqual(before, self.digest())

    def test_partial_failure_rolls_back_ddl_and_dml(self):
        conn = self.legacy()
        def failed(c):
            c.execute('CREATE TABLE should_not_survive (id INTEGER)')
            raise RuntimeError('synthetic migration fault')
        with patch.object(self.migrate, 'MIGRATIONS', self.migrate.MIGRATIONS + [('0008-test-fault', failed)]):
            with self.assertRaises(RuntimeError):
                self.migrate.apply(conn)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], 0)
        self.assertEqual(conn.execute('SELECT occurred_at FROM events').fetchone()[0], '2026-01-02T10:00:00+02:00')
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='should_not_survive'").fetchone())
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='migration_checksums'").fetchone())
        conn.close()

    def test_failed_connect_upgrade_keeps_valid_backup(self):
        conn = self.legacy()
        conn.close()
        def failed(c):
            c.execute('CREATE TABLE transient (id INTEGER)')
            raise RuntimeError('synthetic fault')
        with patch.object(self.migrate, 'MIGRATIONS', self.migrate.MIGRATIONS + [('0008-test-fault', failed)]):
            with self.assertRaises(RuntimeError):
                self.db.connect(self.target)
        copies = list((self.home / 'migration-backups').glob('*.db'))
        self.assertEqual(len(copies), 1)
        for target in (self.target, copies[0]):
            conn = sqlite3.connect(target)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='transient'").fetchone())
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            conn.close()

    def test_nested_transaction_is_not_committed(self):
        conn = self.legacy()
        conn.execute("UPDATE events SET actor='pending'")
        self.migrate.apply(conn)
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], 0)
        self.assertIsNone(conn.execute('SELECT actor FROM events').fetchone()[0])
        conn.close()

    def test_wal_content_in_backup(self):
        conn = self.legacy()
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute("UPDATE events SET actor='wal-only'")
        conn.commit()
        self.assertTrue(Path(str(self.target) + '-wal').exists())
        from store.compatibility import backup
        copy = backup(conn, self.target)
        old = sqlite3.connect(copy)
        self.assertEqual(old.execute('SELECT actor FROM events').fetchone()[0], 'wal-only')
        old.close()
        conn.close()

    def test_legacy_recorded_history_adopted_without_reapplying(self):
        conn = self.db.connect(self.target)
        conn.execute('DROP TABLE migration_checksums')
        conn.execute('PRAGMA user_version=0')
        conn.commit()
        conn.close()
        conn = self.db.connect(self.target)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM migration_checksums').fetchone()[0], CURRENT_MIGRATIONS)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], CURRENT_MIGRATIONS)
        conn.close()
        self.assertEqual(len(list((self.home / 'migration-backups').glob('*.db'))), 1)

    def test_every_historical_prefix_upgrades(self):
        for count in range(8):
            with self.subTest(applied=count):
                target = self.home / f'prefix-{count}.db'
                conn = sqlite3.connect(target)
                conn.row_factory = sqlite3.Row
                conn.executescript(self.db.SCHEMA.read_text())
                conn.execute('CREATE TABLE migrations (id TEXT PRIMARY KEY, applied_at TEXT, note TEXT)')
                for mid, fn in self.migrate.MIGRATIONS[:count]:
                    note = fn(conn)
                    conn.execute('INSERT INTO migrations VALUES (?,?,?)', (mid, '2026-01-01T00:00:00Z', note))
                conn.commit()
                conn.close()
                conn = self.db.connect(target)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], CURRENT_MIGRATIONS)
                self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                conn.close()

    def test_future_direct_apply_refused_without_creating_metadata(self):
        conn = self.legacy()
        conn.execute('PRAGMA user_version=99')
        with self.assertRaises(self.migrate.CompatibilityError):
            self.migrate.apply(conn)
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='migration_checksums'").fetchone())
        conn.close()

    def test_busy_lock_times_out(self):
        from store.compatibility import upgrade_lock
        with upgrade_lock(self.target):
            with self.assertRaises(TimeoutError):
                with upgrade_lock(self.target, timeout=0.01):
                    self.fail('second opener acquired exclusive lock')

    def test_a_file_mid_creation_does_not_fail_the_opener(self):
        from store import compatibility, db
        from unittest.mock import patch
        calls = []
        real = compatibility.preflight
        def racing(target):
            calls.append(target)
            if len(calls) == 1:
                raise sqlite3.DatabaseError("file is being created by another process")
            return real(target)
        with patch.object(compatibility, "preflight", racing):
            conn = db.connect(self.target)
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], CURRENT_MIGRATIONS)
            finally:
                conn.close()
        self.assertEqual(len(calls), 2, "the check under the lock still runs")

    def test_a_wal_that_vanishes_between_the_look_and_the_read_is_no_wal(self):
        # The last connection to close deletes the WAL; a reader that saw it and then
        # read its size failed with FileNotFoundError (0.18.0 gate, 2026-10-06).
        from store import compatibility, db
        conn = db.connect(self.target)
        conn.close()
        wal = Path(str(self.target) + "-wal")
        wal.write_bytes(b"")
        real_stat = Path.stat

        def vanished(path, *a, **kw):
            if str(path).endswith("-wal"):
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real_stat(path, *a, **kw)
        with patch.object(Path, "stat", vanished):
            uri = compatibility.readonly_uri(self.target)
        self.assertIn("mode=ro", uri)
        wal.unlink(missing_ok=True)

    def test_many_concurrent_first_opens(self):
        ctx = multiprocessing.get_context('spawn')
        for round_ in range(3):
            target = self.target.with_name(f"concurrent-{round_}.db")
            q = ctx.Queue()
            workers = [ctx.Process(target=open_worker, args=(str(target), str(self.home), q)) for _ in range(4)]
            for p in workers:
                p.start()
            for p in workers:
                p.join(60)
                self.assertEqual(p.exitcode, 0)
            got = [q.get(timeout=5) for _ in workers]
            # Each worker answers its count or its error's text, so a failure says which.
            self.assertEqual(got.count(CURRENT_MIGRATIONS), 4, got)
            q.close()

    def test_concurrent_first_open(self):
        ctx = multiprocessing.get_context('spawn')
        q = ctx.Queue()
        workers = [ctx.Process(target=open_worker, args=(str(self.target), str(self.home), q)) for _ in range(2)]
        for p in workers:
            p.start()
        for p in workers:
            p.join(20)
            if p.is_alive():
                p.terminate()
                p.join()
                self.fail('concurrent opener timed out')
            self.assertEqual(p.exitcode, 0)
        self.assertEqual([q.get(timeout=2) for _ in workers], [CURRENT_MIGRATIONS] * 2)
        q.close()

    def test_corrupt_database_not_replaced(self):
        self.target.write_bytes(b'not a database')
        before = self.digest()
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.connect(self.target)
        self.assertEqual(before, self.digest())

    def test_readonly_database_is_not_made_writable(self):
        conn = self.legacy()
        conn.close()
        self.target.chmod(0o400)
        before = self.digest()
        with self.assertRaises(PermissionError):
            self.db.connect(self.target)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o400)
        self.assertEqual(before, self.digest())
        self.target.chmod(0o600)

    def test_database_symlink_refused(self):
        real = self.home / 'other.db'
        real.write_bytes(b'untouched')
        self.target.symlink_to(real)
        with self.assertRaises(self.migrate.CompatibilityError):
            self.db.connect(self.target)
        self.assertEqual(real.read_bytes(), b'untouched')

    def test_packaged_schema_not_runtime_store(self):
        import paths
        with patch.object(paths, 'STORE', self.home / 'empty-store'):
            self.assertEqual(self.db.SCHEMA, ROOT / 'store' / 'schema.sql')
            conn = self.db.connect(self.target)
            conn.close()


if __name__ == '__main__':
    unittest.main()
