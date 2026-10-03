"""Has the code this process was started from been replaced on disk? (lifecycle LC-10)

WHY. Every Claude Code session starts its own `mcp/server.py` and keeps it for the
whole session. An update replaces the files under it, and the server goes on
answering from the previous release held in memory: on 2026-10-03, eight of ten
servers had started before that morning's update and still ran the old code, with
the contract drift (and the ImportError of a lazy import) that implies. The updater
cannot restart them; the session owns them.

So the server checks for itself. At start it remembers the identity of its own
version file (device, inode, mtime) and the version it imported. On each call a
`stat` of that file tells whether anything was installed over it; only then is the
file read to name the version now on disk. Any replacement counts — a reinstall of
the same version replaces code too — and a missing file means the code is gone.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_VERSION = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)


def _identity(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns)


def installed_version(path: Path) -> str | None:
    try:
        m = _VERSION.search(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


class Freshness:
    """The started-from identity of one version file, and the check against it."""

    def __init__(self, marker: Path, version: str) -> None:
        self.marker = Path(marker)
        self.version = version
        self.identity = _identity(self.marker)

    def check(self) -> dict | None:
        """None while the code on disk is the code this process started from;
        else {"running", "installed", "reason"} — `installed` None when it is gone."""
        now = _identity(self.marker)
        if now is not None and now == self.identity:
            return None
        installed = installed_version(self.marker) if now is not None else None
        if installed is None:
            reason = "the code this server was started from is no longer installed"
        elif installed != self.version:
            reason = f"release {installed} was installed after this server started on {self.version}"
        else:
            reason = f"release {installed} was reinstalled after this server started"
        return {"running": self.version, "installed": installed, "reason": reason}
