"""Whole-file advisory locks on every OS the engine runs on: `flock(fd, op)`.

The engine takes a lock with `fcntl.flock` in some twenty places — the workspace, the store's
upgrade lock, the tick, the maintenance pass, the vault, the job runner — and `fcntl` does not
exist on Windows, so one top-level `import fcntl` in `workspace.py` stopped the whole engine from
importing there (2026-10-10, docs/design/WINDOWS-LINUX.md, W1). This module keeps `fcntl.flock`'s
contract and its constants, so every call site reads as before:

    oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)   # BlockingIOError when held elsewhere
    oslocks.flock(fd, oslocks.LOCK_SH | oslocks.LOCK_NB)
    oslocks.flock(fd, oslocks.LOCK_UN)

POSIX: `fcntl.flock` itself, unchanged.

Windows: `LockFileEx` / `UnlockFileEx` through ctypes — exclusive or shared, waiting or failing at
once — on ONE BYTE FAR PAST THE END of the file. Windows byte-range locks are mandatory: a lock
over the data would make other processes' reads and writes of a locked file fail, which no POSIX
caller expects; a byte nobody reads or writes gives the same mutual exclusion and nothing else.
Contention raises `BlockingIOError`, as `fcntl.flock` does with `LOCK_NB`.

One difference no caller may depend on, and none does today: on POSIX a second `flock` call on a
descriptor that holds a shared lock converts it into an exclusive one; on Windows it requests a
second lock on the same handle instead. On both systems a lock belongs to the open file (the
handle), is released by `LOCK_UN` or by closing it, and conflicts with a lock taken through any
other open of the same file — in another process or in this one.
"""
from __future__ import annotations

import errno
import os

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

if os.name != "nt":
    import fcntl as _fcntl

    _POSIX = {LOCK_SH: _fcntl.LOCK_SH, LOCK_EX: _fcntl.LOCK_EX, LOCK_NB: _fcntl.LOCK_NB,
              LOCK_UN: _fcntl.LOCK_UN}

    def flock(fd: int, operation: int) -> None:
        native = 0
        for ours, theirs in _POSIX.items():
            if operation & ours:
                native |= theirs
        _fcntl.flock(fd, native)

else:  # pragma: no cover - exercised by the Windows CI row
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_NOT_LOCKED = 158
    _ERROR_IO_PENDING = 997
    # The locked byte: far past any file this engine writes, inside the 64-bit offset range.
    _OFFSET_LOW, _OFFSET_HIGH = 0xFFFFFFFE, 0x7FFFFFFF

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                            wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(_OVERLAPPED)]
    _UnlockFileEx.restype = wintypes.BOOL

    def _overlapped() -> _OVERLAPPED:
        ov = _OVERLAPPED()
        ov.Offset, ov.OffsetHigh = _OFFSET_LOW, _OFFSET_HIGH
        return ov

    def flock(fd: int, operation: int) -> None:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if operation & LOCK_UN:
            if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_overlapped())):
                err = ctypes.get_last_error()
                if err != _ERROR_NOT_LOCKED:  # unlocking what is not locked is not an error in flock
                    raise ctypes.WinError(err)
            return
        flags = _LOCKFILE_EXCLUSIVE_LOCK if operation & LOCK_EX else 0
        if operation & LOCK_NB:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        if not _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_overlapped())):
            err = ctypes.get_last_error()
            if err in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
                raise BlockingIOError(errno.EWOULDBLOCK, "the file is locked by another handle")
            raise ctypes.WinError(err)
