"""Synthetic registry and plugin compatibility checks; never reads a live estate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PublicContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve(); self.home=self.base/'runtime'
        self.registry=self.home/'registry'; self.registry.mkdir(parents=True)
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('OBSERVATORY_','CLAUDE_MEM_'))}
        self.env.update(HOME=str(self.base/'user'), USERPROFILE=str(self.base/'user'),OBSERVATORY_HOME=str(self.home),PYTHONDONTWRITEBYTECODE='1')
        (self.home/'config').mkdir()
        (self.home/'config/activity_tiers.json').write_text((ROOT/'defaults/activity_tiers.json').read_text())
        for filename,key in [('domains','domains'),('domain-exclusions','domains'),('projects','projects'),('repositories','repositories'),('sources','sources')]:
            self.write(filename,{key:[]})
        self.write('relations',{'relations':[],'relation_types':[]})

    def write(self,name,value):
        (self.registry/(name+'.json')).write_text(json.dumps(value))

    def validate(self):
        return subprocess.run([sys.executable,'tools/validate_registry.py'],cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=20)

    def run_code(self,code):
        p=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr)

    def test_private_requirements_use_configured_roots(self):
        self.run_code("""
import importlib.util, json
from pathlib import Path
import paths
spec=importlib.util.spec_from_file_location('runner',paths.ROOT/'collectors/run_plugins.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
secret_store=paths.HOME/'separate-secrets';secret_store.mkdir()
(secret_store/'fixture-token').write_text('synthetic value')
paths.config_file('settings.json').write_text(json.dumps({'schema_version':1,'sources':{'secret_store':str(secret_store)},'integrations':{'test_provider':True},'features':{}}))
paths.config_file('example.json').write_text('{}')
(paths.REGISTRY/'example.json').write_text('{}')
assert r.missing_requirement('secret:fixture-token')==''
assert r.missing_requirement('config:example.json')==''
assert r.missing_requirement('registry:example.json')==''
assert r.missing_requirement('integration:test_provider')==''
assert 'disabled' in r.missing_requirement('integration:another_provider')
assert r.missing_requirement('secret:absent-token')
assert r.missing_requirement('network')==''
assert r.missing_requirement('bin:git')==''
""")

    def test_private_requirements_reject_traversal_and_symlink_escape(self):
        self.run_code("""
import importlib.util
import paths
spec=importlib.util.spec_from_file_location('runner',paths.ROOT/'collectors/run_plugins.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
for kind in ('secret','config','registry'):
    for suffix in ('../outside','/outside','.'):
        assert r.requirement_error(kind+':'+suffix)
        assert r.missing_requirement(kind+':'+suffix)
paths.SECRETS.mkdir()
outside=paths.HOME/'outside';outside.write_text('synthetic')
(paths.SECRETS/'escape').symlink_to(outside)
assert 'escapes' in r.missing_requirement('secret:escape')
inside=paths.SECRETS/'inside';inside.write_text('synthetic')
(paths.SECRETS/'alias').symlink_to(inside)
assert r.missing_requirement('secret:alias')==''
assert r.requirement_error('integration:../bad')
""")

    def test_network_plugin_entrypoints_require_opt_in(self):
        for filename,key in [('cloudflare_analytics','cloudflare_analytics'),
                             ('ga4_analytics','ga4'),('search_console','search_console')]:
            with self.subTest(plugin=filename):
                result=subprocess.run([sys.executable,'plugins/'+filename+'.py'],
                                      cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout,'')
                self.assertIn('integration '+key+' disabled',result.stderr)

    def test_initialized_local_pipeline_preserves_degradation(self):
        home=self.base/'full-runtime'; self.env['OBSERVATORY_HOME']=str(home)
        projects=self.base/'projects'; (projects/'sample').mkdir(parents=True)
        (projects/'sample/README.md').write_text('Synthetic local project')
        steps=[['observatory.py','init'],['collectors/scan_filesystem.py',str(home/'store/raw/local.json')],['collectors/merge.py'],['collectors/emit_registry.py'],['tools/validate_registry.py']]
        for index,step in enumerate(steps):
            if index==1:
                settings=home/'config/settings.json';d=json.loads(settings.read_text())
                d['sources']['projects']=str(projects);settings.write_text(json.dumps(d))
            p=subprocess.run([sys.executable,*step],cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=30)
            self.assertEqual(p.returncode,0,step[0]+': '+p.stderr)
        model=json.loads((home/'store/raw/model.json').read_text())
        public=json.loads((home/'registry/projects.json').read_text())
        self.assertEqual(len(public['projects']),1)
        self.assertEqual(public['degraded'],model['degraded'])
        self.assertTrue(public['degraded'])
        self.assertEqual(public['projects'][0]['source_refs'],['SRC-0007'])
        sources=json.loads((home/'registry/sources.json').read_text())['sources']
        github=next(x for x in sources if x['id']=='SRC-0008')
        self.assertIsNone(github['observed_on'])
        self.assertEqual(github['availability'],'not-measured')
        (home/'store/raw/local.json').unlink()
        p=subprocess.run([sys.executable,'collectors/merge.py'],cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=30)
        self.assertNotEqual(p.returncode,0)

    def test_projects_that_slug_to_one_key_are_all_kept(self):
        home=self.base/'collide-runtime'; self.env['OBSERVATORY_HOME']=str(home)
        projects=self.base/'collide-projects'
        for name in ('a b','a-b'):
            (projects/name).mkdir(parents=True); (projects/name/'README.md').write_text('Synthetic '+name)
        steps=[['observatory.py','init'],['collectors/scan_filesystem.py',str(home/'store/raw/local.json')],['collectors/merge.py'],['collectors/emit_registry.py']]
        for index,step in enumerate(steps):
            if index==1:
                settings=home/'config/settings.json';d=json.loads(settings.read_text())
                d['sources']['projects']=str(projects);settings.write_text(json.dumps(d))
            p=subprocess.run([sys.executable,*step],cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=30)
            self.assertEqual(p.returncode,0,step[0]+': '+p.stderr)
        public=json.loads((home/'registry/projects.json').read_text())
        names=sorted(x['name'] for x in public['projects'])
        self.assertEqual(names,['a b','a-b'],'both colliding projects survive the merge')
        self.assertEqual(len({x['id'] for x in public['projects']}),2)
        self.assertTrue(any(d.get('source')=='identity' and 'share the key' in d.get('reason','') for d in public['degraded']))

    def test_empty_registry_and_optional_sources(self):
        p=self.validate();self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('degraded_sources=2',p.stdout)
        self.assertIn('export parity not verified',p.stdout)

    def test_generic_domains_are_not_owner_specific(self):
        self.write('domains',{'domains':[{'id':'domain:example.org','name':'example.org','ownership':'owned','registrar':'generic-registrar','dns':{'provider':'generic-dns'},'source_refs':[]}]})
        p=self.validate();self.assertEqual(p.returncode,0,p.stderr)

    def test_bad_reference_still_fails(self):
        self.write('relations',{'relation_types':['owns'],'relations':[{'id':'relation:1','type':'owns','from':'project:missing','to':'domain:missing.example','source_refs':[]}]})
        p=self.validate();self.assertEqual(p.returncode,1)
        self.assertIn('unresolved relation endpoint',p.stderr)

    def test_configured_snapshot_compares_only_cloudflare_dns(self):
        snapshot=self.base/'snapshot.json';snapshot.write_text(json.dumps({'zones':[{'domain_id':'domain:example.org','status':'active','plan':'business'}]}))
        (self.home/'config').mkdir(exist_ok=True)
        (self.home/'config/settings.json').write_text(json.dumps({'schema_version':1,'sources':{'cloudflare_snapshot':str(snapshot)},'integrations':{},'features':{}}))
        self.write('domains',{'domains':[{'id':'domain:example.org','name':'example.org','ownership':'owned','registrar':'generic-registrar','dns':{'provider':'cloudflare','zone_present':True},'source_refs':[]}]})
        p=self.validate();self.assertEqual(p.returncode,0,p.stderr)
        snapshot.write_text(json.dumps({'zones':[]}))
        p=self.validate();self.assertEqual(p.returncode,1);self.assertIn('snapshot mismatch',p.stderr)

    def test_malformed_input_fails_without_traceback(self):
        self.write('projects',{'projects':[{}]})
        p=self.validate();self.assertEqual(p.returncode,1);self.assertNotIn('Traceback',p.stderr)

    def test_plugin_versions_and_paths_before_execution(self):
        self.run_code('''
from pathlib import Path
import tempfile, sqlite3
from collectors import run_plugins as r
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp); plugins=root/'plugins';plugins.mkdir();r.PLUGINS=plugins
 marker=root/'executed'
 script=plugins/'safe.py';script.write_text('from pathlib import Path\\nPath('+repr(str(marker))+').write_text("yes")\\n')
 m={'id':'synthetic','title':'Synthetic','script':'safe.py','metrics':[{'name':'size','unit':'bytes','means':'Synthetic size'}],'why':'Test boundary','requires':[]}
 assert r.check_manifest(m)==[]
 c=sqlite3.connect(':memory:');c.execute('CREATE TABLE metrics(project_id TEXT,metric TEXT,at TEXT,value REAL,unit TEXT,source TEXT,payload_json TEXT,recorded_at TEXT,UNIQUE(project_id,metric,at))')
 for version in (2,True,'1',None):
  bad=dict(m,api_version=version)
  result=r.run_one(c,bad,set(),True)
  assert 'unsupported plugin' in result['skipped'] and not marker.exists()
 outside=root/'outside.py';outside.write_text(script.read_text())
 (plugins/'escape.py').symlink_to(outside)
 for name in ('../outside.py',str(outside),'escape.py'):
  result=r.run_one(c,dict(m,script=name),set(),True)
  assert 'manifest refused' in result['skipped'] and not marker.exists()
 for shape in ({'metrics':['bad']},{'requires':'network'},{'every_hours':float('nan')}):
  result=r.run_one(c,dict(m,**shape),set(),True)
  assert result['skipped'] and not marker.exists()
 result=r.run_one(c,m,set(),True)
 assert marker.exists() and result['written']==0 and not result['skipped']
 c.close()
''')

    def test_plugin_bad_output_is_refused_without_crashing(self):
        self.run_code("""
from pathlib import Path
import json, tempfile, sqlite3
from collectors import run_plugins as r
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp); r.PLUGINS=root
 rows=[[],{'metric':[]},{'metric':'size','project_id':[]}, {'metric':'size','project_id':'project:synthetic','at':'2026-01-01T00:00:00Z','value':float('inf')}, {'metric':'size','project_id':'project:synthetic','at':'2026-01-01T00:00:00Z','value':3}]
 (root/'rows.py').write_text('print('+repr('\\n'.join(json.dumps(x) for x in rows))+')')
 m={'id':'synthetic','title':'Synthetic','script':'rows.py','metrics':[{'name':'size','unit':'bytes','means':'Synthetic size'}],'why':'Test output','requires':[]}
 c=sqlite3.connect(':memory:');c.execute('CREATE TABLE metrics(project_id TEXT,metric TEXT,at TEXT,value REAL,unit TEXT,source TEXT,payload_json TEXT,recorded_at TEXT,UNIQUE(project_id,metric,at))')
 result=r.run_one(c,m,{'project:synthetic'},True)
 assert result['written']==1 and len(result['refused'])==4, result
 c.close()
""")

    def test_curated_config_paths_are_private(self):
        self.run_code('''
import paths
from tools import sign_credential
import proposals
assert sign_credential.FILE == paths.config_file('credential_annotations.json')
# Curation is private even when the module was imported from public source.
assert 'paths.config_file(name)' in (paths.ROOT/'proposals.py').read_text()
assert 'paths.config_file(name)' in (paths.ROOT/'tools/review.py').read_text()
''')

    def test_google_error_cannot_reflect_signed_credential(self):
        self.run_code("""
import io, json, sys, tempfile, types, urllib.error
from pathlib import Path
from unittest.mock import patch
from plugins import google_auth as g
class Signer:
 @classmethod
 def from_service_account_info(cls, info): return cls()
 def sign(self, value): return b'synthetic-signature'
google=types.ModuleType('google');auth=types.ModuleType('google.auth')
auth.crypt=types.SimpleNamespace(RSASigner=Signer);google.auth=auth
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp)/'account.json';p.write_text(json.dumps({'client_email':'synthetic@example.invalid','private_key':'synthetic-private-key'}))
 leaked='synthetic-reflected-signed-assertion'
 error=urllib.error.HTTPError('https://example.invalid',401,'Unauthorized',{},io.BytesIO(json.dumps({'error_description':leaked}).encode()))
 with patch.dict(sys.modules,{'google':google,'google.auth':auth}), patch.object(g.urllib.request,'urlopen',side_effect=error):
  try: g.access_token(p,'synthetic-scope',now=1)
  except RuntimeError as exc:
   assert 'HTTP 401' in str(exc) and leaked not in str(exc) and 'private-key' not in str(exc)
  else: raise AssertionError('HTTP refusal was swallowed')
""")

if __name__=='__main__': unittest.main()
