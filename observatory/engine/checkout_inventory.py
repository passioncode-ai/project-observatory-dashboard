"""Bounded explicit-root checkout observations. No cleanup consumer or authority."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
import atomic
import osproc
import oslocks
import osprivacy
import step_budget


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def identity(path):
    row = path.lstat()
    if not stat.S_ISDIR(row.st_mode):
        raise OSError('not a directory')
    return [row.st_dev, row.st_ino, row.st_mtime_ns]


def safe_path(path, root):
    return path.is_absolute() and str(path) == os.path.abspath(path) and path.is_relative_to(root) and not any(p.is_symlink() for p in (path, *path.parents))


def marker(path):
    try:
        mode = (path / '.git').lstat().st_mode
        if stat.S_ISDIR(mode): return 'git-directory'
        if stat.S_ISREG(mode): return 'git-file'
        return 'unsupported-git-marker'
    except FileNotFoundError:
        pass
    kinds = [('HEAD', stat.S_ISREG), ('objects', stat.S_ISDIR), ('refs', stat.S_ISDIR)]
    for name, check in kinds:
        try:
            if not check((path / name).lstat().st_mode): return None
        except FileNotFoundError:
            return None
    return 'bare-indicators'


def _probe(path):
    """Only metadata reads. The parent bounds this process including Git setup."""
    import safe_git
    def query(*args):
        result = safe_git.run(args, repo=path, timeout=1, offline=True)
        return result.stdout.strip() if result.returncode == 0 else None
    bare = query('rev-parse', '--is-bare-repository')
    gitdir = query('rev-parse', '--absolute-git-dir')
    common = query('rev-parse', '--git-common-dir')
    if bare not in ('true', 'false') or not gitdir or not common:
        return {'classification': 'unknown', 'reason': 'git-metadata-unavailable'}
    candidate = Path(path).resolve()
    if bare == 'true':
        matches_root = Path(gitdir).resolve() == candidate
    else:
        inside = query('rev-parse', '--is-inside-work-tree')
        top = query('rev-parse', '--show-toplevel')
        matches_root = inside == 'true' and top is not None and Path(top).resolve() == candidate
    if not matches_root:
        return {'classification': 'unknown', 'reason': 'git-root-mismatch'}
    common = str((Path(path) / common).resolve())
    classification = 'bare-repository' if bare == 'true' else ('linked-worktree' if common != gitdir else 'clone')
    head = query('rev-parse', '--verify', 'HEAD')
    branch = query('symbolic-ref', '--quiet', '--short', 'HEAD')
    result = {'classification': classification, 'head': head, 'branch': branch,
            'git_directory': gitdir, 'common_directory': common,
            'registration_status': 'not-measured', 'reason': None}
    import checkout_state
    result['local_state'] = checkout_state.probe(candidate, result)
    return result


def classify(path, seconds):
    if seconds <= 0:
        return {'classification': 'unknown', 'reason': 'git-budget-expired'}
    child = None
    try:
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--probe', str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 stdin=subprocess.DEVNULL, **osproc.new_group())
        output, _ = child.communicate(timeout=seconds)
        result = json.loads(output) if child.returncode == 0 else None
        if isinstance(result, dict) and result.get('classification') in ('clone', 'linked-worktree', 'bare-repository', 'unknown'):
            return result
        return {'classification': 'unknown', 'reason': 'git-metadata-unavailable'}
    except subprocess.TimeoutExpired:
        if child:
            osproc.stop_group(child.pid, grace=0)
            child.communicate(timeout=1)
        return {'classification': 'unknown', 'reason': 'git-probe-timeout'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'classification': 'unknown', 'reason': 'git-probe-unavailable'}


def registration_evidence(receipt):
    """Fresh path membership is evidence of registration, never ownership or idleness."""
    try:
        doc = json.loads(receipt.read_text(encoding='utf-8'))
        if (not isinstance(doc, dict) or type(doc.get('schema_version')) is not int
                or doc['schema_version'] != 1 or doc.get('kind') != 'worktree-registration'
                or not isinstance(doc.get('repositories'), list)):
            raise ValueError
        current = datetime.now(timezone.utc)
        try:
            stamp = datetime.fromisoformat(doc.get('measured_at').replace('Z', '+00:00'))
            top_current = 0 <= (current - stamp).total_seconds() <= 1800
        except (ValueError, TypeError, AttributeError):
            top_current = False
        known, complete = {}, doc.get('partial') is False and top_current
        for row in doc['repositories']:
            if not isinstance(row, dict) or type(row.get('stale')) is not bool:
                complete = False; continue
            stamp = row.get('registered_at')
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                age = (current - parsed).total_seconds()
            except (ValueError, TypeError, AttributeError):
                complete = False; continue
            trees = row.get('worktrees')
            if row['stale'] or not 0 <= age <= 1800 or not isinstance(trees, list):
                complete = False; continue
            for tree in trees:
                path = tree.get('path') if isinstance(tree, dict) else None
                if not isinstance(path, str) or not Path(path).is_absolute():
                    complete = False; continue
                known[path] = stamp
        return known, complete, doc.get('measured_at')
    except (OSError, ValueError):
        return {}, False, None


def _scan(out, roots, *, seconds=20, entries=250, depth=8, root_seconds=2):
    out = Path(out)
    started = now()
    if not roots or len(roots) > 64:
        raise ValueError('Name between one and 64 explicit roots')
    roots = sorted({os.path.abspath(p) for p in roots})
    entries, depth = max(1, min(10000, int(entries))), max(1, min(32, int(depth)))
    total_budget = min(60, max(0, seconds), max(0, step_budget.remaining()))
    graph_budget = min(5, total_budget / 4)
    graph_end = time.monotonic() + total_budget
    end = graph_end - graph_budget
    degraded = []
    registered, registration_complete, registration_at = registration_evidence(out.parent / "worktree-registration.json")
    def error(path, reason):
        degraded.append({'source': str(path), 'reason': reason})
    if not registration_complete:
        error('worktree-registration.json', 'registration-coverage-not-current')
    try:
        old = json.loads(out.read_text(encoding='utf-8'))
        if not isinstance(old, dict) or old.get('kind') != 'checkout-candidates' or old.get('schema_version') != 1:
            raise ValueError
    except FileNotFoundError:
        old = {}
    except (OSError, ValueError):
        error('previous', 'prior-receipt-unreadable'); old = {}
    prior_roots = old.get('roots', [])
    prior_candidates = old.get('candidates', [])
    if not isinstance(prior_roots, list) or not isinstance(prior_candidates, list):
        error('previous', 'prior-receipt-invalid'); prior_roots, prior_candidates = [], []
    states = {r['path']: r for r in prior_roots if isinstance(r, dict) and r.get('path') in roots}
    candidates = {r['path']: {**r, 'stale': True, 'actionable': False} for r in prior_candidates
                  if isinstance(r, dict) and isinstance(r.get('path'), str) and r.get('root') in roots}
    for root in roots:
        states.setdefault(root, {'path': root, 'attempted_at': None, 'measured_at': None, 'stack': [], 'complete': False})
        states[root] = {'attempted_at': None, 'measured_at': None, 'complete': False, **states[root], 'stale': True}
    last = old.get('cursor', {}).get('last_attempted') if isinstance(old.get('cursor'), dict) else None
    order = [r for r in roots if not isinstance(last, str) or r > last] + [r for r in roots if isinstance(last, str) and r <= last]
    graph_last = old.get('upstream_cursor', {}).get('last_attempted') if isinstance(old.get('upstream_cursor'), dict) else None
    def save():
        doc = {'schema_version': 1, 'kind': 'checkout-candidates', 'attempted_at': started,
               'roots': [states[r] for r in roots], 'candidates': sorted(candidates.values(), key=lambda r: r['path']),
               'cursor': {'last_attempted': last}, 'upstream_cursor': {'last_attempted': graph_last}, 'partial': bool(degraded) or any(r['stale'] or not r['complete'] for r in states.values()) or any(r['stale'] for r in candidates.values()),
               'degraded': degraded, 'limits': {'seconds': seconds, 'entries_per_root': entries, 'depth': depth,
                                               'root_seconds': root_seconds, 'candidate_limit': 10000, 'upstream_seconds': graph_budget},
               'authority': 'inventory-only'}
        atomic.write_json(out, doc)
        return doc
    for root_text in order:
        if time.monotonic() >= end: break
        root = Path(root_text); state = states[root_text]
        last = root_text; state['attempted_at'] = now(); state['stale'] = True
        save()  # advance root before a filesystem read or Git call can stall
        try:
            if not safe_path(root, root): raise OSError
            root_id = identity(root)
        except OSError:
            error(root, 'root-unavailable'); state['complete'] = False; continue
        stack = state.get('stack')
        if not isinstance(stack, list) or len(stack) > depth + 1 or any(not isinstance(f, dict) or not isinstance(f.get('path'), str) or type(f.get('offset')) is not int or f['offset'] < -1 for f in stack):
            error(root, 'cursor-invalid'); stack = []
        if state.get('identity') not in (None, root_id):
            error(root, 'cursor-identity-changed'); stack = []
        if not stack: stack = [{'path': root_text, 'offset': -1}]
        state.update(identity=root_id, stack=stack, complete=False)
        stop = min(end, time.monotonic() + max(0.01, min(10, root_seconds)))
        visited, root_errors = 0, len(degraded)
        while stack and visited < entries and time.monotonic() < stop:
            frame = stack[-1]; path = Path(frame['path'])
            try:
                if not safe_path(path, root):
                    error(path, 'symlink-skipped'); stack.pop(); continue
                current = identity(path)
                if frame.get('identity') not in (None, current):
                    error(path, 'cursor-identity-changed'); frame['offset'] = -1
                frame['identity'] = current
                if frame['offset'] == -1:
                    visited += 1
                    kind = marker(path)
                    frame['offset'] = 0
                    if kind:
                        stack.pop()
                        if len(candidates) >= 10000 and str(path) not in candidates:
                            error(path, 'candidate-limit'); continue
                        stamp = now()
                        row = {'id': 'checkout:' + hashlib.sha256(str(path).encode()).hexdigest()[:24],
                               'path': str(path), 'root': root_text, 'source_kind': kind, 'identity': current,
                               'attempted_at': stamp, 'measured_at': stamp, 'stale': False,
                               'classification': 'unknown', 'registration_status': 'not-measured',
                               'allocated_bytes': None, 'reclaimable_bytes': None, 'size_reason': 'not-measured', 'owner': None,
                               'active_use': 'unknown', 'restore_cost': None, 'regeneration_command': None,
                               'retention_policy': None, 'actionable': False,
                               'blockers': ['inventory-only', 'ownership-unmeasured', 'local-state-unmeasured', 'holders-unmeasured']}
                        row['upstream'] = candidates.get(str(path), {}).get('upstream')
                        candidates[str(path)] = row; save()
                        evidence = ({'classification': 'unknown', 'reason': 'unsupported-git-marker'} if kind == 'unsupported-git-marker'
                                    else classify(path, min(2, max(0, stop - time.monotonic()))))
                        if identity(path) != current:
                            evidence = {'classification': 'unknown', 'reason': 'candidate-identity-changed'}
                        row.update(evidence)
                        local = evidence.get('local_state')
                        if isinstance(local, dict):
                            row['blockers'].remove('local-state-unmeasured')
                            if local.get('state') != 'clean-observed':
                                row['blockers'].append('local-state-not-clean-or-unknown')
                        row['git_attempted_at'] = stamp
                        row['git_measured_at'] = now() if evidence.get('classification') != 'unknown' else None
                        row['registration'] = {'status': 'listed-current' if str(path) in registered else ('not-listed-in-current-pass' if registration_complete else 'unknown'),
                                               'measured_at': registered.get(str(path), registration_at), 'source': 'worktree-registration.json'}
                        row['registration_status'] = row['registration']['status']
                        row['registration']['reason'] = 'coverage-not-current' if row['registration_status'] == 'unknown' else None
                        if evidence.get('reason'): error(path, evidence['reason'])
                        continue
                with os.scandir(path) as iterator:
                    item = None
                    for index, child in enumerate(iterator):
                        if time.monotonic() >= stop: break
                        if index < frame['offset']: continue
                        item = child; frame['offset'] = index + 1; visited += 1; break
                    else:
                        stack.pop(); continue
                    if item is None: break
                    if item.is_symlink(): error(item.path, 'symlink-skipped')
                    elif item.is_dir(follow_symlinks=False):
                        if len(stack) >= depth: error(item.path, 'depth-limit')
                        else: stack.append({'path': item.path, 'offset': -1})
            except OSError:
                error(path, 'directory-unreadable')
                if stack and stack[-1] is frame: stack.pop()
        state.update(measured_at=now(), stale=False, complete=not stack and len(degraded) == root_errors,
                     incomplete_reason=('entry-budget' if visited >= entries else 'time-budget') if stack else ('source-error' if len(degraded) > root_errors else None))
        save()
    save()  # local-state publication survives every independent graph attempt
    import checkout_graph
    graph_end = min(graph_end, time.monotonic() + graph_budget)
    graph_paths = sorted(candidates)
    graph_order = [p for p in graph_paths if not isinstance(graph_last, str) or p > graph_last] + [p for p in graph_paths if isinstance(graph_last, str) and p <= graph_last]
    for candidate in graph_order:
        if time.monotonic() >= graph_end: break
        row = candidates[candidate]
        if row.get('stale') or row.get('classification') not in ('clone', 'linked-worktree'): continue
        graph_last = candidate
        save()  # advance before the child, including failed attempts
        try:
            if not safe_path(Path(candidate), Path(row['root'])) or identity(Path(candidate)) != row['identity']:
                row['upstream'] = checkout_graph.unknown('identity-changed')
            else:
                row['upstream'] = checkout_graph.observe(candidate, min(2.5, max(0, graph_end-time.monotonic())))
        except (OSError, ValueError, TypeError, KeyError):
            row['upstream'] = checkout_graph.unknown('graph-probe-unavailable')
        save()
    return save()


def scan(out, roots, **limits):
    """One writer per receipt; a competing pass refuses without losing cursor progress."""
    out = Path(out)
    if any(p.is_symlink() for p in (out, *out.parents)):
        raise ValueError('Output path must not contain symbolic links')
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(out.with_name(out.name + '.lock'),
                        os.O_RDWR | os.O_CREAT | osprivacy.NOFOLLOW | osprivacy.NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Inventory lock must be a regular file')
        oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        try:
            return _scan(out, roots, **limits)
        finally:
            oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('output', type=Path)
    ap.add_argument('--root', action='append', required=True, type=Path)
    ap.add_argument('--budget-seconds', type=float, default=20)
    ap.add_argument('--entries-per-root', type=int, default=250)
    args = ap.parse_args(argv)
    print(json.dumps(scan(args.output, args.root, seconds=args.budget_seconds, entries=args.entries_per_root)))
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        try: print(json.dumps(_probe(sys.argv[2])))
        except Exception: print(json.dumps({'classification': 'unknown', 'reason': 'git-probe-failed'}))
    else:
        raise SystemExit(main())
