#!/usr/bin/env python3
"""Run the explicit, synthetic full-engine regression set in private sandboxes.

No test discovery, inherited credentials, original registry or operational data
is used. Each suite gets a source copy and a fresh HOME/OBSERVATORY_HOME. JSON
on stdout is the receipt; progress goes to stderr. Network-backed provider and
external host acceptance tests are explicitly outside this offline set.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
LEGACY = (
    'ledger', 'time', 'provider_boundary', 'provider_health', 'credential_doors',
    'vault', 'plugins', 'wire_contract', 'metric_series', 'analytics_plugins',
    'mcp_wire', 'wire_inputs', 'retention', 'provenance', 'events', 'fingerprints',
    'pages', 'dashboard_render', 'atomic_writers', 'ack', 'rollup',
    'runtime_identity', 'remote_env',
    'project_identity',
    'sessions',
    'env_page', 'metric_labels', 'hosting_groups', 'action_outcomes', 'workspace_redesign', 'google_identity', 'i18n',
)
BOUNDARY = (
    'workspace', 'workspace_upgrade', 'workspace_boundaries', 'dashboard_shell',
    'workspace_scheduler', 'schema_compatibility', 'keyserver_boundary',
    'private_sources', 'public_contracts', 'vault_boundaries', 'provider_secret_boundaries', 'cli_compatibility', 'dashboard_portability',
    'agent_plugin', 'audit_regressions', 'identity_map', 'zone_accounts', 'deployed_commit', 'scrub_incremental', 'leak_scan_incremental', 'tick_health', 'accounts', 'environments', 'credential_bindings', 'local_keys', 'trap_anchors', 'leak_coverage', 'backup_vault',
)
SUITES = LEGACY + BOUNDARY
HELPERS = ('tmp.py', 'source_reader.py', 'live_estate.py',
           'render_provider_health.py', 'render_dashboard.mjs', 'test_portable_mcp.py', 'run_portable.py',
           'dashboard_fixture.py', 'emitter_fixture.py', 'session_fixture.py', 'surface_fixture.py',
           'env_tab_check.js', 'action_outcome_check.mjs')
RUNTIME_DIRS = ('agent', 'collectors', 'dashboard', 'mcp', 'plugins', 'store', 'tools')
ROOT_FILES = (
    'activity.py', 'atomic.py', 'companion_faults.py', 'configuration.py',
    'degradations.py', 'estate.py', 'identity.py', 'observatory.py', 'paths.py',
    'identity_map.py', 'leak_register.py', 'private_io.py', 'proposals.py', 'runtime_identity.py', 'store_faults.py', 'survey.py', 'tick_health.py', 'workspace.py',
    'workspace_upgrade.py', 'backup_vault.py', 'tmp.py', 'fabric-agent.json', 'fabric-contract.lock.json', 'public-profile.json',
)
SKILL_FILES = (
    'skill/plugins/observatory-log/skills/handling-secrets/SKILL.md',
    'skill/plugins/observatory-log/skills/explaining-changes/SKILL.md',
    'skill/plugins/observatory-log/.claude-plugin/plugin.json',
    'skill/plugins/observatory-log/hooks/hooks.json',
)
NOT_RUN = (
    {'scope': 'live-provider-acceptance', 'status': 'NOT_RUN',
     'reason': 'Requires independently configured provider accounts and explicit opt-in; no credentials are inherited.'},
    {'scope': 'external-mcp-host-admission', 'status': 'NOT_RUN',
     'reason': 'The local stdio protocol is tested; acceptance by another host requires a separate configured fixture run.'},
    {'scope': 'live-secret-rotation-and-delivery', 'status': 'NOT_RUN',
     'reason': 'Synthetic credential boundaries are tested; real rotation/delivery must be independently authorized.'},
)


def copy_source(target: Path) -> None:
    """Only code, reviewed defaults and exact fixture dependencies are copied."""
    selected = {ROOT / name for name in ROOT_FILES + SKILL_FILES}
    selected |= {ROOT / 'tests' / ('test_' + name + '.py') for name in SUITES}
    selected |= {ROOT / 'tests' / name for name in HELPERS}
    selected.add(ROOT / 'plugins/README.md')
    selected.add(ROOT / 'fabric/FABRIC-CONFORMANCE.md')
    selected.add(ROOT / 'store/schema.sql')
    for folder in RUNTIME_DIRS:
        selected |= {p for p in (ROOT / folder).rglob('*')
                     if p.suffix in {'.py', '.sh', '.js', '.css', '.html', '.svg'}
                     and '__pycache__' not in p.parts}
    # The dashboard's catalogs and the vendored brand manifest are data it reads.
    for folder in ('defaults', 'plugins', 'fabric/fixtures', 'fabric/schemas', 'dashboard/locales', 'dashboard/brand'):
        selected |= set((ROOT / folder).glob('*.json'))
    for source in sorted(selected):
        if not source.is_file():
            # Optional root modules/skill docs are not needed by every release.
            if source.parent == ROOT / 'tests' or source == ROOT / 'plugins/README.md':
                raise FileNotFoundError(str(source.relative_to(ROOT)))
            continue
        if source.is_symlink():
            raise ValueError('Refusing source symlink: ' + str(source.relative_to(ROOT)))
        destination = target / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def runtime_environment() -> dict[str, str]:
    # Hosted CPython builds can require their shared-library search path.
    # Keep only loader configuration, never provider tokens or user config.
    return {name: os.environ[name] for name in
            ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH')
            if name in os.environ}


def clean_env(base: Path) -> dict[str, str]:
    user, temporary = base / 'user', base / 'tmp'
    user.mkdir(); temporary.mkdir()
    runtime = base / 'runtime'
    return {
        **runtime_environment(), 'PATH': os.environ.get('PATH', ''), 'HOME': str(user),
        'TMPDIR': str(temporary), 'LANG': 'C.UTF-8', 'LC_ALL': 'C',
        'PYTHONDONTWRITEBYTECODE': '1', 'OBSERVATORY_HOME': str(runtime),
        'OBSERVATORY_REGISTRY': str(runtime / 'registry'),
        'OBSERVATORY_DB': str(runtime / 'store/observatory.db'),
        'OBSERVATORY_STATE': str(runtime / 'store'),
        'OBSERVATORY_SCRATCH': str(runtime / 'store/raw'),
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    }


LEGACY_BOOTSTRAP = '''
import pathlib, runpy, shutil, sys
root=pathlib.Path.cwd()
sys.path.insert(0,str(root/'tests')); sys.path.insert(0,str(root))
from test_portable_mcp import setup
setup()
import paths
sys.argv=[str(root/'tests'/('test_'+sys.argv[1]+'.py'))]
runpy.run_path(sys.argv[0],run_name='__main__')
'''


def run_suite(name: str, base: Path, template: Path, timeout: int) -> dict:
    started = time.monotonic(); work = base / name; work.mkdir()
    checkout = work / 'source'; shutil.copytree(template, checkout)
    env = clean_env(work)
    command = ([sys.executable, '-c', LEGACY_BOOTSTRAP, name] if name in LEGACY else
               [sys.executable, 'tests/test_' + name + '.py'])
    process = subprocess.Popen(command, cwd=checkout, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output = process.communicate(timeout=timeout)[0]; code = process.returncode
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output = process.communicate()[0]; code = 124
    log = work / 'run.log'; log.write_text(output, encoding='utf-8'); log.chmod(0o600)
    result = {'file': 'tests/test_' + name + '.py',
              'status': 'PASS' if code == 0 else 'TIMEOUT' if code == 124 else 'FAIL',
              'exit_code': code, 'seconds': round(time.monotonic()-started, 2),
              'pass_assertions': len(re.findall(r'^\s*PASS\b',output,re.M)),
              'fail_assertions': len(re.findall(r'^\s*FAIL\b',output,re.M)),
              'skip_assertions': len(re.findall(r'^\s*SKIP\b',output,re.M)),
              'unittest_cases': sum(map(int,re.findall(r'Ran (\d+) tests? in',output)))}
    if code != 0:
        result['failure_tail'] = output[-6000:]
    result['log'] = name + '/run.log'
    print(result['status'] + ' ' + result['file'],file=sys.stderr,flush=True)
    return result


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',action='append',choices=SUITES,
                        help='Run a named subset; omitted runs the entire explicit set.')
    parser.add_argument('--jobs',type=int,default=4)
    parser.add_argument('--timeout',type=int,default=120,help='Seconds per suite.')
    parser.add_argument('--report-dir', type=Path, help='Write only synthetic suite logs and the summary to a new directory.')
    parser.add_argument('--keep',action='store_true',help='Keep private synthetic logs and source sandboxes.')
    args=parser.parse_args()
    if args.jobs < 1 or args.timeout < 1: parser.error('jobs and timeout must be positive')
    names=tuple(dict.fromkeys(args.suite or SUITES))
    base=Path(tempfile.mkdtemp(prefix='observatory-portable-')).resolve();base.chmod(0o700)
    try:
        template=base/'template';template.mkdir();copy_source(template)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results=list(pool.map(lambda name:run_suite(name,base,template,args.timeout),names))
        report={'schema_version':1,'coverage':'synthetic-offline',
                'status':'PASS' if all(row['status']=='PASS' for row in results) else 'FAIL',
                'suites':results,'not_run':list(NOT_RUN),
                'suite_count':len(results),'python':sys.version.split()[0]}
        if args.report_dir:
            args.report_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            for row in results:
                target=args.report_dir / row['log']; target.parent.mkdir(mode=0o700)
                shutil.copyfile(base / row['log'], target); target.chmod(0o600)
            summary=args.report_dir / 'summary.json'
            summary.write_text(json.dumps(report, indent=2) + '\n'); summary.chmod(0o600)
        if args.keep:report['private_logs']=str(base)
        print(json.dumps(report,indent=2))
        return 0 if report['status']=='PASS' else 1
    except Exception as exc:
        print(json.dumps({'schema_version':1,'status':'FAIL',
                          'error_type':type(exc).__name__,
                          'not_run':list(NOT_RUN)}))
        return 1
    finally:
        if not args.keep:shutil.rmtree(base,ignore_errors=True)


if __name__=='__main__':
    raise SystemExit(main())
