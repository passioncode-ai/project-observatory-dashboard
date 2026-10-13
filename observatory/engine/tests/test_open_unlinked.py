"""Open-unlinked observations describe visible logical bytes, never cleanup authority."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
from datetime import datetime, timedelta, timezone
from test_machine import Base

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import open_unlinked as subject


def listing(pid=1, size=100, inode=42, fd=3):
    return f'p{pid}\nf{fd}\ntREG\ns{size}\nD0x10\ni{inode}\n'.encode()


class Observation(unittest.TestCase):
    def test_shared_inode_is_counted_once_and_refs_are_snapshot_only(self):
        doc = subject.parse(listing() + listing(2) + listing(fd=4))
        self.assertEqual(doc['logical_bytes'], 100)
        self.assertEqual(doc['unique_files'], 1)
        self.assertEqual(doc['handle_count'], 3)
        self.assertEqual(doc['records'][0]['pids'], [1, 2])
        self.assertIsNone(doc['reclaimable_bytes'])
        self.assertIsNone(doc['whole_machine_complete'])
        self.assertFalse(doc['actionable'])

    def test_conflicting_size_is_unknown_and_positive_peer_survives(self):
        doc = subject.parse(listing() + listing(2, 101) + listing(3, 9, 99))
        self.assertIsNone(doc['logical_bytes'])
        self.assertEqual(doc['known_logical_bytes'], 9)
        self.assertIn('conflicting-size', doc['degraded'])

    def test_bad_fields_and_encoding_degrade_without_echoing_input(self):
        for bad in (b'n/private/location\n', b'pno\n', b'i-9\n', b'\xff\n', b'p2\nf4\ntREG\ns0\ni9\n'):
            doc = subject.parse(listing() + bad)
            self.assertTrue(doc['degraded'])
            self.assertNotIn('/private/location', json.dumps(doc))
            self.assertIsNone(doc['logical_bytes'])

    def test_unknown_type_is_not_a_successful_empty_observation(self):
        bad = subject.parse(listing().replace(b'tREG', b'tMALFORMED'))
        self.assertFalse(bad['command_complete'])
        self.assertIsNone(bad['logical_bytes'])
        self.assertIn('unknown-file-type', bad['degraded'])
        self.assertEqual(subject.parse(listing().replace(b'tREG', b'tDIR'))['logical_bytes'], 0)

    def test_empty_is_measured_only_for_complete_success(self):
        self.assertEqual(subject.parse(b'')['logical_bytes'], 0)
        for extra in ({'returncode': 1}, {'stderr_seen': True}, {'truncated': True}):
            doc = subject.parse(b'', **extra)
            self.assertIsNone(doc['logical_bytes'])
            self.assertFalse(doc['command_complete'])

    def test_limits_and_unknown_size_are_not_zero(self):
        doc = subject.parse(listing() + listing(2, inode=99), max_records=1)
        self.assertEqual(doc['unique_files'], 1)
        self.assertIsNone(doc['logical_bytes'])
        self.assertIn('record-limit', doc['degraded'])
        self.assertIsNone(subject.parse(listing().replace(b's100\n', b''))['logical_bytes'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe selector')
    def test_runner_bounds_both_streams_and_owned_child_timeout(self):
        result = subject.capture([sys.executable, '-c', 'import sys;sys.stdout.write("x"*200000);sys.stderr.write("y"*200000)'], seconds=2, max_bytes=1024)
        self.assertLessEqual(len(result['stdout']), 1024)
        self.assertTrue(result['truncated'])
        result = subject.capture([sys.executable, '-c', 'import time;time.sleep(10)'], seconds=.05)
        self.assertEqual(result['reason'], 'command-timeout')

    def test_unsupported_has_no_process_or_false_zero(self):
        with patch.object(subject.sys, 'platform', 'win32'), patch.object(subject, 'capture', side_effect=AssertionError('spawn')):
            doc = subject.observe()
        self.assertIsNone(doc['logical_bytes'])
        self.assertIn('unsupported-platform', doc['degraded'])

    @unittest.skipUnless(sys.platform in ('darwin', 'linux') and shutil.which('lsof'), 'POSIX lsof unavailable')
    def test_real_owned_open_unlinked_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'disposable'
            with file.open('w+b') as held:
                held.write(b'x' * 8192); held.flush(); file.unlink()
                result = subject.capture([shutil.which('lsof'), '-nP', '-a', '-p', str(os.getpid()), '+L1', '-FpfstDi'], seconds=5)
                doc = subject.parse(result['stdout'], returncode=result['returncode'], stderr_seen=result['stderr_seen'], truncated=result['truncated'])
                self.assertTrue(any(row['logical_bytes'] == 8192 and os.getpid() in row['pids'] for row in doc['records']))


class SavedObservation(Base):
    def put(self):
        doc = subject.parse(listing())
        doc.update(attempted_at=subject.now(), completed_at=subject.now(), parser_sha256='a' * 64)
        target = self.home / 'store/raw/open-unlinked.json'
        target.write_text(json.dumps(doc))
        return target, doc

    def test_saved_reader_recomputes_allowlisted_totals(self):
        path, doc = self.put()
        doc['logical_bytes'] = 999999
        doc['records'][0]['command'] = 'DO_NOT_ECHO'
        path.write_text(json.dumps(doc))
        with patch.object(subject, 'observe', side_effect=AssertionError('rescan')):
            out = subject.saved_summary()
        self.assertEqual(out['logical_bytes'], 100)
        self.assertNotIn('DO_NOT_ECHO', json.dumps(out))
        self.assertEqual(out['unique_files'], 1)
        self.assertIsNone(out['reclaimable_bytes'])
        self.assertTrue(out['partial'])

    def test_saved_bad_future_stale_missing_and_symlink(self):
        self.assertIn('not-surveyed', subject.saved_summary()['degraded'])
        path, doc = self.put()
        doc['completed_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        path.write_text(json.dumps(doc))
        self.assertIn('invalid-receipt', subject.saved_summary()['degraded'])
        path, doc = self.put()
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        doc.update(attempted_at=old, completed_at=old)
        path.write_text(json.dumps(doc))
        self.assertTrue(subject.saved_summary()['stale'])
        doc['records'].append(doc['records'][0].copy())
        path.write_text(json.dumps(doc))
        self.assertIn('invalid-receipt', subject.saved_summary()['degraded'])
        path.unlink()
        try: path.symlink_to('missing')
        except OSError: return
        self.assertIn('unreadable-or-oversized', subject.saved_summary()['degraded'])

    def test_cli_collects_once_and_coverage_reads_same_saved_summary(self):
        import workspace, coverage_view
        doc = subject.parse(listing())
        doc.update(attempted_at=subject.now(), completed_at=subject.now(), parser_sha256='a' * 64)
        with patch.object(subject, 'observe', return_value=doc) as call, contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(workspace.machine_command('machine', ['--open-unlinked']), 0)
        call.assert_called_once()
        self.assertEqual(json.loads(buf.getvalue())['logical_bytes'], 100)
        with patch.object(subject, 'observe', side_effect=AssertionError('rescan')):
            self.assertEqual(coverage_view.summary()['open_unlinked']['logical_bytes'], 100)
        with patch.object(subject, 'collect', side_effect=AssertionError('invalid flags may not collect')), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): workspace.machine_command('machine', ['--open-unlinked', '--explain', '1'])


if __name__ == '__main__': unittest.main()
