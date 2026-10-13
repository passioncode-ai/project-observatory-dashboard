"""Metadata-only cache/runtime coverage; declarations are not lifecycle authority."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess  # tests assert that no subprocess entry point is called
import sys
import time
from datetime import datetime, timezone
import atomic
import oslocks
import osprivacy
import paths
import step_budget

ROOT = Path(__file__).resolve().parent
KINDS = {'cache', 'build', 'toolchain', 'simulator', 'vm'}
EXTRA_PATH = ('/opt/homebrew/bin', '/usr/local/bin', '~/.local/bin', '~/.cargo/bin', '~/go/bin')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def age(stamp):
    try:
        value = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
        return value if value >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def number(value):
    try: return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError: return False


def presence(path):
    # Check before any metadata access: another app's container is not ours to inspect.
    sys.path.insert(0, str(ROOT / 'collectors')) if str(ROOT / 'collectors') not in sys.path else None
    from scan_machine import protected_place
    stamp = now()
    base = {'attempted_at': stamp, 'measured_at': None, 'present': None, 'reason': None}
    if protected_place(path): return {**base, 'reason': 'privacy-guarded'}
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            return {**base, 'reason': 'symlink-not-probed'}
        mode = path.lstat().st_mode
        return {**base, 'measured_at': now(), 'present': True,
                'kind': 'directory' if stat.S_ISDIR(mode) else 'other'}
    except FileNotFoundError:
        return {**base, 'measured_at': now(), 'present': False}
    except OSError:
        return {**base, 'reason': 'metadata-unavailable'}


def tool_name(tool):
    return (isinstance(tool, str) and re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', tool)
            and not re.search(r'sk-|gh[pousr]_|token|password|secret|bearer|cookie|api.?key', tool, re.I))


def availability(tool):
    base = {'attempted_at': now(), 'measured_at': None, 'available': None, 'reason': None}
    if not tool_name(tool):
        return {**base, 'reason': 'invalid-tool-declaration'}
    try:
        found = shutil.which(tool)
        if not found:
            found = next((str(p) for parent in EXTRA_PATH if (p := Path(os.path.expanduser(parent)) / tool).is_file() and os.access(p, os.X_OK)), None)
        return {**base, 'measured_at': now(), 'available': bool(found), 'basis': 'executable-presence-only'}
    except OSError:
        return {**base, 'reason': 'tool-presence-unavailable'}


def _collect(out, *, seconds=3, max_rows=128):
    started = now(); end = time.monotonic() + min(10, max(0, seconds), max(0, step_budget.remaining()))
    max_rows = max(0, min(128, int(max_rows)))
    degraded, sources = [], []
    def error(source, reason): degraded.append({'source': source, 'reason': reason})
    def read(path, source, *, missing_ok=False):
        attempted = now()
        try:
            # Refuse special files before opening; NONBLOCK also closes the FIFO
            # replacement race. An opened descriptor must still match the lstat.
            if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode): raise ValueError
            fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino): raise ValueError
                with os.fdopen(fd, 'rb', closefd=False) as stream: text = stream.read(1_048_577)
            finally: os.close(fd)
            if len(text) > 1_048_576: raise ValueError
            doc = json.loads(text)
            if not isinstance(doc, dict): raise ValueError
            sources.append({'source': source, 'attempted_at': attempted, 'measured_at': now(), 'readable': True})
            return doc
        except FileNotFoundError:
            if not missing_ok: error(source, 'source-missing')
        except (OSError, ValueError): error(source, 'source-unreadable')
        sources.append({'source': source, 'attempted_at': attempted, 'measured_at': None, 'readable': False})
        return {}
    def config(name):
        path = paths.config_file(name)
        try: path.lstat()
        except FileNotFoundError: path = ROOT / 'defaults' / name
        except OSError: pass
        return read(path, ('config/' if path.parent != ROOT / 'defaults' else 'defaults/') + name)
    machine, cleanup = config('machine.json'), config('cleanup.json')
    survey = read(paths.SCRATCH / 'machine.json', 'machine.json', missing_ok=True)
    previous = read(out, 'previous-cache-coverage', missing_ok=True)
    if previous and (previous.get('kind') != 'cache-coverage' or type(previous.get('schema_version')) is not int or previous.get('schema_version') != 1):
        error('previous-cache-coverage', 'invalid-receipt'); previous = {}
    def rows(doc, key, source):
        found = doc.get(key, [])
        if not isinstance(found, list): error(source, 'invalid-collection'); return []
        if len(found) > 128: error(source, 'row-limit')
        return found[:128]
    def prior_rows(key):
        valid = {}
        for row in rows(previous, key, 'previous-cache-coverage'):
            good = isinstance(row, dict) and isinstance(row.get('id'), str)
            if good and key == 'locations':
                declared = row.get('declaration_sources')
                good = (isinstance(declared, list) and bool(declared)
                        and all(v in ('machine.locations', 'cleanup.guard.cache_roots') for v in declared)
                        and isinstance(row.get('path'), str) and bool(row['path'])
                        and isinstance(row.get('category'), str) and row['category'] in KINDS)
            if good and key == 'commands':
                good = row.get('tool') is None or bool(tool_name(row.get('tool')))
            if not good:
                error('previous-cache-coverage', 'invalid-' + key + '-row'); continue
            valid[row['id']] = row
        return valid
    old_locations = prior_rows('locations')
    old_commands = prior_rows('commands')
    if survey and not isinstance(survey.get('disk'), dict): error('machine.json', 'invalid-disk-shape')
    disk = survey.get('disk') if isinstance(survey.get('disk'), dict) else {}
    sizes = {r['path']: r for r in rows(disk, 'locations', 'machine.json') if isinstance(r, dict) and isinstance(r.get('path'), str)}
    guard = cleanup.get('guard') if isinstance(cleanup.get('guard'), dict) else {}
    if not isinstance(cleanup.get('guard'), dict): error('cleanup-config', 'invalid-guard-shape')
    machine_valid = isinstance(machine.get('locations'), list)
    roots_valid = isinstance(guard.get('cache_roots'), list)
    commands_valid = isinstance(guard.get('commands'), list)
    if not machine_valid: error('machine-config', 'invalid-locations')
    if not roots_valid: error('cleanup-config', 'invalid-cache-roots')
    if not commands_valid: error('cleanup-config', 'invalid-commands')
    definitions = rows(machine, 'locations', 'machine-config')
    for path in rows(guard, 'cache_roots', 'cleanup-config'):
        definitions.append({'path': path, 'kind': 'cache', 'declaration': 'cleanup.guard.cache_roots'})
    locations, commands = {}, {}
    if len(definitions) > 128: error('machine-config', 'row-limit')
    for item in definitions[:128]:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path'] or len(item['path']) > 4096 or '\0' in item['path']:
            error('machine-config', 'invalid-location'); continue
        if not isinstance(item.get('kind'), str):
            error('machine-config', 'invalid-category'); continue
        if item['kind'] not in KINDS: continue
        raw = item['path']; key = 'location:' + hashlib.sha256(raw.encode()).hexdigest()[:24]
        if key in locations:
            locations[key]['declaration_sources'].append(item.get('declaration', 'machine.locations'))
            continue
        old = old_locations.get(key, {})
        locations[key] = {'id': key, 'path': raw, 'category': item['kind'], 'declaration_sources': [item.get('declaration', 'machine.locations')],
                          'attempted_at': old.get('attempted_at'), 'measured_at': old.get('measured_at'), 'stale': True, 'declaration_current': True,
                          'presence': old.get('presence') if isinstance(old.get('presence'), dict) else {'present': None, 'measured_at': None, 'reason': 'not-probed'},
                          'size': {'reported_gb': None, 'measured_at': None, 'complete': None, 'reason': 'not-measured'},
                          'allocated_bytes': None, 'reclaimable_bytes': None, 'owner': None, 'owner_reason': 'unverified',
                          'active_use': 'unknown', 'restore_cost': None, 'actionable': False,
                          'regeneration': {'declared_policy': item.get('reclaim') if item.get('reclaim') in ('regenerable', 'command', 'manual', 'history') else None, 'verified': False, 'evidence': None},
                          'manual_only': item.get('manual_only') is True}
        old_size = old.get('size') if isinstance(old.get('size'), dict) else {}
        if number(old_size.get('reported_gb')):
            locations[key]['size'] = {'reported_gb': old_size['reported_gb'], 'measured_at': old_size.get('measured_at'),
                                      'complete': None, 'basis': 'du-reported-rounded-gb', 'source': 'machine.json',
                                      'age_seconds': age(old_size.get('measured_at')), 'reason': 'carried-prior-observation'}
        prior_size = sizes.get(raw, {})
        if number(prior_size.get('gb')):
            stamp = prior_size.get('measured_at')
            locations[key]['size'] = {'reported_gb': prior_size['gb'], 'measured_at': stamp, 'complete': None,
                                      'basis': 'du-reported-rounded-gb', 'source': 'machine.json',
                                      'age_seconds': age(stamp), 'reason': 'completeness-unverified'}
    for key, old in old_locations.items():
        declared = old.get('declaration_sources', [])
        missing_owner_source = ((not machine_valid and 'machine.locations' in declared)
                                or (not roots_valid and 'cleanup.guard.cache_roots' in declared))
        if missing_owner_source and key not in locations:
            locations[key] = {**old, 'stale': True, 'declaration_current': False, 'actionable': False, 'reclaimable_bytes': None, 'owner': None}
    for index, item in enumerate(rows(guard, 'commands', 'cleanup-config')):
        argv = item.get('argv') if isinstance(item, dict) else None
        tool = argv[0] if isinstance(argv, list) and argv else None
        valid = tool_name(tool)
        key = f'command:{index:04d}'
        old = old_commands.get(key, {})
        commands[key] = {'id': key, 'command_reference': f'config/cleanup.json#/guard/commands/{index}',
                         'tool': tool if valid else None, 'attempted_at': old.get('attempted_at'),
                         'measured_at': old.get('measured_at'), 'stale': True, 'declaration_current': True,
                         'availability': old.get('availability') if old.get('tool') == tool and isinstance(old.get('availability'), dict) else {'available': None, 'reason': 'not-probed'},
                         'allocated_bytes': None, 'reclaimable_bytes': None, 'owner': None,
                         'active_use': 'unknown', 'daemon_running': None, 'regeneration_verified': False, 'actionable': False}
    if not commands_valid:
        commands = {key: {**row, 'stale': True, 'declaration_current': False, 'actionable': False, 'reclaimable_bytes': None} for key, row in old_commands.items()}
    catalog = {**locations, **commands}
    cursor = previous.get('cursor', {}).get('last_attempted') if isinstance(previous.get('cursor'), dict) else None
    order = sorted(catalog)
    if isinstance(cursor, str): order = [k for k in order if k > cursor] + [k for k in order if k <= cursor]
    system_areas = ['/Library/Caches', '/System/Library/Caches'] if sys.platform == 'darwin' else (['/var/cache'] if sys.platform.startswith('linux') else ['system-managed-cache'])
    uncovered = [{'path': p, 'category': 'system-managed', 'presence': {'present': None, 'measured_at': None, 'reason': 'not-probed'},
                  'owner': None, 'owner_reason': 'unverified', 'allocated_bytes': None, 'reclaimable_bytes': None,
                  'actionable': False, 'reason': 'outside-configured-root-metadata-coverage'} for p in system_areas]
    def save():
        doc = {'schema_version': 1, 'kind': 'cache-coverage', 'attempted_at': started,
               'scope': 'configured-root-metadata-only', 'coverage_complete': False,
               'partial': True, 'measurement_complete': not degraded and all(not r['stale'] for r in catalog.values()),
               'locations': list(locations.values()), 'commands': list(commands.values()), 'uncovered': uncovered,
               'sources': sources, 'degraded': degraded, 'cursor': {'last_attempted': cursor},
               'limits': {'seconds': seconds, 'max_rows': max_rows}, 'commands_executed': 0, 'recursive_walks': 0}
        atomic.write_json(out, doc); return doc
    for key in order[:max_rows]:
        if time.monotonic() >= end: break
        row = catalog[key]; cursor = key; save()
        if not row.get('declaration_current'): continue
        row['attempted_at'] = now()
        if key in locations:
            raw = row['path']
            if row['manual_only']:
                observed = {'present': None, 'measured_at': None, 'reason': 'privacy-guarded'}
            elif '{' in raw or not (raw.startswith('~/') or Path(raw).is_absolute()):
                observed = {'present': None, 'measured_at': None, 'reason': 'unsupported-path-declaration'}
            else: observed = presence(Path(os.path.expanduser(raw)))
            row['presence'] = observed
        else:
            observed = availability(row['tool']); row['availability'] = observed
        row['measured_at'] = observed.get('measured_at'); row['stale'] = False
        if observed.get('reason'): error(key, observed['reason'])
    if any(r['stale'] for r in catalog.values()): error('coverage', 'budget-or-row-limit')
    return save()


def collect(out=None, **limits):
    out = Path(out) if out is not None else paths.SCRATCH / 'cache-coverage.json'
    if any(p.is_symlink() for p in (out, *out.parents)): raise ValueError('Output must not contain symbolic links')
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(out.with_name(out.name + '.lock'), os.O_RDWR | os.O_CREAT | osprivacy.NOFOLLOW | osprivacy.NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('Inventory lock must be regular')
        oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        try: return _collect(out, **limits)
        finally: oslocks.flock(fd, oslocks.LOCK_UN)
    finally: os.close(fd)
