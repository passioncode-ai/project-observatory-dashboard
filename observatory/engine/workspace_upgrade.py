"""Private snapshots and forward-only upgrades; restore always creates a new home."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import oslocks
import osprivacy
import hashlib
import json
import ntpath
import os
import posixpath
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid

import backup_vault
import configuration as config
import machine_identity
import secret_store_carry
import workspace
from store import compatibility

SNAPSHOT_FORMAT = 1
JOURNAL = 'upgrade-in-progress.json'
EXCLUDED_DIRS = {'.git', '__pycache__', '.venv', 'backups', 'migration-backups'}
#: What a snapshot restored on ANOTHER machine never takes (OBS-284): this machine's agent-network
#: identity (`node/device/` from `full node --init`), its organization enrollments with their own
#: keys (`node/orgs/`), the estate state it verified (`node/records/`), its shares with other people
#: (`node/shares/`: a received share is sealed to this machine, contract DEC-0142, and a sent one is
#: renewed and revoked from the machine that made it), the mailbox it deployed and its owner key
#: (`config/cloud.json` and `node/shares/owner/`: the second machine deploys its own under another
#: name), and the record of its own maintenance runs. The new machine makes its own with
#: `full node --init`, `full org enroll` and `full cloud deploy`. Its scheduler (`features.scheduler`)
#: is turned off, every `always_on` app it enrolled is held off (the lifecycle supervisor would start
#: it here too), and the workspace gets its own `instance_id`, so a tick never runs on two machines
#: at once and the two machines' daily backups, which may share one iCloud folder, never rotate each
#: other's files.
#: The anonymous usage data's install id and unsent queue (`store/analytics/`, OBS-324) stay behind too:
#: two machines with one install id would read as one install, and the queue would be sent twice. The
#: person's switch, the file `analytics` in the home, moves with the workspace.
#: So does the backup passphrase (`secrets/backup-passphrase`, OBS-348): each machine keeps its own, so
#: one leaked passphrase never opens both machines' backups. Restored here, `ensure_passphrase` would
#: have taken the other machine's as this one's and mirrored it into this machine's store; without it the
#: next `full init` or maintenance pass makes this machine's own, as on a fresh install.
#: Said in every second-machine restore answer (OBS-348 review): the first machine's passphrase no longer
#: travels, so a retired first machine would take with it the only key to its backups, this snapshot's included.
OTHER_PASSPHRASE_NOTE = ("this machine does not keep the other machine's backup passphrase: if that machine may be "
                         "retired, keep its passphrase somewhere off both machines now, because its backups, this "
                         "snapshot included, open only with it")
ANOTHER_MACHINE_LEAVES = ('node/device', 'node/orgs', 'node/records', 'node/shares', 'config/cloud.json',
                          'store/maintenance.json', 'store/analytics', 'secrets/backup-passphrase')
LIFECYCLE = 'config/node-lifecycle.json'


def digest(file: Path) -> str:
    h = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


#: Files that describe this machine's own last restore and never travel in a snapshot (OBS-350): restored
#: elsewhere, another run's record would say another machine's store is missing.
EXCLUDED_FILES = {'store/raw/secret-store-restore.json'}


def excluded(path: Path) -> bool:
    return (any(p in EXCLUDED_DIRS for p in path.parts) or path.name in {'.workspace.lock', 'tick.lock', 'service.lock'}
            or path.name.endswith(('.upgrade.lock', '-shm')) or PurePosixPath(*path.parts).as_posix() in EXCLUDED_FILES)


def inventory(base: Path) -> dict[str, tuple[int, int, str]]:
    """Detect movement while copying; refuse links and special files rather than follow them."""
    result = {}
    for directory, children, files in os.walk(base,followlinks=False):
        current = Path(directory)
        kept = []
        for name in sorted(children):
            file = current / name
            if excluded(file.relative_to(base)):
                continue
            if file.is_symlink():
                raise config.ConfigurationError('Snapshot refuses symbolic links')
            kept.append(name)
        children[:] = kept
        for name in sorted(files):
            file = current / name
            rel = file.relative_to(base)
            if excluded(rel):
                continue
            if file.is_symlink():
                raise config.ConfigurationError('Snapshot refuses symbolic links')
            if not stat.S_ISREG(file.stat().st_mode):
                raise config.ConfigurationError('Snapshot refuses special files')
            info = file.stat()
            result[rel.as_posix()] = (info.st_size,info.st_mtime_ns,digest(file))
    return result


def directories(base: Path) -> set[str]:
    found = set()
    for directory, children, _ in os.walk(base,followlinks=False):
        current = Path(directory)
        children[:] = [name for name in children if not excluded((current/name).relative_to(base))]
        for name in children:
            file = current/name
            if file.is_symlink():
                raise config.ConfigurationError('Snapshot refuses symbolic links')
            found.add(file.relative_to(base).as_posix())
    return found


def managed_layout(base: Path) -> None:
    """Do not claim a full-home backup while legacy overrides point elsewhere."""
    expected = {"OBSERVATORY_DB": base / "store/observatory.db",
                "OBSERVATORY_STATE": base / "store", "OBSERVATORY_REGISTRY": base / "registry"}
    for name, value in expected.items():
        override = os.environ.get(name)
        if override and Path(override).expanduser().absolute() != value.absolute():
            raise config.ConfigurationError(f"{name} overrides the managed layout; migrate it into OBSERVATORY_HOME before upgrading")


def sync_directory(path: Path) -> None:
    fd = osprivacy.open(path,os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(base: Path) -> None:
    directories = [base]
    for file in base.rglob('*'):
        if file.is_dir():
            directories.append(file)
        else:
            fd = osprivacy.open(file,os.O_RDONLY|osprivacy.NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in reversed(directories):
        sync_directory(directory)


def preflight(base: Path) -> dict:
    workspace.require_runtime()
    workspace.reject_symlinks(base)
    marker = config.validate_workspace(base, required=True)
    config.load(base)
    config.validate_registries(base / 'registry')
    if (base / JOURNAL).exists():
        raise config.ConfigurationError('Interrupted upgrade: restore the verified snapshot into a new home')
    compatibility.preflight(base / 'store/observatory.db')
    return marker


@contextlib.contextmanager
def operation_lock(base: Path, *, existing: bool = True):
    """Stable sibling lock plus existing workspace/tick locks, never a swapped inode."""
    workspace.reject_symlinks(base)
    base.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    locks = [base.parent / ('.' + base.name + '.observatory-operation.lock')]
    if existing:
        locks += [base / '.workspace.lock', base / 'store/tick.lock']
    handles = []
    try:
        for file in locks:
            file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = osprivacy.open(file, os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            handles.append(fd)
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
            except BlockingIOError:
                raise config.ConfigurationError('Workspace or scheduler is busy; stop writers and retry') from None
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)


def sqlite_file(file: Path) -> bool:
    with file.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\x00'


#: A copy is verified before it counts. A session MCP server can write to the store while
#: the tick and server are stopped, and a reader opened `immutable` (an empty WAL) does not
#: see that write coming, so the copy can come out torn and fail its integrity check — seen
#: live on 2026-10-05. The source is never touched, so copying again is safe.
COPY_ATTEMPTS = 3


def copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for attempt in range(COPY_ATTEMPTS):
        fd = osprivacy.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | osprivacy.NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with contextlib.closing(sqlite3.connect(target)) as dst:
                # `backup` replaces the whole destination, so a re-read starts clean.
                compatibility.read_consistently(
                    source, lambda src: (src.backup(dst), compatibility.verify_database(dst)))
            return
        except (sqlite3.DatabaseError, RuntimeError) as exc:
            target.unlink(missing_ok=True)
            # A torn copy reads as malformed, or as a failed integrity check; a missing
            # sqlite-vec package is not torn and is not retried.
            torn = isinstance(exc, sqlite3.DatabaseError) or "integrity verification failed" in str(exc)
            if not torn or attempt == COPY_ATTEMPTS - 1:
                raise
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def require_stopped(writers_stopped: bool) -> None:
    if not writers_stopped:
        raise config.ConfigurationError('Stop foreground/background writers, then pass --writers-stopped for a cross-file snapshot')


LOG_SUFFIXES = ('.log', '.err', '.out', '.jsonl')
#: What the session hooks write on every agent turn, whether or not the tick and the
#: server are stopped: the Stop hook's receipt (`tools/record_turn.py`), the folders a
#: session opened in (`tools/session_start.py`), a hook's fault log
#: (`companion_faults.py`) and the journal of requests for a why (`why_asks.py`). Before 0.19.2 a write to any of them while an update was
#: staged refused the update, though no update reads them. They are copied, never
#: required to hold still; `test_every_receipt_a_session_hook_writes_is_exempt` keeps
#: this set equal to what those modules write.
SESSION_RECEIPTS = frozenset({'store/raw/record-turn.json', 'store/raw/sessions-seen.jsonl',
                              'store/raw/companion-faults.jsonl', 'store/raw/why-asks.jsonl',
                              'store/raw/why-asks.jsonl.1', 'store/raw/why-asks.jsonl.lock'})


def volatile(name: str, databases: set[str]) -> bool:
    """A path the snapshot copies without requiring it to hold still: a database and its
    WAL companions, an append-only log (with its rotated generations) under store/logs,
    or a receipt the session hooks write (SESSION_RECEIPTS)."""
    if name in databases or (name.endswith(('-wal', '-journal', '-shm')) and name.rsplit('-', 1)[0] in databases):
        return True
    if name in SESSION_RECEIPTS:
        return True
    path = PurePosixPath(name)
    if path.parts[:2] != ('store', 'logs'):
        return False
    stem, _, tail = path.name.rpartition('.')
    return path.suffix in LOG_SUFFIXES or (tail.isdigit() and PurePosixPath(stem).suffix in LOG_SUFFIXES)


def _snapshot(base: Path, output: Path, *, carry_stores: bool = False,
              not_carried: str = 'plaintext-output') -> dict:
    """Copy the home into a new snapshot directory at `output`.

    `carry_stores` copies each secret store the settings name outside the workspace into
    `external/<name>/` (OBS-350); only `snapshot()` asks for it, and only for a snapshot it is
    about to encrypt. Every other snapshot records each such store as not carried, with
    `not_carried` as the reason: a plaintext copy of an external store is never written."""
    marker = preflight(base)
    workspace.reject_symlinks(output)
    output = output.resolve()
    if output == base or output in base.parents or (base in output.parents and not output.is_relative_to(base / 'backups')):
        raise config.ConfigurationError('Snapshot must be outside the workspace or under its backups directory')
    if output.exists():
        raise config.ConfigurationError('Snapshot destination must not exist')
    before = inventory(base)
    before_dirs = directories(base)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=output.parent))  # paths-check: allow — renamed to the snapshot on success, removed on any failure
    try:
        entries = []
        (stage/'data').mkdir(mode=0o700)
        for name in sorted(before_dirs):
            (stage/'data'/name).mkdir(mode=0o700,parents=True,exist_ok=True)
        databases = {name for name in before if not name.endswith(('-wal', '-journal', '-shm')) and sqlite_file(base / name)}
        for name in before:
            if name.endswith(('-wal', '-journal', '-shm')) and name.rsplit('-', 1)[0] in databases:
                continue
            source, dest = base / name, stage / 'data' / name
            if name in databases:
                copy_database(source, dest)
            else:
                workspace.copy_private(source, dest)
            entries.append({'path':name, 'sha256':digest(dest), 'size':dest.stat().st_size,
                            'kind':'sqlite' if name in databases else 'file'})
        # WHAT MUST HOLD STILL is everything but the databases and the append-only logs.
        # Session MCP servers and the Stop hook keep writing to both while the tick and server
        # are stopped; a database is copied through the backup API and verified, and a log
        # copied mid-line loses at most that line (audit A11). A settings, registry or vault
        # file that changed during the copy still refuses the snapshot.
        steady = lambda inv: {k: v for k, v in inv.items() if not volatile(k, databases)}  # noqa: E731
        if steady(before) != steady(inventory(base)) or before_dirs != directories(base):
            raise config.ConfigurationError('Workspace changed during snapshot; stop all writers and retry')
        # The secret stores the settings name outside the workspace (OBS-350): beside `data/`,
        # never inside it, so an older engine verifies and restores the home alone. A store is
        # never a reason for the snapshot to fail: one that cannot be carried says why.
        externals = secret_store_carry.carry_into(stage, secret_store_carry.plan(
            base, carry_now=carry_stores, why_not=None if carry_stores else not_carried))
        manifest = {'format_version':SNAPSHOT_FORMAT, 'application_version':config.VERSION,
                    'minimum_reader':marker['minimum_reader'], 'minimum_writer':marker['minimum_writer'],
                    'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'machine_id':machine_identity.machine_id(),
                    # The home the paths in settings were written under (OBS-347): a restore under
                    # another home rewrites that prefix. Additive; an older reader ignores it.
                    # Absent when this process has no home (`this_home`): nothing is rewritten then.
                    **({'home_path':str(home),
                        # The same home with its links resolved: `tracked.json` keeps resolved paths
                        # (`tracked.canonical`), so a home behind a link is matched in both forms.
                        'home_path_resolved':_canonical(home)} if (home := this_home()) else {}),
                    'files':entries, 'directories':sorted(before_dirs),
                    # True only when a store outside the workspace was named and every one was carried:
                    # external source folders (`sources.projects`, checkouts) are never carried.
                    'external_sources_included':bool(externals) and all(e['state'] == 'carried' for e in externals),
                    'externals':externals,
                    'external_warnings':secret_store_carry.external_warnings(base),
                    'excluded':['backup collections', 'runtime locks', 'Git metadata', 'Python environments/caches']}
        workspace.write_json(stage / 'manifest.json', manifest)
        verify_snapshot(stage)
        sync_tree(stage)
        os.rename(stage, output)
        sync_directory(output.parent)
        return {'status':'snapshot-created','snapshot':str(output),'files':len(entries),
                'external_sources_included':manifest['external_sources_included'],
                'secret_store':secret_store_carry.summary(externals)}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


#: Provider keys the engine keeps as files in its state folder (review of OBS-389; tools/install_key.py,
#: agent/providers.py). Not `.keyserver-token`: every workspace has one (runtime_identity), a restore must
#: carry it unchanged, and it opens only this machine's loopback keyserver, behind the peer check, so a copy
#: on this machine grants nothing its live file does not and a copy elsewhere opens nothing.
STATE_SECRET_FILES = ('.openrouter-key', '.openai-key')


def secret_stores_inside(base: Path) -> list[str]:
    """The secret stores inside the workspace that hold a file, as workspace-relative folders (OBS-389).

    A plaintext snapshot copies every file of the workspace into `data/`, so a store inside it (the
    default `<home>/secrets`, or a `sources.secret_store`/`sources.secrets`, `OBSERVATORY_SECRETS` or
    `OBSERVATORY_VAULT_DIR` folder under the home) is copied unencrypted. A store outside the workspace
    is never copied into a plaintext snapshot (`plaintext-output`) and is not listed. Locks and
    temporaries (`secret_store_carry.SKIPPED`) are not secrets; an empty store is not listed."""
    home = base.resolve()
    unreadable = []
    try:
        doc = config.load(base)
    except config.ConfigurationError:
        # The settings that name the stores cannot be read: an explicit plaintext copy fails closed.
        doc, unreadable = {}, ['config/settings.json (unreadable: the secret stores it names are unknown)']
    sources = doc.get('sources') if isinstance(doc.get('sources'), dict) else {}
    values = [sources.get('secret_store'), sources.get('secrets'),
              os.environ.get(backup_vault.SECRETS_ENV), os.environ.get(backup_vault.VAULT_DIR_ENV)]
    candidates = [home / 'secrets']
    # The node folder holds private keys outside every vault store (review of OBS-389): the device keys,
    # each organization's keys, every share's keys (owner, sent, received) and the relay's sealing key.
    # Counted whole, under OBSERVATORY_NODE when it is inside the home and at <home>/node in any case.
    for node in sorted({home / 'node', Path(os.environ.get('OBSERVATORY_NODE') or home / 'node').expanduser().resolve()}):
        if node == home / 'node' or home in node.parents:
            candidates.append(node)
    # Provider keys kept as files in the state folder (OBSERVATORY_STATE, <home>/store by default).
    for state in sorted({home / 'store', Path(os.environ.get('OBSERVATORY_STATE') or home / 'store').expanduser().resolve()}):
        if state == home / 'store' or home in state.parents:
            candidates += [state / name for name in STATE_SECRET_FILES]
    for value in values:
        if isinstance(value, str) and value:
            path = Path(value).expanduser().resolve()
            if home in path.parents:
                candidates.append(path)
    found: list[str] = list(unreadable)
    for store in candidates:
        if store.is_symlink():
            continue
        if store.is_file():
            rel = store.relative_to(home).as_posix()
            if rel not in found and store.stat().st_size and not excluded(PurePosixPath(rel)):
                found.append(rel)
            continue
        if not store.is_dir():
            continue
        rel = store.relative_to(home).as_posix()
        if rel in found or any(rel.startswith(f + '/') for f in found) or excluded(PurePosixPath(rel)):
            continue
        for directory, children, files in os.walk(store, followlinks=False):
            children[:] = [c for c in children
                           if not excluded((Path(directory) / c).relative_to(home))]
            if any(not secret_store_carry._skipped(name)
                   and not excluded((Path(directory) / name).relative_to(home)) for name in files):
                found.append(rel)
                break
    return found


def snapshot(base: Path, output: Path | None = None, *, writers_stopped: bool = False,
             kind: str = 'snapshot', update: bool = False, plaintext: bool = False) -> dict:
    """An explicit --output is written as a plaintext directory. When the workspace holds secrets
    (a store inside it with a file, `secret_stores_inside`) that copy is refused unless the caller
    passes `plaintext=True` (`--plaintext`), and then the answer says so (OBS-389); without secrets
    it is written exactly as before. Without an output the snapshot is staged under <home>/backups
    and then handed to backup_vault: exported encrypted to the backups root when a passphrase is
    configured, otherwise kept locally under rotation. `update` names the snapshot an update takes
    before it installs (its reason for not carrying a secret store); that caller passes `plaintext`
    as well, since its stage is the plaintext copy a rollback restores."""
    managed_layout(base)
    preflight(base)  # unknown versions refuse before locks or other writes
    require_stopped(writers_stopped)
    explicit = output is not None
    if plaintext and not explicit:
        raise config.ConfigurationError('--plaintext applies only to --output DIR; without --output the snapshot '
                                        'is exported encrypted when a backup passphrase is configured')
    if kind not in backup_vault.SNAPSHOT_KINDS:
        raise config.ConfigurationError(f'Unknown snapshot kind: {kind}')
    output = output or base / 'backups' / (f'{kind}-' + uuid.uuid4().hex)
    with operation_lock(base):
        # No plaintext copy under the workspace's backups keeps an external store: a run killed
        # between the copy and the encryption left one; this one removes it first (OBS-350).
        swept = secret_store_carry.sweep(base / 'backups')
        # A folder the sweep could not clean is said, never a reason to stop the backup.
        unswept = {'secret_store_sweep': {'failed': swept['failed']}} if swept['failed'] else {}
        if explicit:
            stores = secret_stores_inside(base)
            if stores and not plaintext:
                named = ', '.join(stores)
                raise config.ConfigurationError(
                    f'This workspace holds secrets ({named}) and --output writes a plaintext copy of them. '
                    'Either run without --output, which exports the snapshot encrypted to the backups root '
                    'once a backup passphrase is set (`project-observatory full backup-passphrase status` says, '
                    '`... set` sets one), or pass --plaintext to write the plaintext copy anyway and keep it as '
                    'private as the vault')
            result = _snapshot(base, output, not_carried='update-snapshot' if update else 'plaintext-output')
            result['encrypted'] = False
            if stores:
                result['plaintext'] = True
                result['warnings'] = [f'the snapshot holds the secrets of {", ".join(stores)} in plaintext '
                                      '(files 0600, folders 0700): keep it as private as the vault and delete '
                                      'it when done']
            result.update(unswept)
            return result
        # The store is carried only into a snapshot about to be encrypted: the condition
        # `after_snapshot` encrypts on (OBS-350).
        try:
            secret = backup_vault.passphrase(base)
        except backup_vault.BackupError:
            secret = None
        result = _snapshot(base, output, carry_stores=bool(secret), not_carried='no-passphrase')
        staged = Path(result['snapshot'])
        try:
            result.update(backup_vault.after_snapshot(base, staged, kind))
        except BaseException:
            # Interrupted or failed inside the encryption (a signal, a missing library, a bug):
            # the plaintext copy that stays must not keep the store. Then the failure goes on.
            try:
                if staged.is_dir():
                    secret_store_carry.strip(staged, 'export-failed')
            except Exception:  # noqa: BLE001 - the original failure goes on; the next run's sweep retries
                pass
            raise
        if not result.get('encrypted') and result['secret_store'].get('state') == 'carried':
            # The export failed and the copy stays plaintext under the workspace's backups:
            # it must not keep the external store.
            secret_store_carry.strip(staged, 'export-failed')
            result['secret_store'] = {**result['secret_store'], 'state': 'not-carried', 'reason': 'export-failed',
                                      'files': 0, 'size': 0}
            result['external_sources_included'] = False
        result.update(unswept)
        if result['secret_store'].get('state') != 'not-applicable' or unswept:
            problem = _record_carry(base, kind, Path(result['snapshot']).name, result['secret_store'],
                                    len(swept['failed']))
            if problem:
                result['carry_record_error'] = problem
        return result


def _record_carry(base: Path, kind: str, file: str, store: dict, sweep_failed: int = 0) -> str | None:
    """`store/raw/secret-store-carry.json`: what the newest exported or kept snapshot did with the
    external secret store (names, counts, times; never a value), the record doctor reads (OBS-349).
    Written only for snapshots `backup_vault` keeps or exports, never for an explicit `--output`, so a
    plaintext copy taken by hand never hides whether the daily encrypted one carried the store."""
    record = {'at':datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'kind':kind,
              'file':file, 'state':store.get('state'), 'reason':store.get('reason'),
              'files':store.get('files', 0), 'size':store.get('size', 0),
              'not_carried':store.get('not_carried', 0),
              # Copies under the workspace's backups the sweep could not clean of an external store.
              'sweep_failed':sweep_failed}
    try:
        workspace.write_json(base.joinpath(*backup_vault.CARRY_RECORD), record)
    except (OSError, config.ConfigurationError) as exc:
        # The snapshot stands; doctor then reads the previous record, or none, and keeps warning.
        problem = f'the secret store carry record was not written: {type(exc).__name__}'
        print(f'Observatory: {problem}', file=sys.stderr)
        return problem
    return None


def safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise config.ConfigurationError('Invalid snapshot path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'..', '.'} for part in value.split('/')) or path.as_posix() != value:
        raise config.ConfigurationError('Snapshot path is not normalized and relative')
    return value


def verify_snapshot(source: Path) -> dict:
    workspace.reject_symlinks(source)
    manifest = config.read_json(source / 'manifest.json')
    if type(manifest.get('format_version')) is not int or manifest['format_version'] != SNAPSHOT_FORMAT:
        raise config.ConfigurationError('Unsupported snapshot format')
    config.version_tuple(manifest.get('application_version'))
    for field in ('minimum_reader', 'minimum_writer'):
        if config.version_tuple(manifest.get(field)) > config.version_tuple(config.VERSION):
            raise config.ConfigurationError('Snapshot requires a newer compatible Observatory release')
    entries = manifest.get('files')
    if not isinstance(entries, list):
        raise config.ConfigurationError('Snapshot manifest must list files')
    expected = set()
    for row in entries:
        if not isinstance(row, dict):
            raise config.ConfigurationError('Invalid snapshot file record')
        name = safe_relative(row.get('path'))
        if type(row.get('size')) is not int or row['size'] < 0:
            raise config.ConfigurationError('Invalid snapshot file size')
        if name in expected or excluded(Path(name)) or name == JOURNAL:
            raise config.ConfigurationError('Duplicate or excluded snapshot path')
        expected.add(name)
        file = source / 'data' / name
        workspace.reject_symlinks(file)
        if not file.is_file() or file.stat().st_size != row.get('size') or digest(file) != row.get('sha256'):
            raise config.ConfigurationError('Snapshot file does not match its SHA256 manifest')
        if row.get('kind') == 'sqlite':
            with contextlib.closing(sqlite3.connect(file.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
                compatibility.verify_database(conn)
        elif row.get('kind') != 'file':
            raise config.ConfigurationError('Unknown snapshot file kind')
    declared_dirs = manifest.get('directories')
    if not isinstance(declared_dirs,list):
        raise config.ConfigurationError('Snapshot must list its directories')
    expected_dirs = {safe_relative(name) for name in declared_dirs}
    if len(expected_dirs) != len(declared_dirs) or any(excluded(Path(name)) for name in expected_dirs):
        raise config.ConfigurationError('Invalid snapshot directory list')
    actual = set()
    actual_dirs = set()
    for file in (source / 'data').rglob('*'):
        if file.is_symlink() or (not file.is_file() and not file.is_dir()):
            raise config.ConfigurationError('Snapshot contains a link or special file')
        if file.is_file():
            actual.add(file.relative_to(source / 'data').as_posix())
        elif file.is_dir():
            actual_dirs.add(file.relative_to(source / 'data').as_posix())
    if expected_dirs != actual_dirs:
        raise config.ConfigurationError('Snapshot contains missing or unlisted directories')
    if expected != actual or 'workspace.json' not in expected or 'config/settings.json' not in expected:
        raise config.ConfigurationError('Snapshot contains missing or unlisted workspace files')
    # The secret stores carried beside `data/` (OBS-350): listed, digested, nothing more.
    secret_store_carry.verify(source, manifest)
    preflight(source / 'data')
    return manifest


def _left_out(name: str) -> bool:
    return any(name == p or name.startswith(p + '/') for p in ANOTHER_MACHINE_LEAVES)


def _as_another_machine(stage: Path) -> dict:
    """Make a restored copy this machine's own (OBS-284): leave out the other machine's identity
    and schedule state, turn its scheduler off, and give the workspace its own instance id."""
    left = []
    for name in ANOTHER_MACHINE_LEAVES:
        path = stage / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            left.append(name + '/')
        elif os.path.lexists(path):
            path.unlink()
            left.append(name)
    settings_file = stage / 'config' / 'settings.json'
    doc = config.read_json(settings_file)
    scheduler = 'off'
    if (doc.get('features') or {}).get('scheduler') is True:
        doc['features']['scheduler'] = False
        workspace.write_json(settings_file, doc)
        scheduler = 'turned off: `project-observatory full configure features scheduler true` on the one machine that runs it'
    held = []
    lifecycle_file = stage / LIFECYCLE
    if lifecycle_file.is_file():
        doc = config.read_json(lifecycle_file)
        targets = doc.get('targets') if isinstance(doc, dict) else None
        for target_id, target in sorted((targets or {}).items()):
            if isinstance(target, dict) and target.get('desiredState') == 'always_on':
                target['desiredState'] = 'held_off'
                target['policyRevision'] = int(target.get('policyRevision') or 0) + 1
                held.append(target_id)
        if held:
            workspace.write_json(lifecycle_file, doc)
    marker_file = stage / 'workspace.json'
    marker = config.read_json(marker_file)
    previous = marker.get('instance_id')
    marker['instance_id'] = str(uuid.uuid4())
    workspace.write_json(marker_file, marker)
    return {'left_out': left, 'scheduler': scheduler, 'new_instance_id': True, 'held_off': held,
            'previous_instance_label': str(previous or '').replace('-', '')[:8] or None,
            'notes': [OTHER_PASSPHRASE_NOTE],
            'next': ['project-observatory full node --init  (this machine\'s own device id)',
                     # The passphrase file stayed behind (OBS-348): until this machine makes its own, every
                     # snapshot it takes stays unencrypted inside the workspace, secrets included.
                     'project-observatory full maintain ensure  (this machine\'s own backup passphrase, kept '
                     'outside the workspace too; until then snapshots stay unencrypted in the workspace)',
                     *(f'project-observatory full lifecycle policy {target} --desired always_on  (only on the one '
                       f'machine that runs it)' for target in held)]}


# --- paths written under another home (OBS-347) -----------------------------------------------

#: Settings that hold a path a restore under another home rewrites and checks.
PATH_FILES = {'settings': 'config/settings.json', 'machine': 'config/machine-backup.json',
              'tracked': 'config/tracked.json'}


def _recorded_home(value: str) -> PurePath:
    """The flavour of path the snapshot's machine wrote: a Windows home (`C:\\Users\\a`, a UNC
    share) compares as Windows parts, everything else as POSIX parts, whatever this machine is."""
    if re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith('\\\\'):
        return PureWindowsPath(value)
    return PurePosixPath(value)


def _home_rest(value: str, old_home: str) -> tuple[str, ...] | None:
    """The parts of `value` below `old_home`, compared part by part (so `/Users/ann` never
    matches `/Users/anna`); None when `value` is not the old home or under it. A home that is
    a filesystem root is no home: everything would be under it."""
    if not isinstance(value, str) or not isinstance(old_home, str) or not value or not old_home:
        return None
    home = _recorded_home(old_home)
    # Lexically normalised first: `<home>/../other` leaves the home and is outside it, never `~/../other`.
    flavour = ntpath if isinstance(home, PureWindowsPath) else posixpath
    path = type(home)(flavour.normpath(value))
    if not home.is_absolute() or not path.is_absolute() or home.parent == home:
        return None
    fold = (lambda parts: tuple(p.casefold() for p in parts)) if isinstance(home, PureWindowsPath) else tuple
    n = len(home.parts)
    if fold(path.parts[:n]) != fold(home.parts):
        return None
    return tuple(path.parts[n:])


def _rest_in_any(value: str, homes: tuple[str, ...]) -> tuple[str, ...] | None:
    """`_home_rest` against each recorded form of the old home, the first that holds `value`."""
    for home in homes:
        rest = _home_rest(value, home)
        if rest is not None:
            return rest
    return None


def relative_to_home(value: str, old_home: str, *also: str) -> str | None:
    """`~/<rest>` (POSIX separators) for a value equal to `old_home` (or one of `also`, the same
    home in another form) or under it, else None."""
    rest = _rest_in_any(value, (old_home, *(h for h in also if h)))
    if rest is None:
        return None
    return '~' if not rest else '~/' + '/'.join(rest)


def fix_for(setting: str) -> str:
    """The exact step that points a setting a restore could not resolve somewhere else."""
    section, _, source = setting.partition('.')
    if section == 'storage':
        return 'project-observatory full configure storage backups PATH'
    return f'project-observatory full configure sources {source} PATH'


def _path_settings(doc: dict):
    """(setting, value) for every source and the backups root a settings document names."""
    for name, value in sorted((doc.get('sources') or {}).items()):
        yield f'sources.{name}', value
    storage = doc.get('storage') if isinstance(doc.get('storage'), dict) else {}
    if 'backups' in storage:
        yield 'storage.backups', storage['backups']


def this_home() -> Path | None:
    """This process's home folder, or None when it has none.

    `Path.home()` raises on Windows when neither USERPROFILE nor HOMEDRIVE and HOMEPATH are set
    (a service account, a scheduled task without a profile, a test environment built from
    scratch); a snapshot or a restore must not fail for it, it only cannot rewrite home paths."""
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None


def _exists(value: str) -> bool:
    """Whether a stored path exists here; `~/...` with no home to expand into does not."""
    try:
        return Path(value).expanduser().exists()
    except (RuntimeError, KeyError, OSError):
        return False


def _canonical(path: Path) -> str:
    """A declared folder in `tracked.json`'s own form (`tracked.canonical`: resolved, not strict)."""
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path)


def rewrite_home_paths(stage: Path, *, old_home: str | None, new_home: Path | None,
                       old_home_resolved: str | None = None) -> dict:
    """Make a restored workspace's paths resolve under THIS home (OBS-347).

    WHY. `configure` stores a path under the home as `~/...` now, but a workspace written by
    an older release, or a value typed absolute, names the first machine's home
    (`<home of a>/...`). Restored under another user name or another platform's home, every source,
    the backups root and the external secret store pointed at a home that is not there. This
    rewrites the snapshot's recorded home (`manifest.home_path`) and nothing else: a value
    outside it is kept as given, however much it looks like another account's home (no
    inference from a name). It changes strings only: it never follows a link and never
    creates a folder.

    - `config/settings.json`: `sources.*` and `storage.backups` become `~/<rest>`;
    - `config/machine-backup.json`: `include` and `roots` entries become `~/<rest>`;
    - `config/tracked.json`: each declared folder's `path` stays ABSOLUTE (the file's rule),
      under the new home and resolved as `tracked.canonical` writes it; every other field of the
      row is kept. A declared folder that does not exist afterwards is named in `unresolved`.

    The old home is matched as recorded and, when the snapshot names it, with its links resolved
    (`home_path_resolved`): `tracked.json` holds resolved paths, so a home behind a link would
    otherwise never match them. A value is normalised lexically first, so one that leaves the old
    home through `..` is outside it.

    A file is written only when something in it changed, so a same-home restore keeps every
    byte. `unresolved` names every source and backups root that does not exist afterwards,
    whatever the home did, with its `configure` step; a missing one is not a refusal."""
    new = str(new_home) if new_home is not None else None
    rewrite = bool(old_home) and new is not None and old_home != new
    homes = tuple(dict.fromkeys(h for h in (old_home, old_home_resolved) if isinstance(h, str) and h))
    rewritten, unresolved = [], []

    def read(key: str):
        file = stage / PATH_FILES[key]
        if not file.is_file() or file.is_symlink():
            return file, None
        try:
            doc = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            doc = 'unreadable'
        if doc == 'unreadable' or not isinstance(doc, dict):
            unresolved.append({'file': PATH_FILES[key], 'reason': 'unreadable'})
            return file, None
        return file, doc

    file, doc = read('settings')
    if doc is not None:
        changed = False
        for setting, value in list(_path_settings(doc)):
            to = relative_to_home(value, *homes) if rewrite else None
            if to is None:
                continue
            section, _, name = setting.partition('.')
            doc[section][name] = to
            rewritten.append({'file': PATH_FILES['settings'], 'setting': setting, 'to': to})
            changed = True
        if changed:
            workspace.write_json(file, doc)
        for setting, value in _path_settings(doc):
            if isinstance(value, str) and value and not _exists(value):
                unresolved.append({'file': PATH_FILES['settings'], 'setting': setting, 'value': value,
                                   'exists': False, 'reason': 'missing-after-restore', 'fix': fix_for(setting)})
    if rewrite:
        file, doc = read('machine')
        if doc is not None:
            changed = False
            for key in ('include', 'roots'):
                values = doc.get(key)
                if not isinstance(values, list):
                    continue
                for i, value in enumerate(values):
                    to = relative_to_home(value, *homes)
                    if to is not None:
                        values[i] = to
                        rewritten.append({'file': PATH_FILES['machine'], 'setting': f'{key}[{i}]', 'to': to})
                        changed = True
            if changed:
                workspace.write_json(file, doc)
    file, doc = read('tracked')
    if doc is not None:
        changed = False
        for key in ('local_folders', 'dismissed_local_folders'):
            rows = doc.get(key)
            if not isinstance(rows, list):
                continue
            for i, row in enumerate(rows):
                rest = _rest_in_any(row.get('path'), homes) if rewrite and isinstance(row, dict) else None
                if rest is None:
                    continue
                row['path'] = _canonical(new_home.joinpath(*rest))
                rewritten.append({'file': PATH_FILES['tracked'], 'setting': f'{key}[{i}].path', 'to': row['path']})
                changed = True
        if changed:
            workspace.write_json(file, doc)
        # A declared folder this home does not hold (outside the old home, or not cloned yet) reads nothing.
        for i, row in enumerate(doc.get('local_folders') if isinstance(doc.get('local_folders'), list) else []):
            value = row.get('path') if isinstance(row, dict) else None
            if isinstance(value, str) and value and not _exists(value):
                unresolved.append({'file': PATH_FILES['tracked'], 'setting': f'local_folders[{i}].path',
                                   'value': value, 'exists': False, 'reason': 'missing-after-restore',
                                   'fix': 'project-observatory full track local-folder PATH'})
    return {'home_from': old_home or None, 'home_to': new, 'rewritten': rewritten, 'unresolved': unresolved}


def _another_home(manifest: dict) -> bool:
    """Whether the snapshot names a home other than this process's (a snapshot naming none is this home's)."""
    old, new = manifest.get('home_path'), this_home()
    if not isinstance(old, str) or not old or new is None or old == str(new):
        return False
    try:
        return Path(old).resolve() != new.resolve()
    except (OSError, RuntimeError):
        return True


def _current_sources(destination: Path) -> dict:
    """This machine's own settings' `sources`, from the workspace this process runs for when it is another
    folder than the restore's destination (an empty home has none). A foreign snapshot's store is written
    without `--secret-store-target` only where these name the same folder."""
    try:
        base = config.home()
        if base.resolve() == destination.resolve() or not (base / 'workspace.json').is_file():
            return {}
        sources = config.load(base).get('sources')
        return dict(sources) if isinstance(sources, dict) else {}
    except (config.ConfigurationError, OSError, RuntimeError):
        return {}


def restored_sources(source: Path, manifest: dict) -> dict:
    """The snapshot's `sources` as `rewrite_home_paths` will leave them, computed without writing: the
    restore decides where a carried secret store goes before the home is staged (OBS-350)."""
    try:
        doc = config.read_json(source / 'data' / PATH_FILES['settings'])
    except (OSError, ValueError, config.ConfigurationError):
        return {}
    sources = dict(doc.get('sources')) if isinstance(doc.get('sources'), dict) else {}
    old_home, new_home = manifest.get('home_path'), this_home()
    if not old_home or new_home is None or old_home == str(new_home):
        return sources
    homes = tuple(dict.fromkeys(h for h in (old_home, manifest.get('home_path_resolved')) if isinstance(h, str) and h))
    for name, value in list(sources.items()):
        to = relative_to_home(value, *homes)
        if to is not None:
            sources[name] = to
    return sources


#: Where a restored workspace keeps the vault digest manifests (`tools/vault_digests.py`).
DIGESTS = 'store/vault-digests'


def digest_manifest_to_peers(stage: Path) -> dict | None:
    """On a second machine, the first machine's own digest manifest becomes a peer (OBS-350).

    `store/vault-digests/local.json` names the machine that wrote it. Restored as this machine's own, the
    next `vault-digests write` here would refuse it as a key lost; as `peers/<its machine>.json` the view
    compares against it at once. A machine name equal to this machine's, or a peer file of that name
    already there, gets `peer-<hex8>.json`. A file that is not a manifest stays where it is, named."""
    local = stage / DIGESTS / 'local.json'
    if not os.path.lexists(local):
        return None
    try:
        tools = str(Path(__file__).resolve().parent / 'tools')
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import vault_digests
        doc = vault_digests.read_manifest(local)
        machine = doc['machine']
        try:
            here = vault_digests.machine_name()
        except Exception:  # noqa: BLE001: an unplain host name is no match for a plain one
            here = None
    except Exception as exc:  # noqa: BLE001: Refused, an import or OS error: the file stays and is named
        reason = str(exc) if exc.__class__.__name__ == 'Refused' else type(exc).__name__
        return {'state': 'left', 'reason': reason[:200]}
    peers = stage / DIGESTS / 'peers'
    name = f'{machine}.json'
    if machine == here or not vault_digests.MACHINE.fullmatch(machine) or os.path.lexists(peers / name):
        name = f'peer-{digest(local)[:8]}.json'
    if not peers.is_dir():
        peers.mkdir(parents=True, mode=0o700)
        osprivacy.make_private(peers)
    os.rename(local, peers / name)
    return {'state': 'moved-to-peers', 'file': f'peers/{name}'}


def restore(source: Path, destination: Path, *, secret: str | None = None, same_machine: bool | None = None,
            second_machine: bool | None = None, unknown: str = 'second', stdin_value: str | None = None,
            prompt: bool = True, secret_store: str = 'auto', secret_store_names: bool = False,
            snapshot_name: str | None = None, secret_store_target: str | None = None,
            snapshot_path: str | None = None, secret_store_verify_into: str | os.PathLike | None = None) -> dict:
    """A snapshot directory, or an encrypted `.obsnap` file from the backups root.

    The encrypted file is authenticated in full and decrypted beside the new home. Its
    passphrase is the first of `backup_vault.open_with`'s candidates that opens it: `secret`
    when the caller found it (`maintenance.restore_latest`), OBSERVATORY_BACKUP_PASSPHRASE,
    `stdin_value` (`--passphrase-stdin`), the OS credential store under the backups folder's
    label, then one terminal prompt (`prompt`). Another machine's passphrase is used for this
    restore and kept nowhere (OBS-348); the answer's `passphrase_source` says which opened it.

    Paths written under the snapshot's home are rewritten under this one before the restored
    copy is checked (`rewrite_home_paths`, OBS-347); the answer's `paths` names each.

    The snapshot's `machine_id` against this machine's decides how (OBS-284): taken here, the
    copy is restored whole, as before; taken on another machine, or naming none (before 0.30.1),
    it is restored without `ANOTHER_MACHINE_LEAVES` (`_as_another_machine`). `same_machine=True`
    is the person's override at a terminal (the command line checks the terminal); `second_machine=True`
    asks for the second-machine way anywhere. `unknown='keep'` (`full init` on an empty home) restores
    a snapshot that names no machine whole and answers `machine.mode: unknown` with the step that
    restores it as a second machine; only a known other machine id is restored without its identity.

    A secret store the snapshot carried beside `data/` (OBS-350) is written where the restored settings
    name it, under `secret_store` (`auto`, `keep`, `merge`, `replace`, `skip`; `secret_store_carry`):
    decided after verification and before the home is staged (a refusal writes nothing), and written
    before the home is renamed into place (a store that could not be written leaves no home). The
    answer's `secret_store` gives counts, and relative paths only with `secret_store_names`. A snapshot
    taken on another machine or under another home writes its store only where this machine's own
    settings name the same folder, or where `secret_store_target` (`--secret-store-target`) names it.

    `secret_store_verify_into` (`--secret-store-verify-into DIR`, OBS-394) writes each carried store into
    `DIR/<name>/` instead and reads it back against the manifest (`secret_store_carry.verify_into`): the folder
    the settings name is never written, locked or recorded, no confirmation is asked, and the restore record
    is not written. A policy other than `auto` or a `secret_store_target` beside it is refused."""
    if secret_store not in secret_store_carry.POLICIES:
        raise config.ConfigurationError(f"--secret-store takes one of {', '.join(secret_store_carry.POLICIES)}")
    if secret_store_verify_into is not None:
        # Refused before anyone is asked for a passphrase: what needs no snapshot is checked first.
        secret_store_carry.verify_conflict(secret_store, secret_store_target)
        early = [(destination, 'the new home')]
        if source.is_file():
            early.append((Path(os.path.abspath(source)), 'the snapshot file'))
        secret_store_carry.check_verify_into(secret_store_verify_into, early, require_empty=False)
    if source.is_file() and not source.is_symlink():
        # Refused before anyone is asked for a passphrase (the same refusal as below).
        if destination.exists() and not destination.is_symlink() and destination.is_dir() and any(destination.iterdir()):
            raise config.ConfigurationError('Restore requires a new empty destination; existing state is never overwritten')
        own = destination if (destination / 'workspace.json').is_file() else None
        # verify=False: extract_tree authenticates the whole file before it writes a byte.
        secret, _header, opened_by = backup_vault.open_with(
            source.resolve(), base=own, label=source.resolve().parent.name, given=secret, stdin_value=stdin_value,
            prompt=prompt, verify=False)
        stage = backup_vault.extract_tree(source.resolve(), destination.resolve().parent, secret)
        try:
            result = restore(stage, destination, same_machine=same_machine, second_machine=second_machine,
                             unknown=unknown, secret_store=secret_store, secret_store_names=secret_store_names,
                             snapshot_name=snapshot_name or source.name, secret_store_target=secret_store_target,
                             snapshot_path=snapshot_path or str(source.resolve()),
                             secret_store_verify_into=secret_store_verify_into)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        result['passphrase_source'] = opened_by
        return result
    workspace.reject_symlinks(source)
    workspace.reject_symlinks(destination)
    source, destination = source.resolve(), destination.resolve()
    manifest = verify_snapshot(source)
    try:
        placing = machine_identity.restore_mode(
            manifest.get('machine_id'), unknown=unknown,
            asked='same' if same_machine else ('second' if second_machine else None))
    except machine_identity.MachineIdentityError as exc:
        raise config.ConfigurationError(str(exc)) from None
    another = placing['mode'] == 'second-machine'
    if destination == source or destination in source.parents or source in destination.parents:
        raise config.ConfigurationError('Restore source and destination must be separate')
    if destination.exists() and any(destination.iterdir()):
        raise config.ConfigurationError('Restore requires a new empty destination; existing state is never overwritten')
    # The carried secret stores, decided before the home is staged: a refusal here changes nothing.
    verify_into = verify_avoid = verified = None
    if secret_store_verify_into is not None:
        verify_avoid = _verify_avoid(source, manifest, destination, snapshot_path,
                                     [(restored_sources(source, manifest), "the snapshot's settings")])
        verify_into = secret_store_carry.check_verify_into(secret_store_verify_into, verify_avoid)
        store_plan = None
    else:
        store_plan = secret_store_carry.restore_plan(
            source, manifest, restored_sources(source, manifest), destination, secret_store,
            foreign=placing['mode'] != 'same-machine' or _another_home(manifest), confirm=secret_store_target,
            current_sources=_current_sources(destination))
    with operation_lock(destination, existing=False):
        if destination.exists() and any(destination.iterdir()):
            raise config.ConfigurationError('Restore destination changed')
        stage = Path(tempfile.mkdtemp(prefix='.restore-',dir=destination.parent))  # paths-check: allow — renamed to the destination on success, removed on any failure
        try:
            workspace.copy_private(source / 'data', stage)
            verify_snapshot(source)  # do not publish a copy of a moving snapshot
            for row in manifest['files']:
                if digest(stage / row['path']) != row['sha256']:
                    raise config.ConfigurationError('Restored file hash mismatch')
            machine = _as_another_machine(stage) if another else None
            # The same in either mode: the rewrite depends on the home, never on the machine.
            paths = rewrite_home_paths(stage, old_home=manifest.get('home_path'), new_home=this_home(),
                                       old_home_resolved=manifest.get('home_path_resolved'))
            digests = digest_manifest_to_peers(stage) if another else None
            preflight(stage)
            sync_tree(stage)
            # The stores before the home: a store that cannot be written leaves the home unwritten.
            if verify_into is not None:
                verified = secret_store_carry.verify_into(source, manifest, verify_into, verify_avoid)
                stores = []
            else:
                stores = secret_store_carry.apply_plan(source, store_plan, secret_store,
                                                       snapshot=snapshot_name or source.name,
                                                       taken_on=manifest.get('machine_id'))
            if destination.exists():
                destination.rmdir()
            os.rename(stage,destination)
            sync_directory(destination.parent)
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True)
            # A verified copy goes with the home it came with, so DIR is left as it was found (and a
            # `--latest` that falls back to an older snapshot meets it empty again).
            secret_store_carry.discard_verified(verified)
            raise
    restored = len(manifest['files']) - (sum(1 for row in manifest['files'] if _left_out(row['path'])) if another else 0)
    result = {'status':'restored','files':restored,'scheduler_activated':False,
              'external_sources_included':bool(stores) and all(r['state'] in ('written','same','merged','replaced')
                                                              for r in stores),
              'machine':{**placing, **(machine or {})}, 'paths':paths}
    if verify_into is not None:
        # A verification is not a restore: doctor's restore record is neither written nor removed.
        result['secret_store'] = secret_store_carry.verify_summary(verified)
    else:
        result['secret_store'] = _said(secret_store_carry.restore_summary(store_plan, stores, names=secret_store_names),
                                       snapshot_path or str(source))
        _record_restore(destination, result['secret_store'], snapshot_name or source.name)
    if digests is not None:
        result['digest_manifest'] = digests
    return result


def _live_workspaces(destination: Path) -> list[Path]:
    """The workspaces on this machine other than `destination` whose secret stores a verification must not
    touch: the one this process runs for (`OBSERVATORY_HOME`, else the default), and the default one
    (`osprivacy.default_home`), which `--home NEW full restore` points away from (OBS-394)."""
    out: list[Path] = []
    candidates = []
    with contextlib.suppress(config.ConfigurationError, OSError, RuntimeError, KeyError):
        candidates.append(config.home())
    with contextlib.suppress(OSError, RuntimeError, KeyError):
        candidates.append(osprivacy.default_home('project-observatory-full'))
    for base in candidates:
        try:
            if (base / 'workspace.json').is_file() and not secret_store_carry._overlaps(base, destination) \
                    and all(not secret_store_carry._overlaps(base, seen) for seen in out):
                out.append(base)
        except (OSError, RuntimeError):
            continue
    return out


def _verify_avoid(source: Path, manifest: dict, home: Path, snapshot_path: str | None,
                  settings: list[tuple[dict, str]]) -> list[tuple[Path | None, str]]:
    """Every folder `--secret-store-verify-into` must stay clear of (OBS-394): the new home or this workspace,
    the snapshot and its file (not the folder holding the file: a DIR beside a carried `.obsnap` is fine), each
    live workspace and the folders its settings name for a secret store, each workspace's backups folder, and
    the folders `settings` (the snapshot's) and the environment name. A workspace whose settings do not read
    is refused (`secret-store-verify-into-unchecked`), never skipped: its store's folder is unknown."""
    avoid: list[tuple[Path | None, str]] = [(home, 'the new home or this workspace'), (source, 'the snapshot')]
    if snapshot_path:
        avoid.append((Path(os.path.abspath(snapshot_path)), 'the snapshot file'))
    settings = list(settings)
    for base in [home, *_live_workspaces(home)]:
        if (base / 'workspace.json').is_file():
            avoid.append((base, 'a workspace on this machine'))
            try:
                sources = config.load(base).get('sources')
            except (config.ConfigurationError, OSError, ValueError) as exc:
                raise secret_store_carry.CarryRefused(secret_store_carry.VERIFY_UNCHECKED, (
                    f"The settings of the workspace at {backup_vault._display(base)} do not read "
                    f"({type(exc).__name__}), so the secret store folders they name cannot be kept clear of "
                    "--secret-store-verify-into; nothing was written. Repair them, then run the restore again "
                    "(what is wrong: project-observatory full doctor)")) from None
            if isinstance(sources, dict):
                settings.append((sources, f"the workspace at {backup_vault._display(base)}'s settings"))
        with contextlib.suppress(config.ConfigurationError, OSError, ValueError, RuntimeError):
            avoid.append((backup_vault.root_info(base)['path'].parent, 'the backups folder'))
    return avoid + secret_store_carry.setting_folders(manifest, settings, home)


def store_next_step(summary: dict, snapshot: str) -> str | None:
    """The exact command that writes a store a restore skipped (`skipped` with a reason), or None.

    `--secret-store-only` writes into the folders the restored workspace's settings name; a store skipped
    because its folder was not confirmed is confirmed with `--secret-store-target`, and a folder the
    settings must name first is named with `configure sources`."""
    reason = summary.get('reason')
    if summary.get('state') != 'skipped' or not reason:
        return None
    only = f'project-observatory full restore {shlex.quote(snapshot)} --secret-store-only'
    if reason == 'secret-store-target-unconfirmed' and summary.get('target'):
        return f"{only} --secret-store-target {shlex.quote(summary['target'])}"
    if reason in ('secret-store-unresolved', 'secret-store-target-inside-home', 'secret-store-target-refused'):
        setting = summary.get('setting') or 'secret_store'
        return f'project-observatory full configure sources {setting} PATH, then {only}'
    return only


def _said(summary: dict, snapshot: str) -> dict:
    """The summary with each skipped store's `next` (the first store's at the top, every store's in `stores`)."""
    def one(row: dict) -> dict:
        step = store_next_step(row, snapshot)
        return {**row, 'next': step} if step else row
    out = one(summary)
    if isinstance(summary.get('stores'), list):
        out['stores'] = [one(row) for row in summary['stores']]
    return out


def _recorded(row: dict) -> dict:
    """One store as the restore record keeps it. A skip the caller chose (`--secret-store skip`, no reason)
    is `skipped-by-choice`, which doctor never warns about; a skip with a reason stays `skipped`."""
    state = row.get('state')
    if state == 'skipped' and not row.get('reason'):
        state = 'skipped-by-choice'
    return {'name':row.get('name'), 'state':state, 'reason':row.get('reason'), 'target':row.get('target'),
            'files':row.get('files', 0), 'next':row.get('next')}


def _record_restore(base: Path, summary: dict, snapshot: str, *, full: bool = True) -> None:
    """`store/raw/secret-store-restore.json`: what the newest restore did with each carried store (codes, the
    folder in display form, the step; never a value), which doctor's `backups.secret_store.last_restore`
    reads, so a store skipped under `keep` (what `full init` passes) is said until one is in place. Every
    restore rewrites it, and one whose snapshot carried no store removes it; the file never travels in a
    snapshot (`EXCLUDED_FILES`). Every row decides, not the first. `--secret-store-only` (`full` false) with a
    snapshot that carried no store leaves the record as it is: the store it names is still missing. One that
    cannot be written is said on stderr."""
    file = base.joinpath(*backup_vault.RESTORE_RECORD)
    nothing = (None, 'not-in-snapshot', 'not-applicable')
    try:
        rows = summary['stores'] if isinstance(summary.get('stores'), list) else [summary]
        rows = [row for row in rows if row.get('state') not in nothing]
        if not rows:
            if full and os.path.lexists(file):
                file.unlink()
            return
        first = _recorded(rows[0])
        record = {'at':datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'snapshot':snapshot, **{k: v for k, v in first.items() if k != 'name'},
                  'stores':[_recorded(row) for row in rows]}
        workspace.write_json(file, record)
    except (OSError, config.ConfigurationError) as exc:
        print(f'Observatory: the secret store restore record was not written: {type(exc).__name__}', file=sys.stderr)


def restore_store_only(source: Path, base: Path, *, secret_store: str = 'auto', secret_store_names: bool = False,
                       secret: str | None = None, stdin_value: str | None = None, prompt: bool = True,
                       snapshot_name: str | None = None, secret_store_target: str | None = None,
                       snapshot_path: str | None = None,
                       secret_store_verify_into: str | os.PathLike | None = None) -> dict:
    """Write only the secret stores a snapshot carried, into the folders the CURRENT workspace's settings
    name (`full restore SNAPSHOT --secret-store-only`, OBS-350). For a workspace an older engine restored,
    a restore interrupted between the store and the home, or a store a restore skipped. The home is never
    written, except its restore record. `secret_store_target`, when given, must name that folder (the
    confirmation a skipped restore's step carries); another folder is refused, never ignored.

    `secret_store_verify_into` (OBS-394) verifies instead of writing: each store goes into `DIR/<name>/` and is
    read back against the manifest, the settings' folders are never written or locked, the restore record is
    left as it is, and no workspace is needed here (one that is there adds its settings' folders to those DIR
    may not touch)."""
    if secret_store not in secret_store_carry.POLICIES:
        raise config.ConfigurationError(f"--secret-store takes one of {', '.join(secret_store_carry.POLICIES)}")
    verifying = secret_store_verify_into is not None
    if verifying:
        secret_store_carry.verify_conflict(secret_store, secret_store_target)
        early = [(base, 'the new home or this workspace')]
        if source.is_file():
            early.append((Path(os.path.abspath(source)), 'the snapshot file'))
        secret_store_carry.check_verify_into(secret_store_verify_into, early, require_empty=False)
    workspace.reject_symlinks(base)
    has_workspace = (base / 'workspace.json').is_file()
    if not has_workspace and not verifying:
        raise config.ConfigurationError('--secret-store-only writes the stores of an existing workspace; there is '
                                        'none here: restore the whole snapshot into a new empty home instead')
    if source.is_file() and not source.is_symlink():
        secret, _header, opened_by = backup_vault.open_with(
            source.resolve(), base=base if has_workspace else None, label=source.resolve().parent.name, given=secret,
            stdin_value=stdin_value, prompt=prompt, verify=False)
        stage = backup_vault.extract_tree(source.resolve(), base.resolve().parent, secret)
        try:
            result = restore_store_only(stage, base, secret_store=secret_store, secret_store_names=secret_store_names,
                                        snapshot_name=snapshot_name or source.name,
                                        secret_store_target=secret_store_target,
                                        snapshot_path=snapshot_path or str(source.resolve()),
                                        secret_store_verify_into=secret_store_verify_into)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        result['passphrase_source'] = opened_by
        return result
    workspace.reject_symlinks(source)
    source, base = source.resolve(), base.resolve()
    manifest = verify_snapshot(source)
    sources = (config.load(base).get('sources') or {}) if has_workspace else {}
    if verifying:
        avoid = _verify_avoid(source, manifest, base, snapshot_path,
                              [(restored_sources(source, manifest), "the snapshot's settings")])
        into = secret_store_carry.check_verify_into(secret_store_verify_into, avoid)
        summary = secret_store_carry.verify_summary(secret_store_carry.verify_into(source, manifest, into, avoid))
        # A verification is not a restore: doctor's restore record is left as it is. A snapshot that carried no
        # store says so in `status`, exit 0 like a restore of one (`not-in-snapshot`).
        return {'status':'store-verified' if summary['verified'] else 'nothing-to-verify', 'secret_store':summary}
    with operation_lock(base):
        # A target given is a confirmation like a foreign snapshot's: it must be the folder the settings name.
        store_plan = secret_store_carry.restore_plan(source, manifest, sources, base, secret_store,
                                                     foreign=bool(secret_store_target), confirm=secret_store_target)
        stores = secret_store_carry.apply_plan(source, store_plan, secret_store, snapshot=snapshot_name or source.name,
                                               taken_on=manifest.get('machine_id'))
    summary = _said(secret_store_carry.restore_summary(store_plan, stores, names=secret_store_names),
                    snapshot_path or str(source))
    _record_restore(base, summary, snapshot_name or source.name, full=False)
    return {'status':'store-restored', 'secret_store':summary}


def add_missing(current: dict, defaults: dict) -> dict:
    """User values and unknown fields always win over new defaults."""
    out = dict(current)
    for key, value in defaults.items():
        if key not in out:
            out[key] = value
        elif isinstance(out[key],dict) and isinstance(value,dict):
            out[key] = add_missing(out[key],value)
    return out


def prepare_upgrade(stage: Path) -> list[str]:
    changed = []
    for file in sorted((config.SOURCE / 'defaults').glob('*.json')):
        if file.name == 'empty-registry.json':
            continue
        target = stage / 'config' / file.name
        defaults = json.loads(file.read_text())
        current = config.read_json(target) if target.exists() else {}
        merged = add_missing(current,defaults)
        if not target.exists() or merged != current:
            workspace.write_json(target,merged)
            changed.append(target.relative_to(stage).as_posix())
    # A subprocess resolves all paths freshly; inherited individual path overrides
    # must not let migrations touch the source or an external database.
    env = {key:value for key,value in os.environ.items() if not key.startswith('OBSERVATORY_')}
    env['OBSERVATORY_HOME'] = str(stage)
    if _migrate(stage):
        raise config.ConfigurationError('Staged database migration failed; original workspace is unchanged')
    changed.append('store/observatory.db')
    marker = config.read_json(stage / 'workspace.json')
    marker.update({'format_version':config.WORKSPACE_VERSION,'updated_by':config.VERSION})
    workspace.write_json(stage / 'workspace.json',marker)
    changed.append('workspace.json')
    return changed


_MIGRATE = 'from store import db; c=db.connect(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()'


def _migrate(home: Path) -> str:
    """Open the store under `home` with this release's code, which migrates it in place
    (`store.db.connect`: the process lock, then SQLite's own write transaction, so a
    concurrent writer waits rather than tears). Returns the failure's last line, or ''."""
    env = {key:value for key,value in os.environ.items() if not key.startswith('OBSERVATORY_')}
    env['OBSERVATORY_HOME'] = str(home)
    done = subprocess.run([sys.executable,'-c',_MIGRATE],cwd=config.SOURCE,env=env,capture_output=True,text=True)
    if done.returncode:
        tail = (done.stderr or '').strip().splitlines()
        return (tail[-1] if tail else f'exit {done.returncode}')[:300]
    return ''


def upgrade(base: Path, *, apply: bool = False, writers_stopped: bool = False) -> dict:
    managed_layout(base)
    marker = preflight(base)
    preview = {'status':'preview','application_version':config.VERSION,
               'workspace_format':marker['format_version'],'backup_required':True,
               'stop_writers_required':True,'automatic_downgrade':False}
    if not apply:
        return preview
    require_stopped(writers_stopped)
    with operation_lock(base):
        preflight(base)
        before = inventory(base)
        # THE STORE IS MIGRATED IN PLACE, NOT SWAPPED (2026-10-07). It was replaced by the
        # staged copy, so a single write by any of the session MCP servers or Stop hooks
        # while the copy was migrated refused the whole update ("Workspace changed while
        # upgrade was staged") — with twenty sessions open it rolled back 0.19.0, though
        # 0.19.0 carried no migration at all. The staged copy now only proves the migration
        # runs; the live store is migrated by the same code under SQLite's locks, and the
        # databases and the append-only logs are left out of the stillness check, as the
        # snapshot already leaves them (audit A11). The before-upgrade snapshot stays the
        # way back.
        databases = {name for name in before if not name.endswith(('-wal', '-journal', '-shm'))
                     and sqlite_file(base / name)}
        steady = lambda inv: {k: v for k, v in inv.items() if not volatile(k, databases)}  # noqa: E731
        receipt = _snapshot(base,base / 'backups' / ('before-upgrade-' + uuid.uuid4().hex),
                            not_carried='update-snapshot')
        stage = Path(tempfile.mkdtemp(prefix='.upgrade-',dir=base.parent))  # paths-check: allow — removed by the finally below on every path
        rollback = base / 'backups' / ('rollback-' + uuid.uuid4().hex)
        replaced = []
        committed = False
        try:
            workspace.copy_private(Path(receipt['snapshot']) / 'data',stage)
            changed = [name for name in prepare_upgrade(stage) if name not in databases]
            held, now = steady(before), steady(inventory(base))
            if held != now:
                # Name what moved (workspace-relative paths, never contents): without them the
                # refusal of 0.22.0 on 2026-10-11 could not say which writer to stop.
                moved = sorted(k for k in set(held) | set(now) if held.get(k) != now.get(k))
                more = f' and {len(moved) - 5} more' if len(moved) > 5 else ''
                raise config.ConfigurationError('Workspace changed while upgrade was staged; stop all writers '
                                                f'(changed: {", ".join(moved[:5])}{more})')
            # The live store first, before the interrupted-upgrade marker exists (the code
            # that migrates refuses a workspace mid-upgrade): one SQLite transaction, so a
            # failure changes nothing and no file has been swapped yet.
            failure = _migrate(base)
            if failure:
                raise config.ConfigurationError(f'Database migration failed; nothing was changed: {failure}')
            rollback.mkdir(mode=0o700,parents=True)
            workspace.write_json(base / JOURNAL,{'application_version':config.VERSION,'snapshot':receipt['snapshot'],
                                                 'recovery':'Restore snapshot into a new home with this compatible release'})
            sync_directory(base)
            for name in changed:
                target = base / name
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                saved = rollback / name
                existed = target.exists()
                if existed:
                    saved.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    os.replace(target,saved)
                replaced.append((name,existed))
                if name == 'store/observatory.db':
                    for suffix in ('-wal','-shm','-journal'):
                        companion = Path(str(target)+suffix)
                        if companion.exists():
                            saved_side = Path(str(saved)+suffix)
                            os.replace(companion,saved_side)
                            replaced.append((name+suffix,True))
                os.replace(stage / name,target)
                sync_directory(target.parent)
            (base / JOURNAL).unlink()
            sync_directory(base)
            committed = True
            # Cleanup cannot turn a committed upgrade into an attempted rollback.
            shutil.rmtree(rollback,ignore_errors=True)
            # Nor can the backup export: the upgrade is done, the snapshot exists
            # either way, and the result says where it ended up and why.
            try:
                backup = backup_vault.after_snapshot(base,Path(receipt['snapshot']),'before-upgrade')
            except Exception as exc:  # noqa: BLE001 — reported, never raised past a commit
                backup = {'snapshot':receipt['snapshot'],'encrypted':False,'export_error':f'{type(exc).__name__}: {exc}'}
            return {'status':'upgraded','version':config.VERSION,'snapshot':backup['snapshot'],
                    'files_updated':len(changed),'store_migrated_in_place':True,
                    'scheduler_activated':False,'backup':backup}
        except BaseException:
            if committed:
                raise
            for name,existed in reversed(replaced):
                target,saved = base / name,rollback / name
                target.unlink(missing_ok=True)
                if existed:
                    os.replace(saved,target)
                sync_directory(target.parent)
            (base / JOURNAL).unlink(missing_ok=True)
            sync_directory(base)
            if rollback.exists():
                shutil.rmtree(rollback)
            raise
        finally:
            shutil.rmtree(stage,ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    """Shared by `main` and the gate's parse-only check (`workspace.parse`).

    `backup` is reached as `full workspace-backup` (the engine's `backup` step is
    the SQLite-only one), so its usage line says the name a person types."""
    ap = argparse.ArgumentParser(prog='project-observatory full', description=__doc__)
    commands = ap.add_subparsers(dest='command',required=True)
    backup = commands.add_parser('backup', prog='project-observatory full workspace-backup')
    backup.add_argument('--output',type=Path,
                        help='write a plaintext snapshot folder here instead of the managed backup; refused '
                             'when the workspace holds secrets unless --plaintext is given (OBS-389)')
    backup.add_argument('--plaintext',action='store_true',
                        help='with --output: write the plaintext copy even though it holds the vault\'s secrets')
    backup.add_argument('--writers-stopped',action='store_true')
    up = commands.add_parser('upgrade', prog='project-observatory full upgrade')
    up.add_argument('--apply',action='store_true')
    up.add_argument('--writers-stopped',action='store_true')
    res = commands.add_parser('restore', prog='project-observatory full restore')
    res.add_argument('snapshot',type=Path,nargs='?')
    res.add_argument('--latest',action='store_true',
                     help='the newest backup a workspace at this path left, opened with the passphrase '
                          'kept in the OS credential store, else asked once at a terminal')
    res.add_argument('--label',help='with --latest: which workspace\'s backups, when several exist')
    res.add_argument('--second-machine',action='store_true',
                     help='restore as another machine: no device identity, enrollments, shares, mailbox or schedule '
                          'state, the scheduler off (the default for a snapshot from another machine or naming none)')
    res.add_argument('--same-machine',action='store_true',
                     help='restore a snapshot taken on another machine, or one that names none, whole: its '
                          'device identity, enrollments and schedule state included; only at a terminal')
    res.add_argument('--passphrase-stdin',action='store_true',
                     help='read the backup\'s passphrase (another machine\'s) from stdin once, a file or a pipe, '
                          'never a terminal; used for this restore and stored nowhere')
    res.add_argument('--secret-store',choices=secret_store_carry.POLICIES,default='auto',
                     help='what to do with a secret store the snapshot carried when its folder already holds one: '
                          'auto (write it where none is, refuse when a different one is there), keep, merge (add the '
                          'missing files, keep the rest), replace (move the old one aside, never deleted), skip')
    res.add_argument('--secret-store-only',action='store_true',
                     help='write only the snapshot\'s carried secret stores, into the folders THIS workspace\'s '
                          'settings name; the home is not touched')
    res.add_argument('--secret-store-target',metavar='PATH',
                     help='confirm where a snapshot taken on another machine or under another home writes its '
                          'secret store: must equal the folder its settings name, as the refusal shows it')
    res.add_argument('--secret-store-verify-into',metavar='DIR',
                     help='verify the carried secret stores instead of putting them back: write each into DIR/<name>/ '
                          '(DIR empty or absent, outside the workspace and every secret store) and check every '
                          'SHA-256 against the snapshot; the folder the settings name is never touched, and the '
                          'verified copy is left in DIR for you to remove')
    res.add_argument('--secret-store-names',action='store_true',
                     help='name the store\'s conflicting and not restored files by relative path (default: counts)')
    res.add_argument('--json',action='store_true',
                     help='a refusal or failure as {"error": {"code", "message"}} on stdout')
    return ap


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        # Read once, before any other work: a refusal of the value comes before anything is touched.
        stdin_value = backup_vault.read_passphrase_stdin() if getattr(args, 'passphrase_stdin', False) else None
        base = config.home()
        if args.command == 'backup':
            result = snapshot(base,args.output,writers_stopped=args.writers_stopped,plaintext=args.plaintext)
        elif args.command == 'upgrade':
            result = upgrade(base,apply=args.apply,writers_stopped=args.writers_stopped)
            # An update from a release before 0.17.0 runs that release's transaction, which
            # knows nothing of the maintenance job; this new code schedules it when it may
            # touch the machine (a person's terminal, or OBSERVATORY_SYSTEM_SETUP=1).
            import maintenance
            if args.apply and maintenance.system_setup_allowed(base):
                try:
                    result['maintenance'] = maintenance.ensure(base)
                except Exception as exc:  # noqa: BLE001 — the upgrade is done either way
                    result['maintenance'] = {'result':'unscheduled','detail':f'{type(exc).__name__}: {exc}'[:300]}
        elif getattr(args, 'secret_store_only', False):
            if args.latest or args.snapshot is None:
                raise config.ConfigurationError('--secret-store-only takes a snapshot, not --latest')
            if args.same_machine or args.second_machine:
                raise config.ConfigurationError('--secret-store-only writes the stores alone; it takes no '
                                                '--same-machine or --second-machine')
            result = restore_store_only(args.snapshot, base, secret_store=args.secret_store,
                                        secret_store_names=args.secret_store_names, stdin_value=stdin_value,
                                        secret_store_target=args.secret_store_target,
                                        secret_store_verify_into=args.secret_store_verify_into)
        elif getattr(args, 'same_machine', False) and getattr(args, 'second_machine', False):
            raise config.ConfigurationError('Give --same-machine or --second-machine, not both')
        # A terminal the person types in (a real console on Windows, where NUL also reads as a tty).
        elif getattr(args, 'same_machine', False) and not osprivacy.is_terminal():
            raise config.ConfigurationError('--same-machine restores the other machine\'s device identity and '
                                            'enrollments here, so it is a person\'s choice at a terminal')
        elif args.latest:
            if args.snapshot is not None:
                raise config.ConfigurationError('Give a snapshot or --latest, not both')
            import maintenance
            result = maintenance.restore_latest(base, label=args.label, same_machine=args.same_machine or None,
                                                second_machine=args.second_machine or None,
                                                stdin_value=stdin_value, ask=True, secret_store=args.secret_store,
                                                secret_store_names=args.secret_store_names,
                                                secret_store_target=args.secret_store_target,
                                                secret_store_verify_into=args.secret_store_verify_into)
            if result.get('status') != 'restored':
                print(json.dumps(result,indent=2))
                return 2
        else:
            if args.snapshot is None:
                raise config.ConfigurationError('Name a snapshot, or pass --latest')
            result = restore(args.snapshot,base,same_machine=args.same_machine or None,
                             second_machine=args.second_machine or None,stdin_value=stdin_value,
                             secret_store=args.secret_store,secret_store_names=args.secret_store_names,
                             secret_store_target=args.secret_store_target,
                             secret_store_verify_into=args.secret_store_verify_into)
        print(json.dumps(result,indent=2))
        return 0
    # The same voice as every other command: `Observatory: <reason>`. A refusal
    # before any change (configuration, a missing --writers-stopped) is exit 2;
    # a failure while working is exit 1.
    except (config.ConfigurationError,ValueError) as exc:
        return _refused(args, exc, 2, 'refused')
    except (RuntimeError,OSError,sqlite3.Error) as exc:
        return _refused(args, exc, 1, 'failed')


def _refused(args, exc: BaseException, code: int, default: str) -> int:
    """`Observatory: <reason>` on stderr; with `restore --json`, `{"error": {"code", "message"}}` on stdout,
    the code stable and kebab-case (`secret-store-not-empty`, ...; `refused` or `failed` otherwise)."""
    if getattr(args, 'json', False):
        print(json.dumps({'error': {'code': getattr(exc, 'code', None) or default, 'message': str(exc)}}, indent=2))
    else:
        print(f'Observatory: {exc}',file=sys.stderr)
    return code


if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
