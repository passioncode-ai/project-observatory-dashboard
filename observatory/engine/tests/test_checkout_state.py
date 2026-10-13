"""Local Git observations preserve hidden state and never authorize disposal."""
import json
import os
import unittest
from unittest.mock import patch
from test_machine import Base, git, commit


class CheckoutState(Base):
    def repo(self):
        path = self.data / 'checkout'; path.mkdir()
        git(path, 'init', '-q'); commit(path, 'tracked')
        return path

    def observe(self, path):
        import checkout_inventory
        return checkout_inventory.classify(path, 5)['local_state']

    @unittest.skipIf(os.name == 'nt', 'bounded pipe observation currently POSIX only')
    def test_clean_then_changed_ignored_and_untracked(self):
        path = self.repo()
        first = self.observe(path)
        self.assertEqual(first['state'], 'clean-observed')
        (path / 'tracked').write_text('changed')
        (path / '.gitignore').write_text('ignored\n')
        (path / 'ignored').write_text('DO NOT DISCLOSE THIS')
        (path / 'new').write_text('new')
        row = self.observe(path)
        self.assertEqual(row['state'], 'changes-observed')
        self.assertEqual(row['tracked_records'], 1)
        self.assertEqual(row['untracked_records'], 2)
        self.assertEqual(row['ignored_records'], 1)
        self.assertNotIn('DO NOT DISCLOSE', json.dumps(row))
        self.assertFalse(row['actionable'])

    @unittest.skipIf(os.name == 'nt', 'bounded pipe observation currently POSIX only')
    def test_hidden_index_flag_is_unknown_not_clean(self):
        path = self.repo(); git(path, 'update-index', '--assume-unchanged', 'tracked')
        (path / 'tracked').write_text('hidden change')
        row = self.observe(path)
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['assume_unchanged_records'], 1)
        self.assertIn('hidden-index-flags', row['degraded'])
        git(path, 'update-index', '--no-assume-unchanged', 'tracked')
        git(path, 'update-index', '--skip-worktree', 'tracked')
        row = self.observe(path)
        self.assertEqual(row['skip_worktree_records'], 1)
        self.assertEqual(row['state'], 'unknown')

    @unittest.skipIf(os.name == 'nt', 'bounded pipe observation currently POSIX only')
    def test_redirected_worktree_before_or_during_probe_is_unknown(self):
        import checkout_state as subject
        for during in (False, True):
            with self.subTest(during=during):
                path = self.data / ('during' if during else 'before'); path.mkdir()
                git(path, 'init', '-q'); commit(path, 'tracked')
                other = self.data / ('other-during' if during else 'other-before'); other.mkdir()
                (other / 'tracked').write_bytes((path / 'tracked').read_bytes())
                (path / 'tracked').write_text('unsaved local work')
                metadata = {'classification': 'clone', 'git_directory': str(path / '.git')}
                original = subject.query
                def redirected(candidate, args, deadline):
                    output = original(candidate, args, deadline)
                    if args[0] == 'ls-files':
                        git(path, 'config', 'core.worktree', str(other))
                    return output
                if not during:
                    git(path, 'config', 'core.worktree', str(other))
                with patch.object(subject, 'query', side_effect=redirected if during else original):
                    row = subject.probe(path, metadata)
                self.assertEqual(row['state'], 'unknown')
                self.assertFalse(row['complete'])
                self.assertIn('identity-changed', row['degraded'])
                self.assertIsNone(row['tracked_records'])

    def test_index_and_worktree_not_changed_by_probe(self):
        path = self.repo(); index = path / '.git/index'
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        self.observe(path)
        self.assertEqual(before, (index.read_bytes(), index.stat().st_mtime_ns))

    def test_rename_and_non_utf8_names_are_counted_without_emission(self):
        import checkout_state as subject
        row = subject.parse_status(b'R  destination\0source\0?? private-\xff\0!! hidden\0')
        self.assertEqual(row, {'tracked_records': 1, 'untracked_records': 1, 'ignored_records': 1})
        self.assertNotIn('private', json.dumps(row))

    def test_malformed_or_incomplete_records_refuse(self):
        import checkout_state as subject
        for raw in (b'?? no-terminator', b'R  no-source\0', b'ZZ private\0', b'?? \0', b'\0'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                subject.parse_status(raw)
        with self.assertRaises(ValueError):
            subject.parse_index(b'? invalid-for-index\0')

    @unittest.skipIf(os.name == 'nt', 'bounded pipe observation currently POSIX only')
    def test_changed_binding_is_unknown(self):
        import checkout_state as subject
        path = self.repo()
        with patch.object(subject, 'binding', side_effect=[{'head': 'a'}, {'head': 'b'}]):
            row = subject.probe(path, {'classification': 'clone', 'git_directory': str(path / '.git')})
        self.assertEqual(row['state'], 'unknown')
        self.assertIn('identity-changed', row['degraded'])

    def test_incomplete_command_does_not_publish_zero_counts(self):
        import checkout_state as subject
        path = self.repo()
        for reason in ('command-timeout', 'output-limit', 'command-diagnostics'):
            with patch.object(subject, 'query', side_effect=ValueError(reason)):
                row = subject.probe(path, {'classification': 'clone', 'git_directory': str(path / '.git')})
            self.assertEqual(row['state'], 'unknown')
            self.assertIsNone(row['tracked_records'])
            self.assertFalse(row['complete'])

    def test_bare_and_unborn_do_not_claim_clean(self):
        import checkout_state as subject
        self.assertEqual(subject.probe(self.data, {'classification': 'bare-repository'})['state'], 'not-applicable')
        path = self.data / 'unborn'; path.mkdir(); git(path, 'init', '-q')
        self.assertEqual(self.observe(path)['state'], 'unknown')

    def test_record_limit_is_explicit(self):
        import checkout_state as subject
        with self.assertRaises(ValueError):
            subject.parse_status(b'?? x\0' * 3, max_records=2)

    def test_windows_is_explicit_unknown(self):
        import checkout_state as subject
        with patch.object(subject.os, 'name', 'nt'):
            row = subject.probe(self.data, {'classification': 'clone'})
        self.assertEqual(row['degraded'], ['bounded-pipe-observation-unavailable'])
        self.assertFalse(row['complete'])

    def test_saved_projection_refuses_stale_or_malformed_clean(self):
        import coverage_view
        from datetime import datetime, timezone, timedelta
        at = datetime.now(timezone.utc)
        value = {'state': 'clean-observed', 'complete': True, 'measured_at': at.isoformat(),
                 'degraded': [], 'private': 'DO NOT COPY', **dict.fromkeys(('tracked_records',
                 'untracked_records', 'ignored_records', 'index_records',
                 'assume_unchanged_records', 'skip_worktree_records'), 0)}
        row = coverage_view.local_state_summary(value, at, True)
        self.assertEqual(row['current_state'], 'clean-observed')
        self.assertNotIn('DO NOT COPY', json.dumps(row))
        self.assertEqual(coverage_view.local_state_summary(value, at, False)['current_state'], 'unknown')
        for change in ({'tracked_records': 1}, {'index_records': True}, {'complete': False},
                       {'measured_at': (at + timedelta(seconds=1)).isoformat()},
                       {'measured_at': (at - timedelta(hours=1)).isoformat()},
                       {'assume_unchanged_records': 1}):
            with self.subTest(change=change):
                self.assertEqual(coverage_view.local_state_summary({**value, **change}, at, True)['current_state'], 'unknown')


class OfflineGitObservation(Base):
    def partial(self, name='partial'):
        source = self.data / ('source-' + name); source.mkdir()
        git(source, 'init', '-q'); commit(source, 'tracked', 'synthetic content')
        git(source, 'config', 'uploadpack.allowFilter', 'true')
        git(source, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        target = self.data / name
        git(self.data, 'clone', '--quiet', '--filter=tree:0', '--no-checkout', source.as_uri(), str(target))
        return source, target, git(source, 'rev-parse', 'HEAD^{tree}').strip()

    def objects(self, target):
        import hashlib
        return {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (target / '.git/objects').rglob('*') if p.is_file()}

    def test_offline_missing_objects_refuse_without_object_writes(self):
        import safe_git
        for name in ('cat-file', 'rev-list', 'show'):
            source, target, tree = self.partial(name)
            before = self.objects(target)
            args = {'cat-file': ['cat-file', '-p', tree], 'rev-list': ['rev-list', '--objects', 'HEAD'],
                    'show': ['show', 'HEAD:tracked']}[name]
            result = safe_git.run(args, repo=target, offline=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.objects(target), before)
            # Explicit default-profile reads retain their historical fetch semantics.
            self.assertEqual(safe_git.run(args, repo=target).returncode, 0)
            self.assertNotEqual(self.objects(target), before)

    @unittest.skipIf(os.name == 'nt', 'bounded checkout pipes currently POSIX only')
    def test_checkout_query_refuses_lazy_fetch_and_preserves_objects(self):
        import checkout_state, time
        source, target, tree = self.partial()
        before = self.objects(target)
        with self.assertRaises(ValueError):
            checkout_state.query(target, ['cat-file', '-p', tree], time.monotonic() + 5)
        self.assertEqual(self.objects(target), before)

    @unittest.skipIf(os.name == 'nt', 'bounded checkout pipes currently POSIX only')
    def test_partial_checkout_observation_is_unknown_without_fetch(self):
        import checkout_inventory
        source, target, tree = self.partial()
        before = self.objects(target)
        row = checkout_inventory.classify(target, 5)
        self.assertEqual(row['classification'], 'clone')
        self.assertEqual(row['local_state']['state'], 'unknown')
        self.assertFalse(row['local_state']['complete'])
        self.assertFalse(row['local_state']['actionable'])
        self.assertEqual(self.objects(target), before)

    def test_offline_local_positive_and_explicit_remote_default(self):
        import safe_git
        source, target, tree = self.partial()
        self.assertEqual(safe_git.run(['rev-list', '--count', 'HEAD'], repo=target, offline=True).stdout.strip(), '1')
        self.assertNotEqual(safe_git.run(['ls-remote', source.as_uri()], repo=target, offline=True).returncode, 0)
        self.assertEqual(safe_git.run(['ls-remote', source.as_uri()], repo=target).returncode, 0)
        self.assertEqual(safe_git.run(['fetch', 'origin'], repo=target, write=True).returncode, 0)

    def test_offline_settings_cannot_be_reopened_by_extra_environment(self):
        import safe_git
        argv, env = safe_git.command(['rev-parse', 'HEAD'], offline=True,
                                    extra_env={'GIT_NO_LAZY_FETCH': '0', 'GIT_ALLOW_PROTOCOL': 'file'})
        self.assertEqual(env['GIT_NO_LAZY_FETCH'], '1')
        self.assertEqual(env['GIT_ALLOW_PROTOCOL'], '')
        with self.assertRaises(ValueError):
            safe_git.command(['commit'], offline=True, write=True)

    def test_transport_fallback_refuses_missing_object_even_without_lazy_fetch_variable(self):
        import safe_git, subprocess
        source, target, tree = self.partial()
        before = self.objects(target)
        argv, env = safe_git.command(['cat-file', '-p', tree], repo=target, offline=True)
        env.pop('GIT_NO_LAZY_FETCH')  # simulate an older Git ignoring this setting
        result = subprocess.run(argv, env=env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.objects(target), before)


if __name__ == '__main__':
    unittest.main()
