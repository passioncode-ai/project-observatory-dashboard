"""How many people's OS accounts this host has: one, several, or unknown.

Loopback is one per machine. On a host with one person's account every loopback peer is that
account's (or the system's), and the engine's servers serve as they always have; on a host with
several, another account's process reaches every port this account listens on, and the servers
refuse it before reading its request (`peer_check.py`, docs/design/ACCESS.md rule 10). This
module answers the one question that switches that check on.

    count() -> int | None     people's accounts on this host; None when it cannot be read

- macOS: `dscl . -list /Users UniqueID`, uids from 501 (hidden accounts included; the system's
  own accounts sit below 500, and `nobody` is -2).
- Linux: the password database, uids from UID_MIN to UID_MAX of `/etc/login.defs` (1000 to
  60000 when it does not say), whose shell lets a person log in (not `nologin` or `false`).
- Windows: the profile list in the registry, SIDs `S-1-5-21-*` with a relative id from 1000.

It returns a count, never a name. `None` is read by every rule as "more than one": an
unreadable count switches the check on, never off. The answer is cached for TTL seconds in the
process, so an account added later is seen within ten minutes without a restart, and a
one-account host pays one `dscl` per ten minutes at most, not one per request.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

TTL = 600.0
TIMEOUT_SECONDS = 2.0
_NO_LOGIN = ("nologin", "false")

_LOCK = threading.Lock()
#: (time.monotonic() of the read, the count)
_CACHE: list = [None, None]


def parse_dscl(text: str) -> int:
    """`dscl . -list /Users UniqueID`: a name and a uid per line; people's accounts from 501."""
    n = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("-").isdigit() and int(parts[-1]) >= 501:
            n += 1
    return n


def parse_login_defs(text: str) -> tuple[int, int]:
    """UID_MIN and UID_MAX from `/etc/login.defs`, 1000 and 60000 when absent."""
    lo, hi = 1000, 60000
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            if parts[0] == "UID_MIN":
                lo = int(parts[1])
            elif parts[0] == "UID_MAX":
                hi = int(parts[1])
    return lo, hi


def count_passwd(entries, lo: int, hi: int) -> int:
    """Password entries (objects with pw_uid and pw_shell) of people who can log in."""
    return sum(1 for e in entries
               if lo <= e.pw_uid <= hi and not any(e.pw_shell.rstrip().endswith(x) for x in _NO_LOGIN))


def count_profiles(sids) -> int:
    """Windows profile SIDs: local and domain people's accounts are S-1-5-21-...-RID, RID from 1000."""
    n = 0
    for sid in sids:
        parts = sid.split("-")
        if sid.startswith("S-1-5-21-") and parts[-1].isdigit() and int(parts[-1]) >= 1000:
            n += 1
    return n


def _read() -> int | None:
    try:
        if sys.platform == "darwin":
            done = subprocess.run(["/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"], capture_output=True,
                                  text=True, timeout=TIMEOUT_SECONDS, check=False, stdin=subprocess.DEVNULL)
            return parse_dscl(done.stdout) if done.returncode == 0 else None
        if sys.platform.startswith("linux"):
            import pwd
            try:
                with open("/etc/login.defs", encoding="utf-8", errors="replace") as fh:
                    lo, hi = parse_login_defs(fh.read(256 * 1024))
            except OSError:
                lo, hi = 1000, 60000
            return count_passwd(pwd.getpwall(), lo, hi)
        if sys.platform == "win32":  # pragma: no cover - Windows only
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList")
            sids, i = [], 0
            with key:
                while True:
                    try:
                        sids.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
            return count_profiles(sids)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def count() -> int | None:
    """People's accounts on this host, read at most once per TTL seconds; None when unreadable."""
    now = time.monotonic()
    with _LOCK:
        if _CACHE[0] is not None and now - _CACHE[0] < TTL:
            return _CACHE[1]
        value = _read()
        _CACHE[0], _CACHE[1] = now, value
        return value
