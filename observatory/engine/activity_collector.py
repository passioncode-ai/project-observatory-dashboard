"""The device's activity collector: usage lines and session intervals read from agent transcripts (OBS-76 OE-01a).

Design: `docs/design/ACTIVITY-COLLECTOR.md`. Events are the contract's (fabric-agent-contract `fabric-activity/0.1`,
`telemetry-event.schema.json`); this module is Observatory's half, run by each organization enrollment
(`Enrollment.collect_activity`, a step of `full org run`):

- **What it reads.** Claude Code's session files (`~/.claude/projects/*/*.jsonl`, its subagents' files for usage only)
  and Codex's rollouts (`~/.codex/sessions`, `~/.codex/archived_sessions`), as `paths.transcript_homes()` names them.
  Of each line it keeps a time, the session, the folder, the model and the token counts; a line's content never
  leaves the parser.
- **How it keeps its place.** A cursor per file in the enrollment's own `activity.sqlite` (owner-only): device, inode,
  the SHA-256 of the file's first bytes and the offset read. A file whose head changed or that shrank is read again
  from the start; lines are read up to the last newline only; a pass stops at its byte and time budget and says how
  much it left.
- **What it emits.** `usage.line` per model call, with the provider's request id as `dedupeKey` and a derived event
  id, so a call read twice is one event; Claude Code writes one call over several lines, so a call is emitted once a
  line of another call (or the idle threshold) follows it, with the largest count each field reached. Its cost is
  `null` with no basis: no price is known yet (OE-01b adds the price list). `session.interval` by `method: transcript`:
  `agent_working` while lines follow each other within the idle threshold, `idle` across a longer silence;
  `awaiting_input` is not claimed.
- **Where it goes.** Only to this enrollment's organization, when the session's folder is a project of that
  organization (`route_target`, DEC-0082); a personal session's events are counted and kept on the device, never put
  in an outbox. Nothing before the enrollment's collection start is emitted.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

IDLE_THRESHOLD = 300
BUDGET_BYTES = 32 * 1024 * 1024
BUDGET_SECONDS = 20.0
HEAD_BYTES = 4096
#: A line longer than this is skipped, never read whole into memory.
MAX_LINE = 64 * 1024 * 1024
METHOD = 'transcript'
SOURCES = {'claude': ('claude_code.transcript', 'claude_code'), 'codex': ('codex.rollout', 'codex')}

_SOURCE_KEY = re.compile(r'(?![\s\S]*[._:~#-]-(?:[Uu]sers|home)-[A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._:~#-]{0,255}')
_OPAQUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}')
_BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}')
_TOKENS = ('input', 'output', 'cacheRead', 'cacheWrite')


# ---- identifiers ------------------------------------------------------------------------------------------------

def event_id(source: str, source_key: str) -> str:
    """`sha256:` of the settings-form canonical `{key, source}` (the contract's `activityEventId`)."""
    from node.repair import settings_canonical_json
    return 'sha256:' + hashlib.sha256(settings_canonical_json({'key': source_key, 'source': source})
                                      .encode('utf-8')).hexdigest()


def path_source_key(key: bytes, relative: str, offset: int) -> str:
    """`hmac-sha256:` of the settings-form canonical `{offset, path}` (the contract's `pathSourceKey`)."""
    from node.repair import settings_canonical_json
    message = settings_canonical_json({'offset': offset, 'path': relative}).encode('utf-8')
    return 'hmac-sha256:' + hmac.new(key, message, hashlib.sha256).hexdigest()


def hashed_branch(key: bytes, branch: str) -> str:
    return 'hmac-sha256:' + hmac.new(key, branch.encode('utf-8'), hashlib.sha256).hexdigest()


def _epoch(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---- what each runtime's line says ------------------------------------------------------------------------------

def parse_claude(doc: dict) -> dict:
    """The parts of one Claude Code line the collector uses; never its content."""
    out = {'at': _epoch(doc.get('timestamp')), 'session': doc.get('sessionId'), 'cwd': doc.get('cwd'),
           'version': doc.get('version'), 'branch': doc.get('gitBranch'), 'side': bool(doc.get('isSidechain'))}
    message = doc.get('message')
    if doc.get('type') == 'assistant' and isinstance(message, dict) and isinstance(message.get('usage'), dict):
        usage, model = message['usage'], message.get('model')
        if isinstance(model, str) and not model.startswith('<'):
            out['call'] = {'id': doc.get('requestId') or message.get('id'), 'model': model,
                           'tokens': {'input': _count(usage.get('input_tokens')),
                                      'output': _count(usage.get('output_tokens')),
                                      'cacheRead': _count(usage.get('cache_read_input_tokens')),
                                      'cacheWrite': _count(usage.get('cache_creation_input_tokens'))}}
    return out


def parse_codex(doc: dict) -> dict:
    """The parts of one Codex rollout line the collector uses. OpenAI's `input_tokens` counts the cached ones too, so
    the contract's `input` is the uncached part and `cacheRead` the cached; reasoning is part of `output_tokens`."""
    kind, payload = doc.get('type'), doc.get('payload') if isinstance(doc.get('payload'), dict) else {}
    out = {'at': _epoch(doc.get('timestamp'))}
    if kind == 'session_meta':
        git = payload.get('git') if isinstance(payload.get('git'), dict) else {}
        out.update(session=payload.get('id'), cwd=payload.get('cwd'), version=payload.get('cli_version'),
                   branch=git.get('branch'))
    elif kind == 'turn_context':
        out.update(cwd=payload.get('cwd'), model=payload.get('model'))
    elif kind == 'token_usage_record' and isinstance(payload.get('usage'), dict):
        usage = payload['usage']
        whole, cached = _count(usage.get('input_tokens')), _count(usage.get('cached_input_tokens'))
        out['call'] = {'id': payload.get('response_id'), 'model': None,
                       'tokens': {'input': max(whole - cached, 0), 'output': _count(usage.get('output_tokens')),
                                  'cacheRead': cached,
                                  'cacheWrite': _count(usage.get('cache_write_input_tokens'))}}
    return out


PARSERS = {'claude': parse_claude, 'codex': parse_codex}


# ---- where the transcripts are ----------------------------------------------------------------------------------

def transcript_files(homes: Optional[List[dict]] = None) -> Iterator[Tuple[str, Path, Path, bool]]:
    """(runtime, root, file, main) for every transcript of the two runtimes. `main` is a session's own file; a Claude
    Code subagent's file (under `<session>/subagents/`) is read for usage only, so one session's intervals never come
    from two files."""
    if homes is None:
        import paths
        homes = paths.transcript_homes()
    seen = set()
    for row in homes:
        runtime = row.get('agent')
        if runtime not in SOURCES:
            continue
        for tree, suffixes in row.get('trees') or []:
            tree = Path(tree)
            if '.jsonl' not in suffixes or not tree.is_dir() or tree in seen:
                continue
            seen.add(tree)
            for file in sorted(tree.rglob('*.jsonl')):
                if file.is_file() and not file.is_symlink():
                    yield runtime, tree, file, not (runtime == 'claude' and 'subagents' in file.parts)


def _context_of(folder: str, cache: Dict[str, Tuple[str, Optional[str]]]) -> Tuple[str, Optional[str]]:
    """A folder's context and project: `org:<id>` when it is a project of an organization whose
    `config/organizations.json` entry names its server's id as `fabric_org`, else `personal` (nothing leaves). A
    folder the attribution cannot tell is personal too."""
    if folder in cache:
        return cache[folder]
    answer = ('personal', None)
    try:
        import fabric_attribution
        import organizations
        import paths
        found = fabric_attribution.attribute_local(folder)['answer']
        project = found.get('project') if found.get('outcome') == 'attached' else None
        if project:
            rows = json.loads((paths.REGISTRY / 'projects.json').read_text(encoding='utf-8')).get('projects') or []
            row = next((r for r in rows if r.get('id') == project), {})
            org = row.get('organization') if row.get('organization_source') in ('declared', 'rule') else None
            server = (organizations.load().get('organizations') or {}).get(org or '', {}).get('fabric_org')
            if isinstance(server, str) and server:
                answer = ('org:' + server, project)
    except Exception:  # noqa: BLE001 - an attribution that fails is personal: the safe reading sends nothing
        answer = ('personal', None)
    cache[folder] = answer
    return answer


# ---- the cursor store -------------------------------------------------------------------------------------------

@contextlib.contextmanager
def _db(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'activity.sqlite'
    fresh = not path.exists()
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if fresh:
            os.chmod(path, 0o600)
        conn.execute("""CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, dev TEXT, ino TEXT,
            head_len INTEGER, head TEXT, offset INTEGER NOT NULL DEFAULT 0, session TEXT, cwd TEXT, model TEXT,
            version TEXT, branch TEXT, pending TEXT, start REAL, last REAL, ended REAL)""")
        yield conn
    finally:
        conn.close()


def _head(path: Path, length: int) -> str:
    with open(path, 'rb') as stream:
        return hashlib.sha256(stream.read(length)).hexdigest()


def _identity(st) -> tuple:
    # Text, not INTEGER: Windows reports a 64-bit unsigned (ReFS: 128-bit) file id that SQLite's signed
    # 64-bit INTEGER cannot hold.
    return str(st.st_dev), str(st.st_ino)


def _fresh_row(path: str, st) -> dict:
    dev, ino = _identity(st)
    return {'path': path, 'dev': dev, 'ino': ino, 'head_len': 0, 'head': None, 'offset': 0,
            'session': None, 'cwd': None, 'model': None, 'version': None, 'branch': None, 'pending': None,
            'start': None, 'last': None, 'ended': None}


# ---- one pass -----------------------------------------------------------------------------------------------------

class Pass:
    """One collection pass for one enrollment. `emit(kind, data, occurred, key, envelope, event_id)` is the
    enrollment's `enqueue`; `enrolled` the org ids of the device's enrollments; `resolve(folder)` a folder's
    (context, project)."""

    def __init__(self, org_id: str, folder: Path, emit: Callable[..., Optional[dict]], key: bytes, *,
                 enrolled: List[str], start: float, resolve: Optional[Callable[[str], Tuple[str, Optional[str]]]]
                 = None, now: Optional[float] = None, idle: int = IDLE_THRESHOLD, budget_bytes: int = BUDGET_BYTES,
                 budget_seconds: float = BUDGET_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.org_id, self.folder, self.emit, self.key = org_id, folder, emit, key
        self.enrollments = [{'org': o} for o in enrolled]
        self.start, self.now, self.idle = start, now if now is not None else time.time(), idle
        self.budget_bytes, self.deadline, self.clock = budget_bytes, clock() + budget_seconds, clock
        cache: Dict[str, Tuple[str, Optional[str]]] = {}
        self.resolve = resolve or (lambda folder: _context_of(folder, cache))
        self.receipt = {'files': 0, 'readBytes': 0, 'usage': 0, 'intervals': 0, 'keptPersonal': 0,
                        'otherContext': 0, 'noProject': 0, 'skippedLines': 0, 'leftBytes': 0, 'degraded': []}

    # -- routing
    def _send(self, row: dict, runtime: str, kind: str, data: dict, occurred: float, *, key: str,
              source_key: Optional[str] = None) -> None:
        from node import context_route
        context, project = self.resolve(row['cwd']) if row['cwd'] else ('personal', None)
        route = context_route.route_target(context, self.enrollments)
        if route.get('reason') == 'personal':
            self.receipt['keptPersonal'] += 1
            return
        if route.get('route') != 'enrollment' or route.get('org') != self.org_id:
            self.receipt['otherContext'] += 1
            return
        if not project or not _OPAQUE.fullmatch(project) or not row['session'] \
                or not _OPAQUE.fullmatch(row['session']):
            self.receipt['noProject'] += 1
            return
        source, name = SOURCES[runtime]
        envelope = {'session': {'id': row['session']}, 'project': {'id': project}, 'runtime': {'name': name},
                    'account': {'class': 'unknown'}}
        if row['version'] and _VERSION.fullmatch(row['version']):
            envelope['runtime']['version'] = row['version']
        if row['branch'] and _BRANCH.fullmatch(row['branch']):
            envelope['git'] = {'branch': hashed_branch(self.key, row['branch'])}
        if source_key:
            envelope['sourceKey'] = source_key
        made = self.emit(kind, data, occurred=_iso(occurred), key=key, source=source, envelope=envelope,
                         event_id=event_id(source, source_key) if source_key else None)
        if made:
            self.receipt['usage' if kind == 'usage.line' else 'intervals'] += 1

    def _call(self, row: dict, runtime: str, root: Path, file: Path, call: dict, at: float, offset: int) -> None:
        model = call.get('model') or row['model']
        if at < self.start or not isinstance(model, str) or not _MODEL.fullmatch(model):
            return
        rid = call.get('id')
        source_key = rid if isinstance(rid, str) and _SOURCE_KEY.fullmatch(rid) else \
            path_source_key(self.key, file.relative_to(root).as_posix(), offset)
        source = SOURCES[runtime][0]
        data = {'model': model, 'tokens': {k: call['tokens'][k] for k in _TOKENS}, 'cost': {'usd': None},
                'dedupeKey': source_key}
        self._send(row, runtime, 'usage.line', data, at, key=event_id(source, source_key), source_key=source_key)

    def _interval(self, row: dict, runtime: str, start: float, end: float, state: str) -> None:
        start = max(start, self.start)
        if end <= start:
            return
        data = {'start': _iso(start), 'end': _iso(end), 'state': state, 'method': METHOD,
                'idleThresholdSeconds': self.idle}
        key = 'session.interval|%s|%s|%s|%s' % (runtime, row['session'], data['start'], state)
        self._send(row, runtime, 'session.interval', data, start, key=key)

    def _point(self, row: dict, runtime: str, at: float) -> None:
        """One more line of the session at `at`: the working stretch grows, or a silence closes it."""
        if row['start'] is None:
            if row['ended'] is not None and at - row['ended'] > self.idle:
                self._interval(row, runtime, row['ended'], at, 'idle')
            row['start'] = row['last'] = at
            row['ended'] = None
        elif at - row['last'] <= self.idle:
            row['last'] = max(row['last'], at)
        else:
            self._interval(row, runtime, row['start'], row['last'], 'agent_working')
            self._interval(row, runtime, row['last'], at, 'idle')
            row['start'] = row['last'] = at

    def _settle(self, row: dict, runtime: str, root: Path, file: Path, at: Optional[float]) -> None:
        """A held Claude Code call is emitted once a later line belongs to another call, or the file went quiet."""
        if not row['pending']:
            return
        held = json.loads(row['pending'])
        if at is None or at - held['at'] > self.idle:
            row['pending'] = None
            self._call(row, runtime, root, file, held, held['at'], held['offset'])

    def read(self, runtime: str, root: Path, file: Path, main: bool, conn) -> None:
        path = str(file)
        try:
            st = file.stat()
        except OSError:
            return
        found = conn.execute('SELECT * FROM files WHERE path = ?', (path,)).fetchone()
        row = dict(found) if found else _fresh_row(path, st)
        if found and ((row['dev'], row['ino']) != _identity(st) or st.st_size < row['offset']
                      or (row['head_len'] and _head(file, row['head_len']) != row['head'])):
            row = _fresh_row(path, st)
        parse = PARSERS[runtime]
        if st.st_size > row['offset'] and self.receipt['readBytes'] < self.budget_bytes \
                and self.clock() < self.deadline:
            self.receipt['files'] += 1
            want = max(self.budget_bytes - self.receipt['readBytes'], 1 << 20)
            with open(file, 'rb') as stream:
                stream.seek(row['offset'])
                chunk = stream.read(want)
                if b'\n' not in chunk and len(chunk) == want:
                    chunk += stream.read(MAX_LINE)
            end = chunk.rfind(b'\n')
            if end < 0 and len(chunk) >= MAX_LINE:
                row['offset'] += len(chunk)
                self.receipt['skippedLines'] += 1
                self.receipt['degraded'].append({'reason': 'line-too-long', 'runtime': runtime})
            elif end >= 0:
                position = row['offset']
                for raw in chunk[:end].split(b'\n'):
                    offset, position = position, position + len(raw) + 1
                    try:
                        doc = json.loads(raw)
                    except ValueError:
                        self.receipt['skippedLines'] += int(bool(raw.strip()))
                        continue
                    if not isinstance(doc, dict):
                        continue
                    self._line(row, runtime, root, file, main, parse(doc), offset)
                row['offset'] += end + 1
                self.receipt['readBytes'] += end + 1
            if not row['head_len'] or row['head_len'] < HEAD_BYTES:
                row['head_len'] = min(HEAD_BYTES, st.st_size)
                row['head'] = _head(file, row['head_len'])
        self.receipt['leftBytes'] += max(st.st_size - row['offset'], 0)
        if self.now - (row['last'] or self.now) > self.idle:
            self._settle(row, runtime, root, file, None)
            if main and row['start'] is not None:
                self._interval(row, runtime, row['start'], row['last'], 'agent_working')
                row['ended'], row['start'], row['last'] = row['last'], None, None
        elif row['pending'] and self.now - json.loads(row['pending'])['at'] > self.idle:
            self._settle(row, runtime, root, file, None)
        conn.execute('INSERT OR REPLACE INTO files (%s) VALUES (%s)' % (', '.join(row), ', '.join('?' * len(row))),
                     tuple(row.values()))

    def _line(self, row: dict, runtime: str, root: Path, file: Path, main: bool, said: dict, offset: int) -> None:
        for name in ('session', 'cwd', 'version', 'branch', 'model'):
            if isinstance(said.get(name), str) and said[name]:
                row[name] = said[name]
        at = said.get('at')
        call = said.get('call')
        if call and at is not None:
            if runtime == 'codex':
                self._call(row, runtime, root, file, call, at, offset)
            else:
                held = json.loads(row['pending']) if row['pending'] else None
                if held and held['id'] == call['id']:
                    for k in _TOKENS:
                        held['tokens'][k] = max(held['tokens'][k], call['tokens'][k])
                    row['pending'] = json.dumps(held)
                else:
                    if held:
                        row['pending'] = None
                        self._call(row, runtime, root, file, held, held['at'], held['offset'])
                    row['pending'] = json.dumps({**call, 'at': at, 'offset': offset})
        if at is not None and main and not said.get('side') and at >= self.start:
            self._point(row, runtime, at)

    def run(self, files: Optional[Iterator[Tuple[str, Path, Path, bool]]] = None) -> dict:
        listed = []
        for runtime, root, file, main in (files if files is not None else transcript_files()):
            try:
                mtime = file.stat().st_mtime
            except OSError:
                continue
            if mtime >= self.start:
                listed.append((mtime, runtime, root, file, main))
        with _db(self.folder) as conn:
            for _, runtime, root, file, main in sorted(listed, key=lambda item: item[0]):
                conn.execute('BEGIN IMMEDIATE')
                try:
                    self.read(runtime, root, file, main, conn)
                    conn.execute('COMMIT')
                except BaseException:
                    conn.execute('ROLLBACK')
                    raise
        if self.receipt['leftBytes']:
            self.receipt['degraded'].append({'reason': 'budget', 'leftBytes': self.receipt['leftBytes']})
        return self.receipt
