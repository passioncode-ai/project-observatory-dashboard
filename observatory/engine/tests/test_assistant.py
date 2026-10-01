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

    def test_dashboard_refuses_another_workspace_and_uses_selected_port(self):
        from tools import dashboard_open
        (self.a.paths.SCRATCH/'serverd.json').write_text(json.dumps({'port':48123}))
        with patch.object(dashboard_open,'served_workspace',return_value='/another-workspace'):
            with self.assertRaisesRegex(self.a.AssistantError,'dashboard-workspace-mismatch'):self.a.dashboard()
        with patch.object(dashboard_open,'served_workspace',return_value=str(self.a.paths.HOME)) as get:
            self.assertEqual(self.a.dashboard()['url'],'http://127.0.0.1:48123/dashboard/index.html')
            get.assert_called_once_with(48123,timeout=3)

    def enabled(self):
        from contextlib import ExitStack
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

    def test_cancel_during_spawn_stops_new_process(self):
        def spawn(argv,env):
            self.a.jobs.cancel(argv[-1]);return type('Proc',(),{'pid':12345})()
        with patch.object(self.a.jobs.os,'killpg') as kill:
            doc,_=self.a.jobs.start('fixture',{},spawn=spawn)
            self.assertEqual(doc['status'],'cancelled');kill.assert_called_once()

if __name__=='__main__':unittest.main()
