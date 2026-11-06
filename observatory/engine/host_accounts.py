"""How many people's OS accounts this host has: one, several, or unknown.

Loopback is one per machine. On a host with one person's account every loopback peer is that
account's (or the system's), and the engine's servers serve as they always have; on a host with
several, another account's process reaches every port this account listens on, and the servers
refuse it before reading its request (`peer_check.py`, docs/design/ACCESS.md rule 10). This
module answers the one question that switches that check on.

    count() -> int | None     people's accounts on this host; None when it cannot be told
    detail() -> dict          the same with the reason: {"count", "reason"}

A count must never be too low: one too few is the check switched off on a host that needs it.
So it is the union of two readings, and a host bound to a directory is "cannot be told":

- **The local account database.** macOS: `dscl . -list /Users UniqueID` and `UserShell`, every
  account whose name does not start with `_`, other than `root`, `daemon` and `nobody` (the
  system's own accounts all do), whose shell lets a person log in (not `false` or `nologin`), so a
  hidden administrator below 501 and the Guest account (201, listed only while it is enabled)
  count, and a service account without a login shell does not. Linux: the password database, uids from UID_MIN to UID_MAX of
  `/etc/login.defs` (1000 to 60000 when it does not say), whose shell lets a person log in.
  Windows: the profile list in the registry, SIDs `S-1-5-21-*` with a relative id from 1000 whose
  profile folder exists.
- **Who runs processes now.** The distinct uids of running processes that are people's (macOS:
  501 and above, or an account of the first reading; Linux: UID_MIN to UID_MAX): a directory
  account that has logged in is counted even when the database above does not list it.
- **A directory binding** (macOS: a search node other than `/Local/` and `/BSD/` in
  `dscl /Search -read / CSPSearchPath`; Linux: `sss`, `ldap`, `winbind` or `nis` for `passwd` in
  `/etc/nsswitch.conf`) means any directory user may log in: the answer is None, `directory-bound`.

None (unreadable, or directory-bound) is read by every rule as "more than one": an unknown count
switches the check on, never off. The answer is cached for TTL seconds in the process, so an
account added later is seen within ten minutes without a restart, and a one-account host pays a
few short subprocesses per ten minutes at most, never one per request. It returns a count and a
reason, never a name.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

TTL = 600.0
TIMEOUT_SECONDS = 2.0
_NO_LOGIN = ("nologin", "false")
_MAC_SYSTEM_NAMES = frozenset({"root", "daemon", "nobody"})
_DIRECTORY_SOURCES = ("sss", "ldap", "winbind", "nis", "nisplus")

_LOCK = threading.Lock()
#: (time.monotonic() of the read, {"count", "reason"})
_CACHE: list = [None, None]


def parse_dscl(text: str, shells: str = "") -> set[int]:
    """`dscl . -list /Users UniqueID` (a name and a uid per line) and, optionally, `dscl . -list
    /Users UserShell`. People's accounts: every name not starting with `_` except root, daemon and
    nobody, with a positive uid. The shell (when listed) excludes only hidden service accounts: a uid
    below 501 whose shell is `false` or `nologin`. From 501 up every account counts whatever its shell,
    because a desktop login does not use the login shell."""
    shell = {}
    for line in shells.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            shell[parts[0]] = parts[-1]
    uids = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[-1].lstrip("-").isdigit():
            continue
        name, uid = parts[0], int(parts[-1])
        if uid <= 0 or name.startswith("_") or name in _MAC_SYSTEM_NAMES:
            continue
        if uid < 501 and any(shell.get(name, "").endswith(x) for x in _NO_LOGIN):
            continue
        uids.add(uid)
    return uids


def parse_search_path(text: str) -> bool:
    """`dscl /Search -read / CSPSearchPath`: True when a node other than the local ones is searched."""
    nodes = [w for w in text.split() if w.startswith("/")]
    return any(not (n.startswith("/Local/") or n.startswith("/BSD/")) for n in nodes)


def parse_nsswitch(text: str) -> bool:
    """`/etc/nsswitch.conf`: True when `passwd` is also looked up in a directory."""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("passwd:"):
            sources = line.split(":", 1)[1].replace("[", " ").split()
            return any(s in _DIRECTORY_SOURCES for s in sources)
    return False


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


def passwd_uids(entries, lo: int, hi: int) -> set[int]:
    """Password entries (objects with pw_uid and pw_shell) of people who can log in."""
    return {e.pw_uid for e in entries
            if lo <= e.pw_uid <= hi and not any(e.pw_shell.rstrip().endswith(x) for x in _NO_LOGIN)}


def parse_ps_uids(text: str) -> set[int]:
    """`ps -A -o uid=`: one uid per line."""
    return {int(w) for w in text.split() if w.isdigit()}


def people(process_uids: set[int], accounts: set[int], lo: int, hi: int) -> set[int]:
    """The process uids that are people's: in [lo, hi], or already an account of the database."""
    return {u for u in process_uids if lo <= u <= hi or u in accounts}


def count_profiles(profiles) -> int:
    """Windows: (SID, profile folder exists) pairs. People's accounts are S-1-5-21-...-RID with a
    RID from 1000; a profile whose folder is gone is a leftover, not an account."""
    n = 0
    for sid, exists in profiles:
        parts = sid.split("-")
        if sid.startswith("S-1-5-21-") and parts[-1].isdigit() and int(parts[-1]) >= 1000 and exists:
            n += 1
    return n


def _run(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _linux_process_uids() -> set[int]:
    uids = set()
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                uids.add(os.stat(f"/proc/{name}").st_uid)
            except OSError:
                continue
    return uids


def _read() -> dict:
    try:
        if sys.platform == "darwin":
            search = _run(["/usr/bin/dscl", "/Search", "-read", "/", "CSPSearchPath"])
            if search is None:
                return {"count": None, "reason": "unreadable"}
            if parse_search_path(search):
                return {"count": None, "reason": "directory-bound"}
            listed = _run(["/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"])
            if listed is None:
                return {"count": None, "reason": "unreadable"}
            accounts = parse_dscl(listed, _run(["/usr/bin/dscl", ".", "-list", "/Users", "UserShell"]) or "")
            ps = _run(["/bin/ps", "-A", "-o", "uid="])
            running = people(parse_ps_uids(ps), accounts, 501, 2 ** 31 - 1) if ps is not None else set()
            return {"count": len(accounts | running), "reason": "local"}
        if sys.platform.startswith("linux"):
            import pwd
            try:
                with open("/etc/nsswitch.conf", encoding="utf-8", errors="replace") as fh:
                    if parse_nsswitch(fh.read(256 * 1024)):
                        return {"count": None, "reason": "directory-bound"}
            except FileNotFoundError:
                pass
            try:
                with open("/etc/login.defs", encoding="utf-8", errors="replace") as fh:
                    lo, hi = parse_login_defs(fh.read(256 * 1024))
            except OSError:
                lo, hi = 1000, 60000
            accounts = passwd_uids(pwd.getpwall(), lo, hi)
            return {"count": len(accounts | people(_linux_process_uids(), accounts, lo, hi)), "reason": "local"}
        if sys.platform == "win32":  # pragma: no cover - Windows only
            import winreg
            root = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
            profiles, i = [], 0
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as key:
                while True:
                    try:
                        sid = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, sid) as sub:
                            path = winreg.QueryValueEx(sub, "ProfileImagePath")[0]
                        exists = os.path.isdir(os.path.expandvars(path))
                    except OSError:
                        exists = False
                    profiles.append((sid, exists))
            return {"count": count_profiles(profiles), "reason": "local"}
    except (OSError, ValueError) as exc:
        return {"count": None, "reason": f"unreadable: {type(exc).__name__}"}
    return {"count": None, "reason": "unsupported-platform"}


def detail() -> dict:
    """{"count": people's accounts or None, "reason": local, directory-bound, unreadable...},
    read at most once per TTL seconds."""
    now = time.monotonic()
    with _LOCK:
        if _CACHE[0] is not None and now - _CACHE[0] < TTL:
            return dict(_CACHE[1])
        value = _read()
        _CACHE[0], _CACHE[1] = now, value
        return dict(value)


def count() -> int | None:
    """People's accounts on this host; None when it cannot be told (read it as several)."""
    return detail()["count"]
