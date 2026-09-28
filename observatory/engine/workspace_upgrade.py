"""Private snapshots and forward-only upgrades; restore always creates a new home."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid

import backup_vault
import configuration as config
import workspace
from store import compatibility

SNAPSHOT_FORMAT = 1
JOURNAL = 'upgrade-in-progress.json'
EXCLUDED_DIRS = {'.git', '__pycache__', '.venv', 'backups', 'migration-backups'}


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
    fd = os.open(path,os.O_RDONLY)
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
            fd = os.open(file,os.O_RDONLY|os.O_NOFOLLOW)
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
            fd = os.open(file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            handles.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise config.ConfigurationError('Workspace or scheduler is busy; stop writers and retry') from None
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)


def sqlite_file(file: Path) -> bool:
    with file.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\x00'


def copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with contextlib.closing(sqlite3.connect(compatibility.readonly_uri(source), uri=True)) as src:
            with contextlib.closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                compatibility.verify_database(dst)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def require_stopped(writers_stopped: bool) -> None:
    if not writers_stopped:
        raise config.ConfigurationError('Stop foreground/background writers, then pass --writers-stopped for a cross-file snapshot')


def _snapshot(base: Path, output: Path) -> dict:
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
    stage = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=output.parent))
    try:
        entries = []
        (stage/'data').mkdir(mode=0o700)
        for name in sorted(before_dirs):
            (stage/'data'/name).mkdir(mode=0o700,parents=True,exist_ok=True)
        databases = {name for name in before if not name.endswith(('-wal', '-journal')) and sqlite_file(base / name)}
        for name in before:
            if name.endswith(('-wal', '-journal')) and name.rsplit('-', 1)[0] in databases:
                continue
            source, dest = base / name, stage / 'data' / name
            if name in databases:
                copy_database(source, dest)
            else:
                workspace.copy_private(source, dest)
            entries.append({'path':name, 'sha256':digest(dest), 'size':dest.stat().st_size,
                            'kind':'sqlite' if name in databases else 'file'})
        if before != inventory(base) or before_dirs != directories(base):
            raise config.ConfigurationError('Workspace changed during snapshot; stop all writers and retry')
        manifest = {'format_version':SNAPSHOT_FORMAT, 'application_version':config.VERSION,
                    'minimum_reader':marker['minimum_reader'], 'minimum_writer':marker['minimum_writer'],
                    'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'files':entries, 'directories':sorted(before_dirs), 'external_sources_included':False,
                    'excluded':['backup collections', 'runtime locks', 'Git metadata', 'Python environments/caches']}
        workspace.write_json(stage / 'manifest.json', manifest)
        verify_snapshot(stage)
        sync_tree(stage)
        os.rename(stage, output)
        sync_directory(output.parent)
        return {'status':'snapshot-created','snapshot':str(output),'files':len(entries),
                'external_sources_included':False}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def snapshot(base: Path, output: Path | None = None, *, writers_stopped: bool = False) -> dict:
    """An explicit --output is honoured as a plaintext directory, exactly as before.
    Without one the snapshot is staged under <home>/backups and then handed to
    backup_vault: exported encrypted to the backups root when a passphrase is
    configured, otherwise kept locally under rotation."""
    managed_layout(base)
    preflight(base)  # unknown versions refuse before locks or other writes
    require_stopped(writers_stopped)
    explicit = output is not None
    output = output or base / 'backups' / ('snapshot-' + uuid.uuid4().hex)
    with operation_lock(base):
        result = _snapshot(base, output)
        if explicit:
            result['encrypted'] = False
            return result
        result.update(backup_vault.after_snapshot(base, Path(result['snapshot']), 'snapshot'))
        return result


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
    preflight(source / 'data')
    return manifest


def restore(source: Path, destination: Path) -> dict:
    """A snapshot directory, or an encrypted `.obsnap` file from the backups root.

    The encrypted file is authenticated in full and decrypted beside the new home;
    the passphrase comes from OBSERVATORY_BACKUP_PASSPHRASE or a terminal prompt,
    because a new machine has no workspace to keep it in yet."""
    if source.is_file() and not source.is_symlink():
        secret = backup_vault.require_passphrase(destination, prompt=True)
        stage = backup_vault.extract_tree(source.resolve(), destination.resolve().parent, secret)
        try:
            return restore(stage, destination)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    workspace.reject_symlinks(source)
    workspace.reject_symlinks(destination)
    source, destination = source.resolve(), destination.resolve()
    manifest = verify_snapshot(source)
    if destination == source or destination in source.parents or source in destination.parents:
        raise config.ConfigurationError('Restore source and destination must be separate')
    if destination.exists() and any(destination.iterdir()):
        raise config.ConfigurationError('Restore requires a new empty destination; existing state is never overwritten')
    with operation_lock(destination, existing=False):
        if destination.exists() and any(destination.iterdir()):
            raise config.ConfigurationError('Restore destination changed')
        stage = Path(tempfile.mkdtemp(prefix='.restore-',dir=destination.parent))
        try:
            workspace.copy_private(source / 'data', stage)
            verify_snapshot(source)  # do not publish a copy of a moving snapshot
            for row in manifest['files']:
                if digest(stage / row['path']) != row['sha256']:
                    raise config.ConfigurationError('Restored file hash mismatch')
            preflight(stage)
            sync_tree(stage)
            if destination.exists():
                destination.rmdir()
            os.rename(stage,destination)
            sync_directory(destination.parent)
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True)
            raise
    return {'status':'restored','files':len(manifest['files']),'scheduler_activated':False,
            'external_sources_included':False}


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
    command = 'from store import db; c=db.connect(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()'
    done = subprocess.run([sys.executable,'-c',command],cwd=config.SOURCE,env=env,capture_output=True,text=True)
    if done.returncode:
        raise config.ConfigurationError('Staged database migration failed; original workspace is unchanged')
    changed.append('store/observatory.db')
    marker = config.read_json(stage / 'workspace.json')
    marker.update({'format_version':config.WORKSPACE_VERSION,'updated_by':config.VERSION})
    workspace.write_json(stage / 'workspace.json',marker)
    changed.append('workspace.json')
    return changed


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
        receipt = _snapshot(base,base / 'backups' / ('before-upgrade-' + uuid.uuid4().hex))
        stage = Path(tempfile.mkdtemp(prefix='.upgrade-',dir=base.parent))
        rollback = base / 'backups' / ('rollback-' + uuid.uuid4().hex)
        replaced = []
        committed = False
        try:
            workspace.copy_private(Path(receipt['snapshot']) / 'data',stage)
            changed = prepare_upgrade(stage)
            if before != inventory(base):
                raise config.ConfigurationError('Workspace changed while upgrade was staged; stop all writers')
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
                    'files_updated':len(changed),'scheduler_activated':False,'backup':backup}
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--output',type=Path)
    backup.add_argument('--writers-stopped',action='store_true')
    up = commands.add_parser('upgrade')
    up.add_argument('--apply',action='store_true')
    up.add_argument('--writers-stopped',action='store_true')
    res = commands.add_parser('restore')
    res.add_argument('snapshot',type=Path)
    args = parser.parse_args(argv)
    try:
        base = config.home()
        if args.command == 'backup':
            result = snapshot(base,args.output,writers_stopped=args.writers_stopped)
        elif args.command == 'upgrade':
            result = upgrade(base,apply=args.apply,writers_stopped=args.writers_stopped)
        else:
            result = restore(args.snapshot,base)
        print(json.dumps(result,indent=2))
        return 0
    except (config.ConfigurationError,RuntimeError,ValueError,OSError,sqlite3.Error) as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
