"""The macOS adapter of the lifecycle broker (OBS-79 step L2, docs/design/LIFECYCLE.md).

The broker (`broker.py`) decides; this module observes and acts on one Mac, and only through Launch Services and
launchd. It never takes a command, an executable, arguments or an environment from a request: what it runs is a
fixed system tool (`codesign`, `launchctl`), the broker's own NSWorkspace helper (`native_app.swift`), or a product
CLI whose signature it checked first.

- **An app is its designated requirement.** `requirement_for` builds `anchor apple generic and identifier "<id>" and
  certificate leaf[subject.OU] = "<team>"` from a validated bundle id and team, never from text the bundle or the
  registry supplies, so nothing can smuggle an `or true` into the requirement language. A rebuild or update signed by
  the same team keeps matching; another team, an ad hoc or unsigned build, a broken seal or another bundle id does not.
- **Installed places only.** An app is a bundle under `/Applications` or `~/Applications`; every component of its path
  belongs to root or to this account and no one else may write it (`trusted_path`). The one exception is the system's
  own `/Applications`, which macOS ships `root:admin 0775`; a launchd job's interpreter behind Homebrew's
  `Cellar` gets the same narrow exception for the `Cellar` folder Homebrew owns.
- **A background job is its label and the hashes of what it runs.** An interpreter behind a stable path (Homebrew's
  `opt` link, a SIP-sealed system path) is pinned by that path, so `brew upgrade` is not a new identity; everything
  else it names is pinned by its bytes.
- **Every child is in a process group of its own** and the group dies with it on a timeout (`Runner`): a `codesign` or
  a helper that spawned children leaves no orphan, and `close` ends whatever is still running at once.
- **A deep signature check runs once per bundle state.** One background verifier runs them in turn, every caller that
  misses the cache waits on the same check (single flight), and a caller waits only up to its operation's deadline:
  `verification_pending` is an honest "not yet", and the result is cached when it lands. The cache key is a walk of the
  bundle's metadata (inode, size, times, mode, owner of every entry); the cache is bounded and kept in an owner-only
  file, so an unchanged bundle is not checked again after a restart.
- **Readiness** is a pid (`process`), a Fabric service's well-known document (`fabric-service`), or one of the two
  product profiles: `fabric-hub` (Fabric's own `hub.json` names the running pid and its loopback origin answers; the
  door token it names is never read) and `switchboard-runtime` (Switchboard's bundled CLI, verified under the enrolled
  team, reports its runtime online).

Linux and Windows have no adapter yet: `platform_adapter` answers `unsupported_platform` there for every call, never a
silent success. The helper is built from `native_app.swift`: `build_helper` compiles it with `swiftc` for a source
install (`python3 node/lifecycle/macos.py build-helper`); the release path is in the design document.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import plistlib
import queue
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

if __name__ == '__main__':                             # run as a script: `python3 node/lifecycle/macos.py ...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import osprivacy  # noqa: E402
import osproc  # noqa: E402

from node.lifecycle.broker import (  # noqa: E402
    BUNDLE_ID,
    IDENTITY_SCHEME,
    LAUNCHD_LABEL,
    SWITCHBOARD_BUNDLE_ID,
    SWITCHBOARD_CLI,
    TEAM_ID,
    LifecycleError,
    canonical,
    default_state_dir,
)

CODESIGN = '/usr/bin/codesign'
LAUNCHCTL = '/bin/launchctl'
#: The only PATH a child gets: the system's own folders.
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
#: A launchd job's interpreter at one of these places is pinned by its path, not its bytes: Homebrew's `opt/` link
#: survives `brew upgrade`, and the system folders are sealed. A versioned `Cellar/` path is never stable.
STABLE_EXECUTABLE_PREFIXES = (
    '/opt/homebrew/opt/',
    '/usr/local/opt/',
    '/usr/bin/',
    '/bin/',
    '/usr/sbin/',
    '/sbin/',
    '/usr/libexec/',
)
STABLE_PIN = 'stable-path'
#: macOS's `admin` group, which owns `/Applications` (0775) and Homebrew's `Cellar`.
ADMIN_GID = 80
#: A deep check reads the whole bundle; under disk load a throttled child takes minutes, so it gets far longer than a
#: plain check. With verified bundles cached it runs only after an update.
DEEP_VERIFY_TIMEOUT = 180
PLAIN_VERIFY_TIMEOUT = 60
#: How long a caller with no deadline of its own waits for another caller's check of the same bundle.
VERIFY_WAIT_SECONDS = 25
#: The metadata walk of one bundle. Out of time means nothing was decided: `verification_pending`, retryable.
METADATA_WALK_SECONDS = 5
MAX_BUNDLE_ENTRIES = 100_000
#: Verified bundle states kept, in memory and in the cache file; the oldest goes first.
CACHE_LIMIT = 256
CACHE_MAX_BYTES = 1_048_576
#: Fabric's hub. Fabric is an app, not a fabric-service, and publishes no well-known document. While it serves it
#: writes `hub.json` (owner-only, atomically) after its local stack is up, naming its pid and its loopback origin, and
#: removes it when it quits; a Fabric stuck in a startup-failure dialog has a pid and no hub.
FABRIC_HUB_PROTOCOL = 'fabric-hub/0.1'
FABRIC_HUB_MAX_BYTES = 64 * 1024
FABRIC_HUB_ORIGIN = re.compile(r'http://127\.0\.0\.1:([0-9]{4,5})')
#: What Switchboard's `status` reports while its runtime takes requests.
SWITCHBOARD_ONLINE = 'Next request · HTTP/SSE'
SWITCHBOARD_PROXY = re.compile(r'127\.0\.0\.1:[0-9]{1,5}')
#: The compiled helper's file name, and where a source install keeps it.
HELPER_NAME = 'lifecycle-native'
HELPER_SOURCE = Path(__file__).with_name('native_app.swift')
#: How long the helper may take to say which source it was built from. An older build answers at once
#: (`invalid_argument`); one that does not answer in time is treated as outdated, never waited on.
HELPER_VERSION_TIMEOUT = 5
#: The line of `native_app.swift` that declares its version; its value is left out of the digest it states.
HELPER_VERSION_LINE = re.compile(r'^let helperVersion = "([0-9a-f]*)"$', re.M)
#: The signal that ends a process group at once; on Windows `osproc` stops the tree whatever the signal.
_KILL = getattr(signal, 'SIGKILL', signal.SIGTERM)
#: The fields of a helper answer the broker reads; anything else it says is dropped.
HELPER_FIELDS = ('pid', 'bundleId', 'windows', 'active', 'hidden', 'inputIdleSeconds')
WATCH_EVENTS = ('launched', 'terminated', 'activated')


def default_helper() -> Path:
    """Where a source install builds the helper: `<state>/node-lifecycle/bin/lifecycle-native`, owner only."""
    return default_state_dir() / 'bin' / HELPER_NAME


def source_version(text: str) -> Optional[str]:
    """The version a helper built from `text` declares: the first 16 hex digits of the SHA-256 of the source with the
    value of its `let helperVersion = "..."` line left empty and line ends as LF. None without exactly one such line.

    Any other change to the source changes it, and `test_the_source_declares_the_version_the_engine_expects` fails
    until the declared value is the new one, so the version cannot stay behind its source unnoticed."""
    text = text.replace('\r\n', '\n')
    if len(HELPER_VERSION_LINE.findall(text)) != 1:
        return None
    blank = HELPER_VERSION_LINE.sub('let helperVersion = ""', text)
    return hashlib.sha256(blank.encode('utf-8')).hexdigest()[:16]


def declared_helper_version(text: str) -> Optional[str]:
    """The value `text`'s `let helperVersion` line declares, which a helper built from it prints for `--version`."""
    found = HELPER_VERSION_LINE.findall(text.replace('\r\n', '\n'))
    return found[0] if len(found) == 1 else None


_expected_version: dict = {}


def expected_helper_version() -> Optional[str]:
    """The version of the helper this engine ships (`native_app.swift` beside this module), read once per source
    path. None when the source cannot be read: then no helper is run (`helper_source_missing`), and nothing is
    remembered, so a restored source is read again."""
    if HELPER_SOURCE not in _expected_version:
        try:
            _expected_version[HELPER_SOURCE] = source_version(HELPER_SOURCE.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError):
            return None
    return _expected_version[HELPER_SOURCE]


def helper_safe(helper) -> bool:
    """The helper may be run: a regular file of this account that no one else may change, in a folder of this
    account that no one else may write, neither of them a link. Whoever could replace it would act with every grant
    the broker holds, so nothing runs it before this check, `helper_state` included."""
    helper = Path(helper)
    folder = helper.parent
    try:
        return (
            # The folder first: whoever may write it may swap the file between this check and the exec.
            folder.is_dir()
            and not folder.is_symlink()
            and osprivacy.owned_by_me(folder)
            and not osprivacy.others_may_write(folder)
            and helper.is_file()
            and not helper.is_symlink()
            and osprivacy.owned_by_me(helper)
            and not osprivacy.others_may_write(helper)
        )
    except OSError:
        return False


def helper_version(helper, *, run) -> Optional[str]:
    """What `helper --version` says it was built from, or None: an older build that does not know the flag
    (`invalid_argument`), a timeout, an exit code, or an answer of another shape. Nothing else is asked of it."""
    try:
        result = run([str(helper), '--version'], timeout=HELPER_VERSION_TIMEOUT)
    except LifecycleError:
        return None
    if result.returncode:
        return None
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(answer, dict) or answer.get('state') != 'version' or not isinstance(answer.get('version'), str):
        return None
    return answer['version']


def helper_current(helper, *, run) -> bool:
    """The helper was built from the source this engine ships."""
    expected = expected_helper_version()
    return expected is not None and helper_version(helper, run=run) == expected


def helper_state(helper=None, *, run=None, platform: Optional[str] = None) -> str:
    """What `full lifecycle status` and `macos.py helper` report, decided before anything runs:

    `unsupported_platform` off macOS; `not_built` (no file); `unsafe` (a link, or a file or folder others may change:
    never run, as the adapter never runs it); `source_missing` (this engine's `native_app.swift` cannot be read, so no
    version is known and nothing is run); then `current` or `outdated` from `helper --version`."""
    if (platform or sys.platform) != 'darwin':
        return 'unsupported_platform'
    helper = Path(helper) if helper else default_helper()
    if not helper.is_file() and not helper.is_symlink():
        return 'not_built'
    if not helper_safe(helper):
        return 'unsafe'
    if expected_helper_version() is None:
        return 'source_missing'
    return 'current' if helper_current(helper, run=run or Runner()) else 'outdated'


def default_cache() -> Path:
    """The verified-bundle cache beside the broker's store."""
    return default_state_dir() / 'verified-bundles.json'


def _default_hub_file() -> Path:
    import fabric_hub                                  # Fabric's public hub.json place, and its override
    return fabric_hub.hub_file()


def application_roots(home: Path) -> tuple:
    """The places an enrolled app may be installed: the system's and this account's Applications."""
    return (Path('/Applications'), Path(home) / 'Applications')


def stable_executable(arg: str) -> bool:
    return arg.startswith(STABLE_EXECUTABLE_PREFIXES) and '/Cellar/' not in arg and '..' not in arg


def digest(path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def trusted_path(path, directory: bool = False, *, interpreter: bool = False) -> Path:
    """`path`, when it and every folder above it belong to root or this account and no one else may write them.

    No component may be a link (the path is its own resolution). `/Applications` as macOS ships it (`root:admin
    0775`) is the one admin-writable parent an app may have: its admin group may install bundles, and each enrolled
    bundle must itself be non-group-writable and pass the pinned signature check. `interpreter` extends the same
    narrow exception to Homebrew's `Cellar` folder, for a launchd job's interpreter only. A world-writable folder is
    allowed only with the sticky bit (`/tmp`), where no one may replace another's entry."""
    p = Path(path).absolute()
    if p != p.resolve():
        raise LifecycleError('identity_mismatch')
    try:
        st = p.stat()
    except FileNotFoundError:
        raise LifecycleError('not_installed') from None
    if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
        raise LifecycleError('identity_mismatch')
    if directory != stat.S_ISDIR(st.st_mode):
        raise LifecycleError('identity_mismatch')
    for parent in p.parents:
        st = parent.stat()
        system_applications = (
            parent == Path('/Applications')
            and st.st_uid == 0
            and st.st_gid == ADMIN_GID
            and stat.S_IMODE(st.st_mode) == 0o775
        )
        brew_interpreter_parent = (
            interpreter
            and parent == Path('/opt/homebrew/Cellar')
            and st.st_uid == os.getuid()
            and st.st_gid == ADMIN_GID
            and stat.S_IMODE(st.st_mode) == 0o775
        )
        if st.st_uid not in (0, os.getuid()) or (
            st.st_mode & 0o022
            and not st.st_mode & stat.S_ISVTX
            and not system_applications
            and not brew_interpreter_parent
        ):
            raise LifecycleError('identity_mismatch')
    return p


class Runner:
    """Runs a fixed system tool with a minimal environment, each child in a process group of its own.

    On a timeout the whole group is killed, so a tool that spawned children leaves no orphan, and the answer comes now
    rather than when the orphan ends. `close` kills every group still running (the broker is stopping). A `codesign`
    that times out or cannot start is `verification_timeout` / `verification_unavailable`, never a false
    `identity_mismatch`; any other tool's is `delivery_unknown`."""

    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home) if home else Path.home()
        self._children: set = set()
        self._lock = threading.Lock()

    def __call__(self, args, timeout: float = 10) -> subprocess.CompletedProcess:
        args = [str(a) for a in args]
        env = {'PATH': SYSTEM_PATH, 'HOME': str(self.home), 'USER': os.environ.get('USER', '')}
        verifying = args[0] == CODESIGN
        try:
            child = subprocess.Popen(
                args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                **osproc.new_group(),
            )
        except OSError:
            raise LifecycleError('verification_unavailable' if verifying else 'delivery_unknown') from None
        with self._lock:
            self._children.add(child.pid)
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            osproc.signal_group(child.pid, _KILL)
            child.communicate()
            raise LifecycleError('verification_timeout' if verifying else 'delivery_unknown') from None
        finally:
            with self._lock:
                self._children.discard(child.pid)
        return subprocess.CompletedProcess(args, child.returncode, out, err)

    def close(self) -> None:
        with self._lock:
            groups = list(self._children)
        for pgid in groups:
            osproc.signal_group(pgid, _KILL)


def bundle_metadata_hash(path, *, clock: Callable[[], float] = time.monotonic,
                         budget: float = METADATA_WALK_SECONDS) -> str:
    """The cache key of one completed signature check, without reading large binaries again.

    Never an identity: an update changes it, which only makes the requirement be checked again. It covers every
    entry's place, device, inode, size, nanosecond modification and change times, mode, owner and link count, so
    restoring a modification time after an in-place write does not hide it. A link is recorded, never followed, and
    must resolve inside the bundle. A walk that runs out of `budget` decided nothing: `verification_pending`."""
    root = trusted_path(path, True)
    digestor = hashlib.sha256()
    pending = [root]
    count = 0
    deadline = clock() + budget
    while pending:
        if clock() > deadline:
            raise LifecycleError('verification_pending')
        entry = pending.pop()
        info = entry.lstat()
        count += 1
        if count > MAX_BUNDLE_ENTRIES:
            raise LifecycleError('identity_mismatch')
        record = [
            str(entry.relative_to(root)), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
        ]
        if stat.S_ISLNK(info.st_mode):
            if not entry.resolve().is_relative_to(root):
                raise LifecycleError('identity_mismatch')
            record.append(os.readlink(entry))
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(entry) as children:
                pending.extend(Path(child.path) for child in sorted(children, key=lambda c: c.name, reverse=True))
        elif not stat.S_ISREG(info.st_mode):
            raise LifecycleError('identity_mismatch')
        digestor.update(json.dumps(record, separators=(',', ':')).encode())
        digestor.update(b'\n')
    return digestor.hexdigest()


def requirement_for(identifier, team_id) -> str:
    """The pinned code requirement: an Apple-issued certificate of this team, for this identifier.

    Built from validated parts, never copied from the bundle, so registry or bundle text can never smuggle `or true`
    into the requirement language."""
    if not isinstance(identifier, str) or not BUNDLE_ID.fullmatch(identifier):
        raise LifecycleError('identity_mismatch')
    if not isinstance(team_id, str) or not TEAM_ID.fullmatch(team_id):
        raise LifecycleError('identity_mismatch')
    return f'anchor apple generic and identifier "{identifier}" and certificate leaf[subject.OU] = "{team_id}"'


def signature_info(path, *, run) -> dict:
    """The signing identifier, team and ad hoc flag, as `codesign -d --verbose=2` reports them."""
    result = run([CODESIGN, '-d', '--verbose=2', str(path)])
    if result.returncode:
        raise LifecycleError('identity_mismatch')
    text = result.stderr + result.stdout
    fields = dict(re.findall(r'^(Identifier|TeamIdentifier|Signature)=(.+)$', text, re.M))
    team = fields.get('TeamIdentifier', '').strip()
    return {
        'identifier': fields.get('Identifier', '').strip() or None,
        'teamId': team if TEAM_ID.fullmatch(team) else None,
        'adhoc': fields.get('Signature', '').strip() == 'adhoc',
    }


def designated_requirement_text(path, *, run) -> Optional[str]:
    """The bundle's own designated requirement, recorded for audit only and never evaluated."""
    result = run([CODESIGN, '-d', '-r-', str(path)])
    found = re.search(r'^designated => (.+)$', result.stdout + result.stderr, re.M)
    return found.group(1).strip()[:2048] if found and not result.returncode else None


def satisfies(path, requirement: str, *, deep: bool, run) -> bool:
    """The signature is valid (strict, deep when asked) AND satisfies `requirement`."""
    args = [CODESIGN, '--verify'] + (['--deep'] if deep else []) + ['--strict', '-R', '=' + requirement, str(path)]
    return run(args, timeout=DEEP_VERIFY_TIMEOUT if deep else PLAIN_VERIFY_TIMEOUT).returncode == 0


def installed_bundle(path, roots) -> Path:
    p = trusted_path(path, True)
    if not any(p.is_relative_to(root) for root in roots) or p.suffix != '.app':
        raise LifecycleError('not_installed')
    return p


def _bundle_plist(p: Path) -> dict:
    metadata = plistlib.loads((p / 'Contents/Info.plist').read_bytes())
    trusted_path(p / 'Contents/MacOS' / metadata['CFBundleExecutable'])
    return metadata


def inspect_app(path, *, run, roots) -> tuple:
    """(identity, metadata hash) of an installed, validly signed bundle: what an owner enrolls.

    Refuses an ad hoc or unsigned bundle, a signing identifier other than the Info.plist bundle id, and a bundle that
    changes while it is checked. Switchboard's identity also records its bundled CLI, signed by the same team."""
    p = installed_bundle(path, roots)
    before = bundle_metadata_hash(p)
    bundle_id = _bundle_plist(p)['CFBundleIdentifier']
    signature = signature_info(p, run=run)
    if signature['adhoc'] or not signature['teamId'] or signature['identifier'] != bundle_id:
        raise LifecycleError('identity_mismatch')
    requirement = requirement_for(bundle_id, signature['teamId'])
    if not satisfies(p, requirement, deep=True, run=run):
        raise LifecycleError('identity_mismatch')
    identity = {
        'scheme': IDENTITY_SCHEME,
        'bundleId': bundle_id,
        'teamId': signature['teamId'],
        'requirement': requirement,
        'designatedRequirement': designated_requirement_text(p, run=run),
    }
    if bundle_id == SWITCHBOARD_BUNDLE_ID:
        cli = trusted_path(p / SWITCHBOARD_CLI)
        cli_signature = signature_info(cli, run=run)
        if cli_signature['adhoc'] or cli_signature['teamId'] != signature['teamId']:
            raise LifecycleError('identity_mismatch')
        if not satisfies(cli, requirement_for(cli_signature['identifier'], signature['teamId']), deep=False, run=run):
            raise LifecycleError('identity_mismatch')
        identity['readinessCli'] = {'path': SWITCHBOARD_CLI, 'identifier': cli_signature['identifier']}
    after = bundle_metadata_hash(p)
    if before != after:
        raise LifecycleError('identity_changed_during_verification')
    return identity, after


def verify_app(path, identity: dict, *, run, roots) -> Optional[str]:
    """The bundle's metadata hash when the installed bundle satisfies the pinned requirement, else None.

    The requirement is rebuilt from the pinned team and bundle id. A bundle that changes while it is checked is None:
    neither trusted nor cached, and the next probe checks it again."""
    p = installed_bundle(path, roots)
    before = bundle_metadata_hash(p)
    if _bundle_plist(p).get('CFBundleIdentifier') != identity['bundleId']:
        return None
    if not satisfies(p, requirement_for(identity['bundleId'], identity['teamId']), deep=True, run=run):
        return None
    if bundle_metadata_hash(p) != before:
        return None
    return before


def launchd_fingerprint(path, *, home) -> dict:
    """A background job's identity: its label, the hash of its definition and the hashes of what it runs.

    Only a LaunchAgent of this account. The first argument is the interpreter: behind a stable path it is pinned by
    that path (it must still be trusted where it resolves), otherwise by the bytes it resolves to. Every other
    absolute file it names is pinned by its bytes, and so is the whole release folder a `releases/<version>/` argument
    sits in, because startup effects belong to that immutable content and not only to the entry point."""
    p = trusted_path(path)
    if p.parent != Path(home) / 'Library/LaunchAgents' or p.suffix != '.plist':
        raise LifecycleError('not_installed')
    metadata = plistlib.loads(p.read_bytes())
    args = metadata.get('ProgramArguments', [])
    label = metadata.get('Label', '')
    if not isinstance(label, str) or not LAUNCHD_LABEL.fullmatch(label) or not isinstance(args, list) or not args:
        raise LifecycleError('identity_mismatch')
    if not isinstance(args[0], str) or not Path(args[0]).is_absolute():
        raise LifecycleError('identity_mismatch')
    hashes = {}
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            raise LifecycleError('identity_mismatch')
        if Path(arg).is_absolute() and Path(arg).is_file():
            if index == 0 and stable_executable(arg):
                trusted_path(Path(arg).resolve(), interpreter=True)
                hashes[arg] = STABLE_PIN
                continue
            artifact = Path(arg).resolve() if index == 0 else Path(arg)
            hashes[str(artifact)] = digest(trusted_path(artifact, interpreter=index == 0))
    if not hashes:
        raise LifecycleError('identity_mismatch')
    for arg in args[1:]:
        p_arg = Path(arg)
        if p_arg.is_absolute() and p_arg.is_file() and '/releases/' in str(p_arg):
            prefix, suffix = str(p_arg).split('/releases/', 1)
            release = Path(prefix) / 'releases' / suffix.split('/')[0]
            for file in sorted(release.rglob('*')):
                if file.is_symlink():
                    raise LifecycleError('identity_mismatch')
                if file.is_file() and '__pycache__' not in file.parts:
                    hashes[str(file)] = digest(trusted_path(file))
    return {'label': label, 'plistHash': digest(p), 'artifacts': hashes}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise LifecycleError('identity_mismatch')


def _loopback_opener():
    """No proxy and no redirect: a probe reaches the loopback origin it was given, or nothing."""
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _private_regular_fd(path, limit: int) -> Optional[bytes]:
    """The bytes of a small regular file this account owns and no one else may write, read without following a link
    and without waiting on a FIFO; None when it is absent. Anything else is a ValueError."""
    try:
        fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError('unreadable') from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or not osprivacy.owned_by_me(fd)
            or osprivacy.others_may_write(fd)
            or info.st_size > limit
        ):
            raise ValueError('not a private regular file')
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def read_fabric_hub(path) -> Optional[dict]:
    """`hub.json` as Fabric writes it, or None when there is none; a ValueError when it is not a hub. Read like Fabric's
    own reader (a small regular file, no link, no FIFO), and only when this account owns it and nobody else can write
    it. The door token it names is never opened here."""
    data = _private_regular_fd(path, FABRIC_HUB_MAX_BYTES)
    if data is None:
        return None
    doc = json.loads(data)
    origin = doc.get('origin') if isinstance(doc, dict) else None
    port = FABRIC_HUB_ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
    if (
        not port
        or not 1024 <= int(port.group(1)) <= 65535
        or doc.get('protocol') != FABRIC_HUB_PROTOCOL
        or doc.get('mcp') != '/mcp'
        or type(doc.get('pid')) is not int
        or doc['pid'] <= 0
    ):
        raise ValueError('not a fabric hub')
    return doc


def _object(value) -> dict:
    """A JSON object read from a document, or an empty one for anything else (a string, a list, null)."""
    return value if isinstance(value, dict) else {}


def observation(state: str, **extra) -> dict:
    return {
        'state': state,
        'processReady': state == 'ready',
        'transportReady': False,
        'capabilityReady': False,
        'authState': 'unknown',
        'observedAt': int(time.time() * 1000),
        **extra,
    }


class MacAdapter:
    """The broker's adapter on macOS. Everything it touches can be injected, so its tests run no real tool:

    - `run`: the runner of `codesign`, `launchctl`, the helper and Switchboard's CLI (`Runner` by default);
    - `helper`: the compiled NSWorkspace helper (`default_helper()` by default);
    - `cache_path`: the owner-only verified-bundle cache (none: kept in memory only);
    - `fabric_hub_file`: Fabric's `hub.json` (its public place by default);
    - `home`: whose `~/Applications` and `~/Library/LaunchAgents` count;
    - `uid`: the launchd GUI domain, `gui/<uid>`."""

    def __init__(self, *, run=None, helper=None, cache_path=None, fabric_hub_file=None, home=None, uid=None,
                 platform: Optional[str] = None):
        self.home = Path(home) if home else Path.home()
        self.run = run if run is not None else Runner(self.home)
        self.helper = Path(helper) if helper else default_helper()
        self._helper_current_key = None                 # the file state that last answered the expected version
        self.fabric_hub_file = Path(fabric_hub_file) if fabric_hub_file else _default_hub_file()
        self.cache_path = Path(cache_path) if cache_path else None
        self.roots = application_roots(self.home)
        self.platform = platform or sys.platform
        self.domain = f'gui/{os.getuid() if uid is None else uid}'
        # (path, canonical identity) -> metadata hash of the bundle state that last passed the deep check;
        # (cli path, requirement) -> the stat key of the CLI that passed. Insertion order is age (CACHE_LIMIT).
        self._verified: dict = {}
        self._verified_lock = threading.Lock()
        # One deep check per bundle at a time: every caller missing the cache for it waits on the same future.
        self._inflight: dict = {}
        # One daemon verifier runs the deep checks in turn (in parallel they only slow each other under load). A
        # daemon thread, not an executor: an executor is joined at exit, and a check of up to DEEP_VERIFY_TIMEOUT
        # would hold the broker's exit.
        self._queue: queue.Queue = queue.Queue()
        self._verifier: Optional[threading.Thread] = None
        self._watcher: Optional[subprocess.Popen] = None
        self._load_cache()

    # -- the verified-bundle cache ---------------------------------------------------------------------------------

    def _remember(self, key, value) -> None:
        """Under `_verified_lock`: keep `value`, newest last, and drop the oldest past CACHE_LIMIT."""
        self._verified.pop(key, None)
        self._verified[key] = value
        while len(self._verified) > CACHE_LIMIT:
            self._verified.pop(next(iter(self._verified)))

    def _load_cache(self) -> None:
        """Believed only when it is ours alone: a regular file, not a link, owned by this account, unreadable to anyone
        else, the same bar as the broker's own state. Anything else is ignored, not trusted."""
        if not self.cache_path:
            return
        try:
            fd = osprivacy.open(self.cache_path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        except OSError:
            return
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or not osprivacy.owned_by_me(fd)
                or not osprivacy.private(fd)
                or info.st_size > CACHE_MAX_BYTES
            ):
                return
            saved = json.loads(os.read(fd, CACHE_MAX_BYTES + 1))
        except (OSError, ValueError):
            return
        finally:
            os.close(fd)
        if not isinstance(saved, dict):
            return
        with self._verified_lock:
            for k, v in saved.items():
                if isinstance(k, str) and '\n' in k and isinstance(v, str):
                    self._remember(tuple(k.split('\n', 1)), v)

    def _save_cache(self) -> None:
        """An optimisation only: a cache that cannot be written is checked again at the next start."""
        if not self.cache_path:
            return
        with self._verified_lock:
            doc = {k[0] + '\n' + k[1]: v for k, v in self._verified.items()
                   if isinstance(k, tuple) and len(k) == 2 and isinstance(v, str)}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self.cache_path.parent, prefix='.verified-', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:      # mkstemp: owner-only from the start
                    json.dump(doc, stream, sort_keys=True)
                os.replace(tmp, self.cache_path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass

    # -- identity ----------------------------------------------------------------------------------------------------

    def _app_matches(self, target: dict, deadline: Optional[float] = None) -> bool:
        identity = target['identity']
        if identity.get('scheme') != IDENTITY_SCHEME:
            return False                                # a record of another scheme is never trusted
        key = (target['path'], canonical(identity))
        current = bundle_metadata_hash(target['path'])
        with self._verified_lock:
            if self._verified.get(key) == current:
                return True
            future = self._inflight.get(key)
            if future is None:
                future = concurrent.futures.Future()
                self._inflight[key] = future
                if self._verifier is None:
                    self._verifier = threading.Thread(target=self._verify_loop, name='lifecycle-verify', daemon=True)
                    self._verifier.start()
                self._queue.put((future, key, target['path'], identity))
        wait = VERIFY_WAIT_SECONDS if deadline is None else max(0.0, deadline - time.time())
        try:
            return future.result(timeout=wait)
        except concurrent.futures.TimeoutError:
            raise LifecycleError('verification_pending') from None

    def _verify_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, key, path, identity = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(self._verify(key, path, identity))
            except BaseException as exc:  # noqa: BLE001 - the waiter re-raises it as its answer
                future.set_exception(exc)

    def _verify(self, key, path, identity) -> bool:
        try:
            verified = verify_app(path, identity, run=self.run, roots=self.roots)
        finally:
            with self._verified_lock:
                self._inflight.pop(key, None)
        with self._verified_lock:
            if verified is None:
                self._verified.pop(key, None)
            else:
                self._remember(key, verified)
        self._save_cache()
        return verified is not None

    def identity_matches(self, target: dict, *, deadline: Optional[float] = None) -> bool:
        """False for another identity or nothing installed; `verification_pending`, `verification_timeout` and
        `verification_unavailable` raise, because they decided nothing."""
        try:
            if target['kind'] == 'app':
                return self._app_matches(target, deadline)
            return launchd_fingerprint(target['path'], home=self.home) == target['identity']
        except LifecycleError as exc:
            if exc.code in ('identity_mismatch', 'not_installed'):
                return False
            raise
        except (OSError, ValueError, KeyError):
            return False

    def fingerprint(self, kind: str, path) -> dict:
        """What an owner enrolls for an installed target (the CLI's enroll verb, step L3)."""
        if self.platform != 'darwin':
            raise LifecycleError('unsupported_platform')
        if kind == 'app':
            return inspect_app(path, run=self.run, roots=self.roots)[0]
        if kind == 'launchd':
            return launchd_fingerprint(path, home=self.home)
        raise LifecycleError('invalid_argument')

    # -- observation -------------------------------------------------------------------------------------------------

    def _helper_problem(self) -> Optional[str]:
        """None when the helper may run, else why not: `helper_not_built` (no file), `helper_unsafe` (a link, or a
        file or folder others may change), `helper_source_missing` (this engine's `native_app.swift` cannot be read,
        so no version is known) or `helper_outdated` (built from another `native_app.swift`). All are
        `setup_required`; `full lifecycle build-helper` repairs the first, the third needs the engine reinstalled.

        A helper that answered the expected version is not asked again until its file changes (device, inode, size,
        modification and change times). A mismatch is never remembered: a rebuilt helper is current at once."""
        if not self.helper.is_file() and not self.helper.is_symlink():
            return 'helper_not_built'
        if not helper_safe(self.helper):
            return 'helper_unsafe'
        if expected_helper_version() is None:
            return 'helper_source_missing'
        try:
            info = self.helper.stat()
        except OSError:
            return 'helper_not_built'
        key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._helper_current_key == key:
            return None
        if not helper_current(self.helper, run=self.run):
            return 'helper_outdated'
        self._helper_current_key = key
        return None

    def _helper_ready(self) -> bool:
        return self._helper_problem() is None

    def _helper_answer(self, args, timeout: float = 10) -> dict:
        try:
            answer = json.loads(self.run([str(self.helper), *args], timeout=timeout).stdout)
        except ValueError:
            raise LifecycleError('failed') from None
        if not isinstance(answer, dict) or not isinstance(answer.get('state'), str):
            raise LifecycleError('failed')
        return answer

    def _helper_status(self, target: dict) -> dict:
        return self._helper_answer(['status', target['path']])

    def _launchd_state(self, target: dict) -> dict:
        if self.run([LAUNCHCTL, 'print', self.domain]).returncode:
            return observation('login_required')
        disabled = self.run([LAUNCHCTL, 'print-disabled', self.domain])
        if disabled.returncode:
            disabled = self.run([LAUNCHCTL, 'print-disabled', self.domain])     # once more: it may be transient
        if disabled.returncode:
            # Unreadable is not a hold: `held_off` would tell the caller the operator switched it off. An unknown
            # disable state starts nothing and is answered as a failure the caller may retry.
            return observation('failed', reason='launchd_disabled_state_unknown')
        label = target['identity']['label']
        if re.search(r'"' + re.escape(label) + r'"\s*=>\s*(true|disabled)', disabled.stdout):
            return observation('held_off')              # a disable is the operator's durable hold
        result = self.run([LAUNCHCTL, 'print', self.domain + '/' + label])
        if result.returncode:
            return observation('stopped', loaded=False)
        pid = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
        return observation('ready' if pid else 'stopped', loaded=True, pid=int(pid.group(1)) if pid else None)

    def _cli_trusted(self, target: dict) -> Optional[Path]:
        """Switchboard's CLI, when its signature satisfies the enrolled team's requirement for its enrolled
        identifier; checked again whenever the file changes, never run before that."""
        identity = target['identity']
        cli_identity = identity.get('readinessCli') or {}
        if identity.get('scheme') != IDENTITY_SCHEME or cli_identity.get('path') != SWITCHBOARD_CLI:
            return None
        cli = trusted_path(Path(target['path']) / SWITCHBOARD_CLI)
        info = cli.lstat()
        stamp = json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode])
        requirement = requirement_for(cli_identity['identifier'], identity['teamId'])
        key = (str(cli), requirement)
        with self._verified_lock:
            if self._verified.get(key) == stamp:
                return cli
        if not satisfies(cli, requirement, deep=False, run=self.run):
            return None
        with self._verified_lock:
            self._remember(key, stamp)
        return cli

    def _switchboard_probe(self, target: dict, seen: dict) -> dict:
        """Ready when the enrolled app runs AND its runtime reports online. No caller supplies a CLI path, arguments
        or a data root; a runtime online while the app is not running is someone else's runtime."""
        if seen['state'] not in ('ready', 'stopped'):
            return seen
        try:
            cli = self._cli_trusted(target)
        except LifecycleError as exc:
            if exc.code in ('identity_mismatch', 'not_installed'):
                cli = None
            else:
                raise
        if cli is None:
            return seen | {'state': 'identity_mismatch', 'capabilityReady': False}
        response = self.run([str(cli), '--json', 'status'], timeout=3)
        if response.returncode:
            return seen | {'state': 'auth_required', 'authState': 'unknown', 'capabilityReady': False,
                           'reason': 'switchboard_status_failed'}
        try:
            document = json.loads(response.stdout)
            data = document['data']
            if document.get('ok') is not True or not isinstance(data, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return seen | {'state': 'identity_mismatch', 'capabilityReady': False}
        address = data.get('proxy_address')
        online = (
            data.get('live_mode') == SWITCHBOARD_ONLINE
            and isinstance(address, str)
            and bool(SWITCHBOARD_PROXY.fullmatch(address))
        )
        if online and seen['state'] == 'stopped':
            return seen | {'state': 'identity_mismatch', 'transportReady': True, 'capabilityReady': False,
                           'reason': 'runtime_owner_is_not_enrolled_app'}
        if online:
            return seen | {'state': 'ready', 'transportReady': True, 'capabilityReady': True,
                           'capability': 'runtime-online', 'authState': 'owner_authenticated'}
        if seen['state'] == 'stopped':
            return seen
        # The app runs and its runtime is offline. Starting the CLI's own server here would make a second owner of
        # the shared store, so this is reported, not repaired.
        return seen | {'state': 'capability_missing', 'transportReady': False, 'capabilityReady': False,
                       'reason': 'app_runtime_offline'}

    def _fabric_hub_probe(self, seen: dict) -> dict:
        """Fabric serves when `hub.json` names the running pid and its origin answers.

        No hub while the process runs is `capability_missing`: progress during a start (the stack can take minutes),
        and the honest answer for a Fabric stuck in its failure dialog. The answer is any HTTP response to `GET /`
        (Fabric answers 404 to anything but `POST /mcp` and records nothing for it); no credential is sent."""
        if seen['state'] != 'ready':
            return seen
        missing = seen | {'state': 'capability_missing', 'capabilityReady': False}
        try:
            hub = read_fabric_hub(self.fabric_hub_file)
        except (ValueError, OSError):
            return missing | {'reason': 'hub_invalid'}
        if hub is None:
            return missing | {'reason': 'hub_not_published'}
        if hub['pid'] != seen.get('pid'):
            return missing | {'reason': 'hub_names_another_process'}     # a hub left by a Fabric that died
        try:
            with _loopback_opener().open(hub['origin'] + '/', timeout=2):
                pass
        except urllib.error.HTTPError:
            pass                                        # an HTTP answer from the published origin: it listens
        except (OSError, ValueError, LifecycleError):
            return seen | {'state': 'remote_unreachable', 'capabilityReady': False, 'reason': 'hub_not_answering'}
        return seen | {'transportReady': True, 'capabilityReady': True, 'capability': 'fabric-hub',
                       'authState': 'not_checked'}

    def _service_probe(self, readiness: dict, seen: dict) -> dict:
        """A Fabric service is ready when its well-known document names this service, instance, build and pid."""
        try:
            with _loopback_opener().open(readiness['origin'] + '/.well-known/fabric-service', timeout=2) as response:
                doc = json.loads(response.read(128 * 1024))
        except urllib.error.HTTPError as exc:
            return seen | {'state': 'auth_required' if exc.code in (401, 403) else 'remote_unreachable'}
        except (OSError, ValueError, LifecycleError):
            return seen | {'state': 'remote_unreachable'}
        if not isinstance(doc, dict):
            return seen | {'state': 'identity_mismatch'}
        service, process = _object(doc.get('service')), _object(doc.get('process'))
        build = _object(service.get('build')).get('digest') or _object(service.get('build')).get('commit')
        if (
            service.get('id') != readiness['serviceId']
            or service.get('instance') != readiness['instance']
            or build != readiness['buildDigest']
            or process.get('pid') != seen.get('pid')
        ):
            return seen | {'state': 'identity_mismatch'}
        capability = readiness.get('capability')
        capabilities = _object(_object(doc.get('surfaces')).get('mcp')).get('capabilities')
        if not isinstance(capabilities, list):
            capabilities = []                           # a string would answer `in` by substring
        # A capability in the descriptor is transport evidence, not an authenticated call to it.
        if capability and capability not in capabilities:
            return seen | {'state': 'capability_missing', 'transportReady': True}
        if capability and capability != 'process':
            return seen | {'state': 'capability_missing', 'transportReady': True,
                           'reason': 'authenticated_capability_probe_not_enrolled'}
        ready = doc.get('status') == 'ready'
        return seen | {'transportReady': True, 'capabilityReady': ready, 'state': 'ready' if ready else 'starting',
                       'authState': 'not_checked'}

    def probe(self, target: dict) -> dict:
        if self.platform != 'darwin':
            return observation('unsupported_platform')
        if not self.identity_matches(target):
            return observation('identity_mismatch')
        if target['kind'] == 'app':
            if self.run([LAUNCHCTL, 'print', self.domain]).returncode:
                return observation('login_required')
            problem = self._helper_problem()
            if problem:
                return observation('setup_required', reason=problem)
            try:
                response = self._helper_status(target)
            except LifecycleError:
                return observation('failed')
            seen = observation(response['state'], **{k: response[k] for k in HELPER_FIELDS if k in response})
        else:
            seen = self._launchd_state(target)
        kind = target['readiness']['kind']
        if kind == 'switchboard-runtime':
            return self._switchboard_probe(target, seen)
        if kind == 'fabric-hub':
            return self._fabric_hub_probe(seen)
        if seen['state'] != 'ready':
            return seen
        if kind == 'process':
            return seen | {'capabilityReady': True, 'capability': 'process', 'authState': 'not_applicable'}
        return self._service_probe(target['readiness'], seen)

    # -- effects -----------------------------------------------------------------------------------------------------

    def start(self, target: dict) -> str:
        if self.platform != 'darwin':
            raise LifecycleError('unsupported_platform')
        if not self.identity_matches(target):
            raise LifecycleError('identity_mismatch')
        if target['kind'] == 'app':
            if self.run([LAUNCHCTL, 'print', self.domain]).returncode:
                raise LifecycleError('login_required')
            if not self._helper_ready():
                raise LifecycleError('setup_required')
            args = ['open', target['path']]
            if target.get('backgroundLaunch') is True:
                args.append('--background')             # only for a target the owner enrolled as understanding it
            response = self._helper_answer(args, timeout=12)
            if response['state'] not in ('ready', 'accepted_by_os'):
                raise LifecycleError(response['state'])
            return 'launch_services_open'
        state = self._launchd_state(target)
        if state['state'] not in ('stopped', 'ready'):
            raise LifecycleError(state['state'])
        if state['state'] == 'ready':
            return 'already_running'
        label = target['identity']['label']
        # No enable, no -k, no kill, no restart: an operator's disable is a durable hold, never undone here.
        arguments = (
            [LAUNCHCTL, 'kickstart', self.domain + '/' + label]
            if state['loaded']
            else [LAUNCHCTL, 'bootstrap', self.domain, target['path']]
        )
        if self.run(arguments).returncode:
            raise LifecycleError('delivery_unknown')
        return 'launchd_kickstart' if state['loaded'] else 'launchd_bootstrap'

    def stop(self, target: dict, pid: int, *, force: bool = False, mode: str = 'quit') -> str:
        """Ask exactly the enrolled process `pid` to end.

        The pid is read again from the OS right before acting: a process that changed since the broker decided
        (relaunched, replaced) is never touched. An app is first asked through its own quit path (`quit`, like Cmd+Q);
        `term` sends SIGTERM; SIGKILL only on the owner's `force`. A background job is booted out (SIGTERM, its
        ExitTimeOut, then unloaded so a KeepAlive job does not come straight back), or killed on `force`."""
        if self.platform != 'darwin':
            raise LifecycleError('unsupported_platform')
        if not self.identity_matches(target):
            raise LifecycleError('identity_mismatch')
        if target['kind'] == 'app':
            if not self._helper_ready():
                raise LifecycleError('setup_required')
            current = self._helper_status(target)
            if current.get('state') != 'ready' or current.get('pid') != pid:
                raise LifecycleError('identity_mismatch')
            if mode == 'quit' and not force:
                response = self._helper_answer(['quit', target['path'], str(pid)])
                if response.get('state') == 'quit_requested':
                    return 'quit_requested'
                if response.get('state') == 'identity_mismatch':
                    # The pid no longer belongs to the enrolled bundle. Either the app exited on its own between
                    # `status` and `quit` (the pid is gone: the stop is done, `already_exited`), or the pid is someone
                    # else's process now. Probe again to tell them apart; nothing is signalled in either case.
                    try:
                        again = self._helper_status(target)
                    except LifecycleError:
                        raise LifecycleError('identity_mismatch') from None
                    if again.get('state') == 'stopped':
                        return 'already_exited'
                    raise LifecycleError('identity_mismatch')
                if response.get('state') != 'quit_refused':
                    raise LifecycleError('failed')
                # A refused quit falls through to SIGTERM rather than reporting a stop it never asked for.
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                return 'already_exited'
            except PermissionError:
                raise LifecycleError('denied') from None
            return 'sigkill' if force else 'sigterm'
        state = self._launchd_state(target)
        if state.get('pid') != pid:
            raise LifecycleError('identity_mismatch')
        address = self.domain + '/' + target['identity']['label']
        if force:
            if self.run([LAUNCHCTL, 'kill', 'SIGKILL', address]).returncode:
                raise LifecycleError('delivery_unknown')
            return 'launchd_sigkill'
        if self.run([LAUNCHCTL, 'bootout', address], timeout=30).returncode:
            raise LifecycleError('delivery_unknown')
        return 'launchd_bootout'

    # -- the watcher -------------------------------------------------------------------------------------------------

    def watch(self, on_event: Callable[[dict], None], stop_event: threading.Event,
              *, popen=subprocess.Popen) -> None:
        """Feed the helper's launch, terminate and activate events to `on_event` until `stop_event` is set.

        One resident helper instead of a timer. It exits when its stdin closes, so it cannot outlive the broker; a
        helper that crashes is started again with backoff (5 s, doubling to 300 s, reset after a minute of life)."""
        backoff = 5
        while not stop_event.is_set():
            if not self._helper_ready():
                return
            started = time.monotonic()
            try:
                proc = popen(
                    [str(self.helper), 'watch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, env={'PATH': SYSTEM_PATH, 'HOME': str(self.home)},
                    **osproc.new_group(),
                )
            except OSError:
                stop_event.wait(backoff)
                continue
            self._watcher = proc
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(event, dict)
                    and event.get('event') in WATCH_EVENTS
                    and isinstance(event.get('bundleId'), str)
                    and type(event.get('pid')) is int
                ):
                    on_event({'event': event['event'], 'bundleId': event['bundleId'], 'pid': event['pid']})
            proc.wait()
            if stop_event.is_set():
                return
            backoff = 5 if time.monotonic() - started > 60 else min(backoff * 2, 300)
            stop_event.wait(backoff)

    def stop_watch(self) -> None:
        proc = self._watcher
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            osproc.signal_group(proc.pid, _KILL)
            proc.wait(timeout=2)

    def close(self) -> None:
        """Stop verifying and end every child still running, without waiting (the broker is stopping)."""
        self._queue.put(None)
        self.stop_watch()
        close = getattr(self.run, 'close', None)
        if close:
            close()


class UnsupportedAdapter:
    """The adapter of a platform with none yet (Linux, Windows): every call says `unsupported_platform`, so nothing
    is ever reported as started, stopped or verified there."""

    def __init__(self, platform: str):
        self.platform = platform

    def identity_matches(self, target: dict, *, deadline: Optional[float] = None) -> bool:
        raise LifecycleError('unsupported_platform')

    def probe(self, target: dict) -> dict:
        return observation('unsupported_platform', platform=self.platform)

    def start(self, target: dict) -> str:
        raise LifecycleError('unsupported_platform')

    def stop(self, target: dict, pid: int, *, force: bool = False, mode: str = 'quit') -> str:
        raise LifecycleError('unsupported_platform')

    def fingerprint(self, kind: str, path) -> dict:
        raise LifecycleError('unsupported_platform')


def platform_adapter(platform: Optional[str] = None, **kwargs):
    """The adapter for this platform: `MacAdapter` on macOS, `UnsupportedAdapter` elsewhere."""
    platform = platform or sys.platform
    if platform == 'darwin':
        return MacAdapter(platform=platform, **kwargs)
    return UnsupportedAdapter(platform)


# -- the helper, built for a source install --------------------------------------------------------------------------

XCRUN = '/usr/bin/xcrun'


def _xcrun(args, *, run) -> Optional[str]:
    """One absolute path `xcrun` answers (Xcode or the Command Line Tools), else None."""
    try:
        found = run([XCRUN, *args], timeout=30)
    except LifecycleError:
        return None
    path = found.stdout.strip()
    return path if not found.returncode and path.startswith('/') and Path(path).exists() else None


def find_swiftc(*, run=None) -> Optional[str]:
    """`swiftc` as `xcrun` finds it, else None."""
    return _xcrun(['--find', 'swiftc'], run=run or Runner())


def build_helper(dest=None, *, swiftc: Optional[str] = None, sdk: Optional[str] = None, run=None,
                 platform: Optional[str] = None) -> Path:
    """Compile `native_app.swift` into `dest` (`default_helper()`): owner-only folder, executable, replaced whole.

    The source install's path: the helper is built on the machine that runs it, by the account that runs it, from the
    source shipped in the package; on Apple silicon the linker signs it ad hoc. Refusals: `unsupported_platform` off
    macOS, `swiftc_missing` without the Command Line Tools (`xcode-select --install`), `helper_build_failed` when the
    compiler refuses (its last lines are on the exception's `detail`)."""
    if (platform or sys.platform) != 'darwin':
        raise LifecycleError('unsupported_platform')
    run = run or Runner()
    swiftc = swiftc or find_swiftc(run=run)
    # The compiler runs with the system PATH only, so it is told where the macOS SDK is: without it a toolchain
    # `swiftc` cannot load its standard library.
    sdk = sdk or _xcrun(['--sdk', 'macosx', '--show-sdk-path'], run=run)
    if not swiftc or not sdk:
        raise LifecycleError('swiftc_missing')
    dest = Path(dest) if dest else default_helper()
    if dest.parent.exists():
        # A folder that is not ours alone is refused, never re-moded: `--dest` may name any folder.
        folder = dest.parent
        if folder.is_symlink() or not osprivacy.owned_by_me(folder) or osprivacy.others_may_write(folder):
            raise LifecycleError('unsafe_config')
    else:
        dest.parent.mkdir(parents=True, mode=0o700)
        osprivacy.private_folder(dest.parent)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix='.' + dest.name + '.', suffix='.build')
    os.close(fd)
    try:
        try:
            result = run([swiftc, '-sdk', sdk, '-O', '-o', tmp, str(HELPER_SOURCE)], timeout=600)
        except LifecycleError:
            raise LifecycleError('helper_build_failed') from None
        if result.returncode or not Path(tmp).stat().st_size:
            error = LifecycleError('helper_build_failed')
            error.detail = '\n'.join((result.stderr or '').strip().splitlines()[-5:])
            raise error
        os.chmod(tmp, 0o700)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dest


def main(argv=None) -> int:
    """`build-helper [--dest PATH]`: compile the helper for a source install. `helper`: where it is, whether it
    exists and its `state` (`current`, `outdated`, `not_built`). Each prints one JSON document; a refusal prints its
    code and exits 1."""
    import argparse

    parser = argparse.ArgumentParser(prog='macos.py', description='The lifecycle broker\'s macOS helper.')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build-helper', help='compile native_app.swift with swiftc (source install)')
    build.add_argument('--dest', help='where to write it (default: <state>/node-lifecycle/bin/lifecycle-native)')
    sub.add_parser('helper', help='where the helper is, whether it is built and from this engine\'s source')
    args = parser.parse_args(argv)
    try:
        if args.command == 'build-helper':
            print(json.dumps({'helper': str(build_helper(args.dest)), 'built': True}))
        else:
            path = default_helper()
            print(json.dumps({'helper': str(path), 'built': path.is_file(), 'state': helper_state(path),
                              'expectedVersion': expected_helper_version()}))
    except LifecycleError as exc:
        print(json.dumps({'error': exc.code, 'detail': getattr(exc, 'detail', '')}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
