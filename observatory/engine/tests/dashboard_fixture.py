"""Build the real dashboard from synthetic data, never a cached personal page."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from emitter_fixture import seed as seed_emitter
PYTHON=str(ROOT/'.venv/bin/python') if (ROOT/'.venv/bin/python').exists() else sys.executable


def _require_bootstrapped_home() -> None:
    """These suites run on the synthetic workspace `run_portable.py` builds (its
    LEGACY bootstrap: `init`, sample projects, a first registry). Started directly
    they used to inherit the operator's live workspace and read its configuration
    without a word; tests/tmp.py now hands them an empty sandbox instead (audit A08
    review), and this says what to run rather than failing deep in the emitter."""
    home = Path(os.environ.get("OBSERVATORY_HOME", ""))
    if not (home / "config" / "settings.json").is_file():
        raise RuntimeError("this suite needs the synthetic workspace of the portable runner: "
                           "python tests/run_portable.py --suite <name>  (from observatory/engine)")


def seed(root: Path, *, samples=2) -> dict:
    _require_bootstrapped_home()
    env=seed_emitter(root)
    for name in ('state','estate','wiki','empty-vault'):(root/name).mkdir(exist_ok=True)
    env.update(OBSERVATORY_DATA=str(root/'estate'), OBSERVATORY_VAULT=str(root/'wiki'),
               CLAUDE_MEM_DB=str(root/'absent-companion.db'), OBSERVATORY_DB=str(root/'state/data.db'),
               OBSERVATORY_DASHBOARD=str(root/'page.html'), OBSERVATORY_DASHBOARD_DIR=str(root/'pages'))
    model=root/'raw/model.json';doc=json.loads(model.read_text())
    for name,p in doc['projects'].items():p['folders']=[name]
    model.write_text(json.dumps(doc))
    p=subprocess.run([PYTHON,str(ROOT/'collectors/emit_registry.py'),str(root/'raw')],cwd=ROOT,env=env,capture_output=True,text=True)
    if p.returncode:raise RuntimeError('synthetic emitter failed: '+p.stderr[-300:])
    from store import db
    now=datetime.now(timezone.utc);stamp=now.strftime('%Y-%m-%dT%H:%M:%SZ');before=(now-timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn=db.connect(root/'state/data.db')
    with conn:
        conn.execute('INSERT INTO scans(id,started_at,finished_at,counts_json,degraded_json,collector_version) VALUES (?,?,?,?,?,?)',('fixture-scan',stamp,stamp,'{}','[]','fixture/1'))
        conn.execute('INSERT INTO events(id,project_id,kind,ref,actor,occurred_at,payload_json) VALUES (?,?,?,?,?,?,?)',('fixture-commit','project:fixture-a','commit','fixture-commit','fixture',stamp,'{"subject":"Synthetic work"}'))
        for at,value in ([(before,1000000000)] if samples>1 else [])+[(stamp,2000000000)]:
            conn.execute('INSERT INTO metrics(project_id,metric,at,value,unit,source,recorded_at) VALUES (?,?,?,?,?,?,?)',('project:fixture-a','disk.bytes',at,value,'bytes','disk-usage',stamp))
    conn.close()
    (root/'state/wallet.json').write_text(json.dumps({'days':{now.strftime('%Y-%m-%d'):2.5},'months':{now.strftime('%Y-%m'):4.0},'denomination':'credits'}))
    (root/'state/provider-health.json').write_text(json.dumps({}))
    # A deliberately different content stamp proves the two clocks independently.
    p=root/'registry/projects.json';doc=json.loads(p.read_text());doc['updated_on']='2001-01-01';p.write_text(json.dumps(doc))
    (root/'registry/findings.json').write_text(json.dumps({'counts':{'info':1,'warning':0,'critical':0},'findings':[{'id':'finding:fixture','type':'work.unattributed','subject':'session-name:fixture','severity':'info','title':'Synthetic unresolved work','detail':'Fixture evidence','action':'Review fixture mapping','evidence':['SRC-0012']}]}))
    from collectors.env_registry import document
    files=[]
    for project,kind,variables in [
        ('fixture-a','env',[{'name':'TEST_SHARED','class':'secret','fingerprint':'synthetic-shared'},{'name':'TEST_LOCAL','class':'secret','fingerprint':'synthetic-local'}]),
        ('fixture-b','env',[{'name':'TEST_SHARED','class':'secret','fingerprint':'synthetic-shared'}]),
        ('fixture-a','template',[{'name':'TEST_TEMPLATE','class':'secret'}])]:
        files.append({'path':project+('/.env' if kind=='env' else '/.env.example'),'project':project,'kind':kind,'git':'ignored','mode':'600','modified_on':stamp[:10],'unparsed_lines':0,'variables':variables})
    (root/'registry/env-inventory.json').write_text(json.dumps(document({'files':files,'scanned_at':stamp,'root':str(root/'estate')},stamp[:10])))
    return env


def build(root: Path, *, samples=2) -> Path:
    env=seed(root,samples=samples)
    p=subprocess.run([PYTHON,str(ROOT/'dashboard/build_dashboard.py')],cwd=ROOT,env=env,capture_output=True,text=True)
    if p.returncode:raise RuntimeError('synthetic dashboard failed: '+p.stderr[-400:])
    return Path(env['OBSERVATORY_DASHBOARD'])
