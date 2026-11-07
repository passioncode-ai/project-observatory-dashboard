#!/usr/bin/python3
"""Run a headless space's background jobs as system-domain daemons that run as that account (OBS-361).

WHY. On macOS an OS account with no graphical login has no `gui/<uid>` launchd domain, and
`launchctl bootstrap user/<uid> PLIST` refuses agents (exit 5) even as root. So a job staged for such
an account (a second-machine restore into a "space" nobody logs in to) can never start as an agent,
and asking a person to sign in after every boot is a human gate per reboot. launchd.plist(5) gives
the way out: `UserName` and `GroupName` apply in the privileged system domain, so the same job, written
to /Library/LaunchDaemons with `UserName` set to the space's account, runs as that account (never as
root) and comes back after a reboot by itself.

WHO RUNS IT. An administrator, once per job, with sudo:

    sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons apply --user ACCOUNT --label LABEL

`-I` (isolated mode) is part of the command: Python then leaves the script's folder and the user's
site-packages out of its module path and ignores every PYTHON* variable, so no module the space's
account planted is imported as root. `apply` and `remove` refuse to run without it (`not-isolated`).

Root must never run a file the space's account can write, so this script is not run from the engine's
folder. The administrator installs a root-owned copy once, from the release downloaded and verified in
the administrator's OWN account, never copied from the space account's folders:

    gh release download vX.Y.Z -R passioncode-ai/project-observatory-dashboard -p 'SHA256SUMS*' -p '*.whl'
      (without gh: curl -LO https://github.com/passioncode-ai/project-observatory-dashboard/releases/download/vX.Y.Z/ASSET
       for SHA256SUMS, SHA256SUMS.asc and project_observatory-X.Y.Z-py3-none-any.whl)
    shasum -a 256 -c SHA256SUMS --ignore-missing
    curl -LO https://raw.githubusercontent.com/passioncode-ai/.github/main/release-signing/passioncode-release-signing.asc
    gpg --import passioncode-release-signing.asc     (gpg is not on macOS by default: brew install gnupg)
    gpg --status-fd 1 --verify SHA256SUMS.asc SHA256SUMS | grep -q "VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7"
      (without gpg, the administrator's own Observatory install checks the same pinned key:
       python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import release_signature as r;
       sys.exit(r.verify(open("SHA256SUMS", "rb").read(), open("SHA256SUMS.asc").read()) != sys.argv[2])'
       "$(project-observatory full-path)" 63B30DC324BD697487AA31944FAFB8AEC803B6A7)
    unzip -p project_observatory-X.Y.Z-py3-none-any.whl observatory/engine/tools/space_daemons.py > space_daemons.py
    sudo install -d -o root -g wheel -m 0755 /usr/local/libexec
    sudo install -o root -g wheel -m 0755 space_daemons.py /usr/local/libexec/observatory-space-daemons

`--version` prints the version and this file's sha256 for information; the verification is above.

`apply` and `remove` refuse to run from a copy that is not owned by root, is writable by group or
others, is a symbolic link, or sits in a folder anyone but root can write. `plan`, `status` and
`--version` work from any copy. That check runs after Python has read this file's imports: with `-I`
the script's folder is not searched for modules, and the folder must still be root's alone before the
copy goes in, which the administrator checks (`ls -ld /usr/local /usr/local/libexec`).

    space_daemons.py --version                       the version and the sha256 of this file (information)
    space_daemons.py plan   --user NAME [--from background|staged] [--label L ...] [--json]
    space_daemons.py apply  --user NAME [--label L ...] [--json]          (root)
    space_daemons.py remove --user NAME --label L [--json]                (root)
    space_daemons.py status --user NAME [--json]
    space_daemons.py trust show [--user NAME] [--json]
    space_daemons.py trust set --owner NAME ... --group NAME ... [--json]   (root)

TRUSTED OWNERS (1.1.0). A program path normally belongs to root or the account. Where Homebrew's prefix is
owned by the host's admin account and group-writable by `admin`, the administrator lists those in the
root-owned trust file /Library/Application Support/ProjectObservatory/space-daemons-trust.json,
{"schema": "observatory-space-daemons-trust/1", "owners": [...], "groups": [...]}. Only root-equivalents
belong there: groups wheel or admin, owners that are members of admin (dsmemberutil, checked when set and
again at every read). Any process running as a trusted admin, even without its sudo password, can replace
those programs and so run code as every space that uses them: trust only when spaces are less trusted than
the admin. A file that is not root's alone is ignored with a warning; the space account itself is never
trusted for its own jobs.

WHAT IT READS. The account's background folder, `<home>/.local/share/project-observatory/background/
LaunchAgents/` (`full machine-move cutover` puts a job there when the account has no GUI session), or,
for `plan --from staged`, the staging folder `.../machine-move/LaunchAgents/`. Root cannot see the
account's XDG_DATA_HOME, so a space with a custom XDG_DATA_HOME is not supported here (part 1). Root
reads a definition only when the folder and the file are the account's own and writable by nobody
else, the file is a regular file opened without following a link, and it is at most 64 KiB.

WHAT IT WRITES. Only a definition that passes every rule of `validate`: the allowed keys, a program the
account or root owns in folders nobody else can write, a WorkingDirectory inside the account's home. The daemon
plist is those keys plus `UserName`, `GroupName`, `InitGroups` and HOME, USER and LOGNAME in its
environment, written atomically to /Library/LaunchDaemons/<label>.plist (root:wheel 0644), then loaded
with `launchctl bootstrap system`. A file it did not write is never replaced: what it wrote is recorded
in /Library/Application Support/ProjectObservatory/space-daemons.json (root:wheel 0644), a stable
interface other tools read without root: {"schema": "observatory-space-daemons/1", "entries": [{label,
user, uid, source, sourceSha256, written, writtenSha256, at}]}. One copy only: a label loaded as an agent
in the account's `gui/<uid>` domain (`loaded-in-gui`), or held by another account's entry, is refused.

LOGS. launchd may open a daemon's StandardOutPath and StandardErrorPath with root's rights, after
this tool's check: a path in a folder the account controls could be swapped for a link and make root
write elsewhere. So the definition's own log paths are ignored (`plan` notes `logs-moved`) and the
daemon writes to /Library/Logs/ProjectObservatory/<user>/<label>.out.log and .err.log, a folder root
owns (root:wheel 0700, under a root:wheel 0755 one) with one inherited ACL entry that lets the account
read it. Both files are created ahead of launchd, owned by the account, mode 0600: the account reads and
appends to them but cannot create, rename, link or unlink in the folder, so they are safe whichever
credentials launchd opens them with. WorkingDirectory stays as validated (inside the home, its final
component not a link).

Standard library only, and Python 3.9 (macOS's /usr/bin/python3): nothing here imports the engine.

Exit codes: 0 done; 1 a definition was refused or a step failed (each is named); 2 refused before
anything changed (not root, not the root-owned copy, an unknown account, an unreadable folder).
"""
import argparse
import datetime
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile

try:
    import fcntl
    import grp
    import pwd
except ImportError:  # Windows: the module still imports, so its pure rules can be tested there.
    fcntl = grp = pwd = None

SPACE_DAEMONS_VERSION = "1.1.0"
REGISTRY_SCHEMA = "observatory-space-daemons/1"
DAEMONS_DIR = "Library/LaunchDaemons"
REGISTRY_DIR = "Library/Application Support/ProjectObservatory"
REGISTRY_NAME = "space-daemons.json"
#: The optional trust file (1.1.0): accounts and groups that already hold root through sudo, whose files and
#: folders a program path may sit in (Homebrew's prefix, owned by the host's admin and group-writable by
#: `admin`). Root-owned, beside the registry.
TRUST_SCHEMA = "observatory-space-daemons-trust/1"
TRUST_NAME = "space-daemons-trust.json"
#: The only groups a trust file may name: their members hold root through sudo already.
TRUSTED_GROUPS = ("wheel", "admin")
DSMEMBERUTIL = "/usr/sbin/dsmemberutil"
DS_MEMBER = "user is a member of the group"
DS_NOT_MEMBER = "user is not a member of the group"
MAX_TRUST_BYTES = 64 * 1024
INSTALLED_COPY = "/usr/local/libexec/observatory-space-daemons"
#: The organization's release key, pinned: the engine's `release_signature.PINNED` holds the same one (a
#: test keeps the two equal); this standalone tool imports nothing from the engine, so it is written here.
RELEASE_KEY_FINGERPRINT = "63B30DC324BD697487AA31944FAFB8AEC803B6A7"
RELEASE_KEY_URL = ("https://raw.githubusercontent.com/passioncode-ai/.github/main/release-signing/"
                   "passioncode-release-signing.asc")
#: The only way root runs the copy: isolated mode, so nothing from the script's folder, the user's
#: site-packages or a PYTHON* variable is imported or honoured.
INSTALLED_COMMAND = "/usr/bin/python3 -I " + INSTALLED_COPY
LAUNCHCTL = "/bin/launchctl"
CHMOD = "/bin/chmod"
LS = "/bin/ls"
#: One ACL entry as `ls -lde` prints it: " 0: user:NAME [inherited ]allow|deny PERMISSIONS".
ACL_LINE = re.compile(r"^\s*\d+:\s+((?:user|group):\S+ (?:inherited )?(?:allow|deny) \S+)\s*$")
SYSTEM_DAEMONS_DIR = "System/Library/LaunchDaemons"
LOGS_DIR = "Library/Logs/ProjectObservatory"
#: The one ACL entry the account's log folder carries: read, inherited by every log inside.
LOG_ACL = "%s allow list,search,read,readattr,readextattr,readsecurity,file_inherit,directory_inherit"
#: The account's data folder as root sees it: `machine_identity.data_dir` without XDG_DATA_HOME.
DATA_PARTS = (".local", "share", "project-observatory")
BACKGROUND_PARTS = ("background", "LaunchAgents")
STAGED_PARTS = ("machine-move", "LaunchAgents")
MAX_DEFINITION_BYTES = 64 * 1024
FIRST_PERSON_UID = 500

#: Test seams. They take effect only when the switch is "1" AND the process is not root, so no
#: ordinary environment variable can redirect a root write in production.
TEST_SWITCH = "OBSERVATORY_SPACE_DAEMONS_FOR_TESTS"
TEST_ROOT = "OBSERVATORY_SPACE_DAEMONS_TEST_ROOT"
TEST_HOME = "OBSERVATORY_SPACE_DAEMONS_TEST_HOME"
TEST_LAUNCHCTL = "OBSERVATORY_SPACE_DAEMONS_TEST_LAUNCHCTL"

#: The label rule `apply` and the engine's headless `machine-move cutover` share (always `fullmatch`):
#: letters and digits, then . _ @ - (Homebrew's `homebrew.mxcl.postgresql@14` among them), at most 128.
LABEL_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
#: Every integer a daemon carries fits launchd's 32-bit fields.
INT_MAX = 2 ** 31 - 1
PROCESS_TYPES = ("Background", "Standard", "Adaptive", "Interactive")
CALENDAR_KEYS = ("Minute", "Hour", "Day", "Weekday", "Month")
KEEPALIVE_BOOLS = ("SuccessfulExit", "Crashed", "AfterInitialDemand")
#: PathState only, its paths inside the account's home. OtherJobEnabled is refused: it would tie this
#: daemon to another account's or the system's jobs.
KEEPALIVE_PATHS = ("PathState",)
#: Keys copied from an agent definition. Everything else (UserName, GroupName, InitGroups,
#: SessionCreate, LaunchEvents, MachServices, Sockets, WatchPaths, QueueDirectories, RootDirectory,
#: HardResourceLimits, ...) is refused: in the system domain they would act with root's reach. Umask and
#: SoftResourceLimits (1.1.0) only narrow what the account's own process may do.
ALLOWED_KEYS = ("Label", "ProgramArguments", "Program", "EnvironmentVariables", "WorkingDirectory",
                "RunAtLoad", "KeepAlive", "StartInterval", "StartCalendarInterval", "StandardOutPath",
                "StandardErrorPath", "ThrottleInterval", "ProcessType", "Nice", "LowPriorityIO", "ExitTimeOut",
                "AbandonProcessGroup", "LimitLoadToSessionType", "Umask", "SoftResourceLimits",
                "AssociatedBundleIdentifiers")
#: Allowed, and left out of the daemon (`plan` notes `stripped`): a session type and the bundles a job
#: is shown under in System Settings do not apply to a system daemon of another account.
DROPPED_KEYS = ("LimitLoadToSessionType", "AssociatedBundleIdentifiers")
#: launchd.plist(5)'s resource limit names; each value a non-negative integer.
RESOURCE_LIMIT_KEYS = ("Core", "CPU", "Data", "FileSize", "MemoryLock", "NumberOfFiles", "NumberOfProcesses",
                       "ResidentSetSize", "Stack")
#: Checked inside the home. The log paths are not: they are replaced by a root-owned folder's (LOGS).
PATH_KEYS = ("WorkingDirectory",)
LOG_KEYS = ("StandardOutPath", "StandardErrorPath")


class SpaceDaemonsError(Exception):
    """The command was refused before anything changed. The message names why and what to do; `code`
    names the refusal for a reader of --json."""

    def __init__(self, message, code=None):
        Exception.__init__(self, message)
        self.code = code


class Account(object):
    """The space's OS account as root sees it: name, uid, primary gid and its group's name, home."""

    def __init__(self, name, uid, gid, group, home):
        self.name, self.uid, self.gid, self.group, self.home = name, uid, gid, group, home

    def data_dir(self):
        return os.path.join(self.home, *DATA_PARTS)

    def background_dir(self):
        return os.path.join(self.data_dir(), *BACKGROUND_PARTS)

    def staged_dir(self):
        return os.path.join(self.data_dir(), *STAGED_PARTS)


class Trust(object):
    """What the trust file adds for one account: uids and gids whose files and folders a program path may
    sit in, the names behind them, and the warnings of entries (or a whole file) that were not trusted."""

    def __init__(self, uids=(), gids=(), owners=(), groups=(), warnings=(), path=None):
        self.uids, self.gids = set(uids), set(gids)
        self.owners, self.groups = list(owners), list(groups)
        self.warnings, self.path = list(warnings), path

    def as_dict(self):
        return {"path": self.path, "owners": self.owners, "groups": self.groups, "warnings": self.warnings}


class System(object):
    """Where the system's files live and how commands run; every seam a test needs, injected.

    `root` prefixes /Library/LaunchDaemons and the registry; `runner` stands in for subprocess.run;
    `euid` for os.geteuid(); `set_owner(fd or path)` makes a written file or folder root:wheel; `self_check()` answers
    the problems of the copy being run (an empty list when it is the root-owned one); `owner_uid` is
    who must own the system folders it writes into (root; a test's own uid in a temporary root);
    `isolated` whether Python runs in isolated mode (`-I`); `chown_fd` gives a log file to the account."""

    def __init__(self, root="/", runner=None, euid=None, set_owner=None, self_check=None, launchctl=LAUNCHCTL,
                 owner_uid=0, isolated=None, chmod=CHMOD, chown_fd=None):
        self.root = root
        self.isolated = (sys.flags.isolated == 1) if isolated is None else isolated
        self.chmod = chmod
        #: chown_fd(fd, uid, gid): hands a pre-created log file to the account (os.fchown).
        self.chown_fd = chown_fd or os.fchown
        self.owner_uid = owner_uid
        self.runner = runner or subprocess.run
        if euid is None:
            euid = os.geteuid() if hasattr(os, "geteuid") else -1
        self.euid = euid
        self.set_owner = set_owner or _own_root
        self.self_check = self_check or (lambda: check_own_copy(os.path.abspath(__file__)))
        self.launchctl = launchctl

    def daemons_dir(self):
        return os.path.join(self.root, DAEMONS_DIR)

    def daemon_path(self, label):
        return os.path.join(self.daemons_dir(), label + ".plist")

    def registry_dir(self):
        return os.path.join(self.root, REGISTRY_DIR)

    def registry_path(self):
        return os.path.join(self.registry_dir(), REGISTRY_NAME)

    def trust_path(self):
        return os.path.join(self.registry_dir(), TRUST_NAME)

    def log_dir(self, user):
        return os.path.join(self.root, LOGS_DIR, user)

    def run(self, args):
        """launchctl with ARGS: (exit code, last line of its error). A launchctl that is not there answers 127."""
        return self.command([self.launchctl] + list(args))

    def command(self, argv):
        """ARGV through the runner: (exit code, last line of its error); 127 when it could not start."""
        code, _out, tail = self.output(argv)
        return code, tail

    def output(self, argv):
        """ARGV through the runner: (exit code, its output, last line of its error)."""
        try:
            p = self.runner(list(argv), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, "", type(exc).__name__
        tail = ((p.stderr or "").strip().splitlines() or [""])[-1][:200]
        return p.returncode, p.stdout or "", tail

    def label_dirs(self):
        """The folders whose plists hold the system domain's labels."""
        return [self.daemons_dir(), os.path.join(self.root, SYSTEM_DAEMONS_DIR)]

    def loaded(self, label):
        return self.run(["print", "system/" + label])[0] == 0


def _own_root(target):
    if isinstance(target, int):
        os.fchown(target, 0, 0)
    else:
        os.chown(target, 0, 0)


def _testing():
    return os.environ.get(TEST_SWITCH) == "1" and hasattr(os, "geteuid") and os.geteuid() != 0


def system_from_environment():
    """The production System, or, only under the test switch in a process that is not root, one whose
    system root and launchctl the tests chose."""
    if _testing():
        return System(root=os.environ.get(TEST_ROOT) or "/",
                      launchctl=os.environ.get(TEST_LAUNCHCTL) or LAUNCHCTL)
    return System()


def registry_path():
    """Where the registry is, for a reader (the engine's `machine-move`) that never writes it."""
    return system_from_environment().registry_path()


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the account ------------------------------------------------------------------------------------


def lookup_account(name, getpwnam=None, getgrgid=None):
    """The account NAME, refused when it is root or a system account (uid below 500)."""
    if pwd is None and getpwnam is None:
        raise SpaceDaemonsError("this tool runs on macOS: there is no account database here")
    getpwnam = getpwnam or pwd.getpwnam
    getgrgid = getgrgid or grp.getgrgid
    try:
        entry = getpwnam(name)
    except KeyError:
        raise SpaceDaemonsError("no account named %r on this computer" % name)
    if entry.pw_uid == 0:
        raise SpaceDaemonsError("%s is root: a space's jobs never run as root" % name)
    if entry.pw_uid < FIRST_PERSON_UID:
        raise SpaceDaemonsError("%s is a system account (uid %d, below %d): only a person's or a space's account "
                                "takes jobs" % (name, entry.pw_uid, FIRST_PERSON_UID))
    try:
        group = getgrgid(entry.pw_gid).gr_name
    except KeyError:
        raise SpaceDaemonsError("the primary group (gid %d) of %s has no name" % (entry.pw_gid, name))
    home = entry.pw_dir
    if _testing() and os.environ.get(TEST_HOME):
        home = os.environ[TEST_HOME]
    if not os.path.isabs(home):
        raise SpaceDaemonsError("the home of %s is not an absolute path" % name)
    return Account(name, entry.pw_uid, entry.pw_gid, group, home)


# --- the copy being run -------------------------------------------------------------------------------


def _writable_by_others(info, allowed_uids, allowed_gids=(0,)):
    """Whether someone other than ALLOWED_UIDS (and the ALLOWED_GIDS, root's group and the trusted ones) can
    change what INFO describes. A world- or group-writable folder with the sticky bit lets nobody replace
    another owner's entry."""
    mode = info.st_mode
    if info.st_uid not in allowed_uids:
        return True
    sticky = bool(mode & stat.S_ISVTX) and stat.S_ISDIR(mode)
    if mode & stat.S_IWOTH and not sticky:
        return True
    if mode & stat.S_IWGRP and not sticky and info.st_gid not in allowed_gids:
        return True
    return False


def _folders_up(path):
    """PATH's folders from its parent up to /."""
    out, current = [], os.path.dirname(path)
    while True:
        out.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return out
        current = parent


def check_own_copy(path):
    """The problems of the copy at PATH as one root may run: [] when it is a regular root-owned file,
    writable only by root, in folders only root can write."""
    problems = []
    try:
        info = os.lstat(path)
    except OSError as exc:
        return ["the copy %s cannot be read (%s)" % (path, type(exc).__name__)]
    if stat.S_ISLNK(info.st_mode):
        return ["the copy %s is a symbolic link" % path]
    if not stat.S_ISREG(info.st_mode):
        problems.append("the copy %s is not a regular file" % path)
    if info.st_uid != 0:
        problems.append("the copy %s is not owned by root" % path)
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        problems.append("the copy %s is writable by group or others" % path)
    for folder in _folders_up(os.path.realpath(path)):
        try:
            finfo = os.stat(folder)
        except OSError:
            problems.append("the folder %s cannot be read" % folder)
            continue
        if finfo.st_uid != 0 or finfo.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            problems.append("the folder %s can be written by an account other than root" % folder)
    return problems


# --- reading definitions -------------------------------------------------------------------------------


def _problem(code, detail=""):
    return {"code": code, "detail": detail}


def _check_folder(folder, account):
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SpaceDaemonsError("the folder %s cannot be read (%s)" % (folder, type(exc).__name__))
    if not stat.S_ISDIR(info.st_mode):
        raise SpaceDaemonsError("%s is not a folder (a link is refused)" % folder)
    if info.st_uid != account.uid:
        raise SpaceDaemonsError("the folder %s is not owned by %s" % (folder, account.name))
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SpaceDaemonsError("the folder %s is writable by group or others: `chmod go-w` it as %s"
                                % (folder, account.name))
    return True


def _read_one(path, account):
    """(bytes, problems) of one definition file, opened without following a link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path):
            return None, [_problem("symlink", "a definition is never read through a link")]
        return None, [_problem("unreadable", type(exc).__name__)]
    try:
        info = os.fstat(fd)
        problems = []
        if not stat.S_ISREG(info.st_mode):
            problems.append(_problem("not_regular_file"))
        if info.st_uid != account.uid:
            problems.append(_problem("not_owned_by_account", "owner uid %d" % info.st_uid))
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            problems.append(_problem("writable_by_others"))
        if info.st_size > MAX_DEFINITION_BYTES:
            problems.append(_problem("too_large", "%d bytes, at most %d" % (info.st_size, MAX_DEFINITION_BYTES)))
        if problems:
            return None, problems
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DEFINITION_BYTES:
                return None, [_problem("too_large", "grew while read")]
            chunks.append(chunk)
        return b"".join(chunks), []
    finally:
        os.close(fd)


def read_definitions(folder, account, labels=None, log_dir=None, trust=None):
    """Every `*.plist` in FOLDER, each with its label, file, problems and the daemon it would become, its
    logs in LOG_DIR (default the production one) and `notes` (`logs-moved` when it named its own)."""
    if not _check_folder(folder, account):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if name.startswith(".") or not name.endswith(".plist"):
            continue
        path = os.path.join(folder, name)
        data, problems = _read_one(path, account)
        doc, daemon, source = None, None, None
        if data is not None:
            source = _sha256_bytes(data)
            doc, code, detail = parse_plist_detail(data)
            if code is not None:
                problems.append(_problem(code, detail))
        notes = []
        if doc is not None:
            found, daemon = validate(doc, name, account, log_dir, trust)
            problems.extend(found)
            notes = definition_notes(doc)
        label = doc.get("Label") if isinstance(doc, dict) and isinstance(doc.get("Label"), str) else name[:-6]
        if labels and label not in labels:
            continue
        out.append({"label": label, "file": path, "source_sha256": source,
                    "verdict": "ok" if not problems else sorted(set(p["code"] for p in problems)),
                    "problems": problems, "daemon": None if problems else daemon, "notes": notes})
    return out


def strip_xml_comments(data):
    """(DATA without its XML comments, whether every comment closed), in one linear pass: find `<!--`, then
    `-->` from there, keep the text between comments. A comment that never closes stops the pass at once."""
    out, i = [], 0
    while True:
        start = data.find(b"<!--", i)
        if start < 0:
            out.append(data[i:])
            return b"".join(out), True
        out.append(data[i:start])
        end = data.find(b"-->", start + 4)
        if end < 0:
            return b"".join(out), False
        i = end + 3


def parse_plist_detail(data):
    """(the definition as a dict, None, "") or (None, a problem code, why) from bytes already read once.

    launchd and plutil accept an XML comment holding `--` (`<!-- run with --install-launchd -->`), which XML
    forbids and expat refuses. So an XML plist plistlib refuses is parsed once more with its comments removed
    (strip_xml_comments, on the bytes): a comment's text is never parsed, so nothing inside one becomes a key.
    This is close to launchd's reading, not proven identical, so a plist that also holds a CDATA section (where
    `<!--` is text, not a comment) is refused rather than guessed. The daemon written is regenerated from what was
    parsed, never these bytes; `sourceSha256` stays the hash of the original bytes. A binary plist is parsed as
    it is. Codes: `unreadable_plist`, `xml_comment` (a comment that does not close, or comments beside CDATA),
    `not_a_dictionary`."""
    try:
        doc = plistlib.loads(data)
    except Exception:  # plistlib raises several types for a file that is not a plist
        if not data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            return None, "unreadable_plist", ""
        if b"<!--" in data and b"<![CDATA[" in data:
            return None, "xml_comment", "XML comments had to be removed and the plist holds a CDATA section"
        without, closed = strip_xml_comments(data)
        if not closed:
            return None, "xml_comment", "an XML comment does not close"
        try:
            doc = plistlib.loads(without)
        except Exception:  # still not a plist
            return None, "unreadable_plist", ""
    if not isinstance(doc, dict):
        return None, "not_a_dictionary", ""
    return doc, None, ""


def parse_plist(data):
    """(the definition, None) or (None, a problem code): parse_plist_detail without the why."""
    doc, code, _detail = parse_plist_detail(data)
    return doc, code


def definition_notes(doc):
    """What the daemon changes from the definition, so `plan` says it: `logs-moved`, `stripped` (a key
    left out) and `working-directory-home` (a WorkingDirectory of / made the home)."""
    notes = []
    if any(key in doc for key in LOG_KEYS):
        notes.append("logs-moved")
    if any(key in doc for key in DROPPED_KEYS):
        notes.append("stripped")
    if doc.get("WorkingDirectory") == "/":
        notes.append("working-directory-home")
    return notes


def _read_root_file(path, folder, system, max_bytes):
    """The bytes of a file only root can change, or (None, why not): the folder and the file are real (no
    link), owned by root, not writable by group or others, the file at most MAX_BYTES. Absent: (None, None)."""
    try:
        finfo = os.lstat(folder)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "%s cannot be read (%s)" % (folder, type(exc).__name__)
    if not stat.S_ISDIR(finfo.st_mode) or finfo.st_uid != system.owner_uid or \
            finfo.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None, "%s is not a folder only root can write" % folder
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "%s cannot be opened without following a link (%s)" % (path, type(exc).__name__)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "%s is not a regular file" % path
        if info.st_uid != system.owner_uid:
            return None, "%s is not owned by root" % path
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None, "%s is writable by group or others" % path
        if info.st_size > max_bytes:
            return None, "%s is larger than %d bytes" % (path, max_bytes)
        return os.read(fd, max_bytes + 1), None
    finally:
        os.close(fd)


def admin_member(system, name):
    """Whether account NAME is a member of `admin` now (True or False), or None when it cannot be told:
    `dsmemberutil checkmembership -U NAME -G admin` through the runner, its exact answer."""
    code, out, _tail = system.output([DSMEMBERUTIL, "checkmembership", "-U", name, "-G", "admin"])
    answer = (out or "").strip()
    if code == 0 and answer == DS_MEMBER:
        return True
    if code == 0 and answer == DS_NOT_MEMBER:
        return False
    return None


def read_trust(system, account=None, getpwnam=None, getgrnam=None):
    """The trust file for ACCOUNT, read only: an absent file trusts nobody; a file that fails root's checks,
    or does not read as the schema, is ignored with a warning, never trusted. An owner that is ACCOUNT
    itself, or a name this computer does not know, is left out with a warning."""
    path = system.trust_path()
    data, why = _read_root_file(path, system.registry_dir(), system, MAX_TRUST_BYTES)
    if data is None:
        return Trust(warnings=["the trust file is ignored: " + why] if why else [], path=path)
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or doc.get("schema") != TRUST_SCHEMA or not all(
            isinstance(doc.get(k, []), list) and all(isinstance(n, str) for n in doc.get(k, []))
            for k in ("owners", "groups")):
        return Trust(warnings=["the trust file is ignored: %s is not a %s file" % (path, TRUST_SCHEMA)], path=path)
    getpwnam = getpwnam or (pwd.getpwnam if pwd else None)
    getgrnam = getgrnam or (grp.getgrnam if grp else None)
    trust = Trust(path=path)
    for name in doc.get("owners", []):
        try:
            uid = getpwnam(name).pw_uid
        except (KeyError, TypeError):
            trust.warnings.append("trusted owner %s is not an account on this computer: left out" % name)
            continue
        member = admin_member(system, name)
        if member is not True:
            # Checked at every read: a demotion, or a name added by hand, stops being trusted at once.
            trust.warnings.append("trusted owner %s is %s: left out" % (
                name, "not a member of admin" if member is False else "of unknown admin membership"))
            continue
        if account is not None and uid == account.uid:
            trust.warnings.append("trusted owner %s is the space account itself: never trusted for its own jobs"
                                  % name)
            continue
        trust.uids.add(uid)
        trust.owners.append(name)
    for name in doc.get("groups", []):
        if name not in TRUSTED_GROUPS:
            trust.warnings.append("trusted group %s is not wheel or admin: left out" % name)
            continue
        try:
            gid = getgrnam(name).gr_gid
        except (KeyError, TypeError):
            trust.warnings.append("trusted group %s is not a group on this computer: left out" % name)
            continue
        trust.gids.add(gid)
        trust.groups.append(name)
    return trust


def write_trust(system, owners, groups, getpwnam=None, getgrnam=None):
    """Replace the trust file (root, isolated, the root-owned copy): owners that exist and are members of
    admin now, groups wheel or admin only."""
    _require_root(system, "trust set", what="the root-owned trust file",
                  usage="trust set --owner ADMIN --group admin")
    getpwnam = getpwnam or (pwd.getpwnam if pwd else None)
    getgrnam = getgrnam or (grp.getgrnam if grp else None)
    for name in owners:
        try:
            uid = getpwnam(name).pw_uid
        except (KeyError, TypeError):
            raise SpaceDaemonsError("no account named %r on this computer" % name, "unknown-owner")
        if uid == 0:
            raise SpaceDaemonsError("root is always trusted; name the admin account that holds root through sudo",
                                    "unknown-owner")
        member = admin_member(system, name)
        if member is not True:
            raise SpaceDaemonsError("%s is %s: only an account that already holds root through sudo (a member of "
                                    "admin) may be trusted" % (name, "not a member of admin" if member is False
                                                               else "of unknown admin membership (dsmemberutil did "
                                                                    "not answer)"), "not-admin")
    for name in groups:
        if name not in TRUSTED_GROUPS:
            raise SpaceDaemonsError("only wheel or admin may be trusted, not %s: their members already hold root"
                                    % name, "group-not-allowed")
        try:
            getgrnam(name)
        except (KeyError, TypeError):
            raise SpaceDaemonsError("no group named %r on this computer" % name, "unknown-group")
    doc = {"schema": TRUST_SCHEMA, "owners": sorted(set(owners)), "groups": sorted(set(groups))}
    with _Lock(system):
        _ensure_system_dir(system.registry_dir(), system)
        write_atomic(system.trust_path(), (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                     system)
    return {"tool": "space-daemons", "version": SPACE_DAEMONS_VERSION, "trust": system.trust_path(),
            "owners": doc["owners"], "groups": doc["groups"], "ok": True}


# --- the rules -------------------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _str_dict(value, values=str):
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, values) for k, v in value.items())


def _inside(path, home):
    home = os.path.realpath(home)
    return path == home or path.startswith(home.rstrip(os.sep) + os.sep)


def _check_program(program, account, trust=None):
    """The program and every folder above it (as written and resolved) belong to root, the account or a
    trusted owner, and only those or a trusted group (or root's) can write them."""
    trust = trust or Trust()
    problems = []
    if not os.path.isabs(program):
        return [_problem("program_not_absolute", program)]
    real = os.path.realpath(program)
    try:
        info = os.stat(real)
    except OSError:
        return [_problem("program_missing", program)]
    if not stat.S_ISREG(info.st_mode):
        return [_problem("program_not_regular_file", program)]
    allowed = set([0, account.uid]) | trust.uids
    gids = set([0]) | trust.gids
    if info.st_uid not in allowed:
        problems.append(_problem("program_owner", "%s is owned by uid %d" % (real, info.st_uid)))
    if info.st_mode & stat.S_IWOTH or (info.st_mode & stat.S_IWGRP and info.st_gid not in gids):
        problems.append(_problem("program_writable_by_others", real))
    # Both the path as written and the file it resolves to: a link in a folder someone else can write
    # could be pointed elsewhere after this check.
    seen = set()
    for folder in _folders_up(program) + _folders_up(real):
        if folder in seen:
            continue
        seen.add(folder)
        try:
            finfo = os.stat(folder)
        except OSError:
            problems.append(_problem("program_dir_unreadable", folder))
            continue
        if _writable_by_others(finfo, allowed, gids):
            problems.append(_problem("program_dir_writable", folder))
    return problems


def _check_path(key, value, account):
    if not isinstance(value, str) or not value:
        return [_problem("bad_value", key)]
    if not os.path.isabs(value):
        return [_problem("path_not_absolute", key)]
    parent = os.path.realpath(os.path.dirname(os.path.normpath(value)))
    final = os.path.join(parent, os.path.basename(os.path.normpath(value)))
    if not _inside(final, account.home):
        return [_problem("path_outside_home", "%s: %s" % (key, value))]
    if os.path.islink(final):
        return [_problem("path_symlink", "%s: %s" % (key, value))]
    return []


def _check_keepalive(value, account):
    """The problems of a KeepAlive value: a bool, or a dict of the simple conditions, PathState's paths
    inside the account's home."""
    if isinstance(value, bool):
        return []
    if not isinstance(value, dict):
        return [_problem("bad_value", "KeepAlive")]
    problems = []
    for k, v in value.items():
        if k in KEEPALIVE_BOOLS:
            if not isinstance(v, bool):
                problems.append(_problem("bad_value", "KeepAlive." + k))
        elif k in KEEPALIVE_PATHS:
            if not _str_dict(v, bool):
                problems.append(_problem("bad_value", "KeepAlive." + k))
            else:
                for path in sorted(v):
                    problems.extend(_check_path("KeepAlive.PathState", path, account))
        else:
            problems.append(_problem("keepalive_condition_not_allowed", k))
    return problems


def _int_problems(value, where):
    """Every integer inside VALUE outside 0..2**31-1, named by where it sits."""
    if isinstance(value, dict):
        return [p for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) for p in _int_problems(v, where + "." + str(k))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _int_problems(v, "%s[%d]" % (where, i))]
    if _is_int(value) and not 0 <= value <= INT_MAX:
        return [_problem("integer_out_of_range", "%s is outside 0..%d" % (where, INT_MAX))]
    return []


def _check_calendar(value):
    entries = value if isinstance(value, list) else [value]
    if not entries:
        return False
    for entry in entries:
        if not isinstance(entry, dict) or not entry:
            return False
        for k, v in entry.items():
            if k not in CALENDAR_KEYS or not _is_int(v) or v < 0:
                return False
    return True


def validate(doc, file_name, account, log_dir=None, trust=None):
    """(problems, daemon) for one agent definition: every problem a named code, and the daemon plist
    only when there is none. TRUST (read_trust) widens the owners a program path may have."""
    problems = []
    for key in sorted(doc):
        if key not in ALLOWED_KEYS:
            problems.append(_problem("key_not_allowed", key))
    label = doc.get("Label")
    if not isinstance(label, str) or not LABEL_SHAPE.fullmatch(label):
        problems.append(_problem("label_shape", "letters and digits, then . _ @ - (at most 128)"))
    else:
        if file_name != label + ".plist":
            problems.append(_problem("file_name", "the file is %s, the label asks for %s.plist" % (file_name, label)))
        if label.startswith("com.apple."):
            problems.append(_problem("label_reserved", "com.apple. is the system's"))
    args = doc.get("ProgramArguments")
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        problems.append(_problem("program_arguments", "a non-empty list of strings"))
    else:
        if "Program" in doc and doc["Program"] != args[0]:
            problems.append(_problem("program_mismatch", "Program differs from ProgramArguments[0]"))
        problems.extend(_check_program(args[0], account, trust))
    checks = {
        "EnvironmentVariables": lambda v: _str_dict(v),
        "RunAtLoad": lambda v: isinstance(v, bool),
        "LowPriorityIO": lambda v: isinstance(v, bool),
        "AbandonProcessGroup": lambda v: isinstance(v, bool),
        "StartInterval": lambda v: _is_int(v) and v > 0,
        "ThrottleInterval": lambda v: _is_int(v) and v >= 0,
        "ExitTimeOut": lambda v: _is_int(v) and v >= 0,
        "StartCalendarInterval": _check_calendar,
        "ProcessType": lambda v: v in PROCESS_TYPES,
        "Nice": lambda v: _is_int(v) and 0 <= v <= 20,
        "LimitLoadToSessionType": lambda v: isinstance(v, str) or (isinstance(v, list) and
                                                                  all(isinstance(s, str) for s in v)),
        "AssociatedBundleIdentifiers": lambda v: isinstance(v, str) or (isinstance(v, list) and
                                                                       all(isinstance(s, str) for s in v)),
        "Umask": lambda v: _is_int(v) and 0 <= v <= 0o777,
        "SoftResourceLimits": lambda v: isinstance(v, dict) and bool(v) and all(
            k in RESOURCE_LIMIT_KEYS and _is_int(n) and n >= 0 for k, n in v.items()),
    }
    for key, ok in sorted(checks.items()):
        if key in doc and not ok(doc[key]):
            problems.append(_problem("bad_value", key))
    if "KeepAlive" in doc:
        problems.extend(_check_keepalive(doc["KeepAlive"], account))
    env = doc.get("EnvironmentVariables")
    if isinstance(env, dict):
        for name in sorted(k for k in env if isinstance(k, str) and k.upper().startswith("DYLD_")):
            problems.append(_problem("dyld_variable", name))
    for key in PATH_KEYS:
        # A WorkingDirectory of "/" (launchd's own default) becomes the home in the daemon.
        if key in doc and not (key == "WorkingDirectory" and doc[key] == "/"):
            problems.extend(_check_path(key, doc[key], account))
    problems.extend(_int_problems(doc, "definition"))
    if problems:
        return problems, None
    daemon = daemon_plist(doc, account, log_dir)
    try:
        # What plan calls ok must be writable: a control character or an integer the format refuses
        # fails here, not in the middle of apply.
        plistlib.dumps(daemon, fmt=plistlib.FMT_XML, sort_keys=True)
    except Exception as exc:  # plistlib raises ValueError, TypeError or OverflowError
        return [_problem("not_writable_as_plist", "%s: %s" % (type(exc).__name__, exc))], None
    return [], daemon


def default_log_dir(user):
    return os.path.join("/", LOGS_DIR, user)


def log_paths(log_dir, label):
    """The one place the log layout is decided: (StandardOutPath, StandardErrorPath) of LABEL's daemon in
    the account's root-owned LOG_DIR. Change the scheme here and nowhere else."""
    return os.path.join(log_dir, label + ".out.log"), os.path.join(log_dir, label + ".err.log")


def daemon_plist(doc, account, log_dir=None):
    """The daemon for a definition that passed `validate`: its keys, run as ACCOUNT, logging into the
    root-owned LOG_DIR (the definition's own log paths are ignored)."""
    daemon = dict((k, v) for k, v in doc.items() if k in ALLOWED_KEYS and k not in DROPPED_KEYS)
    log_dir = log_dir or default_log_dir(account.name)
    daemon["StandardOutPath"], daemon["StandardErrorPath"] = log_paths(log_dir, doc["Label"])
    env = dict(doc.get("EnvironmentVariables") or {})
    env.update({"HOME": account.home, "USER": account.name, "LOGNAME": account.name})
    daemon["EnvironmentVariables"] = env
    if daemon.get("WorkingDirectory", "/") == "/":
        daemon["WorkingDirectory"] = account.home
    daemon["UserName"] = account.name
    daemon["GroupName"] = account.group
    daemon["InitGroups"] = True
    return daemon


# --- writing --------------------------------------------------------------------------------------------


def write_atomic(path, data, system, mode=0o644):
    """DATA at PATH, whole or not at all: a temporary file in the same folder, its mode and owner set,
    synced, then renamed over the name; the folder synced after."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        os.fchmod(fd, mode)
        system.set_owner(fd)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.rename(tmp, path)
        tmp = None
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None and os.path.lexists(tmp):
            os.unlink(tmp)


def _ensure_system_dir(path, system):
    """A root-owned 0755 folder (created when absent); a link or a file in its place is refused."""
    if not os.path.lexists(path):
        os.makedirs(path, 0o755)
        system.set_owner(path)
        os.chmod(path, 0o755)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise SpaceDaemonsError("%s is not a folder (a link is refused)" % path)
    if system.euid == 0 and (info.st_uid != system.owner_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise SpaceDaemonsError("%s is not root's alone: make it root:wheel 0755 first" % path)


def read_registry(system):
    """The registry's entries ({label, user, uid, source, sourceSha256, written, writtenSha256, at}); [] when
    absent. One
    that does not read is refused by name, never replaced."""
    path = system.registry_path()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SpaceDaemonsError("the registry %s cannot be read (%s)" % (path, type(exc).__name__))
    try:
        with os.fdopen(fd, "rb") as stream:
            fd = None
            doc = json.loads(stream.read(4 * 1024 * 1024).decode("utf-8"))
    except (ValueError, OSError):
        raise SpaceDaemonsError("the registry %s is not a %s file: move it aside" % (path, REGISTRY_SCHEMA))
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(doc, dict) or doc.get("schema") != REGISTRY_SCHEMA or not isinstance(doc.get("entries"), list):
        raise SpaceDaemonsError("the registry %s is not a %s file: move it aside" % (path, REGISTRY_SCHEMA))
    return [e for e in doc["entries"] if isinstance(e, dict) and isinstance(e.get("label"), str)]


def write_registry(system, entries):
    _ensure_system_dir(system.registry_dir(), system)
    doc = {"schema": REGISTRY_SCHEMA, "entries": sorted(entries, key=lambda e: e["label"])}
    write_atomic(system.registry_path(), (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"), system)


class _Lock(object):
    """One writer of the registry at a time (two administrators, two terminals)."""

    def __init__(self, system):
        self.system, self.fd = system, None

    def __enter__(self):
        if fcntl is None:
            return self
        _ensure_system_dir(self.system.registry_dir(), self.system)
        path = os.path.join(self.system.registry_dir(), ".lock")
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        return False


def _require_root(system, verb, account_name=None, what="/Library/LaunchDaemons", usage=None):
    usage = usage or "%s --user %s ..." % (verb, account_name)
    if system.euid != 0:
        raise SpaceDaemonsError("%s writes %s and needs root: sudo %s %s" % (verb, what, INSTALLED_COMMAND, usage),
                                "not-root")
    if not system.isolated:
        raise SpaceDaemonsError("not-isolated: %s runs only in Python's isolated mode, so no module from the "
                                "script's folder, the user's site-packages or a PYTHON* variable is used as root: "
                                "sudo %s %s" % (verb, INSTALLED_COMMAND, usage), "not-isolated")
    problems = system.self_check()
    if problems:
        raise SpaceDaemonsError("%s runs only from the root-owned copy: %s. Install it once from the release, "
                                "downloaded and verified in your own account: gh release download (or curl -LO), "
                                "shasum -a 256 -c SHA256SUMS --ignore-missing, the release key from %s imported "
                                "(gpg is not on macOS by default), `gpg --status-fd 1 --verify SHA256SUMS.asc "
                                "SHA256SUMS | grep -q \"VALIDSIG %s\"`, unzip -p the wheel's "
                                "observatory/engine/tools/space_daemons.py; then `sudo install -o root -g wheel -m 0755 "
                                "space_daemons.py %s` and run `sudo %s %s`"
                                % (verb, "; ".join(problems), RELEASE_KEY_URL, RELEASE_KEY_FINGERPRINT, INSTALLED_COPY,
                                   INSTALLED_COMMAND, usage), "not-root-owned-copy")


def _check_root_dir(path, system, mode=None):
    """PATH is a real folder (not a link) that root owns and nobody else can write; with MODE, exactly it."""
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SpaceDaemonsError("the log folder %s cannot be read (%s)" % (path, type(exc).__name__), "log-folder")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SpaceDaemonsError("the log folder %s is a link or not a folder: move it aside" % path, "log-folder")
    if info.st_uid != system.owner_uid:
        raise SpaceDaemonsError("the log folder %s is not owned by root: move it aside" % path, "log-folder")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise SpaceDaemonsError("the log folder %s has mode %o, not %o" % (path, stat.S_IMODE(info.st_mode), mode),
                                "log-folder")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SpaceDaemonsError("the log folder %s is writable by group or others: move it aside" % path,
                                "log-folder")


def ensure_log_dir(system, account):
    """/Library/Logs/ProjectObservatory (root:wheel 0755) and <user> in it (root:wheel 0700) with one
    inherited ACL entry that lets the account read its logs. A folder already there is used only when it is
    root's own real folder; its owner and mode are checked before and after."""
    user_dir = system.log_dir(account.name)
    base = os.path.dirname(user_dir)
    for path, mode in ((base, 0o755), (user_dir, 0o700)):
        if os.path.lexists(path):
            _check_root_dir(path, system)
        else:
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent, 0o755)
            os.mkdir(path, mode)
            system.set_owner(path)
        os.chmod(path, mode)
        _check_root_dir(path, system, mode)
    # Only our entry: any ACL already on either folder (someone's +a) is cleared first, then checked.
    for path in (base, user_dir):
        code, tail = system.command([system.chmod, "-N", path])
        if code != 0:
            raise SpaceDaemonsError("the ACL of %s could not be cleared (chmod -N: exit %d %s); nothing was installed"
                                    % (path, code, tail), "log-folder")
    code, tail = system.command([system.chmod, "+a", LOG_ACL % account.name, user_dir])
    if code != 0:
        raise SpaceDaemonsError("the account's read access to %s could not be added (chmod +a: exit %d %s); nothing "
                                "was installed" % (user_dir, code, tail), "log-folder")
    for path, expected in ((base, []), (user_dir, ["user:%s allow" % account.name])):
        found = _acl_entries(system, path)
        if found is None or len(found) != len(expected) or not all(
                f.startswith(e) for f, e in zip(found, expected)):
            raise SpaceDaemonsError("the ACL of %s is not the one entry this tool sets (%s); nothing was installed"
                                    % (path, "; ".join(found or ["unreadable"])), "log-folder")
    _check_root_dir(base, system, 0o755)
    _check_root_dir(user_dir, system, 0o700)
    return user_dir


def _acl_entries(system, path):
    """The ACL entries of PATH as `ls -lde` prints them ("user:NAME allow ..."), or None when unreadable."""
    code, out, _tail = system.output([LS, "-lde", path])
    if code != 0:
        return None
    entries = []
    for line in out.splitlines():
        if not re.match(r"^\s*\d+:", line):
            continue
        m = ACL_LINE.match(line)
        if not m:
            return None  # an entry this does not understand: fail closed, never "no entries"
        entries.append(m.group(1).strip())
    return entries


class LogFileError(SpaceDaemonsError):
    """A daemon's log file is already there and is not one the account may be given."""


def prepare_log_file(system, account, path):
    """Create the daemon's log file ahead of launchd: a new file, never through a link, owned by the account,
    mode 0600, inside root's 0700 folder (the account reads and appends to it, but cannot create, rename,
    link or unlink there). So it is safe whether launchd opens it as root or as the account. A file already
    there is kept only when it is a regular file with one link, owned by the account (or by root, then given
    to the account), and not writable by group or others. Answers `created`, `kept` or `adopted`."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _keep_log_file(system, account, path)
    try:
        system.chown_fd(fd, account.uid, account.gid)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return "created"


def _keep_log_file(system, account, path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LogFileError("%s is a link or cannot be opened (%s)" % (path, type(exc).__name__), "log-file")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LogFileError("%s is not a regular file" % path, "log-file")
        if info.st_nlink != 1:
            raise LogFileError("%s has %d links: a hard link is never written through" % (path, info.st_nlink),
                               "log-file")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise LogFileError("%s is writable by group or others" % path, "log-file")
        if info.st_uid == account.uid:
            return "kept"
        if info.st_uid == system.owner_uid:
            system.chown_fd(fd, account.uid, account.gid)
            return "adopted"
        raise LogFileError("%s is owned by uid %d, neither %s nor root" % (path, info.st_uid, account.name),
                           "log-file")
    finally:
        os.close(fd)


def label_holders(system):
    """{label: [plist paths]} of every plist in /Library/LaunchDaemons and /System/Library/LaunchDaemons.
    A plist that does not read, or is too large, is ignored."""
    holders = {}
    for folder in system.label_dirs():
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".plist"):
                continue
            path = os.path.join(folder, name)
            try:
                if not os.path.isfile(path) or os.path.getsize(path) > 1024 * 1024:
                    continue
                with open(path, "rb") as stream:
                    doc = plistlib.load(stream)
            except Exception:  # an unreadable plist holds no label we can see
                continue
            if isinstance(doc, dict) and isinstance(doc.get("Label"), str):
                holders.setdefault(doc["Label"], []).append(path)
    return holders


def _plist_label(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            doc = plistlib.loads(stream.read(1024 * 1024))
    except Exception:  # not a plist, or a link
        return None
    return doc.get("Label") if isinstance(doc, dict) else None


def _file_sha(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        return _sha256_bytes(stream.read())


# --- the commands ----------------------------------------------------------------------------------------


def plan(account, source="background", labels=None, system=None):
    folder = account.background_dir() if source == "background" else account.staged_dir()
    system = system or system_from_environment()
    trust = read_trust(system, account)
    defs = read_definitions(folder, account, labels, system.log_dir(account.name), trust)
    found = set(d["label"] for d in defs)
    missing = [label for label in (labels or []) if label not in found]
    return {"tool": "space-daemons", "version": SPACE_DAEMONS_VERSION, "user": account.name, "uid": account.uid,
            "from": source, "folder": folder, "definitions": defs, "not_found": missing, "trust": trust.as_dict()}


def _install_one(system, account, definition, entries, holders=None):
    """Write and load one daemon; on a failure put back what was there. Answers its outcome."""
    label = definition["label"]
    target = system.daemon_path(label)
    mine = [e for e in entries if e["label"] == label]
    entry = mine[0] if mine else None
    result = {"label": label, "file": definition["file"], "daemon": target}
    if entry is not None and (entry.get("user") != account.name or entry.get("uid") != account.uid):
        result.update(outcome="refused", problems=[_problem("owned_by_other_account", str(entry.get("user")))])
        return result
    # One copy only: a job this account already runs as an agent is never started a second time as a daemon.
    if system.run(["print", "gui/%d/%s" % (account.uid, label)])[0] == 0:
        result.update(outcome="refused", problems=[_problem(
            "loaded-in-gui", "gui/%d/%s is loaded; unload the agent first" % (account.uid, label))])
        return result
    # The system domain's label namespace: a label any other daemon plist carries is someone else's.
    holders = label_holders(system) if holders is None else holders
    taken = [p for p in holders.get(label, []) if not (p == target and entry is not None)]
    if taken:
        result.update(outcome="refused", problems=[_problem("label_taken", "already the label of " + ", ".join(taken))])
        return result
    previous = None
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target):
            result.update(outcome="refused", problems=[_problem("not_written_by_this_tool", "not a regular file")])
            return result
        if entry is None:
            result.update(outcome="refused", problems=[_problem(
                "not_written_by_this_tool", "%s exists and the registry does not name it" % target)])
            return result
        current = _file_sha(target)
        if current != entry.get("writtenSha256"):
            result.update(outcome="refused", problems=[_problem(
                "changed_since_written", "%s differs from what this tool wrote; `remove` it, then apply again"
                % target)])
            return result
        with open(target, "rb") as stream:
            previous = stream.read()
    elif entry is None and system.loaded(label):
        result.update(outcome="refused", problems=[_problem(
            "label_taken", "system/%s is loaded and this tool did not load it" % label)])
        return result
    # The log files exist, the account's, before launchd ever opens them (whichever credentials it uses).
    try:
        logs = [prepare_log_file(system, account, definition["daemon"][key]) for key in LOG_KEYS]
    except LogFileError as exc:
        result.update(outcome="refused", problems=[_problem("log-file", str(exc))])
        return result
    result["logs"] = dict(zip(("out", "err"), (definition["daemon"][key] for key in LOG_KEYS)))
    result["log_files"] = logs
    data = plistlib.dumps(definition["daemon"], fmt=plistlib.FMT_XML, sort_keys=True)
    new_entry = {"label": label, "user": account.name, "uid": account.uid, "source": definition["file"],
                 "sourceSha256": definition["source_sha256"], "written": target,
                 "writtenSha256": _sha256_bytes(data), "at": _now()}
    others = [e for e in entries if e["label"] != label]
    # The registry first: a crash between the two writes leaves an entry whose plist is missing (apply
    # writes it again, remove cleans it, status says `missing`), never a daemon nobody recorded.
    write_registry(system, others + [new_entry])
    try:
        _ensure_system_dir(system.daemons_dir(), system)
        write_atomic(target, data, system)
    except Exception as exc:
        write_registry(system, others + ([entry] if entry is not None else []))
        result.update(outcome="failed", problems=[_problem("write_failed", "%s: %s" % (type(exc).__name__, exc))])
        return result

    def put_back(problem):
        if previous is not None:
            write_atomic(target, previous, system)
            write_registry(system, others + [entry])
            if not system.loaded(label):
                system.run(["bootstrap", "system", target])
            note = "the previous daemon is back"
        else:
            if os.path.lexists(target):
                os.unlink(target)
            write_registry(system, others)
            note = "nothing is left in /Library/LaunchDaemons"
        result.update(outcome="failed", problems=[problem], put_back=note)
        return result

    if system.loaded(label):
        code, tail = system.run(["bootout", "system/" + label])
        if code != 0 and system.loaded(label):
            # Still the old job: put the old file back (or none, as before) and leave it running.
            if previous is not None:
                write_atomic(target, previous, system)
            elif os.path.lexists(target):
                os.unlink(target)
            write_registry(system, others + ([entry] if entry is not None else []))
            result.update(outcome="failed", problems=[_problem("bootout_failed", "exit %d %s" % (code, tail))])
            return result
    code, tail = system.run(["bootstrap", "system", target])
    if code != 0:
        return put_back(_problem("bootstrap_failed", ("exit %d %s" % (code, tail)).strip()))
    entries[:] = others + [new_entry]
    result.update(outcome="loaded", written_sha256=new_entry["writtenSha256"])
    return result


def apply(system, account, labels=None):
    _require_root(system, "apply", account.name)
    folder = account.background_dir()
    logs = system.log_dir(account.name)
    trust = read_trust(system, account)
    defs = read_definitions(folder, account, labels, logs, trust)
    found = set(d["label"] for d in defs)
    results = [{"label": label, "outcome": "not-found", "problems": [_problem("not_found", folder)]}
               for label in (labels or []) if label not in found]
    with _Lock(system):
        entries = read_registry(system)
        if any(not d["problems"] for d in defs):
            ensure_log_dir(system, account)
        holders = label_holders(system)
        for definition in defs:
            if definition["problems"]:
                results.append({"label": definition["label"], "file": definition["file"], "outcome": "skipped",
                                "problems": definition["problems"]})
                continue
            try:
                results.append(_install_one(system, account, definition, entries, holders))
            except Exception as exc:  # one definition's failure never stops the others
                results.append({"label": definition["label"], "file": definition["file"], "outcome": "failed",
                                "problems": [_problem("failed", "%s: %s" % (type(exc).__name__, exc))]})
    return {"tool": "space-daemons", "version": SPACE_DAEMONS_VERSION, "user": account.name, "uid": account.uid,
            "folder": folder, "logs": logs, "results": results, "trust": trust.as_dict(),
            "ok": all(r["outcome"] == "loaded" for r in results)}


def remove(system, account, label):
    _require_root(system, "remove", account.name)
    with _Lock(system):
        entries = read_registry(system)
        mine = [e for e in entries if e["label"] == label and e.get("user") == account.name]
        if not mine:
            raise SpaceDaemonsError("the registry names no daemon %s of %s: this tool removes only what it wrote "
                                    "(`status --user %s` lists them)" % (label, account.name, account.name))
        target = system.daemon_path(label)
        # Ours or nothing, checked before anything stops: a refusal leaves the daemon running as it was.
        if os.path.lexists(target):
            if os.path.islink(target):
                raise SpaceDaemonsError("%s is a link: it is left alone" % target, "not-ours")
            if _file_sha(target) != mine[0].get("writtenSha256") and _plist_label(target) != label:
                raise SpaceDaemonsError("%s is neither what this tool wrote nor a daemon labelled %s: it is left "
                                        "alone; move it aside by hand" % (target, label), "not-ours")
        if system.loaded(label):
            code, tail = system.run(["bootout", "system/" + label])
            if code != 0 and system.loaded(label):
                return {"label": label, "user": account.name, "outcome": "failed",
                        "problems": [_problem("bootout_failed", ("exit %d %s" % (code, tail)).strip())], "ok": False}
        if os.path.lexists(target):
            os.unlink(target)
        write_registry(system, [e for e in entries if e["label"] != label])
    kept = [p for p in log_paths(system.log_dir(account.name), label) if os.path.lexists(p)]
    return {"label": label, "user": account.name, "outcome": "removed", "ok": True, "logs_kept": kept,
            "note": "the log files stay in %s; delete them by hand if they are not wanted"
                    % system.log_dir(account.name) if kept else ""}


def status(system, account):
    rows = []
    for entry in read_registry(system):
        if entry.get("user") != account.name:
            continue
        target = system.daemon_path(entry["label"])
        sha = _file_sha(target)
        matches = sha is not None and sha == entry.get("writtenSha256")
        rows.append({"label": entry["label"], "uid": entry.get("uid"), "at": entry.get("at"),
                     "daemon": target, "present": sha is not None, "matches": matches,
                     "state": "missing" if sha is None else ("as-written" if matches else "changed"),
                     "loaded": system.loaded(entry["label"])})
    return {"tool": "space-daemons", "version": SPACE_DAEMONS_VERSION, "user": account.name,
            "registry": system.registry_path(), "daemons": rows}


# --- the command line ------------------------------------------------------------------------------------


def version_line():
    path = os.path.abspath(__file__)
    with open(path, "rb") as stream:
        digest = _sha256_bytes(stream.read())
    return "space-daemons %s sha256 %s %s" % (SPACE_DAEMONS_VERSION, digest, path)


def parser():
    ap = argparse.ArgumentParser(prog="observatory-space-daemons",
                                 description="run a headless space's background jobs as system-domain daemons "
                                             "that run as that account (UserName), never as root",
                                 epilog="apply and remove run as root, only in isolated mode, from the root-owned "
                                        "copy: sudo %s apply --user ACCOUNT --label LABEL" % INSTALLED_COMMAND)
    ap.add_argument("--version", action="store_true", help="the version and the sha256 of this file (information "
                                                              "only; verify the release's SHA256SUMS and signature)")
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("plan", help="what apply would write, for each definition; loads nothing")
    p.add_argument("--user", required=True)
    p.add_argument("--from", dest="source", choices=("background", "staged"), default="background")
    p.add_argument("--label", action="append")
    p.add_argument("--json", action="store_true")
    a = sub.add_parser("apply", help="write and load the daemons (root, from the root-owned copy)")
    a.add_argument("--user", required=True)
    a.add_argument("--label", action="append")
    a.add_argument("--json", action="store_true")
    r = sub.add_parser("remove", help="unload one daemon this tool wrote and delete it (root)")
    r.add_argument("--user", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="the daemons this tool wrote for the account, loaded or not")
    s.add_argument("--user", required=True)
    s.add_argument("--json", action="store_true")
    t = sub.add_parser("trust", help="the accounts and groups a program path may belong to besides root: only "
                                     "those that already hold root through sudo")
    tsub = t.add_subparsers(dest="trust_action", required=True)
    ts = tsub.add_parser("show", help="the trust file, what it trusts and what it leaves out")
    ts.add_argument("--user", help="as it applies to this space account (its own name is never trusted)")
    ts.add_argument("--json", action="store_true")
    tw = tsub.add_parser("set", help="replace the trust file (root, from the root-owned copy)")
    tw.add_argument("--owner", action="append", default=[], help="an account that already holds root through sudo")
    tw.add_argument("--group", action="append", default=[], help="a group whose members all hold root (admin)")
    tw.add_argument("--json", action="store_true")
    return ap


def _render(command, result):
    lines = []
    for warning in (result.get("trust") or {}).get("warnings", []) if command in ("plan", "apply") else []:
        lines.append("warning: " + warning)
    if command == "plan":
        lines.append("%d definition(s) in %s" % (len(result["definitions"]), result["folder"]))
        for d in result["definitions"]:
            verdict = "ok" if d["verdict"] == "ok" else "refused: " + ", ".join(
                "%s (%s)" % (p["code"], p["detail"]) if p["detail"] else p["code"] for p in d["problems"])
            notes = (" (%s)" % ", ".join(d.get("notes") or [])) if d.get("notes") else ""
            lines.append("  %s  %s%s" % (d["label"], verdict, notes))
        for label in result["not_found"]:
            lines.append("  %s  not found" % label)
    elif command == "apply":
        for r in result["results"]:
            detail = "; ".join(p["code"] + (" (%s)" % p["detail"] if p["detail"] else "")
                               for p in r.get("problems", []))
            lines.append("  %s  %s%s" % (r["label"], r["outcome"], ": " + detail if detail else ""))
        if not result["results"]:
            lines.append("nothing to apply in %s" % result["folder"])
    elif command == "remove":
        lines.append("  %s  %s" % (result["label"], result["outcome"]))
        if result.get("note"):
            lines.append("  " + result["note"])
    elif command == "trust":
        t = result["trust"] if isinstance(result["trust"], dict) else {
            "path": result["trust"], "owners": result["owners"], "groups": result["groups"], "warnings": []}
        lines.append("trust file %s: owners %s; groups %s" % (t["path"], ", ".join(t["owners"]) or "none",
                                                              ", ".join(t["groups"]) or "none"))
        for warning in t.get("warnings", []):
            lines.append("  warning: " + warning)
    else:
        lines.append("%d daemon(s) of %s in %s" % (len(result["daemons"]), result["user"], result["registry"]))
        for d in result["daemons"]:
            lines.append("  %s  %s, %s" % (d["label"], "loaded" if d["loaded"] else "not loaded", d["state"]))
    return "\n".join(lines)


def main(argv=None, system=None):
    args = parser().parse_args(argv)
    if args.version:
        print(version_line())
        return 0
    if not args.command:
        parser().print_usage(sys.stderr)
        return 2
    system = system or system_from_environment()
    try:
        if args.command == "trust":
            if args.trust_action == "set":
                result = write_trust(system, args.owner, args.group)
            else:
                account = lookup_account(args.user) if args.user else None
                result = {"tool": "space-daemons", "version": SPACE_DAEMONS_VERSION,
                          "trust": read_trust(system, account).as_dict()}
            print(json.dumps(result, indent=2, sort_keys=True) if args.json else _render("trust", result))
            return 0
        account = lookup_account(args.user)
        if args.command == "plan":
            result = plan(account, args.source, args.label, system)
            ok = all(d["verdict"] == "ok" for d in result["definitions"]) and not result["not_found"]
        elif args.command == "apply":
            result = apply(system, account, args.label)
            ok = result["ok"]
        elif args.command == "remove":
            result = remove(system, account, args.label)
            ok = result["ok"]
        else:
            result = status(system, account)
            ok = True
    except SpaceDaemonsError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "code": exc.code}, indent=2))
        else:
            print("space-daemons: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else _render(args.command, result))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
