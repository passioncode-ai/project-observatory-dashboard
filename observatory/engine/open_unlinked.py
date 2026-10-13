"""Bounded lsof evidence for files directory walks cannot see.

Logical sizes describe one command-visible snapshot, not allocated or reclaimable
bytes. No pathname, command, environment, owner inference or lifecycle action is
collected. Device/inode values identify records only within this snapshot.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time

MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 10000
MAX_FIELDS = 100000
PARSER_VERSION = 1
# Recognized nonregular types are excluded. Other dialect types, including DEL
# (deleted mapping with uncertain regular-file metadata), retain unknown coverage.
NONREGULAR_TYPES = {'DIR', 'CHR', 'BLK', 'FIFO', 'PIPE', 'IPv4', 'IPv6', 'unix',
                    'KQUEUE', 'EVENTFD', 'FSEVENTS', 'LINK'}


def now():
    return datetime.now(timezone.utc).isoformat()


def capture(argv, *, seconds=5, max_bytes=MAX_BYTES, env=None):
    """Stop only this owned observation child on its deadline/output ceiling."""
    out = bytearray()
    result = {'stdout': b'', 'returncode': None, 'stderr_seen': False,
              'truncated': False, 'reason': None}
    end = time.monotonic() + max(0, min(10, seconds))
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env if env is not None else {'PATH': os.defpath, 'LC_ALL': 'C'})
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'out')
            selector.register(process.stderr, selectors.EVENT_READ, 'err')
            total = 0
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    result['reason'] = 'command-timeout'; break
                for key, _ in selector.select(min(.1, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    total += len(chunk)
                    if key.data == 'out': out.extend(chunk[:max(0, max_bytes - len(out))])
                    else: result['stderr_seen'] = True
                    if total > max_bytes:
                        result['truncated'] = True; break
                if result['truncated']: break
        if process.poll() is None and (result['reason'] or result['truncated']): process.kill()
        try: result['returncode'] = process.wait(timeout=max(.01, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            result['reason'] = 'command-timeout'; process.kill(); result['returncode'] = process.wait(timeout=1)
    except (OSError, ValueError, subprocess.SubprocessError):
        result['reason'] = 'command-unavailable'
    finally:
        if process is not None:
            if process.poll() is None: process.kill(); process.wait(timeout=1)
            if process.stdout: process.stdout.close()
            if process.stderr: process.stderr.close()
    result['stdout'] = bytes(out)
    return result


def parse(raw, *, returncode=0, stderr_seen=False, truncated=False, max_records=MAX_RECORDS):
    errors = []
    def error(reason):
        if reason not in errors: errors.append(reason)
    if returncode != 0: error('command-not-successful')
    if stderr_seen: error('command-diagnostics')
    if truncated or len(raw) > MAX_BYTES: error('output-limit')
    raw = raw[:MAX_BYTES]
    try: text = raw.decode('ascii', errors='strict')
    except UnicodeError:
        text = ''; error('invalid-encoding')
    if text and not text.endswith('\n'):
        error('incomplete-field'); text = text.rsplit('\n', 1)[0] + '\n' if '\n' in text else ''
    records, handles = {}, set()
    pid, current = None, {}
    seen_file = False
    def finish():
        nonlocal seen_file
        if not current: return
        seen_file = True
        if current.get('t') != 'REG':
            if 't' not in current: error('missing-file-type')
            elif current['t'] not in NONREGULAR_TYPES: error('unknown-file-type')
            return
        if pid is None or not all(k in current for k in ('f', 'D', 'i')):
            error('incomplete-file'); return
        device, inode = current['D'], current['i']
        if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{1,32}', device) or not re.fullmatch(r'[0-9]{1,32}', inode):
            error('invalid-file-identity'); return
        key = (format(int(device, 16), 'x'), str(int(inode)))
        size = current.get('s')
        size = int(size) if size is not None and re.fullmatch(r'[0-9]{1,24}', size) else None
        if size is None: error('unknown-size')
        if key not in records:
            if len(records) >= min(MAX_RECORDS, max(0, max_records)):
                error('record-limit'); return
            records[key] = {'device': key[0], 'inode': key[1], 'logical_bytes': size,
                            'pids': set(), 'size_conflict': False}
        row = records[key]
        if row['logical_bytes'] != size:
            row['size_conflict'] = True; row['logical_bytes'] = None; error('conflicting-size')
        row['pids'].add(pid)
        handles.add((pid, current['f'], *key))
    lines = text.splitlines()
    if len(lines) > MAX_FIELDS: error('field-limit')
    for line in lines[:MAX_FIELDS]:
        if not line: error('empty-field'); continue
        tag, value = line[0], line[1:]
        if tag == 'p':
            finish(); current = {}
            pid = int(value) if re.fullmatch(r'[1-9][0-9]{0,10}', value) else None
            if pid is None: error('invalid-pid')
        elif tag == 'f':
            finish(); current = {}
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,24}', value): error('invalid-descriptor')
            else: current['f'] = value
        elif tag in ('t', 's', 'D', 'i'):
            if 'f' not in current or tag in current: error('unexpected-field')
            elif len(value) > 32: error('oversized-field')
            else: current[tag] = value
        else: error('unexpected-field')
    finish()
    if text and not seen_file: error('no-file-records')
    rows = [{**row, 'pids': sorted(row['pids'])} for row in records.values()]
    known = sum(row['logical_bytes'] for row in rows if row['logical_bytes'] is not None)
    return {'schema_version': 1, 'kind': 'open-unlinked', 'parser_version': PARSER_VERSION,
            'command_complete': not errors, 'whole_machine_complete': None,
            'scope': 'command-visible-snapshot', 'partial': True, 'actionable': False,
            'logical_bytes': known if not errors else None, 'known_logical_bytes': known,
            'allocated_bytes': None, 'reclaimable_bytes': None,
            'unique_files': len(rows), 'handle_count': len(handles), 'records': rows,
            'degraded': errors, 'limits': {'bytes': MAX_BYTES, 'fields': MAX_FIELDS, 'records': MAX_RECORDS}}


def observe():
    started = now()
    reason = None
    if sys.platform not in ('darwin', 'linux'): reason = 'unsupported-platform'
    executable = shutil.which('lsof') if reason is None else None
    if reason is None and executable is None: reason = 'source-unavailable'
    if reason:
        result = {'stdout': b'', 'returncode': None, 'stderr_seen': False, 'truncated': False, 'reason': reason}
    else:
        result = capture([executable, '-nP', '+L1', '-FpfstDi'])
    doc = parse(result['stdout'], returncode=result['returncode'],
                stderr_seen=result['stderr_seen'], truncated=result['truncated'])
    if result['reason']: doc['degraded'].append(result['reason']); doc['command_complete'] = False; doc['logical_bytes'] = None
    doc.update(attempted_at=started, completed_at=now(),
               parser_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return doc


def collect():
    """Persist only normalized metadata; concurrent collectors never interleave."""
    import atomic
    import oslocks
    import osprivacy
    import paths
    import stat
    out = paths.SCRATCH / 'open-unlinked.json'
    if any(p.is_symlink() for p in (out, *out.parents)): raise ValueError('Unsafe observation destination')
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(out.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | osprivacy.NOFOLLOW | osprivacy.NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('Unsafe observation lock')
        oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        try:
            doc = observe(); atomic.write_json(out, doc); return doc
        finally: oslocks.flock(fd, oslocks.LOCK_UN)
    finally: os.close(fd)


def saved_summary(at=None, *, base=None):
    """Bounded, allowlisted shared CLI/MCP read; never launch an observation."""
    import coverage_view
    import paths
    at = at or datetime.now(timezone.utc)
    out = {'source': 'open-unlinked.json', 'scope': 'command-visible-snapshot',
           'attempted_at': None, 'completed_at': None, 'age_seconds': None, 'stale': None,
           'partial': True, 'whole_machine_complete': None, 'actionable': False,
           'logical_bytes': None, 'known_logical_bytes': None, 'unique_files': None,
           'allocated_bytes': None, 'reclaimable_bytes': None, 'degraded': ['visibility-limited'],
           'sample': [], 'parser_sha256': None}
    source = paths.SCRATCH if base is None else Path(base) / 'store/raw'
    try: doc = coverage_view._read(source / 'open-unlinked.json')
    except FileNotFoundError: out['degraded'].append('not-surveyed'); return out
    except (OSError, ValueError, RecursionError): out['degraded'].append('unreadable-or-oversized'); return out
    try:
        if type(doc.get('schema_version')) is not int or doc['schema_version'] != 1 or doc.get('kind') != 'open-unlinked': raise ValueError
        digest = doc.get('parser_sha256')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest): raise ValueError
        if type(doc.get('parser_version')) is not int or doc['parser_version'] != PARSER_VERSION: raise ValueError
        rows = doc.get('records')
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS: raise ValueError
        seen, known, all_known, sample = set(), 0, True, []
        for row in rows:
            if not isinstance(row, dict): raise ValueError
            device, inode, pids, size = (row.get(k) for k in ('device', 'inode', 'pids', 'logical_bytes'))
            if not isinstance(device, str) or not re.fullmatch('[0-9a-f]{1,32}', device): raise ValueError
            if not isinstance(inode, str) or not re.fullmatch('[0-9]{1,32}', inode): raise ValueError
            key = (int(device, 16), int(inode))
            if key in seen: raise ValueError
            seen.add(key)
            if not isinstance(pids, list) or not pids or len(pids) > MAX_FIELDS or any(type(p) is not int or not 0 < p < 10**11 for p in pids): raise ValueError
            if size is not None and (type(size) is not int or not 0 <= size < 10**24): raise ValueError
            if type(row.get('size_conflict')) is not bool: raise ValueError
            if row['size_conflict'] and size is not None: raise ValueError
            if size is None: all_known = False
            else: known += size
            if len(sample) < 10: sample.append({'logical_bytes': size, 'pid_count': len(set(pids))})
        if type(doc.get('command_complete')) is not bool or not isinstance(doc.get('degraded'), list): raise ValueError
        if doc['degraded']: out['degraded'].append('producer-degraded')
        if not doc['command_complete']: out['degraded'].append('command-incomplete')
        start, end = doc.get('attempted_at'), doc.get('completed_at')
        if not all(isinstance(v, str) and len(v) <= 64 for v in (start, end)): raise ValueError
        start_age, end_age = coverage_view.stamp_age(start, at), coverage_view.stamp_age(end, at)
        if start_age is None or end_age is None or start_age < end_age: raise ValueError
        out.update(attempted_at=start, completed_at=end, age_seconds=end_age,
                   stale=end_age > 1800, parser_sha256=digest, unique_files=len(seen),
                   known_logical_bytes=known, sample=sample)
        if out['stale']: out['degraded'].append('stale-snapshot')
        if doc['command_complete'] and not doc['degraded'] and all_known:
            out['logical_bytes'] = known
    except (ValueError, TypeError, OverflowError): out['degraded'].append('invalid-receipt')
    return out
