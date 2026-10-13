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
    return {'read_at': at.isoformat(), 'scope': 'saved-inventory-only', 'actionable': False,
            'limits': {'bytes_per_source': MAX_BYTES, 'rows_per_collection': MAX_ROWS,
                       'sample_rows_per_source': SAMPLE_ROWS, 'max_age_seconds': MAX_AGE_SECONDS},
            'checkouts': _source('checkout-candidates.json', 'checkout-candidates', ('candidates',), at),
            'caches': _source('cache-coverage.json', 'cache-coverage', ('locations', 'commands'), at),
            'open_unlinked': open_unlinked.saved_summary(at)}


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
    'type': 'object', 'required': ['read_at', 'scope', 'actionable', 'checkouts', 'caches', 'limits'],
    'properties': {'read_at': {'type': 'string'}, 'scope': {'const': 'saved-inventory-only'},
                   'actionable': {'const': False}, 'checkouts': SOURCE_SCHEMA, 'caches': SOURCE_SCHEMA,
                   'limits': {'type': 'object'},
                   'open_unlinked': {'type': 'object', 'required': ['partial', 'actionable', 'reclaimable_bytes'],
                                     'properties': {'partial': {'const': True}, 'actionable': {'const': False},
                                                    'reclaimable_bytes': {'type': 'null'}}}},
}
