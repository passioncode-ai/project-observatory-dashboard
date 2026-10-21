"""Shared private synthetic estate for legacy integration suites.

No account, credential, network collector or existing runtime is used.
"""
from __future__ import annotations
import atexit
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
PROJECT_ID='project:example-sample-0'
_BASE=None

def setup():
    global _BASE
    if _BASE is not None: return _BASE
    temp=tempfile.TemporaryDirectory(prefix='observatory-synthetic-')
    atexit.register(temp.cleanup)
    base=Path(temp.name).resolve(); _BASE=base
    from run_portable import runtime_environment
    from git_env import git_env
    env=git_env({**runtime_environment(),'PATH':os.environ['PATH'],'HOME':str(base/'user'),'LANG':'en_US.UTF-8',
                 'PYTHONDONTWRITEBYTECODE':'1','OBSERVATORY_HOME':str(base/'runtime')})
    os.environ.clear();os.environ.update(env)
    (base/'user').mkdir(); (base/'tmp').mkdir(); os.environ['TMPDIR']=str(base/'tmp'); tempfile.tempdir=str(base/'tmp')
    projects=base/'projects';projects.mkdir()
    def run(*args):
        p=subprocess.run(args,cwd=ROOT,env=dict(os.environ),capture_output=True,text=True,timeout=40)
        if p.returncode: raise RuntimeError('Synthetic setup failed: '+p.stderr[-500:])
    run(sys.executable,'observatory.py','init')
    runtime=base/'runtime'
    settings=runtime/'config/settings.json';d=json.loads(settings.read_text());d['sources']['projects']=str(projects);settings.write_text(json.dumps(d))
    (runtime/'config/ownership.json').write_text(json.dumps({'organizations':[],'work_organizations':[]}))
    for i in range(8):
        directory=projects/f'sample-{i}';directory.mkdir();(directory/'README.md').write_text('Synthetic example project '+str(i))
        run('git','init','-q','-b','main',str(directory))
        run('git','-C',str(directory),'remote','add','origin',f'https://github.com/example/sample-{i}.git')
    for args in [('collectors/scan_filesystem.py',str(runtime/'store/raw/local.json')),('collectors/merge.py',),('collectors/emit_registry.py',)]:run(sys.executable,*args)
    reg=runtime/'registry'
    pdoc=json.loads((reg/'projects.json').read_text()); rdoc=json.loads((reg/'repositories.json').read_text())
    ids=[p['id'] for p in pdoc['projects']]
    assert PROJECT_ID in ids,ids
    for project in pdoc['projects']: project['ownership']='owned'
    pdoc['projects'][0]['sites']=[{'host':'example.test','owned_domain':None,'confidence':'declared-not-in-registry','evidence':['fixture']}]
    for row in rdoc['repositories']:
        row['local'].update(sync='behind',remote_head='b'*40,remote_checked_on='2026-01-01')
        row['source_refs']=sorted(set(row['source_refs'])|{'SRC-0010','SRC-0005'})
    (reg/'projects.json').write_text(json.dumps(pdoc));(reg/'repositories.json').write_text(json.dumps(rdoc))
    sdoc=json.loads((reg/'sources.json').read_text());sdoc['sources'].append({'id':'SRC-0005','kind':'operator','description':'Synthetic curation'})
    (reg/'sources.json').write_text(json.dumps(sdoc))
    (reg/'domain-liveness.json').write_text(json.dumps({'schema_version':1,'source_refs':['SRC-0011'],'scanned_on':'2026-01-01','hosts':[],'degraded':[]}))
    (runtime/'config/repo_overrides.json').write_text(json.dumps({'repositories':{'example/sample-0':{'source_refs':['SRC-0005']}}}))
    (runtime/'config/project_overrides.json').write_text(json.dumps({'projects':{'sample-0':{'description':'Synthetic description'}}}))
    now=datetime.datetime.now(datetime.timezone.utc)
    run(sys.executable,'-c',"from store import db; c=db.connect(); c.execute(\"INSERT INTO metrics(project_id,metric,at,value,unit,source,payload_json,recorded_at) VALUES (?,?,?,?,?,?,?,?)\", ('"+PROJECT_ID+"','disk.bytes','2026-01-01T00:00:00Z',2000000000,'bytes','disk-usage','{}','2026-01-01T00:00:00Z')); c.execute(\"INSERT INTO metrics(project_id,metric,at,value,unit,source,payload_json,recorded_at) VALUES (?,?,?,?,?,?,?,?)\", ('"+PROJECT_ID+"','disk.bytes','2026-01-02T00:00:00Z',3000000000,'bytes','disk-usage','{}','2026-01-02T00:00:00Z')); c.commit();c.close()")
    run(sys.executable,'-c',"from store import db; c=db.connect(); ids="+repr(ids)+"; template=c.execute('SELECT metric,at,value,unit,source,payload_json,recorded_at FROM metrics').fetchall(); c.executemany('INSERT OR IGNORE INTO metrics(project_id,metric,at,value,unit,source,payload_json,recorded_at) VALUES (?,?,?,?,?,?,?,?)', [(pid,*row) for pid in ids for row in template]); c.commit();c.close()")
    models=json.loads((runtime/'config/models.json').read_text());models['chain']=[{'id':'vendor/synthetic','why':'Synthetic test catalogue'}];models['wallet'].update(daily_ceiling=2,monthly_ceiling=10,velocity_ceiling=2)
    (runtime/'config/models.json').write_text(json.dumps(models))
    (runtime/'store/openrouter-catalogue.json').write_text(json.dumps({'fetched_at':now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'models':{'vendor/synthetic':{'id':'vendor/synthetic','price_in_per_1m':1.0,'price_out_per_1m':1.0,'context_length':1000,'supported_parameters':['response_format']}}}))
    (runtime/'store/wallet.json').write_text(json.dumps({'events':[],'days':{now.strftime('%Y-%m-%d'):0.2},'months':{now.strftime('%Y-%m'):0.5},'denomination':'credits'}))
    run(sys.executable,'tools/build_findings.py')
    run(sys.executable,'dashboard/build_dashboard.py')
    return base
