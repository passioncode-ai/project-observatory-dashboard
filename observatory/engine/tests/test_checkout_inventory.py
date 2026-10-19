"""Explicit synthetic roots only; checkout observations never authorize cleanup."""
import json
import os
import sys
from unittest.mock import patch
from test_machine import Base, ROOT, git, commit
sys.path.insert(0, str(ROOT))


class Candidates(Base):
    def output(self):
        return self.home / 'store/raw/checkout-candidates.json'

    def test_clone_linked_and_bare_are_inventory_only(self):
        import checkout_inventory as inventory
        root = self.data / 'roots'; root.mkdir()
        repo = root / 'clone'; repo.mkdir(); git(repo, 'init', '-q'); commit(repo, 'file')
        linked = root / 'linked'; git(repo, 'worktree', 'add', '-qb', 'side', str(linked))
        bare = root / 'bare'; git(root, 'init', '--bare', '-q', str(bare))
        result = inventory.scan(self.output(), [root], seconds=10, entries=100, root_seconds=8)
        rows = result['candidates']
        self.assertEqual({r['classification'] for r in rows}, {'clone', 'linked-worktree', 'bare-repository'})
        self.assertTrue(all(r['reclaimable_bytes'] is None and r['allocated_bytes'] is None for r in rows))
        self.assertTrue(all(r['actionable'] is False and r['active_use'] == 'unknown' for r in rows))
        self.assertNotIn('abandoned', json.dumps(rows))

    def test_cursor_continues_and_small_second_root_is_not_starved(self):
        import checkout_inventory as inventory
        large = self.data / 'large'; large.mkdir()
        for i in range(12): (large / str(i)).mkdir()
        small = self.data / 'small'; small.mkdir(); (small / '.git').mkdir()
        with patch.object(inventory, 'classify', return_value={'classification': 'unknown', 'reason': 'synthetic'}):
            first = inventory.scan(self.output(), [large, small], seconds=3, entries=2)
            self.assertTrue(any(r['path'] == str(small) for r in first['candidates']))
            self.assertTrue(first['partial'])
            complete = False
            for _ in range(30):
                result = inventory.scan(self.output(), [large, small], seconds=3, entries=2)
                complete |= any(r['complete'] for r in result['roots'] if r['path'] == str(large))
        self.assertTrue(complete)

    def test_symlink_escape_and_missing_root_are_explicit(self):
        import checkout_inventory as inventory
        root = self.data / 'root'; root.mkdir()
        outside = self.base / 'outside'; outside.mkdir(); (outside / '.git').mkdir()
        (root / 'escape').symlink_to(outside, target_is_directory=True)
        result = inventory.scan(self.output(), [root, self.data / 'missing'], seconds=2)
        self.assertFalse(result['candidates'])
        self.assertTrue(result['partial'])
        reasons = [r['reason'] for r in result['degraded']]
        self.assertIn('symlink-skipped', reasons)
        self.assertIn('root-unavailable', reasons)

    def test_unobserved_candidate_keeps_its_original_time_and_unknowns(self):
        import checkout_inventory as inventory
        root = self.data / 'root'; root.mkdir(); (root / '.git').mkdir()
        with patch.object(inventory, 'classify', return_value={'classification': 'unknown', 'reason': 'git-failed'}):
            first = inventory.scan(self.output(), [root], seconds=2)
        second = inventory.scan(self.output(), [root], seconds=0)
        self.assertEqual(first['candidates'][0]['measured_at'], second['candidates'][0]['measured_at'])
        self.assertTrue(second['candidates'][0]['stale'])
        self.assertTrue(second['partial'])

    def test_changed_cursor_identity_resets_and_permission_failure_is_not_empty(self):
        import checkout_inventory as inventory
        root = self.data / 'root'; root.mkdir()
        for i in range(5): (root / str(i)).mkdir()
        first = inventory.scan(self.output(), [root], entries=1, seconds=2)
        (root / 'new').mkdir()
        second = inventory.scan(self.output(), [root], entries=1, seconds=2)
        self.assertIn('cursor-identity-changed', [r['reason'] for r in second['degraded']])
        with patch.object(inventory.os, 'scandir', side_effect=PermissionError('PRIVATE ERROR BODY')):
            failed = inventory.scan(self.output(), [root], seconds=2)
        self.assertTrue(failed['partial'])
        self.assertIn('directory-unreadable', [r['reason'] for r in failed['degraded']])
        self.assertNotIn('PRIVATE ERROR BODY', json.dumps(failed))

    def test_cli_requires_explicit_roots_and_preserves_registration_uncertainty(self):
        import checkout_inventory as inventory, workspace
        import contextlib, io
        root = self.data / 'root'; root.mkdir(); (root / '.git').mkdir()
        stamp = inventory.now()
        receipt = {'schema_version': 1, 'kind': 'worktree-registration', 'measured_at': stamp,
                   'partial': True, 'repositories': [{'stale': False, 'registered_at': stamp,
                                                     'worktrees': [{'path': str(root)}]}]}
        (self.output().parent / 'worktree-registration.json').write_text(json.dumps(receipt))
        with patch.object(inventory, 'classify', return_value={'classification': 'unknown', 'reason': 'git-failed'}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workspace.machine_command('machine', ['--checkouts', '--root', str(root)]), 0)
        row = json.loads(output.getvalue())['candidates'][0]
        self.assertEqual(row['registration_status'], 'listed-current')
        self.assertFalse(row['actionable'])
        receipt['repositories'] = [None]
        (self.output().parent / 'worktree-registration.json').write_text(json.dumps(receipt))
        self.assertEqual(inventory.registration_evidence(self.output().parent / 'worktree-registration.json')[:2], ({}, False))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            workspace.machine_command('machine', ['--checkouts'])

    def test_tampered_cursor_cannot_escape_explicit_root(self):
        import checkout_inventory as inventory
        root = self.data / 'root'; root.mkdir()
        outside = self.data / 'outside'; outside.mkdir(); (outside / '.git').mkdir()
        old = {'schema_version': 1, 'kind': 'checkout-candidates', 'candidates': [],
               'roots': [{'path': str(root), 'complete': False, 'stack': [{'path': str(root / '..' / 'outside'), 'offset': -1}]}]}
        self.output().write_text(json.dumps(old))
        result = inventory.scan(self.output(), [root], seconds=1)
        self.assertFalse(result['candidates'])
        self.assertTrue(result['partial'])

    def test_competing_writer_refuses_and_probe_timeout_stops_only_owned_child(self):
        import checkout_inventory as inventory
        from unittest.mock import MagicMock
        inventory.scan(self.output(), [self.data], seconds=0)
        before = self.output().read_bytes()
        fd = os.open(self.output().with_name(self.output().name + '.lock'), os.O_RDWR)
        try:
            inventory.oslocks.flock(fd, inventory.oslocks.LOCK_EX | inventory.oslocks.LOCK_NB)
            with self.assertRaises(BlockingIOError): inventory.scan(self.output(), [self.data], seconds=0)
            self.assertEqual(before, self.output().read_bytes())
        finally:
            inventory.oslocks.flock(fd, inventory.oslocks.LOCK_UN); os.close(fd)
        child = MagicMock(pid=54321)
        child.communicate.side_effect = [inventory.subprocess.TimeoutExpired('probe', .01), ('', '')]
        with patch.object(inventory.subprocess, 'Popen', return_value=child), patch.object(inventory.osproc, 'stop_group') as stop:
            result = inventory.classify(self.data, .01)
        self.assertEqual(result['reason'], 'git-probe-timeout')
        stop.assert_called_once_with(54321, grace=0)

    def test_symlink_git_marker_never_reaches_git_probe(self):
        import checkout_inventory as inventory
        root = self.data / 'root'; root.mkdir()
        outside = self.base / 'git-metadata'; outside.mkdir()
        (root / '.git').symlink_to(outside, target_is_directory=True)
        with patch.object(inventory, 'classify') as probe:
            result = inventory.scan(self.output(), [root], seconds=1)
        probe.assert_not_called()
        self.assertEqual(result['candidates'][0]['classification'], 'unknown')
        self.assertEqual(result['candidates'][0]['reason'], 'unsupported-git-marker')

    def test_negative_registration_membership_requires_current_top_receipt(self):
        import checkout_inventory as inventory
        root = self.data / 'root'; root.mkdir(); (root / '.git').mkdir()
        receipt = self.output().parent / 'worktree-registration.json'
        for stamp in ('2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z', None):
            receipt.write_text(json.dumps({'schema_version': 1, 'kind': 'worktree-registration',
                                          'partial': False, 'measured_at': stamp, 'repositories': []}))
            with patch.object(inventory, 'classify', return_value={'classification': 'clone', 'reason': None}):
                result = inventory.scan(self.output(), [root], seconds=1)
            row = result['candidates'][0]
            self.assertEqual(row['registration_status'], 'unknown')
            self.assertEqual(row['registration']['reason'], 'coverage-not-current')
            self.assertTrue(result['partial'])
            self.assertIn('registration-coverage-not-current', [r['reason'] for r in result['degraded']])
        receipt.write_text(json.dumps({'schema_version': 1, 'kind': 'worktree-registration',
                                      'partial': False, 'measured_at': inventory.now(), 'repositories': []}))
        with patch.object(inventory, 'classify', return_value={'classification': 'clone', 'reason': None}):
            result = inventory.scan(self.output(), [root], seconds=1)
        self.assertEqual(result['candidates'][0]['registration_status'], 'not-listed-in-current-pass')

    def test_git_metadata_or_inherited_parent_is_not_a_checkout_root(self):
        import checkout_inventory as inventory
        repo = self.data / 'repo'; repo.mkdir(); git(repo, 'init', '-q'); commit(repo, 'file')
        fake = repo / 'fake'; fake.mkdir()
        (fake / 'HEAD').write_text('not a repository')
        (fake / 'objects').mkdir(); (fake / 'refs').mkdir()
        for root in (repo / '.git', fake):
            result = inventory.scan(self.output(), [root], seconds=5, root_seconds=4)
            self.assertEqual(result['candidates'][0]['classification'], 'unknown')
            self.assertEqual(result['candidates'][0]['reason'], 'git-root-mismatch')
            self.assertFalse(result['roots'][0]['complete'])

    def test_empty_roots_refused_and_prior_malformed_receipt_degrades(self):
        import checkout_inventory as inventory
        with self.assertRaises(ValueError): inventory.scan(self.output(), [], seconds=1)
        self.output().write_text('{bad')
        result = inventory.scan(self.output(), [self.data], seconds=1)
        self.assertIn('prior-receipt-unreadable', [r['reason'] for r in result['degraded']])


if __name__ == '__main__':
    import unittest
    unittest.main()
