"""What the processes of this account hold, so nothing they use is removed — on every OS.

ONE answer for the disk guard (tools/disk_guard.py) and the cleanup's git tier
(tools/cleanup.py). Before 2026-10-12 each had its own: the guard read one `lsof` listing
at the start of a run and reused it for minutes of removals, and the git tier looked only
at processes' working directories, so a worktree whose files an editor or a dev server
held open was "free". An independent review found both (docs/design/DISK-GUARD.md).

A path is HELD when a process of this account
  * has a file under it open or memory-mapped (lsof; /proc/<pid>/fd and maps),
  * works inside it (cwd) or runs from it (its executable), or
  * names it in its command line — a program an `npx` or a virtualenv runs closes its
    script files once loaded, so only its arguments still say where it runs from.

How it is known, per system:
  lsof     macOS and Linux; run under a UTF-8 locale, and `\\xNN` escapes decoded, because
           without one lsof prints a non-ASCII name escaped and it matched nothing;
  proc     Linux without lsof: /proc/<pid>/{fd,cwd,root,exe,maps};
  rename   Windows: nothing lists open files without a kernel driver, but Windows refuses
           to rename a folder while a file in it is open, so the removal renames the folder
           aside first and a refusal is the "held" answer — nothing of it deleted;
  unknown  none of these: every path is held, and nothing is removed.

Values never leave this module: a command line is cut to its absolute-path words, kept in
memory for one decision, never logged, printed or written.
"""
from __future__ import annotations

import bisect
import codecs
import os
import re
import shutil
import subprocess
import sys

#: `\xNN` as lsof writes a byte outside its locale's printable set.
_ESCAPE = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")


def _decode_escapes(name: str) -> str:
    """lsof's `\\xd1\\x84` back to the UTF-8 text it stands for; anything else unchanged."""
    def one(m: re.Match) -> str:
        raw = codecs.decode(m.group(0).replace("\\x", ""), "hex")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return m.group(0)
    return _ESCAPE.sub(one, name)


def _run(cmd: list[str], timeout: float, env: dict | None = None) -> str | None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(1.0, timeout),
                           errors="replace", env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 when one process could not be read and still lists the rest.
    return p.stdout if p.returncode in (0, 1) and p.stdout else None


def _utf8_env() -> dict:
    env = dict(os.environ)
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        env.pop(key, None)
    env["LC_ALL"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def lsof_paths(timeout: float = 120) -> list[str] | None:
    if not shutil.which("lsof"):
        return None
    out = _run(["lsof", "-n", "-P", "-w", "-Fn"], timeout, _utf8_env())
    if out is None:
        return None
    return [_decode_escapes(line[1:]) for line in out.splitlines() if line.startswith("n/")]


def proc_paths() -> list[str] | None:
    """Linux without lsof: this account's processes' files, maps, cwd, root and executable."""
    if not os.path.isdir("/proc/self/fd"):
        return None
    found: list[str] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = f"/proc/{pid}"
        for link in ("cwd", "root", "exe"):
            try:
                found.append(os.readlink(f"{base}/{link}"))
            except OSError:
                pass
        try:
            for fd in os.listdir(f"{base}/fd"):
                try:
                    found.append(os.readlink(f"{base}/fd/{fd}"))
                except OSError:
                    pass
        except OSError:
            continue
        try:
            with open(f"{base}/maps", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split(None, 5)
                    if len(parts) == 6 and parts[5].startswith("/"):
                        found.append(parts[5].strip())
        except OSError:
            pass
    return [p.removesuffix(" (deleted)") for p in found if p.startswith("/") and p != "/"]


def argument_paths(timeout: float = 20) -> list[str] | None:
    """The absolute-path words of every process's command line (POSIX `ps`).

    None when `ps` cannot be read — the caller then knows less, and says so."""
    if os.name == "nt":
        return []
    out = _run(["ps", "-axww", "-o", "args="], timeout)
    if out is None:
        return None
    paths: list[str] = []
    for line in out.splitlines():
        for word in line.split():
            word = word.split("=", 1)[-1] if word.startswith("-") and "=" in word else word
            if word.startswith("/") and len(word) > 1:
                paths.append(word.rstrip(",;:"))
    return paths


class Holders:
    """The paths held right now. `known` False means "cannot tell": every path is held."""

    def __init__(self, names: list[str] | None, mode: str = "lsof"):
        self.known = names is not None
        self.mode = mode if self.known else "unknown"
        self.names = sorted({os.path.normpath(n) for n in names or [] if n.startswith("/")})

    @classmethod
    def take(cls, timeout: float = 120) -> "Holders":
        if os.name == "nt":
            return cls([], "rename")
        files = lsof_paths(timeout)
        mode = "lsof"
        if files is None:
            files, mode = proc_paths(), "proc"
        if files is None:
            return cls(None)
        args = argument_paths(min(20.0, timeout))
        if args is None:
            # Without the command lines, a program running from a cache folder is invisible;
            # refuse to know rather than guess.
            return cls(None)
        return cls(files + args, mode)

    def busy(self, path: str | os.PathLike) -> bool:
        if not self.known:
            return True
        if self.mode == "rename":
            return False                     # the removal's rename is the check
        real = os.path.normpath(os.path.realpath(str(path)))
        i = bisect.bisect_left(self.names, real)
        if i < len(self.names) and self.names[i] == real:
            return True
        j = bisect.bisect_left(self.names, real + "/")
        if j < len(self.names) and self.names[j].startswith(real + "/"):
            return True
        # An argument naming a file INSIDE `path` is caught above; one naming `path`'s
        # parent is not a hold on `path`.
        return False


def running(names: list[str], timeout: float = 20) -> bool | None:
    """Whether a process of one of these tools runs now: True, False, or None (cannot tell).

    By the basenames of the first words of every command line, so `brew` (bash, then ruby)
    and `pnpm` (node) are seen as what they are, not as their interpreter."""
    if not names:
        return False
    if os.name == "nt":
        return None
    out = _run(["ps", "-axww", "-o", "args="], timeout)
    if out is None:
        return None
    wanted = set(names)
    for line in out.splitlines():
        for word in line.split()[:3]:
            if os.path.basename(word) in wanted:
                return True
    return False
