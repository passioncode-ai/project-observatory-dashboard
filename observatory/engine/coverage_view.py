"""Bounded saved inventory summaries. No producer calls or cleanup authority."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import stat
from datetime import datetime, timezone
import osprivacy
import paths

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 10000
SAMPLE_ROWS = 10
MAX_AGE_SECONDS = 1800


def local_state_summary(value, at, current):
    """Project saved counters only; never promote malformed/stale clean evidence."""
    keys = ('tracked_records', 'untracked_records', 'ignored_records', 'index_records',
            'assume_unchanged_records', 'skip_worktree_records')
    result = {'recorded_state': 'unknown', 'current_state': 'unknown', 'stale': True,
              'age_seconds': None, 'actionable': False, **dict.fromkeys(keys)}
    if not isinstance(value, dict): return result
    state = value.get('state')
    if state not in ('clean-observed', 'changes-observed', 'unknown', 'not-applicable'):
        return result
    counts = {key: value.get(key) for key in keys}
    valid = all(type(n) is int and 0 <= n <= 10000 for n in counts.values())
    if valid: result.update(counts)
    age = stamp_age(value.get('measured_at'), at)
    fresh = current and age is not None and age <= MAX_AGE_SECONDS
    result.update(recorded_state=state, age_seconds=age, stale=not fresh)
    if (fresh and valid and value.get('complete') is True and value.get('degraded') == []
            and not counts['assume_unchanged_records'] and not counts['skip_worktree_records']
            and state in ('clean-observed', 'changes-observed')):
        changed = any(counts[k] for k in keys[:3])
        if changed == (state == 'changes-observed'):
            result['current_state'] = state
    return result


def upstream_summary(value, at, candidate=None):
    """A separately dated graph observation cannot inherit another phase's freshness."""
    import checkout_graph
    out = {'state': 'unknown', 'ahead': None, 'behind': None, 'upstream_kind': 'unknown',
           'age_seconds': None, 'stale': True, 'remote_durability': 'unknown',
           'scope': checkout_graph.SCOPE, 'actionable': False}
    if not isinstance(value, dict): return out
    age = stamp_age(value.get('measured_at'), at)
    out.update(age_seconds=age, stale=age is None or age > MAX_AGE_SECONDS)
    binding = value.get('binding')
    def directory_binding(entry):
        return (isinstance(entry, list) and len(entry) == 2 and safe_text(entry[0]) is not None
                and os.path.isabs(entry[0]) and isinstance(entry[1], list) and len(entry[1]) == 4
                and all(type(n) is int and n >= 0 for n in entry[1]))
    bound = (isinstance(binding, dict) and isinstance(binding.get('root'), list)
             and len(binding['root']) == 4 and all(type(n) is int and n >= 0 for n in binding['root'])
             and all(isinstance(binding.get(k), str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', binding[k])
                     for k in ('head','upstream_commit'))
             and binding.get('upstream_kind') == value.get('upstream_kind')
             and all(directory_binding(binding.get(k)) for k in ('git_directory','common_directory'))
             and all(isinstance(binding.get(k), str) and re.fullmatch(r'[0-9a-f]{64}', binding[k])
                     for k in ('config_sha256','branch_sha256','upstream_ref_sha256')))
    try: expected_source = checkout_graph.source_hash()
    except OSError: return out
    attempt_age = stamp_age(value.get('attempted_at'), at)
    bound = bound and attempt_age is not None and age is not None and attempt_age >= age
    if candidate is not None:
        identity = candidate.get('identity')
        root = binding.get('root') if isinstance(binding, dict) else None
        bound = (bound and isinstance(identity, list) and len(identity) == 3 and all(type(n) is int and n >= 0 for n in identity) and len(root) == 4
                 and root[:2] == identity[:2] and root[3] == identity[2]
                 and binding.get('head') == candidate.get('head')
                 and binding['git_directory'][0] == candidate.get('git_directory')
                 and binding['common_directory'][0] == candidate.get('common_directory')
                 and candidate.get('stale') is False)
    if (bound and not out['stale'] and value.get('complete') is True and value.get('state') == 'observed'
            and value.get('degraded') == [] and value.get('source_sha256') == expected_source
            and value.get('actionable') is False and value.get('scope') == checkout_graph.SCOPE and value.get('remote_durability') == 'unknown'
            and value.get('upstream_kind') in ('local-branch', 'remote-tracking')
            and all(type(value.get(k)) is int and 0 <= value[k] <= checkout_graph.MAX_COUNT for k in ('ahead','behind'))):
        out.update(state='observed', ahead=value['ahead'], behind=value['behind'], upstream_kind=value['upstream_kind'])
    return out


def stamp_age(value, at):
    if not isinstance(value, str): return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None: return None
        age = (at - stamp).total_seconds()
        return age if age >= 0 else None
    except (ValueError, OverflowError): return None


def safe_text(value, limit=512):
    return value if isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value) else None


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode): raise ValueError
    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino): raise ValueError
        with os.fdopen(fd, 'rb', closefd=False) as stream: raw = stream.read(MAX_BYTES + 1)
    finally: os.close(fd)
    if len(raw) > MAX_BYTES: raise ValueError
    doc = json.loads(raw)
    if not isinstance(doc, dict): raise ValueError
    return doc


def _source(name, kind, collections, at):
    out = {'source': name, 'attempted_at': None, 'age_seconds': None, 'stale': None,
           'partial': True, 'snapshot_count': None, 'current_snapshot_count': None,
           'count_basis': 'saved-valid-rows-within-declared-scope',
           'invalid_rows': 0, 'rows': [], 'rows_omitted': None,
           'allocated_bytes': None, 'reclaimable_bytes': None,
           'coverage_complete': False, 'actionable': False, 'degraded': []}
    def error(code):
        if code not in out['degraded']: out['degraded'].append(code)
    try: doc = _read(paths.SCRATCH / name)
    except FileNotFoundError: error('not-surveyed'); return out
    except (OSError, ValueError, RecursionError): error('unreadable-or-oversized'); return out
    if type(doc.get('schema_version')) is not int or doc['schema_version'] != 1 or doc.get('kind') != kind:
        error('invalid-receipt'); return out
    age = stamp_age(doc.get('attempted_at'), at)
    out['attempted_at'] = doc.get('attempted_at') if isinstance(doc.get('attempted_at'), str) and len(doc['attempted_at']) < 64 else None
    out['age_seconds'] = age
    out['stale'] = age > MAX_AGE_SECONDS if age is not None else None
    if age is None: error('invalid-or-future-attempt-time')
    elif out['stale']: error('stale-snapshot')
    if type(doc.get('partial')) is not bool: error('invalid-partial-state')
    upstream = doc.get('degraded')
    if not isinstance(upstream, list): error('invalid-degradation-state')
    elif upstream: error('producer-degraded')
    out['partial'] = doc.get('partial') is not False
    count, all_current, known_collection = 0, True, False
    for collection in collections:
        rows = doc.get(collection)
        if not isinstance(rows, list): error('invalid-' + collection); continue
        known_collection = True
        if len(rows) > MAX_ROWS: error('row-limit')
        for row in rows[:MAX_ROWS]:
            if not isinstance(row, dict):
                out['invalid_rows'] += 1; error('invalid-row'); continue
            key = 'tool' if collection == 'commands' else 'path'
            label = safe_text(row.get(key), 80 if key == 'tool' else 512)
            if key == 'tool' and label is not None and (not re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', label) or re.search(r'sk-|gh[pousr]_|token|password|secret|bearer|cookie|api.?key', label, re.I)):
                label = None
            if label is None:
                out['invalid_rows'] += 1; error('invalid-row'); continue
            row_age = stamp_age(row.get('measured_at'), at)
            current = (out['stale'] is False and row.get('stale') is False
                       and row.get('declaration_current', True) is True
                       and row_age is not None and row_age <= MAX_AGE_SECONDS)
            count += 1; all_current = all_current and current
            if len(out['rows']) < SAMPLE_ROWS:
                sample = {key: label, 'type': collection, 'stale': not current,
                          'age_seconds': row_age, 'actionable': False,
                          'allocated_bytes': None, 'reclaimable_bytes': None}
                if collection == 'candidates':
                    value = row.get('classification')
                    sample['classification'] = value if value in ('clone', 'linked-worktree', 'bare-repository', 'unknown') else 'unknown'
                    value = row.get('registration_status')
                    sample['recorded_registration'] = value if value in ('listed-current', 'not-listed-in-current-pass', 'unknown', 'not-measured') else 'unknown'
                    sample['local_state'] = local_state_summary(row.get('local_state'), at, current)
                    sample['upstream'] = upstream_summary(row.get('upstream'), at, row)
                out['rows'].append(sample)
    out['snapshot_count'] = count if known_collection else None
    out['rows_omitted'] = count - len(out['rows']) if known_collection else None
    out['partial'] = out['partial'] or bool(out['degraded']) or not all_current
    if not out['partial'] and out['stale'] is False:
        out['current_snapshot_count'] = count
    return out


def summary():
    import open_unlinked
    at = datetime.now(timezone.utc)
    out = {'read_at': at.isoformat(), 'scope': 'saved-inventory-only', 'actionable': False,
           'limits': {'bytes_per_source': MAX_BYTES, 'rows_per_collection': MAX_ROWS,
                      'sample_rows_per_source': SAMPLE_ROWS, 'max_age_seconds': MAX_AGE_SECONDS},
           'checkouts': _source('checkout-candidates.json', 'checkout-candidates', ('candidates',), at),
           'caches': _source('cache-coverage.json', 'cache-coverage', ('locations', 'commands'), at),
           'open_unlinked': open_unlinked.saved_summary(at)}
    # Every surface that embeds this block (CLI, MCP section, overview) carries what it did not
    # measure with it; an empty list would read as full coverage.
    out['degraded'] = degradations(out)
    return out


#: The command that refreshes each saved coverage receipt, named in its degradations.
REFRESH = {'checkouts': 'project-observatory full machine --checkouts --root PATH',
           'caches': 'project-observatory full machine --cache-coverage',
           'open_unlinked': 'project-observatory full machine --open-unlinked'}


def degradations(coverage: dict) -> list[dict]:
    """Every coverage source that is not fully measured, as `{source, code, reason}`.

    The MCP contract reads an empty `degraded` as full coverage, so a source that was never
    surveyed, is unreadable, stale or partial is named here with what refreshes it; a cache
    survey is partial by design (contents are never walked) and is named every time.
    """
    out = []
    for key, command in REFRESH.items():
        source = coverage.get(key) if isinstance(coverage, dict) else None
        if not isinstance(source, dict):
            out.append({'source': key, 'code': 'unreadable', 'reason': f'no summary; run `{command}`'})
            continue
        name = source.get('source') or key
        codes = [c for c in source.get('degraded') or [] if isinstance(c, str)]
        if not codes and (source.get('partial') is not False or source.get('coverage_complete') is False):
            codes = ['partial-coverage']
        for code in codes:
            reason = (f'not surveyed yet; run `{command}`' if code == 'not-surveyed'
                      else f'{code.replace("-", " ")}; `{command}` refreshes it')
            out.append({'source': name, 'code': code, 'reason': reason})
    return out


# Shared by the actual MCP tool schema; other machine sections retain their schema.
SOURCE_SCHEMA = {
    'type': 'object',
    'required': ['source', 'partial', 'snapshot_count', 'current_snapshot_count',
                 'rows', 'allocated_bytes', 'reclaimable_bytes', 'actionable', 'degraded'],
    'properties': {
        'source': {'type': 'string'}, 'partial': {'type': 'boolean'},
        'snapshot_count': {'type': ['integer', 'null'], 'minimum': 0},
        'current_snapshot_count': {'type': ['integer', 'null'], 'minimum': 0},
        'allocated_bytes': {'type': 'null'}, 'reclaimable_bytes': {'type': 'null'},
        'actionable': {'const': False}, 'degraded': {'type': 'array', 'items': {'type': 'string'}},
        'rows': {'type': 'array', 'maxItems': SAMPLE_ROWS, 'items': {
            'type': 'object', 'required': ['actionable'],
            'properties': {'actionable': {'const': False}}}},
    },
}
OUTPUT_SCHEMA = {
    'type': 'object', 'required': ['read_at', 'scope', 'actionable', 'checkouts', 'caches', 'limits', 'degraded'],
    'properties': {'read_at': {'type': 'string'}, 'scope': {'const': 'saved-inventory-only'},
                   'degraded': {'type': 'array', 'items': {'type': 'object', 'required': ['source', 'code', 'reason']}},
                   'actionable': {'const': False}, 'checkouts': SOURCE_SCHEMA, 'caches': SOURCE_SCHEMA,
                   'limits': {'type': 'object'},
                   'open_unlinked': {'type': 'object', 'required': ['partial', 'actionable', 'reclaimable_bytes'],
                                     'properties': {'partial': {'const': True}, 'actionable': {'const': False},
                                                    'reclaimable_bytes': {'type': 'null'}}}},
}
