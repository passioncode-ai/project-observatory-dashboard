"""Offline, bounded local upstream graph evidence; never remote durability."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import checkout_state
import open_unlinked
import safe_git

MAX_COUNT = 1_000_000_000
MAX_BYTES = 1024 * 1024
SCOPE = 'local-resolved-commit-graph-only'
REASONS = {'command-timeout', 'command-unavailable', 'output-limit', 'command-diagnostics',
           'git-state-unavailable', 'identity-changed', 'unsupported-state-file',
           'shallow-history', 'promisor-history', 'replacement-history', 'grafted-history',
           'alternate-object-store', 'detached-head', 'upstream-unresolved', 'head-unresolved',
           'malformed-graph-counts', 'unsupported-upstream', 'git-root-unresolved'}


def now():
    return datetime.now(timezone.utc).isoformat()


def source_hash():
    digest = hashlib.sha256()
    for name in ('checkout_graph.py', 'safe_git.py', 'open_unlinked.py', 'checkout_state.py'):
        digest.update(name.encode()); digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def unknown(reason='not-measured'):
    return {'state': 'unknown', 'complete': False, 'attempted_at': now(), 'measured_at': None,
            'ahead': None, 'behind': None, 'upstream_kind': 'unknown', 'binding': None,
            'scope': SCOPE, 'remote_durability': 'unknown', 'actionable': False,
            'source_sha256': source_hash(), 'degraded': [reason]}


def query(path, args, deadline, *, absent=False):
    remaining = deadline-time.monotonic()
    if remaining <= 0: raise ValueError('command-timeout')
    argv, env = safe_git.command(args, repo=path, timeout=remaining, offline=True,
                                extra_env={'GIT_NO_REPLACE_OBJECTS': '1'})
    row = open_unlinked.capture(argv, env=env, seconds=max(0, deadline-time.monotonic()), max_bytes=MAX_BYTES)
    if row['reason']: raise ValueError(row['reason'])
    if row['truncated']: raise ValueError('output-limit')
    if row['stderr_seen']: raise ValueError('command-diagnostics')
    if row['returncode'] == 1 and absent: return None
    if row['returncode'] != 0: raise ValueError('git-state-unavailable')
    return row['stdout']


def oid(raw):
    value = raw.strip()
    if not re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', value): raise ValueError('head-unresolved')
    return value.decode('ascii')


def parse_counts(raw):
    if not isinstance(raw, bytes) or not re.fullmatch(rb'[0-9]{1,10}[\t ]+[0-9]{1,10}\n?', raw):
        raise ValueError('malformed-graph-counts')
    values = tuple(map(int, raw.split()))
    if any(n > MAX_COUNT for n in values): raise ValueError('malformed-graph-counts')
    return values


def binding(path, deadline):
    if query(path, ['rev-parse', '--is-inside-work-tree'], deadline).strip() != b'true':
        raise ValueError('git-root-unresolved')
    top = Path(os.fsdecode(query(path, ['rev-parse', '--show-toplevel'], deadline).rstrip(b'\n')))
    if top.resolve() != path.resolve(): raise ValueError('identity-changed')
    gitdir = Path(os.fsdecode(query(path, ['rev-parse', '--absolute-git-dir'], deadline).rstrip(b'\n')))
    common = (path / os.fsdecode(query(path, ['rev-parse', '--git-common-dir'], deadline).rstrip(b'\n'))).resolve()
    for name, reason in (('shallow', 'shallow-history'), ('info/grafts', 'grafted-history'),
                         ('objects/info/alternates', 'alternate-object-store'),
                         ('objects/info/http-alternates', 'alternate-object-store')):
        try: (common / name).lstat()
        except FileNotFoundError: pass
        else: raise ValueError(reason)
    if query(path, ['rev-parse', '--is-shallow-repository'], deadline).strip() != b'false':
        raise ValueError('shallow-history')
    if query(path, ['config', '--includes', '--get-regexp', r'^(extensions\.partialclone|remote\..*\.promisor)$'], deadline, absent=True) is not None:
        raise ValueError('promisor-history')
    if query(path, ['for-each-ref', '--format=%(refname)', 'refs/replace/'], deadline).strip():
        raise ValueError('replacement-history')
    branch = query(path, ['symbolic-ref', '--quiet', 'HEAD'], deadline, absent=True)
    if branch is None: raise ValueError('detached-head')
    branch = branch.rstrip(b'\n')
    if not branch.startswith(b'refs/heads/') or b'\n' in branch or b'\0' in branch:
        raise ValueError('head-unresolved')
    head = oid(query(path, ['rev-parse', '--verify', 'HEAD^{commit}'], deadline))
    upstream = query(path, ['for-each-ref', '--format=%(upstream)', os.fsdecode(branch)], deadline).rstrip(b'\n')
    if not upstream or b'\n' in upstream or b'\0' in upstream: raise ValueError('upstream-unresolved')
    kind = ('local-branch' if upstream.startswith(b'refs/heads/') else
            'remote-tracking' if upstream.startswith(b'refs/remotes/') else 'unknown')
    if kind == 'unknown': raise ValueError('unsupported-upstream')
    upstream_oid = oid(query(path, ['rev-parse', '--verify', os.fsdecode(upstream)+ '^{commit}'], deadline))
    config = query(path, ['config', '--includes', '--null', '--list'], deadline)
    return {'root': checkout_state.identity(path, directory=True),
            'git_directory': [str(gitdir), checkout_state.identity(gitdir, directory=True)],
            'common_directory': [str(common), checkout_state.identity(common, directory=True)],
            'head': head, 'upstream_commit': upstream_oid, 'upstream_kind': kind,
            'branch_sha256': hashlib.sha256(branch).hexdigest(),
            'upstream_ref_sha256': hashlib.sha256(upstream).hexdigest(),
            'config_sha256': hashlib.sha256(config).hexdigest()}


def probe(path, *, seconds=2):
    row = unknown(); row['degraded'] = []
    if os.name == 'nt': row['degraded'] = ['bounded-pipe-observation-unavailable']; return row
    deadline = time.monotonic() + max(0, min(10, seconds))
    try:
        path = Path(path)
        if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError('identity-changed')
        before = binding(path, deadline)
        counts = parse_counts(query(path, ['rev-list', '--left-right', '--count',
                                          before['head']+'...'+before['upstream_commit']], deadline))
        after = binding(path, deadline)
        if before != after: raise ValueError('identity-changed')
        row.update(state='observed', complete=True, ahead=counts[0], behind=counts[1],
                   upstream_kind=before['upstream_kind'], binding=before, measured_at=now())
    except (OSError, ValueError, TypeError, KeyError) as exc:
        row['degraded'] = [str(exc) if isinstance(exc, ValueError) and str(exc) in REASONS else 'git-state-unavailable']
    return row


def observe(path, seconds=2):
    """The independent graph child cannot erase an already-published local-state row."""
    if seconds <= 0: return unknown('command-timeout')
    row = open_unlinked.capture([sys.executable, '-B', str(Path(__file__).resolve()), '--probe', str(path)],
                               seconds=seconds, max_bytes=MAX_BYTES)
    if row['reason'] or row['truncated'] or row['stderr_seen'] or row['returncode'] != 0:
        return unknown('graph-probe-unavailable')
    try:
        value = json.loads(row['stdout'])
        if not isinstance(value, dict) or value.get('scope') != SCOPE: raise ValueError
        return value
    except (ValueError, TypeError): return unknown('graph-probe-unavailable')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--probe':
    print(json.dumps(probe(sys.argv[2])))
