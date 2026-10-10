"""Private runtime identity: reads never bootstrap; init never replaces.

The selected final directory is opened without following a symlink. Operations
are relative to that descriptor. Ancestors belong to the local operator; this
is not a defence against a hostile owner moving the ancestor hierarchy.

Windows has no directory descriptors: there the folder is held by its path, refused when it is
a link, and every operation names `folder / name` (docs/design/WINDOWS-LINUX.md, W2b).
"""
from __future__ import annotations
import os
import osprivacy
from pathlib import Path
import re
import secrets
import stat

KINDS = {
    'keyserver-token': ('.keyserver-token', r'[A-Za-z0-9_-]{43}', lambda: secrets.token_urlsafe(32)),
    'env-fingerprint-salt': ('.env-fingerprint-salt', r'[0-9a-f]{64}', lambda: secrets.token_hex(32)),
}
MAX_BYTES = 128


class IdentityError(RuntimeError):
    """Safe diagnostic containing a path and reason, never file contents."""


def failure(path: Path, kind: str, reason: str) -> IdentityError:
    return IdentityError(
        f'{kind}: {reason}: {path}. Restore a known good copy to preserve identity. '
        f'Only for a new identity, run `project-observatory full init` (or '
        f'`python3 tools/runtime_identity.py init {kind}`) with the same '
        'OBSERVATORY_STATE. Existing files are never replaced.')


def _at(folder, name: str) -> tuple:
    """(what to open, dir_fd) for `name` inside `folder`."""
    return (name, folder) if isinstance(folder, int) else (folder / name, None)


def _directory(path: Path, kind: str, initialize: bool):
    if initialize:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if osprivacy.WINDOWS:
        folder = path.parent
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise FileNotFoundError(str(folder))
        if not osprivacy.owned_by_me(folder) or osprivacy.others_may_write(folder):
            raise failure(path, kind, 'state directory owner or permissions are unsafe '
                                      f'({osprivacy.explain(folder)})')
        return folder
    fd = osprivacy.open(path.parent, os.O_RDONLY | osprivacy.DIRECTORY | osprivacy.NOFOLLOW)
    try:
        if not osprivacy.owned_by_me(fd) or osprivacy.others_may_write(fd):
            raise failure(path, kind, 'state directory owner or permissions are unsafe '
                                      f'({osprivacy.explain(fd)})')
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(fd, path: Path, kind: str) -> str:
    # NONBLOCK makes a FIFO refuse without waiting for a writer.
    name, dir_fd = _at(fd, path.name)
    child = osprivacy.open(name, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK, dir_fd=dir_fd)
    try:
        info = os.fstat(child)
        if not stat.S_ISREG(info.st_mode):
            raise failure(path, kind, 'identity is not a regular file')
        if not osprivacy.owned_by_me(child) or not osprivacy.private(child):
            raise failure(path, kind, 'identity owner or permissions are unsafe; require owner-only access')
        with os.fdopen(child, 'rb', closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        try:
            value = data.decode('ascii').strip()
        except UnicodeError:
            value = ''
        if len(data) > MAX_BYTES or not re.fullmatch(KINDS[kind][1], value):
            raise failure(path, kind, 'identity is empty or invalid')
        return value
    finally:
        os.close(child)


def load(path: Path, kind: str, *, initialize: bool = False) -> str:
    """Read one identity, or explicitly publish a fully written new file.

    link() is atomic and refuses an existing destination. Another initializer
    either wins publication or reads the same complete winner. No O_TRUNC path.
    Errors leave any existing destination untouched; no legacy fallback exists.
    """
    if kind not in KINDS:
        raise ValueError('unknown runtime identity kind')
    fd = None
    temporary = None
    try:
        fd = _directory(path, kind, initialize)
        try:
            return _read(fd, path, kind)
        except FileNotFoundError:
            if not initialize:
                raise
        candidate = f'.identity-{secrets.token_hex(16)}.tmp'
        name, dir_fd = _at(fd, candidate)
        child = osprivacy.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW,
                               0o600, dir_fd=dir_fd)
        temporary = candidate  # cleanup only a file this call actually created
        with os.fdopen(child, 'wb') as stream:
            stream.write((KINDS[kind][2]() + '\n').encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if isinstance(fd, int):
                os.link(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd,
                        follow_symlinks=False)
            else:
                os.link(fd / temporary, fd / path.name)
        except FileExistsError:
            pass
        if isinstance(fd, int):
            os.fsync(fd)
        return _read(fd, path, kind)
    except FileNotFoundError:
        raise failure(path, kind, 'identity is missing; no new value was created') from None
    except OSError:
        raise failure(path, kind, 'state cannot be read or initialized; check path and permissions') from None
    finally:
        if fd is not None:
            try:
                if temporary is not None:
                    try:
                        target, dir_fd = _at(fd, temporary)
                        os.unlink(target, dir_fd=dir_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        raise failure(path, kind, 'temporary file cleanup failed; inspect private state') from None
            finally:
                if isinstance(fd, int):
                    os.close(fd)
