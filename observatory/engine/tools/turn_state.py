#!/usr/bin/env python3
"""Which changes in a checkout are THIS turn's, kept per session and checkout.

**The noise this closes.** `record_turn.py` read `git status --porcelain` and
called everything in it the turn's work. A checkout with standing untracked files
(a pasted log, a `.worktrees/` folder, a transcript export) therefore "changed"
on every turn: measured 2026-10-20 on one machine, the Stop hook asked for a
`why` on every recorded turn of 21 sessions (103 of 103 in one), and one session
of one repository was asked 35 times about the same "4 files changed, +0/-0",
none of which that session had touched.

What this keeps, so the recorder can tell the turn's work from the tree's state:

* a **snapshot** of the working tree: for each `git status` entry its status
  code, size and modification time, and the HEAD commit;
* per session and checkout, the **baseline** (the snapshot when the session
  first saw the checkout) and the **last** snapshot (the previous turn's).

A turn changed something when an entry's fingerprint differs from the last
snapshot, or HEAD moved. It changed TRACKED content when such an entry is, or
was, tracked (any status but `??`), or HEAD moved; only then does the recorder
ask for a `why`. The session's files are the entries that differ from the
baseline, so a file that was already dirty or untracked before the session
started is not counted as the session's work.

**The baseline is taken at SessionStart** (`tools/session_start.py`). A session
that reaches its first Stop without one (it started before this engine was
installed, or SessionStart failed) takes the untracked entries it finds as
already there and counts the tracked ones: an untracked file cannot be told
apart from one the first turn created, and failing toward silence is the point
of this module, while a tracked edit still asks.

**No path is kept.** An entry is keyed by a digest of its path, and the file is
named by a digest of the session id and the checkout: the state names nothing
a reader could not already see in the checkout. Files live under
`$XDG_STATE_HOME/project-observatory/turns/` (owner-only), outside the
workspace store, so the snapshot gate and workspace backups never see them, and
are pruned after `PRUNE_SECONDS`.

Every function here is total: a failure answers "no state", never an exception,
because the caller is a hook that must stay silent. Standard library only.
"""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import time

SCHEMA = 1
#: A session that has not ended a turn for this long is over; its state goes.
PRUNE_SECONDS = 7 * 86400
#: The status read, shared by the recorder and the baseline so both see the same
#: entries. `-z` keeps unusual file names whole; `--ignore-submodules=dirty` keeps
#: a submodule's own uncommitted work out (it is that repository's business; a
#: moved submodule pointer still shows) and is what made the read cheap: measured
#: 2026-10-20 in a checkout with eleven submodules, 0.45-1.9 s became 0.01-0.3 s.
STATUS_ARGS = ("status", "--porcelain=v1", "-z", "--ignore-submodules=dirty")
UNTRACKED = "??"


def state_dir() -> pathlib.Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return pathlib.Path(base) / "project-observatory" / "turns"


def parse_status(raw: str) -> list[tuple[str, str]]:
    """`(XY, path)` for each entry of `git status --porcelain=v1 -z` output. A
    rename or copy carries its source as the next field, which is skipped: the
    entry is the path the file has now."""
    out: list[tuple[str, str]] = []
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4 or f[2] != " ":
            continue
        xy, path = f[:2], f[3:]
        if "R" in xy or "C" in xy:
            i += 1
        out.append((xy, path))
    return out


def key(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:24]


def fingerprint(top: pathlib.Path, xy: str, path: str) -> list:
    """`[XY, size, mtime_ns]`; size and time are None for a path that is gone.
    An untracked directory (`?? dir/`) is fingerprinted by the directory itself,
    whose time moves when an entry is added or removed directly inside it."""
    try:
        st = os.lstat(top / path.rstrip("/"))
        return [xy, st.st_size, st.st_mtime_ns]
    except (OSError, ValueError):
        return [xy, None, None]


def snapshot(top: pathlib.Path, entries: list[tuple[str, str]], head: str) -> dict:
    return {"head": head or None,
            "entries": {key(p): fingerprint(top, xy, p) for xy, p in entries}}


def _file(session: str, top: pathlib.Path) -> pathlib.Path:
    name = hashlib.sha256(f"{session}\0{top}".encode("utf-8", "surrogateescape")).hexdigest()[:32]
    return state_dir() / f"{name}.json"


def load(session: str, top: pathlib.Path) -> dict | None:
    """The session's state for this checkout, or None (none kept, or unreadable)."""
    if not session:
        return None
    try:
        doc = json.loads(_file(session, top).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != SCHEMA:
        return None
    if not isinstance(doc.get("baseline"), dict) or not isinstance(doc.get("last"), dict):
        return None
    if not isinstance(doc["last"].get("entries"), dict):
        return None
    return doc


def save(session: str, top: pathlib.Path, baseline: dict, last: dict) -> bool:
    """Write the state owner-only and atomically; prune what no session will read
    again. False when it could not be kept: the next turn then finds no state
    and takes its untracked entries as already there, never a false change."""
    if not session:
        return False
    try:
        d = state_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        now = time.time()
        for p in d.iterdir():
            try:
                if p.suffix in (".json", ".tmp") and now - p.lstat().st_mtime > PRUNE_SECONDS:
                    p.unlink()
            except OSError:
                pass
        path = _file(session, top)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        mask = os.umask(0o077)
        try:
            tmp.write_text(json.dumps({"v": SCHEMA, "at": int(now), "baseline": baseline,
                                       "last": last}), encoding="utf-8")
        finally:
            os.umask(mask)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception:                                                  # noqa: BLE001
        return False


def fallback(current: dict) -> dict:
    """The baseline for a session seen for the first time at a Stop: its untracked
    entries are taken as already there, its tracked ones are not."""
    return {k: fp for k, fp in current["entries"].items() if fp and fp[0] == UNTRACKED}


def compare(last: dict, current: dict, *, head_moved: bool | None = None) -> dict:
    """`{"changed": n, "tracked": bool}` between two snapshots. `head_moved`
    overrides the HEAD comparison when the last snapshot has none to compare."""
    before, after = last.get("entries") or {}, current.get("entries") or {}
    changed, tracked = 0, False
    for k in set(before) | set(after):
        a, b = before.get(k), after.get(k)
        if a == b:
            continue
        changed += 1
        if (a and a[0] != UNTRACKED) or (b and b[0] != UNTRACKED):
            tracked = True
    moved = head_moved if head_moved is not None else (
        bool(last.get("head")) and last.get("head") != current.get("head"))
    if moved:
        changed += 1
        tracked = True
    return {"changed": changed, "tracked": tracked}


def session_paths(baseline: dict, current: dict, entries: list[tuple[str, str]]) -> list[str]:
    """The entries this session changed: every path whose fingerprint in `current`
    differs from the baseline's (a path absent from the baseline differs)."""
    now = current.get("entries") or {}
    return sorted({p for _xy, p in entries if baseline.get(key(p)) != now.get(key(p))})
