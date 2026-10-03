"""Shared assistant: deterministic safety and persistence, no provider credentials."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'OBSERVATORY_HOME': str(self.home/'ws')}, clear=True)
        self.env.start()
        import workspace
        workspace.initialize(self.home/'ws')
        import paths
        importlib.reload(paths)
        from agent import assistant
        self.a = importlib.reload(assistant)
        self.a.paths.REGISTRY.joinpath('projects.json').write_text(json.dumps({'projects':[
            {'id':'project:demo','name':'Demo','lifecycle':'active','credential':'DO-NOT-READ'}]}))
    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()
    def request(self, **kw):
        return {'question':'What changed?', 'request_id':'request-demo-001', **kw}
    def test_input_rejects_unknown_fields_paths_and_blank(self):
        for kw in ({'question':' '}, {'conversation_id':'../../secret'}, {'shell':'rm'}, {'question':'x'*6001}):
            with self.assertRaises(self.a.AssistantError): self.a.validate(self.request(**kw))
    def test_context_is_bounded_and_whitelisted(self):
        (self.a.paths.REGISTRY/'findings.json').unlink()
        context = self.a.evidence('project:demo')
        self.assertNotIn('DO-NOT-READ', json.dumps(context))
        self.assertLess(len(json.dumps(context)), 30000)
        self.assertTrue(context['items'])
        self.assertTrue(context['degraded'])
    def test_unknown_project_is_not_substituted_with_whole_estate(self):
        with self.assertRaises(self.a.AssistantError): self.a.evidence('project:absent')
    def test_unknown_citation_is_rejected(self):
        with self.assertRaises(self.a.AssistantError):
            self.a.check_answer({'answer':'x','evidence_ids':['made-up'],'next_steps':[]}, [{'id':'E1'}])
    def test_valid_answer_and_conversation_survive_reload(self):
        c=self.a.new_conversation('Example'); self.a.save_conversation(c)
        self.assertEqual(self.a.get_conversation(c['id'])['title'],'Example')
        self.assertEqual(self.a.list_conversations()[0]['id'],c['id'])
        self.assertEqual(self.a.get_conversation(c['id'])['turns'],[])
        self.assertEqual((self.a.conversations_dir()/f"{c['id']}.json").stat().st_mode & 0o777,0o600)
    def test_corrupt_history_and_symlink_are_refused(self):
        c=self.a.new_conversation('Example');self.a.save_conversation(c)
        p=self.a.conversations_dir()/f"{c['id']}.json";p.write_text('{')
        with self.assertRaises(self.a.AssistantError):self.a.get_conversation(c['id'])
        p.unlink();p.symlink_to(self.home/'outside')
        with self.assertRaises(self.a.AssistantError):self.a.get_conversation(c['id'])
    def test_disabled_agent_refuses_before_job_or_provider(self):
        with patch.object(self.a.configuration,'enabled',return_value=False),patch.object(self.a.jobs,'start') as start:
            with self.assertRaises(self.a.AssistantError):self.a.ask(self.request())
            start.assert_not_called()
    def test_model_answer_cannot_claim_unreferenced_evidence(self):
        answer=self.a.check_answer({'answer':'Measured','evidence_ids':['E1'],'next_steps':['Review']},[{'id':'E1','title':'Snapshot'}])
        self.assertEqual(answer['evidence'][0]['title'],'Snapshot')
    def test_status_is_local_and_never_spends(self):
        with patch.object(self.a.providers,'complete') as complete:
            doc=self.a.status();self.assertEqual(doc['protocol'],'observatory-assistant/1');complete.assert_not_called()

    def test_dashboard_names_the_server_and_the_built_files(self):
        from tools import dashboard_open
        (self.a.paths.SCRATCH/'serverd.json').write_text(json.dumps({'port':48123}))
        index=self.a.paths.DASHBOARD_DIR/'index.html'
        with patch.object(dashboard_open,'served_workspace',return_value='/another-workspace'):
            doc=self.a.dashboard()
            self.assertEqual((doc['server'],doc.get('url'),doc['files']),('other-workspace',None,None))
        index.parent.mkdir(parents=True,exist_ok=True);index.write_text('<html></html>')
        with patch.object(dashboard_open,'served_workspace',return_value=None):
            doc=self.a.dashboard()
            # No server: the built pages are still a dashboard, opened as files.
            self.assertEqual((doc['server'],doc.get('url')),('absent',None))
            self.assertEqual(doc['files'],str(index.resolve()))
            self.assertTrue(doc['built_at'])
        with patch.object(dashboard_open,'served_workspace',return_value=str(self.a.paths.HOME)) as get:
            doc=self.a.dashboard()
            self.assertEqual((doc['server'],doc['url']),('verified','http://127.0.0.1:48123/dashboard/index.html'))
            get.assert_called_once_with(48123,timeout=3)

    def test_serve_starts_only_when_asked_and_reuses_a_verified_server(self):
        from tools import dashboard_open
        home=str(self.a.paths.HOME);answers=iter([None,home])
        with patch.object(dashboard_open,'served_workspace',side_effect=lambda *a,**k:next(answers)), \
             patch.object(dashboard_open,'_always_on',return_value=False), \
             patch.object(dashboard_open,'start_server') as start:
            doc=self.a.serve()
            start.assert_called_once_with(47311)
            self.assertEqual(doc['server'],'verified')
        with patch.object(dashboard_open,'served_workspace',return_value=home), \
             patch.object(dashboard_open,'start_server') as start:
            self.a.serve();start.assert_not_called()

    def test_serve_restarts_an_installed_server_through_launchd(self):
        from tools import dashboard_open
        home=str(self.a.paths.HOME);answers=iter([None,None,home,home])
        with patch.object(dashboard_open,'served_workspace',side_effect=lambda *a,**k:next(answers)), \
             patch.object(dashboard_open,'_always_on',return_value=True), \
             patch.object(dashboard_open,'start_server') as start, \
             patch.object(self.a.subprocess,'run') as run, patch.object(self.a.time,'sleep'):
            run.return_value=type('R',(),{'returncode':0})()
            doc=self.a.serve()
        start.assert_not_called()
        argv=run.call_args[0][0]
        self.assertEqual(argv[:3],['launchctl','kickstart','-k'])
        self.assertTrue(argv[3].endswith('.server'))
        self.assertEqual(doc['server'],'verified')

    def test_serve_refuses_a_port_another_workspace_holds(self):
        from tools import dashboard_open
        with patch.object(dashboard_open,'served_workspace',return_value='/elsewhere'), \
             patch.object(dashboard_open,'start_server') as start:
            with self.assertRaisesRegex(self.a.AssistantError,'dashboard-port-busy'):self.a.serve()
            start.assert_not_called()

    def test_serve_failure_is_typed(self):
        from tools import dashboard_open
        with patch.object(dashboard_open,'served_workspace',return_value=None), \
             patch.object(dashboard_open,'_always_on',return_value=False), \
             patch.object(dashboard_open,'start_server',side_effect=self.a.configuration.ConfigurationError('exited 1')):
            with self.assertRaisesRegex(self.a.AssistantError,'dashboard-start-failed'):self.a.serve()

    def test_build_runs_the_dashboard_step_and_reports_failure(self):
        from tools import dashboard_open
        with patch.object(dashboard_open,'build',return_value=1):
            with self.assertRaisesRegex(self.a.AssistantError,'dashboard-build-failed'):self.a.build()
        index=self.a.paths.DASHBOARD_DIR/'index.html'
        def built():
            index.parent.mkdir(parents=True,exist_ok=True);index.write_text('<html></html>');return 0
        with patch.object(dashboard_open,'build',side_effect=built), \
             patch.object(dashboard_open,'served_workspace',return_value=None):
            self.assertEqual(self.a.build()['files'],str(index.resolve()))

    def configure_model(self, chain=('example/model-a',), ceilings=(1.0, 10.0, 0.5)):
        models=self.a.paths.config_file('models.json')
        doc=json.loads(models.read_text())
        doc['chain']=[{'id':m,'why':'configured'} for m in chain]
        doc['wallet'].update(daily_ceiling=ceilings[0],monthly_ceiling=ceilings[1],velocity_ceiling=ceilings[2])
        models.write_text(json.dumps(doc))
    def enabled(self, model=True):
        from contextlib import ExitStack
        if model:self.configure_model()
        ctx=ExitStack()
        ctx.enter_context(patch.object(self.a.configuration,'enabled',return_value=True))
        ctx.enter_context(patch.object(self.a.providers,'have_key',return_value=True))
        ctx.enter_context(patch.object(self.a.jobs,'_spawn',return_value=type('Proc',(),{'pid':os.getpid()})()))
        return ctx
    def test_replay_deduplicates_and_other_question_is_busy(self):
        with self.enabled():
            first=self.a.ask(self.request())
            self.assertEqual(self.a.ask(self.request())['job']['id'],first['job']['id'])
            with self.assertRaisesRegex(self.a.AssistantError,'request-id-conflict'):
                self.a.ask(self.request(question='Different'))
            with self.assertRaisesRegex(self.a.AssistantError,'assistant-busy'):
                self.a.ask(self.request(request_id='request-other-002'))
    def test_journal_failure_never_spawns(self):
        original=self.a.write
        def full(p,d):
            if p.name=='requests.json':raise OSError('full')
            return original(p,d)
        with self.enabled(),patch.object(self.a,'write',side_effect=full),patch.object(self.a.jobs,'_spawn') as spawn:
            with self.assertRaises(OSError):self.a.ask(self.request())
            spawn.assert_not_called()
    def test_completed_answer_archives_and_cancel_is_terminal(self):
        reply={'parsed':{'answer':'Demo exists','evidence_ids':['E1'],'next_steps':[]},'model':'fixture','cost':0}
        with self.enabled(),patch.object(self.a.providers,'complete',return_value=reply):
            first=self.a.ask(self.request())
            job=self.a.jobs.get(first['job']['id'])
            self.a.run_question(job)
            c=self.a.get_conversation(first['conversation_id'])
            self.assertEqual(c['turns'][0]['answer']['answer'],'Demo exists')
            second=self.a.ask(self.request(request_id='request-other-002'))
            job=self.a.jobs.get(second['job']['id'])
            with patch.object(self.a.jobs.os,'killpg'):
                self.a.jobs.cancel(job['id'])
            self.a.run_question(job)
            self.assertEqual(self.a.jobs.get(job['id'])['status'],'cancelled')
            self.assertNotIn('answer',self.a.get_conversation(second['conversation_id'])['turns'][0])
    def test_real_detached_runner_dispatches_and_persists_answer(self):
        import time
        real_spawn=self.a.jobs._spawn
        # Only the provider boundary is replaced in this test child. The real
        # run_job dispatcher, heartbeat, completion and private files execute.
        code=("import sys,runpy;sys.path.insert(0,"+repr(str(ROOT))+");"
              "from agent import assistant;"
              "assistant.providers.complete=lambda *a,**k:{'parsed':{'answer':'Demo active','evidence_ids':['E1'],'next_steps':[]},'model':'fixture','cost':0};"
              "runpy.run_path("+repr(str(ROOT/'tools/run_job.py'))+",run_name='__main__')")
        children=[]
        def spawn(argv,env):
            child=real_spawn([sys.executable,'-c',code,argv[-1]],env);children.append(child);return child
        with self.enabled(),patch.object(self.a.jobs,'_spawn',side_effect=spawn):
            first=self.a.ask(self.request())
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                doc=self.a.jobs.get(first['job']['id'])
                if doc['status'] in self.a.jobs.TERMINAL:break
                time.sleep(.05)
            if doc['status'] not in self.a.jobs.TERMINAL:self.a.jobs.cancel(doc['id'])
            for child in children:child.wait(timeout=5)
            self.assertEqual(doc['status'],'completed',doc.get('error'))
            self.assertEqual(self.a.get_conversation(first['conversation_id'])['turns'][0]['answer']['answer'],'Demo active')
            self.assertEqual(self.a.ask(self.request())['job']['id'],doc['id'])

    # ── the registry as a real estate writes it ─────────────────────────────
    def real_estate(self, projects=60, findings=20):
        """The registry's real keys: `updated_on`, `built_at`, `subject`, long descriptions."""
        reg = self.a.paths.REGISTRY
        reg.joinpath('projects.json').write_text(json.dumps({'schema_version': 1, 'updated_on': '2026-01-02',
            'projects': [{'id': f'project:p{i:03d}', 'name': f'P{i}', 'lifecycle': 'active',
                          'description': 'd' * 450, 'local_folders': [f'p{i:03d}'],
                          'sites': [{'host': f'p{i:03d}.example', 'owned_domain': f'p{i:03d}.example',
                                     'confidence': 'declared', 'evidence': []}],
                          'ownership': 'owned', 'membership_rules': []} for i in range(projects)]}))
        reg.joinpath('findings.json').write_text(json.dumps({'built_at': '2026-01-02T03:04:05Z',
            'findings': [{'id': f'f{i}', 'type': 'heroku.app_down', 'severity': 'critical' if i < 5 else 'warning',
                          'subject': f'domain:p{i:03d}.example', 'title': f'Finding {i}', 'detail': 'x' * 300,
                          'action': f'act {i}', 'first_seen': '2026-01-01'} for i in range(findings)]}))
        self.a.paths.SCRATCH.joinpath('machine.json').write_text(json.dumps({
            'measured_at': '2026-01-02T03:00:00Z', 'memory': {'total_mb': 16384, 'free_mb': 900, 'swap_used_mb': 4000},
            'disk': {'volume': {'free_gb': 29.0, 'total_gb': 460.0, 'free_percent': 6.3},
                     'locations': [{'label': f'Cache {i}', 'gb': float(i), 'kind': 'cache'} for i in range(12)]}}))

    def test_an_unmeasured_machine_is_named_not_measured_with_its_command(self):
        # A workspace where `full machine` never ran said "the machine snapshot
        # could not be read" — a failure, about something nobody had measured.
        self.real_estate(projects=2, findings=1)
        self.a.paths.SCRATCH.joinpath('machine.json').unlink()
        ctx = self.a.evidence()
        rows = [d for d in ctx['degraded'] if d.get('source') == 'machine']
        self.assertEqual([d.get('code') for d in rows], ['not-measured'], ctx['degraded'])
        self.assertIn('project-observatory full machine', rows[0]['reason'])
        # an unreadable snapshot is still "unavailable"
        self.a.paths.SCRATCH.joinpath('machine.json').write_text('{not json')
        rows = [d for d in self.a.evidence()['degraded'] if d.get('source') == 'machine']
        self.assertEqual([d.get('code') for d in rows], ['unavailable'])

    def test_disk_evidence_survives_a_large_estate(self):
        self.real_estate()
        ctx = self.a.evidence()
        machine = [i for i in ctx['items'] if i['source'] == 'store/raw/machine.json']
        self.assertEqual(len(machine), 1, ctx['degraded'])
        self.assertEqual(machine[0]['facts']['disk_free_gb'], 29.0)
        self.assertEqual(machine[0]['facts']['memory_free_mb'], 900)
        self.assertEqual(machine[0]['facts']['largest_locations'][0], {'label': 'Cache 11', 'gb': 11.0})
        self.assertEqual(sum(1 for i in ctx['items'] if i['source'] == 'registry/findings.json'), 15)
        self.assertLess(len(json.dumps(ctx['items'])), 24000)
        # What was left out is NAMED with its count, not a bare "budget reached".
        reasons = ' '.join(d['reason'] for d in ctx['degraded'])
        self.assertIn('20 findings', reasons)
        self.assertIn('of 60 projects', reasons)
        # A code and the numbers travel beside the sentence, so an app can word it.
        trimmed = {d['source']: d for d in ctx['degraded'] if d.get('code') == 'trimmed'}
        self.assertEqual((trimmed['findings']['total'], trimmed['projects']['total']), (20, 60))

    def test_measurement_times_come_from_the_real_keys(self):
        self.real_estate()
        ctx = self.a.evidence()
        self.assertTrue(all(i['measured_at'] for i in ctx['items']), [i for i in ctx['items'] if not i['measured_at']][:1])
        self.assertNotIn('timestamps', [d['source'] for d in ctx['degraded']])

    def test_project_scope_sees_its_findings_with_subject_and_action(self):
        self.real_estate()
        ctx = self.a.evidence('project:p003')
        found = [i for i in ctx['items'] if i['source'] == 'registry/findings.json']
        self.assertEqual([f['facts']['id'] for f in found], ['f3'])
        self.assertEqual(found[0]['facts']['subject'], 'domain:p003.example')
        self.assertEqual(found[0]['facts']['action'], 'act 3')

    def test_request_index_is_pruned_instead_of_refusing_forever(self):
        with self.enabled():
            idx = {f'request-old-{i:04d}': {'input_hash': 'h', 'job_id': f'gone-{i}', 'conversation_id': 'chat-' + '0' * 32}
                   for i in range(1000)}
            self.a.folder().mkdir(parents=True, exist_ok=True)
            self.a.write(self.a.folder() / 'requests.json', {'requests': idx})
            out = self.a.ask(self.request())
            self.assertTrue(out['job']['id'])
            kept = json.loads((self.a.folder() / 'requests.json').read_text())['requests']
            self.assertEqual(list(kept), ['request-demo-001'])

    def test_conversation_can_be_deleted_but_not_while_working(self):
        reply = {'parsed': {'answer': 'ok', 'evidence_ids': [], 'next_steps': []}, 'model': 'fixture', 'cost': 0}
        with self.enabled(), patch.object(self.a.providers, 'complete', return_value=reply):
            first = self.a.ask(self.request())
            with self.assertRaisesRegex(self.a.AssistantError, 'conversation-busy'):
                self.a.delete_conversation(first['conversation_id'])
            self.a.run_question(self.a.jobs.get(first['job']['id']))
            self.a.delete_conversation(first['conversation_id'])
            self.assertEqual(self.a.list_conversations(), [])
            with self.assertRaisesRegex(self.a.AssistantError, 'unknown-conversation'):
                self.a.get_conversation(first['conversation_id'])
            self.assertNotIn('request-demo-001',
                             json.loads((self.a.folder() / 'requests.json').read_text())['requests'])

    def test_a_pruned_job_does_not_turn_a_completed_answer_into_a_failure(self):
        reply = {'parsed': {'answer': 'kept', 'evidence_ids': [], 'next_steps': []}, 'model': 'fixture', 'cost': 0}
        with self.enabled(), patch.object(self.a.providers, 'complete', return_value=reply):
            first = self.a.ask(self.request())
            self.a.run_question(self.a.jobs.get(first['job']['id']))
        (self.a.jobs.jobs_dir() / f"{first['job']['id']}.json").unlink()
        turn = self.a.get_conversation(first['conversation_id'])['turns'][0]
        self.assertEqual((turn['status'], turn['answer']['answer']), ('completed', 'kept'))

    def test_reconciled_failure_is_written_back(self):
        with self.enabled():
            first = self.a.ask(self.request())
        self.a.jobs.update(first['job']['id'], status='failed', error={'code': 'provider-failed', 'message': 'x'})
        self.a.get_conversation(first['conversation_id'])
        stored = json.loads((self.a.conversations_dir() / f"{first['conversation_id']}.json").read_text())
        self.assertEqual((stored['turns'][0]['status'], stored['turns'][0]['error']), ('failed', 'provider-failed'))

    def test_job_and_cancel_answer_only_for_assistant_jobs(self):
        import io
        doc, _ = self.a.jobs.start('fixture', {}, spawn=lambda argv, env: type('Proc', (), {'pid': 12345})())
        for action in ('job', 'cancel'):
            out = io.StringIO()
            with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps({'id': doc['id']}).encode()))), \
                 patch('sys.stdout', out), patch.object(self.a.jobs.os, 'killpg') as kill:
                self.assertEqual(self.a.main([action]), 1)
                kill.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())['error'], 'unknown-job')
        self.assertNotEqual(self.a.jobs.get(doc['id'])['status'], 'cancelled')

    def test_status_names_what_the_model_still_needs(self):
        # A fresh workspace has no model chain and every ceiling at 0: a newcomer
        # with a key got `budget-reached` or "no model in the configured chain".
        doc=self.a.status()
        self.assertEqual((doc['model_configured'],doc['model_status']),(False,'no-model'))
        self.configure_model(ceilings=(0.0,0.0,0.0))
        doc=self.a.status()
        self.assertEqual((doc['model_configured'],doc['model_status']),(True,'no-budget'))
        self.configure_model()
        # No key: not ready, and `next` names the step (it was `ready`, no `next`).
        doc=self.a.status()
        self.assertEqual((doc['model_status'],doc['key_status'],doc['key_source']),('no-key','absent',None))
        self.assertIn('install_key.py" --for observatory',' '.join(doc['next']))
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'sk-or-v1-'+'FAKE'*16}):
            doc=self.a.status()
        self.assertEqual((doc['model_status'],doc['key_status'],doc['next']),('ready','present',[]))
        self.assertIn('OPENROUTER_API_KEY',doc['key_source'])
        self.assertNotIn('FAKE',json.dumps(doc))

    def test_ask_refuses_an_unconfigured_model_before_any_spend(self):
        for setup,code in ((lambda:None,'model-unconfigured'),
                           (lambda:self.configure_model(ceilings=(0.0,10.0,0.5)),'budget-unset')):
            setup()
            with self.enabled(model=False),patch.object(self.a.jobs,'start') as start, \
                 patch.object(self.a.providers,'complete') as complete:
                with self.assertRaisesRegex(self.a.AssistantError,code):self.a.ask(self.request())
                start.assert_not_called();complete.assert_not_called()

    def test_status_names_a_missing_workspace(self):
        (self.a.paths.HOME / 'workspace.json').unlink()
        with self.assertRaisesRegex(self.a.AssistantError, 'unknown-workspace'):
            self.a.status()

    def test_provider_status_never_carries_key_characters(self):
        with patch.object(self.a.providers,'have_key',return_value=False), \
             patch.object(self.a.providers,'read_key',return_value=('',None)):
            self.assertEqual(self.a.status()['provider_status'],'absent')
        with patch.object(self.a.providers,'have_key',return_value=False), \
             patch.object(self.a.providers,'read_key',side_effect=self.a.providers.Fatal('key file is mode 644; chmod 600 it')):
            self.assertEqual(self.a.status()['provider_status'],'refused: key file is mode 644; chmod 600 it')
        with patch.object(self.a.providers,'have_key',return_value=True):
            self.assertNotIn('provider_status',self.a.status())

    def test_status_project_list_is_optional(self):
        self.assertIn('projects', self.a.status())
        doc = self.a.status(include_projects=False)
        self.assertNotIn('projects', doc)
        self.assertEqual(doc['project_count'], 1)
        self.assertEqual(doc['degraded'], [])

    def test_the_deadline_is_disarmed_before_the_answer_is_committed(self):
        calls = []
        reply = {'parsed': {'answer': 'ok', 'evidence_ids': [], 'next_steps': []}, 'model': 'fixture', 'cost': 0}
        original = self.a.save_conversation
        def save(doc):
            calls.append(('save', any(t.get('answer') for t in doc['turns'])))
            return original(doc)
        with self.enabled(), patch.object(self.a.providers, 'complete', return_value=reply):
            first = self.a.ask(self.request())
            with patch.object(self.a.signal, 'alarm', side_effect=lambda s: calls.append(('alarm', s))), \
                 patch.object(self.a, 'save_conversation', side_effect=save):
                self.a.run_question(self.a.jobs.get(first['job']['id']))
        commit = calls.index(('save', True))
        self.assertIn(('alarm', 0), calls[:commit])

    def test_stop_ends_a_runner_started_with_sigterm_blocked(self):
        # The Mac app's bridge once handed its children a blocked signal mask; Stop
        # then marked the job cancelled while the runner kept waiting on the model.
        import signal as sig, time
        real_spawn=self.a.jobs._spawn
        code=("import sys,time,runpy;sys.path.insert(0,"+repr(str(ROOT))+");"
              "from agent import assistant;"
              "assistant.providers.complete=lambda *a,**k:time.sleep(30);"
              "runpy.run_path("+repr(str(ROOT/'tools/run_job.py'))+",run_name='__main__')")
        children=[]
        def spawn(argv,env):
            import subprocess
            log=open(self.a.jobs.jobs_dir()/'runner.log','ab')
            child=subprocess.Popen([sys.executable,'-c',code,argv[-1]],env=env,stdout=log,stderr=log,
                                   start_new_session=True,
                                   preexec_fn=lambda:sig.pthread_sigmask(sig.SIG_BLOCK,{sig.SIGTERM}))
            children.append(child);return child
        with self.enabled(),patch.object(self.a.jobs,'_spawn',side_effect=spawn):
            first=self.a.ask(self.request())
            time.sleep(1.5)                                    # the runner is waiting on the model
            self.a.jobs.cancel(first['job']['id'])
            try:code=children[0].wait(timeout=10)
            except Exception:children[0].kill();children[0].wait();code=None
        self.assertIsNotNone(code,'the runner ignored SIGTERM after Stop')
        self.assertEqual(self.a.jobs.get(first['job']['id'])['status'],'cancelled')

    def test_cancel_during_spawn_stops_new_process(self):
        def spawn(argv,env):
            self.a.jobs.cancel(argv[-1]);return type('Proc',(),{'pid':12345})()
        with patch.object(self.a.jobs.os,'killpg') as kill:
            doc,_=self.a.jobs.start('fixture',{},spawn=spawn)
            self.assertEqual(doc['status'],'cancelled');kill.assert_called_once()

if __name__=='__main__':unittest.main()
