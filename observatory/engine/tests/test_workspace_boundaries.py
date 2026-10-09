#!/usr/bin/env python3
"""Regression probes for workspace migration, version and filesystem boundaries."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration
import workspace


class WorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="observatory-workspace-boundary-")
        self.root = Path(self.temporary.name).resolve()
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.root / "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def original(self):
        source = self.root / "original"
        source.mkdir()
        (source / "paths.py").write_text("# synthetic legacy installation\n")
        for name in ("registry", "collectors", "store", "agent", "plugins/config"):
            (source / name).mkdir(parents=True, exist_ok=True)
        (source / "registry/projects.json").write_text('{"projects":[]}')
        return source

    def test_home_inside_the_engine_or_its_package_is_refused(self):
        source = Path(configuration.__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp).resolve() / "link"
            link.symlink_to(source)
            homes = [source, source / "state", link / "state"]
            if source.parent.name == "observatory" and (source.parent / "__init__.py").is_file():
                homes.append(source.parent / "state")  # installed or checked-out package
            for home in homes:
                with self.subTest(home=home), patch.dict(os.environ, {"OBSERVATORY_HOME": str(home)}):
                    with self.assertRaises(configuration.ConfigurationError) as caught:
                        configuration.home()
                    self.assertIn("outside the installed code", str(caught.exception))
            outside = Path(tmp).resolve() / "home"
            with patch.dict(os.environ, {"OBSERVATORY_HOME": str(outside)}):
                self.assertEqual(configuration.home(), outside)

    def test_initializer_rechecks_after_acquiring_lock(self):
        base = self.root / "home"
        real_lock = workspace.lock
        instance = []
        @contextlib.contextmanager
        def competing_initializer(path):
            # Simulate another completed init between the outer check and lock.
            with patch.object(workspace, "lock", real_lock):
                workspace.initialize(path)
                doc = configuration.load(path)
                doc["integrations"]["synthetic_provider"] = True
                workspace.write_json(path / "config/settings.json", doc)
                instance.append(configuration.validate_workspace(path)["instance_id"])
            with real_lock(path):
                yield
        with patch.object(workspace, "lock", competing_initializer):
            result = workspace.initialize(base)
        self.assertEqual(result["status"], "already-initialized")
        self.assertTrue(configuration.load(base)["integrations"]["synthetic_provider"])
        self.assertEqual(configuration.validate_workspace(base)["instance_id"], instance[0])

    def test_configure_refuses_symlinked_configuration_directory(self):
        base = self.root / "home"
        workspace.initialize(base)
        external = self.root / "external"
        external.mkdir()
        source = base / "config/settings.json"
        original = source.read_bytes()
        (external / "settings.json").write_bytes(original)
        shutil.rmtree(base / "config")
        (base / "config").symlink_to(external, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            result = workspace.main(["configure", "integrations", "github", "true"])
        self.assertEqual(result, 2)
        self.assertEqual((external / "settings.json").read_bytes(), original)

    def test_write_json_refuses_a_symlinked_parent(self):
        external = self.root / "external"
        external.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaises(configuration.ConfigurationError):
            workspace.write_json(linked / "settings.json", {"synthetic": True})
        self.assertEqual(list(external.iterdir()), [])

    def test_a_symlinked_home_is_refused_with_the_path_to_use_instead(self):
        # "use the resolved path" without printing it left a new user guessing,
        # typically under /tmp, which is itself a link on macOS.
        external = self.root / "external"
        external.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaises(configuration.ConfigurationError) as caught:
            workspace.initialize(linked / "home")
        self.assertIn(str((external / "home").resolve()), str(caught.exception))
        self.assertIn("OBSERVATORY_HOME", str(caught.exception))

    def test_migration_refuses_wal_change_after_database_backup(self):
        source = self.original()
        connection = sqlite3.connect(source / "store/observatory.db")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fixture (n INTEGER)")
        connection.execute("INSERT INTO fixture VALUES (1)")
        connection.commit()
        real_backup = workspace.backup_database
        def append_after_backup(src, dst):
            real_backup(src, dst)
            connection.execute("INSERT INTO fixture VALUES (2)")
            connection.commit()
        with patch.object(workspace, "backup_database", append_after_backup):
            with self.assertRaises(configuration.ConfigurationError):
                workspace.migrate_local(source, self.root / "new", True)
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(list(self.root.glob(".new.migration-*")), [])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM fixture").fetchone()[0], 2)

    def test_a_copy_keeps_each_files_modification_time(self):
        # 2026-10-08: a rolled-back update restored the workspace with every file dated at the
        # restore, and the tick's daily refreshes (`find -mmin` in tick.sh) slipped by half a day.
        src = self.root / "src" / "store" / "raw"  # paths-check: allow — a synthetic workspace tree the copy is driven over
        src.mkdir(parents=True)
        (src / "heroku.json").write_text("{}")
        old = 1_700_000_000
        os.utime(src / "heroku.json", (old, old))
        workspace.copy_private(self.root / "src", self.root / "copied")
        self.assertEqual(int((self.root / "copied/store/raw/heroku.json").stat().st_mtime), old)
        self.assertEqual((self.root / "copied/store/raw/heroku.json").stat().st_mode & 0o777, 0o600)

    def test_copy_refuses_dangling_symlinks(self):
        linked = self.root / "dangling"
        linked.symlink_to(self.root / "missing")
        with self.assertRaises(configuration.ConfigurationError):
            workspace.copy_private(linked, self.root / "copied")
        self.assertFalse((self.root / "copied").exists())

    def test_configuration_refuses_dangling_marker_and_settings(self):
        for relative in ("workspace.json", "config/settings.json"):
            with self.subTest(relative=relative):
                base = self.root / relative.replace("/", "-")
                workspace.initialize(base)
                target = base / relative
                target.unlink()
                target.symlink_to(self.root / "absent")
                with self.assertRaises(configuration.ConfigurationError):
                    configuration.load(base)

    def test_permission_tightening_does_not_follow_directory_symlink(self):
        import paths
        external = self.root / "external"
        (external / "logs").mkdir(parents=True)
        (external / "raw").mkdir()
        journal = external / "logs/event.jsonl"
        journal.write_text("{}\n")
        journal.chmod(0o644)
        for directory in (external, external / "logs", external / "raw"):
            directory.chmod(0o755)
        linked = self.root / "linked"
        linked.symlink_to(external, target_is_directory=True)
        with patch.multiple(paths, STORE=linked, STATE=linked, SCRATCH=linked / "raw", DB=linked / "db"):
            changes = paths.tighten()
        self.assertEqual(changes, [])
        self.assertEqual(journal.stat().st_mode & 0o777, 0o644)
        self.assertEqual((external / "logs").stat().st_mode & 0o777, 0o755)

    def test_future_workspace_refused_without_mutations(self):
        base = self.root / "home"
        workspace.initialize(base)
        marker = configuration.read_json(base / "workspace.json")
        marker["minimum_writer"] = "999.0.0"
        workspace.write_json(base / "workspace.json", marker)
        before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}
        with self.assertRaises(configuration.ConfigurationError):
            workspace.initialize(base)
        with contextlib.redirect_stderr(io.StringIO()):
            result = workspace.main(["configure", "integrations", "github", "true"])
        self.assertEqual(result, 2)
        after = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_migration_keeps_the_original_runtime_identities(self):
        import secrets as _secrets
        source = self.original()
        salt = _secrets.token_hex(32) + "\n"
        token = _secrets.token_urlsafe(32) + "\n"
        for name, value in ((".env-fingerprint-salt", salt), (".keyserver-token", token)):
            f = source / "store" / name
            f.write_text(value)
            f.chmod(0o600)
        destination = self.root / "migrated"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSERVATORY_STATE", None)
            workspace.migrate_local(source, destination, True)
        self.assertEqual((destination / "store/.env-fingerprint-salt").read_text(), salt,
                         "fingerprints stay comparable only if the salt moves unchanged")
        self.assertEqual((destination / "store/.keyserver-token").read_text(), token)

    def test_migration_without_identities_creates_them_once(self):
        source = self.original()
        destination = self.root / "migrated-fresh"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSERVATORY_STATE", None)
            workspace.migrate_local(source, destination, True)
        for name in (".env-fingerprint-salt", ".keyserver-token"):
            f = destination / "store" / name
            self.assertTrue(f.is_file(), name)
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_migration_failure_preserves_source_and_existing_destination(self):
        source = self.original()
        destination = self.root / "existing"
        destination.mkdir()
        retained = destination / "user-data"
        retained.write_text("keep")
        before = (source / "registry/projects.json").read_bytes()
        with self.assertRaises(configuration.ConfigurationError):
            workspace.migrate_local(source, destination, True)
        self.assertEqual(retained.read_text(), "keep")
        self.assertEqual((source / "registry/projects.json").read_bytes(), before)

    def test_migration_refuses_future_registry_before_writing(self):
        source = self.original()
        (source / 'registry/projects.json').write_text('{"schema_version":999,"projects":[]}')
        for apply in (False,True):
            with self.assertRaises(configuration.ConfigurationError):
                workspace.migrate_local(source,self.root/'new',apply)
        self.assertFalse((self.root/'new').exists())
        self.assertEqual(list(self.root.glob('.new.migration-*')),[])

    def test_migration_refuses_future_database_before_writing(self):
        source = self.original()
        with contextlib.closing(sqlite3.connect(source/'store/observatory.db')) as conn:
            conn.execute('CREATE TABLE fixture (n INTEGER)')
            conn.execute('PRAGMA user_version=999')
        before = (source/'store/observatory.db').read_bytes()
        with self.assertRaises(configuration.ConfigurationError):
            workspace.migrate_local(source,self.root/'new',True)
        self.assertEqual(before,(source/'store/observatory.db').read_bytes())
        self.assertFalse((self.root/'new').exists())

    def test_migration_detects_late_identity_change_even_same_mtime(self):
        source = self.original()
        identity = source/'identity.py'
        identity.write_text("ID_OVERRIDE = {'old': 'one'}\n")
        before = identity.stat()
        real_write = workspace.write_json
        def change_identity(path,value):
            real_write(path,value)
            if path.name=='migration-receipt.json':
                identity.write_text("ID_OVERRIDE = {'old': 'two'}\n")
                os.utime(identity,ns=(before.st_atime_ns,before.st_mtime_ns))
        with patch.object(workspace,'write_json',side_effect=change_identity):
            with self.assertRaises(configuration.ConfigurationError):
                workspace.migrate_local(source,self.root/'new',True)
        self.assertFalse((self.root/'new').exists())

    def test_migration_rechecks_staged_registry(self):
        source = self.original()
        real_copy = workspace.copy_private
        def bad_copy(src,dest):
            result=real_copy(src,dest)
            if src==source/'registry':
                (dest/'projects.json').write_text('{"schema_version":999,"projects":[]}')
            return result
        with patch.object(workspace,'copy_private',side_effect=bad_copy):
            with self.assertRaises(configuration.ConfigurationError):
                workspace.migrate_local(source,self.root/'new',True)
        self.assertFalse((self.root/'new').exists())

    def test_doctor_rejects_future_registry_with_nonzero_exit(self):
        base=self.root/'home'
        workspace.initialize(base)
        file=base/'registry/projects.json'
        file.write_text('{"schema_version":999,"projects":[]}')
        before=file.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
            result=workspace.main(['doctor'])
        self.assertEqual(result,2)
        self.assertEqual(before,file.read_bytes())

    def test_doctor_rejects_corrupt_database_without_replacing_it(self):
        base=self.root/'home'
        workspace.initialize(base)
        file=base/'store/observatory.db'
        file.write_bytes(b'synthetic bad database')
        with contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
            result=workspace.main(['doctor'])
        self.assertEqual(result,2)
        self.assertEqual(file.read_bytes(),b'synthetic bad database')

    def test_doctor_is_read_only_for_checkpointed_database(self):
        base=self.root/'home'
        workspace.initialize(base)
        with contextlib.closing(sqlite3.connect(base/'store/observatory.db')) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE fixture (n INTEGER)')
            conn.commit()
        before={str(p.relative_to(base)):p.read_bytes() for p in base.rglob('*') if p.is_file()}
        result=workspace.doctor(base)
        self.assertTrue(result['database']['integrity_checked'])
        after={str(p.relative_to(base)):p.read_bytes() for p in base.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_migration_parse_error_never_prints_source_line(self):
        source=self.original()
        marker='SYNTHETIC_PRIVATE_MARKER'
        (source/'identity.py').write_text('ID_OVERRIDE = '+marker+' bad syntax')
        stderr=io.StringIO()
        with patch.dict(os.environ,{'OBSERVATORY_HOME':str(self.root/'new')}),contextlib.redirect_stderr(stderr):
            result=workspace.main(['migrate-local',str(source),'--apply','--writers-stopped'])
        self.assertEqual(result,2)
        self.assertNotIn(marker,stderr.getvalue())
        self.assertFalse((self.root/'new').exists())


if __name__ == "__main__":
    unittest.main()
