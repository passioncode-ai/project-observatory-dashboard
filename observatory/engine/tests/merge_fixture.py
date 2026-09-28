"""Synthetic complete merge inputs, local Git trees and a strict offline gh stub.

The merge reads its curation from the selected workspace's `config/`, and it
follows GitHub transfers only when the workspace enables the `github`
integration. So a seeded root carries a private workspace of its own (`home/`)
with that integration on, a synthetic owned organisation, and the curation files
the merge requires. The `gh` it finds on PATH is a stub that answers only the
one request shape the merge makes, from a fixed transfer table, and records every
call it receives.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
from emitter_fixture import seed as emitter_seed

#: The owned organisation every fixture repository lives under.
OWNER = 'example-org'
TRANSFERS = {'old/fixture-a': OWNER + '/fixture-a', 'old/fixture-b': OWNER + '/fixture-b'}


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def repo(nwo):
    return dict(nameWithOwner=nwo, url='https://github.com/' + nwo,
                visibility='PRIVATE', isArchived=False, isFork=False)


def local(folder, remote=''):
    return dict(folder=folder, path='/fixture/' + folder, is_git=True, remote=remote,
                symlink=False, kinds=[], readme='', homepage_hits=[], last_commit='',
                commits=1, dirty=0, branch='main', file_count=1, mtime='')


def curation(root):
    """The seeded workspace's configuration directory — where curation lives."""
    return root / 'home' / 'config'


def workspace(root):
    """Initialize `root/home` with GitHub enabled and one owned organisation."""
    import workspace as ws
    home = root / 'home'
    ws.initialize(home, identities=False)
    settings = home / 'config' / 'settings.json'
    doc = json.loads(settings.read_text(encoding='utf-8'))
    doc['integrations']['github'] = True
    settings.write_text(json.dumps(doc), encoding='utf-8')
    write(home / 'config' / 'ownership.json',
          {'organizations': [OWNER], 'work_organizations': []})
    return home


def seed(root):
    env = emitter_seed(root)
    raw, cfg = root/'raw', curation(root)
    workspace(root)
    (raw/'model.json').unlink()
    (raw/'gh').mkdir()
    write(raw/'gh/fixture.json', [repo(k) for k in
          [*TRANSFERS.values(), OWNER + '/fixture-parent', OWNER + '/fixture-module-a',
           OWNER + '/fixture-module-b']])
    rows = [local(k.split('/')[1], 'https://github.com/'+k) for k in TRANSFERS]
    rows += [local('fixture-parent', 'https://github.com/' + OWNER + '/fixture-parent'),
             local('fixture-unknown', 'https://github.com/unknown/gone'),
             local('fixture-excluded')]
    write(raw/'local.json', {'folders': rows, 'degraded': []})
    write(raw/'vault.json', [dict(folder='fixture-parent', overview='', note_count=0,
          summary='', domains=[], github=[], bitbucket=[], datapaths=[])])
    for name, value in [('denied_links', {'denied': []}), ('verified_links', {'links': []}),
                        ('domain_claims', {'claims': {}}),
                        ('folder_exclusions', {'prefixes': [], 'names': [
                            {'name': 'fixture-excluded', 'why': 'This fixture is a container rather than a project.'}]})]:
        write(cfg/(name+'.json'), value)
    stub = root/'bin'; stub.mkdir()
    (root/'nobin').mkdir()
    script = f'''#!{sys.executable}
import json, pathlib, sys
with open({str(root/'gh-calls.jsonl')!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
args=sys.argv[1:]
if len(args)!=4 or args[0]!='api' or args[2:]!=['--jq', '.full_name']:
    print('Unexpected fixture gh request', file=sys.stderr); sys.exit(70)
nwo=args[1].removeprefix('repos/')
transfers={TRANSFERS!r}
if nwo in transfers: print(transfers[nwo]); sys.exit(0)
print('Not Found (HTTP 404)', file=sys.stderr); sys.exit(1)
'''
    (stub/'gh').write_text(script); (stub/'gh').chmod(0o700)
    return environment(root)


def environment(root, absent=False):
    from emitter_fixture import environment as base
    return {**base(root), 'PATH': str(root/('nobin' if absent else 'bin')),
            'OBSERVATORY_HOME': str(root/'home'),
            'OBSERVATORY_VAULT': str(root/'wiki'), 'OBSERVATORY_DATA': str(root/'data')}


def git_estate(root):
    """Use actual Git locally with no hooks, template, identity or remote lookup."""
    data=root/'data'; data.mkdir(exist_ok=True)
    checkout=data/'fixture-local'; checkout.mkdir(exist_ok=True)
    if not (checkout/'.git').exists():
        env={**os.environ, 'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':os.devnull}
        subprocess.run(['git', 'init', '--template=', str(checkout)], env=env,
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath='+os.devnull,
                        'commit', '--allow-empty', '-m', 'Synthetic fixture'], env=env,
                       check=True, capture_output=True)
    (data/'fixture-excluded').mkdir(exist_ok=True)
    return data
