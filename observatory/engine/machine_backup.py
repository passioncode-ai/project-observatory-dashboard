"""The machine's agent setup, backed up beside the workspace's copies, and put back on a new machine.

WHY (OBS-157). The engine backed up only its own workspace: daily encrypted snapshots, the
database copies, a snapshot before each update. Everything around it that makes a machine
an agent machine was on this disk alone: the Claude Code and Codex configuration, SSH and
Git settings, the shell profile, the CLI logins, the launchd jobs, the Fabric service
descriptors, the packages installed, and every checkout's unpushed commits and uncommitted
work. Moving to another machine meant reconstructing it by hand.

WHAT A BUNDLE IS. One file, `machine-<UTC stamp>-<id>.obmachine`, in the SAME backups root as
the workspace snapshots and in the same `OBSENC1` format (backup_vault.py): AES-256-GCM, the
same passphrase. The tar inside is streamed straight into the encrypting writer, so no
plaintext copy of what it carries is ever written next to the root, and without a passphrase
nothing is written at all. Inside:

    files/<path under the home>     copies of the files the manifest names
    abs/<absolute path>             an operator's extra path outside the home
    inventories/                    Homebrew's Brewfile, global npm, pipx and uv tools,
                                    the checkouts and the engine's own workspace pointer
    checkouts/<id>/                 per checkout: a git bundle of the commits no remote has,
                                    a patch of uncommitted changes, untracked files
    MANIFEST.json                   every item (source, size, sha256, kind, mode), every
                                    item skipped with its reason, every item NOT CARRIED

A symbolic link is recorded as a link with its target and never followed into another tree.
Anything over a cap (a file, an entry, the bundle, a checkout's bundle or patch) is NOT
CARRIED and named: in MANIFEST.json, in the command's answer, in `doctor` and on Health.

WHAT IT NEVER HOLDS. Transcripts (`~/.claude/projects`, opt-in with `transcripts: true`),
plugin and package caches, the engine's own workspace (the daily snapshot carries it),
and ignored files in checkouts. A value never reaches the manifest's names, the state file,
a log or an answer: those carry paths, sizes, digests and reasons only.

THE OPERATOR'S OWN PATHS live in `<home>/config/machine-backup.json`, private to the machine
and never in this repository:

    {"include": ["~/.config/some-agent", "/opt/agent/etc/agent.toml"],
     "exclude": ["~/.ssh/*.old", "*.log"],
     "roots": ["~/src"],                 # where checkouts are looked for
     "transcripts": false, "enabled": true,
     "limits": {"total_mb": 160, "min_free_gb": 2}}

WHEN. Once a day from the maintenance job (`full maintain`), before every engine update
unless a bundle younger than an hour exists, and on `full machine-backup`. A write is refused
when the root's disk would keep less than `min_free_gb` free; the refusal is recorded and
shown, never silent.

RESTORE. `full machine-restore [BUNDLE | --latest]` prints the plan: every item, where it
goes, whether it is missing, the same, or in conflict. `--apply` writes only what is missing;
`--overwrite` replaces a conflict after moving the existing file aside; `--packages` installs
the inventories; `--clone` clones the recorded checkouts and applies their carried commits,
patches and untracked files. Nothing is printed but paths, counts and reasons.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import backup_vault
import configuration as config
import oslocks

KIND, SUFFIX = backup_vault.MACHINE_KIND, backup_vault.MACHINE_SUFFIX
FORMAT = "observatory-machine/1"
#: Measured on a working agent machine (2026-10-19): one bundle of the built-in manifest, the
#: inventories and every checkout's unpushed and uncommitted work is a few tens of MB. Three
#: are a few days of history at a cost well under the free space the disk guard keeps.
KEEP = 3
EVERY = datetime.timedelta(hours=24)
RETRY = datetime.timedelta(hours=1)
#: `doctor` and Health warn past this age: a daily job that missed one day is late, not lost.
STALE = datetime.timedelta(hours=36)
#: Before an update a bundle is taken unless one this young exists (the daily one usually is).
FRESH = datetime.timedelta(hours=1)
CONFIG_NAME = "machine-backup.json"
STATE_NAME = "machine-backup.json"
#: Every limit, and the key that changes it in machine-backup.json → `limits`.
LIMITS = {
    "file_mb": 16,          # one file
    "entry_mb": 32,         # one manifest entry (a directory such as ~/.claude/skills)
    "total_mb": 160,        # every file copy together; the target is a bundle well under 200 MB
    "bundle_mb": 16,        # one checkout's git bundle of unpushed commits
    "patch_mb": 8,          # one checkout's patch of uncommitted changes
    "untracked_mb": 8,      # one checkout's untracked files together
    "checkouts_mb": 64,     # everything carried for checkouts together
    "min_free_gb": 2,       # free space the root's disk keeps after the write
    "depth": 4,             # how deep a root is searched for checkouts
    "checkout_seconds": 900,  # the whole checkout inventory; past it the rest is named, not read
}
#: Directories never searched for checkouts: dependency and build trees hold no checkout
#: of the operator's and cost most of the walk.
PRUNE = frozenset({"node_modules", ".venv", "venv", ".tox", "build", "dist", "vendor", "Pods", ".build",
                   "DerivedData", "__pycache__", ".next", "target", ".cache", ".gradle", ".terraform"})
#: Never copied, wherever they are.
NEVER = ("*/.DS_Store", ".DS_Store")
GIT_TIMEOUT = 60
TOOL_TIMEOUT = 180
NOT_CARRIED_SHOWN = 20


class MachineBackupError(config.ConfigurationError):
    """A machine bundle could not be taken, read or restored. Never carries a value."""


class DiskRefused(MachineBackupError):
    """The root's disk would keep less free space than `min_free_gb`."""


# --- what is carried ---------------------------------------------------------------

class Entry:
    """One place the manifest names: a file or a directory, under a category."""

    def __init__(self, category: str, path: Path, *, pattern: str | None = None, recurse: bool = True,
                 exclude: tuple[str, ...] = (), skip: str | None = None):
        self.category, self.path, self.pattern = category, path, pattern
        self.recurse, self.exclude, self.skip = recurse, exclude, skip


def _fabric_services_dir(home: Path) -> Path:
    """Where Fabric keeps its service descriptors (fabric_service.services_dir), for this home."""
    override = os.environ.get("FABRIC_SERVICES_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return home / "Library/Application Support/ai.passioncode.fabric/services"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData/Local") / "passioncode-fabric" / "services"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local/share") / "passioncode-fabric/services"


def builtin_entries(home: Path, *, transcripts: bool = False) -> list[Entry]:
    """The well-known locations of the tools an agent machine runs. Generic names only: an
    operator's own agents are added in machine-backup.json, never here."""
    h = home
    e = [
        Entry("claude", h / ".claude/settings.json"),
        Entry("claude", h / ".claude/settings.local.json"),
        Entry("claude", h / ".claude/CLAUDE.md"),
        Entry("claude", h / ".claude/keybindings.json"),
        Entry("claude", h / ".claude/agents"),
        Entry("claude", h / ".claude/commands"),
        Entry("claude", h / ".claude/hooks"),
        Entry("claude", h / ".claude/skills", exclude=(".trash",)),
        Entry("claude", h / ".claude/plugins/installed_plugins.json"),
        Entry("claude", h / ".claude/plugins/known_marketplaces.json"),
        Entry("claude", h / ".claude.json"),
        Entry("claude", h / ".claude/projects", skip=None if transcripts else
              "transcripts are opt-in: `\"transcripts\": true` in machine-backup.json"),
        Entry("claude", h / ".claude/plugins/cache", skip="a plugin cache: reinstalled from the plugin registry"),
        Entry("claude", h / ".claude/plugins/marketplaces", skip="marketplace clones: fetched again from known_marketplaces.json"),
        Entry("agents", h / ".agents/skills"),
        Entry("codex", h / ".codex/config.toml"),
        Entry("codex", h / ".codex/AGENTS.md"),
        Entry("git", h / ".gitconfig"),
        Entry("git", h / ".gitignore_global"),
        Entry("git", h / ".config/git"),
        Entry("ssh", h / ".ssh", exclude=("*.sock", "control-*", "cm-*")),
        Entry("shell", h / ".zshrc"), Entry("shell", h / ".zprofile"), Entry("shell", h / ".zshenv"),
        Entry("shell", h / ".bashrc"), Entry("shell", h / ".bash_profile"), Entry("shell", h / ".profile"),
        Entry("cli", h / ".config/gh"),
        Entry("cli", h / ".config/doctl"),
        Entry("cli", h / ".config/heroku"),
        Entry("cli", h / ".netrc"),
        Entry("fabric", _fabric_services_dir(h)),
    ]
    if sys.platform == "darwin":
        e += [Entry("cli", h / "Library/Application Support/doctl"),
              Entry("launchd", h / "Library/LaunchAgents", pattern="*.plist", recurse=False)]
    elif sys.platform != "win32":
        e += [Entry("systemd", h / ".config/systemd/user", recurse=False)]
    return e


# --- settings ------------------------------------------------------------------------

def settings_path(base: Path) -> Path:
    return base / "config" / CONFIG_NAME


def load_settings(base: Path) -> dict:
    """machine-backup.json, validated. Absent is every default; a wrong value is refused
    by name rather than ignored, so a typo never silently drops a path."""
    file = settings_path(base)
    out = {"enabled": True, "include": [], "exclude": [], "roots": None, "transcripts": False,
           "limits": dict(LIMITS)}
    if not file.exists() and not file.is_symlink():
        return out
    doc = config.read_json(file)
    known = {"enabled", "include", "exclude", "roots", "transcripts", "limits"}
    unknown = sorted(set(doc) - known)
    if unknown:
        raise MachineBackupError(f"{file.name}: unknown setting {unknown[0]}; known: {', '.join(sorted(known))}")
    for key in ("enabled", "transcripts"):
        if key in doc:
            if type(doc[key]) is not bool:
                raise MachineBackupError(f"{file.name}: {key} must be true or false")
            out[key] = doc[key]
    for key in ("include", "exclude", "roots"):
        if key in doc:
            value = doc[key]
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise MachineBackupError(f"{file.name}: {key} must be a list of strings")
            if key != "exclude":
                for v in value:
                    if not Path(v).expanduser().is_absolute():
                        raise MachineBackupError(f"{file.name}: {key} needs absolute paths or ~/… ({v!r} is neither)")
            out[key] = list(value)
    if "limits" in doc:
        if not isinstance(doc["limits"], dict):
            raise MachineBackupError(f"{file.name}: limits must be an object")
        for key, value in doc["limits"].items():
            if key not in LIMITS:
                raise MachineBackupError(f"{file.name}: unknown limit {key}; known: {', '.join(LIMITS)}")
            if type(value) not in (int, float) or value < 0:
                raise MachineBackupError(f"{file.name}: limit {key} must be a number of at least 0")
            out["limits"][key] = value
    return out


def enabled(base: Path) -> bool:
    try:
        return load_settings(base)["enabled"]
    except config.ConfigurationError:
        return True   # a broken file is reported by `take`, never a reason to stop silently


# --- paths -----------------------------------------------------------------------------

def display(path: Path, home: Path) -> str:
    """`~/rel` for a path under the home, the absolute path otherwise: what a person reads,
    and what a restore on another account maps back onto ITS home."""
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not rel.parts else "~/" + rel.as_posix()


def arcname(path: Path, home: Path) -> str:
    try:
        return "files/" + path.relative_to(home).as_posix()
    except ValueError:
        return "abs/" + path.as_posix().lstrip("/")


def _excluded(path: Path, home: Path, patterns: tuple[str, ...]) -> bool:
    shown = display(path, home)
    return any(fnmatch.fnmatch(shown, p) or ("/" not in p and fnmatch.fnmatch(path.name, p))
               for p in (*NEVER, *patterns))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- collecting --------------------------------------------------------------------------

class Collection:
    """What the bundle will hold, with the reasons for what it will not."""

    def __init__(self, home: Path, limits: dict):
        self.home, self.limits = home, limits
        self.files: list[dict] = []        # {"path": Path, "source", "arcname", "size", "mode", "category"}
        self.links: list[dict] = []
        self.dirs: list[dict] = []
        self.skipped: list[dict] = []      # by design: excluded, opt-in, not a regular file
        self.not_carried: list[dict] = []  # wanted but not in the bundle: a cap, unreadable, vanished
        self.absent: list[str] = []
        self.total = 0
        self.entry_total = 0
        self.entry_root: Path | None = None
        self.groups: dict[tuple[str, str], dict] = {}
        self.seen: set[str] = set()

    def mb(self, key: str) -> int:
        return int(self.limits[key] * 1024 * 1024)

    def skip(self, path: Path, reason: str) -> None:
        self.skipped.append({"source": display(path, self.home), "reason": reason})

    def miss(self, source: str, reason: str, size: int | None = None) -> None:
        self.not_carried.append({"source": source, "reason": reason, **({"size": size} if size is not None else {})})

    def miss_capped(self, path: Path, reason: str, size: int) -> None:
        """A file a cap left out, named by the entry's top-level child it belongs to: one
        directory of 2,000 files over the cap is one line with a count, not 2,000 lines
        (measured on a real machine: 5,888 lines for two skill folders)."""
        group = path
        if self.entry_root is not None and self.entry_root in path.parents:
            group = self.entry_root / path.relative_to(self.entry_root).parts[0]
        key = (display(group, self.home), reason)
        record = self.groups.get(key)
        if record is None:
            record = {"source": key[0], "reason": reason, "size": 0, "files": 0}
            self.groups[key] = record
            self.not_carried.append(record)
        record["size"] += size
        record["files"] += 1

    def link(self, path: Path, category: str) -> None:
        shown = display(path, self.home)
        if shown in self.seen:
            return
        self.seen.add(shown)
        try:
            target = os.readlink(path)
        except OSError as exc:
            self.miss(shown, f"unreadable link: {type(exc).__name__}")
            return
        # A target under this home is recorded home-relative, so a restore on another
        # account points the link into ITS home.
        if os.path.isabs(target):
            target = display(Path(target), self.home)
        self.links.append({"kind": "link", "source": shown, "target": target, "category": category})

    def directory(self, path: Path, category: str) -> None:
        shown = display(path, self.home)
        if shown in self.seen:
            return
        self.seen.add(shown)
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError:
            mode = 0o700
        self.dirs.append({"kind": "dir", "source": shown, "mode": mode, "category": category})

    def file(self, path: Path, category: str) -> None:
        shown = display(path, self.home)
        if shown in self.seen:
            return
        self.seen.add(shown)
        try:
            info = path.lstat()
        except OSError as exc:
            self.miss(shown, f"unreadable: {type(exc).__name__}")
            return
        size = info.st_size
        if size > self.mb("file_mb"):
            self.miss_capped(path, f"over the per-file cap of {self.limits['file_mb']} MB (limits.file_mb)", size)
            return
        if self.entry_total + size > self.mb("entry_mb"):
            self.miss_capped(path, f"over the per-entry cap of {self.limits['entry_mb']} MB (limits.entry_mb)", size)
            return
        if self.total + size > self.mb("total_mb"):
            self.miss_capped(path, f"over the bundle's cap of {self.limits['total_mb']} MB (limits.total_mb)", size)
            return
        self.entry_total += size
        self.total += size
        self.files.append({"path": path, "source": shown, "arcname": arcname(path, self.home), "size": size,
                           "mode": stat.S_IMODE(info.st_mode), "category": category})

    def walk(self, entry: Entry, excludes: tuple[str, ...]) -> None:
        root, category = entry.path, entry.category
        self.entry_total, self.entry_root = 0, root
        if not os.path.lexists(root):
            self.absent.append(display(root, self.home))
            return
        if entry.skip:
            self.skip(root, entry.skip)
            return
        patterns = (*excludes, *(f"{display(root, self.home)}/{p}" for p in entry.exclude),
                    *(p for p in entry.exclude if "/" not in p))
        if _excluded(root, self.home, excludes):
            self.skip(root, "excluded by machine-backup.json")
            return
        if root.is_symlink():
            self.link(root, category)
            return
        if root.is_file():
            self.file(root, category)
            return
        if not root.is_dir():
            self.skip(root, "not a regular file, directory or link")
            return
        self.directory(root, category)
        try:
            for current, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: self.miss(
                    display(Path(e.filename or root), self.home), f"unreadable: {type(e).__name__}")):
                here = Path(current)
                keep = []
                for name in sorted(dirs):
                    path = here / name
                    if _excluded(path, self.home, patterns):
                        self.skip(path, "excluded")
                    elif path.is_symlink():
                        self.link(path, category)
                    elif entry.recurse:
                        self.directory(path, category)
                        keep.append(name)
                dirs[:] = keep
                for name in sorted(files):
                    path = here / name
                    if entry.pattern and not fnmatch.fnmatch(name, entry.pattern):
                        continue
                    if _excluded(path, self.home, patterns):
                        self.skip(path, "excluded")
                    elif path.is_symlink():
                        self.link(path, category)
                    elif path.is_file():
                        self.file(path, category)
                    else:
                        self.skip(path, "not a regular file")
        except OSError as exc:
            self.miss(display(root, self.home), f"unreadable: {type(exc).__name__}")


# --- inventories --------------------------------------------------------------------------

def tool_path(home: Path) -> str:
    """PATH for the inventory tools: this process's, then the usual package-manager places a
    launchd job's minimal PATH lacks."""
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for extra in ("/opt/homebrew/bin", "/usr/local/bin", str(home / ".local/bin"), str(home / ".cargo/bin")):
        if extra not in parts and Path(extra).is_dir():
            parts.append(extra)
    return os.pathsep.join(parts)


def _tool_env(home: Path, path: str) -> dict:
    return {"HOME": str(home), "PATH": path, "LANG": "en_US.UTF-8", "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_ANALYTICS": "1", "HOMEBREW_NO_ENV_HINTS": "1", "NO_COLOR": "1",
            **{k: os.environ[k] for k in ("TMPDIR", "USER", "LOGNAME") if k in os.environ}}


#: (name, executable, arguments, file in the bundle). Each answers on stdout without the network.
INVENTORY_TOOLS = (
    ("brew", "brew", ["bundle", "dump", "--file=-", "--force"], "inventories/Brewfile"),
    ("npm", "npm", ["ls", "-g", "--depth=0", "--json"], "inventories/npm-global.json"),
    ("pipx", "pipx", ["list", "--json"], "inventories/pipx.json"),
    ("uv", "uv", ["tool", "list"], "inventories/uv-tools.txt"),
)


def inventories(home: Path, path: str, runner=subprocess.run) -> list[dict]:
    out = []
    env = _tool_env(home, path)
    for name, exe, args, arc in INVENTORY_TOOLS:
        found = shutil.which(exe, path=path)
        if not found:
            out.append({"name": name, "status": "absent", "detail": f"{exe} is not on PATH"})
            continue
        try:
            p = runner([found, *args], capture_output=True, timeout=TOOL_TIMEOUT, env=env,
                       stdin=subprocess.DEVNULL, cwd=str(home))
        except subprocess.TimeoutExpired:
            out.append({"name": name, "status": "failed", "detail": f"no answer in {TOOL_TIMEOUT} s"})
            continue
        except OSError as exc:
            out.append({"name": name, "status": "failed", "detail": type(exc).__name__})
            continue
        data = p.stdout or b""
        # npm exits 1 for an "extraneous" or invalid global package and still prints the list.
        if p.returncode != 0 and not (name == "npm" and data.strip().startswith(b"{")):
            tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            out.append({"name": name, "status": "failed", "detail": f"exit {p.returncode}: {tail[0][:160]}"})
            continue
        out.append({"name": name, "status": "ok", "arcname": arc, "data": data})
    return out


# --- checkouts ------------------------------------------------------------------------------

def default_roots(base: Path) -> list[Path]:
    """Where checkouts are looked for when machine-backup.json names no `roots`: the projects
    source the engine already reads, and every local folder declared to it."""
    roots = []
    try:
        projects = (config.load(base).get("sources") or {}).get("projects")
        if projects:
            roots.append(Path(projects).expanduser())
    except config.ConfigurationError:
        pass
    try:
        import tracked
        roots += [Path(row["path"]) for row in tracked.listing().get("local_folders", []) if row.get("path")]
    except Exception:  # noqa: BLE001: declared folders are extra roots, never a reason to fail
        pass
    return roots


def find_checkouts(roots: list[Path], depth: int, deadline: float) -> tuple[list[Path], list[str]]:
    """Every directory holding `.git` (a repository or a worktree) under the roots, to `depth`
    levels; links are not followed and dependency trees are not searched."""
    found, seen, notes = [], set(), []
    for root in roots:
        if not root.is_dir():
            notes.append(f"{root}: not a directory")
            continue
        start = len(root.parts)
        for current, dirs, _files in os.walk(root, followlinks=False):
            if time.monotonic() > deadline:
                notes.append(f"{root}: the search stopped at its time budget")
                break
            here = Path(current)
            if (here / ".git").exists() and here not in seen:
                seen.add(here)
                found.append(here)
            if len(here.parts) - start >= depth:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d != ".git" and d not in PRUNE and not (here / d).is_symlink())
    return found, notes


def redact_url(url: str) -> str:
    """A remote URL without its user information: `https://token@host/x` keeps no token."""
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@", r"\1", url.strip())


def _git(args: list[str], repo: Path, *, text: bool = True, timeout: float = GIT_TIMEOUT):
    import safe_git
    return safe_git.run(args, repo=repo, text=text, timeout=timeout)


def _status_v2(raw: bytes) -> dict:
    """`git status --porcelain=v2 --branch -z`: branch, upstream, ahead/behind, changes."""
    out = {"branch": None, "head": None, "upstream": None, "ahead": None, "behind": None,
           "changed": 0, "untracked": []}
    fields = raw.split(b"\x00")
    i = 0
    while i < len(fields):
        line = fields[i].decode("utf-8", "surrogateescape")
        i += 1
        if not line:
            continue
        if line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2]
            out["head"] = None if oid == "(initial)" else oid
        elif line.startswith("# branch.head "):
            head = line.split(" ", 2)[2]
            out["branch"] = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            _, _, a, b = line.split(" ")
            out["ahead"], out["behind"] = int(a.lstrip("+")), int(b.lstrip("-"))
        elif line.startswith("? "):
            out["untracked"].append(line[2:])
        elif line.startswith("2 "):
            out["changed"] += 1
            i += 1   # a rename carries its original path as the next field
        elif line[:2] in ("1 ", "u "):
            out["changed"] += 1
    return out


def inspect_checkout(path: Path, home: Path) -> dict:
    """What a checkout holds that no remote does. Reads only; runs no hook or filter (safe_git)."""
    rec = {"path": display(path, home), "worktree": (path / ".git").is_file()}
    p = _git(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"], path, text=False)
    if p.returncode != 0:
        lines = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        rec["error"] = lines[-1][:200] if lines else f"git status exit {p.returncode}"
        return rec
    rec.update(_status_v2(p.stdout))
    rec["dirty"] = bool(rec["changed"] or rec["untracked"])
    p = _git(["remote", "-v"], path)
    remotes = {}
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = redact_url(parts[1])
    rec["remotes"] = remotes
    p = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    rec["common_dir"] = (p.stdout or "").strip() if p.returncode == 0 else None
    if rec["head"]:
        p = _git(["rev-list", "--count", "--branches", "--not", "--remotes"], path)
        rec["unpushed"] = int((p.stdout or "0").strip() or 0) if p.returncode == 0 else None
    else:
        rec["unpushed"] = 0
    return rec


def carry_checkout(rec: dict, path: Path, home: Path, col: Collection, work: Path, cid: str,
                   bundled_dirs: set, budget: list) -> list[dict]:
    """The members this checkout adds: a bundle of unpushed commits (once per repository), a
    patch of uncommitted changes, untracked files. Over a cap, the part is named instead."""
    members, shown, limits = [], rec["path"], col.limits
    carried = rec.setdefault("carried", {})

    def fits(size: int, cap_key: str, what: str) -> bool:
        if size > col.mb(cap_key):
            col.miss(shown, f"{what}: over the cap of {limits[cap_key]} MB", size)
            return False
        if budget[0] + size > col.mb("checkouts_mb"):
            col.miss(shown, f"{what}: over the checkouts' cap of {limits['checkouts_mb']} MB", size)
            return False
        budget[0] += size
        return True

    # One bundle per repository: a worktree shares its main checkout's branches.
    repository = rec.get("common_dir") or str(path)
    if rec.get("unpushed") and repository not in bundled_dirs:
        bundled_dirs.add(repository)
        target = work / f"{cid}.bundle"
        p = _git(["bundle", "create", str(target), "--branches", "--not", "--remotes"], path, timeout=300)
        if p.returncode != 0 or not target.is_file():
            col.miss(shown, f"unpushed commits: git bundle failed ({(p.stderr or '').strip()[:120]})")
        else:
            size = target.stat().st_size
            if fits(size, "bundle_mb", f"{rec['unpushed']} unpushed commit(s)"):
                data = target.read_bytes()
                members.append({"arcname": f"checkouts/{cid}/unpushed.bundle", "data": data})
                carried["bundle"] = f"checkouts/{cid}/unpushed.bundle"
            target.unlink(missing_ok=True)
    if rec.get("changed") and rec.get("head"):
        p = _git(["diff", "--binary", "HEAD"], path, text=False, timeout=300)
        if p.returncode != 0:
            col.miss(shown, "uncommitted changes: git diff failed")
        elif p.stdout and fits(len(p.stdout), "patch_mb", "uncommitted changes"):
            members.append({"arcname": f"checkouts/{cid}/changes.patch", "data": p.stdout})
            carried["patch"] = f"checkouts/{cid}/changes.patch"
    if rec.get("untracked"):
        files, size = [], 0
        for rel in rec["untracked"]:
            f = path / rel
            if f.is_symlink() or not f.is_file() or ".." in Path(rel).parts:
                continue
            files.append((rel, f))
            size += f.stat().st_size
        if files and fits(size, "untracked_mb", f"{len(files)} untracked file(s)"):
            carried["untracked"] = []
            for rel, f in files:
                try:
                    data = f.read_bytes()
                except OSError as exc:
                    col.miss(f"{shown}/{rel}", f"unreadable: {type(exc).__name__}")
                    continue
                members.append({"arcname": f"checkouts/{cid}/untracked/{rel}", "data": data,
                                "mode": stat.S_IMODE(f.stat().st_mode)})
                carried["untracked"].append(rel)
    return members


# --- state -----------------------------------------------------------------------------------

def state_path(base: Path) -> Path:
    return base / "store" / STATE_NAME


def read_state(base: Path) -> dict:
    try:
        doc = json.loads(state_path(base).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(doc, out, indent=1, ensure_ascii=False)
            out.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _record(base: Path, record: dict) -> None:
    state = read_state(base)
    state["last"] = record
    if record.get("result") == "taken":
        state["success"] = record
    _write_json(state_path(base), state)


@contextlib.contextmanager
def _lock(base: Path):
    folder = base / "store"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    import osprivacy
    fd = osprivacy.open(folder / ".machine-backup.lock", os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
    try:
        try:
            oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        except BlockingIOError:
            raise MachineBackupError("another machine backup is running") from None
        yield
    finally:
        os.close(fd)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# --- taking one ---------------------------------------------------------------------------------

def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def workspace_pointer(base: Path, home: Path) -> dict:
    """The engine's own workspace: where it is, its version and its newest snapshot. The
    workspace itself travels in its daily snapshot, restored with `full restore`."""
    marker = {}
    try:
        marker = config.read_json(base / "workspace.json")
    except config.ConfigurationError:
        pass
    newest = None
    try:
        root = backup_vault.root_info(base)["path"]
        snaps = sorted((f for k in backup_vault.SNAPSHOT_KINDS
                        for f in backup_vault.artifacts(root, k, backup_vault.SNAPSHOT_SUFFIX)),
                       key=lambda f: backup_vault._artifact_stamp(f.name))
        newest = snaps[-1].name if snaps else None
    except (config.ConfigurationError, OSError):
        root = None
    return {"workspace": display(base, home), "engine_version": config.VERSION,
            "instance_id": marker.get("instance_id"), "backups_root": display(root, home) if root else None,
            "newest_snapshot": newest,
            "restore": "project-observatory full restore --latest (or full restore <snapshot>.obsnap)"}


def take(base: Path, *, trigger: str = "manual", home: Path | None = None, runner=subprocess.run,
         path: str | None = None, at: datetime.datetime | None = None) -> dict:
    """Take one machine bundle now. Raises MachineBackupError (DiskRefused for space) with
    the reason; the attempt is recorded either way, so `status` shows it."""
    at = at or datetime.datetime.now(datetime.timezone.utc)
    started = time.monotonic()
    try:
        with _lock(base):
            result = _take(base, trigger, home or Path.home(), runner, path, at, started)
    except DiskRefused as exc:
        _record(base, {"at": iso(at), "result": "refused-disk", "trigger": trigger, "detail": str(exc)[:300]})
        raise
    except MachineBackupError as exc:
        if "another machine backup is running" not in str(exc):
            _record(base, {"at": iso(at), "result": "failed", "trigger": trigger, "detail": str(exc)[:300]})
        raise
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        _record(base, {"at": iso(at), "result": "failed", "trigger": trigger,
                       "detail": f"{type(exc).__name__}: {str(exc)[:240]}"})
        raise MachineBackupError(f"the machine backup failed: {type(exc).__name__}: {exc}") from exc
    _record(base, result)
    return result


def _take(base: Path, trigger: str, home: Path, runner, path: str | None, at, started) -> dict:
    settings = load_settings(base)
    limits = settings["limits"]
    secret = backup_vault.passphrase(base)
    if not secret:
        raise MachineBackupError("no backup passphrase: a machine bundle holds ~/.ssh and CLI logins, so it is "
                                 "never written unencrypted; run `project-observatory full backup-passphrase set`")
    root = backup_vault.root_info(base)["path"]
    floor = int(limits["min_free_gb"] * 1024 ** 3)
    if free_bytes(root) < floor:
        raise DiskRefused(f"too little free space: {free_bytes(root) / 1024 ** 3:.1f} GB, under the "
                          f"{limits['min_free_gb']} GB a machine backup leaves (limits.min_free_gb in "
                          f"config/{CONFIG_NAME}); the disk holds {root}")
    col = Collection(home, limits)
    excludes = tuple(settings["exclude"])
    for entry in builtin_entries(home, transcripts=settings["transcripts"]):
        col.walk(entry, excludes)
    for extra in settings["include"]:
        col.walk(Entry("operator", Path(extra).expanduser()), excludes)
    path = path or tool_path(home)
    invs = inventories(home, path, runner)
    roots = [Path(r).expanduser() for r in settings["roots"]] if settings["roots"] is not None else default_roots(base)
    deadline = started + float(limits["checkout_seconds"])
    found, notes = find_checkouts(roots, int(limits["depth"]), deadline)
    work = Path(tempfile.mkdtemp(prefix=".machine-backup-", dir=base / "store"))  # paths-check: allow: removed in the finally below
    try:
        def inspect(p: Path) -> dict:
            if time.monotonic() > deadline:
                return {"path": display(p, home), "error": "not read: the checkout inventory's time budget ran out"}
            try:
                return inspect_checkout(p, home)
            except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                return {"path": display(p, home), "error": f"{type(exc).__name__}"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(inspect, found))
        members, bundled, budget = [], set(), [0]
        # A main checkout before its worktrees, so the repository's one bundle is the main
        # checkout's, which a restore clones first and adds the worktrees to.
        for p, rec in sorted(zip(found, records), key=lambda pair: bool(pair[1].get("worktree"))):
            rec["id"] = hashlib.sha256(rec["path"].encode()).hexdigest()[:12]
            if rec.get("error"):
                col.miss(rec["path"], f"checkout not read: {rec['error']}")
                continue
            members += carry_checkout(rec, p, home, col, work, rec["id"], bundled, budget)
        for rec in records:
            if "untracked" in rec:
                rec["untracked_count"] = len(rec.pop("untracked"))
        estimate = col.total + budget[0] + sum(len(i.get("data") or b"") for i in invs)
        if free_bytes(root) - estimate < floor:
            raise DiskRefused(f"too little free space: a bundle of up to {estimate / 1024 ** 2:.0f} MB would leave "
                              f"under the {limits['min_free_gb']} GB a machine backup keeps (limits.min_free_gb "
                              f"in config/{CONFIG_NAME}); the disk holds {root}")
        manifest = {"format": FORMAT, "created_at": iso(at), "trigger": trigger, "engine_version": config.VERSION,
                    "platform": sys.platform, "home_path": str(home), "items": [], "skipped": col.skipped,
                    "absent": col.absent, "not_carried": col.not_carried,
                    "inventories": [{k: v for k, v in i.items() if k != "data"} for i in invs],
                    "checkouts": records, "checkout_notes": notes}

        def fill(writer) -> None:
            with tarfile.open(fileobj=writer, mode="w|gz", format=tarfile.PAX_FORMAT) as tar:
                def add(name: str, data: bytes) -> None:
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(data), 0o600, int(at.timestamp())
                    tar.addfile(info, io.BytesIO(data))
                for item in col.files:
                    try:
                        data = item["path"].read_bytes()
                    except OSError as exc:
                        col.miss(item["source"], f"unreadable: {type(exc).__name__}", item["size"])
                        continue
                    if len(data) > col.mb("file_mb"):
                        col.miss(item["source"], "grew past the per-file cap while it was read", len(data))
                        continue
                    add(item["arcname"], data)
                    manifest["items"].append({"kind": "file", "source": item["source"], "arcname": item["arcname"],
                                              "size": len(data), "sha256": _sha256(data), "mode": item["mode"],
                                              "category": item["category"]})
                manifest["items"] += col.dirs + col.links
                for inv in invs:
                    if inv.get("data") is not None:
                        add(inv["arcname"], inv["data"])
                        manifest["items"].append({"kind": "inventory", "source": inv["name"], "arcname": inv["arcname"],
                                                  "size": len(inv["data"]), "sha256": _sha256(inv["data"])})
                for name, doc in (("inventories/checkouts.json", records),
                                  ("inventories/observatory.json", workspace_pointer(base, home))):
                    data = json.dumps(doc, indent=1, ensure_ascii=False).encode()
                    add(name, data)
                    manifest["items"].append({"kind": "inventory", "source": name.split("/")[-1].split(".")[0],
                                              "arcname": name, "size": len(data), "sha256": _sha256(data)})
                for m in members:
                    add(m["arcname"], m["data"])
                    manifest["items"].append({"kind": "checkout", "source": m["arcname"], "arcname": m["arcname"],
                                              "size": len(m["data"]), "sha256": _sha256(m["data"]),
                                              **({"mode": m["mode"]} if "mode" in m else {})})
                manifest["totals"] = {"items": len(manifest["items"]), "skipped": len(col.skipped),
                                      "not_carried": len(col.not_carried), "checkouts": len(records)}
                add("MANIFEST.json", json.dumps(manifest, indent=1, ensure_ascii=False).encode())

        backup_vault._mark_owner(root, base)
        dest = root / f"{KIND}-{at.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}{SUFFIX}"
        backup_vault.encrypt_stream(dest, fill, secret, kind=KIND, content="tar+gzip")
        dropped = backup_vault.rotate(root, KIND, SUFFIX, KEEP)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"at": iso(at), "result": "taken", "trigger": trigger, "file": dest.name, "root": str(root),
            "size": dest.stat().st_size, "items": manifest["totals"]["items"],
            "checkouts": len(records), "skipped": len(col.skipped),
            "not_carried_count": len(col.not_carried), "not_carried": col.not_carried[:NOT_CARRIED_SHOWN],
            "inventories": {i["name"]: i["status"] for i in invs}, "rotated": dropped,
            "seconds": round(time.monotonic() - started, 1)}


# --- the schedule ---------------------------------------------------------------------------------

def automatic_allowed() -> bool:
    """A scheduled or pre-update bundle runs only for this account's own home: a test or a
    sandbox with a temporary HOME, or OBSERVATORY_SYSTEM_SETUP=0, never backs up a machine."""
    if os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0":
        return False
    try:
        import osprivacy
        real = osprivacy.real_home()
        return real is not None and Path.home().resolve() == real.resolve()
    except (OSError, RuntimeError):
        return False


def newest(base: Path) -> Path | None:
    try:
        found = backup_vault.artifacts(backup_vault.root_info(base)["path"], KIND, SUFFIX)
    except config.ConfigurationError:
        return None
    return found[-1] if found else None


def _stamp_time(file: Path) -> datetime.datetime | None:
    stamp = backup_vault._artifact_stamp(file.name)
    try:
        return datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def step(base: Path, at: datetime.datetime, *, take_fn=None) -> dict:
    """The maintenance job's daily step: a bundle when the newest is a day old."""
    if not enabled(base):
        return {"result": "off", "detail": f"`enabled: false` in config/{CONFIG_NAME}"}
    if take_fn is None and not automatic_allowed():
        return {"result": "skipped", "detail": "not this account's own home (a test or a sandbox)"}
    last = newest(base)
    when = _stamp_time(last) if last else None
    if when and at - when < EVERY:
        return {"result": "not-due"}
    tried = read_state(base).get("last") or {}
    failed_at = _parse(tried.get("at")) if tried.get("result") in ("failed", "refused-disk") else None
    if failed_at and at - failed_at < RETRY:
        return {"result": "backing-off", "detail": f"the last attempt {tried.get('result')}; the next is after "
                                                   f"{iso(failed_at + RETRY)}"}
    try:
        out = (take_fn or take)(base, trigger="daily", at=at)
        return {k: out[k] for k in ("result", "file", "size", "items", "not_carried_count", "seconds") if k in out}
    except DiskRefused as exc:
        return {"result": "refused-disk", "detail": str(exc)[:300]}
    except MachineBackupError as exc:
        return {"result": "busy" if "is running" in str(exc) else "failed", "detail": str(exc)[:300]}


def before_update(base: Path, *, take_fn=None, at: datetime.datetime | None = None) -> dict:
    """Before an engine update: a bundle unless one younger than FRESH exists. Never raises:
    an update is not held back by the machine backup, it only says what happened."""
    at = at or datetime.datetime.now(datetime.timezone.utc)
    try:
        if not enabled(base):
            return {"result": "off"}
        if take_fn is None and not automatic_allowed():
            return {"result": "skipped", "detail": "not this account's own home (a test or a sandbox)"}
        last = newest(base)
        when = _stamp_time(last) if last else None
        if when and at - when < FRESH:
            return {"result": "fresh", "file": last.name}
        out = (take_fn or take)(base, trigger="before-update", at=at)
        return {k: out[k] for k in ("result", "file", "size", "not_carried_count") if k in out}
    except DiskRefused as exc:
        return {"result": "refused-disk", "detail": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001: an update never fails on its machine backup
        return {"result": "failed", "detail": f"{type(exc).__name__}: {str(exc)[:240]}"}


def status(base: Path, at: datetime.datetime | None = None) -> dict:
    """For doctor, `maintain status`, `backups status` and Health. Paths and counts only."""
    at = at or datetime.datetime.now(datetime.timezone.utc)
    state = read_state(base)
    last, success = state.get("last") or {}, state.get("success") or {}
    file = newest(base)
    when = _stamp_time(file) if file else None
    try:
        size = file.stat().st_size if file else None
    except OSError:   # rotated away between the listing and here
        size = None
    out = {"enabled": enabled(base), "newest": file.name if file else None,
           "newest_at": iso(when) if when else None,
           "age_hours": round((at - when).total_seconds() / 3600, 1) if when else None,
           "size": size, "keep": KEEP,
           "last_attempt": {k: last.get(k) for k in ("at", "result", "trigger", "detail") if k in last} or None,
           "items": success.get("items"), "not_carried_count": success.get("not_carried_count"),
           "not_carried": success.get("not_carried") or [], "settings": str(settings_path(base))}
    items = []

    def warn(code: str, text: str, **params) -> None:
        items.append({"code": code, "text": text, **params})

    try:
        load_settings(base)
    except config.ConfigurationError as exc:
        warn("machine-backup-settings", f"config/{CONFIG_NAME} is refused: {exc}", detail=str(exc)[:200])
    if not out["enabled"]:
        pass
    elif last.get("result") == "refused-disk" and (not when or (_parse(last.get("at")) or at) >= when):
        warn("machine-backup-refused-disk", f"the last machine backup was not written: {last.get('detail', '')[:200]}",
             detail=str(last.get("detail", ""))[:200])
    elif last.get("result") == "failed" and (not when or (_parse(last.get("at")) or at) >= when):
        warn("machine-backup-failed", f"the last machine backup failed: {last.get('detail', '')[:200]}; "
                                      "it is tried again within the hour", detail=str(last.get("detail", ""))[:200])
    if out["enabled"] and when is None:
        warn("machine-backup-missing", "no machine backup yet: the maintenance job takes one a day; "
                                       "`project-observatory full machine-backup` takes one now")
    elif out["enabled"] and at - when > STALE:
        warn("machine-backup-stale", f"the newest machine backup is {out['age_hours']:.0f} hours old",
             hours=out["age_hours"])
    if file and out["not_carried_count"]:
        names = ", ".join(i["source"] for i in out["not_carried"][:5])
        warn("machine-backup-not-carried", f"{out['not_carried_count']} item(s) were not carried in the newest "
                                           f"machine backup ({names}…); see `full machine-backup status`",
             count=out["not_carried_count"])
    out["warnings"] = [i["text"] for i in items]
    out["warning_items"] = items
    return out


# --- restoring --------------------------------------------------------------------------------

def find_latest(base: Path | None) -> Path | None:
    """The newest machine bundle in this workspace's root, else in any folder of the default
    roots (where a bundle copied from another machine is put)."""
    folders = []
    if base is not None:
        try:
            folders.append(backup_vault.root_info(base)["path"])
        except config.ConfigurationError:
            pass
    for root in ([Path(os.environ[backup_vault.ROOT_ENV]).expanduser()] if os.environ.get(backup_vault.ROOT_ENV) else []) \
            + ([backup_vault.documents_dir() / "Project Observatory" / "Backups"] if sys.platform == "darwin" else []) \
            + [backup_vault.data_dir() / "project-observatory-backups"]:
        if root.is_dir():
            folders += [f for f in root.iterdir() if f.is_dir() and not f.is_symlink()]
    found = [f for folder in folders for f in backup_vault.artifacts(folder, KIND, SUFFIX)]
    return max(found, key=lambda f: backup_vault._artifact_stamp(f.name)) if found else None


def _candidate_secrets(bundle: Path, base: Path | None):
    if os.environ.get(backup_vault.PASS_ENV):
        yield os.environ[backup_vault.PASS_ENV]
    if base is not None:
        try:
            value = backup_vault.passphrase(base)
        except backup_vault.BackupError:
            value = None
        if value:
            yield value
    try:
        yield from backup_vault.stored_passphrases(backup_vault.SecretStore(), bundle.parent.name)
    except (backup_vault.BackupError, OSError):
        pass
    if sys.stdin is not None and sys.stdin.isatty():
        import getpass
        value = getpass.getpass("Backup passphrase: ")
        if value:
            yield value


def secret_for(bundle: Path, base: Path | None) -> tuple[str, dict]:
    """The passphrase that opens `bundle`, and the bundle's header: the environment, this
    workspace's, the OS credential store under the bundle's folder label, then a terminal
    prompt. Each is tried by authenticating the whole file; the first that opens it wins."""
    seen = set()
    for value in _candidate_secrets(bundle, base):
        if value in seen:
            continue
        seen.add(value)
        try:
            return value, backup_vault.verify_file(bundle, value)
        except backup_vault.BackupError:
            continue
    raise MachineBackupError(f"no passphrase opens {bundle.name}: set {backup_vault.PASS_ENV}, or run this in a "
                             "terminal to be asked for it")


def _destination(source: str, home: Path) -> Path:
    if source == "~":
        return home
    if source.startswith("~/"):
        rel = Path(source[2:])
        if rel.is_absolute() or ".." in rel.parts:
            raise MachineBackupError(f"refusing an unsafe path in the manifest: {source}")
        return home / rel
    path = Path(source)
    if not path.is_absolute() or ".." in path.parts:
        raise MachineBackupError(f"refusing an unsafe path in the manifest: {source}")
    return path


def _link_target(target: str, home: Path) -> str:
    return str(home / target[2:]) if target.startswith("~/") else (str(home) if target == "~" else target)


def open_bundle(bundle: Path, secret: str, parent: Path) -> tuple[Path, dict]:
    """Authenticate, extract into a new private directory under `parent`, read MANIFEST.json."""
    stage = backup_vault.extract_tree(bundle, parent, secret)
    try:
        manifest = json.loads((stage / "MANIFEST.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise MachineBackupError(f"{bundle.name} is not a machine bundle this engine reads")
    except (OSError, ValueError):
        shutil.rmtree(stage, ignore_errors=True)
        raise MachineBackupError(f"{bundle.name} holds no readable MANIFEST.json") from None
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage, manifest


def plan(manifest: dict, stage: Path, home: Path) -> list[dict]:
    """Every file, directory and link: where it goes and whether it is missing, the same, or a conflict."""
    out = []
    for item in manifest.get("items", []):
        if item.get("kind") not in ("file", "dir", "link"):
            continue
        dest = _destination(item["source"], home)
        row = {"kind": item["kind"], "source": item["source"], "destination": display(dest, home)}
        if item["kind"] == "dir":
            row["state"] = "same" if dest.is_dir() and not dest.is_symlink() else \
                ("conflict" if os.path.lexists(dest) else "missing")
        elif item["kind"] == "link":
            target = _link_target(item["target"], home)
            if dest.is_symlink():
                row["state"] = "same" if os.readlink(dest) == target else "conflict"
            else:
                row["state"] = "conflict" if os.path.lexists(dest) else "missing"
        else:
            if dest.is_symlink() or (dest.exists() and not dest.is_file()):
                row["state"] = "conflict"
            elif dest.is_file():
                row["state"] = "same" if _sha256(dest.read_bytes()) == item["sha256"] else "conflict"
            else:
                row["state"] = "missing"
        out.append(row)
    return out


def _aside(dest: Path, stamp: str) -> str:
    moved = dest.with_name(f"{dest.name}.before-machine-restore-{stamp}")
    os.replace(dest, moved)
    return moved.name


def apply(manifest: dict, stage: Path, home: Path, *, overwrite: bool = False) -> dict:
    """Write what is missing; with `overwrite`, replace a conflict after moving it aside."""
    rows = plan(manifest, stage, home)
    by_source = {(i["kind"], i["source"]): i for i in manifest.get("items", [])}
    stamp = backup_vault.stamp()
    done = {"written": 0, "same": 0, "kept": 0, "replaced": 0, "failed": []}
    order = {"dir": 0, "file": 1, "link": 2}
    for row in sorted(rows, key=lambda r: (order[r["kind"]], r["source"])):
        item = by_source[(row["kind"], row["source"])]
        dest = _destination(item["source"], home)
        if row["state"] == "same":
            done["same"] += 1
            continue
        if row["state"] == "conflict" and not overwrite:
            done["kept"] += 1
            continue
        try:
            if row["state"] == "conflict":
                if row["kind"] == "dir" and dest.is_dir() and not dest.is_symlink():
                    done["same"] += 1
                    continue
                _aside(dest, stamp)
                done["replaced"] += 1
            if row["kind"] == "dir":
                dest.mkdir(parents=True, exist_ok=True)
                os.chmod(dest, item.get("mode", 0o700))
            elif row["kind"] == "link":
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(_link_target(item["target"], home), dest)
            else:
                source = stage / item["arcname"]
                data = source.read_bytes()
                if _sha256(data) != item["sha256"]:
                    raise MachineBackupError("the copy in the bundle does not match its digest")
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".restore")
                try:
                    os.fchmod(fd, item.get("mode", 0o600) & 0o7777)
                    with os.fdopen(fd, "wb") as out:
                        out.write(data)
                    os.replace(tmp, dest)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
            if row["state"] == "missing":
                done["written"] += 1
        except (OSError, MachineBackupError) as exc:
            done["failed"].append({"destination": row["destination"], "reason": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return done


def _run(argv: list[str], *, cwd: Path | None = None, runner=subprocess.run, timeout: int = 3600) -> dict:
    try:
        p = runner(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": " ".join(argv[:4]), "exit": 125, "detail": type(exc).__name__}
    tail = ((p.stderr or "").strip().splitlines() or [""])[-1][:200]
    return {"command": " ".join(argv[:4]), "exit": p.returncode, **({"detail": tail} if p.returncode else {})}


def packages(stage: Path, *, runner=subprocess.run, which=shutil.which) -> list[dict]:
    """Install what the inventories list, with each tool that is present."""
    inv = stage / "inventories"
    out = []
    brewfile = inv / "Brewfile"
    if brewfile.is_file():
        brew = which("brew")
        out.append(_run([brew, "bundle", "install", f"--file={brewfile}", "--no-upgrade"], runner=runner)
                   if brew else {"command": "brew bundle install", "exit": None, "detail": "brew is not installed: "
                                 "install Homebrew first (https://brew.sh), then run this again"})
    npm_file = inv / "npm-global.json"
    if npm_file.is_file() and which("npm"):
        try:
            deps = (json.loads(npm_file.read_text(encoding="utf-8")).get("dependencies") or {})
        except ValueError:
            deps = {}
        names = [f"{n}@{d.get('version')}" if isinstance(d, dict) and d.get("version") else n
                 for n, d in sorted(deps.items()) if n != "npm"]
        if names:
            out.append(_run([which("npm"), "install", "-g", *names], runner=runner))
    pipx_file = inv / "pipx.json"
    if pipx_file.is_file() and which("pipx"):
        try:
            venvs = json.loads(pipx_file.read_text(encoding="utf-8")).get("venvs") or {}
        except ValueError:
            venvs = {}
        for name in sorted(venvs):
            package = (((venvs[name] or {}).get("metadata") or {}).get("main_package") or {}).get("package") or name
            out.append(_run([which("pipx"), "install", package], runner=runner))
    uv_file = inv / "uv-tools.txt"
    if uv_file.is_file() and which("uv"):
        for line in uv_file.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("-", " ")):
                out.append(_run([which("uv"), "tool", "install", line.split()[0]], runner=runner))
    return out


def clone(manifest: dict, stage: Path, home: Path, *, runner=subprocess.run, git: str = "git") -> list[dict]:
    """Clone each recorded checkout that is not on this machine, then put back its carried
    unpushed commits, uncommitted changes and untracked files. Plain git with the person's own
    configuration: a clone needs their SSH keys and credential helpers."""
    out = []
    records = sorted(manifest.get("checkouts", []), key=lambda r: bool(r.get("worktree")))
    mains: dict[str, Path] = {}
    for rec in records:
        if rec.get("error"):
            continue
        dest = _destination(rec["path"], home)
        row = {"checkout": display(dest, home)}
        out.append(row)
        carried = rec.get("carried") or {}
        bundle = stage / carried["bundle"] if carried.get("bundle") else None
        if dest.exists():
            row["result"] = "exists"
            if rec.get("common_dir") and not rec.get("worktree"):
                mains[rec["common_dir"]] = dest
            continue
        remotes = rec.get("remotes") or {}
        url = remotes.get("origin") or next(iter(remotes.values()), None)
        if rec.get("worktree"):
            main = mains.get(rec.get("common_dir"))
            if main is None or not rec.get("branch"):
                row["result"] = "worktree-not-recreated"
                row["detail"] = "its main checkout is not on this machine, or it had no branch"
                continue
            step_ = _run([git, "-C", str(main), "worktree", "add", str(dest), rec["branch"]], runner=runner)
        elif url:
            step_ = _run([git, "clone", url, str(dest)], runner=runner)
        elif bundle is not None and bundle.is_file():
            step_ = _run([git, "clone", str(bundle), str(dest)], runner=runner)
        else:
            row["result"] = "nothing-to-clone-from"
            row["detail"] = "no remote, and no commits were carried"
            continue
        if step_["exit"] != 0:
            row.update(result="clone-failed", detail=step_.get("detail", ""))
            continue
        if rec.get("common_dir") and not rec.get("worktree"):
            mains[rec["common_dir"]] = dest
        for name, remote in remotes.items():
            if name != "origin" or not url:
                _run([git, "-C", str(dest), "remote", "add", name, remote], runner=runner)
        steps = []
        if bundle is not None and bundle.is_file() and not rec.get("worktree"):
            steps.append(_run([git, "-C", str(dest), "fetch", "--update-head-ok", str(bundle),
                               "+refs/heads/*:refs/heads/*"], runner=runner))
        if rec.get("branch") and not rec.get("worktree"):
            steps.append(_run([git, "-C", str(dest), "checkout", rec["branch"]], runner=runner))
        if rec.get("head"):
            steps.append(_run([git, "-C", str(dest), "reset", "--hard", rec["head"]], runner=runner))
        if carried.get("patch"):
            steps.append(_run([git, "-C", str(dest), "apply", "--binary", str(stage / carried["patch"])], runner=runner))
        written = 0
        for rel in carried.get("untracked") or []:
            target = dest / rel
            if ".." in Path(rel).parts or target.exists():
                continue
            source = stage / "checkouts" / rec["id"] / "untracked" / rel
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                written += 1
        failed = [s for s in steps if s["exit"] != 0]
        row["result"] = "cloned" if not failed else "cloned-with-problems"
        if failed:
            row["problems"] = failed
        if written:
            row["untracked_written"] = written
    return out


def restore(bundle: Path, home: Path, *, base: Path | None = None, mode: str = "plan", overwrite: bool = False,
            do_packages: bool = False, do_clone: bool = False, secret: str | None = None,
            runner=subprocess.run, which=shutil.which, git: str = "git") -> dict:
    if not bundle.is_file():
        raise MachineBackupError(f"no machine bundle at {bundle}")
    if secret:
        header = backup_vault.verify_file(bundle, secret)
    else:
        secret, header = secret_for(bundle, base)
    if header.get("kind") != KIND:
        raise MachineBackupError(f"{bundle.name} is a {header.get('kind')} backup, not a machine bundle")
    need = bundle.stat().st_size * 3
    if free_bytes(home) < need:
        raise DiskRefused(f"{home} has too little free space to unpack {bundle.name} (needs about "
                          f"{need / 1024 ** 2:.0f} MB)")
    stage, manifest = open_bundle(bundle, secret, home)
    try:
        rows = plan(manifest, stage, home)
        counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("missing", "same", "conflict")}
        result = {"bundle": bundle.name, "created_at": manifest.get("created_at"), "trigger": manifest.get("trigger"),
                  "from_platform": manifest.get("platform"), "mode": mode, "counts": counts, "plan": rows,
                  "not_carried": manifest.get("not_carried", []),
                  "checkouts": [{"path": r.get("path"), "branch": r.get("branch"), "unpushed": r.get("unpushed"),
                                 "dirty": r.get("dirty"), "carried": sorted((r.get("carried") or {}).keys()),
                                 "remotes": sorted((r.get("remotes") or {}).keys()),
                                 "exists_here": _destination(r["path"], home).exists() if r.get("path") else None}
                                for r in manifest.get("checkouts", [])],
                  "inventories": manifest.get("inventories", [])}
        if mode == "apply":
            result["applied"] = apply(manifest, stage, home, overwrite=overwrite)
        if do_packages:
            result["packages"] = packages(stage, runner=runner, which=which)
        if do_clone:
            result["clone"] = clone(manifest, stage, home, runner=runner, git=git)
        pointer = stage / "inventories" / "observatory.json"
        if pointer.is_file():
            result["workspace"] = json.loads(pointer.read_text(encoding="utf-8"))
        result["next"] = [s for s in (
            None if mode == "apply" else "project-observatory full machine-restore --apply  (writes only what is missing)",
            "project-observatory full machine-restore --apply --overwrite  (replaces conflicts, moving each aside)"
            if counts["conflict"] and not overwrite else None,
            None if do_packages else "project-observatory full machine-restore --packages  (Homebrew, npm, pipx, uv)",
            None if do_clone else "project-observatory full machine-restore --clone  (the recorded checkouts)",
            "project-observatory full restore --latest  (the workspace itself, from its own snapshot)") if s]
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# --- the command line ---------------------------------------------------------------------------

PROG = "project-observatory full"


def backup_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"{PROG} machine-backup",
                                 description="back up this machine's agent setup (configuration, CLI logins, "
                                             "launchd jobs, package inventories, unpushed and uncommitted work) "
                                             "into one encrypted .obmachine file in the backups root")
    ap.add_argument("action", nargs="?", choices=["now", "status"], default="now",
                    help="now (default): take one; status: the newest, its age, size and what was not carried")
    return ap


def restore_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"{PROG} machine-restore",
                                 description="put a machine bundle back on this machine; without --apply, "
                                             "--packages or --clone it only prints the plan")
    ap.add_argument("bundle", nargs="?", type=Path, help="a .obmachine file (default with --latest)")
    ap.add_argument("--latest", action="store_true", help="the newest bundle in the backups roots this machine knows")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print every item, its destination and state (default)")
    mode.add_argument("--apply", action="store_true", help="write only what is missing")
    ap.add_argument("--overwrite", action="store_true", help="with --apply: replace a conflict, moving the existing one aside")
    ap.add_argument("--packages", action="store_true", help="install the Homebrew, npm, pipx and uv inventories")
    ap.add_argument("--clone", action="store_true", help="clone the recorded checkouts and put back their carried work")
    return ap


def _home_or_none() -> Path | None:
    try:
        base = config.home()
        return base if (base / "workspace.json").is_file() else None
    except config.ConfigurationError:
        return None


def backup_main(argv: list[str]) -> int:
    a = backup_parser().parse_args(argv)
    try:
        base = config.home()
        config.validate_workspace(base, required=True)
        result = status(base) if a.action == "status" else take(base, trigger="manual")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2


def restore_main(argv: list[str]) -> int:
    a = restore_parser().parse_args(argv)
    try:
        if a.overwrite and not a.apply:
            raise MachineBackupError("--overwrite replaces files, so it needs --apply as well")
        if bool(a.bundle) == bool(a.latest):
            raise MachineBackupError("name one bundle, or pass --latest")
        base = _home_or_none()
        bundle = a.bundle.expanduser() if a.bundle else find_latest(base)
        if bundle is None:
            raise MachineBackupError("no machine bundle was found in the backups roots this machine knows; "
                                     "copy one here and name it")
        result = restore(bundle, Path.home(), base=base, mode="apply" if a.apply else "plan",
                         overwrite=a.overwrite, do_packages=a.packages, do_clone=a.clone)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        failed = (result.get("applied") or {}).get("failed")
        return 1 if failed else 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
