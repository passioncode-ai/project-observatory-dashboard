"""The privacy boundary on every OS the engine runs on (docs/design/WINDOWS-LINUX.md, W2b).

The engine's claim is the same everywhere — only the owner reads the workspace — and each OS
proves it its own way. POSIX keeps it in mode bits (0600, 0700); Windows keeps no mode bits at
all (`st_mode` reads 0o666 for any writable file) and keeps it in the file's access-control list.
Every privacy decision goes through here:

    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW)   # binary, refuses a link
    osprivacy.private(path_or_fd)          # nobody but the owner may read it
    osprivacy.others_may_write(path_or_fd) # someone else may change it
    osprivacy.owned_by_me(path_or_fd)      # its owner is this account
    osprivacy.make_private(path)           # owner only, from now on
    osprivacy.private_folder(path)         # an existing folder, owner only, never through a link
    osprivacy.loose(path, 0o600)           # wants tightening: not exactly `mode` / not owner-only
    osprivacy.describe(path_or_fd)         # "0600" on POSIX, "owner-only"/"shared" on Windows
    osprivacy.explain(path_or_fd)          # the mode and uid, or the owner and ACL, for a diagnostic
    osprivacy.real_home()                  # the account's home, never from HOME/USERPROFILE

POSIX: the calls the engine made before — `O_NOFOLLOW`, `S_IMODE(st_mode) & 0o077`, `st_uid`,
`chmod`, the password database.

Windows:
- `open` adds `O_BINARY`. Without it the C runtime opens a descriptor in TEXT mode and turns
  every LF written through it into CRLF — `os.fdopen(fd, "wb")` does not undo that.
- `NOFOLLOW`, `DIRECTORY` and `NONBLOCK` are this module's own bits there, stripped by `open`:
  a link (symbolic link or junction) is refused by looking at it first, which leaves a window
  between the look and the open that `O_NOFOLLOW` closes on POSIX; a directory has no descriptor
  on Windows, so `DIRECTORY` and `dir_fd` raise `NotImplementedError` and the caller takes its
  path-based branch; there are no FIFOs to wait on.
- "Private" means every ACE that grants access names this account, LocalSystem or the
  Administrators group (the counterpart of root, who reads every file on POSIX too). An ACE that
  only passes inheritance on to children (`IO`) grants nothing on the object itself.
- `make_private` replaces the list with a protected one: this account, LocalSystem and
  Administrators, full control, inherited by whatever is created inside a folder.
- The account's home comes from the known-folder store (`SHGetKnownFolderPath(FOLDERID_Profile)`),
  which reads the account's profile and not the environment.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Union

WINDOWS = os.name == "nt"
Target = Union[str, os.PathLike, int]

if not WINDOWS:
    NOFOLLOW = os.O_NOFOLLOW
    DIRECTORY = os.O_DIRECTORY
    NONBLOCK = os.O_NONBLOCK
    BINARY = 0
else:
    # Bits the C runtime does not define (its highest is _O_U8TEXT, 0x40000); `open` strips them.
    NOFOLLOW, DIRECTORY, NONBLOCK = 0x1000000, 0x2000000, 0x4000000
    BINARY = os.O_BINARY


def _stat(target: Target, follow: bool = True) -> os.stat_result:
    if isinstance(target, int):
        return os.fstat(target)
    return os.stat(target) if follow else os.lstat(target)


def private_folder(path) -> None:
    """Make an existing folder owner-only without following a link to it."""
    if WINDOWS:
        folder = Path(path)
        if folder.is_symlink() or folder.is_junction():
            raise OSError(errno.ELOOP, "refusing to follow a link", os.fspath(path))
        make_private(folder)
        return
    fd = open(path, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def loose(target: Target, mode: int) -> bool:
    """Whether `target` wants tightening: on POSIX its permission bits are not exactly `mode`;
    on Windows, which keeps no bits, it is not owner-only."""
    if WINDOWS:
        return not private(target)
    return stat.S_IMODE(_stat(target).st_mode) != mode


if not WINDOWS:
    def open(path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:  # noqa: A001
        """`os.open`, unchanged on POSIX."""
        return os.open(path, flags, mode, dir_fd=dir_fd)

    def private(target: Target) -> bool:
        """No group or world permission bit is set."""
        return not stat.S_IMODE(_stat(target).st_mode) & 0o077

    def others_may_write(target: Target) -> bool:
        return bool(stat.S_IMODE(_stat(target).st_mode) & 0o022)

    def owned_by_me(target: Target) -> bool:
        return _stat(target).st_uid == os.getuid()

    def make_private(target: Target) -> None:
        """0700 for a folder, 0600 for anything else."""
        info = _stat(target)
        mode = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        if isinstance(target, int):
            os.fchmod(target, mode)
        else:
            os.chmod(target, mode)

    def describe(target: Target) -> str:
        return f"{stat.S_IMODE(_stat(target).st_mode):04o}"

    def explain(target: Target) -> str:
        """Mode and owner, for a diagnostic, never a decision."""
        info = _stat(target)
        return f"mode {stat.S_IMODE(info.st_mode):04o}, uid {info.st_uid} (this account: {os.getuid()})"

    def real_home() -> Path | None:
        """The home the password database records for this uid; None when it has none."""
        import pwd
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            return None

else:  # pragma: no cover - exercised by the Windows CI job
    import ctypes
    import msvcrt
    import uuid
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _SE_FILE_OBJECT = 1
    _OWNER = 0x1
    _DACL = 0x4
    _PROTECTED_DACL = 0x80000000
    _SDDL_REVISION_1 = 1
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _REPARSE_POINT = 0x400
    _PVOID = ctypes.c_void_p

    _advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                                ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
                                                ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
                                                ctypes.POINTER(_PVOID)]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                          ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
                                          ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
                                          ctypes.POINTER(_PVOID)]
    _advapi32.GetSecurityInfo.restype = wintypes.DWORD
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        _PVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG)]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(wintypes.ULONG)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [_PVOID, ctypes.POINTER(wintypes.BOOL),
                                                    ctypes.POINTER(_PVOID), ctypes.POINTER(wintypes.BOOL)]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                                _PVOID, _PVOID, _PVOID, _PVOID]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, _PVOID, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [_PVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_PVOID)]
    _advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [_PVOID]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    #: Trustees that may hold access to a private file, as canonical SIDs: LocalSystem and the
    #: Administrators group; this account is added by `_trusted()`. SDDL writes well-known
    #: accounts by alias — `SY`, `BA`, and `LA` for the built-in Administrator account (RID 500),
    #: which a CI runner's account can be — so every trustee is turned back into its SID first.
    _ROOTLIKE = {"S-1-5-18", "S-1-5-32-544"}
    #: Two-letter rights that only read or run; anything else in an ACE counts as a write.
    _READ_RIGHTS = {"FR", "FX", "GR", "GX", "RC", "LC", "RP", "LO", "SW"}
    #: FILE_WRITE_DATA, APPEND, WRITE_EA, DELETE_CHILD, WRITE_ATTRIBUTES, DELETE, WRITE_DAC,
    #: WRITE_OWNER, GENERIC_ALL, GENERIC_WRITE.
    _WRITE_MASK = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
    _ACE = re.compile(r"\(([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^;()]*)(?:;[^()]*)?\)")
    _user_sid: list[str] = []
    _canonical: dict[str, str] = {}

    def _sid_text(sid) -> str:
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _fail(ctypes.get_last_error(), "ConvertSidToStringSidW")
        try:
            return text.value
        finally:
            _kernel32.LocalFree(text)

    def _sid(trustee: str) -> str:
        """The canonical `S-1-…` form of an SDDL trustee, alias or not."""
        if trustee not in _canonical:
            sid = _PVOID()
            if not _advapi32.ConvertStringSidToSidW(trustee, ctypes.byref(sid)):
                _canonical[trustee] = trustee      # unknown alias: compared as written, never trusted
            else:
                try:
                    _canonical[trustee] = _sid_text(sid)
                finally:
                    _kernel32.LocalFree(sid)
        return _canonical[trustee]

    def _fail(code: int, what: str) -> OSError:
        return OSError(code, f"{what}: {ctypes.FormatError(code).strip()}")

    def user_sid() -> str:
        """This account's SID, from the process token."""
        if _user_sid:
            return _user_sid[0]
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise _fail(ctypes.get_last_error(), "OpenProcessToken")
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
                raise _fail(ctypes.get_last_error(), "GetTokenInformation")
            sid = ctypes.cast(buf, ctypes.POINTER(_PVOID))[0]   # TOKEN_USER.User.Sid
            _user_sid.append(_sid_text(sid))
        finally:
            _kernel32.CloseHandle(token)
        return _user_sid[0]

    def _trusted() -> set[str]:
        return _ROOTLIKE | {user_sid()}

    def _sddl(target: Target, what: int) -> str:
        sd = _PVOID()
        owner, group, dacl, sacl = _PVOID(), _PVOID(), _PVOID(), _PVOID()
        if isinstance(target, int):
            code = _advapi32.GetSecurityInfo(msvcrt.get_osfhandle(target), _SE_FILE_OBJECT, what,
                                             ctypes.byref(owner), ctypes.byref(group), ctypes.byref(dacl),
                                             ctypes.byref(sacl), ctypes.byref(sd))
        else:
            code = _advapi32.GetNamedSecurityInfoW(os.fspath(target), _SE_FILE_OBJECT, what,
                                                   ctypes.byref(owner), ctypes.byref(group),
                                                   ctypes.byref(dacl), ctypes.byref(sacl), ctypes.byref(sd))
        if code:
            raise _fail(code, "GetSecurityInfo")
        try:
            text = wintypes.LPWSTR()
            if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    sd, _SDDL_REVISION_1, what, ctypes.byref(text), None):
                raise _fail(ctypes.get_last_error(), "ConvertSecurityDescriptorToStringSecurityDescriptorW")
            try:
                return text.value or ""
            finally:
                _kernel32.LocalFree(text)
        finally:
            _kernel32.LocalFree(sd)

    def _granting_aces(target: Target):
        """(trustee, rights) for every ACE that grants access to the object itself; None for a
        missing or NULL list, which grants everyone everything."""
        text = _sddl(target, _DACL)
        dacl = text.split("D:", 1)[1] if "D:" in text else ""
        if not dacl or dacl.startswith("NO_ACCESS_CONTROL"):
            return None
        aces = []
        for kind, flags, rights, _obj, _inh, trustee in _ACE.findall(dacl):
            if kind not in ("A", "OA", "XA", "ZA") or "IO" in flags:
                continue
            aces.append((_sid(trustee), rights))
        return aces

    def _writes(rights: str) -> bool:
        if rights.lower().startswith("0x"):
            return bool(int(rights, 16) & _WRITE_MASK)
        tokens = [rights[i:i + 2] for i in range(0, len(rights), 2)]
        return any(t not in _READ_RIGHTS for t in tokens)

    def open(path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:  # noqa: A001
        """`os.open` in binary mode, refusing a link when `NOFOLLOW` is asked."""
        if dir_fd is not None:
            raise NotImplementedError("dir_fd: a directory has no descriptor on Windows")
        if flags & DIRECTORY:
            raise NotImplementedError("DIRECTORY: a directory has no descriptor on Windows")
        if flags & NOFOLLOW:
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                    raise OSError(errno.ELOOP, "refusing to follow a link", os.fspath(path))
        return os.open(path, (flags & ~(NOFOLLOW | DIRECTORY | NONBLOCK)) | BINARY, mode)

    def private(target: Target) -> bool:
        """Every ACE that grants access names this account, LocalSystem or Administrators."""
        aces = _granting_aces(target)
        if aces is None:
            return False
        trusted = _trusted()
        return all(trustee in trusted for trustee, _ in aces)

    def others_may_write(target: Target) -> bool:
        aces = _granting_aces(target)
        if aces is None:
            return True
        trusted = _trusted()
        return any(trustee not in trusted and _writes(rights) for trustee, rights in aces)

    def owned_by_me(target: Target) -> bool:
        text = _sddl(target, _OWNER)
        owner = text.split("O:", 1)[1].split("G:", 1)[0].split("D:", 1)[0] if "O:" in text else ""
        return bool(owner) and _sid(owner) in _trusted()

    def make_private(target: Target) -> None:
        """A protected list: this account, LocalSystem, Administrators; inherited inside a folder."""
        folder = stat.S_ISDIR(_stat(target).st_mode)
        inherit = "OICI" if folder else ""
        sddl = "D:P" + "".join(f"(A;{inherit};FA;;;{who})" for who in (user_sid(), "SY", "BA"))
        sd = _PVOID()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, _SDDL_REVISION_1, ctypes.byref(sd), None):
            raise _fail(ctypes.get_last_error(), "ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), _PVOID()
            if not _advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                                       ctypes.byref(defaulted)):
                raise _fail(ctypes.get_last_error(), "GetSecurityDescriptorDacl")
            # By name even for a descriptor: the C runtime opens without WRITE_DAC, so the
            # handle itself may not change its own list.
            name = _handle_path(target) if isinstance(target, int) else os.fspath(target)
            code = _advapi32.SetNamedSecurityInfoW(name, _SE_FILE_OBJECT, _DACL | _PROTECTED_DACL,
                                                   None, None, dacl, None)
            if code:
                raise _fail(code, "SetSecurityInfo")
        finally:
            _kernel32.LocalFree(sd)

    _kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                                    wintypes.DWORD]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def _handle_path(fd: int) -> str:
        buf = ctypes.create_unicode_buffer(32768)
        n = _kernel32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
        if not n:
            raise _fail(ctypes.get_last_error(), "GetFinalPathNameByHandleW")
        return buf.value

    def describe(target: Target) -> str:
        return "owner-only" if private(target) else "shared"

    def explain(target: Target) -> str:
        """The owner and access list as SDDL, and this account's SID — for a diagnostic, never a
        decision. SIDs name accounts on this machine; they are not credentials."""
        return f"{_sddl(target, _OWNER | _DACL)} (this account: {user_sid()})"

    _FOLDERID_PROFILE = uuid.UUID("5E6C858F-0E22-4760-9AFE-EA3317B67173")

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def real_home() -> Path | None:
        """The account's profile folder from the known-folder store; None when it cannot say."""
        fields = _FOLDERID_PROFILE.fields
        guid = _GUID(fields[0], fields[1], fields[2], (ctypes.c_ubyte * 8)(*_FOLDERID_PROFILE.bytes[8:]))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        try:
            if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
                return None
            return Path(out.value) if out.value else None
        finally:
            ole32.CoTaskMemFree(out)
