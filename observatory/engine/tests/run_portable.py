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
    # Ported suites, first batch: they run over the synthetic estate above.
    'absent_fields', 'agent_faults', 'api_listings', 'caller_identity', 'clone_sync',
    'companion', 'companion_hook', 'dark_domain_cost', 'docs_current', 'export_scope',
    'filesystem_scan', 'finding_rules', 'heroku', 'interpretation_contract', 'keyserver',
    'page_omission', 'plugin_reporting', 'projection_mirror', 'queue_order', 'rdap_keys',
    'recall', 'recount', 'release_cadence', 'remedy_fits', 'render_surface', 'review_queue',
    'serverd', 'stale_is_expected', 'store_modes', 'tick_standdown', 'trace_opens',
    'two_surfaces', 'work_tiles',

    # Merged from the parallel port streams.
    'agent', 'agent_queue', 'blank_page', 'budget_subject', 'dashboard_store', 'erasure_bytes', 'estate_history', 'footprint', 'index', 'key_shape', 'project_surface', 'projection', 'reclaimable_pressure', 'temporary_block', 'traps', 'write_surface',
)
BOUNDARY = (
    'workspace', 'workspace_upgrade', 'workspace_boundaries', 'dashboard_shell',
    'workspace_scheduler', 'schema_compatibility', 'keyserver_boundary',
    'private_sources', 'public_contracts', 'vault_boundaries', 'provider_secret_boundaries', 'cli_compatibility', 'handed_commands', 'dashboard_portability',
    'agent_plugin', 'audit_regressions', 'identity_map', 'zone_accounts', 'deployed_commit', 'scrub_incremental', 'leak_scan_incremental', 'tick_health', 'accounts', 'environments', 'credential_bindings', 'local_keys', 'trap_anchors', 'leak_coverage', 'backup_vault', 'organizations', 'machine', 'local_folders', 'config_locations', 'dashboard_stop',
    'engine_update', 'workspace_profile',
    # Ported suites, first batch: each builds its own temporary workspace.
    'delta_fold', 'mcp_inventory', 'openrouter_keys', 'scan_ids', 'secrets', 'signature',
    'skill_check', 'use_secret', 'validator_rules',

    # Merged from the parallel port streams.
    'cause_and_symptom', 'collector_state', 'corroboration', 'credentials', 'curation_paths', 'degradations', 'domain_probe', 'google', 'handoff', 'install_key', 'ledger_export', 'mechanical_confidence', 'notification', 'portfolio_review', 'project_secrets', 'publish_contract', 'recovery', 'reveals', 'session_start', 'tick', 'tick_failures', 'trap_efficacy', 'trap_map', 'unpublished_work', 'wire_survives', 'witness',
    'fabric_service', 'machine_mcp_inventory', 'interop', 'assistant',
    'git_hardening',
)
# Suites and helpers ported by the second port stream.
BOUNDARY += (
    'activity',
    'at_stake',
    'backup_store',
    'checkout_subject',
    'companion_faults',
    'conformance_receipt',
    'dead_data',
    'delivery',
    'emit_purity',
    'env_inventory',
    'estate_surfaces',
    'fixture_sweep',
    'foreign_vocabulary',
    'freshness',
    'gate_purity',
    'gate_skips',
    'git_locale',
    'horizon',
    'indexer_load',
    'interpretation',
    'key',
    'ledger_pointer',
    'lost_projects',
    'merge_membership',
    'no_silent_truncation',
    'pipeline',
    'plugin_pair',
    'policy_residue',
    'registry_shape',
    'rollup_sessions',
    'scrub',
    'search_path',
    'shared_ancestry',
    'side_effect_attribution',
    'skill',
    'skip_site_kinds',
    'stale_collapse',
    'store_faults',
    'store_integrity',
    'tick_repo',
    'unobservable',
)
# The step list's own guard: every file it names exists, every suite it names runs.
BOUNDARY += ('step_references',)
# Absent, not applicable, slow: told apart from broken.
BOUNDARY += ('honest_absence',)
# One credential-shape heuristic for every door that judges typed text.
BOUNDARY += ('credential_shape',)
# PROJECT is the folder name; the registry id names the same folder.
BOUNDARY += ('vault_project',)
# The product lifecycle contract: bounded steps, idle server, rotated logs, the watch.
BOUNDARY += ('lifecycle', 'lifecycle_watch')
# Agent memory: checkpoints, one executor per workflow, handoff packs.
BOUNDARY += ('workflow_memory',)
# The Agents page: workflows, handoffs and sessions, rendered.
BOUNDARY += ('agents_page',)
# Agents' credentials: the findings that report keys outside the vault.
BOUNDARY += ('agent_secrets',)
SUITES = LEGACY + BOUNDARY
HELPERS = ('tmp.py', 'source_reader.py', 'live_estate.py',
           'render_provider_health.py', 'render_dashboard.mjs', 'test_portable_mcp.py', 'run_portable.py',
           'dashboard_fixture.py', 'emitter_fixture.py', 'session_fixture.py', 'surface_fixture.py',
           'env_tab_check.js', 'action_outcome_check.mjs', 'focus_check.js',
           'own_project.py', 'tick_reader.py', 'validator_fixture.py', 'check_service.py', 'fabric_interop.py', 'mcp_config_fixture.py',
    # Merged from the parallel port streams.
    'concurrency.py', 'merge_fixture.py', 'probe_fixture.py',
)
# Suites and helpers ported by the second port stream.
HELPERS += (
    'watched_repo.py',
)
RUNTIME_DIRS = ('agent', 'collectors', 'dashboard', 'mcp', 'plugins', 'store', 'tools')
ROOT_FILES = (
    'activity.py', 'atomic.py', 'companion_faults.py', 'configuration.py',
    'degradations.py', 'estate.py', 'identity.py', 'observatory.py', 'paths.py',
    'identity_map.py', 'leak_register.py', 'credential_shape.py', 'memory_redact.py', 'agents_view.py', 'vault_project.py', 'private_io.py', 'proposals.py', 'runtime_identity.py', 'store_faults.py', 'survey.py', 'tick_health.py', 'workspace.py',
    'workspace_upgrade.py', 'backup_vault.py', 'organizations.py', 'machine_view.py', 'engine_update.py', 'workspace_profile.py', 'tmp.py',
    'log_policy.py', 'code_freshness.py',
    'fabric_service.py', 'mcp_inventory.py', 'interop.py', 'slow_command.py', 'safe_git.py', 'jobs.py', 'service_identity.py', 'service_health.py', 'service_events.py', 'fabric-agent.json', 'fabric-contract.lock.json', 'public-profile.json',
    # The tested dependency set: `require_runtime` and `full update` name or use it.
    'requirements-full.lock',
)
SKILL_FILES = (
    'skill/plugins/observatory-log/skills/handling-secrets/SKILL.md',
    'skill/plugins/observatory-log/skills/explaining-changes/SKILL.md',
    'skill/plugins/observatory-log/skills/tracking-resources/SKILL.md',
    'skill/plugins/observatory-log/.claude-plugin/plugin.json',
    'skill/plugins/observatory-log/hooks/hooks.json',
    # The companion suites drive the hook scripts themselves.
    'skill/plugins/observatory-log/hooks/record-turn.sh',
    'skill/plugins/observatory-log/hooks/ask-why.py',

    # Merged from the parallel port streams.
    'skill/plugins/observatory-log/hooks/session-start.sh',
)
# Suites and helpers ported by the second port stream.
SKILL_FILES += (
    'skill/.claude-plugin/marketplace.json',
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
    selected.add(ROOT / 'fabric/interop-schemas/README.md')
    selected.add(ROOT / 'store/schema.sql')
    # The documentation suite checks the engine's own shipped docs.
    selected |= set((ROOT / 'docs').glob('*.md'))
    for folder in RUNTIME_DIRS:
        selected |= {p for p in (ROOT / folder).rglob('*')
                     if p.suffix in {'.py', '.sh', '.js', '.css', '.html', '.svg'}
                     and '__pycache__' not in p.parts}
    # The dashboard's catalogs and the vendored brand manifest are data it reads.
    for folder in ('defaults', 'plugins', 'fabric/fixtures', 'fabric/schemas', 'fabric/service-schemas',
                   'fabric/interop-schemas',
                   'dashboard/locales', 'dashboard/brand'):
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


def parser(prog: str | None = None) -> argparse.ArgumentParser:
    """`prog` is the command a person typed (`project-observatory full check`);
    the dispatcher passes it in OBSERVATORY_PROG, and the gate's parse-only check
    (`observatory.refusal`) passes it directly."""
    ap=argparse.ArgumentParser(prog=prog, description=__doc__)
    ap.add_argument('--suite',action='append',choices=SUITES,metavar='NAME',
                    help='Run a named subset (repeatable); omitted runs the entire explicit set.')
    ap.add_argument('--jobs',type=int,default=4)
    ap.add_argument('--timeout',type=int,default=120,help='Seconds per suite.')
    ap.add_argument('--report-dir', type=Path, help='Write only synthetic suite logs and the summary to a new directory.')
    ap.add_argument('--keep',action='store_true',help='Keep private synthetic logs and source sandboxes.')
    return ap


def main() -> int:
    ap=parser(os.environ.get('OBSERVATORY_PROG') or None)
    args=ap.parse_args()
    if args.jobs < 1 or args.timeout < 1: ap.error('jobs and timeout must be positive')
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
