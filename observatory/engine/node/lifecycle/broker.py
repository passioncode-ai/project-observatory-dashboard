"""The lifecycle broker: deterministic authority over enrolled targets (OBS-79 step L1, docs/design/LIFECYCLE.md).

An agent never launches, quits or kills an enrolled app itself; it asks the broker, and the broker acts through an
adapter (`Adapter`) on a target an owner enrolled and approved. Nothing in a request ever becomes an executable, a
path, arguments or an environment: a request names a target id, a request id and the revisions it expects, and the
registry (owner-only, closed schema) says what that target is.

What this module keeps, and why:

- **Operations are durable.** An effect (`ensure`, `stop`, `restart`) is admitted synchronously into SQLite and run
  by a worker; the caller polls the receipt (`get`). A process that dies mid-operation leaves the row `running`; the
  next process takes it over under a new generation, and a worker of the old generation can no longer finish it.
- **One request id means one thing.** A retry with the same request id is answered by the first operation, before
  policy and grant revisions are compared, because a client retrying a lost answer must learn whether the app was
  launched. The same id for another meaning is `idempotency_conflict`. Receipts answer for 24 hours.
- **The effect journal.** Before the only effect of a start the broker commits `effects(target, 'dispatching')`.
  A crash between that commit and the answer leaves a known unknown: the next start reports `delivery_unknown`
  instead of launching twice, until a live process settles it or the target stays stopped past
  `DISPATCH_RECONCILE_SECONDS`.
- **Crash circuits and the restart budget.** Two counted starts of one target within 60 s open a five-minute circuit.
  For always_on targets a slower loop is caught by the budget: three failed supervisor starts within 30 minutes stop
  the relaunching until a caller asks for the target, the owner changes its policy, or it is seen serving.
- **The person comes first.** An agent may stop only a process the broker itself started, that the person has not
  taken over (an activation after `ADOPT_GRACE_SECONDS`) and is not in front of (frontmost with input in the last
  `PERSON_PRESENT_SECONDS`): otherwise `in_use_by_person`. `held_off` is the owner's switch and is never worked
  around. Killing (`force`) and stopping an always_on target are the owner's.
- **Leases.** `ensure_running` again renews a caller's lease; a target with `idleStopMinutes` that the broker started
  and nobody renewed is stopped by the supervision pass (`supervise`). `status` never renews, starts or stops.

Callers are identified by the hash of their credential, compared in constant time, never by a name in a request.
One broker owns a state folder at a time: a second, in this process or another, is refused (`already_running`).
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Union

import oslocks
import osprivacy

ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$')
#: An app's identity is its code-signing designated requirement: signing team and bundle id, so an update signed by
#: the same team keeps matching. Never a content hash, inode, ctime or version: those change on every update.
IDENTITY_SCHEME = 'designated-requirement/1'
TEAM_ID = re.compile(r'^[A-Z0-9]{10}$')
BUNDLE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$')
LAUNCHD_LABEL = re.compile(r'^[A-Za-z0-9._-]{3,200}$')
SHA256_HEX = re.compile(r'^[0-9a-f]{64}$')
#: Readiness kinds whose probe exists. The product profiles (a Fabric hub, a Switchboard runtime) arrive with the
#: macOS adapter in step L2, together with their probes; until then a record naming one is refused, not ignored.
READINESS_KINDS = ('process', 'fabric-service')
TOOLS = (
    'lifecycle.list',
    'lifecycle.status',
    'lifecycle.ensure_running',
    'lifecycle.stop',
    'lifecycle.restart',
    'lifecycle.operation.get',
)
#: The tool each operation kind is authorised by.
KIND_TOOL = {
    'ensure': 'lifecycle.ensure_running',
    'stop': 'lifecycle.stop',
    'restart': 'lifecycle.restart',
}
#: The broker's own supervision pass (always_on, idle stop). It has no credential and no grant: it acts only on
#: enrolled, approved targets, never with owner rights.
SUPERVISOR = {'caller': 'supervisor', 'internal': True, 'grantRevision': 0, 'tokenHash': ''}
#: Caller names a credential can never take: `supervisor` is the broker's own pass, and a caller of that name would
#: read its operations.
RESERVED_CALLERS = frozenset(('supervisor',))
IDLE_MINUTES = (5, 1440)
#: An app the broker launched often brings itself forward in its first seconds (Electron apps do, even when opened
#: without activation). An activation later than this is the person taking the app over.
ADOPT_GRACE_SECONDS = 20
#: A frontmost app with keyboard or mouse input this recent has a person in front of it.
PERSON_PRESENT_SECONDS = 60
#: How long an app's own quit gets before SIGTERM follows.
QUIT_GRACE_SECONDS = 5
#: A start whose outcome is unknown (`effects` reads `dispatching`) blocks a second start only while the first could
#: still be landing. Past this window a target observed stopped is simply stopped: starting it again is no longer a
#: duplicate effect. A live pid settles it at once.
DISPATCH_RECONCILE_SECONDS = 120
#: What a target says while its own start is still coming up. Inside the operation's deadline these are progress,
#: not answers; at the deadline the last one is reported as it is.
START_TRANSIENT = frozenset(('stopped', 'starting', 'capability_missing', 'remote_unreachable', 'auth_required'))
#: Counted starts inside this window open the crash circuit (two starts: a five-minute pause).
CRASH_WINDOW_SECONDS = 60
CIRCUIT_SECONDS = 300
#: The 60 s circuit cannot see a loop whose starts are minutes apart: an app that fails behind a dialog, is closed by
#: the person, exits, and is started again by the supervisor every few minutes. A supervisor start FAILED when its
#: process never appeared, never reached readiness before it exited, or exited within EARLY_EXIT_SECONDS. That many
#: failed starts within RESTART_WINDOW_SECONDS stop always_on supervision of the target until a caller asks for it
#: (ensure_running or restart), the owner changes its policy, or it is seen serving.
RESTART_BUDGET = 3
RESTART_WINDOW_SECONDS = 30 * 60
EARLY_EXIT_SECONDS = 5 * 60
#: An idempotency receipt answers a retry for a day; terminal operations are kept a week.
REQUEST_TTL_SECONDS = 24 * 3600
OPERATION_RETENTION_SECONDS = 7 * 24 * 3600
#: How long `close` waits for running workers before leaving them to recovery.
CLOSE_GRACE_SECONDS = 8
#: Operations running at once, across every target; one more is `busy`.
MAX_RUNNING = 32
#: The registry and the authority are small documents; a larger one is not read.
MAX_DOCUMENT_BYTES = 1_048_576
MAX_TARGETS = 128
STORE_VERSION = 1
STORE_FILE = 'lifecycle.db'
#: One broker per state folder, across processes: two would both recover the same running operations and both pass
#: the effect journal's check. Held for the broker's life on this file beside the store.
LOCK_FILE = 'broker.lock'
_TRACEPARENT = re.compile(r'^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$')
TERMINAL = frozenset((
    'lease_renewed',
    'verification_pending',
    'ready',
    'already_ready',
    'held_off',
    'not_installed',
    'not_enrolled',
    'login_required',
    'auth_required',
    'capability_missing',
    'remote_unreachable',
    'identity_mismatch',
    'dependency_failed',
    'timeout',
    'denied',
    'circuit_open',
    'delivery_unknown',
    'failed',
    'busy',
    'policy_changed',
    'stopped',
    'already_stopped',
    'in_use_by_person',
    'stop_timeout',
    'unsupported_platform',
))


class Adapter(Protocol):
    """What the broker asks of a platform. The broker decides; the adapter observes and acts.

    - `identity_matches(target, *, deadline)`: whether what is installed at the target's place is the enrolled
      identity, answered by `deadline` (epoch seconds); raising `LifecycleError('verification_pending')` when it
      cannot tell in time.
    - `probe(target)`: an observation `{'state', 'pid'?, 'processReady', 'capabilityReady', 'active'?,
      'inputIdleSeconds'?, ...}`. `state` is `stopped`, `starting`, `ready`, or a refusal such as `auth_required`;
      `active` and `inputIdleSeconds` say whether the app is frontmost and how long the person has not touched input.
    - `start(target)`: one launch, returning a short action name; `LifecycleError` with `held_off`,
      `login_required`, `not_installed` or `identity_mismatch` means nothing was launched.
    - `stop(target, pid, *, force=False, mode='quit')`: `mode` `quit` asks the app, `term` signals it, `force` kills
      it; only `pid`, the process observed now, is ever signalled.
    - `close()` (optional): end any child the adapter runs, without waiting.
    """

    def identity_matches(self, target: dict, *, deadline: float) -> bool: ...

    def probe(self, target: dict) -> dict: ...

    def start(self, target: dict) -> str: ...

    def stop(self, target: dict, pid: int, *, force: bool = False, mode: str = 'quit') -> str: ...


class _Interrupted(Exception):
    """The broker began closing before this operation dispatched anything."""


class LifecycleError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


Source = Union[str, os.PathLike, Callable[[], dict]]


def default_state_dir() -> Path:
    """Where the installed broker keeps its store: `<state>/node-lifecycle/`, beside the meter's ledger.

    A file of its own rather than tables in the workspace's `observatory.db`: that database is the tick's, rebuilt
    and migrated by its schema, while these rows are an effect journal written synchronously (WAL, `synchronous=FULL`)
    by a long-running server, and an operation must survive a rebuild of the index it has nothing to do with."""
    import paths
    return Path(paths.STATE) / 'node-lifecycle'


def private_json(path: Union[str, os.PathLike], default: Optional[dict] = None) -> dict:
    """An owner-only JSON document: a regular file of this account that nobody else may read, reached through no
    symbolic link. A missing file is `default`, or `setup_required` when there is none."""
    p = Path(path)
    try:
        fd = osprivacy.open(p, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    except FileNotFoundError:
        if default is not None:
            return default
        raise LifecycleError('setup_required') from None
    except OSError:
        raise LifecycleError('unsafe_config') from None      # a link, or a file we may not read
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not osprivacy.owned_by_me(fd) or not osprivacy.private(fd):
            raise LifecycleError('unsafe_config')
        # A link among the parents would redirect a private file into an authority nobody enrolled.
        if p.absolute() != p.resolve():
            raise LifecycleError('unsafe_config')
        if info.st_size > MAX_DOCUMENT_BYTES:
            raise LifecycleError('invalid_config')
        chunks, size = [], 0
        while size <= MAX_DOCUMENT_BYTES:
            chunk = os.read(fd, MAX_DOCUMENT_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_DOCUMENT_BYTES:
        raise LifecycleError('invalid_config')
    try:
        return json.loads(b''.join(chunks).decode('utf-8'))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise LifecycleError('invalid_config') from None


def _read(source: Source, default: Optional[dict] = None) -> dict:
    return source() if callable(source) else private_json(source, default)


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise LifecycleError('invalid_argument')
    return value


def closed(value: Any, fields, required=()) -> None:
    if not isinstance(value, dict) or set(value) - set(fields) or set(required) - set(value):
        raise LifecycleError('invalid_argument')


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _registry_closed(value: Any, fields, required=()) -> None:
    try:
        closed(value, fields, required)
    except LifecycleError:
        raise LifecycleError('invalid_registry') from None


def registry(source: Source) -> dict:
    """The validated registry of enrolled targets; a missing file is an empty registry."""
    return validate_registry(_read(source, {'schemaVersion': 1, 'targets': {}}))


TARGET_FIELDS = (
    'targetId',
    'kind',
    'path',
    'identity',
    'policyRevision',
    'desiredState',
    'dependencies',
    'readiness',
    'timeoutSeconds',
    'startupEffects',
    'approved',
    'allowedCallers',
)
TARGET_OPTIONAL = ('idleStopMinutes', 'backgroundLaunch')


def validate_registry(doc: Any) -> dict:
    """Check the closed schema of the registry. Every field is required except `idleStopMinutes` and
    `backgroundLaunch`; an unknown field, a cycle among dependencies or a record the adapters could not act on
    safely refuses the whole document (`invalid_registry`, `invalid_dependencies`, `startup_effects_required`)."""
    _registry_closed(doc, ('schemaVersion', 'targets'), ('schemaVersion', 'targets'))
    if doc['schemaVersion'] != 1 or not isinstance(doc['targets'], dict) or len(doc['targets']) > MAX_TARGETS:
        raise LifecycleError('invalid_registry')
    for key, target in doc['targets'].items():
        valid_id(key)
        _registry_closed(target, TARGET_FIELDS + TARGET_OPTIONAL, TARGET_FIELDS)
        if target['targetId'] != key or target['kind'] not in ('app', 'launchd'):
            raise LifecycleError('invalid_registry')
        if not _absolute(target['path']):
            raise LifecycleError('invalid_registry')
        if 'backgroundLaunch' in target and (type(target['backgroundLaunch']) is not bool or target['kind'] != 'app'):
            # A background job has no window to keep closed; anything but a bool is not a decision.
            raise LifecycleError('invalid_registry')
        if target['desiredState'] not in ('on_demand', 'always_on', 'held_off'):
            raise LifecycleError('invalid_registry')
        idle = target.get('idleStopMinutes')
        if idle is not None and (
            type(idle) is not int
            or not IDLE_MINUTES[0] <= idle <= IDLE_MINUTES[1]
            or target['desiredState'] == 'always_on'
        ):
            # An always_on target that also idles out would fight its own supervision.
            raise LifecycleError('invalid_registry')
        if type(target['policyRevision']) is not int or target['policyRevision'] < 1:
            raise LifecycleError('invalid_registry')
        if type(target['approved']) is not bool:
            raise LifecycleError('invalid_registry')
        if not isinstance(target['dependencies'], list) or len(target['dependencies']) > 16:
            raise LifecycleError('invalid_registry')
        if not isinstance(target['allowedCallers'], list) or not target['allowedCallers']:
            raise LifecycleError('invalid_registry')
        for caller in target['allowedCallers']:
            valid_id(caller)
        if type(target['timeoutSeconds']) not in (int, float) or not 1 <= target['timeoutSeconds'] <= 60:
            raise LifecycleError('invalid_registry')
        if (
            not isinstance(target['startupEffects'], list)
            or not target['startupEffects']
            or not all(isinstance(e, str) and e for e in target['startupEffects'])
        ):
            raise LifecycleError('startup_effects_required')
        if target['kind'] == 'app':
            app_identity(target['identity'])
        else:
            launchd_identity(target['identity'])
        readiness = target['readiness']
        _registry_closed(readiness, ('kind', 'origin', 'serviceId', 'instance', 'buildDigest', 'capability'), ('kind',))
        if readiness['kind'] not in READINESS_KINDS:
            raise LifecycleError('invalid_registry')
        if 'capability' in readiness and not (isinstance(readiness['capability'], str) and ID.fullmatch(readiness['capability'])):
            raise LifecycleError('invalid_registry')
        if readiness['kind'] == 'fabric-service':
            _loopback_origin(readiness.get('origin'))
            if not all(isinstance(readiness.get(k), str) and readiness.get(k) for k in ('serviceId', 'instance', 'buildDigest')):
                raise LifecycleError('invalid_registry')
        elif set(readiness) - {'kind', 'capability'}:
            raise LifecycleError('invalid_registry')
    visited, stack = set(), set()

    def visit(key):
        if key in stack or key not in doc['targets']:
            raise LifecycleError('invalid_dependencies')
        if key in visited:
            return
        stack.add(key)
        for child in doc['targets'][key]['dependencies']:
            visit(valid_id(child))
        stack.remove(key)
        visited.add(key)

    for key in doc['targets']:
        visit(key)
    return doc


def _absolute(path: Any) -> bool:
    """An installed place, named absolutely, on the platform the record was enrolled on: the adapter decides
    later whether the place is one it trusts."""
    from pathlib import PurePosixPath, PureWindowsPath

    return (
        isinstance(path, str)
        and 0 < len(path) <= 1024
        and '\x00' not in path
        and (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute())
    )


def _loopback_origin(origin: Any) -> None:
    """A Fabric service's readiness is probed at a loopback origin only, and only an origin: no path, query,
    fragment or credentials ride in the record."""
    from urllib.parse import urlsplit

    if not isinstance(origin, str):
        raise LifecycleError('invalid_registry')
    try:
        uri = urlsplit(origin)
        port = uri.port
    except ValueError:
        raise LifecycleError('invalid_registry') from None
    if (
        uri.scheme != 'http'
        or uri.hostname != '127.0.0.1'
        or not port
        or uri.path
        or uri.query
        or uri.fragment
        or uri.username
        or uri.password
    ):
        raise LifecycleError('invalid_registry')


def app_identity(identity: Any) -> dict:
    """An app's identity: the requirement scheme, its bundle id and signing team, and the requirement text the
    adapter checks the installed bundle against."""
    _registry_closed(
        identity,
        ('scheme', 'bundleId', 'teamId', 'requirement', 'designatedRequirement'),
        ('scheme', 'bundleId', 'teamId', 'requirement'),
    )
    if (
        identity['scheme'] != IDENTITY_SCHEME
        or not isinstance(identity['bundleId'], str)
        or not BUNDLE_ID.fullmatch(identity['bundleId'])
        or not isinstance(identity['teamId'], str)
        or not TEAM_ID.fullmatch(identity['teamId'])
        or not isinstance(identity['requirement'], str)
        or not identity['requirement']
        or not isinstance(identity.get('designatedRequirement', ''), (str, type(None)))
    ):
        raise LifecycleError('invalid_registry')
    return identity


def launchd_identity(identity: Any) -> dict:
    """A background job's identity: its label, the hash of its definition and the hashes of the files it runs."""
    _registry_closed(identity, ('label', 'plistHash', 'artifacts'), ('label', 'plistHash', 'artifacts'))
    if (
        not isinstance(identity['label'], str)
        or not LAUNCHD_LABEL.fullmatch(identity['label'])
        or not isinstance(identity['plistHash'], str)
        or not SHA256_HEX.fullmatch(identity['plistHash'])
        or not isinstance(identity['artifacts'], dict)
        or not identity['artifacts']
        or not all(isinstance(k, str) and k.startswith('/') and isinstance(v, str) for k, v in identity['artifacts'].items())
    ):
        raise LifecycleError('invalid_registry')
    return identity


class Authority:
    """Which caller may use which tool on which target. A credential's hash identifies a caller; caller text in a
    request never does.

    The document is `{"schemaVersion": 1, "callers": {name: grant}}`, a grant being `{"tokenHash": sha256 hex of the
    credential, "revision": int, "revoked": bool, "owner": bool, "operatorProxy"?: bool, "allowedTools": [...],
    "allowedTargets": [...]}`. `operatorProxy` marks a caller that relays the operator's own commands; it may hold an
    always_on target off. A grant missing `revoked` counts as revoked. Only an owner's `*` covers every target."""

    def __init__(self, source: Source):
        self.source = source

    def document(self) -> dict:
        doc = _read(self.source)
        if not isinstance(doc, dict) or doc.get('schemaVersion') != 1 or not isinstance(doc.get('callers'), dict):
            raise LifecycleError('invalid_authority')
        for name, grant in doc['callers'].items():
            if not isinstance(name, str) or not ID.fullmatch(name) or name in RESERVED_CALLERS or not isinstance(grant, dict):
                raise LifecycleError('invalid_authority')
        return doc

    def authenticate(self, header: Any) -> dict:
        if not isinstance(header, str) or not header.startswith('Bearer ') or len(header) > 512:
            raise LifecycleError('unauthorized')
        try:
            digest = hashlib.sha256(header[7:].encode()).hexdigest()
        except UnicodeEncodeError:                          # a lone surrogate is no credential anyone was issued
            raise LifecycleError('unauthorized') from None
        found = None
        # Every grant is compared, so the time taken does not say which caller's hash came close.
        for caller, grant in self.document()['callers'].items():
            if hmac.compare_digest(str(grant.get('tokenHash', '')), digest) and grant.get('revoked', True) is False:
                found = found or {'caller': caller, 'grantRevision': grant.get('revision'), 'tokenHash': digest}
        if found is None or type(found['grantRevision']) is not int:
            raise LifecycleError('unauthorized')
        return found

    def grant(self, principal: dict) -> dict:
        """The caller's current grant, if the credential is still the one it was issued as."""
        grant = self.document()['callers'].get(principal['caller'], {})
        if (
            grant.get('revoked', True) is not False
            or grant.get('revision') != principal['grantRevision']
            or not hmac.compare_digest(str(grant.get('tokenHash', '')), principal['tokenHash'])
        ):
            raise LifecycleError('denied')
        return grant

    def check(self, principal: dict, tool: str, target: Optional[str] = None) -> dict:
        grant = self.grant(principal)
        tools = grant.get('allowedTools', [])
        if not isinstance(tools, list) or tool not in tools:
            raise LifecycleError('denied')
        targets = grant.get('allowedTargets', [])
        if not isinstance(targets, list):
            raise LifecycleError('denied')
        if target is not None and target not in targets and not ('*' in targets and grant.get('owner') is True):
            raise LifecycleError('denied')
        return grant


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
INSERT INTO meta SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM meta);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, target TEXT, caller TEXT,
  principal TEXT, arguments TEXT, policy TEXT, state TEXT, generation INTEGER,
  created REAL, updated REAL, result TEXT);
CREATE TABLE IF NOT EXISTS requests(caller TEXT, request_id TEXT, digest TEXT, operation TEXT, at REAL,
  PRIMARY KEY(caller,request_id));
CREATE TABLE IF NOT EXISTS effects(target TEXT PRIMARY KEY, state TEXT, at REAL, operation TEXT);
CREATE TABLE IF NOT EXISTS attempts(target TEXT, at REAL);
CREATE TABLE IF NOT EXISTS circuits(target TEXT PRIMARY KEY, until_at REAL);
CREATE TABLE IF NOT EXISTS launches(target TEXT PRIMARY KEY, pid INTEGER, at REAL,
  operation TEXT, caller TEXT, adopted REAL, ready_at REAL);
CREATE TABLE IF NOT EXISTS touches(target TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS stops(target TEXT PRIMARY KEY, at REAL, caller TEXT);
CREATE TABLE IF NOT EXISTS phases(operation TEXT PRIMARY KEY, phase TEXT, at REAL);
CREATE TABLE IF NOT EXISTS failed_starts(operation TEXT PRIMARY KEY, target TEXT, at REAL, cause TEXT);
CREATE TABLE IF NOT EXISTS gave_up(target TEXT PRIMARY KEY, at REAL, policy INTEGER, failures INTEGER);
CREATE INDEX IF NOT EXISTS operations_state ON operations(state);
CREATE INDEX IF NOT EXISTS operations_target ON operations(target, state);
CREATE INDEX IF NOT EXISTS attempts_target ON attempts(target, at);
"""
# The tables, and what each one remembers:
#   operations    every admitted effect, its frozen arguments and policy closure, its generation and its result
#   requests      the idempotency receipts: (caller, request id) -> the meaning's digest and the operation
#   effects       the effect journal: a start that was dispatched and whose outcome is not known yet
#   attempts      counted starts, for the crash circuit; circuits the open ones
#   launches      the process the broker itself started (who may stop it, whether it idles out), when the person
#                 took it over (`adopted`), when it was first seen ready (`ready_at`)
#   touches       a caller's last ensure or restart: its lease on a running app
#   stops         an explicit stop, which holds always_on supervision off until the next ensure
#   phases        how far a restart got: recovery resumes after its stop half
#   failed_starts supervisor starts that failed, one row per operation so a start is never counted twice
#   gave_up       the restart budget is spent: supervision waits for a caller, the owner or a serving observation


class Broker:
    def __init__(
        self,
        registry_source: Source,
        state_dir: Union[str, os.PathLike],
        authority: Authority,
        adapter: Adapter,
        *,
        clock: Callable[[], float] = time.time,
        max_workers: int = 8,
        on_finish: Optional[Callable[[dict, Optional[str], dict], None]] = None,
        on_admit: Optional[Callable[[dict], None]] = None,
        on_gave_up: Optional[Callable[[dict], None]] = None,
    ):
        self.registry_source, self.state_dir = registry_source, Path(state_dir)
        self.authority, self.adapter, self.clock = authority, adapter, clock
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        osprivacy.private_folder(self.state_dir)
        self._lock_fd: Optional[int] = None
        self._take_instance_lock()                          # before the store is opened and anything recovered
        self.db_lock = threading.RLock()
        path = self.state_dir / STORE_FILE
        try:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript(_SCHEMA)
            if self.db.execute('SELECT version FROM meta').fetchone()[0] != STORE_VERSION:
                self.db.close()
                raise LifecycleError('store_newer_than_code')
            self.db.commit()
            osprivacy.make_private(path)
        except BaseException:
            self._release_instance_lock()
            raise
        self.locks: dict = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='lifecycle')
        self.closed = False
        self.futures: set = set()
        self.on_finish = on_finish
        self.on_admit = on_admit
        self.on_gave_up = on_gave_up
        # Operations a dead process left running get a new generation; the old one can no longer finish them. An
        # unknown launch is probed, never repeated: `effects` survives even when the worker never wrote its receipt.
        with self.db_lock, self.db:
            rows = self.db.execute("SELECT id,generation FROM operations WHERE state='running'").fetchall()
            for row in rows:
                self.db.execute('UPDATE operations SET generation=generation+1 WHERE id=?', (row['id'],))
                self._submit(row['id'], row['generation'] + 1)
        self.prune()

    def _registry(self) -> dict:
        return registry(self.registry_source)

    def _take_instance_lock(self) -> None:
        fd = osprivacy.open(self.state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
        try:
            oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LifecycleError('already_running') from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _release_instance_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                oslocks.flock(fd, oslocks.LOCK_UN)
            finally:
                os.close(fd)

    @contextlib.contextmanager
    def _write(self):
        """One write transaction, taken at once (`BEGIN IMMEDIATE`): what it reads cannot change before it writes."""
        with self.db_lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            self.db.commit()

    def _submit(self, operation_id: str, generation: int) -> None:
        future = self.executor.submit(self._work, operation_id, generation)
        with self.db_lock:
            self.futures.add(future)
        future.add_done_callback(self._forget_future)

    def _forget_future(self, future) -> None:
        with self.db_lock:
            self.futures.discard(future)

    def prune(self) -> int:
        """Bound the store: receipts after a day, terminal operations after a week, old attempts and failures."""
        now = self.clock()
        with self.db_lock, self.db:
            self.db.execute(
                'DELETE FROM requests WHERE coalesce(at,(SELECT created FROM operations o WHERE o.id=operation),0)<?',
                (now - REQUEST_TTL_SECONDS,),
            )
            old = [
                r[0]
                for r in self.db.execute(
                    "SELECT id FROM operations WHERE state!='running' AND updated<?",
                    (now - OPERATION_RETENTION_SECONDS,),
                )
            ]
            for op in old:
                self.db.execute('DELETE FROM operations WHERE id=?', (op,))
                self.db.execute('DELETE FROM requests WHERE operation=?', (op,))
                self.db.execute('DELETE FROM phases WHERE operation=?', (op,))
            self.db.execute('DELETE FROM attempts WHERE at<?', (now - CRASH_WINDOW_SECONDS,))
            self.db.execute('DELETE FROM circuits WHERE until_at<?', (now,))
            self.db.execute('DELETE FROM failed_starts WHERE at<?', (now - RESTART_WINDOW_SECONDS,))
            self.db.execute("DELETE FROM phases WHERE operation NOT IN (SELECT id FROM operations WHERE state='running')")
        return len(old)

    # -- authority ---------------------------------------------------------------------------------------------

    def _target(self, principal: dict, tool: str, target_id: str, expected: Optional[int] = None) -> dict:
        if principal.get('internal'):
            target = self._registry()['targets'].get(target_id)
            if target is None:
                raise LifecycleError('not_enrolled')
            if target['approved'] is not True:
                raise LifecycleError('denied')
        else:
            self.authority.check(principal, tool, target_id)
            target = self._registry()['targets'].get(target_id)
            if target is None:
                raise LifecycleError('not_enrolled')
            if principal['caller'] not in target['allowedCallers'] or target['approved'] is not True:
                raise LifecycleError('denied')
        if expected is not None and target['policyRevision'] != expected:
            raise LifecycleError('policy_changed')
        return target

    def _holds_always_on(self, principal: dict) -> bool:
        """The owner, or a caller marked `operatorProxy`: one that relays the operator's own commands. Agents are
        neither."""
        grant = self.authority.grant(principal)
        return grant.get('owner') is True or grant.get('operatorProxy') is True

    def _is_owner(self, principal: dict, tool: str) -> bool:
        if principal.get('internal'):
            return False
        return self.authority.check(principal, tool).get('owner') is True

    # -- reads -------------------------------------------------------------------------------------------------

    def list(self, principal: dict) -> dict:
        grant = self.authority.check(principal, 'lifecycle.list')
        allowed = grant.get('allowedTargets', [])
        targets = []
        for target in self._registry()['targets'].values():
            if principal['caller'] in target['allowedCallers'] and (
                target['targetId'] in allowed or (grant.get('owner') is True and '*' in allowed)
            ):
                item = {k: target[k] for k in ('targetId', 'kind', 'desiredState', 'policyRevision')}
                if target.get('idleStopMinutes'):
                    item['idleStopMinutes'] = target['idleStopMinutes']
                if target.get('backgroundLaunch'):
                    item['backgroundLaunch'] = True
                targets.append(item)
        return {'targets': targets, 'observedAt': int(self.clock() * 1000), 'grantRevision': principal['grantRevision']}

    def status(self, principal: dict, target_id: str) -> dict:
        target = self._target(principal, 'lifecycle.status', valid_id(target_id))
        if target['desiredState'] == 'held_off':
            return self._observation('held_off') | {'targetId': target_id, 'policyRevision': target['policyRevision']}
        observation = self.adapter.probe(target)
        self._resolve_dispatch(target_id, observation)
        # Observing never renews a lease and never starts anything; it does say who owns the process.
        launch = self._launch(target_id)
        extra: dict = {'personPresent': self._person_present(observation)}
        if not self._is_owner(principal, 'lifecycle.status'):
            # The machine's idle time and its frontmost app are the person's, not every caller's: callers learn the
            # one bit the stop rules use.
            observation = {k: v for k, v in observation.items() if k not in ('inputIdleSeconds', 'active')}
        if observation.get('pid'):
            if launch and launch['pid'] == observation['pid']:
                extra['startedBy'] = 'person' if launch['adopted'] is not None else 'broker'
            else:
                extra['startedBy'] = 'other'
        gave_up = self._gave_up(target_id)
        if gave_up and target['desiredState'] == 'always_on' and gave_up['policy'] == target['policyRevision']:
            extra['supervision'] = {
                'state': 'gave_up',
                'since': int(gave_up['at'] * 1000),
                'failedStarts': gave_up['failures'],
            }
        return observation | extra | {
            'targetId': target_id,
            'policyRevision': target['policyRevision'],
            'grantRevision': principal['grantRevision'],
            'desiredState': target['desiredState'],
        }

    # -- admission ---------------------------------------------------------------------------------------------

    def ensure(self, principal: dict, args: dict, traceparent: Optional[str] = None) -> dict:
        return self._admit(principal, args, 'ensure', traceparent)

    def stop(self, principal: dict, args: dict, traceparent: Optional[str] = None) -> dict:
        return self._admit(principal, args, 'stop', traceparent)

    def restart(self, principal: dict, args: dict, traceparent: Optional[str] = None) -> dict:
        return self._admit(principal, args, 'restart', traceparent)

    def _admit(self, principal: dict, args: dict, kind: str, traceparent: Optional[str] = None,
               reason: Optional[str] = None) -> dict:
        if self.closed:
            raise LifecycleError('busy')
        tool = KIND_TOOL[kind]
        optional = ('deadlineAt', 'force') if kind == 'stop' else ('deadlineAt', 'requiredCapability')
        closed(
            args,
            ('targetId', 'requestId', 'expectedPolicyRevision', 'grantRevision') + optional,
            ('targetId', 'requestId', 'expectedPolicyRevision', 'grantRevision'),
        )
        target_id, request_id = valid_id(args['targetId']), valid_id(args['requestId'])
        if type(args['expectedPolicyRevision']) is not int or type(args['grantRevision']) is not int:
            raise LifecycleError('invalid_argument')
        if 'force' in args and type(args['force']) is not bool:
            raise LifecycleError('invalid_argument')
        if 'requiredCapability' in args and not isinstance(args['requiredCapability'], str):
            raise LifecycleError('invalid_argument')
        if 'deadlineAt' in args and (type(args['deadlineAt']) not in (int, float) or args['deadlineAt'] != args['deadlineAt']):
            raise LifecycleError('invalid_argument')
        if traceparent is not None and not (isinstance(traceparent, str) and _TRACEPARENT.fullmatch(traceparent)):
            traceparent = None                               # a malformed trace context is dropped, not stored
        # A retry is answered by its receipt BEFORE the policy and grant revisions are compared: a client retrying a
        # lost answer after a policy or grant change must get back the operation that may already have launched the
        # app, not `policy_changed` or `denied`. The digest is the request's meaning (target, tool, force,
        # capability); revisions are preconditions and the deadline is the client's clock, so neither makes it
        # another request. The caller's credential must still hold the tool for the target.
        if not principal.get('internal'):
            self.authority.check(principal, tool, target_id)
        meaning = {k: args[k] for k in ('targetId', 'force', 'requiredCapability') if k in args}
        digest = hashlib.sha256(canonical(meaning | {'_tool': tool}).encode()).hexdigest()
        replay = self._receipt(principal, tool, request_id, digest)
        if replay is not None:
            return replay
        if not principal.get('internal') and args['grantRevision'] != principal['grantRevision']:
            raise LifecycleError('denied')
        target = self._target(principal, tool, target_id, args['expectedPolicyRevision'])
        if args.get('force') and not self._is_owner(principal, tool):
            raise LifecycleError('denied')                  # killing a process is the owner's decision alone
        if kind != 'stop':
            supported = (None, target['readiness'].get('capability'), 'process')
            if args.get('requiredCapability') not in supported:
                raise LifecycleError('capability_missing')
        # A restart is a stop and a start: its budget is doubled, and so is its default deadline.
        budget = target['timeoutSeconds'] * (2 if kind == 'restart' else 1)
        deadline = args.get('deadlineAt', int((self.clock() + budget) * 1000))
        # The receipt is read again and written in one write transaction: two concurrent retries of one request id
        # get one operation, never two and never an integrity error.
        with self._write():
            existing = self.db.execute(
                'SELECT * FROM requests WHERE caller=? AND request_id=?', (principal['caller'], request_id)
            ).fetchone()
            if existing:
                if existing['digest'] != digest:
                    raise LifecycleError('idempotency_conflict')
                return self._view(principal, existing['operation'])
            if deadline <= self.clock() * 1000:
                raise LifecycleError('timeout')
            if self.db.execute("SELECT count(*) FROM operations WHERE state='running'").fetchone()[0] >= MAX_RUNNING:
                raise LifecycleError('busy')
            op = str(uuid.uuid4())
            # Every caller has its own receipt; the target's effect lock joins their launches.
            fixed_args = args | {'deadlineAt': min(deadline, int((self.clock() + budget) * 1000)), '_kind': kind}
            if traceparent is not None:
                fixed_args['_traceparent'] = traceparent
            if reason is not None and principal.get('internal'):
                fixed_args['_reason'] = reason
            # The fence compares only this target and its dependencies, so that is all the snapshot keeps.
            policy = self._closure(self._registry(), target_id)
            self.db.execute(
                'INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (op, target_id, principal['caller'], canonical(principal), canonical(fixed_args), canonical(policy),
                 'running', 1, self.clock(), self.clock(), None),
            )
            self.db.execute(
                'INSERT INTO requests(caller,request_id,digest,operation,at) VALUES(?,?,?,?,?)',
                (principal['caller'], request_id, digest, op, self.clock()),
            )
            if kind in ('ensure', 'restart'):
                # Asking for the target again renews the lease and ends a stop-hold.
                self.db.execute('DELETE FROM stops WHERE target=?', (target_id,))
                if not principal.get('internal'):
                    self.db.execute('INSERT OR REPLACE INTO touches VALUES(?,?)', (target_id, self.clock()))
                    # Someone asked for it: supervision resumes with a fresh restart budget.
                    self.db.execute('DELETE FROM gave_up WHERE target=?', (target_id,))
                    self.db.execute('DELETE FROM failed_starts WHERE target=?', (target_id,))
        self._submit(op, 1)
        if self.on_admit:
            try:
                self.on_admit({
                    'operationId': op,
                    'targetId': target_id,
                    'kind': kind,
                    'caller': principal['caller'],
                    'reason': reason,
                    'traceparent': traceparent,
                })
            except Exception:  # noqa: BLE001 - a journal failure never undoes an admission
                pass
        # The admitting call answers with its receipt; reading an operation by id later is `lifecycle.operation.get`.
        return self._view(principal, op)

    def _receipt(self, principal: dict, tool: str, request_id: str, digest: str) -> Optional[dict]:
        with self.db_lock:
            existing = self.db.execute(
                'SELECT * FROM requests WHERE caller=? AND request_id=?', (principal['caller'], request_id)
            ).fetchone()
        if existing is None:
            return None
        if existing['digest'] != digest:
            raise LifecycleError('idempotency_conflict')
        return self._view(principal, existing['operation'], tool)

    @staticmethod
    def _closure(document: dict, target_id: str) -> dict:
        targets: dict = {}
        todo = [target_id]
        while todo:
            key = todo.pop()
            if key in targets or key not in document['targets']:
                continue
            targets[key] = document['targets'][key]
            todo += targets[key]['dependencies']
        return {'schemaVersion': document['schemaVersion'], 'targets': targets}

    def get(self, principal: dict, operation_id: str) -> dict:
        valid_id(operation_id)
        if not principal.get('internal'):
            self.authority.check(principal, 'lifecycle.operation.get')
        return self._view(principal, operation_id, 'lifecycle.operation.get')

    def _view(self, principal: dict, operation_id: str, tool: Optional[str] = None) -> dict:
        """The caller's own operation as its receipt shows it. With `tool`, the caller must still be allowed that
        tool on the operation's target; without, the caller has just been admitted for it."""
        with self.db_lock:
            row = self.db.execute(
                'SELECT * FROM operations WHERE id=? AND caller=?', (operation_id, principal['caller'])
            ).fetchone()
        if row is None:
            raise LifecycleError('not_found')
        if tool is not None and not principal.get('internal'):
            self._target(principal, tool, row['target'])
        result = json.loads(row['result']) if row['result'] else {}
        arguments = json.loads(row['arguments'])
        return {
            'operationId': row['id'],
            'targetId': row['target'],
            'kind': arguments.get('_kind', 'ensure'),
            'state': row['state'],
            'generation': row['generation'],
            'createdAt': int(row['created'] * 1000),
            'updatedAt': int(row['updated'] * 1000),
            'policyRevision': arguments['expectedPolicyRevision'],
            'grantRevision': principal['grantRevision'],
            'traceparent': arguments.get('_traceparent'),
            **result,
        }

    # -- workers -----------------------------------------------------------------------------------------------

    def _observation(self, state: str, **extra) -> dict:
        return {
            'state': state,
            'processReady': False,
            'transportReady': False,
            'capabilityReady': False,
            'authState': 'unknown',
            'observedAt': int(self.clock() * 1000),
            **extra,
        }

    def _finish(self, operation_id: str, generation: int, result: dict) -> None:
        with self.db_lock, self.db:
            # A worker from an earlier process or generation cannot complete the new lease.
            self.db.execute(
                "UPDATE operations SET state=?,result=?,updated=? WHERE id=? AND generation=? AND state='running'",
                (result['state'], canonical(result), self.clock(), operation_id, generation),
            )

    def _work(self, operation_id: str, generation: int) -> None:
        with self.db_lock:
            row = self.db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        principal, args, policy = (json.loads(row[k]) for k in ('principal', 'arguments', 'policy'))
        kind = args.get('_kind', 'ensure')
        deadline = args['deadlineAt'] / 1000
        if generation > 1 and deadline <= self.clock():
            # Recovered after its deadline: the journal and the restart's phase say what already happened, so the
            # rest still gets the target's own budget instead of ending `timeout` before it looks.
            own = (policy.get('targets', {}).get(args['targetId']) or {}).get('timeoutSeconds', 1)
            deadline = self.clock() + own * (2 if kind == 'restart' else 1)
        try:
            if kind == 'ensure':
                result = self._drive(principal, args['targetId'], deadline, policy, operation_id, generation)
            elif kind == 'stop':
                result = self._drive_stop(
                    principal, args['targetId'], deadline, policy, operation_id, generation,
                    force=args.get('force', False), tool='lifecycle.stop', idle=args.get('_reason') == 'idle',
                )
            else:
                result = self._drive_restart(principal, args['targetId'], deadline, policy, operation_id, generation)
        except _Interrupted:
            # The broker is closing. The operation stays `running`; the next process recovers it under a new
            # generation, and the effect journal says what already happened.
            return
        except LifecycleError as exc:
            result = self._observation(exc.code)
        except Exception:  # noqa: BLE001
            # Never echo raw OS output, request bodies, credential paths or secrets into a receipt.
            result = self._observation('failed')
        if self.closed and result.get('state') == 'timeout':
            return  # a wait cut short by shutdown is not the operation's answer
        self._finish(operation_id, generation, result)
        if self.on_finish:
            try:
                self.on_finish(result, args.get('_traceparent'), {
                    'targetId': args['targetId'],
                    'kind': kind,
                    'caller': principal['caller'],
                    'reason': args.get('_reason'),
                    'operationId': operation_id,
                })
            except Exception:  # noqa: BLE001 - a journal failure never changes a finished operation
                pass

    def _fence(self, principal: dict, target_id: str, policy: dict, op: str, generation: int,
               tool: str = 'lifecycle.ensure_running', allow_held: bool = False) -> dict:
        if not principal.get('internal'):
            self.authority.check(principal, tool, target_id)
        current = self._target(principal, tool, target_id)
        if current != policy['targets'].get(target_id):
            raise LifecycleError('policy_changed')
        if current['desiredState'] == 'held_off' and not allow_held:
            raise LifecycleError('held_off')
        with self.db_lock:
            row = self.db.execute('SELECT state,generation FROM operations WHERE id=?', (op,)).fetchone()
            if not row or row['state'] != 'running' or row['generation'] != generation:
                raise LifecycleError('denied')
        return current

    def _lock(self, target_id: str) -> threading.Lock:
        with self.db_lock:
            return self.locks.setdefault(target_id, threading.Lock())

    def _launch(self, target_id: str):
        with self.db_lock:
            return self.db.execute('SELECT * FROM launches WHERE target=?', (target_id,)).fetchone()

    def _record_launch(self, target_id: str, observation: dict, op: str, principal: dict) -> None:
        if not observation.get('pid'):
            return
        ready = self.clock() if observation.get('state') in ('ready', 'already_ready') else None
        with self.db_lock, self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO launches(target,pid,at,operation,caller,adopted,ready_at) VALUES(?,?,?,?,?,NULL,?)',
                (target_id, observation['pid'], self.clock(), op, principal['caller'], ready),
            )

    def note_activation(self, target_ids, pid: int) -> None:
        """The person brought `pid` forward. Past the launch grace, the broker's process is theirs."""
        with self.db_lock, self.db:
            for target_id in target_ids:
                self.db.execute(
                    'UPDATE launches SET adopted=? WHERE target=? AND pid=? AND adopted IS NULL AND at<=?',
                    (self.clock(), target_id, pid, self.clock() - ADOPT_GRACE_SECONDS),
                )

    @staticmethod
    def _person_present(observation: dict) -> bool:
        idle = observation.get('inputIdleSeconds')
        return observation.get('active') is True and (idle is None or idle < PERSON_PRESENT_SECONDS)

    def _forget_launch(self, target_id: str) -> None:
        with self.db_lock, self.db:
            self.db.execute('DELETE FROM launches WHERE target=?', (target_id,))

    def _identity(self, target: dict, deadline: float) -> bool:
        return self.adapter.identity_matches(target, deadline=deadline)

    def _resolve_dispatch(self, target_id: str, observation: dict) -> bool:
        """Settle an unknown dispatch from what is observed now.

        A live pid means the start landed, whatever its readiness: the outcome is known. A target still stopped
        after DISPATCH_RECONCILE_SECONDS means the start did not survive; starting it again is no longer a
        duplicate. Returns True while the journal entry still holds."""
        with self.db_lock:
            effect = self.db.execute('SELECT * FROM effects WHERE target=?', (target_id,)).fetchone()
        if not effect or effect['state'] != 'dispatching':
            return False
        landed = bool(observation.get('pid')) or observation.get('state') in ('ready', 'already_ready')
        expired = observation.get('state') == 'stopped' and self.clock() - effect['at'] > DISPATCH_RECONCILE_SECONDS
        if landed or expired:
            with self.db_lock, self.db:
                self.db.execute('DELETE FROM effects WHERE target=? AND operation=?', (target_id, effect['operation']))
            return False
        return True

    def _probe_patiently(self, target: dict) -> dict:
        """A probe during our own start: a probe that times out is progress, not an answer."""
        try:
            return self.adapter.probe(target)
        except LifecycleError as exc:
            if exc.code in ('delivery_unknown', 'timeout'):
                return self._observation('starting')
            raise

    def _drive(self, principal: dict, target_id: str, deadline: float, policy: dict, op: str, generation: int,
               tool: str = 'lifecycle.ensure_running', count_attempt: bool = True) -> dict:
        target = self._fence(principal, target_id, policy, op, generation, tool)
        for dependency in target['dependencies']:
            try:
                result = self._drive(principal, dependency, deadline, policy, op, generation, tool)
            except LifecycleError as exc:
                result = self._observation(exc.code)
            if result['state'] not in ('ready', 'already_ready'):
                return self._observation('dependency_failed', dependencyTarget=dependency, dependencyState=result['state'])
        lock = self._lock(target_id)
        remaining = deadline - self.clock()
        if remaining <= 0 or not lock.acquire(timeout=max(0, remaining)):
            return self._observation('timeout')
        try:
            target = self._fence(principal, target_id, policy, op, generation, tool)
            if self.clock() >= deadline:
                return self._observation('timeout')
            if not self._identity(target, deadline):
                return self._observation('identity_mismatch')
            observation = self.adapter.probe(target)
            if observation['state'] in ('ready', 'already_ready'):
                with self.db_lock, self.db:
                    effect = self.db.execute('SELECT operation FROM effects WHERE target=?', (target_id,)).fetchone()
                    self.db.execute('DELETE FROM effects WHERE target=?', (target_id,))
                    self.db.execute('DELETE FROM circuits WHERE target=?', (target_id,))
                if effect is not None and effect['operation'] == op:
                    # This operation's own start, recovered after a crash between the effect and its receipt: the
                    # process is the broker's, as if the answer had been written.
                    self._record_launch(target_id, observation, op, principal)
                self._reconcile_launch(target_id, observation)
                self._note_ready(target, observation)
                return observation | {'state': 'already_ready', 'action': 'none'}
            # A target that is already starting may be completing its own startup: wait, without a second effect.
            while observation['state'] == 'starting' and self.clock() < deadline and not self.closed:
                self._fence(principal, target_id, policy, op, generation, tool)
                time.sleep(min(0.1, max(0, deadline - self.clock())))
                observation = self.adapter.probe(target)
            if observation['state'] == 'starting':
                return self._observation('timeout', processReady=observation.get('processReady', False))
            if observation['state'] in ('ready', 'already_ready'):
                self._reconcile_launch(target_id, observation)
                return observation | {'state': 'already_ready', 'action': 'none'}
            # Alive but unready, waiting for a sign-in, of another identity, switched off: never a reason to start.
            if observation['state'] != 'stopped':
                self._resolve_dispatch(target_id, observation)
                return observation
            self._note_exit(target_id)
            if self._resolve_dispatch(target_id, observation):
                return self._observation('delivery_unknown', action='reconcile_only')
            with self.db_lock:
                circuit = self.db.execute('SELECT until_at FROM circuits WHERE target=?', (target_id,)).fetchone()
            if circuit and circuit['until_at'] > self.clock():
                return self._observation('circuit_open')
            target = self._fence(principal, target_id, policy, op, generation, tool)
            if not self._identity(target, deadline):
                return self._observation('identity_mismatch')
            if self.closed:
                raise _Interrupted()
            with self._write():
                # The journal is read again inside the transaction that writes it: an unknown dispatch another
                # operation recorded since the check above is never followed by a second start.
                pending = self.db.execute(
                    "SELECT operation FROM effects WHERE target=? AND state='dispatching'", (target_id,)
                ).fetchone()
                if pending is not None and pending['operation'] != op:
                    return self._observation('delivery_unknown', action='reconcile_only')
                count = self.db.execute(
                    'SELECT count(*) FROM attempts WHERE target=? AND at>?',
                    (target_id, self.clock() - CRASH_WINDOW_SECONDS),
                ).fetchone()[0]
                if count_attempt and count >= 2:
                    self.db.execute('INSERT OR REPLACE INTO circuits VALUES(?,?)',
                                    (target_id, self.clock() + CIRCUIT_SECONDS))
                    return self._observation('circuit_open')
                if count_attempt:
                    self.db.execute('INSERT INTO attempts VALUES(?,?)', (target_id, self.clock()))
                # Committed BEFORE the only effect: a crash in between is a conservative unknown, never a second start.
                self.db.execute('INSERT OR REPLACE INTO effects VALUES(?,?,?,?)',
                                (target_id, 'dispatching', self.clock(), op))
            self._fence(principal, target_id, policy, op, generation, tool)
            try:
                action = self.adapter.start(target)
            except LifecycleError as exc:
                # An explicit no-effect refusal is retryable under the circuit's budget.
                if exc.code in ('held_off', 'login_required', 'not_installed', 'identity_mismatch', 'unsupported_platform'):
                    with self.db_lock, self.db:
                        self.db.execute('DELETE FROM effects WHERE target=?', (target_id,))
                    return self._observation(exc.code)
                self._failed_start(principal, target_id, op, 'delivery_unknown')
                return self._observation('delivery_unknown')
            last = None
            while self.clock() < deadline and not self.closed:
                self._fence(principal, target_id, policy, op, generation, tool)
                observation = self._probe_patiently(target)
                if observation['state'] in ('ready', 'already_ready'):
                    with self.db_lock, self.db:
                        self.db.execute('DELETE FROM effects WHERE target=?', (target_id,))
                    # This process is the broker's: an agent may stop it, and it may idle out.
                    self._record_launch(target_id, observation, op, principal)
                    return observation | {'state': 'ready', 'action': action}
                if observation['state'] not in START_TRANSIENT:
                    # Another identity, switched off, waiting for a sign-in: no amount of waiting fixes it.
                    self._resolve_dispatch(target_id, observation)
                    self._failed_start(principal, target_id, op, observation['state'])
                    return observation | {'action': action}
                # Still coming up (the window is there, its capability is not yet; the port is not yet bound).
                last = observation
                time.sleep(min(0.2, max(0, deadline - self.clock())))
            if self.closed:
                raise _Interrupted()
            if last is not None and last['state'] not in ('stopped', 'starting'):
                # The process is there but never became ready: report what it said, not "timeout".
                self._resolve_dispatch(target_id, last)
                if last.get('pid'):
                    # Not a failed start yet: a slow start may still serve. It is one if the process exits before it
                    # is seen ready (`_note_exit`).
                    self._record_launch(target_id, last, op, principal)
                else:
                    self._failed_start(principal, target_id, op, last['state'])
                return last | {'action': action}
            self._failed_start(principal, target_id, op, 'timeout')
            return self._observation('timeout', action=action, delivery='unknown_until_reconciled')
        finally:
            lock.release()

    def _reconcile_launch(self, target_id: str, observation: dict) -> None:
        # A process the broker started that has since been replaced (the person relaunched it, or it crashed and
        # something else started it) is no longer the broker's.
        launch = self._launch(target_id)
        if launch and observation.get('pid') and launch['pid'] != observation['pid']:
            self._forget_launch(target_id)

    def _drive_stop(self, principal: dict, target_id: str, deadline: float, policy: dict, op: str, generation: int,
                    *, force: bool, tool: str, record_stop: bool = True, idle: bool = False) -> dict:
        target = self._fence(principal, target_id, policy, op, generation, tool, allow_held=True)
        if (
            record_stop
            and target['desiredState'] == 'always_on'
            and not principal.get('internal')
            and not self._holds_always_on(principal)
        ):
            # always_on is the owner's decision; a stop holds supervision off, so only the owner (or the operator's
            # proxy) may make it, also when the target already happens to be down.
            return self._observation('denied', reason='always_on_is_owner_only')
        lock = self._lock(target_id)
        remaining = deadline - self.clock()
        if remaining <= 0 or not lock.acquire(timeout=max(0, remaining)):
            return self._observation('timeout')
        try:
            target = self._fence(principal, target_id, policy, op, generation, tool, allow_held=True)
            if idle and target.get('idleStopMinutes'):
                # The idle decision was made before this lock; a caller may have renewed the lease since. Read it
                # again here, where nothing can renew it under us.
                with self.db_lock:
                    touch = self.db.execute('SELECT at FROM touches WHERE target=?', (target_id,)).fetchone()
                if touch and self.clock() - touch['at'] < target['idleStopMinutes'] * 60:
                    return self._observation('lease_renewed', action='none')
            # Never signal a process whose identity the broker cannot vouch for.
            if not self._identity(target, deadline):
                return self._observation('identity_mismatch')
            observation = self.adapter.probe(target)
            if observation['state'] == 'stopped':
                self._forget_launch(target_id)
                if record_stop:
                    self._mark_stop(target_id, principal)
                return self._observation('already_stopped', action='none')
            pid = observation.get('pid')
            if not pid:
                # Waiting for a sign-in, not set up, another identity: there is nothing to signal.
                return observation
            launch = self._launch(target_id)
            owned = bool(launch) and launch['pid'] == pid
            if not self._is_owner(principal, tool):
                if not owned:
                    return self._observation('in_use_by_person', reason='not_started_by_broker', pid=pid)
                if launch['adopted'] is not None:
                    return self._observation('in_use_by_person', reason='taken_over_by_person', pid=pid)
                if self._person_present(observation):
                    return self._observation('in_use_by_person', reason='frontmost', pid=pid)
            if record_stop:
                self._mark_stop(target_id, principal)
            self._fence(principal, target_id, policy, op, generation, tool, allow_held=True)
            # A forced stop keeps the last two seconds of its budget for the kill.
            graceful_end = deadline - 2 if force else deadline
            try:
                action = self.adapter.stop(target, pid, force=False)
            except LifecycleError as exc:
                return self._observation(exc.code)
            if action == 'quit_requested':
                # The app's own quit gets a few seconds; then the signal every app must honour.
                stopped = self._wait_stopped(target, min(graceful_end, self.clock() + QUIT_GRACE_SECONDS))
                if not stopped:
                    try:
                        action = self.adapter.stop(target, pid, force=False, mode='term')
                    except LifecycleError as exc:
                        if exc.code != 'identity_mismatch':
                            return self._observation(exc.code)
                        action = 'quit_requested'           # it exited between the probe and the signal
            stopped = self._wait_stopped(target, graceful_end)
            if not stopped and force:
                try:
                    action = self.adapter.stop(target, pid, force=True)
                except LifecycleError as exc:
                    return self._observation(exc.code)
                stopped = self._wait_stopped(target, deadline)
            if stopped:
                self._forget_launch(target_id)
                return self._observation('stopped', action=action)
            return self._observation('stop_timeout', action=action, pid=pid)
        finally:
            lock.release()

    def _wait_stopped(self, target: dict, until: float) -> bool:
        while not self.closed:
            if self.adapter.probe(target)['state'] == 'stopped':
                return True
            if self.clock() >= until:
                return False
            time.sleep(min(0.1, max(0, until - self.clock())))
        return False

    def _mark_stop(self, target_id: str, principal: dict) -> None:
        with self.db_lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO stops VALUES(?,?,?)', (target_id, self.clock(), principal['caller']))
            # A deliberate stop explains the next start; it is not a crash.
            self.db.execute('DELETE FROM attempts WHERE target=?', (target_id,))

    def _drive_restart(self, principal: dict, target_id: str, deadline: float, policy: dict, op: str,
                       generation: int) -> dict:
        tool = 'lifecycle.restart'
        with self.db_lock:
            phase = self.db.execute('SELECT phase FROM phases WHERE operation=?', (op,)).fetchone()
        if phase and phase['phase'] == 'stopped':
            # Recovered after the stop half: stopping again would kill the new launch.
            stopped = {'state': 'stopped'}
        else:
            # Half the budget for the stop, the rest for the start: a hung quit cannot eat the start.
            stop_deadline = self.clock() + (deadline - self.clock()) / 2
            stopped = self._drive_stop(principal, target_id, stop_deadline, policy, op, generation,
                                       force=False, tool=tool, record_stop=False)
            if stopped['state'] not in ('stopped', 'already_stopped'):
                return stopped
            with self.db_lock, self.db:
                self.db.execute('INSERT OR REPLACE INTO phases VALUES(?,?,?)', (op, 'stopped', self.clock()))
        # A restart is a deliberate relaunch: it does not spend the crash circuit's budget a failed start would, and
        # it clears an earlier stop-hold.
        with self.db_lock, self.db:
            self.db.execute('DELETE FROM stops WHERE target=?', (target_id,))
        started = self._drive(principal, target_id, deadline, policy, op, generation, tool, count_attempt=False)
        return started | {'stopState': stopped['state']}

    # -- supervision (always_on, idle) -------------------------------------------------------------------------

    def live_launches(self) -> int:
        with self.db_lock:
            return self.db.execute('SELECT count(*) FROM launches').fetchone()[0]

    def _running_op(self, target_id: str) -> bool:
        with self.db_lock:
            return self.db.execute(
                "SELECT 1 FROM operations WHERE target=? AND state='running'", (target_id,)
            ).fetchone() is not None

    def _admit_internal(self, target: dict, kind: str, reason: str) -> str:
        args = {
            'targetId': target['targetId'],
            # The target id is hashed so a long id still leaves the request id inside the 128-character pattern.
            'requestId': f"{reason}:{hashlib.sha256(target['targetId'].encode()).hexdigest()[:16]}:"
                         f"{int(self.clock() * 1000)}",
            'expectedPolicyRevision': target['policyRevision'],
            'grantRevision': 0,
        }
        return self._admit(SUPERVISOR, args, kind, reason=reason)['operationId']

    def supervise(self, only=None) -> list:
        """One supervision pass; returns the operation ids it admitted. Step L4 runs it on a cadence.

        always_on: a stopped target is started again, unless someone stopped it on purpose (`stops`), its crash
        circuit is open or its restart budget is spent. Idle: a target with `idleStopMinutes` that the broker
        started, that the person has not taken over and is not using, and that no caller renewed for that long is
        stopped gracefully. A process the person opened is never touched. A target that needs neither costs no probe.
        """
        if self.closed:
            return []
        admitted: list = []
        try:
            targets = self._registry()['targets']
        except LifecycleError:
            return []
        now = self.clock()
        for target_id, target in targets.items():
            if only is not None and target_id not in only:
                continue
            if target['desiredState'] == 'held_off' or target['approved'] is not True:
                continue
            if self._running_op(target_id):
                continue
            try:
                if target['desiredState'] == 'always_on':
                    admitted += self._supervise_always_on(target, now)
                elif target.get('idleStopMinutes'):
                    admitted += self._supervise_idle(target, now)
            except LifecycleError:
                continue
        return admitted

    def _supervise_always_on(self, target: dict, now: float) -> list:
        target_id = target['targetId']
        with self.db_lock:
            held = self.db.execute('SELECT 1 FROM stops WHERE target=?', (target_id,)).fetchone()
            circuit = self.db.execute('SELECT until_at FROM circuits WHERE target=?', (target_id,)).fetchone()
        if held or (circuit and circuit['until_at'] > now):
            return []
        gave_up = self._gave_up(target_id)
        if gave_up and gave_up['policy'] != target['policyRevision']:
            self._rearm(target_id)                          # the owner changed the policy: that is acting on it
        observation = self.adapter.probe(target)
        if observation['state'] == 'stopped':
            self._note_exit(target_id)
        else:
            self._note_ready(target, observation)
        # An unknown dispatch is settled by what is observed, never left to disable supervision for good.
        if self._resolve_dispatch(target_id, observation) or observation['state'] != 'stopped':
            return []
        if self._budget_spent(target, now):
            return []
        return [self._admit_internal(target, 'ensure', 'always_on')]

    # -- the always_on restart budget --------------------------------------------------------------------------

    def _gave_up(self, target_id: str):
        with self.db_lock:
            return self.db.execute('SELECT * FROM gave_up WHERE target=?', (target_id,)).fetchone()

    def _rearm(self, target_id: str) -> None:
        with self.db_lock, self.db:
            self.db.execute('DELETE FROM gave_up WHERE target=?', (target_id,))
            self.db.execute('DELETE FROM failed_starts WHERE target=?', (target_id,))

    def _failed_start(self, principal: dict, target_id: str, op: str, cause: str, at: Optional[float] = None) -> None:
        """Count one supervisor start as failed. A caller's starts are the caller's business."""
        if principal.get('caller') != SUPERVISOR['caller'] or not principal.get('internal'):
            return
        with self.db_lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO failed_starts VALUES(?,?,?,?)',
                            (op, target_id, self.clock() if at is None else at, cause))

    def _note_exit(self, target_id: str) -> None:
        """The broker's process is gone. A supervisor start whose process never served, or that exited within
        EARLY_EXIT_SECONDS, was a failed start; then the launch is forgotten."""
        launch = self._launch(target_id)
        if launch and launch['caller'] == SUPERVISOR['caller']:
            if launch['ready_at'] is None:
                cause = 'exited_before_ready'
            elif self.clock() - launch['at'] < EARLY_EXIT_SECONDS:
                cause = 'exited_early'
            else:
                cause = None
            if cause:
                self._failed_start(SUPERVISOR, target_id, launch['operation'], cause, at=launch['at'])
        self._forget_launch(target_id)

    def _note_ready(self, target: dict, observation: dict) -> None:
        """A ready observation: the broker's process served at least once, and a target seen serving again
        (readiness stronger than a pid) is supervised again."""
        if observation.get('state') not in ('ready', 'already_ready'):
            return
        target_id = target['targetId']
        if observation.get('pid'):
            with self.db_lock, self.db:
                self.db.execute(
                    'UPDATE launches SET ready_at=? WHERE target=? AND pid=? AND ready_at IS NULL',
                    (self.clock(), target_id, observation['pid']),
                )
        # With `process` readiness a pid is "ready" even inside a failure dialog: the person opening the app to see
        # why must not restart the loop. Only a real serving probe re-arms.
        if target['readiness']['kind'] != 'process' and self._gave_up(target_id):
            self._rearm(target_id)

    def _budget_spent(self, target: dict, now: float) -> bool:
        target_id = target['targetId']
        if self._gave_up(target_id):
            return True
        with self.db_lock:
            rows = self.db.execute(
                'SELECT cause FROM failed_starts WHERE target=? AND at>? ORDER BY at',
                (target_id, now - RESTART_WINDOW_SECONDS),
            ).fetchall()
        if len(rows) < RESTART_BUDGET:
            return False
        with self.db_lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO gave_up VALUES(?,?,?,?)',
                            (target_id, now, target['policyRevision'], len(rows)))
        if self.on_gave_up:
            try:
                self.on_gave_up({
                    'targetId': target_id,
                    'failedStarts': len(rows),
                    'windowSeconds': RESTART_WINDOW_SECONDS,
                    'causes': [r['cause'] for r in rows],
                })
            except Exception:  # noqa: BLE001 - a journal failure never re-arms the loop
                pass
        return True

    def _supervise_idle(self, target: dict, now: float) -> list:
        target_id = target['targetId']
        launch = self._launch(target_id)
        if not launch:
            return []
        with self.db_lock:
            touch = self.db.execute('SELECT at FROM touches WHERE target=?', (target_id,)).fetchone()
        last = max(launch['at'], touch['at'] if touch else 0)
        if now - last < target['idleStopMinutes'] * 60:
            return []
        if launch['adopted'] is not None:
            return []                                       # the person took it over: never idled out
        observation = self.adapter.probe(target)
        if observation['state'] == 'stopped' or observation.get('pid') != launch['pid']:
            self._forget_launch(target_id)
            return []
        idle_input = observation.get('inputIdleSeconds')
        if observation.get('active') is True and (idle_input is None or idle_input < target['idleStopMinutes'] * 60):
            return []                                       # frontmost, and the person was there inside the window
        return [self._admit_internal(target, 'stop', 'idle')]

    def close(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """Stop admitting, cancel what has not started, give running work `grace` seconds.

        Nothing new is dispatched once `closed` is set; unstarted work is cancelled and stays `running` in the
        store, and the next process recovers it under a new generation."""
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        with self.db_lock:
            pending = list(self.futures)
        close = getattr(self.adapter, 'close', None)
        if callable(close):
            close()                                         # ends a running child at once: no wait, no orphan
        concurrent.futures.wait(pending, timeout=grace)
        with self.db_lock:
            self.db.close()
        self._release_instance_lock()
