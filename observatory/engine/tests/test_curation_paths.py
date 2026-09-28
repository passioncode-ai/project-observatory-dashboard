#!/usr/bin/env python3
"""Selected policy reaches both readers and writers; fixtures own every write.

Curation — the operator's overrides, acknowledgements, annotations and
exclusions — lives in the selected workspace's `config/` directory. Every
reader and every writer must resolve the same file there, an explicit
environment override must move reader and writer together, and nothing may
fall back to (or write into) the program checkout's own copies.
"""
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from emitter_fixture import seed
PY=sys.executable
FAILURES=[]

def check(name, ok, detail=''):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"+(f' — {detail}' if detail and not ok else ''))
    if not ok: FAILURES.append(name)

def run(env, code):
    p=subprocess.run([PY,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=60)
    check('synthetic reader exits successfully',p.returncode==0,p.stderr[-400:])
    return json.loads(p.stdout) if p.returncode==0 else {}

def prepare(root, label):
    env=seed(root)
    for key in ('OBSERVATORY_ACKS','OBSERVATORY_ANNOTATIONS'):env.pop(key,None)
    # Each root is its own workspace: the engine reads curation from
    # OBSERVATORY_HOME/config, so two roots are two independent policies.
    env['OBSERVATORY_HOME']=str(root/'home')
    p=subprocess.run([PY,'observatory.py','init'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=120)
    if p.returncode: raise RuntimeError('synthetic workspace init failed: '+p.stderr[-300:])
    curated=root/'home/config'
    def write(name,value): (curated/name).write_text(json.dumps(value))
    write('project_overrides.json',{'projects':{'project:fixture-a':{'description':label,'why':'Synthetic policy'}}})
    write('repo_overrides.json',{'repositories':{'repository:fixture/service':{'description':label}}})
    write('finding_acks.json',{'acks':[]})
    write('credential_annotations.json',{'annotations':{'credential:fixture':{'purpose':label}}})
    write('credential_owners.json',{'owners':[{'credential':label,'projects':['project:fixture-a'],'evidence':['fixture:policy']}]})
    write('session_name_exclusions.json',{'names':[{'name':label,'why':'Synthetic exclusion'}],'shape_rules':[]})
    write('host_boundary.json',{'hosts':{'fixture.example':{'status':'outside','why':label}}})
    (root/'registry/findings.json').write_text(json.dumps({'findings':[{'id':'finding:fixture','severity':'info','title':'Synthetic finding'}]}))
    return env


def test_selected_readers_and_explicit_overrides():
    with tempfile.TemporaryDirectory(prefix='observatory-curation-') as td:
        roots=[Path(td)/label for label in ('alpha','beta')]
        for root in roots:
            root.mkdir();env=prepare(root,root.name)
            result=run(env,"""
import json,paths,proposals
from collectors import scan_sessions,credentials_registry as credentials,google_registry as google
from tools import sign_credential
import sys
sys.path.insert(0,str(paths.ROOT/'plugins'))
google.consoles=lambda:{'ga4_report':'https://example.invalid/{pid}','ga4_admin':'https://example.invalid/{pid}'}
rows=google.rows({'properties':[{'property':'properties/1','hosts':['fixture.example']}]},[],paths.config_file('missing-ga.json'))
print(json.dumps({'allowed':sorted(proposals.appliable()['project:']), 'names':scan_sessions.exclusions()[1],
 'owners':credentials.load_owners(), 'annotations':credentials.load_annotations(),
 'annotation_writer':str(sign_credential.FILE),'acks':str(paths.FINDING_ACKS),'boundary':rows[0]['boundary_why']}))
""")
            if not result:continue
            # The engine always admits the structured organization/resources fields
            # (agents report what they created through them); everything else is
            # derived from the selected policy.
            check('proposal fields come from selected policy',result['allowed']==['description','organization','resources'],str(result['allowed']))
            check('session exclusions come from selected policy',set(result['names'])=={root.name})
            check('credential owners come from selected policy',result['owners']==[{'credential':root.name,'projects':['project:fixture-a'],'evidence':['fixture:policy']}])
            check('credential annotations come from selected policy',result['annotations']=={'credential:fixture':{'purpose':root.name}})
            check('signature writer uses the same annotation file',result['annotation_writer']==str(root/'home/config/credential_annotations.json'))
            check('ACK default lives in selected curation',result['acks']==str(root/'home/config/finding_acks.json'))
            check('Google boundary is selected policy',result['boundary']==root.name)
            override={**env,'OBSERVATORY_ACKS':str(root/'explicit-acks.json'),'OBSERVATORY_ANNOTATIONS':str(root/'explicit-annotations.json')}
            selected=run(override,"""
import json,paths
from collectors import credentials_registry
from tools import sign_credential
print(json.dumps({'acks':str(paths.FINDING_ACKS),'reader':str(credentials_registry.ANNOTATIONS),'writer':str(sign_credential.FILE)}))
""")
            check('explicit ACK path outranks curation',selected.get('acks')==override['OBSERVATORY_ACKS'])
            check('explicit annotation path selects reader and writer',selected.get('reader')==selected.get('writer')==override['OBSERVATORY_ANNOTATIONS'])
            for name in ('project_overrides.json','repo_overrides.json','credential_owners.json','credential_annotations.json','session_name_exclusions.json'):
                (root/'home/config'/name).unlink()
            absent=run(env,"""
import json,proposals
from collectors import credentials_registry as c,scan_sessions as s
print(json.dumps({'fields':sorted(proposals.appliable()['project:']),'owners':c.load_owners(),'annotations':c.load_annotations(),'exclusions':s.exclusions()}))
""")
            check('absent optional selected policy never borrows checkout contents',absent=={'fields':['organization','resources'],'owners':[],'annotations':{},'exclusions':[[],{}]},str(absent))


def test_selected_proposal_writer_and_emitter():
    with tempfile.TemporaryDirectory(prefix='observatory-curation-write-') as td:
        a,b=Path(td)/'alpha',Path(td)/'beta';a.mkdir();b.mkdir()
        env=prepare(a,'alpha');prepare(b,'beta')
        other=(b/'home/config/project_overrides.json').read_bytes()
        fake=Path(td)/'code';(fake/'defaults').mkdir(parents=True)
        author=fake/'defaults/project_overrides.json'
        author.write_text(json.dumps({'projects':{'project:fixture-a':{'description':'Synthetic checkout policy'}}}))
        before=author.read_bytes()
        shipped=ROOT/'defaults/project_overrides.json'
        shipped_before=shipped.read_bytes()
        # The queue itself is synthetic; terminal ownership is exercised, not bypassed.
        from store import db,ledger
        conn=db.connect(a/'queue.db')
        proposal=ledger.proposals_add(conn,owner='agent:fixture',target_id='project:fixture-a',
                                     patch={'description':'Accepted synthetic description'},evidence=[{'uri':'fixture:policy'}])
        conn.close();env['OBSERVATORY_DB']=str(a/'queue.db')
        driver="import paths; from pathlib import Path; paths.ROOT=Path("+repr(str(fake))+"); from tools import review; raise SystemExit(review.main())"
        argv=[PY,'-c',driver,'accept-proposal',proposal['proposalId'],'--why','Synthetic operator decision']
        refused=subprocess.run(argv,cwd=ROOT,env=env,capture_output=True,text=True)
        check('real review still refuses a pipe',refused.returncode!=0 and 'owned by `operator`' in refused.stdout+refused.stderr)
        master,slave=pty.openpty()
        try:p=subprocess.run(argv,cwd=ROOT,env=env,stdin=slave,capture_output=True,text=True,timeout=60)
        finally:os.close(master);os.close(slave)
        check('review accepts only the synthetic queue through a terminal',p.returncode==0,(p.stdout+p.stderr)[-300:])
        target=a/'home/config/project_overrides.json'
        check('review names its actual selected destination',str(target) in p.stdout,p.stdout[-300:])
        doc=json.loads(target.read_text())['projects']['project:fixture-a']
        check('accepted patch lands in selected curation',doc.get('description')=='Accepted synthetic description')
        check('accepted decision retains reason and proposal evidence',doc.get('why')=='Synthetic operator decision' and doc.get('evidence')==[{'uri':'fixture:policy'}])
        check('second workspace remains byte-identical',(b/'home/config/project_overrides.json').read_bytes()==other)
        check('author policy remains byte-identical',author.read_bytes()==before)
        check('shipped default policy remains byte-identical',shipped.read_bytes()==shipped_before)
        emitted=subprocess.run([PY,'collectors/emit_registry.py',str(a/'raw')],cwd=ROOT,env=env,capture_output=True,text=True)
        check('real emitter applies selected accepted policy',emitted.returncode==0,emitted.stderr[-300:])
        projects=json.loads((a/'registry/projects.json').read_text())['projects']
        check('the project receives the accepted description',next(p for p in projects if p['id']=='project:fixture-a')['description']=='Accepted synthetic description')


def test_selected_metadata_writers():
    with tempfile.TemporaryDirectory(prefix='observatory-curation-metadata-') as td:
        root=Path(td);env=prepare(root,'metadata')
        (root/'registry/credentials.json').write_text(json.dumps({'credentials':[{'id':'credential:fixture'}]}))
        p=subprocess.run([PY,'tools/sign_credential.py','set','credential:fixture','--purpose','Synthetic workload access','--evidence','Synthetic fixture evidence'],cwd=ROOT,env=env,capture_output=True,text=True)
        check('signature command writes selected annotations',p.returncode==0,p.stderr[-300:])
        p=subprocess.run([PY,'tools/ack.py','finding:fixture','--why','Synthetic accepted condition'],cwd=ROOT,env=env,capture_output=True,text=True)
        check('ACK command writes selected policy',p.returncode==0,p.stderr[-300:])
        value=run(env,"""
import json
from collectors import credentials_registry as c
from tools import ack,build_findings as f
print(json.dumps({'purpose':c.load_annotations().get('credential:fixture',{}).get('purpose'),'acks':ack.load_acks()['acks'],'board_acks':str(f.ACKS)}))
""")
        check('credential reader sees the signature',value.get('purpose')=='Synthetic workload access')
        check('ACK reader sees the selected decision',len(value.get('acks',[]))==1 and value['acks'][0]['why']=='Synthetic accepted condition')
        check('board uses the same ACK file',value.get('board_acks')==str(root/'home/config/finding_acks.json'))


def test_invalid_ack_never_overwrites_saved_decisions():
    with tempfile.TemporaryDirectory(prefix='observatory-ack-invalid-') as td:
        root=Path(td);env=prepare(root,'metadata');target=root/'home/config/finding_acks.json'
        malformed=['{private-fixture-content', '[]', '{"acks":{}}', '{"acks":[{}]}',
                   json.dumps({'acks':[{'id':'same','why':'one'},{'id':'same','why':'two'}]}),
                   json.dumps({'acks':[{'id':'some','why':'reason','until':'invalid-date'}]})]
        for body in malformed:
            for args in (['--list'],['finding:fixture','--why','Synthetic reason'],['--undo','finding:fixture']):
                target.write_text(body);before=target.read_bytes()
                p=subprocess.run([PY,'tools/ack.py',*args],cwd=ROOT,env=env,capture_output=True,text=True)
                check('invalid saved ACK refuses '+args[0],p.returncode!=0)
                check('invalid saved ACK bytes survive '+args[0],target.read_bytes()==before)
                check('ACK refusal explains recovery without file content','restore' in p.stderr.lower() and body not in p.stdout+p.stderr)
        target.unlink()
        p=subprocess.run([PY,'tools/ack.py','finding:fixture','--why','Synthetic new register'],cwd=ROOT,env=env,capture_output=True,text=True)
        check('absent ACK file can be initialized',p.returncode==0 and len(json.loads(target.read_text())['acks'])==1)
        p=subprocess.run([PY,'tools/ack.py','--undo','finding:fixture'],cwd=ROOT,env=env,capture_output=True,text=True)
        check('valid selected ACK can still be undone',p.returncode==0 and json.loads(target.read_text())['acks']==[])


if __name__=='__main__':
    for fn in (test_selected_readers_and_explicit_overrides,test_selected_proposal_writer_and_emitter,test_selected_metadata_writers,test_invalid_ack_never_overwrites_saved_decisions):fn()
    if FAILURES:raise SystemExit(1)
