"""Process groups on every OS the engine runs on (docs/design/WINDOWS-LINUX.md, W2).

The engine runs its children — tick steps, collectors' probes, jobs, the update — each in a group
of its own, and stops the WHOLE group when a limit is reached: a collector's `git` or `du`, an MCP
server a probe started. On POSIX that is `start_new_session=True` and `os.killpg`. Windows has
neither, and one call there is worse than missing: `os.kill(pid, 0)`, the POSIX "is it alive?",
TERMINATES the process on Windows. Every call site goes through here instead:

    child = subprocess.Popen(argv, **osproc.new_group())          # its own group
    osproc.alive(pid)                                             # never a signal on Windows
    osproc.group_alive(pgid)
    osproc.stop_group(pgid, grace)                                # TERM, wait `grace`, KILL
    osproc.stop_signals()                                         # the signals this OS delivers
    osproc.unblock_stop_signals()                                 # a mask inherited across exec
    with osproc.deadline(300, on_expiry): ...                     # SIGALRM, or a timer on Windows
    osproc.disarm_deadline()                                      # before a commit it must not cut

POSIX: exactly the calls the engine made before.

Windows: a new process group (`CREATE_NEW_PROCESS_GROUP`), and the tree is stopped with
`taskkill /T /F`, which follows parent → child links. There is no SIGTERM to a process without a
console, so on Windows `stop_group` stops at once; `grace` applies to POSIX only. A grandchild
whose parent already exited is no longer linked to the tree and is not reached — the difference a
Job Object would close; it is named in the design document rather than hidden here. A deadline
is a timer that interrupts the main thread: it runs between bytecodes, so a call blocked inside C
(a socket read without a timeout) is not cut short as SIGALRM would cut it.
"""
from __future__ import annotations

import _thread
import contextlib
import os
import signal
import subprocess
import threading
import time
from typing import Callable, Iterator

WINDOWS = os.name == "nt"


def new_group(*, detached: bool = False) -> dict:
    """`Popen` keyword arguments for a child in a process group of its own.

    `detached`: the child also outlives this process's console (a job, the server)."""
    if not WINDOWS:
        return {"start_new_session": True}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if detached:
        flags |= subprocess.DETACHED_PROCESS
    return {"creationflags": flags}


def stop_signals() -> tuple[int, ...]:
    """The stop signals this OS delivers to a Python process, for `signal.signal`."""
    names = ("SIGTERM", "SIGINT", "SIGHUP") if not WINDOWS else ("SIGTERM", "SIGINT", "SIGBREAK")
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


def unblock_stop_signals() -> None:
    """Unblock the stop signals and the deadline's alarm. A blocked mask survives exec: a process
    started from a thread that blocks asynchronous signals never hears SIGTERM. Windows has no
    signal masks."""
    if hasattr(signal, "pthread_sigmask"):
        extra = {signal.SIGALRM} if hasattr(signal, "SIGALRM") else set()
        signal.pthread_sigmask(signal.SIG_UNBLOCK, set(stop_signals()) | extra)


_disarm: list[Callable[[], None]] = []


def disarm_deadline() -> None:
    """Disarm the running deadline, if any: after this it never fires (main thread)."""
    while _disarm:
        _disarm.pop()()


@contextlib.contextmanager
def deadline(seconds: int, on_expiry: Callable[[], None]) -> Iterator[None]:
    """Call `on_expiry()` in the main thread once `seconds` have passed; it raises to stop the
    work. Main thread only, as `signal.signal` requires."""
    if hasattr(signal, "SIGALRM"):
        previous = signal.signal(signal.SIGALRM, lambda signum, frame: on_expiry())
        _disarm.append(lambda: signal.alarm(0))
        signal.alarm(seconds)
        try:
            yield
        finally:
            disarm_deadline()
            signal.signal(signal.SIGALRM, previous)
        return
    fired, disarmed = threading.Event(), threading.Event()

    def handler(signum, frame):
        if fired.is_set():
            if not disarmed.is_set():
                on_expiry()
            return
        if callable(previous):
            previous(signum, frame)
        else:
            raise KeyboardInterrupt

    def expire():
        fired.set()
        _thread.interrupt_main(signal.SIGINT)
    previous = signal.signal(signal.SIGINT, handler)
    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    _disarm.append(lambda: (disarmed.set(), timer.cancel()))
    timer.start()
    try:
        yield
    finally:
        disarm_deadline()
        signal.signal(signal.SIGINT, previous)


if not WINDOWS:
    import errno

    def alive(pid: int) -> bool:
        """Whether a process with this id exists (a zombie included, as `kill(0)` says)."""
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError as exc:
            return exc.errno == errno.EPERM
        return True

    def group_alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def signal_group(pgid: int, sig: int) -> bool:
        """Send `sig` to the group; False when there is no such group (or it is not ours)."""
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def stop_group(pgid: int, grace: float) -> None:
        """SIGTERM the group, SIGKILL whatever is left after `grace` seconds."""
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
            if not signal_group(pgid, sig):
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not group_alive(pgid):
                    return
                time.sleep(0.05)

else:  # pragma: no cover - exercised by the Windows CI job
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def alive(pid: int) -> bool:
        """Whether the process is still running — asked, never signalled."""
        if not isinstance(pid, int) or pid <= 0:
            return False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, it is not ours
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def group_alive(pgid: int) -> bool:
        # The group's leader stands for the group: Windows keeps no list of a group's members.
        return alive(pgid)

    def signal_group(pgid: int, sig: int) -> bool:
        """Stop the tree rooted at `pgid` (`sig` 0 only asks); False when nothing was there."""
        if sig == 0:
            return alive(pgid)
        if not alive(pgid):
            return False
        subprocess.run(["taskkill", "/PID", str(pgid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True

    def stop_group(pgid: int, grace: float) -> None:
        """Stop the tree at once — Windows has no SIGTERM for a process without a console."""
        if not signal_group(pgid, signal.SIGTERM):
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and alive(pgid):
            time.sleep(0.05)
