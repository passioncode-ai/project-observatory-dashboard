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
import contextlib
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


def _load_git_env():
    """The sibling `git_env.py`, by its path: this runner is itself loaded by path
    (`observatory.py check`, the root tests), where `tests/` is not on `sys.path`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('observatory_runner_git_env', Path(__file__).with_name('git_env.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


git_env = _load_git_env()

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
    'open_unlinked',
    'heroku_scoped', 'holder_capture',
    'workspace', 'workspace_upgrade', 'workspace_boundaries', 'dashboard_shell',
    'workspace_scheduler', 'schema_compatibility', 'keyserver_boundary',
    'private_sources', 'public_contracts', 'vault_boundaries', 'provider_secret_boundaries', 'cli_compatibility', 'handed_commands', 'dashboard_portability',
    'agent_plugin', 'audit_regressions', 'identity_map', 'zone_accounts', 'deployed_commit', 'scrub_incremental', 'leak_scan_incremental', 'tick_health', 'accounts', 'environments', 'credential_bindings', 'local_keys', 'trap_anchors', 'leak_coverage', 'backup_vault', 'organizations', 'machine', 'local_folders', 'config_locations', 'dashboard_stop',
    'engine_update', 'workspace_profile', 'release_signature',
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
# docs/design/WINDOWS-LINUX.md W1: one lock API on every OS.
BOUNDARY += ('oslocks',)
# No test's git leaves a detached gc or maintenance behind (v0.27.0's refused tag).
BOUNDARY += ('git_env',)
# docs/design/WINDOWS-LINUX.md W2: process groups on every OS.
BOUNDARY += ('osproc',)
# docs/design/WINDOWS-LINUX.md W2b: who the engine runs as, on every OS.
BOUNDARY += ('osprivacy',)
BOUNDARY += ('osschedule',)
BOUNDARY += ('harness',)
# OBS-55: the meter and the delegation verifier (contract DEC-0039).
BOUNDARY += ('meter', 'meter_wire', 'spend')
# OBS-162: the verifier as contract DEC-0045 sealed it, over the contract's own fixtures.
BOUNDARY += ('delegation_sealed',)
# docs/design/WINDOWS-LINUX.md W3: a whole tick, through tools/tick.py.
BOUNDARY += ('tick_progress', 'tick_py', 'tick_findings', 'merge_input_age', 'machine_findings')
# OBS-52: every collector a turn, none past its share of the ceiling.
BOUNDARY += ('tick_budget', 'degraded_persistence')
# ENG-003: the agent's deadline reaches providers.complete().
BOUNDARY += ('provider_deadline',)
# One credential-shape heuristic for every door that judges typed text.
BOUNDARY += ('credential_shape',)
# PROJECT is the folder name; the registry id names the same folder.
BOUNDARY += ('vault_project',)
# The product lifecycle contract: bounded steps, idle server, rotated logs, the watch.
BOUNDARY += ('lifecycle', 'lifecycle_watch')
# Agent memory: checkpoints, one executor per workflow, handoff packs.
BOUNDARY += ('workflow_memory',)
# One project under many names: curated aliases, known_as edges, project.resolve.
BOUNDARY += ('identifiers',)
# The Agents page: workflows, handoffs and sessions, rendered.
BOUNDARY += ('agents_page',)
# Agents' credentials: the findings that report keys outside the vault.
BOUNDARY += ('agent_secrets',)
# The agent-memory evaluation set (tools/memory_eval.py).
BOUNDARY += ('memory_eval',)
BOUNDARY += ('textkeys',)
# The header door: a vault value for one MCP server's headersHelper, and its binding.
BOUNDARY += ('secret_header',)
# The agent-network node's foundation (RM-21 P5, OBS-107): RFC 8785, ES256, fabric-relay/0.1 over the contract vectors.
BOUNDARY += ('node_foundation',)
# The node's relay session (OBS-108): a stdlib WebSocket and RelaySession against a loopback relay stub.
BOUNDARY += ('node_session',)
# The node's estate record, passkey assertions, revocation delta and heartbeat (OBS-109).
BOUNDARY += ('node_estate', 'node_revocation')
# The node's sealed secret envelope (OBS-113).
BOUNDARY += ('node_envelope',)
# `project-observatory full node`: this machine's device keys from a terminal (OBS-114).
BOUNDARY += ('node_cli',)
# enroll-token/1, the headless join (DEC-0084): verify, single-use redemption, the token read only from stdin or a file.
BOUNDARY += ('node_enroll', 'node_pairing')
# PB-163: the machine's SSH keys; the private key is never shown or moved.
BOUNDARY += ('ssh_keys',)
# access-bindings/1: who may do what with agent memory, from the transport (PB-137 N-007).
BOUNDARY += ('access_binding',)
# ENG-018: a manifest never pins a tag its release does not have.
BOUNDARY += ('manifest_urls',)
# Vector namespaces: one index per pinned model identity, legacy quarantined (PB-137 N-005).
BOUNDARY += ('vector_namespaces',)
# The SDK properties the HTTP memory service relies on (PB-137 N-015).
BOUNDARY += ('http_transport_pin',)
# embedding-policy/1: which memory text may leave for a remote embedding model (PB-137 N-002).
BOUNDARY += ('embedding_policy',)
# embedding-policy/1 enforced at the provider, the indexer, the query path and the CLI (N-003).
BOUNDARY += ('embedding_enforcement',)
# The contract pin and the profile lock, each under its own name, read through one module (OBS-196).
BOUNDARY += ('contract_lock',)
# access-bindings/1 enforced at every business entry point (PB-137 N-008).
BOUNDARY += ('memory_access', 'memory_freshness', 'checkout_inventory', 'checkout_state', 'cache_coverage', 'coverage_view')
# Scope and validity filtered before the candidate window (PB-137 N-009).
BOUNDARY += ('search_scope',)
# A lesson as its own record kind, and search filtered by kind before the window (M-7).
BOUNDARY += ('lesson', 'search')
# Retrieval receipts, scoped explain, redaction on the way out (PB-137 N-012).
BOUNDARY += ('retrieval_audit',)
# Checkpoint prose with field/chunk provenance; replays resurrect nothing (PB-137 N-011).
BOUNDARY += ('checkpoint_chunks',)
# The loopback HTTP memory service, end to end (PB-137 N-016).
BOUNDARY += ('memory_http',)
# memory/0.1 served under its own names (PB-137 N-025).
BOUNDARY += ('memory_wire',)
# Forget with a receipt: withdrawal, erasure, retained copies named (PB-137 N-013).
BOUNDARY += ('forget_receipt',)
# Facts that expire; lessons that cite their evidence (PB-137 N-014).
BOUNDARY += ('facts_learning',)
# Updates that arrive by themselves; data that survives a reinstall (docs/runs/2026-10-05-auto-update).
BOUNDARY += ('maintenance',)
# The app step alone, which the Mac app's Restart to update runs after it quits (LC-16).
BOUNDARY += ('maintain_app',)
# The disk guard: pressure, the lsof rule, each class; synthetic roots only (2026-10-11).
BOUNDARY += ('disk_guard',)
# The DigitalOcean door against a fake `doctl` and a scratch vault (PLN-142).
BOUNDARY += ('digitalocean_door', 'digitalocean_safety')
# The Heroku door against a fake Platform API: authorizations and team membership (OBS-156).
BOUNDARY += ('heroku_door',)
# The agents registry: Fabric descriptors, launchd and the MCP inventory -> registry/agents.json (OBS-83).
BOUNDARY += ('scan_agents',)
# The request for a why (M-8): once per record, with the next prompt, measured; and the
# canary that it reaches the model, read from a synthetic transcript.
BOUNDARY += ('ask_why', 'ask_why_reach')
# OBS-160: the Fabric contract's folder-attribution vectors (DEC-0089) and the pull from Fabric's hub.
BOUNDARY += ('folder_attribution', 'fabric_attribution')
# The machine bundle and its restore onto an empty home (OBS-157).
BOUNDARY += ('machine_backup',)
# A session MCP server moving itself onto the installed release over real pipes (OBS-159).
BOUNDARY += ('session_reload',)
# OBS-161, OBS-199, OBS-201, OBS-204: organization enrollment against the fake organization server. The suite
# shipped with OBS-161 but was never listed here, so `full check` never ran it (OBS-201); RUN_ELSEWHERE below and
# tests/test_runner_evidence.py now refuse an engine test file that no runner names.
BOUNDARY += ('node_org',)
# OBS-192: the node's harness summary (node-summary/1, contract DEC-0101) and its drift, over the contract's vectors.
BOUNDARY += ('node_summary',)
# OBS-79 step L1: the lifecycle broker (registry, authority, durable operations, receipts, the effect journal,
# circuits, the restart budget, idle and takeover rules) against fake adapters.
BOUNDARY += ('node_lifecycle_broker',)
# OBS-79 step L2: the macOS adapter (signatures and requirements, trusted paths, process groups, the verification cache,
# the product probes, the helper build) against injected tools; elsewhere it answers `unsupported_platform`.
BOUNDARY += ('node_lifecycle_macos',)
# OBS-79 step L3: the six MCP tools on the local server behind access bindings, the event log and the operator's
# `full lifecycle` verbs, against fake adapters and an in-process server on an ephemeral port.
BOUNDARY += ('node_lifecycle_service',)
SUITES = LEGACY + BOUNDARY
#: Engine test files this runner does not run, each with what does. Every other `tests/test_*.py` is a suite or a
#: helper above (tests/test_runner_evidence.py), so a new suite cannot ship without running anywhere.
RUN_ELSEWHERE = {
    'j1_evidence': 'the root suite tests/test_j1_evidence.py runs it against the real runner',
}
HELPERS = ('tmp.py', 'pgp_fixture.py', 'source_reader.py', 'live_estate.py',
           'render_provider_health.py', 'render_dashboard.mjs', 'test_portable_mcp.py', 'run_portable.py',
           'dashboard_fixture.py', 'emitter_fixture.py', 'session_fixture.py', 'surface_fixture.py',
           'env_tab_check.js', 'action_outcome_check.mjs', 'focus_check.js',
           'own_project.py', 'tick_reader.py', 'validator_fixture.py', 'check_service.py', 'fabric_interop.py', 'mcp_config_fixture.py',
    # Merged from the parallel port streams.
    'concurrency.py', 'merge_fixture.py', 'probe_fixture.py',
    # Every test's git environment: configuration shut out, no background maintenance.
    'git_env.py',
)
# Suites and helpers ported by the second port stream.
HELPERS += (
    'watched_repo.py',
    # The accepted access-bindings/1 decision cases (PB-137 N-007).
    'access_binding_cases.json',
    # The accepted embedding-policy/1 decision cases (PB-137 N-002).
    'embedding_policy_cases.json',
    # Grants a synthetic consent so vector-half suites still reach the provider stub.
    'embedding_consent.py',
    # The page script's re-translation, for the checks that a page built in one
    # language reads in the other (L10N-01).
    'relocalize.py',
)
# task-pipeline's lessons.py emit-observatory output, copied: observatory_lesson's arguments (M-7).
HELPERS += ('lesson_emitter_fixture.json',)
# The synthetic transcript the reach canary reads (M-8).
HELPERS += ('ask_why_transcript.json',)
# The DigitalOcean door's account half and collector (OBS-155): run from `test_digitalocean_door.py`,
# the `test-digitalocean` step, so one step covers one door.
HELPERS += ('test_digitalocean_account.py',)
RUNTIME_DIRS = ('agent', 'collectors', 'dashboard', 'mcp', 'node', 'plugins', 'store', 'tools')
ROOT_FILES = (
    'activity.py', 'atomic.py', 'companion_faults.py', 'configuration.py',
    'degradations.py', 'estate.py', 'identity.py', 'observatory.py', 'paths.py', 'tracked.py',
    'identity_map.py', 'identifiers.py', 'folder_attribution.py', 'fabric_attribution.py', 'fabric_hub.py', 'leak_register.py', 'credential_shape.py', 'memory_redact.py', 'agents_view.py', 'textkeys.py', 'vault_project.py', 'private_io.py', 'proposals.py', 'runtime_identity.py', 'store_faults.py', 'survey.py', 'tick_health.py', 'tick_progress.py', 'tick_budget.py', 'workspace.py',
    'workspace_upgrade.py', 'backup_vault.py', 'organizations.py', 'machine_view.py', 'memory_snapshot.py', 'checkout_inventory.py', 'checkout_state.py', 'checkout_graph.py', 'cache_coverage.py', 'open_unlinked.py', 'coverage_view.py', 'engine_update.py', 'workspace_profile.py', 'tmp.py',
    'log_policy.py', 'code_freshness.py', 'session_reload.py', 'access_binding.py', 'embedding_policy.py',
    'memory_access.py', 'retrieval_audit.py',
    # The companion's request for a why: journal, measurement, reach (M-8).
    'why_asks.py',
    # Updates that arrive by themselves and data that survives a reinstall (2026-10-05).
    'maintenance.py', 'app_update.py', 'update_events.py',
    # The machine's agent setup in one encrypted bundle, and its restore (OBS-157).
    'machine_backup.py',
    # The organization's signature on a release, checked before any update (audit A04).
    'release_signature.py',
    # The meter and the delegation verifier (OBS-55).
    'delegation.py', 'meter.py',
    # The one small-order Ed25519 key check (contract DEC-0045, CT-29; OBS-162).
    'ed25519_order.py',
    'fabric_service.py', 'mcp_inventory.py', 'interop.py', 'slow_command.py', 'step_budget.py', 'oslocks.py', 'osproc.py', 'osprivacy.py', 'holders.py', 'cleanup_lock.py', 'cleanup_safety.py', 'osschedule.py', 'safe_git.py', 'jobs.py', 'service_identity.py', 'service_health.py', 'service_events.py', 'fabric-agent.json', 'fabric-contract.lock.json', 'observatory-profile.lock.json', 'contract_lock.py', 'public-profile.json',
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
    'skill/plugins/observatory-log/hooks/prompt-why.py',

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


def source_files() -> list[Path]:
    """Only code, reviewed defaults and exact fixture dependencies are copied."""
    selected = {ROOT / name for name in ROOT_FILES + SKILL_FILES}
    selected |= {ROOT / 'tests' / ('test_' + name + '.py') for name in SUITES}
    selected |= {ROOT / 'tests' / name for name in HELPERS}
    selected.add(ROOT / 'plugins/README.md')
    selected.add(ROOT / 'fabric/FABRIC-CONFORMANCE.md')
    selected.add(ROOT / 'fabric/interop-schemas/README.md')
    selected.add(ROOT / 'fabric/service-schemas/README.md')   # its digests, and the commit the pin holds (OBS-205)
    selected.add(ROOT / 'fabric/memory-schemas/README.md')
    selected.add(ROOT / 'fabric/delegation-schemas/README.md')
    selected.add(ROOT / 'fabric/node-vectors/README.md')
    selected.add(ROOT / 'fabric/delegation-vectors/README.md')
    selected.add(ROOT / 'fabric/memory-provider.json')
    selected.add(ROOT / 'store/schema.sql')
    # The lifecycle broker's NSWorkspace helper, compiled from source by the macOS adapter's suite (OBS-79 L2).
    selected.add(ROOT / 'node/lifecycle/native_app.swift')
    # The agent-memory evaluation corpus (tools/memory_eval.py reads it).
    selected |= set((ROOT / 'tests' / 'memory_eval').glob('*.json'))
    # The documentation suite checks the engine's own shipped docs.
    selected |= set((ROOT / 'docs').glob('*.md'))
    for folder in RUNTIME_DIRS:
        selected |= {p for p in (ROOT / folder).rglob('*')
                     if p.suffix in {'.py', '.sh', '.js', '.css', '.html', '.svg'}
                     and '__pycache__' not in p.parts}
    # The dashboard's catalogs and the vendored brand manifest are data it reads.
    for folder in ('defaults', 'plugins', 'schemas', 'fabric/fixtures', 'fabric/schemas', 'fabric/service-schemas',
                   'fabric/interop-schemas', 'fabric/memory-schemas', 'fabric/delegation-schemas', 'fabric/delegation-vectors', 'fabric/node-vectors',
                   'fabric/attribution-vectors',
                   'dashboard/locales', 'dashboard/brand'):
        selected |= set((ROOT / folder).glob('*.json'))
    # The contract's estate fixtures the node's rules run over (fabric/node-vectors/README.md).
    selected |= set((ROOT / 'fabric/node-vectors/semantic').glob('*.json'))
    selected |= set((ROOT / 'fabric/node-vectors/device').glob('*.json'))     # OBS-161: the CT-65 device fixtures
    selected |= set((ROOT / 'fabric/node-vectors/summary').rglob('*.json'))    # OBS-192: node-summary/1 and the catalogue
    out = []
    for source in sorted(selected):
        if not source.is_file():
            # Optional root modules/skill docs are not needed by every release.
            if source.parent == ROOT / 'tests' or source == ROOT / 'plugins/README.md':
                raise FileNotFoundError(str(source.relative_to(ROOT)))
            continue
        out.append(source)
    return out


def copy_source(target: Path) -> None:
    for source in source_files():
        if source.is_symlink():
            raise ValueError('Refusing source symlink: ' + str(source.relative_to(ROOT)))
        destination = target / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


#: Windows' own locations, which a child cannot work without and which name no user or credential: without
#: SYSTEMROOT, OpenSSL finds no entropy source and every `cryptography` key generation fails ("entropy source strength
#: too weak"), which is how the organization suite failed through this runner on Windows (OBS-201).
WINDOWS_SYSTEM = ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT')


def runtime_environment() -> dict[str, str]:
    # Hosted CPython builds can require their shared-library search path.
    # Keep only loader configuration, never provider tokens or user config.
    names = ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH')
    if os.name == 'nt':
        names += WINDOWS_SYSTEM
    return {name: os.environ[name] for name in names if name in os.environ}


def clean_env(base: Path) -> dict[str, str]:
    user, temporary = base / 'user', base / 'tmp'
    user.mkdir(); temporary.mkdir()
    runtime = base / 'runtime'
    # git_env: the operator's system and global git configuration shut out, and no background
    # `gc --auto` or maintenance that outlives a git command and races the suite's cleanup.
    return git_env.git_env({
        **runtime_environment(), 'PATH': os.environ.get('PATH', ''), 'HOME': str(user),
        'TMPDIR': str(temporary), 'LANG': 'C.UTF-8', 'LC_ALL': 'C',
        'PYTHONDONTWRITEBYTECODE': '1', 'OBSERVATORY_HOME': str(runtime),
        'OBSERVATORY_REGISTRY': str(runtime / 'registry'),
        'OBSERVATORY_DB': str(runtime / 'store/observatory.db'),
        'OBSERVATORY_STATE': str(runtime / 'store'),
        'OBSERVATORY_SCRATCH': str(runtime / 'store/raw'),
        # No suite may schedule a launchd/systemd job or write a Keychain item on the machine
        # running it (audit A07). HOME is temporary, so the backups root is too.
        'OBSERVATORY_SYSTEM_SETUP': '0',
        # The shared update log (update_events.py, LC-16) goes beside the suite, never into
        # ~/Library/Logs or ~/.local/state of the machine running it.
        'OBSERVATORY_PRODUCT_LOG_DIR': str(base / 'product-logs'),
    })


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


#: Suites whose honest run takes longer than the default per-suite timeout, with the
#: seconds they are given at least (measured 2026-10-06 at --jobs 4: maintenance 217 s
#: with its pty and planted-writer cases, conformance_receipt 129 s for its full check;
#: backup_vault 52–104 s alone, past 120 s under the gate's --jobs 6; workspace, interop and
#: indexer_load passed 120 s under the 0.19.0 gate's load; api_listings, which runs the whole
#: tools/check_paths.py over the tree since 0.20.0, took 14 s alone and passed 120 s at a load
#: average near 150).
#: A larger --timeout still wins; a smaller one never cuts these short.
SUITE_SECONDS = {'credential_doors': 600, 'maintenance': 480, 'tick_py': 300, 'conformance_receipt': 360, 'backup_vault': 300, 'dashboard_render': 600,
                 'workspace': 300, 'interop': 300, 'indexer_load': 300, 'api_listings': 300,
                 # 128 cases, each driving the real hooks and git: ~70 s idle, ~310 s at a
                 # load average of 300 (OBS-135 tracks making it cheaper).
                 'ask_why': 600,
                 # 36 cases with real git checkouts, restores and three killed subprocesses:
                 # 71 s idle (2026-10-19), so twice the default leaves room for a loaded runner.
                 'machine_backup': 300}


def suite_timeout(name: str, timeout: int) -> int:
    return max(timeout, SUITE_SECONDS.get(name, 0))


# Bootstrap groups above are not test protocols: some LEGACY suites use unittest.
# New suites default to unittest, requiring a complete summary and at least one
# executed case. These audited compatibility suites report PASS/FAIL/SKIP lines,
# including direct assertions and the ENV JavaScript helper. Silence is never a
# compatibility exemption. Add a reporter here only with an exercised contract.
ASSERTION_LINE_SUITES = frozenset((
    'ledger', 'time', 'provider_boundary', 'provider_health', 'credential_doors', 'vault',
    'plugins', 'wire_contract', 'metric_series', 'analytics_plugins', 'mcp_wire', 'wire_inputs',
    'retention', 'provenance', 'events', 'fingerprints', 'pages', 'dashboard_render',
    'atomic_writers', 'ack', 'rollup', 'runtime_identity', 'remote_env', 'project_identity',
    'sessions', 'env_page', 'metric_labels', 'hosting_groups', 'action_outcomes', 'absent_fields',
    'agent_faults', 'api_listings', 'caller_identity', 'clone_sync', 'companion', 'companion_hook',
    'dark_domain_cost', 'docs_current', 'export_scope', 'filesystem_scan', 'finding_rules',
    'heroku', 'interpretation_contract', 'keyserver', 'page_omission', 'plugin_reporting',
    'projection_mirror', 'queue_order', 'rdap_keys', 'recall', 'recount', 'release_cadence',
    'remedy_fits', 'render_surface', 'review_queue', 'serverd', 'stale_is_expected', 'store_modes',
    'tick_standdown', 'trace_opens', 'two_surfaces', 'work_tiles', 'agent', 'agent_queue',
    'blank_page', 'budget_subject', 'dashboard_store', 'erasure_bytes', 'estate_history',
    'footprint', 'index', 'key_shape', 'project_surface', 'projection', 'reclaimable_pressure',
    'temporary_block', 'traps', 'write_surface', 'local_folders', 'delta_fold', 'mcp_inventory',
    'openrouter_keys', 'scan_ids', 'secrets', 'signature', 'skill_check', 'use_secret',
    'validator_rules', 'cause_and_symptom', 'collector_state', 'corroboration', 'credentials',
    'curation_paths', 'degradations', 'domain_probe', 'google', 'handoff', 'install_key',
    'ledger_export', 'mechanical_confidence', 'notification', 'portfolio_review', 'project_secrets',
    'publish_contract', 'recovery', 'reveals', 'session_start', 'tick', 'tick_failures',
    'trap_efficacy', 'trap_map', 'unpublished_work', 'wire_survives', 'witness', 'activity',
    'at_stake', 'backup_store', 'checkout_subject', 'companion_faults', 'conformance_receipt',
    'dead_data', 'delivery', 'emit_purity', 'env_inventory', 'estate_surfaces', 'fixture_sweep',
    'foreign_vocabulary', 'freshness', 'gate_purity', 'gate_skips', 'git_locale', 'horizon',
    'indexer_load', 'interpretation', 'key', 'ledger_pointer', 'lost_projects', 'merge_membership',
    'no_silent_truncation', 'pipeline', 'plugin_pair', 'policy_residue', 'registry_shape',
    'rollup_sessions', 'scrub', 'search_path', 'shared_ancestry', 'side_effect_attribution',
    'skill', 'skip_site_kinds', 'stale_collapse', 'store_faults', 'store_integrity', 'tick_repo',
    'unobservable', 'step_references', 'honest_absence', 'identifiers', 'memory_eval', 'textkeys',
    'manifest_urls', 'digitalocean_door', 'heroku_door'
))
# Per-suite positive minimums may strengthen the default of one executed check.
# Skips never satisfy a minimum; all-skipped selections remain explicitly SKIP.
SUITE_MINIMUMS: dict[str, int] = {}


#: What a run keeps free under the temporary folder (OBS-167): on 2026-10-19 four parallel
#: runs wrote about 3.5 GB each and left a machine under 1 GB free, failing other suites with
#: ENOSPC. `--min-free-gb` changes it; 0 starts whatever the disk holds.
MIN_FREE_GB = 2
#: The refusal's exit code, the one `full update` uses for the same reason.
EXIT_NO_SPACE = 7


def space_needed(jobs: int, floor_gb: float) -> int:
    """Bytes a run needs free before it starts: the template plus one source copy per suite
    running at once (each sandbox goes when its suite ends, so at most `jobs` exist), and
    the floor left over for the suites' own files and everything else on the machine."""
    source = sum(f.stat().st_size for f in source_files())
    return source * (jobs + 1) + int(floor_gb * 1024 ** 3)


def discard(path: Path) -> None:
    """Remove a sandbox completely, even what a suite made read-only."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for directory, children, _files in os.walk(path):
        for child in children:
            inner = os.path.join(directory, child)
            if not os.path.islink(inner):
                with contextlib.suppress(OSError):
                    os.chmod(inner, 0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    shutil.rmtree(path, ignore_errors=True)


def discard_sandbox(work: Path) -> None:
    """Everything a suite was given and wrote, except its log (OBS-167). A run copied the
    source for every suite and removed none until the run ended: 273 copies, ~3.5 GB."""
    if not work.is_dir():
        return
    for child in work.iterdir():
        if child.name != 'run.log':
            discard(child)


def tree_bytes(root: Path) -> int:
    total = 0
    for directory, _children, files in os.walk(root):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(directory, name)).st_size
    return total


def run_suite(name: str, base: Path, template: Path, timeout: int, keep: bool = False) -> dict:
    """One suite in its sandbox. The receipt's `sandbox_bytes` is what the sandbox held when
    the suite ended (source copy included), so every run says which suites write the most."""
    work = base / name; work.mkdir()
    try:
        result = _run_suite(name, work, template, timeout)
        result['sandbox_bytes'] = tree_bytes(work)
        return result
    finally:
        if not keep:
            discard_sandbox(work)


def _run_suite(name: str, work: Path, template: Path, timeout: int) -> dict:
    timeout = suite_timeout(name, timeout)
    started = time.monotonic()
    checkout = work / 'source'; shutil.copytree(template, checkout)
    env = clean_env(work)
    command = ([sys.executable, '-c', LEGACY_BOOTSTRAP, name] if name in LEGACY else
               [sys.executable, 'tests/test_' + name + '.py'])
    process = subprocess.Popen(command, cwd=checkout, env=env, stdin=subprocess.DEVNULL,
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
              'unittest_cases': 0, 'unittest_skipped': 0}
    assess_evidence(name, output, result)
    if result['status'] == 'SKIP':
        # A suite that exits 0 having run nothing — every check printed SKIP, or every
        # unittest case was skipped (no `node` on PATH, audit A46) — did not pass.
        result['status'] = 'SKIP'
        reasons = re.findall(r'^\s*SKIP\s+(.+)$', output, re.M)
        result['skip_reason'] = reasons[0].strip() if reasons else 'every test case was skipped'
    if code != 0:
        result['failure_tail'] = output[-6000:]
    result['log'] = name + '/run.log'
    print(result['status'] + ' ' + result['file'],file=sys.stderr,flush=True)
    return result


# A counter mentioned in a log is not a unittest result. The final summary start
# must have a complete block; earlier failures remain binding across later runs.
UNITTEST_SUMMARY_START = re.compile(r'^Ran \d+ tests? in\b', re.M)
UNITTEST_SUMMARY = re.compile(
    r'^Ran (\d+) tests? in [0-9.eE+-]+s[ \t]*\r?\n[ \t]*\r?\n'
    r'(OK(?: \([^\r\n]*\))?|FAILED(?: \([^\r\n]*\))?)[ \t]*$', re.M)


def assess_evidence(name: str, output: str, result: dict) -> None:
    """Retain interpreter exit separately from observed test evidence.

    This is a reporting contract for trusted repository tests, not authentication:
    a script can forge a complete summary. It detects missing entry points, silent
    exits, incompatible/contradictory counts and reported failures, not adversarial
    test authors or complete assertion coverage.
    """
    protocol = 'assertion-lines' if name in ASSERTION_LINE_SUITES else 'unittest'
    minimum = SUITE_MINIMUMS.get(name, 1)
    summaries = list(UNITTEST_SUMMARY.finditer(output))
    starts = list(UNITTEST_SUMMARY_START.finditer(output))
    terminal = summaries[-1] if summaries else None
    incomplete_final = bool(starts and (terminal is None or starts[-1].start() != terminal.start()))
    reported_failure = any(summary.group(2).startswith('FAILED') for summary in summaries)
    if incomplete_final:
        terminal = None
    invalid_counter = False
    if terminal:
        result['unittest_cases'] = int(terminal.group(1))
        skip_fields = re.findall(r'\bskipped=([^,)]+)', terminal.group(2))
        invalid_counter = len(skip_fields) > 1 or any(not value.isdecimal() for value in skip_fields)
        result['unittest_skipped'] = int(skip_fields[0]) if skip_fields and not invalid_counter else 0
    executed = (result['pass_assertions'] + result['fail_assertions'] if protocol == 'assertion-lines'
                else result['unittest_cases'] - result['unittest_skipped'])
    skipped = result['skip_assertions'] if protocol == 'assertion-lines' else result['unittest_skipped']
    result.update(test_protocol=protocol, minimum_executed=minimum,
                  executed_checks=executed, skipped_checks=skipped)
    if result['exit_code'] != 0:
        return                              # preserve nonzero/timeout and the actual code
    if type(minimum) is not int or minimum < 1:
        result.update(status='ERROR', reason_code='invalid_evidence_contract',
                      reason='suite evidence minimum must be a positive integer')
    elif ((protocol == 'assertion-lines' and result['fail_assertions'])
          or (protocol == 'unittest' and reported_failure)):
        result.update(status='FAIL', reason_code='reported_test_failure',
                      reason='suite reported failed checks despite a successful interpreter exit')
    elif protocol == 'unittest' and (incomplete_final or invalid_counter
                                   or result['unittest_skipped'] > result['unittest_cases']):
        result.update(status='ERROR', reason_code='invalid_test_evidence',
                      reason='final summary is incomplete or its skipped count is invalid')
    elif nothing_ran(result):
        result['status'] = 'SKIP'
    elif executed < minimum:
        result.update(status='ERROR',
                      reason_code='no_test_evidence' if executed == 0 else 'insufficient_test_evidence',
                      reason='suite did not report the minimum executed checks for its test protocol')


def nothing_ran(result: dict) -> bool:
    """Every check of the declared protocol was explicitly skipped."""
    return result['executed_checks'] == 0 and result['skipped_checks'] > 0


def run_suite_recorded(name: str, base: Path, template: Path, timeout: int, keep: bool = False) -> dict:
    """Keep one receipt per selection even when sandbox setup or log I/O fails.

    Pool.map otherwise raises on the first exception and discards the other suites'
    completed evidence. Exception bodies may contain local paths, so retain only type.
    ERROR means completion could not be established, never a successful test run.
    """
    started = time.monotonic()
    try:
        return run_suite(name, base, template, timeout, keep=keep)
    except Exception as exc:
        result = {'file': 'tests/test_' + name + '.py', 'status': 'ERROR',
                  'exit_code': None, 'seconds': round(time.monotonic() - started, 2),
                  'error_type': type(exc).__name__,
                  'reason': 'suite setup, execution or evidence capture failed; completion unknown'}
        print('ERROR ' + result['file'], file=sys.stderr, flush=True)
        return result


def overall(results: list[dict]) -> str:
    """FAIL when any suite failed or timed out; SKIP when no suite ran anything; else PASS.

    A SKIP row is not a pass: it is named in `not_run` with its reason, and a selection
    in which nothing ran is never reported as PASS."""
    if any(row['status'] not in ('PASS', 'SKIP') for row in results):
        return 'FAIL'
    return 'PASS' if any(row['status'] == 'PASS' for row in results) else 'SKIP'


def not_run(results: list[dict], node: str | None) -> list[dict]:
    """The fixed out-of-scope list, every skipped suite, and the dashboard execution checks
    when `node` is missing (several suites then skip those checks and still run the rest)."""
    rows = list(NOT_RUN)
    rows += [{'scope': 'suite:' + row['file'], 'status': 'NOT_RUN', 'reason': row['skip_reason']}
             for row in results if row['status'] == 'SKIP']
    rows += [{'scope': 'suite:' + row['file'], 'status': 'NOT_RUN', 'reason': row['reason']}
             for row in results if row['status'] == 'ERROR']
    if node is None:
        rows.append({'scope': 'dashboard-execution', 'status': 'NOT_RUN',
                     'reason': 'node is not on PATH: checks that execute the built dashboard '
                               'pages printed SKIP (see each suite\'s skip_assertions)'})
    return rows


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
    ap.add_argument('--min-free-gb', type=float, default=MIN_FREE_GB, metavar='GB',
                    help=f'Refuse to start when the temporary folder would keep less than this free '
                         f'(default {MIN_FREE_GB}); 0 starts regardless.')
    return ap


def space_refusal(jobs: int, floor_gb: float) -> dict | None:
    """None when the run fits; otherwise the receipt's fields, every number in bytes."""
    if floor_gb == 0:
        return None
    where = tempfile.gettempdir()
    free, needed = shutil.disk_usage(where).free, space_needed(jobs, floor_gb)
    if free >= needed:
        return None
    gb = lambda n: f'{n / 1024 ** 3:.2f} GB'  # noqa: E731
    return {'free_bytes': free, 'needed_bytes': needed, 'where': where,
            'reason': (f'Not started: {free} bytes ({gb(free)}) free under the temporary folder {where}, '
                       f'under the {needed} bytes ({gb(needed)}) this run needs: one source copy for each '
                       f'of {jobs} suite(s) at a time plus the template, and {floor_gb:g} GB left free. '
                       f'Free space, lower --jobs, or pass --min-free-gb 0 to start anyway.')}


def main() -> int:
    ap=parser(os.environ.get('OBSERVATORY_PROG') or None)
    args=ap.parse_args()
    if args.jobs < 1 or args.timeout < 1: ap.error('jobs and timeout must be positive')
    if args.min_free_gb < 0: ap.error('--min-free-gb must be 0 or more')
    names=tuple(dict.fromkeys(args.suite or SUITES))
    try:
        refusal = space_refusal(min(args.jobs, len(names)), args.min_free_gb)
    except Exception as exc:
        # Sizing reads the source list, which raises for a missing suite file: a receipt, not a
        # traceback (review of #325).
        print(json.dumps({'schema_version':1,'status':'FAIL','error_type':type(exc).__name__,
                          'reason':'the free-space check could not size the run','not_run':list(NOT_RUN)}))
        return 1
    if refusal:
        print(refusal['reason'], file=sys.stderr, flush=True)
        print(json.dumps({'schema_version':1,'status':'FAIL','reason_code':'insufficient_disk',**refusal,
                          'suites':[],'suite_count':0,'not_run':list(NOT_RUN)},indent=2))
        return EXIT_NO_SPACE
    base=Path(tempfile.mkdtemp(prefix='observatory-portable-')).resolve();base.chmod(0o700)  # paths-check: allow — removed by the finally below unless --keep asks to keep it
    try:
        template=base/'template';template.mkdir();copy_source(template)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results=list(pool.map(lambda name:run_suite_recorded(name,base,template,args.timeout,args.keep),names))
        report={'schema_version':1,'coverage':'synthetic-offline',
                'status':overall(results),
                'suites':results,'not_run':not_run(results, shutil.which('node')),
                'suite_count':len(results),'python':sys.version.split()[0]}
        if args.report_dir:
            try:
                args.report_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                for row in results:
                    if 'log' not in row:
                        continue
                    target=args.report_dir / row['log']; target.parent.mkdir(mode=0o700)
                    shutil.copyfile(base / row['log'], target); target.chmod(0o600)
                summary=args.report_dir / 'summary.json'
                pending=args.report_dir / 'summary.json.tmp'
                # Publish last: failure to write or restrict the receipt must not
                # leave a final PASS artifact while stdout correctly says FAIL.
                pending.write_text(json.dumps(report, indent=2) + '\n'); pending.chmod(0o600)
                pending.replace(summary)
            except OSError as exc:
                # A full disk or refused destination must not erase the stdout receipt.
                # Files already copied are partial evidence, never a completed report.
                report['status'] = 'FAIL'
                report['report_error_type'] = type(exc).__name__
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
