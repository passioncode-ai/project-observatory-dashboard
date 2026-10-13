"""Machine-readable, fail-closed receipts for synthetic adversarial cases."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parents[1]
sys.path.insert(0, str(ENGINE))
import private_io
import safe_git


def source_receipt(source_paths):
    revision = safe_git.run(['rev-parse', 'HEAD'], repo=REPO, text=True, timeout=10)
    if revision.returncode:
        raise RuntimeError('source revision unavailable')
    rows = []
    for path in dict.fromkeys([*map(str, source_paths), __file__]):
        path = Path(path).resolve()
        rows.append({'path': str(path.relative_to(REPO)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return revision.stdout.strip(), rows


def main(case, argv, *, variant, source_paths):
    report = {'schema': 'synthetic-adversary/1', 'variant': variant, 'status': 'ERROR',
              'source_commit': None, 'sources': [], 'attempted': 0, 'completed': 0, 'cases': []}
    output = None
    try:
        if argv and argv[0] == 'one':
            seeds = [int(seed) for seed in argv[1:]]
        else:
            start = int(argv[0]) if argv else 7000000
            count = int(argv[1]) if len(argv) > 1 else 5000
            if not 1 <= count <= 100000 or len(argv) > 3:
                raise ValueError('invalid case count or arguments')
            seeds = list(range(start, start + count))
            output = Path(argv[2]) if len(argv) > 2 else None
        if not seeds or len(seeds) != len(set(seeds)):
            raise ValueError('seeds must be nonempty and unique')
        report['source_commit'], report['sources'] = source_receipt(source_paths)
        report['requested'] = len(seeds)
        for seed in seeds:
            report['attempted'] += 1
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    issues = case(seed)
                if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
                    raise ValueError('invalid case result')
                status = 'ERROR' if any(issue.startswith(('ENVIRONMENT', 'CRASH')) for issue in issues) else ('FAIL' if issues else 'PASS')
            except (Exception, KeyboardInterrupt) as exc:
                issues, status = [f'{type(exc).__name__}: {exc}'], 'ERROR'
            report['cases'].append({'seed': seed, 'status': status, 'issues': issues})
            if status != 'ERROR':
                report['completed'] += 1
        if source_receipt(source_paths) != (report['source_commit'], report['sources']):
            raise RuntimeError('source changed during execution')
        report['status'] = 'ERROR' if any(row['status'] == 'ERROR' for row in report['cases']) else ('FAIL' if any(row['status'] == 'FAIL' for row in report['cases']) else 'PASS')
        if output is not None:
            private_io.write(output, json.dumps(report, sort_keys=True, indent=2) + '\n')
    except (Exception, KeyboardInterrupt) as exc:
        report['status'] = 'ERROR'
        report['receipt_error'] = type(exc).__name__
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'PASS' else 1
