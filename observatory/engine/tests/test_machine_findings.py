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

class BackupLeftBehind(unittest.TestCase):
    """2026-10-20: 19 items the machine bundle did not carry, ten of them the only copy of
    commits, were a count in a receipt. Each is now a finding with what is lost and its fix."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / 'machine-backup.json'

    def write(self, success):
        self.state.write_text(json.dumps({'last': success, 'success': success}))

    def test_each_item_left_behind_is_a_finding_with_its_fix(self):
        self.write({'result': 'taken', 'file': 'machine-x.obmachine', 'not_carried_count': 4, 'not_carried': [
            {'source': '~/src/alpha-web', 'reason': '32 commit(s) no remote has: over the cap of 16 MB (limits.bundle_mb)',
             'size': 130 * 1024 * 1024, 'fix': 'push the branch (`git -C ~/src/alpha-web push`), or raise limits.bundle_mb'},
            {'source': '~/src/beta-api', 'reason': 'uncommitted changes: over the cap of 8 MB (limits.patch_mb)',
             'size': 9 * 1024 * 1024, 'fix': 'commit and push'},
            {'source': '~/.agents/skills/bulky', 'reason': 'over the per-entry cap of 128 MB (limits.entry_mb)',
             'size': 5, 'files': 2, 'fix': 'raise limits.entry_mb'}]})
        found = {f['subject']: f for f in F.backup_findings(self.state)}
        alpha = found['machine-backup:~/src/alpha-web:commits']
        self.assertEqual((alpha['type'], alpha['severity']), ('machine.backup_not_carried', 'warning'))
        self.assertIn('32 commit(s)', alpha['detail'])
        self.assertIn('only copy', alpha['detail'])
        self.assertIn('git -C ~/src/alpha-web push', alpha['action'])
        self.assertEqual(found['machine-backup:~/src/beta-api:changes']['severity'], 'warning')
        self.assertEqual(found['machine-backup:~/.agents/skills/bulky:files']['severity'], 'info')
        # One more than the receipt lists: named as a count, with where to read them all.
        rest = found['machine-backup:more']
        self.assertIn('1', rest['title'])
        self.assertIn('machine-backup status', rest['action'])

    def test_two_parts_of_one_checkout_are_two_findings_with_two_ids(self):
        # Review of #325: a patch over patch_mb and untracked files over untracked_mb of ONE
        # checkout shared one subject, and the board's duplicate-id guard then published nothing.
        # The operator's real receipt held such a pair. Two caps of one home folder too.
        self.write({'result': 'taken', 'not_carried_count': 5, 'not_carried': [
            {'source': '~/src/alpha-web', 'reason': '3 commit(s) no remote has: over the cap of 16 MB (limits.bundle_mb)', 'size': 9},
            {'source': '~/src/alpha-web', 'reason': 'uncommitted changes: over the cap of 8 MB (limits.patch_mb)', 'size': 9},
            {'source': '~/src/alpha-web', 'reason': '1577 untracked file(s): over the cap of 8 MB (limits.untracked_mb)', 'size': 9},
            {'source': '~/.agents/skills/bulky', 'reason': 'over the per-file cap of 16 MB (limits.file_mb)', 'size': 9, 'fix': 'a'},
            {'source': '~/.agents/skills/bulky', 'reason': 'over the per-entry cap of 128 MB (limits.entry_mb)', 'size': 9, 'fix': 'b'}]})
        found = F.backup_findings(self.state)
        ids = [f"{f['type']}:{f['subject']}" for f in found]
        self.assertEqual(len(ids), len(set(ids)), ids)
        alpha = {f['subject'] for f in found if '~/src/alpha-web' in f['subject']}
        self.assertEqual(len(alpha), 3, 'commits, uncommitted changes and untracked files are each a row')
        bulky = [f for f in found if '~/.agents/skills/bulky' in f['subject']]
        self.assertEqual(len(bulky), 1, 'two caps of one folder are one row naming both')
        self.assertIn('limits.file_mb', bulky[0]['detail'])
        self.assertIn('limits.entry_mb', bulky[0]['detail'])
        self.assertIn('a', bulky[0]['action']); self.assertIn('b', bulky[0]['action'])

    def test_a_record_without_a_fix_still_says_where_to_look(self):
        self.write({'result': 'taken', 'not_carried_count': 1,
                    'not_carried': [{'source': '~/x', 'reason': 'unreadable: PermissionError'}]})
        (finding,) = F.backup_findings(self.state)
        self.assertIn('machine-backup status', finding['action'])

    def test_nothing_left_behind_or_no_backup_is_silent(self):
        self.assertEqual(F.backup_findings(self.state), [])
        self.write({'result': 'taken', 'not_carried_count': 0, 'not_carried': []})
        self.assertEqual(F.backup_findings(self.state), [])
        self.state.write_text('not json')
        self.assertEqual(F.backup_findings(self.state), [])

    def test_the_board_reads_it(self):
        source = (ROOT / 'tools' / 'build_findings.py').read_text(encoding='utf-8')
        self.assertIn('machine_findings.backup_findings(', source)
        sys.path.insert(0, str(ROOT / 'dashboard'))
        import finding_types
        self.assertIn('machine.backup_not_carried', finding_types.LABELS)


class VirtualMachineAtRisk(unittest.TestCase):
    """OBS-529: a VM writing its disk while the host disk is low is named before it breaks."""

    def test_a_writing_vm_on_a_low_disk_is_a_finding(self):
        guard = {"warnings": [{"code": "vm-disk-at-risk", "level": "critical", "vms": ["/x/.colima/_lima/_disks/colima/datadisk"],
                               "allocated_gb": 25.0}]}
        found = F.vm_findings(guard)
        self.assertEqual([(f["type"], f["severity"]) for f in found], [("machine.vm_disk_at_risk", "critical")])
        self.assertIn("datadisk", found[0]["detail"])
        self.assertEqual(F.vm_findings({"warnings": [dict(guard["warnings"][0], level="low")]})[0]["severity"], "warning")

    def test_nothing_without_a_warning_or_with_a_malformed_one(self):
        self.assertEqual(F.vm_findings(None), [])
        self.assertEqual(F.vm_findings({"warnings": [{"code": "vm-disk-at-risk", "vms": "not a list"}, "x"]}), [])


class VirtualMachineDiskErrors(unittest.TestCase):
    """The VM's kernel log shows a journal abort: a critical finding with the repair steps as text, never run."""

    WARN = {"code": "vm-disk-errors", "level": "critical",
            "instances": [{"home": "/x/.colima/_lima", "instance": "colima",
                           "lines": ["[ 812.6] JBD2: Aborting journal on device vdb-8."]}]}

    def test_a_vm_with_disk_errors_is_a_critical_finding_with_the_repair_steps(self):
        found = F.vm_findings({"warnings": [self.WARN]})
        self.assertEqual([(f["type"], f["severity"]) for f in found], [("vm.disk_errors", "critical")])
        self.assertIn("colima", found[0]["detail"])
        self.assertIn("Aborting journal", found[0]["detail"])
        for step in ("stop Docker", "e2fsck -fy", "colima restart"):
            self.assertIn(step, found[0]["action"])
        self.assertTrue(found[0]["title"])

    def test_the_type_is_named_and_a_malformed_warning_is_ignored(self):
        sys.path.insert(0, str(ROOT / 'dashboard'))
        import finding_types
        self.assertIn("vm.disk_errors", finding_types.LABELS)
        self.assertEqual(F.vm_findings({"warnings": [{"code": "vm-disk-errors", "instances": "no"}]}), [])
        self.assertEqual(F.vm_findings({"warnings": [{"code": "vm-disk-errors", "instances": [1]}]}), [])


class TemporaryFolderGrowth(unittest.TestCase):
    """OBS-585: a name class of the user's temporary folder arriving faster than its threshold is a row naming the
    class, its rate and the executable writing there, so the owner of a leaking test gets it."""

    GUARD = {"user_temp": {"at": "2026-11-16T10:00:00Z", "window_hours": 2.0,
                           "growing": [{"prefix": "tmp*", "entries_per_day": 12384, "gb_per_day": 1.1,
                                        "entries": 98000, "gb": 7.9, "writers": ["python3.12"]},
                                       {"prefix": "pytest-*", "entries_per_day": 0, "gb_per_day": 2.5,
                                        "entries": 40, "gb": 3.0, "writers": []}]}}

    def test_each_growing_class_is_a_finding_with_its_rate_and_writer(self):
        found = F.tmp_findings(self.GUARD)
        self.assertEqual([(f["type"], f["subject"]) for f in found],
                         [("disk.tmp_growth", "machine:tmp:tmp*"), ("disk.tmp_growth", "machine:tmp:pytest-*")])
        self.assertIn("12384", found[0]["title"].replace(",", ""))
        self.assertIn("python3.12", found[0]["detail"])
        self.assertIn("no program", found[1]["detail"])
        for f in found:
            self.assertEqual(f["severity"], "warning")
            self.assertNotIn("--", f["detail"] + f["action"])
        sys.path.insert(0, str(ROOT / 'dashboard'))
        import finding_types
        self.assertIn("disk.tmp_growth", finding_types.LABELS)

    def test_nothing_without_growth_or_with_a_malformed_section(self):
        self.assertEqual(F.tmp_findings(None), [])
        self.assertEqual(F.tmp_findings({"user_temp": {"growing": "x"}}), [])
        self.assertEqual(F.tmp_findings({"user_temp": {"growing": [{"entries_per_day": 5}, 3]}}), [])

    def test_the_board_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'logs').mkdir()
            (root / 'disk-guard.json').write_text(json.dumps({**self.GUARD, "at": "2026-01-01T12:00:00Z"}))
            found = F.findings(root, root / 'logs', {}, now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
                               auto_cleanup=False)
        self.assertIn("disk.tmp_growth", [f["type"] for f in found])


if __name__ == '__main__':
    unittest.main()
