# Vendored from passioncode-ai/fabric-agent-adapter 744a9045f1cb (fabric-agent-adapter 0.8.2),
# plugins/fabric-agent-adapter/skills/building-fabric-services/scripts/fabric_service.py,
# upstream sha256 a1e0a0bfe59d0805f5e512613e30dd6ed38bb30b5f3493c9f08e7cbd54013795 (the bytes below this header).
# Do not edit here: update the kit upstream and copy it again (tests/test_fabric_service.py checks the digest).
#!/usr/bin/env python3
"""Reference kit for the fabric-service/0.1 local service extension.

Standard library only, Python 3.9+. Copy this file into a service (it is one
module on purpose) or import it by path. Every function implements one rule of
the extension; the rule it implements is named in its docstring, so a service
that calls these functions inherits the rule instead of re-deriving it.

Normative source: fabric-agent-contract docs/specification/service.md (DEC-0015).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "fabric-service/0.1"
EXTENSION_KEY = "https://fabric.passioncode.ai/agent-contract/extensions/service/0.1"
EXIT_ALREADY_RUNNING = 75
# Lifecycle contract (fabric-workspace knowledge/lifecycle.md). A launchd-supervised copy that finds
# the lock held backs off in-process for this long before its exit 75 (LC-03, F8): under KeepAlive
# with ThrottleInterval 10 that is a few starts an hour instead of one every 10 s.
SUPERVISOR_ENV = "FABRIC_SERVICE_SUPERVISOR"
LOCK_WAIT_SUPERVISED_SECONDS = 300.0
LOCK_BACKOFF_FIRST_SECONDS = 0.5
LOCK_BACKOFF_MAX_SECONDS = 30.0
# LC-01: SIGTERM reaches exit in at most 10 s with work in flight — drain 8 s, then hand over,
# and a hard exit 2 s later if the hand-over itself hangs. ExitTimeOut sits above both.
DRAIN_SECONDS = 8.0
DRAIN_GRACE_SECONDS = 2.0
EXIT_HARD_STOP = 70
DEFAULT_EXIT_TIMEOUT = 15
# LC-12: logs rotate by size, 5 x 5 MB by default.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
LOGIN_CODE_TTL_SECONDS = 120
EVENTS_DEFAULT_LIMIT = 50
EVENTS_MAX_LIMIT = 200
LEVELS = ("info", "notice", "warning", "error")
STATUSES = ("starting", "ready", "degraded", "stopping")

_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_INSTANCE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,5}$")
_ORIGIN = re.compile(r"^http://127\.0\.0\.1:([0-9]{3,5})$")
# DEC-0019: a remote placement — an online agent or dashboard — lives at an https DNS name.
_REMOTE_ORIGIN = re.compile(r"^https://((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?::([0-9]{1,5}))?$")
_RESERVED_HOST = re.compile(r"(^|\.)(localhost|local|internal|home\.arpa|lan|localdomain)$")
PLACEMENTS = ("local", "remote")
_CODE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_TRACE_ID = re.compile(r"^(?!0{32}$)[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^(?!0{16}$)[0-9a-f]{16}$")
# DEC-0032: a local path is POSIX (`/…`, `~/…`) or Windows (`C:\…`, `C:/…`, `~\…`); a network
# share (`\\server\…`, `\\?\…`) is never a local path.
_LOCAL_PATH = re.compile(r"^(~[\\/]|/(?!/)|[A-Za-z]:[\\/])")
# DEC-0032: the supervisor per system and the field that names its job.
MANAGERS = ("launchd", "systemd", "task-scheduler", "none")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@-]{0,250}\.service$")
_TASK = re.compile(r"^\\[A-Za-z0-9 ._\\-]{1,254}$")
WINDOWS = os.name == "nt"
# Without O_BINARY a Windows descriptor is in text mode and every LF written becomes CRLF.
_O_BINARY = getattr(os, "O_BINARY", 0)


class ServiceError(Exception):
    """A rule of the extension was violated; the message is one readable sentence."""


class AlreadyRunning(ServiceError):
    def __init__(self, holder_pid: Optional[int], lock_path: Path):
        self.holder_pid = holder_pid
        self.lock_path = lock_path
        who = "process %d" % holder_pid if holder_pid else "another process"
        super().__init__("Another copy is already running (%s holds %s)." % (who, lock_path))


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expand(path: str) -> Path:
    """Paths in a descriptor may start with ~/ (or ~\\ on Windows); everything else must be
    absolute, and a network share is never a local path (DEC-0032)."""
    if not _LOCAL_PATH.match(str(path)):
        raise ServiceError("Path %r must be absolute or start with ~/." % path)
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        raise ServiceError("Path %r must be absolute or start with ~/." % path)
    return p


# --- where things live -------------------------------------------------------

def services_dir() -> Path:
    """Descriptor directory: FABRIC_SERVICES_DIR, else the per-user OS location."""
    override = os.environ.get("FABRIC_SERVICES_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/ai.passioncode.fabric/services"
    if sys.platform == "win32":
        # DEC-0032: LOCALAPPDATA, not APPDATA, so token files never roam with the profile.
        return _local_app_data() / "passioncode-fabric" / "services"
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local/share")
    return Path(base) / "passioncode-fabric/services"


def service_dirs(service_id: str) -> Dict[str, Path]:
    """State lives outside code: data+config, logs and cache in per-user OS directories."""
    _require_id(service_id)
    home = Path.home()
    if sys.platform == "darwin":
        return {
            "data": home / "Library/Application Support" / service_id,
            "logs": home / "Library/Logs" / service_id,
            "cache": home / "Library/Caches" / service_id,
        }
    if sys.platform == "win32":
        data = _local_app_data() / service_id
        return {"data": data, "logs": data / "Logs", "cache": data / "Cache"}
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / service_id
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / service_id
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / service_id
    return {"data": data, "logs": state / "logs", "cache": cache}


def _read_at(fd: int, n: int, offset: int) -> bytes:
    """`os.pread` where it exists; Windows has none, so seek and read (the descriptor's appends
    still go to the end: O_APPEND moves to it before every write)."""
    if hasattr(os, "pread"):
        return os.pread(fd, n, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, n)


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Local"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        _win_protect(path, folder=True)
    else:
        os.chmod(path, 0o700)
    return path


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Temporary file in the same directory, fsync, rename: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if WINDOWS:
            # The file's ACL is its privacy (DEC-0032): set explicitly and protected, so it does
            # not depend on what the folder hands down. A rename keeps it.
            _win_protect(Path(tmp), folder=False)
        else:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# --- one copy ----------------------------------------------------------------

class InstanceLock:
    """Exclusive flock on <data>/service.lock, taken BEFORE any side effect.

    Binding a port is not a lock: a second copy that resumes jobs before its
    bind fails has already done damage. The lock is released by the kernel when
    the process dies, so a crash never leaves it stale.
    """

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "service.lock"
        self._fd: Optional[int] = None

    #: Windows locks a byte range, not the file: one byte far past the pid the file holds, so the
    #: holder may rewrite the pid and another process may still read it (locking past the end of
    #: a file is allowed there).
    WINDOWS_LOCK_OFFSET = 1 << 30

    def acquire(self) -> "InstanceLock":
        ensure_private_dir(self.path.parent)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
        try:
            if WINDOWS:
                import msvcrt

                os.lseek(fd, self.WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                os.lseek(fd, 0, os.SEEK_SET)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, getattr(errno, "EDEADLOCK", -1)):
                raise AlreadyRunning(_read_pid(self.path), self.path) from None
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _read_pid(path: Path) -> Optional[int]:
    try:
        text = path.read_text().strip()
        return int(text) if re.fullmatch(r"[0-9]{1,10}", text, re.ASCII) else None
    except OSError:
        return None


def supervised_by_launchd() -> bool:
    """True when launchd started this process from a plist written by ``launchd_plist``.

    The plist says so explicitly (``FABRIC_SERVICE_SUPERVISOR=launchd``); the parent process or
    ``XPC_SERVICE_NAME`` is not evidence — a terminal inside an app carries one too."""
    return os.environ.get(SUPERVISOR_ENV) == "launchd"


def hold_single_instance(data_dir: Path, wait_seconds: Optional[float] = None, *,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> InstanceLock:
    """Take the lock or exit 75 with one sentence naming the holder.

    Started by hand, a held lock exits 75 at once. Under launchd (KeepAlive) an immediate exit
    is a respawn every ThrottleInterval, forever; so a supervised copy first backs off in-process
    (0.5 s doubling to 30 s, ``LOCK_WAIT_SUPERVISED_SECONDS`` in all), takes over if the holder
    leaves, and exits 75 only when the wait runs out. Nothing has been touched while it waits.
    ``wait_seconds`` overrides the choice (0 = never wait)."""
    if wait_seconds is None:
        wait_seconds = LOCK_WAIT_SUPERVISED_SECONDS if supervised_by_launchd() else 0.0
    deadline = clock() + max(0.0, wait_seconds)
    delay = LOCK_BACKOFF_FIRST_SECONDS
    announced = False
    while True:
        try:
            return InstanceLock(data_dir).acquire()
        except AlreadyRunning as exc:
            remaining = deadline - clock()
            if remaining <= 0:
                print(str(exc), file=sys.stderr)
                raise SystemExit(EXIT_ALREADY_RUNNING)
            if not announced:
                print("%s Waiting up to %.0f s for it to exit." % (exc, remaining), file=sys.stderr, flush=True)
                announced = True
            sleep(min(delay, remaining))
            delay = min(delay * 2, LOCK_BACKOFF_MAX_SECONDS)


# --- Windows ACLs (DEC-0032) ---------------------------------------------------

#: The trustees a token file may grant anything to besides the current user: LocalSystem and the
#: Administrators group — the same trust as root reading a 0600 file on POSIX.
WINDOWS_TRUSTED = ("S-1-5-18", "S-1-5-32-544")


def token_acl_problem(path: Path, owner: str, granting: Sequence[str], user: str) -> Optional[str]:
    """service.md "Windows token files", rules 2 and 3, on SIDs already read: the owner is the
    current user, SYSTEM or Administrators (DEC-0033: a file an elevated administrator creates is
    owned by Administrators), and every SID an allow ACE grants anything to is one of those three.
    Deny ACEs are not passed in: they do not change the decision."""
    if owner != user and owner not in WINDOWS_TRUSTED:
        return "Token file %s is owned by %s, not by this user (%s), SYSTEM or Administrators." % (path, owner or "nobody", user)
    for sid in granting:
        if sid != user and sid not in WINDOWS_TRUSTED:
            return "Token file %s grants access to %s; only this user, SYSTEM and Administrators may hold any." % (path, sid)
    return None


if WINDOWS:  # pragma: no cover - exercised by the Windows CI job
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PVOID = ctypes.c_void_p
    _SE_FILE_OBJECT, _OWNER_INFO, _DACL_INFO, _PROTECTED_DACL = 1, 0x1, 0x4, 0x80000000
    _REPARSE_POINT = 0x400
    _advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.POINTER(_PVOID)] * 5
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD] + [_PVOID] * 4
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        _PVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(wintypes.ULONG)]
    _advapi32.GetSecurityDescriptorDacl.argtypes = [_PVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(_PVOID),
                                                    ctypes.POINTER(wintypes.BOOL)]
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, _PVOID, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [_PVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_PVOID)]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [_PVOID]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ACE = re.compile(r"\(([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^;()]*)(?:;[^()]*)?\)")
    _CANONICAL: Dict[str, str] = {}

    def _win_error(what: str, code: Optional[int] = None) -> OSError:
        code = ctypes.get_last_error() if code is None else code
        return OSError(code, "%s: %s" % (what, ctypes.FormatError(code).strip()))

    def _sid_text(sid: Any) -> str:
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _win_error("ConvertSidToStringSidW")
        try:
            return text.value
        finally:
            _kernel32.LocalFree(text)

    def _canonical_sid(trustee: str) -> str:
        """SDDL writes well-known accounts by alias (SY, BA, LA, OW); compare SIDs."""
        if trustee not in _CANONICAL:
            sid = _PVOID()
            if _advapi32.ConvertStringSidToSidW(trustee, ctypes.byref(sid)):
                try:
                    _CANONICAL[trustee] = _sid_text(sid)
                finally:
                    _kernel32.LocalFree(sid)
            else:
                _CANONICAL[trustee] = trustee
        return _CANONICAL[trustee]

    def windows_user_sid() -> str:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise _win_error("OpenProcessToken")
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                raise _win_error("GetTokenInformation")
            return _sid_text(ctypes.cast(buf, ctypes.POINTER(_PVOID))[0])
        finally:
            _kernel32.CloseHandle(token)

    def _sddl(path: Path) -> str:
        sd = _PVOID()
        refs = [_PVOID() for _ in range(4)]
        code = _advapi32.GetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _OWNER_INFO | _DACL_INFO,
                                               *[ctypes.byref(r) for r in refs], ctypes.byref(sd))
        if code:
            raise _win_error("GetNamedSecurityInfoW", code)
        try:
            text = wintypes.LPWSTR()
            if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    sd, 1, _OWNER_INFO | _DACL_INFO, ctypes.byref(text), None):
                raise _win_error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
            try:
                return text.value or ""
            finally:
                _kernel32.LocalFree(text)
        finally:
            _kernel32.LocalFree(sd)

    def windows_acl(path: Path) -> Tuple[str, List[str]]:
        """(owner SID, SIDs of every allow ACE on the object itself); a missing or NULL list grants
        everyone everything, reported as Everyone (S-1-1-0)."""
        text = _sddl(path)
        owner = text.split("O:", 1)[1].split("G:", 1)[0].split("D:", 1)[0] if "O:" in text else ""
        dacl = text.split("D:", 1)[1] if "D:" in text else ""
        if not dacl or dacl.startswith("NO_ACCESS_CONTROL"):
            return _canonical_sid(owner) if owner else "", ["S-1-1-0"]
        granting = [_canonical_sid(trustee) for kind, flags, _r, _o, _i, trustee in _ACE.findall(dacl)
                    if kind in ("A", "OA", "XA", "ZA") and "IO" not in flags]
        return (_canonical_sid(owner) if owner else ""), granting

    def _win_protect(path: Path, folder: bool) -> None:
        """A protected list (inheritance off): this user, SYSTEM, Administrators, full control;
        inherited by what a folder holds."""
        inherit = "OICI" if folder else ""
        sddl = "D:P" + "".join("(A;%s;FA;;;%s)" % (inherit, who) for who in (windows_user_sid(), "SY", "BA"))
        sd = _PVOID()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), _PVOID()
            if not _advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise _win_error("GetSecurityDescriptorDacl")
            code = _advapi32.SetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _DACL_INFO | _PROTECTED_DACL,
                                                   None, None, dacl, None)
            if code:
                raise _win_error("SetNamedSecurityInfoW", code)
        finally:
            _kernel32.LocalFree(sd)

    def _win_token_problem(path: Path, info: os.stat_result) -> Optional[str]:
        if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT or not stat.S_ISREG(info.st_mode):
            return "Token file %s is not a regular file (a link or junction); refusing it." % path
        profile = os.path.normcase(os.path.realpath(os.environ.get("USERPROFILE") or str(Path.home())))
        real = os.path.normcase(os.path.realpath(str(path)))
        if os.path.commonpath([profile, real]) != profile:
            return "Token file %s lies outside the user's profile; refusing it." % path
        owner, granting = windows_acl(path)
        return token_acl_problem(path, owner, granting, windows_user_sid())
else:
    def _win_protect(path: Path, folder: bool) -> None:  # noqa: ARG001 - POSIX uses modes
        raise ServiceError("Windows ACLs exist only on Windows")

    def _win_token_problem(path: Path, info: os.stat_result) -> Optional[str]:  # noqa: ARG001
        raise ServiceError("Windows token rules apply only on Windows")


# --- token -------------------------------------------------------------------

def ensure_token(path: Path) -> str:
    """Create the service token once (0600, owner only); never rotate silently."""
    path = Path(path)
    if path.exists():
        return read_token(path)
    ensure_private_dir(path.parent)
    token = secrets.token_urlsafe(32)
    atomic_write(path, token.encode(), 0o600)
    return token


def read_token(path: Path) -> str:
    """Refuse a symlink, a file owned by someone else, or one others can read.

    On Windows the rule is service.md "Windows token files" (DEC-0032, DEC-0033): a regular file
    whose real path is in the profile, owned by the current user, SYSTEM or Administrators, every
    granting ACE naming one of those three; a refusal names the SID, never the contents."""
    path = Path(path)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ServiceError("Token file %s is a symlink; refusing it." % path)
    if WINDOWS:
        problem = _win_token_problem(path, info)
        if problem:
            raise ServiceError(problem)
    else:
        if info.st_uid != os.getuid():
            raise ServiceError("Token file %s belongs to another user." % path)
        if info.st_mode & 0o077:
            raise ServiceError("Token file %s is readable by others; set mode 0600." % path)
    token = path.read_text().strip()
    if len(token) < 16:
        raise ServiceError("Token file %s holds no usable token." % path)
    return token


def token_matches(presented: Optional[str], token: str, scheme: str = "Bearer") -> bool:
    if not presented:
        return False
    value = presented
    if scheme == "Bearer":
        if not presented.startswith("Bearer "):
            return False
        value = presented[len("Bearer "):]
    return hmac.compare_digest(value.strip().encode(), token.encode())


# --- network guard -----------------------------------------------------------

def check_request(port: int, host: Optional[str], origin: Optional[str] = None,
                  sec_fetch_site: Optional[str] = None) -> Optional[str]:
    """Return why a request must be refused (403), or None when it may proceed."""
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
    if host not in allowed_hosts:
        return "Host %r is not this service." % host
    if origin is not None and origin not in {"http://" + h for h in allowed_hosts}:
        return "Origin %r is not this service." % origin
    if sec_fetch_site == "cross-site":
        return "Cross-site requests are refused."
    return None


# --- descriptor --------------------------------------------------------------

class LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer for a loopback service: threads are daemons, and the bind asks
    no resolver. HTTPServer.server_bind() calls socket.getfqdn() between bind() and listen();
    on a Mac with a slow resolver the port then stays bound but silent, so a host sees
    neither an answer nor a refusal."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def descriptor_path(service_id: str, instance: str = "default", directory: Optional[Path] = None) -> Path:
    return (directory or services_dir()) / ("%s.%s.json" % (service_id, instance))


def read_descriptors(directory: Optional[Path] = None) -> List[Tuple[Path, Dict[str, Any]]]:
    root = directory or services_dir()
    found: List[Tuple[Path, Dict[str, Any]]] = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        try:
            found.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return found


def validate_descriptor(descriptor: Dict[str, Any]) -> List[str]:
    """Structural checks mirroring service-descriptor.schema.json (the schema stays normative)."""
    problems: List[str] = []
    remote = placement_of(descriptor) == "remote"
    if "placement" in descriptor and descriptor["placement"] not in PLACEMENTS:
        problems.append("placement must be local or remote")
    required = ("protocol", "id", "instance", "name", "origin", "auth", "lifecycle", "installedAt", "installedBy")
    if not remote:
        required = required + ("paths",)
    for key in required:
        if key not in descriptor:
            problems.append("missing %s" % key)
    if problems:
        return problems
    if descriptor["protocol"] != PROTOCOL:
        problems.append("protocol must be %s" % PROTOCOL)
    if not _ID.match(str(descriptor["id"])):
        problems.append("id must match %s" % _ID.pattern)
    if not _INSTANCE.match(str(descriptor["instance"])):
        problems.append("instance must match %s" % _INSTANCE.pattern)
    if remote:
        problems.extend(remote_origin_problems(descriptor["origin"]))
    elif not _ORIGIN.match(str(descriptor["origin"])):
        problems.append("origin must be http://127.0.0.1:<port>")
    auth = descriptor.get("auth") or {}
    if "tokenFile" not in auth:
        problems.append("auth.tokenFile is required")
    elif not _LOCAL_PATH.match(str(auth["tokenFile"])):
        problems.append("auth.tokenFile must be a local path, never a network share")
    paths = descriptor.get("paths") or {}
    for value in ([paths["data"]] if "data" in paths else []) + list(paths.get("logs") or []):
        if not _LOCAL_PATH.match(str(value)):
            problems.append("paths must be local paths, never a network share: %r" % value)
    header = auth.get("header", "Authorization")
    scheme = auth.get("scheme", "Bearer")
    if header != "Authorization" and scheme != "none":
        problems.append("a custom auth header carries the raw token: scheme must be none")
    life = descriptor.get("lifecycle") or {}
    manager = life.get("manager")
    if manager not in MANAGERS:
        problems.append("lifecycle.manager must be launchd, systemd, task-scheduler or none")
    if manager == "launchd" and not (life.get("label") and str(life.get("plist", "")).endswith(".plist")):
        problems.append("a launchd service declares label and plist")
    if "plist" in life and not _LOCAL_PATH.match(str(life["plist"])):
        problems.append("lifecycle.plist must be a local path")
    # DEC-0032: `unit` goes with systemd and `task` with Task Scheduler, and with nothing else.
    for field, owner, pattern in (("unit", "systemd", _UNIT), ("task", "task-scheduler", _TASK)):
        if manager == owner and field not in life:
            problems.append("a %s service declares %s" % (owner, field))
        if field in life and manager != owner:
            problems.append("lifecycle.%s belongs to manager %s only" % (field, owner))
        if field in life and not pattern.match(str(life[field])):
            problems.append("lifecycle.%s must match %s" % (field, pattern.pattern))
    if remote:
        if manager != "none":
            problems.append("a remote service is supervised by its platform: lifecycle.manager must be none")
        for field, of in (("label", "launchd"), ("plist", "launchd"), ("unit", "systemd"), ("task", "Task Scheduler")):
            if field in life:
                problems.append("a remote service has no %s %s" % (of, field))
        if "update" in (descriptor.get("commands") or {}):
            problems.append("a remote service declares no update command")
    for name, argv in (descriptor.get("commands") or {}).items():
        if name not in ("doctor", "update"):
            problems.append("unknown command %s" % name)
        elif not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            problems.append("command %s must be an argument array" % name)
        elif not _LOCAL_PATH.match(argv[0]):
            problems.append("command %s must start with an absolute or ~/ executable" % name)
    return problems


def placement_of(descriptor: Dict[str, Any]) -> str:
    """DEC-0019: ``local`` unless the descriptor says ``remote``."""
    return "remote" if (descriptor or {}).get("placement") == "remote" else "local"


def remote_origin_problems(origin: Any) -> List[str]:
    """Problems with a remote origin: https, a public DNS name, an optional port, nothing else."""
    match = _REMOTE_ORIGIN.match(str(origin or ""))
    if not match:
        return ["a remote origin must be https://<dns-name>[:<port>] with no path, query or IP literal"]
    if _RESERVED_HOST.search(match.group(1)):
        return ["a remote service cannot live on the reserved name %s" % match.group(1)]
    if match.group(2) is not None and not 1 <= int(match.group(2)) <= 65535:
        return ["the origin port is out of range"]
    return []


def port_of(origin: str) -> int:
    match = _ORIGIN.match(origin)
    if not match:
        raise ServiceError("Origin %r is not http://127.0.0.1:<port>." % origin)
    return int(match.group(1))


def write_descriptor(descriptor: Dict[str, Any], directory: Optional[Path] = None) -> Path:
    """Installer-only. Refuses a port or id.instance another descriptor claims (a port is a claim)."""
    problems = validate_descriptor(descriptor)
    if problems:
        raise ServiceError("Descriptor is invalid: %s." % "; ".join(problems))
    root = directory or services_dir()
    me = "%s.%s" % (descriptor["id"], descriptor["instance"])
    # DEC-0019: only a local placement claims a port on this computer.
    port = None if placement_of(descriptor) == "remote" else port_of(descriptor["origin"])
    for path, other in read_descriptors(root):
        key = "%s.%s" % (other.get("id"), other.get("instance", "default"))
        if key == me or port is None:  # a remote origin's port is another computer's
            continue
        try:
            other_port = port_of(str(other.get("origin", "")))
        except ServiceError:
            continue
        if other_port == port:
            raise ServiceError("Port %d is already claimed by %s (%s)." % (port, key, path))
    ensure_private_dir(root)
    target = descriptor_path(descriptor["id"], descriptor["instance"], root)
    atomic_write(target, (json.dumps(descriptor, indent=2, ensure_ascii=False) + "\n").encode(), 0o600)
    return target


def remove_descriptor(service_id: str, instance: str = "default", directory: Optional[Path] = None) -> bool:
    try:
        descriptor_path(service_id, instance, directory).unlink()
        return True
    except FileNotFoundError:
        return False


# --- build identity & well-known ------------------------------------------------

def build_info(repo_root: Optional[Path] = None, package_file: Optional[Path] = None) -> Dict[str, Any]:
    """Commit (env FABRIC_BUILD_COMMIT, else git) or a sha256 digest of the package file."""
    commit = os.environ.get("FABRIC_BUILD_COMMIT")
    dirty: Optional[bool] = None
    if not commit and repo_root is not None:
        try:
            commit = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--short=12", "HEAD"],
                                    capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            status = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=5, check=True).stdout
            dirty = bool(status.strip())
        except (OSError, subprocess.SubprocessError):
            commit = None
    info: Dict[str, Any] = {"builtAt": now_iso()}
    if commit:
        info["commit"] = commit
        if dirty is not None:
            info["dirty"] = dirty
    elif package_file is not None and Path(package_file).is_file():
        info["digest"] = "sha256:" + hashlib.sha256(Path(package_file).read_bytes()).hexdigest()
    else:
        raise ServiceError("No build identity: set FABRIC_BUILD_COMMIT, pass repo_root or package_file.")
    return info


def build_well_known(*, service_id: str, instance: str, name: str, version: str, build: Dict[str, Any],
                     started_at: str, status: str, degraded: Sequence[Dict[str, str]],
                     surfaces: Dict[str, Any], summary: Optional[Sequence[Dict[str, Any]]] = None,
                     update_available: Optional[str] = None, pid: Optional[int] = None) -> Dict[str, Any]:
    """The unauthenticated identity answer. `degraded` is always present; ready means empty."""
    if status not in STATUSES:
        raise ServiceError("status must be one of %s." % ", ".join(STATUSES))
    degraded = list(degraded)
    if status == "ready" and degraded:
        status = "degraded"
    if "events" not in surfaces:
        raise ServiceError("surfaces.events is required.")
    doc: Dict[str, Any] = {
        "protocol": PROTOCOL,
        "service": {"id": service_id, "instance": instance, "name": name, "version": version, "build": build},
        "process": {"pid": pid or os.getpid(), "startedAt": started_at},
        "status": status,
        "degraded": degraded,
        "surfaces": surfaces,
        "update": {"available": update_available},
    }
    if summary:
        doc["summary"] = list(summary)[:6]
    return doc


# --- events ------------------------------------------------------------------

def make_event(event_id: Any, at: str, kind: str, level: str, text: str, *, subject: Optional[Dict[str, str]] = None,
               link: Optional[str] = None, notify: bool = False, trace_id: Optional[str] = None,
               span_id: Optional[str] = None) -> Dict[str, Any]:
    """One activity event: one sentence a person reads, never a machine id.

    An event about traced work carries its trace as a pair, `trace_id` and `span_id`
    (fabric-interop/0.1 C3.4 c); fabric_interop.trace_ids(traceparent) gives both."""
    if level not in LEVELS:
        raise ServiceError("level must be one of %s." % ", ".join(LEVELS))
    if not _KIND.match(kind):
        raise ServiceError("kind %r must be dotted lowercase." % kind)
    text = " ".join(str(text).split())
    if not text:
        raise ServiceError("An event needs a sentence.")
    if link is not None and (not link.startswith("/") or link.startswith("//")):
        raise ServiceError("link must be a path on this service.")
    if (trace_id is None) != (span_id is None):
        raise ServiceError("An event carries traceId and spanId together, or neither.")
    if trace_id is not None and not (_TRACE_ID.match(trace_id) and _SPAN_ID.match(str(span_id))):
        raise ServiceError("traceId is 32 and spanId 16 lowercase hex characters, not all zeros.")
    event: Dict[str, Any] = {"id": str(event_id), "at": at, "kind": kind, "level": level, "text": text[:500]}
    if subject:
        event["subject"] = subject
    if link:
        event["link"] = link
    if notify:
        event["notify"] = True
    if trace_id is not None:
        event["traceId"], event["spanId"] = trace_id, span_id
    return event


def parse_limit(raw: Optional[str]) -> int:
    try:
        value = int(raw) if raw not in (None, "") else EVENTS_DEFAULT_LIMIT
    except ValueError:
        raise ServiceError("limit must be an integer.") from None
    return max(1, min(EVENTS_MAX_LIMIT, value))


def events_page(fetch: Callable[[Optional[str], int], Iterable[Dict[str, Any]]],
                after: Optional[str], limit: int) -> Dict[str, Any]:
    """A cursor page over the log the service already keeps.

    `fetch(after, limit)` returns events with ids greater than `after` in
    ascending order (or the newest `limit`, ascending, when `after` is None).
    """
    events = list(fetch(after, limit))[:limit]
    cursor = events[-1]["id"] if events else (after or None)
    return {"events": events, "cursor": cursor}


class JsonlEventLog:
    """A minimal append-only log for services that keep none yet. Ids are integers as strings."""

    def __init__(self, path: Path, keep: int = 5000):
        self.path = Path(path)
        self.keep = keep

    def _all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def append(self, kind: str, level: str, text: str, **extra: Any) -> Dict[str, Any]:
        rows = self._all()
        next_id = int(rows[-1]["id"]) + 1 if rows else 1
        event = make_event(next_id, now_iso(), kind, level, text, **extra)
        rows.append(event)
        rows = rows[-self.keep:]
        atomic_write(self.path, ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode(), 0o600)
        return event

    def fetch(self, after: Optional[str], limit: int) -> List[Dict[str, Any]]:
        rows = self._all()
        if after is None:
            return rows[-limit:]
        try:
            floor = int(after)
        except ValueError:
            raise ServiceError("cursor %r is not from this service." % after) from None
        return [r for r in rows if int(r["id"]) > floor][:limit]


# --- usage report (DEC-0021) -------------------------------------------------------

USAGE_PATH = "/fabric/v1/usage"
USAGE_DAYS = 31
COST_BASES = ("provider", "price-list", "unknown")
_PROVIDER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


def make_usage_receipt(provider: str, model: str, *, input_tokens: int, output_tokens: int,
                       cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                       cost_usd: Optional[float] = None, cost_basis: Optional[str] = None,
                       at: Optional[str] = None) -> Dict[str, Any]:
    """One model call, from the provider's own usage numbers. No prompt, output or caller.

    `cost_usd` is None when the call cannot be priced: the report counts it as unpriced and
    never as $0. `cost_basis` is `provider` (the provider reported the charge) or `price-list`
    (computed from a published price list); it is `unknown` when there is no cost."""
    if not _PROVIDER.match(provider or ""):
        raise ServiceError("provider must be a lowercase name such as anthropic or openrouter.")
    model = str(model or "").strip()
    if not model or len(model) > 128:
        raise ServiceError("model must be 1 to 128 characters.")
    counts = {"inputTokens": input_tokens, "outputTokens": output_tokens,
              "cacheReadTokens": cache_read_tokens, "cacheWriteTokens": cache_write_tokens}
    for name, value in counts.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ServiceError("%s must be a non-negative integer." % name)
    if cost_usd is not None and (isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)) or cost_usd < 0 or cost_usd != cost_usd):
        raise ServiceError("cost_usd must be a non-negative number or None.")
    basis = cost_basis or ("unknown" if cost_usd is None else "provider")
    if basis not in COST_BASES or (cost_usd is None) != (basis == "unknown"):
        raise ServiceError("cost_basis is provider or price-list with a cost, unknown without one.")
    return {"at": at or now_iso(), "provider": provider, "model": model, **counts,
            "costUsd": None if cost_usd is None else float(cost_usd), "costBasis": basis}


def _add_totals(row: Dict[str, Any], receipt: Dict[str, Any]) -> None:
    row["calls"] += 1
    for name in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
        row[name] += int(receipt.get(name) or 0)
    if receipt.get("costUsd") is None:
        row["unpricedCalls"] += 1
    else:
        row["_cost"] += float(receipt["costUsd"])


def _close_totals(row: Dict[str, Any]) -> Dict[str, Any]:
    cost = row.pop("_cost")
    row["costUsd"] = None if row["calls"] and row["unpricedCalls"] == row["calls"] else round(cost, 6)
    return row


def usage_report(receipts: Iterable[Dict[str, Any]], *, service_id: str, instance: str = "default",
                 now: Optional[_dt.datetime] = None,
                 budget: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The `service-usage.schema.json` answer: the last 31 UTC days, oldest first, per provider
    and model. Days without calls are omitted. A row whose calls are all unpriced has
    `costUsd: None`; totals are the sums of the model rows (FAC-SEM-025)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    first = (now - _dt.timedelta(days=USAGE_DAYS - 1)).strftime("%Y-%m-%d")
    last = now.strftime("%Y-%m-%d")
    days: Dict[str, Dict[str, Dict[str, Any]]] = {}
    bases: Dict[Tuple[str, str, str], set] = {}
    for r in receipts:
        date = str(r.get("at", ""))[:10]
        if not (first <= date <= last):
            continue
        key = (r["provider"], r["model"])
        row = days.setdefault(date, {}).setdefault("%s\u0000%s" % key, {
            "provider": r["provider"], "model": r["model"], "calls": 0, "unpricedCalls": 0,
            "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "_cost": 0.0})
        _add_totals(row, r)
        bases.setdefault((date,) + key, set()).add(r.get("costBasis") or "unknown")
    out_days = []
    for date in sorted(days):
        models = []
        for row in days[date].values():
            seen = bases[(date, row["provider"], row["model"])]
            row["costBasis"] = next(iter(seen)) if len(seen) == 1 else "mixed"
            models.append(_close_totals(row))
        models.sort(key=lambda m: (m["provider"], m["model"]))
        day = {"date": date, "calls": 0, "unpricedCalls": 0, "inputTokens": 0, "outputTokens": 0,
               "cacheReadTokens": 0, "cacheWriteTokens": 0, "_cost": 0.0}
        for m in models:
            for name in ("calls", "unpricedCalls", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
                day[name] += m[name]
            day["_cost"] += m["costUsd"] or 0.0
        day = _close_totals(day)
        day["byModel"] = models
        out_days.append(day)
    report: Dict[str, Any] = {"protocol": PROTOCOL, "service": {"id": service_id, "instance": instance},
                              "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "currency": "USD", "days": out_days}
    if budget is not None:
        if budget.get("period") not in ("day", "month") or not isinstance(budget.get("limitUsd"), (int, float)) or budget["limitUsd"] <= 0:
            raise ServiceError("budget is {period: day|month, limitUsd > 0}.")
        prefix = last if budget["period"] == "day" else last[:7]
        window = [d for d in out_days if d["date"].startswith(prefix)]
        unknown = bool(window) and all(d["costUsd"] is None for d in window)
        spent = None if unknown else round(sum(d["costUsd"] or 0.0 for d in window), 6)
        report["budget"] = {"period": budget["period"], "limitUsd": float(budget["limitUsd"]), "spentUsd": spent}
    return report


class JsonlUsageLedger:
    """Usage receipts for services that keep none yet: append-only JSON lines, mode 0600, pruned
    to the reported window so the file stays bounded (LC-12). One writer process — the service,
    which holds its instance lock; the lock here orders that process's threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, receipt: Dict[str, Any]) -> None:
        line = json.dumps(receipt, separators=(",", ":")) + "\n"
        with self._lock:
            ensure_private_dir(self.path.parent)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o600)
            try:
                size = os.fstat(fd).st_size
                if size and _read_at(fd, 1, size - 1) != b"\n":
                    line = "\n" + line  # a killed writer left a torn line: never glue a receipt to it
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

    def receipts(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn last line from a killed writer is skipped, not fatal
        return out

    def prune(self, now: Optional[_dt.datetime] = None) -> int:
        """Drop receipts older than the reported window; returns how many were dropped."""
        now = now or _dt.datetime.now(_dt.timezone.utc)
        first = (now - _dt.timedelta(days=USAGE_DAYS - 1)).strftime("%Y-%m-%d")
        with self._lock:
            every = self.receipts()
            kept = [r for r in every if str(r.get("at", ""))[:10] >= first]
            dropped = len(every) - len(kept)
            if dropped:
                atomic_write(self.path, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in kept).encode("utf-8"))
            return dropped

    def report(self, *, service_id: str, instance: str = "default", now: Optional[_dt.datetime] = None,
               budget: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return usage_report(self.receipts(), service_id=service_id, instance=instance, now=now, budget=budget)


# --- logs (LC-12) ---------------------------------------------------------------

class RotatingLog:
    """One structured log: JSON lines rotated by size (5 x 5 MB by default), files 0600 in a
    0700 directory. ``service.jsonl`` -> ``.1`` ... ``.<backups>``; the oldest falls off.

    Never pass a token, a cookie, a login code or a request body that may carry one."""

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        if max_bytes < 256 or backups < 1:
            raise ServiceError("A rotating log needs max_bytes >= 256 and at least one backup.")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def _line(self, record: Dict[str, Any]) -> bytes:
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        if len(data) <= self.max_bytes:
            return data
        record = dict(record, truncated=True)
        message = str(record.get("message", ""))
        while message:
            message = message[: len(message) // 2]
            record["message"] = message
            data = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            if len(data) <= self.max_bytes:
                return data
        return (json.dumps({"at": record.get("at"), "level": record.get("level"), "message": "",
                            "truncated": True}) + "\n").encode()

    def write(self, level: str, message: str, **fields: Any) -> None:
        record: Dict[str, Any] = {"at": now_iso(), "level": str(level), "message": str(message)}
        record.update(fields)
        data = self._line(record)
        with self._lock:
            ensure_private_dir(self.path.parent)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data) > self.max_bytes:
                self._rotate()
            fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

    def _rotate(self) -> None:
        for index in range(self.backups - 1, 0, -1):
            older = Path("%s.%d" % (self.path, index))
            if older.exists():
                os.replace(older, "%s.%d" % (self.path, index + 1))
        os.replace(self.path, "%s.1" % self.path)


def cap_stdout_log(path: Path, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """Call once at start: a launchd stdout file over ``max_bytes`` is copied to ``<path>.1`` and
    truncated in place. launchd holds the file open for append, so a rename would leave it
    writing to the old name; truncation keeps its descriptor valid. Returns True when capped."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= max_bytes:
        return False
    backup = Path("%s.1" % path)
    shutil.copyfile(path, backup)
    os.chmod(backup, 0o600)
    with open(path, "r+b") as handle:
        handle.truncate(0)
    return True


# --- shutdown (LC-01) -------------------------------------------------------------

class Stopping(ServiceError):
    """The service is stopping and takes no new work."""


class Drain:
    """SIGTERM/SIGINT: stop taking new work, drain in-flight work until ``deadline``, then hand
    over with ``on_stop(drained)``. A hand-over that hangs is cut by a hard exit
    (``EXIT_HARD_STOP``) ``grace`` seconds later, so quit reaches exit inside the lifecycle bound.

        drain = fs.Drain()
        drain.install(lambda drained: server.shutdown(),
                      on_stopping=lambda: log.append("service.stopping", "info", "Stopping."))
        with drain.work():          # raises fs.Stopping once a stop has begun
            ...
    """

    def __init__(self, deadline: float = DRAIN_SECONDS, grace: float = DRAIN_GRACE_SECONDS):
        self.deadline = deadline
        self.grace = grace
        self._cond = threading.Condition()
        self._inflight = 0
        self._stopping = False

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def inflight(self) -> int:
        return self._inflight

    @contextlib.contextmanager
    def work(self):
        with self._cond:
            if self._stopping:
                raise Stopping("The service is stopping and takes no new work.")
            self._inflight += 1
        try:
            yield
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    def request_stop(self) -> bool:
        """Refuse new work from now on; False when a stop had already begun."""
        with self._cond:
            if self._stopping:
                return False
            self._stopping = True
            return True

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for in-flight work to finish; True when it did inside ``timeout``."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._inflight > 0:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def install(self, on_stop: Callable[[bool], None], *, on_stopping: Optional[Callable[[], None]] = None,
                signals: Sequence[int] = (signal.SIGTERM, signal.SIGINT)) -> "Drain":
        """Install the handlers (main thread only). A second signal changes nothing: the hard-exit
        timer already holds the deadline."""
        def handler(_signum: int, _frame: Any) -> None:
            if self.request_stop():
                threading.Thread(target=self._stop, args=(on_stopping, on_stop), daemon=True).start()

        for sig in signals:
            signal.signal(sig, handler)
        return self

    def _stop(self, on_stopping: Optional[Callable[[], None]], on_stop: Callable[[bool], None]) -> None:
        hard = threading.Timer(self.deadline + self.grace, os._exit, args=(EXIT_HARD_STOP,))
        hard.daemon = True
        hard.start()
        if on_stopping is not None:
            try:
                on_stopping()
            except Exception:  # a failing stop notice must not stop the drain
                traceback.print_exc()
        on_stop(self.wait(self.deadline))


# --- operator login ----------------------------------------------------------

class LoginCodes:
    """Single-use login codes (<=120 s), recorded as used BEFORE they are honoured,
    and HMAC-signed session cookies revoked by rotating the key."""

    def __init__(self, state_dir: Optional[Path], ttl: int = LOGIN_CODE_TTL_SECONDS, *,
                 store: Optional["MemoryCodeStore"] = None, key: Optional[bytes] = None):
        # A local service keeps codes and the key in files. An online one (DEC-0019) usually has no
        # durable disk: pass state_dir=None with store=MemoryCodeStore() (a restart forgets every
        # code, so none can be replayed) and key= bytes from a platform secret (sessions survive a deploy).
        self.ttl = min(ttl, LOGIN_CODE_TTL_SECONDS)
        self._store = store
        self._fixed_key = key
        if state_dir is not None:
            self.dir = ensure_private_dir(Path(state_dir))
            self._key_path = self.dir / "session.key"
            self._codes_path = self.dir / "login-codes.json"
        elif store is None or key is None:
            raise ServiceError("LoginCodes without a state directory needs store= and key=.")
        if key is not None and len(key) < 32:
            raise ServiceError("the session key must be at least 32 bytes.")

    def _key(self) -> bytes:
        if self._fixed_key is not None:
            return bytes(self._fixed_key)
        if not self._key_path.exists():
            atomic_write(self._key_path, secrets.token_bytes(32), 0o600)
        return self._key_path.read_bytes()

    def _load(self) -> Dict[str, Any]:
        if self._store is not None:
            return self._store.load()
        try:
            return json.loads(self._codes_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, codes: Dict[str, Any]) -> None:
        horizon = time.time() - 3600
        codes = {k: v for k, v in codes.items() if v.get("expires", 0) > horizon}
        if self._store is not None:
            self._store.save(codes)
            return
        atomic_write(self._codes_path, json.dumps(codes).encode(), 0o600)

    def issue(self) -> Dict[str, str]:
        code = secrets.token_urlsafe(24)
        expires = time.time() + self.ttl
        codes = self._load()
        codes[hashlib.sha256(code.encode()).hexdigest()] = {"expires": expires, "used": False}
        self._save(codes)
        at = _dt.datetime.fromtimestamp(expires, _dt.timezone.utc).replace(microsecond=0)
        return {"url": "/fabric/v1/login?code=" + code, "expiresAt": at.isoformat().replace("+00:00", "Z")}

    def redeem(self, code: Optional[str]) -> Optional[str]:
        """Return a session cookie value, or None. The code is burned even when it has expired."""
        if not code or not _CODE.match(code):
            return None
        digest = hashlib.sha256(code.encode()).hexdigest()
        codes = self._load()
        entry = codes.get(digest)
        if not entry or entry.get("used"):
            return None
        entry["used"] = True
        self._save(codes)
        if entry.get("expires", 0) < time.time():
            return None
        return self._sign(secrets.token_urlsafe(18))

    def _sign(self, session_id: str) -> str:
        mac = hmac.new(self._key(), session_id.encode(), hashlib.sha256).hexdigest()
        return session_id + "." + mac

    def session_valid(self, cookie_value: Optional[str]) -> bool:
        if not cookie_value or "." not in cookie_value:
            return False
        session_id, mac = cookie_value.rsplit(".", 1)
        expected = hmac.new(self._key(), session_id.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, expected)

    def revoke_all(self) -> None:
        if self._fixed_key is not None:
            raise ServiceError("a platform-held session key is rotated on the platform, not here.")
        atomic_write(self._key_path, secrets.token_bytes(32), 0o600)


class MemoryCodeStore:
    """DEC-0019: a login-code store held in memory — for an online service with no durable disk."""

    def __init__(self) -> None:
        self._codes: Dict[str, Any] = {}

    def load(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._codes))

    def save(self, codes: Dict[str, Any]) -> None:
        self._codes = json.loads(json.dumps(codes))


SESSION_COOKIE = "fabric_session"
REMOTE_SESSION_COOKIE = "__Host-fabric_session"


def session_cookie_header(value: str, max_age: int = 30 * 86400) -> str:
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (SESSION_COOKIE, value, max_age)


def remote_session_cookie_header(value: str, max_age: int = 30 * 86400) -> str:
    """DEC-0019: the remote cookie — __Host- name, Secure, no Domain, Path=/."""
    return "%s=%s; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=%d" % (REMOTE_SESSION_COOKIE, value, max_age)


def check_remote_request(origin: str, host: Optional[str], request_origin: Optional[str] = None,
                         sec_fetch_site: Optional[str] = None, forwarded_proto: Optional[str] = None) -> Optional[str]:
    """DEC-0019: the request guard of an online service; returns the refusal sentence or None.
    ``forwarded_proto`` is the platform-set scheme where TLS ends before the process; pass None
    when the process terminates TLS itself."""
    own = urllib.parse.urlsplit(origin)
    own_host = own.netloc.lower()
    if str(host or "").lower() != own_host:
        return "Host %s is not this service." % host
    if forwarded_proto is not None and str(forwarded_proto).split(",")[0].strip() != "https":
        return "This service answers over https only."
    if request_origin is not None and request_origin != "%s://%s" % (own.scheme, own_host):
        return "Origin %s is not this service." % request_origin
    if sec_fetch_site == "cross-site":
        return "Cross-site requests are refused."
    return None


def well_known_allowed(placement: str, authorization: Optional[str], token: str, scheme: str = "Bearer") -> bool:
    """DEC-0019: a local service answers anyone (the loopback guard already ran); a remote one only
    the bearer of the service token. False is a 401 with an EMPTY body."""
    return placement != "remote" or token_matches(authorization, token, scheme)


def remote_token_file(service_id: str, instance: str = "default", directory: Optional[Path] = None) -> Path:
    return (directory or services_dir()).parent / "tokens" / ("%s.%s.token" % (service_id, instance))


def register_remote(*, service_id: str, name: str, origin: str, token: str, instance: str = "default",
                    summary: Optional[str] = None, doctor: Optional[List[str]] = None,
                    directory: Optional[Path] = None, installed_by: str = "fabric-service register-remote") -> Path:
    """DEC-0019, installer side: register an online service on THIS computer — the token file
    (0600, never printed) and the descriptor. The same token is set on the platform as a secret."""
    if not token or len(token.strip()) < 16:
        raise ServiceError("the service token must be at least 16 characters.")
    root = directory or services_dir()
    token_file = remote_token_file(service_id, instance, root)
    descriptor: Dict[str, Any] = {"protocol": PROTOCOL, "id": service_id, "instance": instance, "name": name,
                                  "placement": "remote", "origin": origin, "auth": {"tokenFile": str(token_file)},
                                  "lifecycle": {"manager": "none"}, "installedAt": now_iso(), "installedBy": installed_by}
    if summary:
        descriptor["summary"] = summary
    if doctor:
        descriptor["commands"] = {"doctor": list(doctor)}
    problems = validate_descriptor(descriptor)
    if problems:
        raise ServiceError("Descriptor is invalid: %s." % "; ".join(problems))
    ensure_private_dir(token_file.parent)
    atomic_write(token_file, token.strip().encode(), 0o600)
    return write_descriptor(descriptor, root)


def cookie_value(cookie_header: Optional[str], name: str = SESSION_COOKIE) -> Optional[str]:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


# --- launchd -----------------------------------------------------------------

def launchd_plist(label: str, program_arguments: Sequence[str], *, working_directory: Path,
                  stdout_path: Path, environment: Optional[Dict[str, str]] = None,
                  exit_timeout: int = DEFAULT_EXIT_TIMEOUT) -> bytes:
    """RunAtLoad + KeepAlive true + ThrottleInterval 10; no secret may appear in `environment`.

    ``ExitTimeOut`` (15 s) sits above the drain and its hard exit (``Drain``: 8 + 2 s), so launchd's
    SIGKILL never arrives first. ``FABRIC_SERVICE_SUPERVISOR=launchd`` tells the lock to back off
    instead of exiting into a respawn loop."""
    env = dict(environment or {})
    for key in env:
        if re.search(r"(TOKEN|SECRET|PASSWORD|KEY)$", key) and not key.endswith("_FILE"):
            raise ServiceError("Environment variable %s looks like a secret; pass a *_FILE path instead." % key)
    env.setdefault("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env[SUPERVISOR_ENV] = "launchd"
    plist = {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": exit_timeout,
        # Standard, never Background: a host probes the service and agents call it while the Mac
        # is busy; Background (and Nice/LowPriorityIO) lets macOS starve it for tens of seconds
        # under load, and every host then reports an outage that never happened.
        "ProcessType": "Standard",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stdout_path),
        "EnvironmentVariables": env,
    }
    return plistlib.dumps(plist)


def _launchctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)


def _domain() -> str:
    if sys.platform == "win32":
        # DEC-0032: launchd is macOS's supervisor; Windows has Task Scheduler, and no uid.
        raise ServiceError("launchd exists only on macOS; this system's supervisor is Task Scheduler.")
    return "gui/%d" % os.getuid()


def launchd_loaded(label: str) -> bool:
    return _launchctl("print", "%s/%s" % (_domain(), label)).returncode == 0


def fetch_well_known(origin: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(origin + "/.well-known/fabric-service", timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


_OVERRIDE_ROW = re.compile(r'^\s*"([^"]+)"\s*=>\s*(\S+)\s*$')


def launchd_override(label: str) -> Optional[str]:
    """The operator's launchd override for ``label``: ``"disabled"``, ``"enabled"``, None when
    none is recorded (a first install), or ``"unknown"`` when the table cannot be read.

    Reads ``launchctl print-disabled gui/<uid>``; macOS 14+ prints ``=> disabled|enabled``,
    older releases ``=> true|false`` (true means disabled)."""
    result = _launchctl("print-disabled", _domain())
    if result.returncode != 0:
        return "unknown"
    for line in result.stdout.splitlines():
        match = _OVERRIDE_ROW.match(line)
        if match and match.group(1) == label:
            return "disabled" if match.group(2) in ("disabled", "true") else "enabled"
    return None


def launchd_install(label: str, plist_path: Path, plist_bytes: bytes, *, origin: str,
                    service_id: str, instance: str = "default", timeout: float = 40.0,
                    force_enable: bool = False) -> Dict[str, Any]:
    """Write + lint the plist, bootout and WAIT for unload, bootstrap (retry EIO 5), then
    poll the well-known document until it answers with this identity.

    The operator's intent wins (LC-14): only a first install (no override recorded) enables the
    label. A label the operator disabled stays disabled and unloaded — the plist is still
    rewritten, so the new release is what starts once they turn it back on — and the answer is
    ``{"disabled": True, "label", "plist", "loaded"}`` instead of the well-known document.
    ``force_enable`` is for an operator who asked for the service to be switched on."""
    plist_path = Path(plist_path)
    atomic_write(plist_path, plist_bytes, 0o644)
    lint = subprocess.run(["plutil", "-lint", str(plist_path)], capture_output=True, text=True)
    if lint.returncode != 0:
        raise ServiceError("plist failed plutil -lint: %s" % lint.stdout.strip())
    target = "%s/%s" % (_domain(), label)
    override = launchd_override(label)
    if force_enable or override is None:
        _launchctl("enable", target)
    elif override == "disabled":
        return {"disabled": True, "label": label, "plist": str(plist_path), "loaded": launchd_loaded(label)}
    _launchctl("bootout", target)
    deadline = time.time() + 15
    while launchd_loaded(label) and time.time() < deadline:
        time.sleep(0.25)
    last = ""
    for _ in range(5):
        result = _launchctl("bootstrap", _domain(), str(plist_path))
        if result.returncode == 0:
            break
        last = (result.stderr or result.stdout).strip()
        time.sleep(1.0)
    else:
        raise ServiceError("launchctl bootstrap failed: %s" % last)
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = fetch_well_known(origin)
        if doc and doc.get("service", {}).get("id") == service_id and doc.get("service", {}).get("instance") == instance:
            return doc
        time.sleep(0.5)
    raise ServiceError("%s did not answer as %s.%s within %.0f s; read its log." % (origin, service_id, instance, timeout))


def launchd_uninstall(label: str, plist_path: Path, *, timeout: float = 30.0, purge: bool = False,
                      service_id: Optional[str] = None) -> Dict[str, Any]:
    """Symmetric with ``launchd_install`` (LC-14): bootout and WAIT until the job is gone, remove
    the plist, and reset the override to ``enabled`` — launchctl has no verb that deletes an
    override, and a leftover ``disabled`` would make a later fresh install stay off. Data stays
    unless ``purge`` (then ``service_id`` names whose data, logs and cache go). A job still
    loaded at ``timeout`` raises, leaving the plist and data in place."""
    if purge and not service_id:
        raise ServiceError("purge needs the service id whose data, logs and cache to remove.")
    target = "%s/%s" % (_domain(), label)
    _launchctl("bootout", target)
    deadline = time.monotonic() + timeout
    while launchd_loaded(label):
        if time.monotonic() >= deadline:
            raise ServiceError("launchd job %s is still loaded %.0f s after bootout; nothing was removed." % (label, timeout))
        time.sleep(0.1)
    try:
        Path(plist_path).unlink()
        plist_removed = True
    except FileNotFoundError:
        plist_removed = False
    _launchctl("enable", target)
    purged: List[str] = []
    if purge:
        for directory in service_dirs(str(service_id)).values():
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
            else:
                continue
            purged.append(str(directory))
    return {"unloaded": True, "plistRemoved": plist_removed, "override": "enabled", "purged": purged}


def prune_releases(releases_dir: Path, keep: int = 2, *, current: Optional[Path] = None) -> List[Path]:
    """Keep the release the job runs and the one before it (LC-11 rollback, LC-15); remove
    older release directories, newest first by modification time. ``current`` (a release or a
    symlink to one) is never removed, even after a rollback to an older release. Symlinks and
    files in ``releases_dir`` are left alone. Returns what was removed."""
    if keep < 2:
        raise ServiceError("keep at least the current release and the one before it (keep >= 2).")
    root = Path(releases_dir)
    if not root.is_dir():
        return []
    releases = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    releases.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    keepers: List[Path] = []
    if current is not None:
        resolved = Path(current).resolve()
        keepers.extend(p for p in releases if p.resolve() == resolved)
    for release in releases:
        if len(keepers) >= keep:
            break
        if release not in keepers:
            keepers.append(release)
    removed = [p for p in releases if p not in keepers]
    for release in removed:
        shutil.rmtree(release)
    return removed


# --- helpers for input validation -----------------------------------------------

def _require_id(service_id: str) -> None:
    if not _ID.match(service_id):
        raise ServiceError("Service id %r must match %s." % (service_id, _ID.pattern))


__all__ = [name for name in dir() if not name.startswith("_")]
