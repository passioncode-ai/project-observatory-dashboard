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
CARRIED and named: in MANIFEST.json, in the command's answer, in `doctor`, on Health and as a
finding (`machine.backup_not_carried`). Every such record carries a `fix`: the push that puts the
commits on a remote, or the exact `limits` value in machine-backup.json that would carry it
(`remedy`). The only copies of work (commits no remote has, then uncommitted changes) are
listed first, so the receipt's first twenty are the ones that matter most.

A checkout's carried parts (bundle, patch, untracked files) are spooled to files in the private
stage and streamed into the tar from there, so a large cap costs disk, checked against
`min_free_gb`, and never memory.

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
patches and untracked files. `--only` and `--skip` limit a restore by category and `--projects`
limits `--clone` (OBS-282, "a partial restore" below); each step that writes leaves a receipt that
`full machine-move status` reads (OBS-286). Nothing is printed but paths, counts and reasons.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
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
import machine_identity
import osprivacy
import oslocks
import secret_store_carry

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
#:
#: The checkout caps were raised on 2026-10-21 from what one working agent machine measured the
#: day before: 476 checkouts, 19 items not carried, ten of them repositories whose unpushed
#: commits made bundles of 16.7, 19.1, 24.0, 26.4, 29.6, 30.3, 76.8, 110.6, 130.0 and 404.3 MB
#: against a 16 MB cap (one held 32 unpushed commits), two patches of 23.4 and 59.1 MB against
#: 8 MB, and untracked sets of 15.9 and 161.5 MB against 8 MB. Those commits are the only copy
#: of the work, and the bundle exists so the machine can move. At 160/64/32 MB and 768 MB for
#: all checkouts, every one of them but the 404 MB bundle (one commit of a large file) and the
#: 161.5 MB untracked set is carried: about 560 MB more per bundle, three bundles kept. 160, not
#: 128: caps are MiB and the largest of them, the 32 commits, is 130.0 MiB (a 128 cap refused
#: it, review of #325). `test_the_default_caps_carry_what_the_measured_machine_left_behind`
#: computes this claim from the receipt's exact sizes through `over_cap`. What still does not
#: fit is a finding with its fix (`remedy`), never only a count.
LIMITS = {
    "file_mb": 16,          # one file
    "entry_mb": 128,        # one manifest entry (a directory such as ~/.claude/skills)
    "total_mb": 320,        # every file copy together; measured: 69 MB compressed with every skill carried
    "bundle_mb": 160,       # one checkout's git bundle of unpushed commits
    "patch_mb": 64,         # one checkout's patch of uncommitted changes
    "untracked_mb": 32,     # one checkout's untracked files together
    "checkouts_mb": 768,    # everything carried for checkouts together
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
        # The secret stores the workspace snapshot carries encrypted (OBS-350): never a second copy here.
        self.store_roots: list[tuple[Path, dict]] = []

    def carried_elsewhere(self, path: Path) -> bool:
        """Skip `path` when it lies in a secret store the workspace snapshot carries, saying where it travels."""
        store = secret_store_carry.inside(path, self.store_roots)
        if store is None:
            return False
        self.skip(path, f"carried encrypted in the workspace snapshot (sources.{store['settings_keys'][0]})")
        return True

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
        if self.carried_elsewhere(root):
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
                    elif self.carried_elsewhere(path):
                        pass
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
                    elif self.carried_elsewhere(path):
                        pass
                    elif path.is_symlink():
                        self.link(path, category)
                    elif path.is_file():
                        self.file(path, category)
                    else:
                        self.skip(path, "not a regular file")
        except OSError as exc:
            self.miss(display(root, self.home), f"unreadable: {type(exc).__name__}")


# --- what was left behind, and how to carry it ------------------------------------------------

CAP = re.compile(r"\(limits\.([a-z_]+)\)")


def loss_rank(record: dict) -> int:
    """0: commits no remote has; 1: uncommitted changes; 2: untracked files; 3: a checkout
    git could not read (its work unknown); 4: a file or folder of the home."""
    reason = str(record.get("reason") or "")
    if "commit(s) no remote has" in reason:
        return 0
    if reason.startswith("uncommitted changes"):
        return 1
    if "untracked file" in reason:
        return 2
    if reason.startswith("checkout not read"):
        return 3
    return 4


def remedy(record: dict, limits: dict) -> str:
    """How to carry what was left behind: in the person's words, with the exact value."""
    reason, source = str(record.get("reason") or ""), str(record.get("source") or "")
    size = int(record.get("size") or 0)
    rank = loss_rank(record)
    where = f"config/{CONFIG_NAME}"
    cap = CAP.search(reason)
    if cap and cap.group(1) in LIMITS and cap.group(1) != "min_free_gb":
        key = cap.group(1)
        # A per-item cap needs the item's size; a cap shared by many (entry, total, checkouts)
        # needs what is already under it as well.
        shared = key in ("entry_mb", "total_mb", "checkouts_mb")
        need = max(1, math.ceil(((limits.get(key) or 0) * 1024 * 1024 * shared + size) / 1024 ** 2))
        raise_it = f'raise limits.{key} in {where} to at least {need} MB ({{"limits": {{"{key}": {need}}}}})'
        first = {0: f"push the branch so a remote holds the commits (`git -C {source} push`), or ",
                 1: f"commit and push the changes in {source}, or ",
                 2: f"commit, ignore or remove the untracked files in {source}, or "}.get(rank, "")
        last = "" if rank < 4 else f", or add it to `exclude` in {where} if it need not move"
        return first + raise_it + last
    if "would leave under" in reason:
        return ("free space on the disk that holds the backups root (`project-observatory full disk-guard --apply` "
                f"removes what loses nothing), or lower limits.min_free_gb in {where}")
    if "git bundle failed" in reason:
        return (f"`git -C {source} fsck` names what the repository lacks; repair it, then push the branch so a "
                "remote holds the commits")
    if rank == 3:
        return f"git cannot read the checkout at {source}: repair or remove its `.git`, or keep it out of `roots` in {where}"
    if reason.startswith("grew past"):
        return f"take the backup again once the file stops growing, or raise limits.file_mb in {where}"
    if "unreadable" in reason:
        return f"make {source} readable by this account, or add it to `exclude` in {where}"
    if "time budget" in reason:
        return f"raise limits.checkout_seconds in {where}, or narrow `roots`"
    return f"see the reason; every setting is in {where}"


def settle_not_carried(col: "Collection") -> None:
    """Every record gets its `fix`, and the only copies of work come first (in place: the
    manifest holds the same list)."""
    for record in col.not_carried:
        record["fix"] = remedy(record, col.limits)
    col.not_carried.sort(key=lambda r: (loss_rank(r), -int(r.get("size") or 0)))


class _Hashing:
    """A file read by tarfile, hashed on the way through: a carried part is never whole in memory."""

    def __init__(self, stream, digest):
        self.stream, self.digest = stream, digest

    def read(self, size=-1):
        block = self.stream.read(size)
        self.digest.update(block)
        return block


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


HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
#: A ref name a bundle may hand to git: no option, no traversal, no control or space.
REF_NAME = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/+@-]{1,200}")
REMOTE_NAME = re.compile(r"(?!-)[A-Za-z0-9._-]{1,64}")
CID = re.compile(r"[0-9a-f]{12}")
#: A repository's tags recorded by name, so a tag on a pushed commit (which no bundle carries)
#: is put back too. A repository with more is named, not recorded in full.
MAX_TAGS = 500


def _checkout_kind(path: Path) -> str:
    """`repository`, `worktree` (a `.git` file into `<common>/worktrees/`) or `submodule`
    (a `.git` file into `<superproject>/.git/modules/`)."""
    marker = path / ".git"
    if not marker.is_file():
        return "repository"
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "repository"
    target = text[len("gitdir:"):].strip().replace("\\", "/") if text.startswith("gitdir:") else ""
    if "/worktrees/" in target:
        return "worktree"
    if "/modules/" in target:
        return "submodule"
    return "repository"


def inspect_checkout(path: Path, home: Path) -> dict:
    """What a checkout holds that no remote does. Reads only; runs no hook or filter (safe_git).

    Local-only commits are counted over every local tip: branches, tags, a detached HEAD and
    every stash entry (round 1 of #305: a detached worktree with 14 commits counted none)."""
    kind = _checkout_kind(path)
    rec = {"path": display(path, home), "worktree": kind == "worktree", "submodule": kind == "submodule"}
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
        if len(parts) >= 2 and parts[0] not in remotes and REMOTE_NAME.fullmatch(parts[0]):
            remotes[parts[0]] = redact_url(parts[1])
    rec["remotes"] = remotes
    p = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    rec["common_dir"] = (p.stdout or "").strip() if p.returncode == 0 else None
    p = _git(["reflog", "show", "--format=%H %gs", "refs/stash", "--"], path)
    stashes = []
    for line in (p.stdout or "").splitlines() if p.returncode == 0 else []:
        sha, _, message = line.partition(" ")
        if HEX.fullmatch(sha):
            stashes.append({"sha": sha, "message": message[:200]})
    rec["stashes"] = stashes
    p = _git(["for-each-ref", "--format=%(refname:strip=2) %(objectname) %(*objectname)", "refs/tags"], path)
    tags = {}
    for line in (p.stdout or "").splitlines() if p.returncode == 0 else []:
        parts = line.split()
        if len(parts) >= 2 and REF_NAME.fullmatch(parts[0]):
            tags[parts[0]] = parts[2] if len(parts) > 2 and HEX.fullmatch(parts[2]) else parts[1]
    if len(tags) > MAX_TAGS:
        rec["tags_over"] = len(tags)
        tags = dict(sorted(tags.items())[:MAX_TAGS])
    rec["tags"] = tags
    tips = ["--branches", "--tags", *(s["sha"] for s in stashes)]
    if rec["head"] and not rec["branch"]:
        tips.append(rec["head"])
    if rec["head"] or stashes or tags:
        p = _git(["rev-list", "--count", *tips, "--not", "--remotes", "--"], path)
        rec["unpushed"] = int((p.stdout or "0").strip() or 0) if p.returncode == 0 else None
    else:
        rec["unpushed"] = 0
    return rec


def _bundle_repository(path: Path, rec: dict, heads: dict, work: Path, cid: str):
    """A temporary bare repository that borrows the checkout's objects (alternates) and names
    every local tip: branches, tags, remote-tracking refs, each detached HEAD of the repository
    (`refs/carried/head/<id>`) and each stash entry (`refs/carried/stash/<n>`). The source is
    never written. Returns (its path, the estimated size in bytes or None)."""
    common = Path(rec["common_dir"]) if rec.get("common_dir") else path / ".git"
    tmp = work / f"{cid}.git"
    p = _git(["init", "-q", "--bare", str(tmp)], work)
    if p.returncode != 0:
        raise MachineBackupError(f"git init failed: {(p.stderr or '').strip()[:120]}")
    (tmp / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (tmp / "objects" / "info" / "alternates").write_text(str(common / "objects") + "\n", encoding="utf-8")
    # A shallow clone's boundary travels with it: without it the walk runs into parents the
    # source never had (measured: six shallow checkouts failed or counted an empty bundle).
    if (common / "shallow").is_file():
        shutil.copyfile(common / "shallow", tmp / "shallow")
    p = _git(["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags", "refs/remotes"], path)
    lines, positive, negative = [], [], set()
    for line in (p.stdout or "").splitlines():
        sha, _, ref = line.partition(" ")
        if not HEX.fullmatch(sha) or not ref or ref.endswith("/HEAD"):
            continue
        if ref.startswith("refs/remotes/"):
            negative.add(sha)   # an exclusion by object, never by a short name git must resolve
        else:
            lines.append(f"update {ref} {sha}")
            positive.append(ref)
    for other, head in sorted(heads.items()):
        lines.append(f"update refs/carried/head/{other} {head}")
        positive.append(f"refs/carried/head/{other}")
    for n, entry in enumerate(rec.get("stashes") or []):
        lines.append(f"update refs/carried/stash/{n} {entry['sha']}")
        positive.append(f"refs/carried/stash/{n}")
    import safe_git
    p = safe_git.run(["update-ref", "--stdin"], repo=tmp, input="\n".join(lines) + "\n", timeout=GIT_TIMEOUT)
    if p.returncode != 0:
        raise MachineBackupError(f"naming the local tips failed: {(p.stderr or '').strip()[:120]}")
    # Full ref names and `^<object>` exclusions on stdin: `--branches`/`--remotes` hand git short
    # names to resolve again, and a name that is also a remote branch was refused as a bad object.
    revs = "\n".join([*positive, *(f"^{sha}" for sha in sorted(negative))]) + "\n"
    # The objects' size on disk, before anything is written: every object the bundle would
    # carry, sized by `cat-file` (`objectsize:disk`), summed.
    p = safe_git.run(["rev-list", "--objects", "--stdin"], repo=tmp, input=revs, timeout=300)
    estimate = None
    if p.returncode == 0:
        names = "\n".join(line.split(" ", 1)[0] for line in (p.stdout or "").splitlines() if line) + "\n"
        q = safe_git.run(["cat-file", "--batch-check=%(objectsize:disk)"], repo=tmp, input=names, timeout=300)
        if q.returncode == 0:
            sizes = [line for line in (q.stdout or "").split() if line.isdigit()]
            estimate = sum(int(n) for n in sizes)
    return tmp, revs, estimate


def over_cap(size: int, cap_key: str, used: int, limits: dict) -> str | None:
    """The cap a checkout part of `size` bytes breaks, or None: its own (`cap_key`) or, with
    `used` bytes already carried for checkouts, `checkouts_mb`. Caps are MiB. The one rule both
    the backup and the test of the documented defaults apply."""
    if size > int(limits[cap_key] * 1024 * 1024):
        return cap_key
    if used + size > int(limits["checkouts_mb"] * 1024 * 1024):
        return "checkouts_mb"
    return None


def carry_checkout(rec: dict, path: Path, home: Path, col: Collection, work: Path, cid: str,
                   bundled_dirs: set, budget: list, heads: dict | None = None) -> list[dict]:
    """The members this checkout adds: one bundle per repository of every commit no remote has
    (branches, tags, detached HEADs, stashes), a patch of uncommitted changes (a staged patch
    for a repository with no commit yet), untracked files with their modes. Over a cap, or
    when the disk would cross its floor, the part is named instead of written."""
    members, shown, limits = [], rec["path"], col.limits
    carried = rec.setdefault("carried", {})
    floor = int(limits["min_free_gb"] * 1024 ** 3)

    def room(size: int, cap_key: str, what: str, charge: bool = True) -> bool:
        broken = over_cap(size, cap_key, budget[0], limits)
        if broken == cap_key:
            col.miss(shown, f"{what}: over the cap of {limits[cap_key]} MB (limits.{cap_key})", size)
            return False
        if broken:
            col.miss(shown, f"{what}: over the checkouts' cap of {limits['checkouts_mb']} MB (limits.checkouts_mb)", size)
            return False
        if free_bytes(work) - size < floor:
            col.miss(shown, f"{what}: writing it would leave under {limits['min_free_gb']} GB free", size)
            return False
        if charge:
            budget[0] += size
        return True

    repository = rec.get("common_dir") or str(path)
    if rec.get("unpushed") and repository not in bundled_dirs:
        bundled_dirs.add(repository)
        what = f"{rec['unpushed']} commit(s) no remote has"
        try:
            tmp, revs, estimate = _bundle_repository(path, rec, heads or {}, work, cid)
        except MachineBackupError as exc:
            col.miss(shown, f"{what}: {exc}")
            tmp = None
        # L1: the size is known before anything is written.
        if tmp is not None and (estimate is None or room(estimate, "bundle_mb", what, charge=False)):
            target = work / f"{cid}.bundle"
            import safe_git
            p = safe_git.run(["bundle", "create", "-q", str(target), "--stdin"], repo=tmp, input=revs, timeout=300)
            if target.exists():
                os.chmod(target, 0o600)
            if p.returncode != 0 or not target.is_file():
                col.miss(shown, f"{what}: git bundle failed ({(p.stderr or '').strip()[:120]})")
            elif room(target.stat().st_size, "bundle_mb", what):
                # Kept in the private stage and streamed into the tar (`_take` removes `work`).
                members.append({"arcname": f"checkouts/{cid}/unpushed.bundle", "file": target})
                carried["bundle"] = f"checkouts/{cid}/unpushed.bundle"
            else:
                target.unlink(missing_ok=True)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    if rec.get("changed"):
        # L2: fixed prefixes, no external diff or textconv (safe_git adds those), whatever the
        # person's configuration says.
        args = ["diff", "--binary", "--no-color", "--src-prefix=a/", "--dst-prefix=b/"]
        unborn = not rec.get("head")
        name = "staged.patch" if unborn else "changes.patch"
        p = _git([*args, "--cached"] if unborn else [*args, "HEAD"], path, text=False, timeout=300)
        if p.returncode != 0:
            col.miss(shown, "uncommitted changes: git diff failed")
        elif p.stdout and room(len(p.stdout), "patch_mb", "uncommitted changes"):
            spool = work / f"{cid}-{name}"
            import osprivacy
            fd = osprivacy.open(spool, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(p.stdout)
            members.append({"arcname": f"checkouts/{cid}/{name}", "file": spool})
            carried["patch"] = f"checkouts/{cid}/{name}"
    if rec.get("untracked"):
        files, size = [], 0
        for rel in rec["untracked"]:
            f = path / rel
            if f.is_symlink() or not f.is_file() or not _safe_rel(rel):
                continue
            files.append((rel, f))
            size += f.stat().st_size
        if files and room(size, "untracked_mb", f"{len(files)} untracked file(s)"):
            carried["untracked"], carried["untracked_modes"] = [], {}
            for n, (rel, f) in enumerate(files):
                # By number, never by name: two names that differ only in case would be one file
                # on a case-insensitive stage, and the second copy would replace the first.
                spool = work / cid / "untracked" / str(n)
                try:
                    spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(f, spool, follow_symlinks=False)
                    os.chmod(spool, 0o600)
                    mode = stat.S_IMODE(f.stat().st_mode)
                except OSError as exc:
                    col.miss(f"{shown}/{rel}", f"unreadable: {type(exc).__name__}")
                    continue
                members.append({"arcname": f"checkouts/{cid}/untracked/{rel}", "file": spool, "mode": mode})
                carried["untracked"].append(rel)
                carried["untracked_modes"][rel] = mode
    return members


def _safe_rel(rel) -> bool:
    """A relative path a bundle may name inside a destination: no root, no `..`, no NUL."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        return False
    parts = Path(rel).parts
    return not Path(rel).is_absolute() and ".." not in parts and not rel.startswith("-")


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


STAGE_DIR = ".machine-stage"


def stage_root(base: Path | None) -> Path:
    """The one private folder every decrypted or temporary copy lives in: under the workspace's
    store, or, before a workspace exists on a new machine, under the per-user data directory.
    Never a visible folder of the home (round 1 of #305: a restore killed mid-way left a
    plaintext `~/.decrypted-*` holding every secret)."""
    if base is not None:
        return base / "store" / STAGE_DIR
    return backup_vault.data_dir() / "project-observatory" / "machine-stage"


def _sweep(root: Path) -> list[str]:
    removed = []
    for entry in root.iterdir():
        if entry.name == ".lock":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed.append(entry.name)
    return removed


def _hold(root: Path) -> int | None:
    """Open and lock the stage's lock file; None when another run holds it."""
    import osprivacy
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    fd = osprivacy.open(root / ".lock", os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
    try:
        oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


@contextlib.contextmanager
def _staging(root: Path):
    """Hold the stage: 0700, locked against a second backup or restore, swept of whatever a
    killed run left (SIGKILL cannot be caught; the next run removes it), and swept again on
    the way out. A run that does not get the lock touches nothing: it raises before any sweep
    (round 2 of #305: a refused run emptied a live restore's stage). Also sweeps the stages
    releases before this one left in the store, and the other stage location (the one used
    before a workspace exists) when no run holds it."""
    fd = _hold(root)
    if fd is None:
        raise MachineBackupError("another machine backup or restore is running")
    try:
        _sweep(root)
        for old in root.parent.glob(".machine-backup-*"):
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old, ignore_errors=True)
        other = stage_root(None)
        if other != root and other.is_dir():
            other_fd = _hold(other)
            if other_fd is not None:
                try:
                    _sweep(other)
                finally:
                    os.close(other_fd)
        yield root
    finally:
        try:
            _sweep(root)
        finally:
            os.close(fd)


@contextlib.contextmanager
def _phase(root: Path):
    folder = Path(tempfile.mkdtemp(prefix="restore-", dir=root))  # paths-check: allow: inside the swept 0700 stage
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def raise_on_signals() -> None:
    """SIGTERM and SIGHUP raise, so every `finally` that removes a stage runs."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, stop)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# --- taking one ---------------------------------------------------------------------------------

def existing_ancestor(path: Path) -> Path:
    """`path`, or its nearest parent that exists: where a write to `path` would land."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def free_bytes(path: Path) -> int:
    """Free bytes on the volume a write to `path` would land on. Shared: `full update`
    sizes its preflight with it too (engine_update.space_check)."""
    return shutil.disk_usage(existing_ancestor(path)).free


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
        snaps = backup_vault.order_artifacts(f for k in backup_vault.SNAPSHOT_KINDS
                                             for f in backup_vault.artifacts(root, k, backup_vault.SNAPSHOT_SUFFIX))
        newest = snaps[-1].name if snaps else None
    except (config.ConfigurationError, OSError):
        root = None
    # Where the secret store kept outside the workspace travels (OBS-350): inside the workspace's
    # encrypted snapshot, never in this bundle. `newest_snapshot_carried` is the carry record's word.
    stores = secret_store_carry.plan(base, carry_now=True, why_not=None)
    if stores and stores[0]["reason"]:
        store = {"external": True, "carried_in": None, "reason": stores[0]["reason"]}
    elif stores:
        last = backup_vault._last_carry(base)
        carried = None if not last or last.get("state") == "unknown" else last.get("state") == "carried"
        store = {"external": True, "carried_in": "workspace snapshot", "newest_snapshot_carried": carried}
    else:
        store = {"external": False}
    return {"workspace": display(base, home), "engine_version": config.VERSION,
            "instance_id": marker.get("instance_id"), "backups_root": display(root, home) if root else None,
            "newest_snapshot": newest, "secret_store": store,
            "restore": "project-observatory full restore --latest (or full restore <snapshot>.obsnap)"}


def take(base: Path, *, trigger: str = "manual", home: Path | None = None, runner=subprocess.run,
         path: str | None = None, at: datetime.datetime | None = None) -> dict:
    """Take one machine bundle now. Raises MachineBackupError (DiskRefused for space) with
    the reason; the attempt is recorded either way, so `status` shows it."""
    at = at or datetime.datetime.now(datetime.timezone.utc)
    started = time.monotonic()
    try:
        with _lock(base), _staging(stage_root(base)) as root:
            result = _take(base, trigger, home or Path.home(), runner, path, at, started, root)
    except DiskRefused as exc:
        _record(base, {"at": iso(at), "result": "refused-disk", "trigger": trigger, "detail": str(exc)[:300]})
        raise
    except MachineBackupError as exc:
        if "is running" not in str(exc):
            _record(base, {"at": iso(at), "result": "failed", "trigger": trigger, "detail": str(exc)[:300]})
        raise
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        _record(base, {"at": iso(at), "result": "failed", "trigger": trigger,
                       "detail": f"{type(exc).__name__}: {str(exc)[:240]}"})
        raise MachineBackupError(f"the machine backup failed: {type(exc).__name__}: {exc}") from exc
    _record(base, result)
    return result


def _take(base: Path, trigger: str, home: Path, runner, path: str | None, at, started, stage: Path) -> dict:
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
    # Only a store the encrypted workspace snapshot carries is left to it (OBS-350): one it would never
    # carry (a folder that holds the workspace itself) is walked like any other path the entries name.
    col.store_roots = secret_store_carry.store_roots(
        [s for s in secret_store_carry.plan(base, carry_now=True, why_not=None) if not s["reason"]], home)
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
    work = Path(tempfile.mkdtemp(prefix="backup-", dir=stage))  # paths-check: allow: inside the swept 0700 stage
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
        # Every detached HEAD of a repository goes into that repository's one bundle.
        heads: dict[str, dict] = {}
        for rec in records:
            if rec.get("head") and not rec.get("branch") and not rec.get("error"):
                rid = hashlib.sha256(rec["path"].encode()).hexdigest()[:12]
                heads.setdefault(rec.get("common_dir") or rec["path"], {})[rid] = rec["head"]
        # A main checkout before its worktrees, so the repository's one bundle is the main
        # checkout's, which a restore clones first and adds the worktrees to.
        for p, rec in sorted(zip(found, records), key=lambda pair: bool(pair[1].get("worktree"))):
            rec["id"] = hashlib.sha256(rec["path"].encode()).hexdigest()[:12]
            if rec.get("error"):
                col.miss(rec["path"], f"checkout not read: {rec['error']}")
                continue
            members += carry_checkout(rec, p, home, col, work, rec["id"], bundled, budget,
                                      heads.get(rec.get("common_dir") or rec["path"], {}))
        for rec in records:
            if "untracked" in rec:
                rec["untracked_count"] = len(rec.pop("untracked"))
        estimate = col.total + budget[0] + sum(len(i.get("data") or b"") for i in invs)
        if free_bytes(root) - estimate < floor:
            raise DiskRefused(f"too little free space: a bundle of up to {estimate / 1024 ** 2:.0f} MB would leave "
                              f"under the {limits['min_free_gb']} GB a machine backup keeps (limits.min_free_gb "
                              f"in config/{CONFIG_NAME}); the disk holds {root}")
        manifest = {"format": FORMAT, "created_at": iso(at), "trigger": trigger, "engine_version": config.VERSION,
                    "platform": sys.platform, "machine_id": machine_identity.machine_id(home),
                    "home_path": str(home), "items": [], "skipped": col.skipped,
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
                    info = tarfile.TarInfo(m["arcname"])
                    info.size, info.mode, info.mtime = m["file"].stat().st_size, 0o600, int(at.timestamp())
                    digest = hashlib.sha256()
                    with m["file"].open("rb") as stream:
                        tar.addfile(info, _Hashing(stream, digest))
                    manifest["items"].append({"kind": "checkout", "source": m["arcname"], "arcname": m["arcname"],
                                              "size": info.size, "sha256": digest.hexdigest(),
                                              **({"mode": m["mode"]} if "mode" in m else {})})
                settle_not_carried(col)
                manifest["totals"] = {"items": len(manifest["items"]), "skipped": len(col.skipped),
                                      "not_carried": len(col.not_carried), "checkouts": len(records)}
                add("MANIFEST.json", json.dumps(manifest, indent=1, ensure_ascii=False).encode())

        backup_vault._mark_owner(root, base)
        dest = root / f"{KIND}-{at.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}{SUFFIX}"
        backup_vault.encrypt_stream(dest, fill, secret, kind=KIND, content="tar+gzip")
        dropped = backup_vault.rotate(root, KIND, SUFFIX, KEEP, protect=dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    settle_not_carried(col)
    return {"at": iso(at), "result": "taken", "trigger": trigger, "file": dest.name, "root": str(root),
            "size": dest.stat().st_size, "items": manifest["totals"]["items"],
            "checkouts": len(records), "skipped": len(col.skipped),
            "not_carried_count": len(col.not_carried), "not_carried": col.not_carried[:NOT_CARRIED_SHOWN],
            "not_carried_commits": sum(1 for r in col.not_carried if loss_rank(r) == 0),
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


def expected_before_update(base: Path, at: datetime.datetime | None = None) -> int:
    """Bytes `before_update` is about to write: the newest bundle's size when one is due (the
    best estimate of the next), 0 when none will be taken. `full update` counts it in its first
    space check, so a marginal disk is refused before a fresh bundle fills it (review of #325)."""
    at = at or datetime.datetime.now(datetime.timezone.utc)
    try:
        if not enabled(base) or not automatic_allowed():
            return 0
        last = newest(base)
        if last is None:
            return 0
        when = _stamp_time(last)
        if when and at - when < FRESH:
            return 0
        return last.stat().st_size
    except OSError:
        return 0


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
           "not_carried_commits": success.get("not_carried_commits"),
           "not_carried": success.get("not_carried") or [], "settings": str(settings_path(base))}
    items = []

    def warn(code: str, text: str, step: str, **params) -> None:
        # `step` is what doctor's attention row tells a person to do (OSS-99).
        items.append({"code": code, "text": text, "step": step, **params})

    take = "project-observatory full machine-backup"
    try:
        load_settings(base)
    except config.ConfigurationError as exc:
        warn("machine-backup-settings", f"config/{CONFIG_NAME} is refused: {exc}",
             f"correct config/{CONFIG_NAME} in the workspace; `{take} status` reads it again",
             detail=str(exc)[:200])
    if not out["enabled"]:
        pass
    elif last.get("result") == "refused-disk" and (not when or (_parse(last.get("at")) or at) >= when):
        warn("machine-backup-refused-disk", f"the last machine backup was not written: {last.get('detail', '')[:200]}",
             f"free disk space (or lower limits.min_free_gb in config/{CONFIG_NAME}), then `{take}`",
             detail=str(last.get("detail", ""))[:200])
    elif last.get("result") == "failed" and (not when or (_parse(last.get("at")) or at) >= when):
        warn("machine-backup-failed", f"the last machine backup failed: {last.get('detail', '')[:200]}; "
                                      "it is tried again within the hour",
             f"`{take}` takes one now and says why it fails", detail=str(last.get("detail", ""))[:200])
    if out["enabled"] and when is None:
        warn("machine-backup-missing", "no machine backup yet: the maintenance job takes one a day; "
                                       "`project-observatory full machine-backup` takes one now", take)
    elif out["enabled"] and at - when > STALE:
        warn("machine-backup-stale", f"the newest machine backup is {out['age_hours']:.0f} hours old", take,
             hours=out["age_hours"])
    if file and out["not_carried_count"]:
        names = ", ".join(i["source"] for i in out["not_carried"][:5])
        commits = out.get("not_carried_commits") or sum(1 for i in out["not_carried"] if loss_rank(i) == 0)
        lost = (f", {commits} of them the only copy of commits no remote has" if commits else "")
        warn("machine-backup-not-carried", f"{out['not_carried_count']} item(s) were not carried in the newest "
                                           f"machine backup{lost} ({names}…); `full machine-backup status` names "
                                           "each with how to carry it",
             f"{take} status", count=out["not_carried_count"], commits=commits)
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
    return backup_vault.order_artifacts(found)[-1] if found else None


def secret_for(bundle: Path, base: Path | None, stdin_value: str | None = None) -> tuple[str, dict, str]:
    """The passphrase that opens `bundle`, the bundle's header and where the passphrase came
    from: the environment, `--passphrase-stdin`, this workspace's, the OS credential store
    under the bundle's folder label, then one terminal prompt (`backup_vault.open_with`,
    OBS-348). A wrong candidate costs one chunk, the winner is authenticated in full once, and
    another machine's passphrase is used for this restore and kept nowhere."""
    secret, header, source = backup_vault.open_with(bundle, base=base, label=bundle.parent.name,
                                                    stdin_value=stdin_value, prompt=True)
    return secret, header, source


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


ARC_PREFIXES = ("files/", "abs/", "inventories/", "checkouts/")


def _safe_arcname(name) -> bool:
    return (isinstance(name, str) and name.startswith(ARC_PREFIXES) and _safe_rel(name)
            and "//" not in name)


def extract(bundle: Path, secret: str, dest: Path, want) -> int:
    """Decrypt only the members `want(name)` accepts into `dest` (inside the stage), each 0600.
    The whole file was authenticated by the caller; any unsafe entry refuses the bundle."""
    import osprivacy
    count = 0
    with bundle.open("rb") as stream:
        reader = backup_vault._Reader(stream, secret)
        with tarfile.open(fileobj=io.BufferedReader(reader, backup_vault.CHUNK), mode="r|gz") as tar:
            for member in tar:
                name = Path(member.name)
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()) \
                        or name.is_absolute() or ".." in name.parts:
                    raise MachineBackupError("the machine bundle contains an unsafe entry")
                if not member.isfile() or not want(member.name):
                    continue
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with os.fdopen(osprivacy.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                    shutil.copyfileobj(tar.extractfile(member), out, backup_vault.CHUNK)
                count += 1
    return count


def validate_checkout(rec: dict, home: Path) -> str | None:
    """Why a checkout record read from a bundle may not be used as paths or git arguments, or
    None. A bundle is the person's own, but it is a file from another machine (L5)."""
    if not isinstance(rec, dict) or not CID.fullmatch(str(rec.get("id", ""))):
        return "an invalid record id"
    try:
        _destination(rec.get("path", ""), home)
    except MachineBackupError as exc:
        return str(exc)
    if rec.get("branch") is not None and not REF_NAME.fullmatch(str(rec["branch"])):
        return "an invalid branch name"
    if rec.get("head") is not None and not HEX.fullmatch(str(rec["head"])):
        return "an invalid HEAD"
    for name, url in (rec.get("remotes") or {}).items():
        if not REMOTE_NAME.fullmatch(str(name)) or not isinstance(url, str) or not url or url.startswith("-"):
            return "an invalid remote"
    for entry in rec.get("stashes") or []:
        if not HEX.fullmatch(str((entry or {}).get("sha", ""))):
            return "an invalid stash entry"
    for name, sha in (rec.get("tags") or {}).items():
        if not REF_NAME.fullmatch(str(name)) or not HEX.fullmatch(str(sha)):
            return "an invalid tag"
    carried, cid = rec.get("carried") or {}, rec["id"]
    if carried.get("bundle") not in (None, f"checkouts/{cid}/unpushed.bundle"):
        return "an invalid carried bundle"
    if carried.get("patch") not in (None, f"checkouts/{cid}/changes.patch", f"checkouts/{cid}/staged.patch"):
        return "an invalid carried patch"
    if not all(_safe_rel(r) for r in carried.get("untracked") or []):
        return "an invalid untracked path"
    return None


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


# --- a second machine (OBS-283) -----------------------------------------------------------------
#
# A bundle restored on ANOTHER machine must not give it this machine's SSH identity or start its
# jobs: one key would sign on two machines, and a bot polling one token, a scheduler or a publisher
# would run twice from the next login. So in second-machine mode (`--second-machine`, and the
# default whenever the bundle names another machine or none) the SSH files a person shares between
# machines are written (config, known_hosts, authorized_keys, allowed_signers, a `*.pub` whose private
# half is not in the bundle) and nothing else of a `.ssh` folder is, whatever its name, and a file that
# reads as a private key is withheld even under a public name. Every launchd plist (systemd user unit
# off macOS) is written into a staging folder nothing loads at login, and named in the placement file
# as running elsewhere, until `full machine-move cutover LABEL` moves it here. Both rules go by the
# path an item is written to, never by the category it was taken under, so an operator's own include
# or an absolute path into a `.ssh` or job folder is held to them too.

#: The `.ssh` names a second machine gets: shared settings and public halves, never a private key.
SSH_PUBLIC = frozenset({"config", "known_hosts", "known_hosts.old", "known_hosts2", "authorized_keys",
                        "authorized_keys2", "allowed_signers", "environment", "rc"})
#: Folders of `.ssh` that hold only configuration fragments (`Include config.d/*`).
SSH_PUBLIC_DIRS = frozenset({"config.d", "conf.d"})
#: What a private key's text holds, whatever the file is called (PEM, OpenSSH, PuTTY).
PRIVATE_MARKERS = (b"PRIVATE KEY-----", b"PuTTY-User-Key-File-")
#: Where each job system's definitions live, as path parts; anything under them is staged.
JOB_PARTS = {"launchd": ("Library", "LaunchAgents"), "systemd": (".config", "systemd", "user")}
#: This engine's own jobs (maintenance, update check, tick, server), by file name: every machine installs
#: its own (`full maintain ensure`, `full agent install`), so a second machine neither stages nor names them.
OWN_JOB_PREFIXES = ("org.project-observatory.", "project-observatory-")
#: A Host alias `ssh-keys rekey --server` takes (tools/ssh_keys.py HOST_ARG).
HOST_ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@%+:\[\]-]{0,252}")
#: ssh's default identity files: read without an IdentityFile line, for every host.
DEFAULT_IDENTITIES = ("id_ed25519", "id_ecdsa", "id_ecdsa_sk", "id_ed25519_sk", "id_rsa", "id_dsa")
REASONS = {
    "ssh-private": "ssh-private: of a .ssh folder a second machine gets only the public files and the settings "
                   "ssh shares between machines; a key among the rest is made anew there under the same name "
                   "(`ssh_keys` in this answer names each step)",
    "ssh-private-content": "ssh-private: the file's text is a private key, so it stays on the machine it came from",
    "ssh-pub-pair": "ssh-private: the public half of a key that stays on the other machine; this machine makes its "
                    "own key under the same name, which this file would block",
    "ssh-link": "ssh-private: a link in a .ssh folder may lead to a private key; it is not written",
    "job-folder-link": "a linked job folder: written as a link, everything it leads to would load at the next login; "
                       "the jobs it held are staged one by one",
    "own-job": "this engine's own job: each machine installs its own (`project-observatory full maintain ensure`), "
               "so it is neither written nor named as running elsewhere",
}


def _area(item: dict, home: Path) -> tuple[str | None, tuple[str, ...]]:
    """Where an item is written, by path: (`ssh`, the parts under the `.ssh` folder), (`launchd` or
    `systemd`, the parts under the job folder), or (None, ()). A path under this home is read from
    its destination; an absolute path from another home (an `abs/` item) from its own parts."""
    source = str(item.get("source", ""))
    try:
        dest = _destination(source, home)
    except MachineBackupError:
        return None, ()
    try:
        parts = dest.relative_to(home).parts
    except ValueError:
        parts = PurePosixPath(source).parts
    # A default macOS or Windows volume ignores case: `~/.SSH` is the `.ssh` folder there.
    folded = tuple(p.casefold() for p in parts)
    for i, part in enumerate(folded):
        if part == ".ssh":
            return "ssh", tuple(parts[i + 1:])
        for system, folder in JOB_PARTS.items():
            if folded[i:i + len(folder)] == tuple(f.casefold() for f in folder):
                return system, tuple(parts[i + len(folder):])
    return None, ()


def _ssh_public_name(rel: tuple[str, ...]) -> bool:
    if len(rel) > 1 and rel[0] in SSH_PUBLIC_DIRS:
        return True
    return bool(rel) and (rel[-1] in SSH_PUBLIC or rel[-1].endswith(".pub"))


def looks_private(data: bytes) -> bool:
    head = data[:8192]
    return any(marker in head for marker in PRIVATE_MARKERS)


def _staged_file(item: dict, stage: Path | None) -> Path | None:
    if stage is None or item.get("kind") != "file" or not _safe_arcname(item.get("arcname")):
        return None
    path = stage / item["arcname"]
    return path if path.is_file() else None


class _Second:
    """What a second machine does with each item of one manifest (OBS-283)."""

    def __init__(self, manifest: dict, home: Path, stage: Path | None):
        self.home, self.stage = home, stage
        self.areas = {(i.get("kind"), i.get("source")): _area(i, home) for i in manifest.get("items", [])}
        self.private: set[str] = set()      # sources of withheld private files and links in a .ssh folder
        for item in manifest.get("items", []):
            area, rel = self.areas[(item.get("kind"), item.get("source"))]
            if area == "ssh" and rel and item.get("kind") in ("file", "link") and not _ssh_public_name(rel):
                self.private.add(item["source"])

    def place(self, item: dict) -> tuple[Path, str | None, str | None]:
        """(destination, how, reason): how is None (its own place), `withheld` or `staged`."""
        dest = _destination(item["source"], self.home)
        area, rel = self.areas.get((item.get("kind"), item.get("source"))) or _area(item, self.home)
        kind = item.get("kind")
        if area == "ssh":
            if not rel:
                return (dest, "withheld", REASONS["ssh-link"]) if kind == "link" else (dest, None, None)
            if kind == "dir":
                return dest, None, None
            if item["source"] in self.private:
                return dest, "withheld", REASONS["ssh-private"] if kind == "file" else REASONS["ssh-link"]
            if kind == "link":
                return dest, None, None
            if rel[-1].endswith(".pub") and item["source"][:-len(".pub")] in self.private:
                return dest, "withheld", REASONS["ssh-pub-pair"]
            data = _staged_file(item, self.stage)
            if data is not None and looks_private(data.read_bytes()):
                return dest, "withheld", REASONS["ssh-private-content"]
            return dest, None, None
        if area in JOB_PARTS:
            if not rel:
                return (dest, "withheld", REASONS["job-folder-link"]) if kind == "link" else (dest, None, None)
            if kind == "dir":
                return dest, "withheld", "a folder inside a job folder: its jobs are staged one by one"
            if rel[-1].startswith(OWN_JOB_PREFIXES):
                return dest, "withheld", REASONS["own-job"]
            return machine_identity.staging_dir(self.home, area) / rel[-1], "staged", None
        return dest, None, None

    def label(self, item: dict, dest: Path) -> str:
        """The job's label as the placement file names it: a plist's `Label` when its text is at hand."""
        data = _staged_file(item, self.stage)
        if data is not None:
            return machine_identity.job_label(dest, data.read_bytes())
        return dest.stem if dest.suffix == ".plist" else dest.name

    def ssh_keys(self, manifest: dict, config_written: bool = True, config_kept=False) -> list[dict]:
        """Every withheld private key, the hosts the carried `.ssh/config` sends to it, its public
        fingerprint, and the one step that makes this machine's own key under the same name and sends it
        where the other machine's key is registered (`full ssh-keys rekey`, OBS-285, OBS-300), so a
        carried `IdentityFile ~/.ssh/NAME` with `IdentitiesOnly yes` (what `full ssh-keys create --host`
        writes) finds a key here. `config_written` is False when this restore leaves the carried
        `~/.ssh/config` out: `--skip ssh-config`, or (`config_kept`) this machine's own different config,
        which a restore without `--overwrite` keeps (OBS-306); a list names every `ssh-config` file
        `--overwrite --only=ssh-config` would replace. rekey reads the live config, so it would not see
        a host."""
        keys: dict[str, dict] = {}
        for source in sorted(self.private):
            item = next((i for i in manifest.get("items", []) if i.get("source") == source), None)
            rel = self.areas[(item.get("kind"), source)][1] if item else ()
            if item is None or item.get("kind") != "file":
                continue
            dest = _destination(source, self.home)
            # Directly in this home's .ssh folder, whatever its case (`~/.SSH/id_ed25519` on a case-blind volume);
            # `~/dotfiles/.ssh/deploy_key` and `~/.ssh/keys/work` are not where `ssh-keys create` writes.
            in_ssh = len(rel) == 1 and dest.parent.parent == self.home and dest.parent.name.casefold() == ".ssh"
            keys[source] = {"key": rel[-1], "file": display(dest, self.home), "hosts": [],
                            "default": in_ssh and rel[0] in DEFAULT_IDENTITIES, "in_ssh": in_ssh}
        by_name = {k["key"]: k for k in keys.values() if k["in_ssh"]}
        named = set()
        for host, hostname, identity in self._config_identities(manifest):
            # The key the line names only when it sits directly in a .ssh folder: `~/.ssh/k/config.local`
            # is not the `~/.ssh/config.local` fragment.
            parts = identity.split("/")
            key = by_name.get(parts[-1]) if len(parts) >= 2 and parts[-2].casefold() == ".ssh" else None
            if key is not None:
                named.add(key["key"])
                if host not in [h["host"] for h in key["hosts"]]:
                    key["hosts"].append({"host": host, "hostname": hostname})
                # rekey reaches a Host only through a line naming exactly this home's ~/.ssh/NAME
                # (tools/ssh_keys.py `rekey_plan`, `_same_path`); `~/.SSH/NAME` or another account's
                # absolute path is a host the plan lists and rekey would not see.
                plain = identity.strip().strip('"') in {f"{t}/.ssh/{key['key']}" for t in
                                                        ("~", "%d", "${HOME}", str(self.home))}
                key.setdefault("reach", {})[host] = key.get("reach", {}).get(host, False) or plain
        out = []
        for source, key in keys.items():
            # Only a key gets a key step: its text is a private key, the carried config names it as an
            # IdentityFile, or it has one of ssh's default key names. A withheld setting such as an
            # `Include config.local` fragment gets none: `ssh-keys create config.local` would write a key
            # where ssh reads settings and break every connection (review of #394).
            item = next(i for i in manifest.get("items", []) if i.get("source") == source)
            data = _staged_file(item, self.stage)
            is_key = (key["key"] in named or key["default"]
                      or (data is not None and looks_private(data.read_bytes())))
            if not is_key:
                continue
            # Quoted for the shell: `IdentityFile "~/.ssh/work key"` names a key with a space.
            name, in_ssh = shlex.quote(key["key"]), key.pop("in_ssh")
            if not in_ssh:
                out.append({**key, "steps": [], "note": "not directly in ~/.ssh: make this machine's own key and "
                                                        "put it where the other machine kept this one, by hand"})
                continue
            # One `rekey` step in place of `create` and a `send` per target (OBS-300). rekey reads the
            # carried config itself: it sends to each Host naming ~/.ssh/NAME (verified) and to GitHub only
            # where gh's account holds the other key's fingerprint. A key no host names gets the same step:
            # rekey still makes the key under its name, reports no place to send it to, and lists what it
            # cannot know from here. A second machine never gets NAME.pub (it is withheld with its key), so
            # rekey alone could not match the key on GitHub: the plan hands it the fingerprint the bundle
            # shows, a public fact read from the private file's clear header, else the carried `.pub`.
            fingerprint = self._public_fingerprint(manifest, source)
            key["fingerprint"] = fingerprint
            reach = key.pop("reach", {})
            servers, unseen, by_hand = [], [], []
            for h in key["hosts"]:
                github = (h["hostname"] or h["host"]).lower() in ("github.com", "ssh.github.com")
                if github:
                    continue   # GitHub is matched by the fingerprint, never by a Host
                if not config_written:
                    unseen.append(h["host"])
                elif reach.get(h["host"]):
                    continue   # rekey finds it in the live config
                elif HOST_ALIAS.fullmatch(h["host"]):
                    servers.append(h["host"])   # its line is not ~/.ssh/NAME: named to rekey explicitly
                else:
                    by_hand.append(h)   # an alias neither rekey nor `send --to server:` takes
            step = f"project-observatory full ssh-keys rekey {name}"
            if fingerprint:
                step += f" --fingerprint {shlex.quote(fingerprint)}"
            step += "".join(f" --server {shlex.quote(alias)}" for alias in servers)
            notes = []
            if unseen and config_kept:
                # No restore route replaces ~/.ssh/config alone: the `ssh-config` category also replaces
                # known_hosts, authorized_keys and allowed_signers, and a replaced authorized_keys can lock a
                # person out of this machine's remote login (review of #414). So the note says to merge by hand.
                replaced = (", ".join(config_kept) if isinstance(config_kept, (list, tuple)) and config_kept
                            else "~/.ssh/config and every other ssh-config file that differs here")
                notes.append(f"~/.ssh/config here differs from the carried one and this restore keeps it (no "
                             f"--overwrite), so rekey does not see Host {', '.join(unseen)}: copy those Host blocks "
                             "from the other machine's ~/.ssh/config into this one by hand, or add each server to "
                             "both steps with --server ALIAS. No restore step replaces ~/.ssh/config alone: "
                             "`project-observatory full machine-restore --apply --overwrite --only=ssh-config` "
                             f"replaces {replaced} (each moved aside), and a replaced ~/.ssh/authorized_keys can "
                             "shut out a remote login to this machine")
            elif unseen:
                notes.append(f"this restore leaves ~/.ssh/config out, so rekey does not see Host {', '.join(unseen)}: "
                             "write the carried config first (`project-observatory full machine-restore --apply "
                             "--only=ssh-config`), or add each server to both steps with --server ALIAS")
            if servers:
                # OBS-306 (4): rekey sends the new key there, but ssh still offers what the carried line names.
                notes.append(f"Host {', '.join(servers)} name this key by another path than ~/.ssh/{key['key']}: "
                             f"where that path is not this machine's ~/.ssh/{key['key']}, point the IdentityFile "
                             f"line at ~/.ssh/{key['key']} so ssh offers the new key")
            for h in by_hand:
                # `send --to server:` refuses the alias by the same pattern (tools/ssh_keys.py HOST_ARG), so the
                # note names the address its HostName gives, or says to give the host an alias both take.
                address = h["hostname"] or ""
                if HOST_ALIAS.fullmatch(address) and "%" not in address:
                    notes.append(f"rekey cannot be pointed at Host {h['host']} by name: send this machine's key to "
                                 f"its address by hand (`project-observatory full ssh-keys send {name} --to "
                                 f"server:{shlex.quote(address)} --verify`); that reaches {address} with ssh's "
                                 f"defaults, not the User, Port or ProxyJump Host {h['host']} sets, so add user@ for "
                                 "its User, and when it needs a Port or ProxyJump give it a plain Host alias with "
                                 "them and add that alias to both steps with --server ALIAS")
                else:
                    notes.append(f"rekey cannot be pointed at Host {h['host']} by name, and it names no address "
                                 "this tool takes: add a plain Host alias for it to ~/.ssh/config (letters, digits, "
                                 ". _ -), then add it to both steps with --server ALIAS")
            if fingerprint is None:
                notes.append("the bundle does not show this key's fingerprint, and without it rekey sends nothing "
                             "to GitHub: run `project-observatory full ssh-keys list` on the other machine, then "
                             "add --fingerprint SHA256:... to both steps")
            if key["default"]:
                # rekey covers GitHub for a default key (by the fingerprint) and names, never guesses, the
                # servers ssh offers it to without an IdentityFile line: those are not in the carried config.
                notes.append(f"ssh offers ~/.ssh/{key['key']} to every host whose config names no key, so a "
                             "server that accepts it need not be in ~/.ssh/config: add each such server to both "
                             "steps with --server ALIAS")
            # The plan step reads only (rekey without --apply); the acting step follows it, so `next`, read
            # in order, has made the key before `--clone` needs it.
            out.append({**key, "steps": [step, step + " --apply"],
                        **({"note": "; ".join(notes)} if notes else {})})
        return out

    def _public_fingerprint(self, manifest: dict, source: str) -> str | None:
        """The other machine's key by its public half: the fingerprint in its private file's clear header,
        else in its carried `.pub`; never its private section. None when neither reads as a key."""
        try:
            sk = _ssh_keys_module()
        except ImportError:
            return None
        files = {i.get("source"): i for i in manifest.get("items", []) if i.get("kind") == "file"}
        for src, public in ((source, False), (source + ".pub", True)):
            staged = _staged_file(files[src], self.stage) if src in files else None
            if staged is None:
                continue
            try:
                if public:
                    fp = (sk.parse_public(staged.read_bytes()[:16384].decode("utf-8", "replace")) or {}).get(
                        "fingerprint")
                else:
                    fp = (sk.private_header(staged) or {}).get("fingerprint")
            except (OSError, ValueError):
                fp = None
            if isinstance(fp, str) and sk.FINGERPRINT.fullmatch(fp):
                return fp
        return None

    def _config_identities(self, manifest: dict):
        """(Host alias, its HostName or None, IdentityFile path) from the carried `.ssh/config` and its fragments."""
        for item in manifest.get("items", []):
            area, rel = self.areas.get((item.get("kind"), item.get("source")), (None, ()))
            if area != "ssh" or not rel or not (rel == ("config",) or (len(rel) > 1 and rel[0] in SSH_PUBLIC_DIRS)):
                continue
            data = _staged_file(item, self.stage)
            if data is None:
                continue
            hosts, hostnames, pending = ["*"], {}, []
            for raw in data.read_bytes()[:1 << 20].decode("utf-8", "replace").splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = re.split(r"[ \t]*=[ \t]*|[ \t]+", line, maxsplit=1)
                key, value = parts[0].lower(), (parts[1] if len(parts) > 1 else "").strip().strip('"')
                if key == "host":
                    hosts = [h for h in value.split() if not h.startswith("!")]
                elif key == "match":
                    hosts = []
                elif key == "hostname":
                    for h in hosts:
                        hostnames.setdefault(h, value)
                elif key == "identityfile":
                    pending += [(h, value) for h in hosts if "*" not in h and "?" not in h]
            for host, identity in pending:
                yield host, hostnames.get(host), identity


def _carried_config(manifest: dict, rows: list[dict], home: Path, overwrite: bool) -> tuple[str, list[str]]:
    """Whether `--apply` leaves the carried `~/.ssh/config` live here (OBS-306): `written` (missing here,
    the same here, or a conflict `--overwrite` replaces; also when the bundle carries none), `left-out`
    (`--only` or `--skip` leaves it out) or `kept` (this machine's own differs and is kept); with `kept`,
    every `ssh-config` file `--overwrite --only=ssh-config` would replace here, by its destination."""
    configs = {i.get("source") for i in manifest.get("items", [])
               if i.get("kind") == "file" and _area(i, home) == ("ssh", ("config",))}
    for row in rows:
        if row.get("source") not in configs:
            continue
        if row["state"] == "left-out":
            return "left-out", []
        if row["state"] == "conflict" and not overwrite:
            return "kept", [r["destination"] for r in rows
                            if r.get("category") == "ssh-config" and r.get("state") == "conflict"]
    return "written", []


def _ssh_keys_module():
    """The `ssh-keys` door (tools/ssh_keys.py): its readers parse a public line and a private key's clear
    header without reading the private section."""
    tools = str(Path(__file__).resolve().parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import ssh_keys
    return ssh_keys


def _other_keys(manifest: dict, stage: Path, home: Path) -> list[dict]:
    """The other machine's SSH keys a second-machine restore withholds, as `{key, fingerprint}`: public
    facts, read from a carried `.pub` or a private key's clear header, never its private section. The
    restore receipt keeps them, so `full machine-move status` can tell this machine's own key from a
    copy of the other machine's (OBS-286)."""
    try:
        sk = _ssh_keys_module()
    except ImportError:
        return []
    rule, seen = _Second(manifest, home, stage), {}
    for item in manifest.get("items", []):
        if item.get("kind") != "file":
            continue
        area, rel = rule.areas.get((item.get("kind"), item.get("source")), (None, ()))
        if area != "ssh" or not rel:
            continue
        staged = _staged_file(item, stage)
        if staged is None or rule.place(item)[1] != "withheld":
            continue
        fp = None
        try:
            if rel[-1].endswith(".pub"):
                parsed = sk.parse_public(staged.read_bytes()[:16384].decode("utf-8", "replace"))
                fp = parsed["fingerprint"] if parsed else None
            else:
                fp = (sk.private_header(staged) or {}).get("fingerprint")
        except (OSError, ValueError):
            fp = None
        name = rel[-1][:-len(".pub")] if rel[-1].endswith(".pub") else rel[-1]
        # Directly in this home's .ssh folder, as `_Second.ssh_keys` reads it: only such a key gets a
        # `rekey` step, in the restore and in `machine-move status` alike (OBS-306).
        try:
            dest = _destination(item["source"], home)
            in_ssh = len(rel) == 1 and dest.parent.parent == home and dest.parent.name.casefold() == ".ssh"
        except MachineBackupError:
            in_ssh = False
        # One entry per key; where the same key also sits elsewhere, the copy directly in ~/.ssh names it
        # (the one the restore gives a rekey step), whichever the manifest lists first.
        if fp and (fp not in seen or (in_ssh and not seen[fp]["in_ssh"])):
            seen[fp] = {"key": name, "fingerprint": fp, "in_ssh": in_ssh}
    return sorted(seen.values(), key=lambda k: (k["key"], k["fingerprint"]))


# --- a partial restore (OBS-282) ------------------------------------------------------------------
#
# A move to a second machine is rarely all or nothing: the person moves most of the setup and keeps
# some of it where it is. So a restore takes `--only` or `--skip` by CATEGORY and `--projects` for
# `--clone`. A category is decided by the same rule as the second-machine one, by the path an item is
# written to: anything in a `.ssh` folder is `ssh-private` (a private key, by name or by its text, and
# the public half of a key the bundle carries) or `ssh-config` (settings, known hosts, authorized keys,
# a public key on its own), anything in a job folder is `launchd` (a systemd user unit off macOS), and
# everything else keeps the category it was taken under, by the name a person reads. A filter never
# widens what a second machine gets: a private key stays withheld there whatever `--only` names, and
# only `--same-machine` at a terminal writes it.

#: Every category a restore can be limited to, in the order the plan shows them.
CATEGORIES = ("ssh-private", "ssh-config", "launchd", "cli-logins", "claude", "codex", "git", "shell",
              "fabric-services", "packages", "agents", "operator")
#: The category an item was taken under (Entry.category) as the restore names it; `ssh`, `launchd` and
#: `systemd` are decided by path instead.
BUNDLE_CATEGORY = {"claude": "claude", "agents": "agents", "codex": "codex", "git": "git", "shell": "shell",
                   "cli": "cli-logins", "fabric": "fabric-services", "operator": "operator"}
CATEGORY_HELP = {
    "ssh-private": "private SSH keys and the public halves of those keys (on a second machine always withheld)",
    "ssh-config": "~/.ssh/config and its fragments, known_hosts, authorized_keys, allowed_signers, a lone *.pub",
    "launchd": "background jobs: launchd plists on macOS, systemd user units elsewhere (staged on a second machine)",
    "cli-logins": "gh, doctl and heroku logins and ~/.netrc",
    "claude": "Claude Code settings, CLAUDE.md, agents, commands, hooks, skills and plugins list",
    "codex": "Codex config.toml and AGENTS.md",
    "git": "~/.gitconfig, the global ignore file and ~/.config/git",
    "shell": "zsh and bash profiles",
    "fabric-services": "Fabric service descriptors",
    "packages": "the Homebrew, npm, pipx and uv inventories that --packages installs",
    "agents": "~/.agents/skills, shared by every coding agent",
    "operator": "the paths machine-backup.json includes",
}
#: Why a row is not written: the person's filter, never a rule of this machine.
LEFT_OUT = "left out by --only or --skip"


def parse_categories(values) -> list[str]:
    """`--only`/`--skip` values (repeatable, comma-separated) as a sorted list; an unknown name is refused
    by name with the valid ones, so a typo never restores everything or nothing silently."""
    out: list[str] = []
    for value in values or []:
        for name in str(value).split(","):
            name = name.strip()
            if not name:
                continue
            if name not in CATEGORIES:
                raise MachineBackupError(f"unknown category {name!r}; the categories are {', '.join(CATEGORIES)}")
            if name not in out:
                out.append(name)
    if values and not out:
        raise MachineBackupError(f"name at least one category; the categories are {', '.join(CATEGORIES)}")
    return sorted(out, key=CATEGORIES.index)


def selection(only=None, skip=None) -> set[str]:
    """The categories a restore writes: every one, `--only`'s, or all but `--skip`'s (never both)."""
    only, skip = parse_categories(only), parse_categories(skip)
    if only and skip:
        raise MachineBackupError("give --only or --skip, not both")
    return set(only) if only else set(CATEGORIES) - set(skip)


class _Categories:
    """The category of each item of one manifest, by the path it is written to (see above)."""

    def __init__(self, manifest: dict, home: Path, stage: Path | None):
        self.home, self.stage = home, stage
        self.areas = {(i.get("kind"), i.get("source")): _area(i, home) for i in manifest.get("items", [])}
        self.private: set[str] = set()
        for item in manifest.get("items", []):
            area, rel = self.areas[(item.get("kind"), item.get("source"))]
            if area == "ssh" and rel and item.get("kind") in ("file", "link") and not _ssh_public_name(rel):
                self.private.add(item["source"])

    def of(self, item: dict, data: bytes | None = None) -> str:
        area, rel = self.areas.get((item.get("kind"), item.get("source"))) or _area(item, self.home)
        if area == "ssh":
            source = str(item.get("source", ""))
            if source in self.private:
                return "ssh-private"
            if item.get("kind") == "file":
                if source.endswith(".pub") and source[:-len(".pub")] in self.private:
                    return "ssh-private"
                if data is None:
                    staged = _staged_file(item, self.stage)
                    data = staged.read_bytes() if staged is not None else None
                if data is not None and looks_private(data):
                    return "ssh-private"
            return "ssh-config"
        if area in JOB_PARTS:
            return "launchd"
        return BUNDLE_CATEGORY.get(item.get("category"), "operator")


def category_summary(rows: list[dict], wanted: set[str], manifest: dict, *, overwrite: bool = False,
                     shown: int = 20) -> dict:
    """Per category: whether it is selected, how many items it holds in each state, and the first
    `shown` destinations an `--apply` would write (a conflict only with `--overwrite`)."""
    out: dict[str, dict] = {}
    for name in CATEGORIES:
        out[name] = {"selected": name in wanted, "items": 0, "would_write": 0, "writes": []}
    for row in rows:
        cat = out[row["category"]]
        cat["items"] += 1
        state = "left_out" if row["state"] == "left-out" else row["state"]
        cat[state] = cat.get(state, 0) + 1
        writes = row["state"] == "missing" or (row["state"] == "conflict" and overwrite)
        if writes and row["kind"] != "dir":
            cat["would_write"] += 1
            if len(cat["writes"]) < shown:
                cat["writes"].append(row["destination"])
    for cat in out.values():
        cat["more"] = max(0, cat["would_write"] - len(cat["writes"]))
    invs = [i.get("name") for i in manifest.get("inventories", []) if isinstance(i, dict) and i.get("name")]
    out["packages"].update(items=len(invs), inventories=invs,
                           note="written only by --packages, which installs each inventory with its tool")
    return out


def _with_folders(rows: list[dict], wanted: set[str]) -> set[str]:
    """The sources a filtered restore writes: every selected file and link, every selected folder, and
    each folder that holds a selected item, so a key never lands in a folder made with the default mode."""
    keep = {r["source"] for r in rows if r["category"] in wanted}
    for row in rows:
        if row["kind"] == "dir" and row["source"] not in keep:
            prefix = row["source"].rstrip("/") + "/"
            if any(s.startswith(prefix) for s in keep):
                keep.add(row["source"])
    return keep


def select_checkouts(records: list[dict], projects, home: Path) -> tuple[list[dict], list[str]]:
    """(the checkout records `--projects` names, the names that matched none). A name is a checkout's
    folder name or a glob over it (`alpha-*`); a path (`~/src/work`, or absolute) names every checkout
    at or under it. A worktree brings its main checkout, which it is added to, and a checkout brings
    what is inside it (its submodules)."""
    wanted: list[str] = []
    for value in projects or []:
        wanted += [v.strip() for v in str(value).split(",") if v.strip()]
    usable = [r for r in records if isinstance(r, dict) and isinstance(r.get("path"), str)]
    chosen: dict[int, dict] = {}
    unmatched = []
    for name in wanted:
        is_path = "/" in name or name.startswith("~")
        if is_path:
            spelled = name.rstrip("/") or "/"
            if Path(spelled).is_absolute():
                spelled = display(Path(spelled), home)
        hit = False
        for i, rec in enumerate(usable):
            path = rec["path"].rstrip("/")
            match = (path == spelled or path.startswith(spelled + "/")) if is_path else \
                fnmatch.fnmatchcase(PurePosixPath(path).name, name)
            if match:
                chosen[i] = rec
                hit = True
        if not hit:
            unmatched.append(name)
    for i, rec in list(chosen.items()):
        inside = rec["path"].rstrip("/") + "/"
        for j, other in enumerate(usable):
            if other["path"].startswith(inside):
                chosen.setdefault(j, other)
            # Only a recorded common folder ties a worktree to its main checkout: two records that name
            # none (an older bundle, a record whose git answer failed) are not one repository.
            if rec.get("worktree") and rec.get("common_dir") is not None \
                    and other.get("common_dir") == rec.get("common_dir") and not other.get("worktree"):
                chosen.setdefault(j, other)
    return [usable[i] for i in sorted(chosen)], unmatched


def plan(manifest: dict, stage: Path, home: Path, *, second: bool = False, wanted: set[str] | None = None) -> list[dict]:
    """Every file, directory and link: where it goes, its category, and whether it is missing, the same,
    or a conflict. On a second machine a private SSH file is `withheld` and a job is `staged` (OBS-283);
    with `wanted` (OBS-282) a row of another category is `left-out`, unless it is a folder that holds a
    selected item."""
    rule = _Second(manifest, home, stage) if second else None
    cats = _Categories(manifest, home, stage)
    out = []
    for item in manifest.get("items", []):
        if item.get("kind") not in ("file", "dir", "link"):
            continue
        dest, how, reason = rule.place(item) if rule else (_destination(item["source"], home), None, None)
        row = {"kind": item["kind"], "source": item["source"], "destination": display(dest, home),
               "category": cats.of(item)}
        if how == "withheld":
            row.update(state="withheld", reason=reason)
            out.append(row)
            continue
        if how == "staged":
            row.update(staged=True, label=rule.label(item, dest))
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
    if wanted is not None:
        keep = _with_folders(out, wanted)
        for row in out:
            if row["source"] not in keep and row["state"] != "withheld":
                row.pop("staged", None)
                row.pop("label", None)
                row.update(state="left-out", reason=LEFT_OUT)
    return out


def inspect_names(manifest: dict, home: Path) -> set[str]:
    """The archive names a second-machine plan reads before anything is written: the `.ssh` and job
    files (a private key's text, the carried `IdentityFile` lines, a plist's `Label`)."""
    return {i["arcname"] for i in manifest.get("items", [])
            if i.get("kind") == "file" and _safe_arcname(i.get("arcname")) and _area(i, home)[0] is not None}


def _aside(dest: Path, stamp: str) -> str:
    moved = dest.with_name(f"{dest.name}.before-machine-restore-{stamp}")
    os.replace(dest, moved)
    return moved.name


def apply(manifest: dict, stage: Path, home: Path, *, overwrite: bool = False, second: bool = False,
          wanted: set[str] | None = None) -> dict:
    """Write what is missing; with `overwrite`, replace a conflict after moving it aside. On a
    second machine, withhold every private SSH file and write jobs to the staging folder. With
    `wanted`, write only those categories (OBS-282)."""
    rows = plan(manifest, stage, home, second=second, wanted=wanted)
    by_source = {(i["kind"], i["source"]): i for i in manifest.get("items", [])}
    stamp = backup_vault.stamp()
    done = {"written": 0, "same": 0, "kept": 0, "replaced": 0, "failed": []}
    if second:
        done["withheld"] = 0
    if wanted is not None:
        done["left_out"] = 0
    rule = _Second(manifest, home, stage) if second else None
    order = {"dir": 0, "file": 1, "link": 2}
    for row in sorted(rows, key=lambda r: (order[r["kind"]], r["source"])):
        item = by_source[(row["kind"], row["source"])]
        dest, how, _ = rule.place(item) if rule else (_destination(item["source"], home), None, None)
        if row["state"] == "withheld":
            done["withheld"] += 1
            continue
        if row["state"] == "left-out":
            done["left_out"] += 1
            continue
        if row["state"] == "same":
            done["same"] += 1
            continue
        if row["state"] == "conflict" and not overwrite:
            done["kept"] += 1
            continue
        try:
            data = None
            if row["kind"] == "file":
                if not _safe_arcname(item.get("arcname")):
                    raise MachineBackupError("an unsafe name in the manifest")
                data = (stage / item["arcname"]).read_bytes()
                if _sha256(data) != item["sha256"]:
                    raise MachineBackupError("the copy in the bundle does not match its digest")
                if second and _area(item, home)[0] == "ssh" and looks_private(data):
                    done["withheld"] += 1  # a private key under a public name stays on its machine
                    continue
                if wanted is not None and "ssh-private" not in wanted and _area(item, home)[0] == "ssh" \
                        and looks_private(data):
                    done["left_out"] += 1  # read as a private key only now: still not what was asked for
                    continue
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
                if how == "staged":
                    machine_identity.ensure_private_dir(dest.parent, home)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(_link_target(item["target"], home), dest)
            else:
                if how == "staged":
                    machine_identity.ensure_private_dir(dest.parent, home)
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
    """Clone each recorded checkout that is not on this machine, then put back what was
    carried: every commit no remote had (branches, tags, a detached HEAD, stashes), the
    uncommitted patch and the untracked files with their modes. A repository that had no commit
    yet is initialised and its staged patch applied to the index. Plain git with the person's
    own configuration: a clone needs their SSH keys and credential helpers."""
    # No background `gc --auto` or maintenance in a restored repository, as in safe_git: a restore writes
    # objects and refs into checkouts a person owns, and a detached gc outlives the restore (and a test's
    # temporary folder).
    gitc = [git, "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
    out = []
    records = sorted(manifest.get("checkouts", []), key=lambda r: (bool(r.get("worktree")), bool(r.get("submodule"))))
    mains: dict[str, Path] = {}
    restored: list[Path] = []
    repo_bundle: dict[str, Path] = {}
    fetched: set[str] = set()
    for rec in records:
        if rec.get("error"):
            continue
        problem = validate_checkout(rec, home)
        if problem:
            out.append({"checkout": str(rec.get("path"))[:200], "result": "refused", "detail": problem})
            continue
        dest = _destination(rec["path"], home)
        row = {"checkout": display(dest, home)}
        out.append(row)
        carried = rec.get("carried") or {}
        bundle = stage / carried["bundle"] if carried.get("bundle") else None
        # Every file the record says was carried must be here; a missing one is a failure,
        # named, never a clone that quietly lacks its commits (round 2 of #305).
        missing = [name for name in ([carried.get("bundle"), carried.get("patch")]
                                     + [f"checkouts/{rec['id']}/untracked/{r}" for r in carried.get("untracked") or []])
                   if name and not (stage / name).is_file()]
        if missing:
            row.update(result="carried-missing", detail=f"not in the unpacked bundle: {', '.join(missing[:3])}")
            continue
        if bundle is not None and rec.get("common_dir"):
            repo_bundle.setdefault(rec["common_dir"], bundle)
        if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
            row["result"] = "exists"
            if rec.get("common_dir") and not rec.get("worktree"):
                mains[rec["common_dir"]] = dest
            continue
        remotes = rec.get("remotes") or {}
        url = remotes.get("origin") or next(iter(remotes.values()), None)
        main = mains.get(rec.get("common_dir")) if rec.get("worktree") else None
        detached = bool(rec.get("head")) and not rec.get("branch")
        if main is not None:
            # The main checkout may have been here before the restore: give it the carried
            # commits first, under refs/carried only, so its own branches are never moved.
            carry = repo_bundle.get(rec.get("common_dir"))
            if carry is not None and rec["common_dir"] not in fetched:
                fetched.add(rec["common_dir"])
                got = _run([*gitc, "-C", str(main), "fetch", "-q", str(carry), "+refs/carried/*:refs/carried/*",
                            "refs/tags/*:refs/tags/*"], runner=runner)
                if got["exit"] != 0:
                    row.update(result="clone-failed", detail="the carried commits could not be fetched into "
                                                             f"{display(main, home)}: {got.get('detail', '')}")
                    continue
                if main not in restored:
                    restored.append(main)
            ref = ["--detach", str(dest), rec["head"]] if detached else [str(dest), rec.get("branch") or rec["head"]]
            step_ = _run([*gitc, "-C", str(main), "worktree", "add", *ref], runner=runner)
        elif not rec.get("head"):
            dest.mkdir(parents=True, exist_ok=True)
            step_ = _run([*gitc, "init", "-q", "-b", rec.get("branch") or "main", str(dest)], runner=runner)
        elif url:
            step_ = _run([*gitc, "clone", "--", url, str(dest)], runner=runner)
        elif bundle is not None and bundle.is_file():
            step_ = _run([*gitc, "clone", "--", str(bundle), str(dest)], runner=runner)
        else:
            row["result"] = "nothing-to-clone-from"
            row["detail"] = "no remote, and no commits were carried"
            continue
        if step_["exit"] != 0:
            row.update(result="clone-failed", detail=step_.get("detail", ""))
            continue
        if main is None:
            restored.append(dest)
            if rec.get("common_dir"):
                mains[rec["common_dir"]] = dest
            for name, remote in remotes.items():
                if name != "origin" or not url or not rec.get("head"):
                    _run([*gitc, "-C", str(dest), "remote", "add", name, remote], runner=runner)
        steps = []
        if main is None and bundle is not None and bundle.is_file():
            steps.append(_run([*gitc, "-C", str(dest), "fetch", "--update-head-ok", str(bundle),
                               "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                               "+refs/carried/*:refs/carried/*"], runner=runner))
        if main is None:
            for name, sha in sorted((rec.get("tags") or {}).items()):
                _run([*gitc, "-C", str(dest), "tag", name, sha], runner=runner)
            # Oldest first, so the newest is stash@{0} again.
            for n in reversed(range(len(rec.get("stashes") or []))):
                entry = rec["stashes"][n]
                steps.append(_run([*gitc, "-C", str(dest), "stash", "store", "-m",
                                   entry.get("message") or "restored stash", entry["sha"]], runner=runner))
            if rec.get("head"):
                target = ["--detach", rec["head"]] if detached else [rec["branch"]]
                steps.append(_run([*gitc, "-C", str(dest), "checkout", "-q", *target, "--"], runner=runner))
        if rec.get("head"):
            steps.append(_run([*gitc, "-C", str(dest), "reset", "-q", "--hard", rec["head"], "--"], runner=runner))
        if carried.get("patch"):
            index = ["--index"] if not rec.get("head") else []
            steps.append(_run([*gitc, "-C", str(dest), "apply", "--binary", *index, str(stage / carried["patch"])],
                              runner=runner))
        written = 0
        modes = carried.get("untracked_modes") or {}
        for rel in carried.get("untracked") or []:
            target = dest / rel
            if target.exists():
                continue
            source = stage / "checkouts" / rec["id"] / "untracked" / rel
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                mode = modes.get(rel)
                os.chmod(target, (mode if isinstance(mode, int) else 0o644) & 0o777)
                written += 1
        failed = [s for s in steps if s["exit"] != 0]
        row["result"] = "cloned" if not failed else "cloned-with-problems"
        if failed:
            row["problems"] = failed
        if written:
            row["untracked_written"] = written
    # The carried names did their work: the commits are on branches, tags, HEADs and stashes.
    for dest in restored:
        p = subprocess.run([*gitc, "-C", str(dest), "for-each-ref", "--format=delete %(refname)", "refs/carried"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if p.returncode == 0 and p.stdout.strip():
            subprocess.run([*gitc, "-C", str(dest), "update-ref", "--stdin"], input=p.stdout,
                           capture_output=True, text=True)
    return out


def restore(bundle: Path, home: Path, *, base: Path | None = None, mode: str = "plan", overwrite: bool = False,
            do_packages: bool = False, do_clone: bool = False, secret: str | None = None,
            runner=subprocess.run, which=shutil.which, git: str = "git", hold=None,
            machine: str | None = None, only=None, skip=None, projects=None,
            stdin_value: str | None = None) -> dict:
    """Plan, and each requested step in its own phase: only what that step needs is decrypted,
    into the private stage, and removed before the next step starts (round 1 of #305).

    `machine` is the person's choice, `second` or `same`; without one the bundle's machine id
    against this machine's decides, and a bundle that names none is another machine's (OBS-283).
    `only` or `skip` (category names, CATEGORIES) limit what `--apply` writes and whether
    `--packages` runs, and `projects` limits `--clone` (OBS-282); both are checked before anything
    is written. Every step that writes leaves a receipt (`machine_identity.restore_receipt_path`)."""
    filtered = bool(only or skip)
    wanted = selection(only, skip) if filtered else None
    if do_packages and wanted is not None and "packages" not in wanted:
        raise MachineBackupError("--packages installs the `packages` category, which --only or --skip leaves out")
    if projects is not None and not [v for v in projects if str(v).strip(", ")]:
        raise MachineBackupError("--projects needs at least one checkout name or path")
    if not bundle.is_file():
        raise MachineBackupError(f"no machine bundle at {bundle}")
    if secret:
        header, opened_by = backup_vault.verify_file(bundle, secret), "given"
    else:
        secret, header, opened_by = secret_for(bundle, base, stdin_value)
    if header.get("kind") != KIND:
        raise MachineBackupError(f"{bundle.name} is a {header.get('kind')} backup, not a machine bundle")
    with _staging(stage_root(base)) as root:
        need = bundle.stat().st_size * 3
        if free_bytes(root) < need:
            raise DiskRefused(f"too little free space to unpack {bundle.name} (needs about {need / 1024 ** 2:.0f} MB)")
        with _phase(root) as stage:
            extract(bundle, secret, stage, lambda n: n in ("MANIFEST.json", "inventories/observatory.json"))
            try:
                manifest = json.loads((stage / "MANIFEST.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise MachineBackupError(f"{bundle.name} holds no readable MANIFEST.json") from None
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise MachineBackupError(f"{bundle.name} is not a machine bundle this engine reads")
            pointer = stage / "inventories" / "observatory.json"
            workspace_doc = json.loads(pointer.read_text(encoding="utf-8")) if pointer.is_file() else None
        try:
            placing = machine_identity.restore_mode(manifest.get("machine_id"), asked=machine, home=home)
        except machine_identity.MachineIdentityError as exc:
            raise MachineBackupError(str(exc)) from None
        second = placing["mode"] == "second-machine"
        chosen = None
        if projects is not None:
            chosen, unmatched = select_checkouts(manifest.get("checkouts", []), projects, home)
            if unmatched:
                raise MachineBackupError(f"no recorded checkout matches {', '.join(unmatched)}: name a checkout's "
                                         "folder, a glob over it, or a path; the plan lists them under `checkouts`")
        ssh_keys = other_keys = None
        if second or filtered:
            # The plan reads what it decides by: a `.ssh` file's text, the carried IdentityFile lines,
            # a plist's Label. Only those small files are decrypted, into the private stage.
            names = inspect_names(manifest, home)
            with _phase(root) as stage:
                extract(bundle, secret, stage, lambda n: n in names)
                rows = plan(manifest, stage, home, second=second, wanted=wanted)
                if second:
                    landing, replaced = _carried_config(manifest, rows, home, overwrite)
                    ssh_keys = _Second(manifest, home, stage).ssh_keys(
                        manifest, config_written=landing == "written", config_kept=(replaced or True) if landing == "kept"
                        else False)
                    other_keys = _other_keys(manifest, stage, home)
        else:
            rows = plan(manifest, None, home)
        counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("missing", "same", "conflict")}
        if second:
            counts["withheld"] = sum(1 for r in rows if r["state"] == "withheld")
            counts["staged"] = sum(1 for r in rows if r.get("staged"))
        if filtered:
            counts["left_out"] = sum(1 for r in rows if r["state"] == "left-out")

        def here(rec):
            try:
                return _destination(rec["path"], home).exists()
            except (MachineBackupError, KeyError, TypeError):
                return None
        picked = {id(r) for r in chosen} if chosen is not None else None
        notes = []
        if second and wanted is not None and "ssh-private" in wanted:
            notes.append("ssh-private: a second machine never gets a private SSH key, whatever --only names; "
                         "this machine makes its own (`ssh_keys` names each step), and only --same-machine at a "
                         "terminal restores the other machine's keys")
        result = {"bundle": bundle.name, "created_at": manifest.get("created_at"), "trigger": manifest.get("trigger"),
                  "from_platform": manifest.get("platform"), "mode": mode, "passphrase_source": opened_by,
                  "counts": counts,
                  "categories": category_summary(rows, wanted if wanted is not None else set(CATEGORIES), manifest,
                                                 overwrite=overwrite),
                  **({"selection": {"only": parse_categories(only), "skip": parse_categories(skip),
                                    "left_out": [c for c in CATEGORIES if c not in wanted]}} if filtered else {}),
                  **({"notes": notes} if notes else {}),
                  "plan": rows,
                  "not_carried": manifest.get("not_carried", []),
                  "checkouts": [{"path": r.get("path"), "branch": r.get("branch"), "unpushed": r.get("unpushed"),
                                 "dirty": r.get("dirty"), "stashes": len(r.get("stashes") or []),
                                 "carried": sorted(k for k in (r.get("carried") or {}) if k != "untracked_modes"),
                                 "remotes": sorted((r.get("remotes") or {}).keys()), "exists_here": here(r),
                                 **({"selected": id(r) in picked} if picked is not None else {})}
                                for r in manifest.get("checkouts", []) if isinstance(r, dict)],
                  "inventories": manifest.get("inventories", []), "machine": placing,
                  **({"ssh_keys": ssh_keys} if second else {})}
        if workspace_doc is not None:
            result["workspace"] = workspace_doc
        receipt: dict = {}
        if mode == "apply":
            with _phase(root) as stage:
                extract(bundle, secret, stage, lambda n: n.startswith(("files/", "abs/")))
                if hold:
                    hold("apply", stage)
                result["applied"] = apply(manifest, stage, home, overwrite=overwrite, second=second, wanted=wanted)
                if second and (wanted is None or "launchd" in wanted):
                    result["placement"] = place_staged(manifest, stage, home, placing)
            done = result["applied"]
            receipt["apply"] = {
                "bundle": bundle.name, "created_at": manifest.get("created_at"),
                "engine_version": manifest.get("engine_version"), "mode": placing["mode"],
                "taken_on": placing.get("taken_on"), "this_machine": placing.get("this_machine"),
                "only": parse_categories(only), "skip": parse_categories(skip),
                "left_out": [c for c in CATEGORIES if wanted is not None and c not in wanted],
                "counts": {k: v for k, v in done.items() if k != "failed"}, "failed": len(done["failed"]),
                "placement_error": bool((result.get("placement") or {}).get("error")),
                **({"other_keys": other_keys or []} if second else {})}
        if do_packages:
            with _phase(root) as stage:
                extract(bundle, secret, stage, lambda n: n.startswith("inventories/"))
                result["packages"] = packages(stage, runner=runner, which=which)
            receipt["packages"] = {"bundle": bundle.name, "ran": len(result["packages"]),
                                   "failed": sum(1 for r in result["packages"] if r.get("exit") != 0)}
        if do_clone:
            with _phase(root) as stage:
                extract(bundle, secret, stage, lambda n: n.startswith("checkouts/"))
                if hold:
                    hold("clone", stage)
                part = manifest if chosen is None else {**manifest, "checkouts": chosen}
                result["clone"] = clone(part, stage, home, runner=runner, git=git)
            result["failed"] = [r for r in result["clone"] if r.get("result") not in ("cloned", "exists")]
            receipt["clone"] = {"bundle": bundle.name, "projects": sorted(str(p) for p in projects or []),
                                "checkouts": len(result["clone"]), "failed": len(result["failed"]),
                                "failed_checkouts": [r["checkout"] for r in result["failed"]][:20]}
        if receipt:
            try:
                result["receipt"] = display(machine_identity.record_restore(receipt, home), home)
            except (machine_identity.MachineIdentityError, OSError) as exc:
                result["receipt_error"] = f"the restore receipt was not written: {type(exc).__name__}"
    # The same filters again, so a step names exactly what the person chose (OBS-282).
    keep = " ".join(f"--{flag}={shlex.quote(','.join(parse_categories(v)))}"
                    for flag, v in (("only", only), ("skip", skip)) if v)
    named = f"--projects={shlex.quote(','.join(str(p) for p in projects))}" if projects else ""
    after_keys = (", after the `ssh-keys rekey ... --apply` steps above make this machine's own SSH keys)"
                  if second and any(k["steps"] for k in ssh_keys or ()) else ")")
    result["next"] = [s for s in (
        None if mode == "apply" else
        (f"project-observatory full machine-restore --apply {keep}  (writes only what is missing)" if keep
         else "project-observatory full machine-restore --apply  (writes only what is missing)"),
        None if not counts["conflict"] or overwrite else
        (f"project-observatory full machine-restore --apply --overwrite {keep}  (replaces conflicts, moving each aside)"
         if keep else "project-observatory full machine-restore --apply --overwrite  (replaces conflicts, moving each aside)"),
        None if do_packages or (wanted is not None and "packages" not in wanted)
        else "project-observatory full machine-restore --packages  (Homebrew, npm, pipx, uv)",
        *((step for key in ssh_keys for step in key["steps"]) if second and not do_clone else ()),
        None if do_clone else
        (f"project-observatory full machine-restore --clone {named}  (the recorded checkouts" + after_keys if named
         else "project-observatory full machine-restore --clone  (the recorded checkouts" + after_keys),
        "project-observatory full restore --latest  (the workspace itself, from its own snapshot)",
        *(SECOND_MACHINE_NEXT if second else ())) if s]
    return result


#: What a person does after a second-machine restore, in order (ONBOARDING, "A second machine").
SECOND_MACHINE_NEXT = (
    "project-observatory full node --init  (after `full restore --latest`: this machine's own device id)",
    "project-observatory full machine-move show  (the jobs staged here, each named as running on the other machine)",
    "on the other machine: project-observatory full machine-move leave LABEL --to MACHINE_ID (this machine's id), then here: "
    "project-observatory full machine-move cutover LABEL  (moves one job, so it never runs on both)",
)


def place_staged(manifest: dict, stage: Path, home: Path, placing: dict) -> dict:
    """Name every job this restore staged in the placement file, as running on the machine the
    bundle was taken on, with its Fabric service id when a carried descriptor names its label.
    A job whose definition this machine already has in its own job folder is left as it is."""
    descriptors = [stage / i["arcname"] for i in manifest.get("items", [])
                   if i.get("kind") == "file" and i.get("category") == "fabric" and _safe_arcname(i.get("arcname"))
                   and str(i.get("source", "")).endswith(".json")]
    rule = _Second(manifest, home, stage)
    services = machine_identity.service_ids_by_label([f for f in descriptors if f.is_file()])
    entries, here, staged = [], [], []
    for item in manifest.get("items", []):
        if item.get("kind") not in ("file", "link"):
            continue
        dest, how, _ = rule.place(item)
        if how != "staged" or not os.path.lexists(dest):
            continue
        label = rule.label(item, dest)
        staged.append({"label": label, "file": display(dest, home)})
        if os.path.lexists(_destination(item["source"], home)):
            here.append(label)
            continue
        entries.append({"label": label, "serviceId": services.get(label),
                        "placedOn": placing.get("taken_on") or "unknown", "by": "machine-restore"})
    try:
        path = machine_identity.place_elsewhere(entries, home, machine=placing["this_machine"])
    except (machine_identity.MachineIdentityError, OSError) as exc:
        return {"file": None, "error": str(exc)[:300], "staged": staged}
    return {"file": display(path, home), "elsewhere": sorted(e["label"] for e in entries),
            "already_here": sorted(here), "staged": staged}


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
    machine = ap.add_mutually_exclusive_group()
    machine.add_argument("--second-machine", action="store_true",
                         help="restore as another machine (the default when the bundle names another machine or "
                              "none): no private SSH key is written, and every launchd job is staged, not loaded, "
                              "until `full machine-move cutover LABEL`")
    machine.add_argument("--same-machine", action="store_true",
                         help="restore as the machine the bundle was taken on, private SSH keys and jobs included; "
                              "only at a terminal")
    part = ap.add_mutually_exclusive_group()
    part.add_argument("--only", action="append", type=_category_arg, metavar="CATEGORY[,CATEGORY]",
                      help="write only these categories (repeatable): " + ", ".join(CATEGORIES)
                           + "; a second machine still never gets a private SSH key")
    part.add_argument("--skip", action="append", type=_category_arg, metavar="CATEGORY[,CATEGORY]",
                      help="write every category but these (repeatable)")
    ap.add_argument("--projects", action="append", metavar="NAME|GLOB|PATH[,...]",
                    help="with --clone: only these checkouts (a folder name, a glob over it, or a path that "
                         "names every checkout under it); a worktree brings its main checkout")
    ap.add_argument("--passphrase-stdin", action="store_true",
                    help="read the bundle's passphrase (another machine's) from stdin once, a file or a pipe, "
                         "never a terminal; used for this restore and stored nowhere")
    ap.epilog = "categories: " + "; ".join(f"{k}: {v}" for k, v in CATEGORY_HELP.items())
    return ap


def _category_arg(value: str) -> str:
    try:
        parse_categories([value])
    except MachineBackupError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return value


def _home_or_none() -> Path | None:
    try:
        base = config.home()
        return base if (base / "workspace.json").is_file() else None
    except config.ConfigurationError:
        return None


def backup_main(argv: list[str]) -> int:
    a = backup_parser().parse_args(argv)
    raise_on_signals()
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
    raise_on_signals()
    try:
        # Read once, before any other work (OBS-348): a refused value stops the command untouched.
        stdin_value = backup_vault.read_passphrase_stdin() if a.passphrase_stdin else None
        if a.overwrite and not a.apply:
            raise MachineBackupError("--overwrite replaces files, so it needs --apply as well")
        if bool(a.bundle) == bool(a.latest):
            raise MachineBackupError("name one bundle, or pass --latest")
        if a.packages and (a.only or a.skip) and "packages" not in selection(a.only, a.skip):
            raise MachineBackupError("--packages installs the `packages` category, which --only or --skip leaves out")
        base = _home_or_none()
        bundle = a.bundle.expanduser() if a.bundle else find_latest(base)
        if bundle is None:
            raise MachineBackupError("no machine bundle was found in the backups roots this machine knows; "
                                     "copy one here and name it")
        # A terminal the person types in (a real console on Windows, where NUL also reads as a tty).
        if a.same_machine and not osprivacy.is_terminal():
            raise MachineBackupError("--same-machine writes the bundle's private SSH keys and its jobs here, so it is "
                                     "a person's choice at a terminal; without it a bundle from another machine is "
                                     "restored as a second machine")
        result = restore(bundle, Path.home(), base=base, mode="apply" if a.apply else "plan",
                         overwrite=a.overwrite, do_packages=a.packages, do_clone=a.clone,
                         machine="second" if a.second_machine else ("same" if a.same_machine else None),
                         only=a.only, skip=a.skip, projects=a.projects, stdin_value=stdin_value)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        for note in result.get("notes") or []:
            print(f"Observatory: {note}", file=sys.stderr)
        if result.get("receipt_error"):
            print(f"Observatory: {result['receipt_error']}", file=sys.stderr)
        failed = (result.get("applied") or {}).get("failed") or result.get("failed")
        if (result.get("placement") or {}).get("error"):
            print("Observatory: the staged jobs were not named in the placement file: "
                  f"{result['placement']['error']}", file=sys.stderr)
            return 1
        if failed:
            print(f"Observatory: {len(failed)} item(s) were not restored; each is named in the answer", file=sys.stderr)
        return 1 if failed else 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
