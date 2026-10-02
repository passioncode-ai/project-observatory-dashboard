#!/usr/bin/env python3
"""OSS-10: execute generated commands against inert tools in synthetic paths."""
from __future__ import annotations
import ast
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node is required to execute dashboard command generation')
class DashboardPortabilityTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'dashboard/build_dashboard.py').read_text())
        self.template = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TEMPLATE' for t in n.targets))
        self.tmp = tempfile.TemporaryDirectory(prefix='observatory-ui-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.engine = self.base / "engine with 'quote"
        self.home = self.base / "private home with 'quote"
        self.projects = self.base / 'project folders'
        self.secrets = self.base / 'secret files'
        self.engine.mkdir()
        (self.engine / 'tools').mkdir()
        self.cwd = self.base / 'unrelated cwd'
        self.cwd.mkdir()
        self.runtime = dict(engine=str(self.engine), home=str(self.home), python=sys.executable, projects=str(self.projects), secrets=str(self.secrets), scratch=str(self.home / 'store/raw'))
        self.helpers = self.template.split('const RUNTIME =', 1)[1].split('// __SHARED_BELOW__', 1)[0]
        self.helpers = 'const RUNTIME =' + self.helpers
        self.credentials = self.template.split('const doorOf =', 1)[1].split('const CRED_SECTIONS', 1)[0]
        self.credentials = 'const doorOf =' + self.credentials
        self.env_actions = 'function envActions' + self.template.split('function envActions', 1)[1].split('\ndocument.addEventListener', 1)[0]

    def javascript(self, expression, *, payload=None, extra=''):
        data = {'runtime':self.runtime, 'rows':[], **(payload or {})}
        # T is the page's translator; English ids read as themselves here.
        script = ('const D=' + json.dumps(data) + '; const LIVE=false; const PROJ_NAME=new Map(); const T=s=>s;\n'
                  + 'const E=x=>String(x).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");\n'
                  + self.helpers + '\n' + self.credentials + '\n' + self.env_actions + '\n' + extra
                  + '\nconsole.log(JSON.stringify(' + expression + '));')
        proc = subprocess.run(['node','-e',script],text=True,capture_output=True,check=True)
        return json.loads(proc.stdout)

    def inert_tool(self, name):
        (self.engine / 'tools' / name).write_text('import json, os, sys\nprint(json.dumps({"args":sys.argv[1:],"home":os.environ.get("OBSERVATORY_HOME")}))\n')

    def execute(self, command):
        env = dict(os.environ, OBSERVATORY_HOME=str(self.base / 'wrong home'))
        proc = subprocess.run(['/bin/sh','-c',command],cwd=self.cwd,env=env,text=True,capture_output=True,check=True)
        return json.loads(proc.stdout)

    def test_tool_command_preserves_workspace_interpreter_and_arbitrary_arguments(self):
        self.inert_tool('ack.py')
        hostile = "name'; printf INJECTION; # $(printf SUBSTITUTION) `printf BACKTICK`\nnext"
        args = [hostile, '--why', 'quoted "reason"']
        command = self.javascript('toolCommand("ack.py", ' + json.dumps(args) + ')')
        result = self.execute(command)
        self.assertEqual(result, {'args':args,'home':str(self.home)})

    def test_provider_actions_preserve_full_name_as_one_argument(self):
        self.inert_tool('openrouter.py')
        name = "provider key; echo WRONG $(echo WRONG) 'quote"
        commands = self.javascript('credVerbs(' + json.dumps({'id':'credential:demo','kind':'llm-api-key','name':name}) + ')')
        for label, command, action in commands[1:]:
            result = self.execute(command)
            self.assertEqual(result['args'][1],name, label)
            self.assertEqual(result['home'],str(self.home))

    def test_credential_annotation_quotes_identifier(self):
        self.inert_tool('sign_credential.py')
        identifier = "credential:synthetic'; echo WRONG; #"
        command = self.javascript('credVerbs(' + json.dumps({'id':identifier,'kind':'machine-secret','name':'TOKEN'}) + ')[0][1]')
        self.assertEqual(self.execute(command)['args'], ['set',identifier,'--purpose','…','--evidence','…'])

    def test_vault_secret_input_uses_explicit_private_file_without_mac_clipboard(self):
        commands = self.javascript('credVerbs(' + json.dumps({'id':'credential:demo','vault_project':'demo','env':'prod','name':'TOKEN'}) + ')')
        command = commands[-1][1]
        self.assertNotIn('pbpaste',command)
        self.assertIn("< '/absolute/path/to/private-input'",command)
        self.assertNotIn('cat ',command)

    def test_project_secret_paths_resolve_against_configured_root(self):
        credential = {'id':'credential:demo','kind':'project-secret-file','in_project':"project's folder",'path':'secrets/key file.json','name':'key file.json'}
        actions = self.javascript('credVerbs(' + json.dumps(credential) + ')')
        import shlex
        command = shlex.split(actions[1][1])
        self.assertEqual(command[-1], str(self.projects / "project's folder/secrets/key file.json"))
        self.assertIn(str(self.engine / 'tools/vault.py'),command)

    def test_env_action_resolves_project_and_never_copies_a_reveal_command(self):
        self.inert_tool('use_secret.py')
        for row, project in [({'project':'demo','path':'demo/.env','name':'TOKEN'},'demo'), ({'path':"folder's name/.env",'name':'TOKEN'},"folder's name")]:
            markup = self.javascript('envActions(' + json.dumps(row) + ')')
            command = html.unescape(re.search(r'data-copy="([^"]+)"',markup).group(1))
            self.assertEqual(self.execute(command)['args'],['where',project,'TOKEN'])
            self.assertNotRegex(command,r'\b(cat|grep|reveal)\b')

    def test_env_without_project_does_not_invent_a_runnable_command(self):
        markup = self.javascript('envActions({name:"TOKEN",path:".env"})')
        self.assertNotIn('data-copy',markup)
        self.assertNotIn('PROJECT',markup)

    def test_movement_evidence_is_one_quoted_argument(self):
        self.inert_tool('vault.py')
        section = 'function movementsSection' + self.template.split('function movementsSection',1)[1].split('\nconst CLS_LABEL',1)[0]
        record = {'project':'demo','vars':['TOKEN'],'app':'name"; echo WRONG; #','version':1,'at':'2026-01-01T00:00:00Z','by':"user's name $(echo WRONG)"}
        markup = self.javascript('movementsSection()',payload={'creds':{'unrecorded':[record]}},extra=section)
        command = html.unescape(re.search(r'data-copy="([^"]+)"',markup).group(1))
        args = self.execute(command)['args']
        self.assertEqual(args[:7],['moved','demo','prod','TOKEN','--at','heroku','--how'])
        self.assertIn(record['app'],args[7])
        self.assertIn(record['by'],args[7])
        self.assertEqual(len(args),8)

    def test_no_public_dashboard_action_depends_on_private_checkout_cwd(self):
        self.assertNotIn('./tools/',self.template)
        self.assertNotIn('./observatory.py',self.template)
        self.assertNotIn('pbpaste',self.template)
        # The empty estate's first step is the installed command, not a path into
        # a checkout: `project-observatory full local`, with this workspace's home.
        self.assertIn('fullCommand("local")',self.template)
        self.assertIn('"project-observatory full "',self.template)


if __name__ == '__main__':
    unittest.main()
