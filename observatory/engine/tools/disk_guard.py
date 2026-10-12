#!/usr/bin/env python3
"""The disk guard: keep free space without breaking anybody's work, every hour.

WHY. On 2026-10-11 the operator's disk fell to 1.7 GB free while the self-cleaning
had not run for a day: `machine` and `cleanup` sit in the middle of the tick, after
the slow collectors, and a loaded machine spends the tick's whole window before
reaching them ("not started — the last 300 s … are kept", hour after hour). The
fuller the disk, the slower the machine, the less the cleanup ran. So the guard is
not a tick step: the hourly maintenance job (`maintain run`, launchd/systemd, always
on) runs it FIRST, before the backup that itself needs space, and a person can run it
at once with `project-observatory full disk-guard [--apply]`.

PRESSURE DECIDES HOW FAR IT GOES. `free_gb` below `levels.low` or `levels.critical`
(config/cleanup.json → guard) moves the thresholds of every class: build output of a
checkout idle 14 days at `normal`, 3 at `low`, 1 at `critical`; temporary entries
older than 3 days, then 1; cache tools' gentle prune, then their full clean.

WHAT IT NEVER DOES — the reason every class looks the same before it acts:
  * removes a path a process has open or works inside: ONE `lsof` listing per run
    (open files and working directories); when that listing cannot be taken, no
    path is removed at all and the run says so;
  * opens a privacy-guarded place (`scan_machine.protected_place`), follows a
    symlink, or touches another user's file;
  * stops, signals or restarts a process: memory and swap are REPORTED, with the
    command that would free them (operator decision 2026-10-11);
  * starts an app: a cache command whose tool needs a daemon (docker) runs only when
    that daemon is already running;
  * runs a cache command while the same tool is running (a prune under an install).

THE NUMBER IT REPORTS IS THE ONE THE DISK SAYS. `du` counts an APFS clone in full
(measured 2026-10-11: 19 abandoned Chrome code-sign clones, "13 GB" by `du`, freed
0 MB), so every action is journalled with the free-space delta read from `statvfs`
before and after it, beside the estimate.

Every action is one line in store/logs/cleanup.jsonl (`mode: guard`); the run's
report is store/raw/disk-guard.json, which the Machine page and findings read.

    disk_guard.py                plan only: what would go at the current pressure
    disk_guard.py --apply        act now (a person, or an agent on their word)
    disk_guard.py --auto         act when features.auto_cleanup is on (maintenance job)
    disk_guard.py --level L      plan/act as if pressure were L (normal|low|critical)
    disk_guard.py --json         print the run's summary as one JSON line (the maintenance job)
    disk_guard.py --budget S     stop by itself after S seconds (default `run_budget_seconds`)
"""
from __future__ import annotations

import bisect
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
import configuration  # noqa: E402
import osprivacy  # noqa: E402
import paths  # noqa: E402
import safe_git  # noqa: E402

LEVELS = ("normal", "low", "critical")
GB = 1024 ** 3
#: Where a cache tool lives when the job's PATH (launchd's minimal one) does not say.
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin", "~/go/bin")
#: An entry tree larger than this is not walked to its end; it is treated as fresh.
WALK_LIMIT = 50_000


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- configuration -------------------------------------------------------------

def load_config() -> dict:
    """The guard section: the shipped defaults with the workspace's own keys on top.

    A workspace's config/cleanup.json predates the guard and carries no `guard` key;
    reading it alone would leave the guard with no thresholds at all."""
    def read(f):
        try:
            return json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    shipped = (read(ROOT / "defaults" / "cleanup.json").get("guard") or {})
    own = (read(paths.config_file("cleanup.json")).get("guard") or {})
    merged = {**shipped, **own}
    for key in ("levels", "temp_days", "build_idle_days", "trash_days"):
        if isinstance(shipped.get(key), dict) and isinstance(own.get(key), dict):
            merged[key] = {**shipped[key], **own[key]}
    return merged


def artifact_names() -> list[str]:
    try:
        own = json.loads(paths.config_file("cleanup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        own = {}
    try:
        shipped = json.loads((ROOT / "defaults" / "cleanup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shipped = {}
    return list(own.get("artifact_dirs") or shipped.get("artifact_dirs") or [])


# --- the disk ------------------------------------------------------------------

def free_bytes(path: str | os.PathLike | None = None) -> int:
    return shutil.disk_usage(str(path or pathlib.Path.home())).free


def level_for(free: int, cfg: dict) -> str:
    """normal | low | critical, from free bytes and the configured lines (GB)."""
    levels = cfg.get("levels") or {}
    if free < float((levels.get("critical") or {}).get("free_gb", 10)) * GB:
        return "critical"
    if free < float((levels.get("low") or {}).get("free_gb", 25)) * GB:
        return "low"
    return "normal"


def by_level(cfg: dict, key: str, level: str, default: float) -> float:
    table = cfg.get(key) or {}
    try:
        return float(table.get(level, default))
    except (TypeError, ValueError):
        return default


# --- who holds what ------------------------------------------------------------

class Holders:
    """Every path a process has open or works inside, from ONE `lsof` listing.

    `known` is False when the listing could not be taken (no lsof, a timeout): then
    nothing is known to be free, and `busy()` answers True for every path — the guard
    removes no path rather than guess."""

    def __init__(self, names: list[str] | None):
        self.known = names is not None
        # lsof already prints the path the kernel resolved (`/private/var/…`); resolving
        # 170,000 names again cost a minute a run. The TARGET is resolved instead.
        self.names = sorted({n for n in names or [] if n.startswith("/")})

    #: How busy() was decided: "lsof", "proc" (Linux without lsof) or "rename" (Windows, where
    #: a folder something holds cannot be renamed — the removal itself is the probe).
    mode = "lsof"

    @classmethod
    def take(cls, timeout: int = 120) -> "Holders":
        if sys.platform == "win32":
            h = cls([])
            h.mode = "rename"
            return h
        if not shutil.which("lsof"):
            if os.path.isdir("/proc/self/fd"):
                h = cls(proc_holders())
                h.mode = "proc"
                return h
            return cls(None)
        try:
            p = subprocess.run(["lsof", "-n", "-P", "-w", "-Fn"], capture_output=True, text=True,
                               timeout=timeout, errors="replace")
        except (OSError, subprocess.SubprocessError):
            return cls(None)
        # lsof exits 1 when one process could not be read and still lists the rest.
        if p.returncode not in (0, 1) or not p.stdout:
            return cls(None)
        return cls([line[1:] for line in p.stdout.splitlines() if line.startswith("n/")])

    def busy(self, path: str | os.PathLike) -> bool:
        if not self.known:
            return True
        real = os.path.realpath(str(path))
        i = bisect.bisect_left(self.names, real)
        if i < len(self.names) and self.names[i] == real:
            return True
        j = bisect.bisect_left(self.names, real + "/")
        return j < len(self.names) and self.names[j].startswith(real + "/")


def proc_holders() -> list[str]:
    """Every path this account's processes hold open or work in, from /proc (Linux without
    lsof). A process another account owns is unreadable and is not ours to protect."""
    found: list[str] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = f"/proc/{pid}"
        for link in ("cwd", "root"):
            try:
                found.append(os.readlink(f"{base}/{link}"))
            except OSError:
                pass
        try:
            fds = os.listdir(f"{base}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                found.append(os.readlink(f"{base}/fd/{fd}"))
            except OSError:
                pass
    return [p for p in found if p.startswith("/") and p != "/"]


def running(names: list[str]) -> bool:
    """True when a process with one of these executable basenames runs now."""
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return True                       # cannot tell — assume busy, skip the command
    have = {os.path.basename(line.strip()) for line in out.splitlines()}
    return any(n in have for n in names)


class Budget:
    """Seconds a run may spend planning. A class that runs out stops between items and
    says so in `degraded`; what it did not reach is first next pass (oldest first)."""

    def __init__(self, seconds: float, degraded: list):
        self.end, self.degraded, self.hit = time.monotonic() + seconds, degraded, set()

    def out(self, what: str) -> bool:
        if time.monotonic() < self.end:
            return False
        if what not in self.hit:
            self.hit.add(what)
            self.degraded.append({"source": what, "reason": "planning stopped at its time budget; "
                                  "the rest is looked at next pass, oldest first"})
        return True


# --- walking ---------------------------------------------------------------------

def newest_and_size(root: str, limit: int = WALK_LIMIT) -> tuple[float, int, bool]:
    """(newest mtime, apparent bytes, complete) for a path, symlinks not followed.

    `complete` is False when the tree held a socket or more than `limit` entries:
    a socket is somebody's live endpoint, and a tree too large to finish cannot be
    shown to be old, so both read as "do not touch"."""
    try:
        st = os.lstat(root)
    except OSError:
        return 0.0, 0, False
    newest, size, seen = st.st_mtime, (st.st_size if stat.S_ISREG(st.st_mode) else 0), 0
    if stat.S_ISSOCK(st.st_mode):
        return newest, 0, False
    if not stat.S_ISDIR(st.st_mode):
        return newest, size, True
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            return newest, size, False
        for e in entries:
            seen += 1
            if seen > limit:
                return newest, size, False
            try:
                s = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISSOCK(s.st_mode):
                return newest, size, False
            newest = max(newest, s.st_mtime)
            if stat.S_ISDIR(s.st_mode):
                stack.append(e.path)
            elif stat.S_ISREG(s.st_mode):
                size += s.st_size
    return newest, size, True


def remove(path: str, probe: bool = False) -> None:
    """Remove a path. With `probe` (Windows), rename it aside first: Windows refuses to rename a
    folder while any process holds a file in it, so a refusal is the "busy" answer lsof gives
    elsewhere, and nothing has been deleted when it comes."""
    p = pathlib.Path(path)
    if probe:
        aside = p.with_name(p.name + ".observatory-removing")
        os.rename(p, aside)            # PermissionError: something holds it — the caller skips
        p = aside
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
    return os.path.expanduser(raw.replace("{tmpdir}", tempfile.gettempdir().rstrip("/"))
                              .replace("{uid}", uid))


# --- the classes -------------------------------------------------------------------

def plan_temp(cfg: dict, level: str, holders: Holders, at: float, budget: Budget | None = None) -> list[dict]:
    """Entries of the temporary roots nobody touched for `temp_days`, held by nobody."""
    days = by_level(cfg, "temp_days", level, 3)
    keep = cfg.get("temp_keep") or []
    out, nested = [], set()
    every = [r for r in cfg.get("temp_roots") or [] if isinstance(r, dict) and r.get("path") and expand(r["path"])]
    for r in every:
        nested.add(os.path.realpath(expand(r["path"])))
    # A root with its own `level` (Xcode's DerivedData: rebuilding costs time) is
    # swept only at that pressure or above — but it is never swept as an entry of
    # another root either, so it stays in `nested` regardless.
    roots = [(expand(r["path"]), int(r.get("depth", 1))) for r in every
             if LEVELS.index(r.get("level", "normal")) <= LEVELS.index(level)]
    for root, depth in roots:
        level_dirs = [root]
        for _ in range(depth - 1):
            level_dirs = [e.path for d in level_dirs if os.path.isdir(d) and not os.path.islink(d)
                          for e in _scan(d) if e.is_dir(follow_symlinks=False)]
        for d in level_dirs:
            entries = []
            for e in _scan(d):
                try:
                    entries.append((e.stat(follow_symlinks=False).st_mtime, e))
                except OSError:
                    continue
            # OLDEST FIRST, and an entry whose own time is recent is not walked at all:
            # creating or removing anything at its top level made it fresh.
            for top, e in sorted(entries, key=lambda x: x[0]):
                if (at - top) / 86400 < days:
                    break
                if budget and budget.out("temp"):
                    return out
                full = e.path
                if (os.path.realpath(full) in nested or any(fnmatch.fnmatch(e.name, k) for k in keep)
                        or e.is_symlink() or not owned(full)):
                    continue
                newest, size, complete = newest_and_size(full)
                age = (at - newest) / 86400
                if complete and age >= days and not holders.busy(full):
                    out.append({"class": "temp", "target": full, "bytes": size,
                                "why": f"untouched {age:.1f} days, nobody holds it"})
    return out


def _scan(d: str) -> list:
    try:
        return list(os.scandir(d))
    except OSError:
        return []


def clone_root() -> pathlib.Path | None:
    """Where macOS keeps a user's code-signing clones (`$TMPDIR/../X`); None elsewhere.
    One function, so a test points it into its own directory — a test that reached the
    real one removed a machine's clones on 2026-10-11 (see the run record)."""
    if sys.platform != "darwin":
        return None
    return pathlib.Path(tempfile.gettempdir()).parent / "X"


def plan_cache_dirs(cfg: dict, level: str, holders: Holders, at: float, budget: "Budget | None" = None) -> list[dict]:
    """Applications' and tools' own cache folders nobody touched for `cache_days`.

    WHY (2026-10-11). The hand cleanup of 2026-10-10 freed about 10 GB from `~/Library/Caches`
    and `~/.cache` alone; the first guard left both to the tools' own commands and freed far
    less, and the disk ran out again. A cache folder is regenerable by definition — except the
    downloaded browsers and SDKs a tool does NOT fetch again by itself (`cache_keep`), which
    stay. A folder anyone holds open, or touched recently anywhere inside, stays too."""
    days = by_level(cfg, "cache_days", level, 7)
    keep = cfg.get("cache_keep") or []
    out = []
    for root in (expand(r) for r in cfg.get("cache_roots") or []):
        if not root:
            continue                  # a root of another OS ({localappdata} on macOS)
        entries = []
        for e in _scan(root):
            try:
                entries.append((e.stat(follow_symlinks=False).st_mtime, e))
            except OSError:
                continue
        for _top, e in sorted(entries, key=lambda x: x[0]):
            if budget and budget.out("cache-dir"):
                return out
            if any(fnmatch.fnmatch(e.name, k) for k in keep) or e.is_symlink() or not owned(e.path):
                continue
            newest, size, complete = newest_and_size(e.path, limit=200_000)
            age = (at - newest) / 86400
            if complete and age >= days and not holders.busy(e.path):
                out.append({"class": "cache-dir", "target": e.path, "bytes": size,
                            "why": f"a cache untouched {age:.1f} days, nobody holds it"})
    return out


def plan_clones(cfg: dict, holders: Holders, at: float) -> list[dict]:
    """Code-sign clones a Chromium browser left behind (macOS): one per launch, removed
    by the browser only on a clean exit. A clone a running browser uses is held open."""
    base = clone_root()
    if base is None:
        return []
    hours = float(cfg.get("clone_min_age_hours", 1))
    out = []
    for holder in _scan(str(base)):
        if not holder.name.endswith(".code_sign_clone") or not holder.is_dir(follow_symlinks=False):
            continue
        for e in _scan(holder.path):
            try:
                age = (at - e.stat(follow_symlinks=False).st_mtime) / 3600
            except OSError:
                continue
            if e.name.startswith("code_sign_clone.") and age >= hours and not holders.busy(e.path):
                out.append({"class": "browser-clone", "target": e.path, "bytes": 0,
                            "why": f"left by a browser {age:.0f} h ago, no process uses it"})
    return out


def trash_dirs() -> list[str]:
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        return [str(home / ".Trash")]
    if sys.platform.startswith("linux"):
        return [str(home / ".local/share/Trash/files")]
    return []


def plan_trash(cfg: dict, level: str, at: float, degraded: list) -> list[dict]:
    """What has been in the Trash longer than `trash_days` — by the moment it was put
    there (ctime: moving a file changes it), not by when it was last edited."""
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
                _, size, _ = newest_and_size(e.path)
                out.append({"class": "trash", "target": e.path, "bytes": size,
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


def plan_commands(cfg: dict, level: str) -> list[dict]:
    """Cache tools' own cleanup, each at the lowest pressure it is allowed at."""
    rank = LEVELS.index(level)
    out = []
    for c in cfg.get("commands") or []:
        if LEVELS.index(c.get("level", "normal")) > rank:
            continue
        argv = list(c.get("argv") or [])
        tool = find_tool(argv[0]) if argv else None
        if not tool:
            continue
        out.append({"class": "cache-command", "target": " ".join(argv), "argv": [tool, *argv[1:]],
                    "busy_if": c.get("busy_if") or [], "needs": c.get("needs_running") or [],
                    "busy_output": c.get("busy_output") or "",
                    "timeout": c.get("timeout_seconds") or cfg.get("command_timeout_seconds", 90),
                    "bytes": 0, "why": c.get("why") or "the tool's own cleanup"})
    return out


def checkout_idle_days(root: str, at: float) -> float | None:
    """Days since anybody worked in a checkout: the newest of its last checkout or
    commit (`logs/HEAD`, `HEAD`) and of the files it holds uncommitted. NOT the index:
    every `git status` — the observatory's own survey included — rewrites it, so by
    that measure every checkout on the machine was "used today" (measured 2026-10-11)."""
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
        try:
            stamps.append(f.stat().st_mtime)
        except OSError:
            pass
    try:
        # Through safe_git, the engine's one git door: no hooks, no external diff driver,
        # and no optional index lock taken in somebody's working checkout.
        p = safe_git.run(["ls-files", "-m", "-o", "--exclude-standard", "-z"], repo=root, timeout=60,
                         text=False, extra_env={"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode:
        return None
    for name in p.stdout.decode("utf-8", "replace").split("\0")[:5000]:
        if name:
            try:
                stamps.append(os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return (at - max(stamps)) / 86400 if stamps else None


def checkouts() -> list[str]:
    """Every checkout and worktree the git survey knows, plus the projects' folders."""
    found: list[str] = []
    try:
        hygiene = json.loads((paths.SCRATCH / "git-hygiene.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hygiene = {}
    for c in hygiene.get("checkouts") or []:
        found.append(c.get("path") or "")
        found += [w.get("path") or "" for w in c.get("worktrees") or [] if w.get("state") != "missing"]
    return sorted({os.path.realpath(p) for p in found if p and os.path.isdir(p)})


def plan_builds(cfg: dict, level: str, holders: Holders, at: float, names: list[str],
                budget: Budget | None = None) -> list[dict]:
    """Regenerable build and dependency output of checkouts nobody worked in for
    `build_idle_days`. Only directories git ignores (or outside git), never inside
    `.git`, never under another checkout of the list (it is judged on its own)."""
    days = by_level(cfg, "build_idle_days", level, 14)
    roots = checkouts()
    wanted = set(names)
    out = []
    for root in roots:
        if budget and budget.out("build-output"):
            break
        idle = checkout_idle_days(root, at)
        if idle is None or idle < days or holders.busy(root):
            continue
        others = [r for r in roots if r != root and r.startswith(root + "/")]
        for dirpath, dirnames, _ in os.walk(root):
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
                    ignored = safe_git.run(["check-ignore", "-q", full], repo=dirpath,
                                           timeout=30).returncode == 0
                    if ignored and not holders.busy(full):
                        _, size, _ = newest_and_size(full, limit=500_000)
                        out.append({"class": "build-output", "target": full, "bytes": size,
                                    "why": f"git-ignored {d}; checkout idle {idle:.0f} days"})
                    continue
                if depth < 5:
                    keep.append(d)
            dirnames[:] = keep
    return out


# --- reports that act on nothing -------------------------------------------------

def large_files(cfg: dict, previous: dict | None, force: bool) -> dict | None:
    """The largest files under the home directory, daily, within a time budget.

    Reported, never removed: a big file is somebody's until they say otherwise. The
    walk skips privacy-guarded places, symlinks and other file systems, and stops at
    its budget, saying so."""
    every = float(cfg.get("large_files_every_hours", 24)) * 3600
    last = (previous or {}).get("large_files") or {}
    try:
        age = time.time() - datetime.strptime(last.get("measured_at", ""), "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        age = float("inf")
    if not force and age < every:
        return last or None
    from scan_machine import protected_place
    home = pathlib.Path.home()
    floor = float(cfg.get("large_file_min_mb", 1024)) * 1024 * 1024
    budget = float(cfg.get("large_files_budget_seconds", 120))
    skip = set(cfg.get("large_files_skip") or [])
    top = int(cfg.get("large_files_top", 30))
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
    try:
        machine = json.loads((paths.SCRATCH / "machine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
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


# --- surveys the guard keeps fresh ------------------------------------------------

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
        if a.get("needs") and not running(a["needs"]):
            return {**rec, "result": "skipped", "reason": "its daemon is not running; the guard starts nothing"}
        if a.get("busy_if") and running(a["busy_if"]):
            return {**rec, "result": "skipped", "reason": "the tool is running now"}
        try:
            # SHORT, and a wait is not a fault (2026-10-11): `uv cache prune` waits on the lock a
            # long-running `uv tool run` server holds, and one such wait spent five minutes of a
            # ten-minute run while sixteen thousand removals waited behind it.
            p = subprocess.run(a["argv"], capture_output=True, text=True,
                               timeout=float(a.get("timeout") or 90), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {**rec, "result": "skipped", "reason": "held up past its time — something else holds the tool's cache"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {**rec, "result": "failed", "reason": f"{type(exc).__name__}"}
        said = (p.stderr or p.stdout or "").strip()
        if p.returncode and a.get("busy_output") and a["busy_output"] in said:
            # The tool refused because something uses its cache now (measured 2026-10-11:
            # a long-running `uv tool run` server holds uv's lock). That is "busy", not a
            # fault: a failure here would read as a broken guard every hour.
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


def journal(rows: list[dict]) -> None:
    if not rows:
        return
    log = paths.STATE / "logs" / "cleanup.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.chmod(log, 0o600)


def planners(cfg: dict, level: str, holders: Holders, at: float, degraded: list) -> list[tuple[str, object]]:
    """Each class's planner, in the order a run takes them.

    PATHS FIRST, THE TOOLS' COMMANDS LAST (2026-10-11). With the commands first, `uv cache
    prune` waiting on a lock spent half a run, and the eight commands together spent the rest
    while sixteen thousand removals that take a second each waited behind them. Within a class
    the largest go first."""
    big = lambda xs: sorted(xs, key=lambda a: -(a.get("bytes") or 0))  # noqa: E731
    return [
        ("cache-dir", lambda b: big(plan_cache_dirs(cfg, level, holders, at, b))),
        ("build-output", lambda b: big(plan_builds(cfg, level, holders, at, artifact_names(), b))),
        ("temp", lambda b: big(plan_temp(cfg, level, holders, at, b))),
        ("trash", lambda b: big(plan_trash(cfg, level, at, degraded))),
        ("browser-clone", lambda b: plan_clones(cfg, holders, at)),
        ("cache-command", lambda b: plan_commands(cfg, level)),
    ]


def build_plan(cfg: dict, level: str, holders: Holders, at: float, degraded: list) -> list[dict]:
    """Every action at this pressure, in the order a run takes them (plan only)."""
    budget = Budget(float(cfg.get("plan_budget_seconds", 300)), degraded)
    out: list[dict] = []
    for _name, planner in planners(cfg, level, holders, at, degraded):
        out += planner(budget)
    return out


def run(argv: list[str]) -> dict:
    """One guard run, inside its own time budget.

    WHY A RUN BUDGET (2026-10-11). The first scheduled pass on 0.23.0 killed the guard at
    the maintenance job's 1200 s ceiling: under launchd's background priority the survey
    refreshes, the plan and two thousand removals did not fit, and because the report and
    the journal were written at the end, the pass recorded only "exit 124". Now the guard
    stops itself at `--budget` (the job passes its ceiling minus a margin) or
    `run_budget_seconds`, acts BEFORE it refreshes any survey (a slightly old survey plans
    as well), journals every action as it happens, and always writes its report."""
    cfg = load_config()
    started = time.monotonic()
    budget = float(cfg.get("run_budget_seconds", 900))
    if "--budget" in argv:
        try:
            budget = float(argv[argv.index("--budget") + 1])
        except (IndexError, ValueError):
            pass
    left = lambda: budget - (time.monotonic() - started)  # noqa: E731
    out_file = paths.SCRATCH / "disk-guard.json"
    try:
        previous = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    auto, apply = "--auto" in argv, "--apply" in argv
    enabled = configuration.enabled("auto_cleanup", "features")
    acting = apply or (auto and enabled)
    degraded: list[dict] = []
    surveys: dict = {}
    phases: dict = {}
    plan: list[dict] = []
    rows: list[dict] = []
    found = (previous or {}).get("large_files")
    free0 = free_bytes()
    level = next((a.split("=", 1)[1] for a in argv if a.startswith("--level=")), None)
    if "--level" in argv:
        level = argv[argv.index("--level") + 1]
    level = level if level in LEVELS else level_for(free0, cfg)
    # HOW OFTEN. The maintenance pass runs every 15 minutes; at `normal` pressure the guard
    # acts once per `normal_every_minutes` (60), and below the `low` line on EVERY pass —
    # on 2026-10-11 the disk went from 6 GB to 0.3 GB inside one hour while the guard waited.
    if auto and level == "normal" and previous:
        try:
            since = time.time() - datetime.strptime(previous.get("at", ""), "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            since = float("inf")
        if since < float(cfg.get("normal_every_minutes", 60)) * 60 and previous.get("acting"):
            return {**previous, "not_due": True, "results": {"removed": 0, "skipped": 0, "failed": 0},
                    "seconds": 0, "acting": False}
    try:
        holders = Holders.take()
        if not holders.known:
            degraded.append({"source": "lsof", "reason": "open files could not be listed; no path is removed"})
        at = time.time()
        phases["lsof"] = round(time.monotonic() - started)
        mark = time.monotonic()
        reserve = float(cfg.get("run_reserve_seconds", 60))
        plan_cap = float(cfg.get("plan_budget_seconds", 300))
        cap = int(cfg.get("max_actions_per_class", 20000))
        stopped = False
        # ONE CLASS AT A TIME: plan it (at most half of what is left), act on it, then the next.
        # Planning everything before acting let a slow plan and slow commands spend a whole
        # run with nothing removed (2026-10-11).
        for name, planner in planners(cfg, level, holders, at, degraded):
            if stopped or left() < reserve:
                if acting and not stopped:
                    degraded.append({"source": "act", "reason": f"stopped at the run budget before {name}; "
                                                                f"the rest is looked at next pass"})
                stopped = True
                continue
            actions = planner(Budget(max(0.0, min(plan_cap, (left() - reserve) / 2)), degraded))
            plan += actions
            if not acting:
                continue
            for n, a in enumerate(actions[:cap]):
                if left() < reserve:
                    degraded.append({"source": "act", "reason": f"stopped at the run budget in {name}; "
                                                                f"{len(actions) - n} action(s) are looked at again next pass"})
                    stopped = True
                    break
                try:
                    row = act(a, holders)
                except (OSError, subprocess.SubprocessError) as exc:
                    row = {"at": now(), "class": a["class"], "target": a["target"], "mode": "guard",
                           "result": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]}
                rows.append(row)
                journal([row])               # as it happens: a run stopped from outside keeps its record
        phases["plan+act"] = round(time.monotonic() - mark)
        mark = time.monotonic()
        if acting:
            if left() > 2 * reserve:
                # The git tier: merged/pushed branches, clean idle worktrees — the cleanup's own
                # rules and re-checks, journalled by it.
                try:
                    p = subprocess.run([sys.executable, str(ROOT / "tools" / "cleanup.py"),
                                        "--apply" if apply else "--auto", "--level", level],
                                       capture_output=True, text=True,
                                       timeout=max(30, min(900, left() - reserve)), cwd=str(ROOT))
                    surveys["git-cleanup"] = (p.stdout.strip().splitlines() or [f"exit {p.returncode}"])[-1][:200]
                except (OSError, subprocess.SubprocessError) as exc:
                    surveys["git-cleanup"] = f"failed: {type(exc).__name__}"
            else:
                surveys["git-cleanup"] = "skipped: the run budget is spent"
        if acting or "--refresh" in argv:
            # Surveys LAST: the git survey feeds the next plan's checkout list, the machine survey
            # the Machine page and the memory report. Each gets only the time that is left.
            for name, script, hours, cap_s in (
                    ("git-hygiene", "collectors/scan_git_hygiene.py", cfg.get("git_survey_every_hours", 6), 600),
                    ("machine", "collectors/scan_machine.py", cfg.get("machine_survey_every_hours", 2), 300)):
                room = left() - reserve / 2
                surveys[name] = (refresh(script, paths.SCRATCH / f"{name}.json", float(hours), int(min(cap_s, room)))
                                 if room > 30 else "skipped: the run budget is spent")
        mark = time.monotonic()
        room = left() - reserve / 2
        if room > 30 or "--large-files" in argv:
            found = large_files({**cfg, "large_files_budget_seconds":
                                 min(float(cfg.get("large_files_budget_seconds", 120)), max(room, 30))},
                                previous, "--large-files" in argv)
        phases["large-files"] = round(time.monotonic() - mark)
    finally:
        free1 = free_bytes()
        doc = {"schema_version": 1, "at": now(), "level": level, "acting": acting, "auto_enabled": enabled,
               "free_gb_before": round(free0 / GB, 1), "free_gb_after": round(free1 / GB, 1),
               "freed_gb": round((free1 - free0) / GB, 2), "seconds": round(time.monotonic() - started),
               "budget_seconds": round(budget), "phases": phases,
               "planned": {cls: sum(1 for a in plan if a["class"] == cls) for cls in
                           ("browser-clone", "temp", "trash", "cache-command", "cache-dir", "build-output")},
               "planned_gb": round(sum(a.get("bytes") or 0 for a in plan) / GB, 1),
               "results": {r: sum(1 for x in rows if x["result"] == r) for r in ("removed", "skipped", "failed")},
               "actions": [{k: a[k] for k in ("class", "target", "why") if k in a}
                           | {"gb": round((a.get("bytes") or 0) / GB, 2)} for a in plan[:200]],
               "surveys": surveys, "large_files": found,
               "memory": memory_report(), "degraded": degraded}
        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out_file)
    return doc


def main(argv: list[str]) -> int:
    doc = run(argv)
    if "--json" in argv:
        # The maintenance job reads this line instead of the report file: the file's
        # directory can be moved (OBSERVATORY_SCRATCH), the process's own answer cannot.
        print(json.dumps({k: doc[k] for k in ("level", "acting", "free_gb_before", "free_gb_after", "freed_gb",
                                              "planned", "results", "seconds")}))
        return 1 if doc["results"]["failed"] else 0
    p = doc["planned"]
    planned = ", ".join(f"{k} {v}" for k, v in p.items() if v) or "nothing"
    if not doc["acting"]:
        why = " (features.auto_cleanup is off)" if "--auto" in argv else ""
        print(f"disk guard: {doc['free_gb_before']} GB free, pressure {doc['level']}; plan only{why} — "
              f"{planned}. `--apply` acts now.")
        return 0
    r = doc["results"]
    print(f"disk guard: pressure {doc['level']}; {r['removed']} done, {r['skipped']} skipped on re-check, "
          f"{r['failed']} failed; free {doc['free_gb_before']} → {doc['free_gb_after']} GB")
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
