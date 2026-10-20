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


class UpstreamGraph(Base):
    def repo(self):
        path=self.data/'graph';path.mkdir();git(path,'init','-qb','main');commit(path,'initial')
        git(path,'branch','peer');git(path,'branch','--set-upstream-to=peer','main')
        return path

    def observe(self,path):
        import checkout_graph
        return checkout_graph.probe(path,seconds=5)

    def test_local_equal_ahead_behind_and_diverged(self):
        path=self.repo()
        for expected, action in [((0,0),None),((1,0),'ahead'),((0,1),'behind'),((1,1),'diverged')]:
            if action=='ahead':commit(path,'ahead')
            elif action=='behind':git(path,'branch','-f','peer','main');git(path,'reset','--hard','HEAD~1')
            elif action=='diverged':commit(path,'other')
            row=self.observe(path)
            self.assertTrue(row['complete'],row)
            self.assertEqual((row['ahead'],row['behind']),expected)
            self.assertEqual(row['upstream_kind'],'local-branch')
            self.assertEqual(row['remote_durability'],'unknown')
            self.assertFalse(row['actionable'])

    def test_stale_remote_tracking_cannot_claim_remote_durability(self):
        path=self.repo();origin=self.data/'remote';git(self.data,'clone','--bare',str(path),str(origin))
        git(path,'remote','add','origin',origin.as_uri());git(path,'fetch','origin')
        git(path,'branch','--set-upstream-to=origin/main','main')
        elsewhere=self.data/'elsewhere';git(self.data,'clone',str(origin),str(elsewhere));commit(elsewhere,'remote-only');git(elsewhere,'push','origin','main')
        tracked=git(path,'rev-parse','refs/remotes/origin/main')
        row=self.observe(path)
        self.assertTrue(row['complete'],row);self.assertEqual((row['ahead'],row['behind']),(0,0))
        self.assertEqual(row['upstream_kind'],'remote-tracking');self.assertEqual(row['remote_durability'],'unknown')
        self.assertEqual(git(path,'rev-parse','refs/remotes/origin/main'),tracked)

    def test_detached_unborn_and_missing_upstream_are_unknown(self):
        path=self.repo();git(path,'checkout','--detach','-q')
        self.assertEqual(self.observe(path)['state'],'unknown')
        git(path,'checkout','-q','main');git(path,'branch','-D','peer')
        self.assertIsNone(self.observe(path)['ahead'])
        unborn=self.data/'unborn';unborn.mkdir();git(unborn,'init','-q')
        self.assertFalse(self.observe(unborn)['complete'])

    def test_shallow_promisor_replacement_graft_and_alternates_refuse(self):
        path=self.repo();head=git(path,'rev-parse','HEAD').strip()
        for filename,content,reason in [('shallow',head+'\n','shallow-history'),('info/grafts',head+'\n','grafted-history'),('objects/info/alternates','/synthetic/missing\n','alternate-object-store')]:
            marker=path/'.git'/filename;marker.parent.mkdir(parents=True,exist_ok=True);marker.write_text(content)
            row=self.observe(path);self.assertIn(reason,row['degraded']);self.assertIsNone(row['ahead']);marker.unlink()
        git(path,'config','remote.origin.promisor','true')
        self.assertIn('promisor-history',self.observe(path)['degraded']);git(path,'config','--unset','remote.origin.promisor')
        git(path,'update-ref','refs/replace/'+head,head)
        self.assertIn('replacement-history',self.observe(path)['degraded'])

    def test_graph_drift_and_bad_counts_are_unknown(self):
        import checkout_graph as subject
        path=self.repo();original=subject.query
        def drift(candidate,args,deadline,**kwargs):
            value=original(candidate,args,deadline,**kwargs)
            if args[0]=='rev-list':git(path,'config','branch.main.merge','refs/heads/missing')
            return value
        with patch.object(subject,'query',side_effect=drift):
            row=self.observe(path)
        self.assertFalse(row['complete']);self.assertIsNone(row['ahead'])
        for value in (b'1 -1\n',b'1 2 3\n',b'1.5 0\n',b'99999999999999999999 0\n'):
            with self.assertRaises(ValueError):subject.parse_counts(value)
        self.assertEqual(subject.parse_counts(b'2\t3\n'),(2,3))

    def test_saved_graph_counts_reject_stale_future_bool_and_wrong_source(self):
        import checkout_graph,coverage_view
        from datetime import datetime,timezone,timedelta
        row=self.observe(self.repo());at=datetime.now(timezone.utc)
        good=coverage_view.upstream_summary(row,at)
        self.assertEqual(good['ahead'],0);self.assertFalse(good['actionable'])
        for changes in ({'ahead':True},{'measured_at':(at-timedelta(hours=1)).isoformat()},
                        {'measured_at':(at+timedelta(seconds=5)).isoformat()}, {'source_sha256':'0'*64}, {'complete':False}):
            bad=coverage_view.upstream_summary({**row,**changes},at)
            self.assertIsNone(bad['ahead']);self.assertEqual(bad['remote_durability'],'unknown')


class GraphPhase(Base):
    def test_failed_graph_keeps_published_local_state_and_advances_cursor(self):
        import checkout_inventory,checkout_graph
        from unittest.mock import patch
        root=self.data/'roots';root.mkdir()
        for name in ('a','b'):
            path=root/name;path.mkdir();git(path,'init','-q');commit(path,'tracked')
        out=self.home/'store/raw/checkout-candidates.json';seen=[]
        def failure(path,seconds):
            self.assertLessEqual(seconds,2)  # seconds=8 reserves at most a quarter for this phase
            published=json.loads(out.read_text())
            row=next(r for r in published['candidates'] if r['path']==str(path))
            self.assertEqual(row['local_state']['state'],'clean-observed')
            self.assertEqual(published['upstream_cursor']['last_attempted'],str(path))
            seen.append(str(path));raise TimeoutError('PRIVATE DETAIL')
        with patch.object(checkout_graph,'observe',side_effect=failure):
            result=checkout_inventory.scan(out,[root],seconds=8,root_seconds=6)
        self.assertEqual(len(seen),2)
        self.assertTrue(all(r['local_state']['state']=='clean-observed' for r in result['candidates']))
        self.assertTrue(all(r['upstream']['ahead'] is None for r in result['candidates']))
        self.assertNotIn('PRIVATE DETAIL',json.dumps(result))

    def test_actual_scan_publishes_bound_positive_graph_for_saved_reader(self):
        import checkout_inventory,coverage_view
        from datetime import datetime,timezone
        path=self.data/'positive';path.mkdir();git(path,'init','-qb','main');commit(path,'tracked')
        git(path,'branch','peer');git(path,'branch','--set-upstream-to=peer','main')
        out=self.home/'store/raw/checkout-candidates.json'
        result=checkout_inventory.scan(out,[path],seconds=8,root_seconds=4)
        row=result['candidates'][0]
        self.assertEqual(row['local_state']['state'],'clean-observed')
        self.assertTrue(row['upstream']['complete'],row['upstream'])
        summary=coverage_view.upstream_summary(row['upstream'],datetime.now(timezone.utc),row)
        self.assertEqual((summary['ahead'],summary['behind']),(0,0))
        for key in ('git_directory','common_directory','config_sha256','branch_sha256','upstream_ref_sha256'):
            incomplete={**row['upstream'],'binding':{k:v for k,v in row['upstream']['binding'].items() if k!=key}}
            with self.subTest(missing=key):
                self.assertIsNone(coverage_view.upstream_summary(incomplete,datetime.now(timezone.utc),row)['ahead'])
        for key in ('git_directory','common_directory','root','config_sha256'):
            invalid={**row['upstream'],'binding':{**row['upstream']['binding'],key:True}}
            self.assertIsNone(coverage_view.upstream_summary(invalid,datetime.now(timezone.utc),row)['ahead'])
        import checkout_graph
        with patch.object(checkout_graph,'source_hash',side_effect=OSError('PRIVATE SOURCE FAILURE')):
            failed=coverage_view.upstream_summary(row['upstream'],datetime.now(timezone.utc),row)
        self.assertIsNone(failed['ahead']);self.assertNotIn('PRIVATE SOURCE FAILURE',json.dumps(failed))
        changed={**row,'head':'0'*40}
        self.assertIsNone(coverage_view.upstream_summary(row['upstream'],datetime.now(timezone.utc),changed)['ahead'])

    def test_phase_budget_cannot_borrow_discovery_time_and_cursor_is_fair(self):
        import checkout_inventory,checkout_graph
        clock=[0.0];seen=[];durations=[];roots=[]
        for index in range(9):
            root=self.data/str(index);root.mkdir();(root/'.git').mkdir();roots.append(root)
        out=self.home/'store/raw/checkout-candidates.json'
        def classify(path,seconds):
            return {'classification':'clone','head':'a'*40,'local_state':{'state':'clean-observed'}}
        def observe(path,seconds):
            seen.append(path);durations.append(seconds);clock[0]+=seconds
            return checkout_graph.unknown('synthetic-failure')
        with patch.object(checkout_inventory.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(checkout_inventory,'classify',side_effect=classify), \
             patch.object(checkout_graph,'observe',side_effect=observe):
            first=checkout_inventory.scan(out,roots,seconds=20)
            self.assertEqual(sum(durations),5)
            last=first['upstream_cursor']['last_attempted'];count=len(seen)
            second=checkout_inventory.scan(out,roots,seconds=20)
            self.assertEqual(sum(durations),10)
        self.assertGreater(seen[count],last)
        self.assertTrue(all(r['local_state']['state']=='clean-observed' for r in second['candidates']))

    def test_graph_query_bounds_and_identity_drifts_stay_unknown(self):
        import checkout_graph
        from unittest.mock import patch
        path=self.data/'r';path.mkdir();git(path,'init','-qb','main');commit(path,'first');git(path,'branch','peer');git(path,'branch','--set-upstream-to=peer','main')
        original=checkout_graph.query
        def changed(candidate,args,deadline,**kwargs):
            value=original(candidate,args,deadline,**kwargs)
            if args[0]=='rev-list':commit(path,'second')
            return value
        with patch.object(checkout_graph,'query',side_effect=changed):
            row=checkout_graph.probe(path,seconds=5)
        self.assertIsNone(row['ahead']);self.assertFalse(row['complete'])
        self.assertEqual(checkout_graph.probe(path,seconds=0)['degraded'],['command-timeout'])
        for change in ({'truncated':True},{'stderr_seen':True},{'reason':'command-timeout'}):
            response={'truncated':False,'stderr_seen':False,'reason':None,'returncode':0,'stdout':b'0\t0\n',**change}
            with patch.object(checkout_graph.open_unlinked,'capture',return_value=response),self.assertRaises(ValueError):
                checkout_graph.query(path,['rev-list','HEAD'],__import__('time').monotonic()+1)


if __name__ == '__main__':
    unittest.main()
