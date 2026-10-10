"""Who the engine runs as, on every OS (docs/design/WINDOWS-LINUX.md, W2b).

    osprivacy.real_home()      # the account's home as the OS records it, never from HOME/USERPROFILE

POSIX: the password database's home for this uid (`pwd`).

Windows: there is no `pwd` and no uid. The profile folder comes from the shell's known-folder
store (`SHGetKnownFolderPath(FOLDERID_Profile)`), which reads the account's profile and not the
environment — so a process that set USERPROFILE to a sandbox, as a test runner does, is told
apart from the real account, as `HOME` is told apart on POSIX.
"""
from __future__ import annotations

import os
from pathlib import Path

WINDOWS = os.name == "nt"


if not WINDOWS:
    def real_home() -> Path | None:
        """The home the password database records for this uid; None when it has none."""
        import pwd
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            return None

else:  # pragma: no cover - exercised by the Windows CI job
    import ctypes
    import uuid
    from ctypes import wintypes

    _FOLDERID_PROFILE = uuid.UUID("5E6C858F-0E22-4760-9AFE-EA3317B67173")

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def real_home() -> Path | None:
        """The account's profile folder from the known-folder store; None when it cannot say."""
        fields = _FOLDERID_PROFILE.fields
        guid = _GUID(fields[0], fields[1], fields[2],
                     (ctypes.c_ubyte * 8)(*_FOLDERID_PROFILE.bytes[8:]))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        try:
            if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
                return None
            return Path(out.value) if out.value else None
        finally:
            ole32.CoTaskMemFree(out)
