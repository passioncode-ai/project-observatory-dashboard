"""Merge freshness is derived from its inputs, never its own write time."""
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'collectors')]

class InputAge(unittest.TestCase):
    def test_old_local_input_degrades_and_preserves_original_timestamp(self):
        import merge_input_age as A
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'local.json'
            path.write_text(json.dumps({'scanned_at': '2026-01-01T00:00:00Z', 'folders': []}))
            inputs, notes = A.survey(Path(raw), now=datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc))
            self.assertEqual(inputs['local.json'], '2026-01-01T00:00:00Z')
            self.assertTrue(any(n['source'] == 'local.json' and '10.0 h old' in n['reason'] for n in notes))

    def test_invalid_explicit_timestamp_never_falls_back_to_fresh_mtime(self):
        import merge_input_age as A
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / 'local.json').write_text(json.dumps({'scanned_at': 'invalid'}))
            inputs, notes = A.survey(Path(raw))
            self.assertIsNone(inputs['local.json'])
            self.assertTrue(notes)

    def test_daily_bound_and_github_legacy_mtime_are_distinct(self):
        import merge_input_age as A
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); (root / 'gh').mkdir()
            (root / 'gh' / 'example.json').write_text('[]')
            (root / 'env.json').write_text(json.dumps({'measured_at': '2026-01-01T00:00:00Z'}))
            inputs, notes = A.survey(root, now=datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc))
            self.assertFalse(any(n['source'] == 'env.json' for n in notes))
            self.assertIn('gh/example.json', inputs)

    def test_real_sessions_date_survives_copy_with_fresh_mtime(self):
        import merge_input_age as A
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'sessions.json').write_text(json.dumps({'scanned_on': '2020-01-01', 'sessions': []}))
            inputs, notes = A.survey(root)
            self.assertEqual(inputs['sessions.json'], '2020-01-01T00:00:00Z')
            self.assertTrue(any(n['source'] == 'sessions.json' and 'old' in n['reason'] for n in notes))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixtures require mkfifo")
    def test_nonregular_input_is_not_opened(self):
        import merge_input_age as A
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); path = root / 'sessions.json'; os.mkfifo(path)
            original = Path.read_text
            def guarded(candidate, *args, **kwargs):
                if candidate == path:
                    raise AssertionError('nonregular input must not be opened')
                return original(candidate, *args, **kwargs)
            with patch.object(Path, 'read_text', guarded):
                inputs, notes = A.survey(root)
            self.assertIsNone(inputs['sessions.json'])
            self.assertTrue(any(n['source'] == 'sessions.json' for n in notes))

    def test_merge_writes_old_input_provenance_and_degradation(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve()
            env = {k: v for k, v in os.environ.items() if k not in {
                'OBSERVATORY_REGISTRY', 'OBSERVATORY_DB', 'OBSERVATORY_STATE', 'OBSERVATORY_SCRATCH'}}
            env.update(HOME=str(home), OBSERVATORY_HOME=str(home / 'workspace'), OBSERVATORY_SYSTEM_SETUP='0')
            initialized = subprocess.run([sys.executable, str(ROOT / 'observatory.py'), 'init'], env=env, capture_output=True, text=True)
            self.assertEqual(initialized.returncode, 0, initialized.stderr[-500:])
            scratch = home / 'workspace/store/raw'; scratch.mkdir(parents=True, exist_ok=True)
            stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=10)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
            (scratch / 'local.json').write_text(json.dumps({'scanned_at': stamp, 'folders': []}))
            (scratch / 'sessions.json').write_text(json.dumps({'scanned_on': '2020-01-01', 'sessions': []}))
            merged = subprocess.run([sys.executable, str(ROOT / 'collectors/merge.py'), str(scratch)], env=env, capture_output=True, text=True)
            self.assertEqual(merged.returncode, 0, merged.stderr[-500:])
            doc = json.loads((scratch / 'model.json').read_text())
            self.assertEqual(doc['inputs']['local.json'], stamp)
            self.assertEqual(doc['inputs']['sessions.json'], '2020-01-01T00:00:00Z')
            self.assertTrue(any(n['source'] == 'sessions.json' and 'old' in n['reason'] for n in doc['degraded']))
            self.assertTrue(any(n['source'] == 'local.json' and 'old' in n['reason'] for n in doc['degraded']))

if __name__ == '__main__':
    unittest.main()
