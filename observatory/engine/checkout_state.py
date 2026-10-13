"""Bounded local Git state inside the inventory's deadline-enforced probe child."""
from __future__ import annotations
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import time
import open_unlinked
import safe_git

MAX_BYTES = 1024 * 1024
MAX_RECORDS = 10000


def query(path, args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('command-timeout')
    argv, env = safe_git.command(args, repo=path, timeout=remaining, offline=True)
    result = open_unlinked.capture(argv, env=env, seconds=max(0, deadline - time.monotonic()), max_bytes=MAX_BYTES)
    if result['reason']:
        raise ValueError(result['reason'])
    if result['truncated']:
        raise ValueError('output-limit')
    if result['stderr_seen']:
        raise ValueError('command-diagnostics')
    if result['returncode'] != 0:
        raise ValueError('git-state-unavailable')
    return result['stdout']


def records(raw, max_records):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES or (raw and not raw.endswith(b'\0')):
        raise ValueError('malformed-git-state')
    rows = raw[:-1].split(b'\0') if raw else []
    if len(rows) > max_records or any(not row for row in rows):
        raise ValueError('git-state-record-limit-or-malformed')
    return rows


def parse_status(raw, *, max_records=MAX_RECORDS):
    rows = records(raw, max_records)
    counts = {'tracked_records': 0, 'untracked_records': 0, 'ignored_records': 0}
    index = 0
    while index < len(rows):
        row = rows[index]; index += 1
        if len(row) < 4 or row[2:3] != b' ':
            raise ValueError('malformed-git-state')
        code = row[:2]
        if code == b'??': counts['untracked_records'] += 1
        elif code == b'!!': counts['ignored_records'] += 1
        elif all(c in b' MTADRCU' for c in code) and code != b'  ':
            counts['tracked_records'] += 1
            if b'R' in code or b'C' in code:
                if index >= len(rows):
                    raise ValueError('malformed-git-state')
                index += 1  # original path is a second NUL record; never decode it
        else:
            raise ValueError('malformed-git-state')
    return counts


def parse_index(raw, *, max_records=MAX_RECORDS):
    counts = {'index_records': 0, 'assume_unchanged_records': 0, 'skip_worktree_records': 0}
    for row in records(raw, max_records):
        if len(row) < 3 or row[1:2] != b' ' or row[:1].upper() not in (b'H', b'S', b'M', b'R', b'C', b'K'):
            raise ValueError('malformed-index-state')
        counts['index_records'] += 1
        counts['assume_unchanged_records'] += int(row[:1].islower())
        counts['skip_worktree_records'] += int(row[:1].upper() == b'S')
    return counts


def identity(path, *, directory=False, missing=False):
    try:
        value = path.lstat()
    except FileNotFoundError:
        if missing: return None
        raise
    valid = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if not valid:
        raise ValueError('unsupported-state-file')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def binding(path, metadata, deadline):
    # Revalidate Git's effective root on both sides: core.worktree can redirect
    # status without replacing the directory, marker, HEAD or index.
    inside = query(path, ['rev-parse', '--is-inside-work-tree'], deadline).rstrip(b'\n')
    top = query(path, ['rev-parse', '--show-toplevel'], deadline).rstrip(b'\n')
    if inside != b'true' or Path(os.fsdecode(top)).resolve() != path.resolve():
        raise ValueError('identity-changed')
    gitdir = Path(metadata['git_directory'])
    actual = query(path, ['rev-parse', '--absolute-git-dir'], deadline).rstrip(b'\n')
    if os.fsdecode(actual) != str(gitdir):
        raise ValueError('identity-changed')
    head = query(path, ['rev-parse', '--verify', 'HEAD'], deadline).strip()
    if not re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('head-unresolved')
    marker = path / '.git'
    return {'root': identity(path, directory=True), 'git_directory': identity(gitdir, directory=True),
            'git_marker': identity(marker, directory=marker.is_dir()),
            'index': identity(gitdir / 'index', missing=True), 'head': head.decode('ascii')}


def probe(path, metadata):
    stamp = datetime.now(timezone.utc).isoformat()
    result = {'state': 'unknown', 'complete': False, 'attempted_at': stamp, 'measured_at': None,
              'tracked_records': None, 'untracked_records': None, 'ignored_records': None,
              'index_records': None, 'assume_unchanged_records': None, 'skip_worktree_records': None,
              'binding': None, 'degraded': [], 'actionable': False,
              'scope': 'point-in-time-git-records; no filesystem completeness or lifecycle authority'}
    if metadata.get('classification') == 'bare-repository':
        result.update(state='not-applicable', degraded=['bare-repository'])
        return result
    if metadata.get('classification') not in ('clone', 'linked-worktree'):
        result['degraded'] = ['git-root-unresolved']; return result
    if os.name == 'nt':
        result['degraded'] = ['bounded-pipe-observation-unavailable']; return result
    deadline = time.monotonic() + 2
    try:
        path = Path(path)
        before = binding(path, metadata, deadline)
        status = parse_status(query(path, ['status', '--porcelain=v1', '-z', '--untracked-files=all',
                                          '--ignored=matching', '--ignore-submodules=none'], deadline))
        flags = parse_index(query(path, ['ls-files', '-v', '-z'], deadline))
        after = binding(path, metadata, deadline)
        if before != after:
            raise ValueError('identity-changed')
        result.update(status); result.update(flags); result['binding'] = after
        result['measured_at'] = datetime.now(timezone.utc).isoformat()
        if flags['assume_unchanged_records'] or flags['skip_worktree_records']:
            result['degraded'] = ['hidden-index-flags']
        else:
            result.update(complete=True, state='changes-observed' if any(status.values()) else 'clean-observed')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        allowed = {'command-timeout', 'command-unavailable', 'output-limit', 'command-diagnostics',
                   'git-state-unavailable', 'malformed-git-state', 'git-state-record-limit-or-malformed',
                   'malformed-index-state', 'identity-changed', 'unsupported-state-file', 'head-unresolved'}
        result['degraded'] = [str(exc) if isinstance(exc, ValueError) and str(exc) in allowed else 'git-state-unavailable']
    return result
