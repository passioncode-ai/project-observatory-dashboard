"""Observed process holds and conservative refusal when coverage is incomplete.

ONE answer for the disk guard (tools/disk_guard.py) and the cleanup's git tier
(tools/cleanup.py). Before 2026-10-12 each had its own: the guard read one `lsof` listing
at the start of a run and reused it for minutes of removals, and the git tier looked only
at processes' working directories, so a worktree whose files an editor or a dev server
held open was "free". An independent review found both (docs/design/DISK-GUARD.md).

A path is HELD when a process of this account
  * has a file under it open or memory-mapped (lsof),
  * works inside it (cwd) or runs from it (its executable), or
  * has a recognized path in rendered command text. Such text supplies positive
    hints only: closed scripts, relative paths, spaces and aliases can be missed.

How it is known, per system:
  lsof     macOS and Linux; run under a UTF-8 locale, and `\\xNN` escapes decoded, because
           without one lsof prints a non-ASCII name escaped and it matched nothing;
  unknown  Linux without lsof: the unbounded /proc fallback is retired;
  unknown  Windows: no supported holder inventory is implemented here. An open handle
           may allow rename/delete through FILE_SHARE_DELETE, so rename success cannot
           prove that a target is unused. Path removal therefore refuses unknown coverage;
  unknown  none of these: every path is held, and nothing is removed.

Production snapshots remain incomplete because rendered argv cannot prove absence.
Explicitly constructed complete snapshots are a legacy test/consumer API, not an
assertion that take() observes every process use.

Values never leave this module: a command line is cut to its absolute-path words, kept in
memory for one decision, never logged, printed or written.
"""
from __future__ import annotations

import bisect
import codecs
import math
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import time

import osproc

# These bound captured payload and parser collections, not whole-process RSS.
MAX_BYTES = 2 * 1024 * 1024
READ_BYTES = 65536
MAX_FIELDS = 100000
MAX_HINTS = 10000
MAX_TOKEN_BYTES = 4096
CLEANUP_SECONDS = .2
_read = os.read

#: `\xNN` as lsof writes a byte outside its locale's printable set.
_ESCAPE = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")


def _decode_escapes(name: str) -> str:
    """lsof's `\\xd1\\x84` back to UTF-8; invalid encoded bytes refuse the listing."""
    def one(m: re.Match) -> str:
        raw = codecs.decode(m.group(0).replace("\\x", ""), "hex")
        return raw.decode("utf-8")
    return _ESCAPE.sub(one, name)


def _deadline(timeout: float, maximum: float = 120) -> float | None:
    try:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            return None
        return time.monotonic() + min(float(timeout), maximum)
    except (TypeError, ValueError, OverflowError):
        return None


def _capture(cmd: list[str], *, deadline: float, env: dict | None = None,
             max_bytes: int = MAX_BYTES) -> dict:
    """Capture only this owned POSIX group within payload and operation budgets.

    Stderr is counted, never retained. Complete requires both pipe EOFs, observed
    direct-child exit and no remaining owned group. Descendants that escape that
    group and uninterruptible process creation/scheduling are outside this bound.
    No holder or service discovered by the query is ever signalled.
    """
    result = {"stdout": b"", "returncode": None, "stderr_seen": False,
              "bytes_seen": 0, "complete": False, "reason": None, "reaped": False}
    try:
        valid = (not isinstance(deadline, bool) and math.isfinite(deadline)
                 and type(max_bytes) is int and 0 < max_bytes <= MAX_BYTES)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid or deadline <= time.monotonic():
        result["reason"] = "invalid-budget"
        return result
    if os.name == "nt":
        result["reason"] = "unsupported-platform"
        return result
    remaining = deadline - time.monotonic()
    reserve = min(CLEANUP_SECONDS, remaining / 4)
    work_end = deadline - reserve
    if work_end <= time.monotonic():
        result["reason"] = "command-timeout"
        return result
    process = None
    output = bytearray()
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, **osproc.new_group())
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                left = work_end - time.monotonic()
                if left <= 0:
                    result["reason"] = "command-timeout"
                    break
                for key, _ in selector.select(min(.05, left)):
                    chunk = _read(key.fileobj.fileno(), READ_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    result["bytes_seen"] += len(chunk)
                    if key.data == "out":
                        output.extend(chunk[:max(0, max_bytes - len(output))])
                    else:
                        result["stderr_seen"] = True
                    if result["bytes_seen"] > max_bytes:
                        result["reason"] = "output-limit"
                        break
                if result["reason"]:
                    break
                # A completed leader does not make a pipe-holding descendant
                # harmless. Stop only the group launched by this capture.
                if process.poll() is not None and osproc.group_alive(process.pid):
                    result["reason"] = "owned-descendants-remain"
                    break
        if result["reason"] is None:
            try:
                process.wait(timeout=max(0, work_end - time.monotonic()))
            except subprocess.TimeoutExpired:
                result["reason"] = "command-timeout"
            if result["reason"] is None and osproc.group_alive(process.pid):
                result["reason"] = "owned-descendants-remain"
    except (OSError, ValueError, subprocess.SubprocessError):
        result["reason"] = "command-unavailable"
    finally:
        if process is not None:
            # No library cleanup routine may add a fresh grace period beyond
            # the caller's absolute deadline. Signals target our new group only.
            try:
                active = process.poll() is None or osproc.group_alive(process.pid)
                if active:
                    osproc.signal_group(process.pid, signal.SIGTERM)
                    grace_end = min(deadline, time.monotonic() + .03)
                    while time.monotonic() < grace_end:
                        process.poll()
                        if not osproc.group_alive(process.pid):
                            break
                        time.sleep(min(.005, max(0, grace_end - time.monotonic())))
                    if osproc.group_alive(process.pid):
                        osproc.signal_group(process.pid, signal.SIGKILL)
                try:
                    result["returncode"] = process.wait(timeout=max(0, deadline - time.monotonic()))
                    result["reaped"] = True
                except subprocess.TimeoutExpired:
                    result["reason"] = "cleanup-incomplete"
            except OSError:
                result["reason"] = "cleanup-incomplete"
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            result["reason"] = "cleanup-incomplete"
        result["stdout"] = bytes(output)
    result["complete"] = result["reason"] is None and result["reaped"]
    return result


def _run(cmd: list[str], timeout: float, env: dict | None = None) -> str | None:
    end = _deadline(timeout)
    if end is None:
        return None
    p = _capture(cmd, deadline=end, env=env if env is not None else _utf8_env())
    # Partial output cannot prove the absence of a holder. Even exit-zero
    # diagnostics can report an inaccessible process or filesystem.
    if not p["complete"] or p["returncode"] != 0 or p["stderr_seen"] or not p["stdout"]:
        return None
    try:
        decoded = p["stdout"].decode("utf-8", errors="strict")
        return decoded if time.monotonic() < end else None
    except UnicodeError:
        return None


class _ParseLimit(ValueError):
    pass


def _lines(text: str):
    # Reject cardinality before allocating split lists. Bytes are already bounded
    # by capture; this validation also protects explicit synthetic callers.
    if (not isinstance(text, str) or len(text) > MAX_BYTES
            or text.count("\n") > MAX_FIELDS or text.count("\r") > MAX_FIELDS):
        raise _ParseLimit()
    try:
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise _ParseLimit()
    except UnicodeError:
        raise _ParseLimit() from None
    for count, field in enumerate(re.finditer(r"[^\r\n]+", text), 1):
        if count > MAX_FIELDS:
            raise _ParseLimit()
        yield field.group()


def _words(text: str):
    count = 0
    for line in _lines(text):
        for position, match in enumerate(re.finditer(r"\S+", line)):
            word = match.group()
            count += 1
            if count > MAX_FIELDS or len(word) > MAX_TOKEN_BYTES or len(word.encode("utf-8")) > MAX_TOKEN_BYTES:
                raise _ParseLimit()
            yield position, word


def _argument_hints(text: str) -> list[str] | None:
    paths = []
    try:
        for _, word in _words(text):
            word = word.split("=", 1)[-1] if word.startswith("-") and "=" in word else word
            if word.startswith("/") and len(word) > 1:
                paths.append(word.rstrip(",;:"))
                if len(paths) > MAX_HINTS:
                    raise _ParseLimit()
    except _ParseLimit:
        return None
    return paths


def _lsof_hints(text: str) -> list[str] | None:
    names = []
    try:
        for line in _lines(text):
            if len(line) > MAX_TOKEN_BYTES or len(line.encode("utf-8")) > MAX_TOKEN_BYTES:
                raise _ParseLimit()
            if line.startswith("n/"):
                names.append(_decode_escapes(line[1:]))
                if len(names) > MAX_HINTS:
                    raise _ParseLimit()
    except (UnicodeError, ValueError):
        return None
    return names or None


def _utf8_env() -> dict:
    env = dict(os.environ)
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        env.pop(key, None)
    env["LC_ALL"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def lsof_paths(timeout: float = 120) -> list[str] | None:
    end = _deadline(timeout)
    if end is None:
        return None
    if not shutil.which("lsof"):
        return None
    out = _run(["lsof", "-n", "-P", "-Fn"], max(0, end - time.monotonic()), _utf8_env())
    if out is None:
        return None
    # A successful global process inventory should name at least one absolute
    # path. Unexpected text (or only headers) is not proof of an empty machine.
    names = _lsof_hints(out)
    return names if time.monotonic() < end else None


def proc_paths() -> list[str] | None:
    """Legacy entry point: unbounded synchronous process enumeration is retired."""
    return None


def argument_paths(timeout: float = 20) -> list[str] | None:
    """Positive lexical path hints from rendered POSIX `ps`, never complete argv.

    Argument boundaries, relative launch paths and symlink identity are not preserved.
    None means unreadable; even a returned list cannot prove an unmatched target free.
    """
    if os.name == "nt":
        return []
    end = _deadline(timeout)
    if end is None:
        return None
    out = _run(["ps", "-axww", "-o", "args="], max(0, end - time.monotonic()))
    if out is None:
        return None
    names = _argument_hints(out)
    return names if time.monotonic() < end else None


class Holders:
    """The paths held right now. `known` False means "cannot tell": every path is held."""

    def __init__(self, names: list[str] | None, mode: str = "lsof", *, complete: bool = True):
        # Explicit constructed snapshots retain the legacy API (used by synthetic
        # consumers); production take() must supply its actual coverage condition.
        self.known = names is not None and mode != "rename" and complete is True
        self.mode = mode if self.known else "unknown"
        self.observation = "unavailable" if names is None else "provided"
        self.names = sorted({os.path.normpath(n) for n in names or [] if n.startswith("/")})

    @classmethod
    def take(cls, timeout: float = 120, *, collect_hints: bool = True) -> "Holders":
        # Admission cannot establish absence with today's collectors. Callers that
        # only need refusal avoid a full process inventory and its output buffers.
        # Explicit hint readers retain the existing incomplete observation path.
        if collect_hints is False:
            result = cls(None)
            result.observation = "not-attempted"
            return result
        if os.name == "nt":
            return cls(None)
        end = _deadline(timeout)
        if end is None:
            return cls(None)
        files = lsof_paths(max(0, end - time.monotonic()))
        mode = "lsof"
        # No unbounded synchronous /proc fallback in production observations.
        if files is None or time.monotonic() >= end:
            return cls(None)
        args = argument_paths(min(20.0, max(0, end - time.monotonic())))
        if args is None or len(files) + len(args) > MAX_HINTS or time.monotonic() >= end:
            # Without the command lines, a program running from a cache folder is invisible;
            # refuse to know rather than guess.
            return cls(None)
        # ps is a display, not a lossless argv protocol. Preserve positive hints,
        # but neither lsof nor /proc file lists cover a script closed after loading.
        result = cls(files + args, mode, complete=False)
        result.observation = "partial"
        return result

    def busy(self, path: str | os.PathLike) -> bool:
        if not self.known:
            return True
        if self.mode == "rename":
            return True                      # legacy rename state is not holder evidence
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

    Rendered command words can provide positive hints for interpreters and tools.
    An unmatched nonempty request remains unknown because argv boundaries are lost.
    """
    if not names:
        return False
    if os.name == "nt":
        return None
    if len(names) > MAX_HINTS or any(not isinstance(n, str) or len(n) > MAX_TOKEN_BYTES for n in names):
        return None
    end = _deadline(timeout)
    if end is None:
        return None
    out = _run(["ps", "-axww", "-o", "args="], max(0, end - time.monotonic()))
    if out is None:
        return None
    wanted = set(names)
    matched = False
    try:
        for position, word in _words(out):
            if position < 3 and os.path.basename(word) in wanted:
                matched = True
    except _ParseLimit:
        return None
    return True if matched and time.monotonic() < end else None


def fresh_busy(target: str, timeout: float = 5.0) -> bool | None:
    """Fresh, target-scoped holder check. Unknown is never a free target.

    POSIX uses a bounded lsof query for this subtree plus fresh command paths. This
    avoids another full-machine open-file inventory per action. Missing lsof, warnings,
    partial output or timeout preserve the target; there is no unbounded /proc fallback.
    Rendered ps supplies only positive hints: an empty lsof result and unmatched
    command display remain unknown, including closed scripts. Windows is unknown
    until a supported inventory exists. This is not a lifecycle or filesystem lock.
    """
    if os.name == "nt":
        return None
    end = _deadline(timeout, 5.0)
    tool = shutil.which("lsof")
    if not tool or end is None:
        return None
    selector = ["+D", target] if os.path.isdir(target) else ["--", target]
    p = _capture([tool, "-n", "-P", "-Fn", *selector], deadline=end, env=_utf8_env())
    if not p["complete"] or p["stderr_seen"] or time.monotonic() >= end:
        return None
    if p["returncode"] == 0 and p["stdout"]:
        try:
            hints = _lsof_hints(p["stdout"].decode("utf-8", errors="strict"))
        except UnicodeError:
            return None
        return True if hints and time.monotonic() < end else None
    if p["returncode"] != 1 or p["stdout"]:
        return None
    out = _run(["ps", "-axww", "-o", "args="], max(0, end - time.monotonic()))
    paths_held = _argument_hints(out) if out is not None else None
    if paths_held is None or time.monotonic() >= end:
        return None
    return True if Holders(paths_held).busy(target) else None
