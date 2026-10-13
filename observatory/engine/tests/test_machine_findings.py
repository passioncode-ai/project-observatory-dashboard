"""Read-only synthetic guard signals and cause-specific recovery."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import machine_findings as F

class GuardSignals(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(); self.logs = self.root / 'logs'; self.logs.mkdir()
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def findings(self):
        return F.findings(self.root, self.logs, {}, now=self.now, auto_cleanup=False)

    def test_three_consecutive_stopped_summaries_warn_with_count_and_level(self):
        rows = [{'kind': 'summary', 'at': '2026-01-01T11:00:00Z', 'level': 'critical', 'git': {'stopped': 5}}] * 3
        (self.logs / 'disk-guard.jsonl').write_text('\n'.join(map(json.dumps, rows)))
        finding = next(f for f in self.findings() if f['type'] == 'machine.guard_stopping')
        self.assertEqual(finding['severity'], 'warning')
        self.assertIn('5', finding['detail']); self.assertIn('critical', finding['detail'])
        rows.append({'kind': 'summary', 'at': '2026-01-01T11:30:00Z', 'git': {'stopped': 0}})
        (self.logs / 'disk-guard.jsonl').write_text('\n'.join(map(json.dumps, rows)))
        self.assertNotIn('machine.guard_stopping', {f['type'] for f in self.findings()})

    def test_fda_remedy_only_for_its_cause(self):
        (self.root / 'disk-guard.json').write_text(json.dumps({'degraded': [
            {'source': 'trash', 'reason': 'the Trash is not readable by this job (macOS asks for Full Disk Access)'},
            {'source': 'build-output', 'reason': 'root quantum spent; cursor advances next pass'}]}))
        found = {f['subject']: f for f in self.findings() if f['type'] == 'machine.guard_degraded'}
        self.assertIn('System Settings', found['guard:trash']['action'])
        self.assertIn('interpreter', found['guard:trash']['action'])
        self.assertNotIn('Full Disk Access', found['guard:build-output']['action'])
        self.assertEqual(found['guard:trash']['severity'], 'info')

    def test_interrupted_git_subprocess_counts_but_invalid_summary_breaks_streak(self):
        row = {'kind': 'summary', 'at': '2026-01-01T11:00:00Z', 'git': {'result': 'stopped'}}
        journal = self.logs / 'disk-guard.jsonl'; journal.write_text('\n'.join([json.dumps(row)] * 3))
        self.assertIn('machine.guard_stopping', {f['type'] for f in self.findings()})
        journal.write_text(json.dumps(row) + '\ninvalid\n' + json.dumps(row))
        self.assertNotIn('machine.guard_stopping', {f['type'] for f in self.findings()})
        self.assertIn('machine.guard_degraded', {f['type'] for f in self.findings()})

    def test_invalid_guard_receipts_degrade_without_hiding_peer_findings(self):
        (self.root / 'machine.json').write_text(json.dumps({'disk': {'volume': {'free_percent': 1, 'free_gb': 1}}}))
        for doc in ([], 7, {'degraded': 7}, {'degraded': [7]}, {'degraded': [{'source': 'trash', 'reason': []}]}):
            with self.subTest(doc=doc):
                (self.root / 'disk-guard.json').write_text(json.dumps(doc))
                found = self.findings()
                self.assertIn('machine.guard_degraded', {f['type'] for f in found})
                self.assertIn('machine.disk_low', {f['type'] for f in found})

if __name__ == '__main__':
    unittest.main()
