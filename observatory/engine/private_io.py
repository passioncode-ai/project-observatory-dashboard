"""Owner-only atomic credential files and a narrowly validated legacy alias."""
from __future__ import annotations
import contextlib
import oslocks
import os
from pathlib import Path
import re
import stat
import tempfile


def check(path: Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError('Private paths must be absolute and contain no symbolic links')
    if path.exists() and not path.is_file():
        raise RuntimeError('Private credential destination must be a regular file')


def parent(path: Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError('Private directories must be absolute and contain no symbolic links')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fchmod(fd,0o700)
    finally:os.close(fd)


def read(path: Path) -> str:
    check(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'r',encoding='utf-8') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError('Private credential file must be regular and owner-only')
        return stream.read()


def write(path: Path, text: str) -> None:
    check(path)
    parent(path.parent)
    fd,name=tempfile.mkstemp(prefix='.credential-write-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
        check(path)
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


def legacy_path(path: Path) -> Path:
    """Accept only the adopt command's relative sibling-admin alias; reject all others."""
    if not path.is_symlink():
        check(path)
        return path
    if path.name!='openrouter-provisioning':
        raise RuntimeError('Unrecognized credential alias')
    target=Path(os.readlink(path))
    if (len(target.parts)!=2 or target.parts[0]!='openrouter-admin'
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',target.parts[1])):
        raise RuntimeError('Legacy credential alias must name one canonical admin slot')
    resolved=path.parent/target
    check(resolved)
    return resolved


@contextlib.contextmanager
def lock(path: Path):
    check(path);parent(path.parent)
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise RuntimeError('Private lock must be regular')
        os.fchmod(fd,0o600);oslocks.flock(fd,oslocks.LOCK_EX)
        yield
    finally:os.close(fd)
