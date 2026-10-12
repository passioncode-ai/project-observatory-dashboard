#!/usr/bin/env python3
"""The disk guard: keep free space without breaking anybody's work, on every system.

WHY. On 2026-10-11 the operator's disk fell to 0.3 GB free and the machine shut down while
the self-cleaning starved in a tick too long to reach it. The guard runs from the
maintenance job instead (`maintain run`: launchd, a systemd user timer or Task Scheduler,
every 15 minutes), FIRST in its pass, before the update and the backup that need space; a
person runs it at once with `project-observatory full disk-guard [--apply]`. It acts hourly
while space is normal and on every pass below the `low` line.

The rules — and the review that set them — are docs/design/DISK-GUARD.md. In short:

  * NOTHING A PROCESS HOLDS: open, mapped, executed from, worked inside, or named on its
    command line (holders.py). The listing is taken again for every class, and every
    removal re-checks its target right before it happens: still unheld, still old.
  * ONLY WHAT LOSES NOTHING: caches, build output git ignores, temporary entries, old
    Trash, abandoned browser clones, the tools' own cache commands; then the cleanup's git
    tier (merged or pushed branches, clean worktrees whose branch stays).
  * NO PRIVACY-GUARDED PLACE is opened (`scan_machine.protected_place`), no symlink
    followed, no tree removed that holds another account's file, no process stopped, no
    daemon started.
  * PRESSURE moves every threshold: `normal`, `low` (< 25 GB free), `critical` (< 10 GB).
  * IT FINISHES INSIDE ITS BUDGET and always writes what it did — ran, not due, busy — with
    the free-space delta the disk reports (`du` counts an APFS clone in full).

Its rows go to store/logs/disk-guard.jsonl (one per action, one summary per run); the git
tier keeps its undo record in store/logs/cleanup.jsonl. The report is store/raw/disk-guard.json.

    disk_guard.py                plan only: what would go at the current pressure
    disk_guard.py --apply        act now (a person, or an agent on their word)
    disk_guard.py --auto         act when features.auto_cleanup is on (the maintenance job)
    disk_guard.py --level L      plan/act as if pressure were L (normal|low|critical)
    disk_guard.py --json         print the run's summary as one JSON line (the maintenance job)
    disk_guard.py --budget S     stop by itself after S seconds (default `run_budget_seconds`)
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
import configuration  # noqa: E402
import osprivacy  # noqa: E402
import paths  # noqa: E402
import safe_git  # noqa: E402
from holders import Holders, running  # noqa: E402,F401  (re-exported: tests patch `disk_guard.running`)

LEVELS = ("normal", "low", "critical")
GB = 1024 ** 3
#: Where a cache tool lives when the job's PATH (launchd's minimal one) does not say.
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin", "~/go/bin")
#: An entry tree larger than this is not walked to its end; it is treated as not removable.
WALK_LIMIT = 50_000
#: The class order of a run. Paths first, the tools' commands last (a lock wait in one
#: command once spent a whole run with nothing removed).
CLASSES = ("cache-dir", "build-output", "temp", "trash", "browser-clone", "cache-command")
#: Set by the guard for the cleanup it starts, which then does not take the lock again.
LOCK_HELD_ENV = "OBSERVATORY_CLEANUP_LOCK_HELD"   # = cleanup_lock.HELD_ENV


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value) -> float | None:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# --- configuration -------------------------------------------------------------

def _read(f) -> dict:
    try:
        doc = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _previous_defaults() -> list[dict]:
    """The guard sections earlier releases shipped (defaults/cleanup-history.json): a
    workspace value equal to one of them is a copy taken at init, not a person's choice."""
    doc = _read(ROOT / "defaults" / "cleanup-history.json")
    return [v for v in (doc.get("guard") or {}).values() if isinstance(v, dict)]


def load_config() -> dict:
    """The guard section: the shipped defaults, with the keys a PERSON set on top.

    `full init` copies defaults/cleanup.json into the workspace, so a workspace made on
    0.23.0 held that release's values as its own and never saw a later default
    (measured in review, 2026-10-12). A workspace value equal to what any release shipped
    for that key is read as unset; anything else is a choice and wins."""
    shipped = _read(ROOT / "defaults" / "cleanup.json").get("guard") or {}
    own = _read(paths.config_file("cleanup.json")).get("guard") or {}
    previous = _previous_defaults()
    merged = dict(shipped)
    for key, value in own.items():
        if key.startswith("_") or value == shipped.get(key) or any(value == p.get(key) for p in previous):
            continue
        if isinstance(value, dict) and isinstance(shipped.get(key), dict):
            merged[key] = {**shipped[key], **value}
        else:
            merged[key] = value
    return merged


def artifact_names() -> list[str]:
    own = _read(paths.config_file("cleanup.json")).get("artifact_dirs")
    return list(own or _read(ROOT / "defaults" / "cleanup.json").get("artifact_dirs") or [])


def _number(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- the disk ------------------------------------------------------------------

def free_bytes(path: str | os.PathLike | None = None) -> int:
    return shutil.disk_usage(str(path or pathlib.Path.home())).free


def level_for(free: int, cfg: dict) -> str:
    """normal | low | critical, from free bytes and the configured lines (GB)."""
    levels = cfg.get("levels") or {}
    if free < _number((levels.get("critical") or {}).get("free_gb"), 10) * GB:
        return "critical"
    if free < _number((levels.get("low") or {}).get("free_gb"), 25) * GB:
        return "low"
    return "normal"


def by_level(cfg: dict, key: str, level: str, default: float) -> float:
    return _number((cfg.get(key) or {}).get(level, default), default)


# --- time ------------------------------------------------------------------------

class Budget:
    """Seconds a phase may spend. A class that runs out stops between items and says so in
    `degraded`; what it did not reach is first next pass (oldest first)."""

    def __init__(self, seconds: float, degraded: list):
        self.end, self.degraded, self.hit = time.monotonic() + max(0.0, seconds), degraded, set()

    def left(self) -> float:
        return self.end - time.monotonic()

    def out(self, what: str) -> bool:
        if time.monotonic() < self.end:
            return False
        if what not in self.hit:
            self.hit.add(what)
            self.degraded.append({"source": what, "reason": "stopped at its time budget; "
                                  "the rest is looked at next pass, oldest first"})
        return True


# --- walking ---------------------------------------------------------------------

def inspect(root: str, cutoff: float | None = None, deadline: float | None = None,
            limit: int = WALK_LIMIT) -> dict:
    """What a path is, without following a link: {newest, size, complete, reason}.

    `complete` False means "do not touch", and `reason` says why: an entry newer than
    `cutoff` (somebody used it), a socket (a live endpoint), an entry another account owns,
    more entries than `limit`, or the `deadline` reached. The walk stops at the first of
    them, so a large tree in use costs one entry, not two hundred thousand."""
    out = {"newest": 0.0, "size": 0, "complete": False, "reason": ""}
    try:
        st = os.lstat(root)
    except OSError:
        out["reason"] = "gone"
        return out

    def refuse(s: os.stat_result) -> str:
        if stat.S_ISSOCK(s.st_mode):
            return "a socket inside"
        if not osprivacy.owns(s):
            return "another account owns an entry"
        if cutoff is not None and s.st_mtime >= cutoff:
            return "used since the cutoff"
        return ""

    why = refuse(st)
    out["newest"] = st.st_mtime
    if why:
        out["reason"] = why
        return out
    if not stat.S_ISDIR(st.st_mode):
        out.update(size=st.st_size if stat.S_ISREG(st.st_mode) else 0, complete=True)
        return out
    seen, stack = 0, [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            out["reason"] = "unreadable inside"
            return out
        for e in entries:
            seen += 1
            if seen > limit:
                out["reason"] = f"more than {limit} entries"
                return out
            if deadline is not None and seen % 2000 == 0 and time.monotonic() > deadline:
                out["reason"] = "the time ran out"
                return out
            try:
                s = e.stat(follow_symlinks=False)
            except OSError:
                continue
            why = refuse(s)
            if why:
                out["reason"] = why
                return out
            out["newest"] = max(out["newest"], s.st_mtime)
            if stat.S_ISDIR(s.st_mode):
                stack.append(e.path)
            elif stat.S_ISREG(s.st_mode):
                out["size"] += s.st_size
    out["complete"] = True
    return out


def newest_and_size(root: str, limit: int = WALK_LIMIT) -> tuple[float, int, bool]:
    """(newest mtime, bytes, complete) — the shape a caller without a cutoff wants."""
    r = inspect(root, limit=limit)
    return r["newest"], r["size"], r["complete"]


def remove(path: str, probe: bool = False) -> None:
    """Remove a path. With `probe` (Windows), rename it aside first: Windows refuses to rename
    a folder while a file in it is open, so a refusal is "held" and nothing has been deleted.
    A removal that fails after the rename puts the folder back under its name."""
    p = pathlib.Path(path)
    if probe:
        aside = p.with_name(p.name + ".observatory-removing")
        os.rename(p, aside)            # PermissionError: something holds it — the caller skips
        try:
            _remove_now(aside)
        except OSError:
            with contextlib.suppress(OSError):
                os.rename(aside, p)
            raise
        return
    _remove_now(p)


def _remove_now(p: pathlib.Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    else:
        shutil.rmtree(p)


def owned(path: str) -> bool:
    """This account's own file (the caller has already refused a symlink)."""
    try:
        return osprivacy.owned_by_me(path)
    except OSError:
        return False


def expand(raw: str) -> str:
    uid = osprivacy.account()
    if "{localappdata}" in raw:
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            return ""                 # Windows only; elsewhere the root does not exist
        raw = raw.replace("{localappdata}", local)
    if "{user}" in raw:
        # pytest names its base by login (`pytest-of-<user>`), not by uid.
        import getpass
        try:
            raw = raw.replace("{user}", getpass.getuser())
        except (KeyError, OSError):
            return ""
    return os.path.expanduser(raw.replace("{tmpdir}", tempfile.gettempdir().rstrip("/"))
                              .replace("{uid}", uid))


def _scan(d: str) -> list:
    try:
        return list(os.scandir(d))
    except OSError:
        return []


def guarded(path: str, degraded: list | None = None) -> bool:
    """A privacy-guarded place (Documents, Desktop, other apps' containers…): the scheduled
    job must not even stat it (lifecycle LC-06). Named in `degraded` once per place."""
    from scan_machine import protected_place
    if protected_place(pathlib.Path(path)):
        if degraded is not None and not any(d.get("path") == path for d in degraded):
            degraded.append({"source": "privacy", "path": path,
                             "reason": "a privacy-guarded place; the guard does not open it"})
        return True
    return False


# --- the classes -------------------------------------------------------------------

def plan_temp(cfg: dict, level: str, holders: Holders, at: float, budget: Budget | None = None,
              degraded: list | None = None) -> list[dict]:
    """Entries of the temporary roots nobody touched for `temp_days`, held by nobody."""
    days = by_level(cfg, "temp_days", level, 3)
    cutoff = at - days * 86400
    keep = cfg.get("temp_keep") or []
    every = [r for r in cfg.get("temp_roots") or [] if isinstance(r, dict) and r.get("path") and expand(r["path"])]
    nested = {os.path.realpath(expand(r["path"])) for r in every}
    # A root with its own `level` (Xcode's DerivedData: rebuilding costs time) is swept
    # only at that pressure or above — and never as an entry of another root.
    # A root may carry its own age in HOURS (`hours`: {level: h}): pytest's numbered run
    # directories are dead the moment their run ends, but the base that holds them is never
    # older than the newest run, so swept as one entry of the temporary root it never went
    # (7 runs, 2.5 GB, on 2026-10-12 with 1.2 GB free).
    roots = [(expand(r["path"]), int(r.get("depth", 1)),
              at - by_level(r, "hours", level, days * 24) * 3600 if isinstance(r.get("hours"), dict) else cutoff)
             for r in every if LEVELS.index(r.get("level", "normal")) <= LEVELS.index(level)]
    out = []
    for root, depth, cutoff in roots:
        if guarded(root, degraded):
            continue
        level_dirs = [root]
        for _ in range(depth - 1):
            level_dirs = [e.path for d in level_dirs if os.path.isdir(d) and not os.path.islink(d)
                          for e in _scan(d) if e.is_dir(follow_symlinks=False)]
        for d in level_dirs:
            entries = []
            for e in _scan(d):
                with contextlib.suppress(OSError):
                    entries.append((e.stat(follow_symlinks=False).st_mtime, e))
            # OLDEST FIRST; an entry whose own time is recent is not walked at all.
            for top, e in sorted(entries, key=lambda x: x[0]):
                if top >= cutoff:
                    break
                if budget and budget.out("temp"):
                    return out
                full = e.path
                if (os.path.realpath(full) in nested or any(fnmatch.fnmatch(e.name, k) for k in keep)
                        or e.is_symlink() or not owned(full)):
                    continue
                r = inspect(full, cutoff, time.monotonic() + max(1.0, budget.left()) if budget else None)
                if r["complete"] and not holders.busy(full):
                    out.append({"class": "temp", "target": full, "bytes": r["size"], "cutoff": cutoff,
                                "why": f"untouched {(at - r['newest']) / 86400:.1f} days, nobody holds it"})
    return out


def clone_root() -> pathlib.Path | None:
    """Where macOS keeps a user's code-signing clones (`$TMPDIR/../X`); None elsewhere.
    One function, so a test points it into its own directory — a test that reached the
    real one removed a machine's clones on 2026-10-11 (see the run record)."""
    if sys.platform != "darwin":
        return None
    return pathlib.Path(tempfile.gettempdir()).parent / "X"


def plan_cache_dirs(cfg: dict, level: str, holders: Holders, at: float, budget: Budget | None = None,
                    degraded: list | None = None) -> list[dict]:
    """Applications' and tools' own cache folders nobody touched for `cache_days`.

    The hand cleanup of 2026-10-10 freed about 10 GB here. A cache folder is regenerable —
    except the downloaded browsers and SDKs a tool does NOT fetch again by itself
    (`cache_keep`), which stay. Anything held or used recently stays too."""
    days = by_level(cfg, "cache_days", level, 7)
    cutoff = at - days * 86400
    keep = cfg.get("cache_keep") or []
    out = []
    for root in (expand(r) for r in cfg.get("cache_roots") or []):
        if not root or guarded(root, degraded):
            continue
        entries = []
        for e in _scan(root):
            with contextlib.suppress(OSError):
                entries.append((e.stat(follow_symlinks=False).st_mtime, e))
        for _top, e in sorted(entries, key=lambda x: x[0]):
            if budget and budget.out("cache-dir"):
                return out
            if any(fnmatch.fnmatch(e.name, k) for k in keep) or e.is_symlink() or not owned(e.path):
                continue
            r = inspect(e.path, cutoff, time.monotonic() + max(1.0, budget.left()) if budget else None,
                        limit=200_000)
            if r["complete"] and not holders.busy(e.path):
                out.append({"class": "cache-dir", "target": e.path, "bytes": r["size"], "cutoff": cutoff,
                            "why": f"a cache untouched {(at - r['newest']) / 86400:.1f} days, nobody holds it"})
    return out


def plan_clones(cfg: dict, holders: Holders, at: float) -> list[dict]:
    """Code-sign clones a Chromium browser left behind (macOS): one per launch, removed by
    the browser only on a clean exit. A clone a running browser uses is held open."""
    base = clone_root()
    if base is None:
        return []
    hours = _number(cfg.get("clone_min_age_hours", 1), 1)
    cutoff = at - hours * 3600
    out = []
    for holder in _scan(str(base)):
        if not holder.name.endswith(".code_sign_clone") or not holder.is_dir(follow_symlinks=False):
            continue
        for e in _scan(holder.path):
            try:
                mtime = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if e.name.startswith("code_sign_clone.") and mtime < cutoff and not holders.busy(e.path):
                out.append({"class": "browser-clone", "target": e.path, "bytes": 0,
                            "why": f"left by a browser {(at - mtime) / 3600:.0f} h ago, no process uses it"})
    return out


def trash_dirs() -> list[str]:
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        return [str(home / ".Trash")]
    if sys.platform.startswith("linux"):
        return [str(home / ".local/share/Trash/files")]
    return []


def plan_trash(cfg: dict, level: str, at: float, degraded: list) -> list[dict]:
    """What has been in the Trash longer than `trash_days` — by the moment it was put there
    (ctime: moving a file changes it), not by when it was last edited."""
    days = by_level(cfg, "trash_days", level, 30)
    if days <= 0:
        return []
    out = []
    for d in trash_dirs():
        if not os.path.isdir(d):
            continue
        try:
            entries = list(os.scandir(d))
        except PermissionError:
            degraded.append({"source": "trash", "reason": "the Trash is not readable by this job "
                             "(macOS asks for Full Disk Access); it was left as it is"})
            continue
        except OSError:
            continue
        for e in entries:
            if e.name in (".DS_Store",):
                continue
            try:
                age = (at - e.stat(follow_symlinks=False).st_ctime) / 86400
            except OSError:
                continue
            if age >= days:
                r = inspect(e.path)
                if r["complete"]:
                    out.append({"class": "trash", "target": e.path, "bytes": r["size"],
                                "why": f"in the Trash {age:.0f} days"})
    return out


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_PATH:
        cand = pathlib.Path(os.path.expanduser(d)) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def plan_commands(cfg: dict, level: str, last: dict | None = None, at: float | None = None) -> list[dict]:
    """Cache tools' own cleanup, each at the lowest pressure it is allowed at, and no more
    often than its `every_hours` (a whole-cache clean four times an hour re-downloads every
    environment's packages)."""
    rank = LEVELS.index(level)
    at = time.time() if at is None else at
    out = []
    for c in cfg.get("commands") or []:
        if LEVELS.index(c.get("level", "normal")) > rank:
            continue
        argv = list(c.get("argv") or [])
        tool = find_tool(argv[0]) if argv else None
        if not tool:
            continue
        target = " ".join(argv)
        every = _number(c.get("every_hours", cfg.get("command_every_hours", 6)), 6)
        ran = parse_stamp((last or {}).get(target))
        if ran is not None and at - ran < every * 3600:
            continue
        out.append({"class": "cache-command", "target": target, "argv": [tool, *argv[1:]],
                    "busy_if": c.get("busy_if") or [], "needs": c.get("needs_running") or [],
                    "busy_output": c.get("busy_output") or "",
                    "timeout": _number(c.get("timeout_seconds") or cfg.get("command_timeout_seconds", 90), 90),
                    "bytes": 0, "why": c.get("why") or "the tool's own cleanup"})
    return out


def checkout_idle_days(root: str, at: float, timeout: float = 60) -> float | None:
    """Days since anybody worked in a checkout: the newest of its last checkout or commit
    (`logs/HEAD`, `HEAD`) and of the files it holds uncommitted. NOT the index: every
    `git status` — the observatory's own survey included — rewrites it (measured 2026-10-11)."""
    gitdir = pathlib.Path(root) / ".git"
    if gitdir.is_file():
        try:
            gitdir = pathlib.Path(gitdir.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
        except (OSError, IndexError):
            return None
        if not gitdir.is_absolute():
            gitdir = (pathlib.Path(root) / gitdir).resolve()
    if not gitdir.is_dir():
        return None
    if (gitdir / "index.lock").exists():
        return 0.0                           # git is working in it right now
    stamps = []
    for f in (gitdir / "logs" / "HEAD", gitdir / "HEAD"):
        with contextlib.suppress(OSError):
            stamps.append(f.stat().st_mtime)
    try:
        # Through safe_git, the engine's one git door: no hooks, no external diff driver, and
        # no optional index lock taken in somebody's working checkout.
        p = safe_git.run(["ls-files", "-m", "-o", "--exclude-standard", "-z"], repo=root,
                         timeout=max(5.0, timeout), text=False,
                         extra_env={"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode:
        return None
    for name in p.stdout.decode("utf-8", "replace").split("\0")[:5000]:
        if name:
            with contextlib.suppress(OSError):
                stamps.append(os.lstat(os.path.join(root, name)).st_mtime)
    return (at - max(stamps)) / 86400 if stamps else None


def checkouts() -> list[str]:
    """Every checkout and worktree the git survey knows."""
    found: list[str] = []
    hygiene = _read(paths.SCRATCH / "git-hygiene.json")
    for c in hygiene.get("checkouts") or []:
        found.append(c.get("path") or "")
        found += [w.get("path") or "" for w in c.get("worktrees") or [] if w.get("state") != "missing"]
    return sorted({os.path.realpath(p) for p in found if p and os.path.isdir(p)})


def _force_tracked(dirpath: str, full: str, timeout: float) -> bool:
    """Whether git tracks anything inside an ignored folder (`git add -f`): then the folder
    holds somebody's file and is not build output. `git check-ignore` usually already calls
    such a folder not ignored; this is the second line, for the patterns where it does not."""
    try:
        p = safe_git.run(["ls-files", "-z", "--", full], repo=dirpath, timeout=max(5.0, timeout), text=False)
    except (OSError, subprocess.SubprocessError):
        return True
    return p.returncode != 0 or bool(p.stdout.strip(b"\0"))


def plan_builds(cfg: dict, level: str, holders: Holders, at: float, names: list[str],
                budget: Budget | None = None, degraded: list | None = None) -> list[dict]:
    """Regenerable build and dependency output of checkouts nobody worked in for
    `build_idle_days`: a folder git ignores, tracks nothing inside, nobody holds, and that
    holds no socket and no other account's file. Never inside `.git`, never in a checkout
    nested in another (that one is judged on its own), never in a privacy-guarded place."""
    days = by_level(cfg, "build_idle_days", level, 14)
    roots = checkouts()
    wanted = set(names)
    out = []
    left = (lambda: budget.left()) if budget else (lambda: 3600.0)  # noqa: E731
    for root in roots:
        if budget and budget.out("build-output"):
            break
        if guarded(root, degraded):
            continue
        idle = checkout_idle_days(root, at, min(60.0, left()))
        if idle is None or idle < days or holders.busy(root):
            continue
        others = [r for r in roots if r != root and r.startswith(root + "/")]
        for dirpath, dirnames, _ in os.walk(root):
            if budget and budget.out("build-output"):
                return out
            if any(dirpath == o or dirpath.startswith(o + "/") for o in others):
                dirnames[:] = []
                continue
            depth = dirpath[len(root):].count(os.sep)
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if d == ".git" or os.path.islink(full):
                    continue
                if d in wanted:
                    t = min(30.0, max(1.0, left()))
                    ignored = safe_git.run(["check-ignore", "-q", full], repo=dirpath, timeout=t).returncode == 0
                    if ignored and not holders.busy(full) and not _force_tracked(dirpath, full, t):
                        r = inspect(full, None, time.monotonic() + max(1.0, left()), limit=500_000)
                        if r["complete"]:
                            out.append({"class": "build-output", "target": full, "bytes": r["size"],
                                        "why": f"git-ignored {d}; checkout idle {idle:.0f} days"})
                    continue
                if depth < 5:
                    keep.append(d)
            dirnames[:] = keep
    return out


# --- reports that act on nothing -------------------------------------------------

def large_files(cfg: dict, previous: dict | None, force: bool) -> dict | None:
    """The largest files under the home directory, daily, within a time budget. Reported,
    never removed: a big file is somebody's until they say otherwise."""
    every = _number(cfg.get("large_files_every_hours", 24), 24) * 3600
    last = (previous or {}).get("large_files") or {}
    stamp = parse_stamp(last.get("measured_at"))
    if not force and stamp is not None and time.time() - stamp < every:
        return last or None
    from scan_machine import protected_place
    home = pathlib.Path.home()
    floor = _number(cfg.get("large_file_min_mb", 1024), 1024) * 1024 * 1024
    budget = _number(cfg.get("large_files_budget_seconds", 120), 120)
    skip = set(cfg.get("large_files_skip") or [])
    top = int(_number(cfg.get("large_files_top", 30), 30))
    started, found, complete = time.monotonic(), [], True
    dev = os.lstat(home).st_dev
    stack = [str(home)]
    while stack:
        if time.monotonic() - started > budget:
            complete = False
            break
        d = stack.pop()
        for e in _scan(d):
            try:
                s = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if s.st_dev != dev:
                continue
            if stat.S_ISDIR(s.st_mode):
                if e.name in skip or protected_place(pathlib.Path(e.path), home):
                    continue
                stack.append(e.path)
            elif stat.S_ISREG(s.st_mode) and s.st_size >= floor:
                found.append({"path": e.path, "gb": round(s.st_size / GB, 2),
                              "modified": datetime.fromtimestamp(s.st_mtime, timezone.utc).strftime("%Y-%m-%d")})
    found.sort(key=lambda f: -f["gb"])
    return {"measured_at": now(), "min_mb": int(floor / 1048576), "complete": complete, "files": found[:top]}


def memory_report() -> dict | None:
    """Who holds the memory that swap writes to this disk, from the machine survey."""
    machine = _read(paths.SCRATCH / "machine.json")
    if not machine:
        return None
    mem = machine.get("memory") or {}
    groups = (machine.get("processes") or {}).get("groups") or []
    sims = [g for g in groups if str(g.get("origin", "")).startswith("simulator:")]
    detached = [g for g in groups if str(g.get("origin", "")).startswith("detached:")]
    return {"measured_at": machine.get("measured_at"), "swap_gb": round((mem.get("swap_used_mb") or 0) / 1024, 1),
            "holders": [{k: g.get(k) for k in ("origin", "processes", "rss_mb")} for g in groups[:8]],
            "simulators": [{"name": g["origin"].split(":", 1)[1], "rss_mb": g.get("rss_mb"),
                            "command": f"xcrun simctl shutdown \"{g['origin'].split(':', 1)[1]}\""} for g in sims],
            "detached_mb": round(sum(g.get("rss_mb") or 0 for g in detached), 1)}


def refresh(script: str, out: pathlib.Path, hours: float, timeout: int, args: tuple = ()) -> str:
    """Run a survey when its file is older than `hours`; the outcome as a word."""
    try:
        age = time.time() - out.stat().st_mtime
    except OSError:
        age = float("inf")
    if age < hours * 3600:
        return "fresh"
    try:
        p = subprocess.run([sys.executable, str(ROOT / script), str(out), *args], capture_output=True,
                           text=True, timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed: {type(exc).__name__}"
    return "refreshed" if p.returncode == 0 else f"exit {p.returncode}"


# --- acting -----------------------------------------------------------------------

def act(a: dict, holders: Holders) -> dict:
    """Do one action after re-checking it, and measure what the disk got back."""
    rec = {"at": now(), "class": a["class"], "target": a["target"], "mode": "guard", "tier": "auto"}
    if a.get("bytes"):
        rec["kb"] = int(a["bytes"] / 1024)
    before = free_bytes()
    if a["class"] == "cache-command":
        needs = running(a["needs"]) if a.get("needs") else True
        if needs is not True:
            return {**rec, "result": "skipped",
                    "reason": "its daemon is not running; the guard starts nothing" if needs is False
                    else "whether its daemon runs could not be read"}
        busy = running(a["busy_if"]) if a.get("busy_if") else False
        if busy is not False:
            return {**rec, "result": "skipped",
                    "reason": "the tool is running now" if busy else "whether the tool runs could not be read"}
        try:
            # SHORT, and a wait is not a fault (2026-10-11): `uv cache prune` waits on the lock
            # a long-running `uv tool run` server holds, and one such wait spent half a run.
            p = subprocess.run(a["argv"], capture_output=True, text=True,
                               timeout=_number(a.get("timeout"), 90), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {**rec, "result": "skipped", "reason": "held up past its time — something else holds the tool's cache"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {**rec, "result": "failed", "reason": f"{type(exc).__name__}"}
        said = (p.stderr or p.stdout or "").strip()
        if p.returncode and a.get("busy_output") and a["busy_output"] in said:
            return {**rec, "result": "skipped", "reason": f"its cache is in use: {said[:120]}"}
        rec["result"] = "removed" if p.returncode == 0 else "failed"
        if p.returncode:
            rec["reason"] = f"exit {p.returncode}: {said[:160]}"
    else:
        target = a["target"]
        if not os.path.lexists(target):
            return {**rec, "result": "skipped", "reason": "already gone"}
        if holders.busy(target):
            return {**rec, "result": "skipped", "reason": "a process holds it now"}
        if a.get("cutoff") is not None:
            # STILL OLD, right now: the plan may be minutes old, and reading or entering a
            # folder does not change it, but writing anything in it does.
            r = inspect(target, a["cutoff"], time.monotonic() + 30)
            if not r["complete"]:
                return {**rec, "result": "skipped", "reason": f"changed since the plan: {r['reason']}"}
        try:
            remove(target, probe=holders.mode == "rename")
            rec["result"] = "removed"
        except PermissionError as exc:
            if holders.mode == "rename":
                return {**rec, "result": "skipped", "reason": "a process holds it now (it could not be renamed)"}
            rec.update(result="failed", reason=f"{type(exc).__name__}: {exc}"[:200])
        except OSError as exc:
            rec.update(result="failed", reason=f"{type(exc).__name__}: {exc}"[:200])
    rec["freed_mb"] = round((free_bytes() - before) / 1048576)
    return rec


def journal_file() -> pathlib.Path:
    return paths.STATE / "logs" / "disk-guard.jsonl"


def journal(rows: list[dict]) -> None:
    if not rows:
        return
    log = journal_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.chmod(log, 0o600)


def planners(cfg: dict, level: str, holders: Holders, at: float, degraded: list,
             last_commands: dict | None = None) -> list[tuple[str, object]]:
    """Each class's planner, in the order a run takes them. Each takes a Budget; within a
    class the largest go first."""
    big = lambda xs: sorted(xs, key=lambda a: -(a.get("bytes") or 0))  # noqa: E731
    return [
        ("cache-dir", lambda b: big(plan_cache_dirs(cfg, level, holders, at, b, degraded))),
        ("build-output", lambda b: big(plan_builds(cfg, level, holders, at, artifact_names(), b, degraded))),
        ("temp", lambda b: big(plan_temp(cfg, level, holders, at, b, degraded))),
        ("trash", lambda b: big(plan_trash(cfg, level, at, degraded))),
        ("browser-clone", lambda b: plan_clones(cfg, holders, at)),
        ("cache-command", lambda b: plan_commands(cfg, level, last_commands, at)),
    ]


def build_plan(cfg: dict, level: str, holders: Holders, at: float, degraded: list) -> list[dict]:
    """Every action at this pressure, in the order a run takes them (plan only)."""
    budget = Budget(_number(cfg.get("plan_budget_seconds", 300), 300), degraded)
    out: list[dict] = []
    for _name, planner in planners(cfg, level, holders, at, degraded):
        out += planner(budget)
    return out


def cleanup_lock():
    """The lock every cleanup shares (cleanup_lock.py)."""
    import cleanup_lock as lock
    return lock.held()


def _write_report(doc: dict) -> None:
    out_file = paths.SCRATCH / "disk-guard.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out_file)


def _git_tier(apply: bool, level: str, seconds: float) -> dict:
    """The cleanup's git tier, under the guard's lock and pressure, inside the time left."""
    env = {**os.environ, LOCK_HELD_ENV: "1"}
    try:
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "cleanup.py"), "--apply" if apply else "--auto",
                            "--level", level, "--deadline-seconds", str(int(max(10, seconds - 15))), "--json",
                            "--git-only"],
                           capture_output=True, text=True, timeout=max(20.0, seconds), cwd=str(ROOT), env=env)
    except subprocess.TimeoutExpired:
        return {"result": "stopped", "reason": f"past {seconds:.0f} s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"result": "failed", "reason": type(exc).__name__}
    line = (p.stdout.strip().splitlines() or [""])[-1]
    try:
        doc = json.loads(line) if line.startswith("{") else {}
    except ValueError:
        doc = {}
    return doc or {"result": "failed" if p.returncode else "ran", "reason": line[:200]}


def run(argv: list[str]) -> dict:
    """One guard run: lock, level, cadence, then the classes one at a time — each with a fresh
    holders listing, a fair share of the time and a re-check at every removal — then the git
    tier, the surveys, and the report, which is written whatever happens."""
    cfg = load_config()
    started, started_at = time.monotonic(), now()
    budget = _number(cfg.get("run_budget_seconds", 900), 900)
    if "--budget" in argv[:-1]:
        budget = _number(argv[argv.index("--budget") + 1], budget)
    left = lambda: budget - (time.monotonic() - started)  # noqa: E731
    previous = _read(paths.SCRATCH / "disk-guard.json") or None
    auto, apply = "--auto" in argv, "--apply" in argv
    enabled = configuration.enabled("auto_cleanup", "features")
    acting = apply or (auto and enabled)
    level = None
    if "--level" in argv[:-1]:
        level = argv[argv.index("--level") + 1]
    free0 = free_bytes()
    level = level if level in LEVELS else level_for(free0, cfg)
    base = {"schema_version": 2, "at": now(), "started_at": started_at, "level": level, "acting": acting,
            "auto_enabled": enabled, "free_gb_before": round(free0 / GB, 1), "free_gb_after": round(free0 / GB, 1),
            "freed_gb": 0.0, "results": {"removed": 0, "skipped": 0, "failed": 0}, "not_due": False, "busy": False}
    # HOW OFTEN: at `normal` once per `normal_every_minutes`, measured from the previous
    # run's START with half an interval's tolerance; below `low`, every pass.
    if auto and level == "normal" and previous and previous.get("acting"):
        since = time.time() - (parse_stamp(previous.get("started_at") or previous.get("at")) or 0)
        gap = _number(cfg.get("normal_every_minutes", 60), 60) * 60 - _number(cfg.get("due_tolerance_seconds", 450), 450)
        if since < gap:
            return {**base, "not_due": True, "acting": False}
    degraded: list[dict] = []
    surveys: dict = {}
    phases: dict = {}
    plan: list[dict] = []
    rows: list[dict] = []
    found = (previous or {}).get("large_files")
    last_commands = dict((previous or {}).get("commands_last") or {})
    holders_mode = "unknown"
    with cleanup_lock() as mine:
        if not mine:
            return {**base, "busy": True, "acting": False,
                    "degraded": [{"source": "lock", "reason": "another cleanup is running; this pass skips"}]}
        try:
            reserve = _number(cfg.get("run_reserve_seconds", 60), 60)
            plan_cap = _number(cfg.get("plan_budget_seconds", 300), 300)
            cap = int(_number(cfg.get("max_actions_per_class", 20000), 20000))
            at = time.time()
            classes = list(CLASSES)
            # The git tier's own share, kept back before the classes split the rest: as the last
            # comer it got only what they left, and on the first scheduled 0.24.0 pass (critical,
            # 425 of 480 s) that was nothing — the worktrees and their dependencies, where most of
            # the space is, were never reached (2026-10-12).
            git_floor = (min(_number(cfg.get("git_tier_seconds", 120), 120), max(0.0, (left() - reserve) / 3))
                         if acting else 0.0)
            for n, name in enumerate(classes):
                share = (left() - reserve - git_floor) / (len(classes) - n)
                if share <= 1:
                    if acting:
                        degraded.append({"source": "act", "reason": f"stopped at the run budget before {name}; "
                                                                    f"the rest is looked at next pass"})
                    break
                mark = time.monotonic()
                # A FRESH LISTING for every class: one taken at the start was minutes old by the
                # time the last removal used it (review, 2026-10-12).
                holders = (Holders([], "none") if name == "cache-command"      # a command needs no listing
                           else Holders.take(min(120.0, max(5.0, share / 2))))
                holders_mode = holders.mode
                if not holders.known and name != "cache-command":
                    if not any(d.get("source") == "holders" for d in degraded):
                        degraded.append({"source": "holders", "reason": "what processes hold could not be read; "
                                                                        "no path is removed"})
                    continue
                planner = dict(planners(cfg, level, holders, at, degraded, last_commands))[name]
                actions = planner(Budget(min(plan_cap, share / 2), degraded))
                plan += actions
                if acting:
                    class_end = mark + share
                    for i, a in enumerate(actions[:cap]):
                        if time.monotonic() > class_end or left() < reserve:
                            degraded.append({"source": name, "reason": f"its share of the run ended; "
                                                                       f"{len(actions) - i} action(s) wait for next pass"})
                            break
                        if a["class"] == "cache-command":
                            # A COMMAND IS BOUNDED BY ITS SHARE, not only checked between commands:
                            # on 2026-10-12 three of them ran 271 s and took the git tier's time.
                            remaining = class_end - time.monotonic()
                            if remaining < 30:
                                degraded.append({"source": name, "reason": f"its share of the run ended; "
                                                                           f"{len(actions) - i} action(s) wait for next pass"})
                                break
                            a = {**a, "timeout": min(_number(a.get("timeout"), 90), remaining)}
                        try:
                            row = act(a, holders)
                        except (OSError, subprocess.SubprocessError) as exc:
                            row = {"at": now(), "class": a["class"], "target": a["target"], "mode": "guard",
                                   "result": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]}
                        rows.append(row)
                        journal([row])           # as it happens: a run stopped from outside keeps its record
                        if a["class"] == "cache-command" and row["result"] == "removed":
                            last_commands[a["target"]] = row["at"]
                phases[name] = round(time.monotonic() - mark)
            if acting:
                room = left() - reserve / 2
                surveys["git-cleanup"] = (_git_tier(apply, level, room)
                                          if room > 30 else {"result": "skipped", "reason": "the run budget is spent"})
            if acting or "--refresh" in argv:
                for name, script, hours, cap_s in (
                        ("git-hygiene", "collectors/scan_git_hygiene.py", cfg.get("git_survey_every_hours", 6), 600),
                        ("machine", "collectors/scan_machine.py", cfg.get("machine_survey_every_hours", 2), 300)):
                    room = left() - reserve / 2
                    surveys[name] = (refresh(script, paths.SCRATCH / f"{name}.json", _number(hours, 6), int(min(cap_s, room)))
                                     if room > 30 else "skipped: the run budget is spent")
            room = left() - reserve / 2
            if room > 30 or "--large-files" in argv:
                mark = time.monotonic()
                found = large_files({**cfg, "large_files_budget_seconds":
                                     min(_number(cfg.get("large_files_budget_seconds", 120), 120), max(room, 30))},
                                    previous, "--large-files" in argv)
                phases["large-files"] = round(time.monotonic() - mark)
        finally:
            free1 = free_bytes()
            results = {r: sum(1 for x in rows if x["result"] == r) for r in ("removed", "skipped", "failed")}
            doc = {**base, "at": now(), "free_gb_after": round(free1 / GB, 1),
                   "freed_gb": round((free1 - free0) / GB, 2), "seconds": round(time.monotonic() - started),
                   "budget_seconds": round(budget), "phases": phases, "holders": holders_mode,
                   "planned": {cls: sum(1 for a in plan if a["class"] == cls) for cls in CLASSES},
                   "planned_gb": round(sum(a.get("bytes") or 0 for a in plan) / GB, 1),
                   "results": results, "commands_last": last_commands,
                   "actions": [{k: a[k] for k in ("class", "target", "why") if k in a}
                               | {"gb": round((a.get("bytes") or 0) / GB, 2)} for a in plan[:200]],
                   "surveys": surveys, "large_files": found, "memory": memory_report(), "degraded": degraded}
            if acting:
                journal([{"at": doc["at"], "kind": "summary", "mode": "guard", "level": level, **results,
                          "freed_mb": round((free1 - free0) / 1048576), "seconds": doc["seconds"],
                          "git": surveys.get("git-cleanup")}])
            _write_report(doc)
    return doc


def summary_line(doc: dict) -> dict:
    """What the maintenance job records: never an earlier run's numbers as this run's."""
    git = (doc.get("surveys") or {}).get("git-cleanup")
    return {**{k: doc.get(k) for k in ("level", "acting", "free_gb_before", "free_gb_after", "freed_gb",
                                       "planned", "results", "seconds", "not_due", "busy")},
            **({"git": git} if isinstance(git, dict) else {})}


def main(argv: list[str]) -> int:
    try:
        doc = run(argv)
    except Exception as exc:  # noqa: BLE001 — the job must get a line, never a silent crash
        if "--json" in argv:
            print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}))
        else:
            print(f"disk guard: failed — {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 2
    if "--json" in argv:
        # The maintenance job reads this line instead of the report file: the file's
        # directory can be moved (OBSERVATORY_SCRATCH), the process's own answer cannot.
        print(json.dumps(summary_line(doc)))
        return 0
    if doc.get("not_due"):
        print(f"disk guard: {doc['free_gb_before']} GB free, pressure normal; not due (it acts hourly while "
              f"space is normal)")
        return 0
    if doc.get("busy"):
        print("disk guard: another cleanup is running; nothing done")
        return 0
    p = doc["planned"]
    planned = ", ".join(f"{k} {v}" for k, v in p.items() if v) or "nothing"
    if not doc["acting"]:
        why = " (features.auto_cleanup is off)" if "--auto" in argv and not doc.get("auto_enabled") else ""
        print(f"disk guard: {doc['free_gb_before']} GB free, pressure {doc['level']}; plan only{why} — "
              f"{planned}. `--apply` acts now.")
        return 0
    r = doc["results"]
    print(f"disk guard: pressure {doc['level']}; {r['removed']} done, {r['skipped']} skipped on re-check, "
          f"{r['failed']} failed; free {doc['free_gb_before']} → {doc['free_gb_after']} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
