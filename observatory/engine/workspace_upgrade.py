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


def excluded(path: Path) -> bool:
    return (any(p in EXCLUDED_DIRS for p in path.parts) or path.name in {'.workspace.lock', 'tick.lock', 'service.lock'}
            or path.name.endswith(('.upgrade.lock', '-shm')))


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


def snapshot(base: Path, output: Path | None = None, *, writers_stopped: bool = False,
             kind: str = 'snapshot') -> dict:
    """An explicit --output is honoured as a plaintext directory, exactly as before.
    Without one the snapshot is staged under <home>/backups and then handed to
    backup_vault: exported encrypted to the backups root when a passphrase is
    configured, otherwise kept locally under rotation."""
    managed_layout(base)
    preflight(base)  # unknown versions refuse before locks or other writes
    require_stopped(writers_stopped)
    explicit = output is not None
    if kind not in backup_vault.SNAPSHOT_KINDS:
        raise config.ConfigurationError(f'Unknown snapshot kind: {kind}')
    output = output or base / 'backups' / (f'{kind}-' + uuid.uuid4().hex)
    with operation_lock(base):
        if explicit:
            result = _snapshot(base, output, not_carried='plaintext-output')
            result['encrypted'] = False
            return result
        # The store is carried only into a snapshot about to be encrypted: the condition
        # `after_snapshot` encrypts on (OBS-350).
        try:
            secret = backup_vault.passphrase(base)
        except backup_vault.BackupError:
            secret = None
        result = _snapshot(base, output, carry_stores=bool(secret), not_carried='no-passphrase')
        staged = Path(result['snapshot'])
        result.update(backup_vault.after_snapshot(base, staged, kind))
        if not result.get('encrypted') and result['secret_store'].get('state') == 'carried':
            # The export failed and the copy stays plaintext under the workspace's backups:
            # it must not keep the external store.
            secret_store_carry.strip(staged, 'export-failed')
            result['secret_store'] = {**result['secret_store'], 'state': 'not-carried', 'reason': 'export-failed',
                                      'files': 0, 'size': 0}
            result['external_sources_included'] = False
        if result['secret_store'].get('state') != 'not-applicable':
            _record_carry(base, kind, Path(result['snapshot']).name, result['secret_store'])
        return result


def _record_carry(base: Path, kind: str, file: str, store: dict) -> None:
    """`store/raw/secret-store-carry.json`: what the newest exported or kept snapshot did with the
    external secret store (names, counts, times; never a value), the record doctor reads (OBS-349).
    Written only for snapshots `backup_vault` keeps or exports, never for an explicit `--output`, so a
    plaintext copy taken by hand never hides whether the daily encrypted one carried the store."""
    record = {'at':datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'kind':kind,
              'file':file, 'state':store.get('state'), 'reason':store.get('reason'),
              'files':store.get('files', 0), 'size':store.get('size', 0)}
    try:
        workspace.write_json(base.joinpath(*backup_vault.CARRY_RECORD), record)
    except (OSError, config.ConfigurationError):
        pass  # doctor then reads the previous record, or none, and keeps warning


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


def restore(source: Path, destination: Path, *, secret: str | None = None, same_machine: bool | None = None,
            second_machine: bool | None = None, unknown: str = 'second', stdin_value: str | None = None,
            prompt: bool = True) -> dict:
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
    restores it as a second machine; only a known other machine id is restored without its identity."""
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
                             unknown=unknown)
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
            preflight(stage)
            sync_tree(stage)
            if destination.exists():
                destination.rmdir()
            os.rename(stage,destination)
            sync_directory(destination.parent)
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True)
            raise
    restored = len(manifest['files']) - (sum(1 for row in manifest['files'] if _left_out(row['path'])) if another else 0)
    return {'status':'restored','files':restored,'scheduler_activated':False,
            'external_sources_included':False, 'machine':{**placing, **(machine or {})}, 'paths':paths}


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
        receipt = _snapshot(base,base / 'backups' / ('before-upgrade-' + uuid.uuid4().hex))
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
    backup.add_argument('--output',type=Path)
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
    return ap


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        # Read once, before any other work: a refusal of the value comes before anything is touched.
        stdin_value = backup_vault.read_passphrase_stdin() if getattr(args, 'passphrase_stdin', False) else None
        base = config.home()
        if args.command == 'backup':
            result = snapshot(base,args.output,writers_stopped=args.writers_stopped)
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
                                                stdin_value=stdin_value, ask=True)
            if result.get('status') != 'restored':
                print(json.dumps(result,indent=2))
                return 2
        else:
            if args.snapshot is None:
                raise config.ConfigurationError('Name a snapshot, or pass --latest')
            result = restore(args.snapshot,base,same_machine=args.same_machine or None,
                             second_machine=args.second_machine or None,stdin_value=stdin_value)
        print(json.dumps(result,indent=2))
        return 0
    # The same voice as every other command: `Observatory: <reason>`. A refusal
    # before any change (configuration, a missing --writers-stopped) is exit 2;
    # a failure while working is exit 1.
    except (config.ConfigurationError,ValueError) as exc:
        print(f'Observatory: {exc}',file=sys.stderr)
        return 2
    except (RuntimeError,OSError,sqlite3.Error) as exc:
        print(f'Observatory: {exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
