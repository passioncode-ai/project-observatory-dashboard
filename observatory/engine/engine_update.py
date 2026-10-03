#!/usr/bin/env python3
"""`full update`: move this installation to a published release, verified and reversible.

    full update                     preview: current, target, what --apply would do
    full update --check             exit 0 up to date, 10 update available, 3 could not look
    full update --apply             install the target release and upgrade the workspace
    full update --version X.Y.Z     pin the target instead of the latest release

Two operators on two machines stay on one engine version by running the same
command after each release. The source is the project's GitHub releases: the
wheel and a `SHA256SUMS` file. A wheel is installed only when its bytes match
BOTH the digest GitHub publishes for the asset and the line in `SHA256SUMS`;
either mismatch refuses before anything is stopped or installed.

--apply is one transaction over three things that can each fail on their own —
the Python environment, the background jobs and the workspace:

1. fetch and verify the target wheel, and a rollback wheel for the running
   version (cached from an earlier update, or downloaded from its own release
   and verified the same way). Without one, apply refuses unless --no-rollback;
2. stop this workspace's launchd tick and server, if loaded, through the
   engine's own launchd helpers (never with --writers-stopped: the caller did);
3. snapshot the workspace with THIS code, which is the code able to restore it;
4. install the wheel with the running interpreter (pip, else `uv pip`), the
   `full` extra included so a dependency added by the release is installed,
   constrained by the lock the wheel carries (LOCK_MEMBER) so the transitive
   dependencies land at the versions that release was tested with;
5. run the new release's `upgrade --apply --writers-stopped` in a new process;
6. verify the installed version, the pinned dependencies and `doctor`;
7. restart exactly the jobs step 2 stopped.

A failure after step 4 reinstalls the rollback wheel. When the workspace was
changed by then (a committed upgrade), the pre-update snapshot is restored at the
same path and the changed copy is kept beside it — nothing is overwritten. Exit
codes are listed in EXIT_* below and in docs/ONBOARDING.md, "Staying in step".
"""
from __future__ import annotations
import argparse
import dataclasses
import datetime
import email.parser
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import configuration as config
import workspace
import workspace_upgrade
import backup_vault
# Imported before the installer runs: once pip has replaced the package's files, a
# lazy import here would load NEW code into this OLD process. Everything the
# rollback path needs is therefore already in memory by the time anything changes.
from store import compatibility, migrate  # noqa: F401

DEFAULT_API = "https://api.github.com"
REPOSITORY_ENV, API_ENV = "OBSERVATORY_RELEASE_REPOSITORY", "OBSERVATORY_RELEASE_API"
DISTRIBUTION = "project-observatory"
WHEEL = "project_observatory-{version}-py3-none-any.whl"
SUMS = "SHA256SUMS"
CACHE = Path("backups") / "engine-releases"   # under `backups/`: never inside a snapshot
LOG = Path("store") / "logs" / "update.jsonl"
KEEP_CACHED = 3
#: Where a release wheel carries its tested dependency set. A release publishes
#: only the wheel and SHA256SUMS, so the lock travels inside the verified wheel;
#: a lock file at any other path in the archive is not the release's and is ignored.
LOCK_MEMBER = "observatory/engine/requirements-full.lock"
MAX_LOCK = 1024 * 1024
MAX_JSON, MAX_SUMS, MAX_WHEEL = 2 * 1024 * 1024, 64 * 1024, 64 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

EXIT_OK = 0
EXIT_FAILED = 1            # apply failed after changes began; rolled back, see the report
EXIT_REFUSED = 2           # refused before any change: verification, downgrade, unsafe state
EXIT_UNDETERMINED = 3      # the release could not be looked up (network, rate limit)
EXIT_NEEDS_PERSON = 4      # rollback incomplete; `human_steps` says exactly what to do
EXIT_SERVICES = 5          # updated, but a stopped job did not start again
EXIT_UPDATE_AVAILABLE = 10  # --check only


class UpdateError(RuntimeError):
    """A refusal or failure with the exit code it maps to. Never carries a credential."""

    def __init__(self, message: str, code: int = EXIT_REFUSED):
        super().__init__(message)
        self.code = code


class Undetermined(UpdateError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_UNDETERMINED)


class InstallFailed(RuntimeError):
    pass


def now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_tail(text: str, limit: int = 1200) -> str:
    """The end of an installer's output, minus any credentials in index URLs.

    pip echoes the index URL it uses, and a private index configured in pip.conf
    carries its user and password inside that URL; the line would otherwise reach the report."""
    text = re.sub(r"(https?://)[^\s/@]+@", r"\1***@", text or "")
    return text[-limit:].strip()


def check_url(url: str) -> str:
    """HTTPS anywhere; plain HTTP only to this machine (tests and local mirrors)."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise UpdateError("Release URL is malformed") from None
    if parsed.username is not None or parsed.password is not None:
        raise UpdateError("Release URLs must not carry credentials")
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK:
        return url
    raise UpdateError(f"Refusing a non-HTTPS release URL on host {parsed.hostname or '?'}")


def default_repository() -> str:
    """The repository the companion plugin's marketplace already names: releases and
    the plugin ship from one place, so the name is kept in one place too."""
    sys.path.insert(0, str(config.SOURCE / "tools"))
    import agent_plugin
    return agent_plugin.REPOSITORY


def plugin_advice() -> str:
    """What to do about the companion plugin once the engine is updated.

    `full agent install` is advice only when the engine's own channel manages the
    plugin: a launcher that installs it under its own marketplace id keeps it
    updated itself, and a second copy under the engine's id would fire every hook
    twice. The update has already succeeded, so a failure here falls back to the
    generic line instead of failing the report.
    """
    generic = "Restart Claude Code sessions and `project-observatory full agent install` if the plugin is used"
    try:
        sys.path.insert(0, str(config.SOURCE / "tools"))
        import agent_plugin
        return agent_plugin.update_advice()
    except Exception:  # noqa: BLE001 — advice, never a reason to fail a finished update
        return generic


def release_source(repository: str | None = None, api: str | None = None) -> tuple[str, str]:
    repo = repository or os.environ.get(REPOSITORY_ENV) or default_repository()
    base = (api or os.environ.get(API_ENV) or DEFAULT_API).rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", repo):
        raise UpdateError("Release repository must be OWNER/NAME")
    return check_url(base), repo


class _Redirects(urllib.request.HTTPRedirectHandler):
    """GitHub serves assets through a redirect; the same scheme rule applies to it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """Anonymous GitHub reads with bounded size, retries on transient failures only."""

    def __init__(self, timeout: float = 20.0, attempts: int = 3, sleep: Callable[[float], None] = time.sleep):
        self.timeout, self.attempts, self.sleep = timeout, max(1, attempts), sleep
        self.opener = urllib.request.build_opener(_Redirects())

    def _open(self, url: str, accept: str):
        check_url(url)
        request = urllib.request.Request(url, headers={
            "Accept": accept, "User-Agent": f"project-observatory/{config.VERSION} (update)",
            "X-GitHub-Api-Version": "2022-11-28"})
        last = "no attempt was made"
        for attempt in range(self.attempts):
            try:
                return self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == 404:
                    raise UpdateError(f"{urllib.parse.urlsplit(url).path} was not found", EXIT_REFUSED) from None
                if exc.code == 403 and (exc.headers.get("X-RateLimit-Remaining") == "0"):
                    raise Undetermined("GitHub API rate limit reached for anonymous requests; "
                                       "retry after it resets (about an hour at most)") from None
                if exc.code < 500 and exc.code != 429:
                    raise Undetermined(f"GitHub answered HTTP {exc.code}") from None
                last = f"HTTP {exc.code}"
            except UpdateError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last = f"{type(reason).__name__}: {reason}"
            if attempt + 1 < self.attempts:
                self.sleep(2 ** attempt)
        raise Undetermined(f"release source unreachable ({last})")

    def get_json(self, url: str) -> dict:
        with self._open(url, "application/vnd.github+json") as response:
            body = response.read(MAX_JSON + 1)
        if len(body) > MAX_JSON:
            raise UpdateError("Release document is larger than expected")
        try:
            doc = json.loads(body)
        except ValueError:
            raise Undetermined("Release document is not JSON") from None
        if not isinstance(doc, dict):
            raise Undetermined("Release document is not an object")
        return doc

    def fetch_file(self, url: str, dest: Path, limit: int) -> tuple[str, int]:
        """Stream into a new private file; returns (sha256, size)."""
        digest, size = hashlib.sha256(), 0
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as out, self._open(url, "application/octet-stream") as response:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        raise UpdateError(f"{dest.name} is larger than {limit} bytes; refusing")
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), size


@dataclasses.dataclass(frozen=True)
class Asset:
    name: str
    url: str
    digest: str | None
    size: int | None


@dataclasses.dataclass(frozen=True)
class Release:
    version: str
    tag: str
    page: str | None
    wheel: Asset | None
    sums: Asset | None

    def installable(self) -> str:
        """Empty when installable; otherwise why not."""
        for name, asset in ((WHEEL.format(version=self.version), self.wheel), (SUMS, self.sums)):
            if asset is None:
                return f"release {self.tag} has no {name} asset"
        return ""


def _asset(row: dict) -> Asset | None:
    url = row.get("browser_download_url")
    if not isinstance(url, str):
        return None
    raw = row.get("digest")
    digest = raw[7:] if isinstance(raw, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", raw) else None
    size = row.get("size") if type(row.get("size")) is int else None
    return Asset(row["name"], url, digest, size)


def parse_release(doc: dict, expected: str | None = None) -> Release:
    tag = doc.get("tag_name")
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag) if isinstance(tag, str) else None
    if not match:
        raise UpdateError("Release tag is not vX.Y.Z; refusing to guess its version")
    version = match.group(1)
    if doc.get("draft"):
        raise UpdateError(f"Release {tag} is a draft")
    if expected and version != expected:
        raise UpdateError(f"Asked for {expected}, the release is {version}")
    assets = {row["name"]: row for row in doc.get("assets") or []
              if isinstance(row, dict) and isinstance(row.get("name"), str)}
    wheel = assets.get(WHEEL.format(version=version))
    sums = assets.get(SUMS)
    return Release(version, tag, doc.get("html_url") if isinstance(doc.get("html_url"), str) else None,
                   _asset(wheel) if wheel else None, _asset(sums) if sums else None)


def resolve_release(fetcher: Fetcher, api: str, repo: str, version: str | None) -> Release:
    path = f"/releases/tags/v{version}" if version else "/releases/latest"
    return parse_release(fetcher.get_json(f"{api}/repos/{repo}{path}"), version)


def parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](\S+)", line.strip())
        if not match:
            continue
        digest, name = match.group(1).lower(), match.group(2)
        if out.get(name, digest) != digest:
            raise UpdateError(f"SHA256SUMS lists two different digests for {name}")
        out[name] = digest
    return out


def requirement_pins(requires: list[str], extras: frozenset[str] = frozenset({"full"})) -> list[dict]:
    """`Requires-Dist` lines that apply to a `[full]` install: name and exact pin.

    Only the `extra == "…"` marker is interpreted. A requirement with another
    marker (a Python version, a platform) is kept but marked conditional, and its
    presence is not asserted afterwards — this parser does not evaluate markers."""
    out = []
    for line in requires:
        spec, _, marker = line.partition(";")
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?\s*([^()]*?)\s*\)?\s*$", spec)
        if not match:
            continue
        wanted = re.findall(r"""extra\s*==\s*["']([^"']+)["']""", marker)
        if wanted and not set(wanted) & extras:
            continue
        rest = re.sub(r"\b(?:and|or)\b", "", re.sub(r"""extra\s*==\s*["'][^"']+["']""", "", marker)).strip(" ()")
        pin = re.fullmatch(r"==\s*([A-Za-z0-9.+!-]+)", match.group(2) or "")
        out.append({"name": match.group(1).lower().replace("_", "-"), "pin": pin.group(1) if pin else None,
                    "conditional": bool(rest)})
    return out


def wheel_metadata(wheel: Path, work: Path) -> dict:
    try:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            meta = [n for n in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            if len(meta) != 1:
                raise UpdateError("Wheel metadata is missing or ambiguous")
            headers = email.parser.Parser().parsestr(zf.read(meta[0]).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeError, OSError):
        raise UpdateError("Wheel is not a readable archive") from None
    return {"name": (headers.get("Name") or "").lower().replace("_", "-"), "version": headers.get("Version"),
            "requires": headers.get_all("Requires-Dist") or [], "constraints": wheel_lock(wheel, work)}


def wheel_lock(wheel: Path, folder: Path) -> Path | None:
    """The wheel's own LOCK_MEMBER written to a constraints file in `folder`, or None.

    None means the wheel carries no lock (a release older than the lock's
    shipping, or a hand-built wheel): the install still works, unconstrained,
    and the caller says so rather than pretending the tested set was installed."""
    try:
        with zipfile.ZipFile(wheel) as zf:
            try:
                info = zf.getinfo(LOCK_MEMBER)
            except KeyError:
                return None
            if info.file_size > MAX_LOCK:
                raise UpdateError(f"{wheel.name} carries a {info.file_size}-byte {LOCK_MEMBER}; refusing to read it")
            data = zf.read(info)
    except (zipfile.BadZipFile, OSError):
        raise UpdateError(f"{wheel.name} is not a readable archive") from None
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / f"constraints-{wheel.stem}.txt"
    target.write_bytes(data)
    return target


def fetch_verified(fetcher: Fetcher, release: Release, work: Path) -> tuple[Path, dict]:
    """The wheel, proven by the GitHub digest AND by SHA256SUMS, or a refusal."""
    why = release.installable()
    if why:
        raise UpdateError(why)
    for asset in (release.wheel, release.sums):
        if not asset.digest:
            raise UpdateError(f"GitHub publishes no sha256 digest for {asset.name} in {release.tag}; "
                              "a file that cannot be checked against it is not installed")
    sums_file = work / f"{release.version}-{SUMS}"
    digest, _ = fetcher.fetch_file(release.sums.url, sums_file, MAX_SUMS)
    if digest != release.sums.digest:
        raise UpdateError(f"{SUMS} of {release.tag} does not match its GitHub asset digest")
    listed = parse_sums(sums_file.read_text(encoding="utf-8", errors="replace")).get(release.wheel.name)
    if not listed:
        raise UpdateError(f"{SUMS} of {release.tag} lists no digest for {release.wheel.name}")
    wheel = work / release.wheel.name
    digest, size = fetcher.fetch_file(release.wheel.url, wheel, MAX_WHEEL)
    if release.wheel.size is not None and size != release.wheel.size:
        raise UpdateError(f"{release.wheel.name} is {size} bytes; the release says {release.wheel.size}")
    if digest != release.wheel.digest:
        raise UpdateError(f"{release.wheel.name} does not match its GitHub asset digest")
    if digest != listed:
        raise UpdateError(f"{release.wheel.name} does not match its line in {SUMS}")
    meta = wheel_metadata(wheel, work)
    if meta["name"] != DISTRIBUTION or meta["version"] != release.version:
        raise UpdateError(f"{release.wheel.name} metadata names {meta['name']} {meta['version']}, "
                          f"not {DISTRIBUTION} {release.version}")
    meta["sha256"] = digest
    return wheel, meta


# --- the rollback wheel -------------------------------------------------------

def _snapshot_destination(base: Path) -> str:
    """Where the pre-update snapshot really goes, as the preview says it: with a
    backup passphrase, encrypted into the backups root; without one, plaintext
    inside the workspace's own `backups/`."""
    try:
        encrypted = backup_vault.passphrase(base) is not None
        root = backup_vault.root_info(base)["path"]
    except (config.ConfigurationError, OSError):
        encrypted, root = False, None
    if encrypted and root is not None:
        return f"snapshot the workspace with the running release, encrypted into {root}"
    return (f"snapshot the workspace with the running release under {base / 'backups'}, "
            f"unencrypted (no backup passphrase is set)")


def cached_wheel(base: Path, version: str) -> Path | None:
    """A wheel kept by an earlier update, trusted only if its receipt still matches."""
    folder = base / CACHE
    wheel = folder / WHEEL.format(version=version)
    receipt = folder / (wheel.name + ".json")
    if not wheel.is_file() or wheel.is_symlink() or not receipt.is_file():
        return None
    try:
        expected = json.loads(receipt.read_text(encoding="utf-8")).get("sha256")
    except (OSError, ValueError):
        return None
    return wheel if expected and workspace_upgrade.digest(wheel) == expected else None


def cache_wheel(base: Path, wheel: Path, version: str, source: str) -> None:
    workspace.reject_symlinks(base / CACHE)
    folder = base / CACHE
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / WHEEL.format(version=version)
    stage = folder / f".{target.name}.{uuid.uuid4().hex}"
    shutil.copyfile(wheel, stage)
    os.chmod(stage, 0o600)
    os.replace(stage, target)
    workspace.write_json(folder / (target.name + ".json"),
                         {"version": version, "sha256": workspace_upgrade.digest(target),
                          "source": source, "cached_at": now_z()})
    versions = sorted({p.name.split("-")[1] for p in folder.glob("project_observatory-*-py3-none-any.whl")},
                      key=config.version_tuple)
    for old in versions[:-KEEP_CACHED]:
        for p in folder.glob(f"project_observatory-{old}-py3-none-any.whl*"):
            p.unlink(missing_ok=True)


# --- seams: installer, launchd jobs, the new release's own commands ----------------

def install_origin() -> dict:
    """Whether the running engine IS the installed distribution a pip install replaces.

    A source checkout or an editable install is updated with Git, not by this
    command: installing a wheel beside it would leave the checkout running."""
    try:
        dist = importlib.metadata.distribution(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return {"kind": "source", "why": "no installed project-observatory distribution; this engine runs from a checkout"}
    try:
        located = Path(dist.locate_file("observatory/engine/configuration.py")).resolve()
    except (OSError, TypeError):
        located = None
    if located != (config.SOURCE / "configuration.py").resolve():
        return {"kind": "source", "why": "the running engine is not the installed distribution's copy"}
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        direct = {}
    if isinstance(direct, dict) and (direct.get("dir_info") or {}).get("editable"):
        return {"kind": "editable", "why": "an editable install follows its checkout; update that with Git"}
    if dist.version != config.VERSION:
        return {"kind": "inconsistent", "why": f"installed metadata says {dist.version}, the engine says {config.VERSION}"}
    return {"kind": "wheel", "version": dist.version}


class Installer:
    """The running interpreter's installer: pip if importable, else `uv pip --python`."""

    def __init__(self, python: str = sys.executable, runner=subprocess.run, timeout: int = 1800):
        self.python, self.runner, self.timeout = python, runner, timeout

    def available(self) -> str | None:
        if importlib.util.find_spec("pip") is not None:
            return "pip"
        return "uv" if shutil.which("uv") else None

    def _base(self, name: str) -> list[str]:
        if name == "pip":
            return [self.python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        return [shutil.which("uv") or "uv", "pip", "install", "--python", self.python]

    def install(self, wheel: Path, *, force: bool, constraints: Path | None = None) -> list[dict]:
        name = self.available()
        if not name:
            raise InstallFailed("neither pip nor uv is available to this interpreter")
        extra = ["-c", str(constraints)] if constraints else []
        # The `full` extra carries the exact dependency pins; installing the wheel
        # through it is what brings in a dependency the new release added.
        commands = [self._base(name) + extra + [f"{wheel}[full]"]]
        if force:
            commands.append(self._base(name) + ["--no-deps", "--force-reinstall" if name == "pip" else "--reinstall", str(wheel)])
        done = []
        for command in commands:
            try:
                p = self.runner(command, capture_output=True, text=True, timeout=self.timeout,
                                env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise InstallFailed(f"{name} could not run: {type(exc).__name__}") from None
            if p.returncode:
                raise InstallFailed(f"{name} exit {p.returncode}: {redact_tail(p.stderr or p.stdout)}")
            done.append({"installer": name, "wheel": wheel.name, "force": command is not commands[0]})
        return done


class LaunchdServices:
    """This workspace's tick and server jobs, through tools/install_launchd.py."""

    def __init__(self):
        sys.path.insert(0, str(config.SOURCE / "tools"))
        import install_launchd
        self.launch = install_launchd

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("launchctl") is not None

    def managed(self) -> list[dict]:
        return self.launch.managed_jobs()

    def loaded(self, label: str) -> bool:
        return self.launch.job_loaded(label)

    def stop(self, label: str) -> tuple[bool, str]:
        return self.launch.stop_job(label)

    def start(self, plist: str) -> tuple[bool, str]:
        return self.launch.start_job(plist)


class NewEngine:
    """The freshly installed release, always in a new process of its own.

    `-P` keeps the working directory off sys.path and PYTHONPATH is dropped, so
    the child imports the installed package and not a checkout that happens to
    be nearby — the point is to test what was installed."""

    def __init__(self, home: Path, python: str = sys.executable, runner=subprocess.run):
        self.home, self.python, self.runner = home, python, runner
        self.env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "OBSERVATORY_ROOT"}}
        self.cwd = tempfile.gettempdir()

    def _run(self, argv: list[str], timeout: int = 1800) -> tuple[int, str, str]:
        try:
            p = self.runner([self.python, "-P", *argv], capture_output=True, text=True,
                            timeout=timeout, env=self.env, cwd=self.cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, "", type(exc).__name__
        return p.returncode, p.stdout, p.stderr

    def _full(self, *args: str) -> tuple[int, str, str]:
        return self._run(["-m", "observatory", "--home", str(self.home), "full", *args])

    def upgrade(self) -> tuple[int, str]:
        code, out, err = self._full("upgrade", "--apply", "--writers-stopped")
        return code, redact_tail(err or out)

    def version(self) -> str | None:
        code, out, _ = self._full("version")
        try:
            return json.loads(out).get("application") if code == 0 else None
        except ValueError:
            return None

    def distribution_version(self) -> str | None:
        return self.dependencies([DISTRIBUTION]).get(DISTRIBUTION)

    def dependencies(self, names: list[str]) -> dict[str, str | None]:
        script = ("import importlib.metadata as m, json, sys\n"
                  "def v(n):\n    try: return m.version(n)\n    except m.PackageNotFoundError: return None\n"
                  "print(json.dumps({n: v(n) for n in json.loads(sys.argv[1])}))")
        code, out, _ = self._run(["-c", script, json.dumps(names)], timeout=120)
        try:
            return json.loads(out) if code == 0 else {n: None for n in names}
        except ValueError:
            return {n: None for n in names}

    def doctor(self) -> tuple[int, str]:
        code, out, err = self._full("doctor")
        return code, redact_tail(err if code else "")


@dataclasses.dataclass
class Dependencies:
    fetcher: Fetcher = dataclasses.field(default_factory=Fetcher)
    installer: object = dataclasses.field(default_factory=Installer)
    services: object | None = None                   # LaunchdServices, created only for --apply
    engine: Callable[[Path], object] = NewEngine
    origin: Callable[[], dict] = install_origin
    idle_timeout: float = 60.0


# --- the workspace side ---------------------------------------------------------

def log(base: Path, event: str, **fields) -> None:
    """One JSON line per step in store/logs/update.jsonl. No credential is ever a field.

    Logging never decides the outcome: a failed write is swallowed here and the
    report the caller prints remains the record of what happened."""
    if not (base / "workspace.json").is_file():
        return
    try:
        file = base / LOG
        workspace.reject_symlinks(file)
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(json.dumps({"at": now_z(), "event": event, **fields}, ensure_ascii=False) + "\n")
    except (OSError, config.ConfigurationError):
        pass


def state(base: Path) -> dict:
    """What the workspace holds, minus this command's own log line: the test for
    "did the new release's upgrade change anything" before deciding on a restore."""
    found = workspace_upgrade.inventory(base)
    found.pop(LOG.as_posix(), None)
    return {"files": found, "journal": (base / workspace_upgrade.JOURNAL).exists()}


def wait_idle(base: Path, timeout: float) -> None:
    """Until no tick or workspace operation holds its lock; a stopped job can still be finishing."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with workspace_upgrade.operation_lock(base):
                return
        except config.ConfigurationError:
            if time.monotonic() >= deadline:
                raise UpdateError("A tick or another workspace operation still holds the lock; "
                                  "wait for it to finish and retry") from None
            time.sleep(1)


def restore_in_place(base: Path, snapshot: Path) -> Path:
    """Put the pre-update snapshot back at `base`, keeping the changed copy beside it.

    `restore` only ever writes a NEW home, so the snapshot is restored next to the
    workspace and the two are swapped by rename; the path (and so the launchd
    labels derived from it) stays the same. `backups/` is not in a snapshot and
    moves across with the swap, so no backup is left behind in the failed copy."""
    stage = base.parent / f".{base.name}.update-restore-{uuid.uuid4().hex[:12]}"
    workspace_upgrade.restore(snapshot, stage)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    failed = base.parent / f"{base.name}.failed-update-{stamp}-{uuid.uuid4().hex[:6]}"
    if (base / "backups").is_dir():
        shutil.rmtree(stage / "backups", ignore_errors=True)
        os.rename(base / "backups", stage / "backups")
    os.rename(base, failed)
    os.rename(stage, base)
    workspace_upgrade.sync_directory(base.parent)
    (base.parent / f".{stage.name}.observatory-operation.lock").unlink(missing_ok=True)
    return failed


# --- the command ------------------------------------------------------------------

def relation(current: str, target: str) -> int:
    a, b = config.version_tuple(current), config.version_tuple(target)
    return (b > a) - (b < a)


def check_result(current: str, release: Release) -> tuple[int, dict]:
    rel = relation(current, release.version)
    out = {"current": current, "target": release.version, "release": release.page, "degraded": []}
    if rel == 0:
        return EXIT_OK, {**out, "status": "up-to-date"}
    if rel < 0:
        return EXIT_OK, {**out, "status": "newer-installed",
                         "note": "this machine runs a newer release than the target; update the other machine"}
    why = release.installable()
    if why:
        return EXIT_UNDETERMINED, {**out, "status": "degraded", "degraded": [why + "; it cannot be installed by `full update`"]}
    return EXIT_UPDATE_AVAILABLE, {**out, "status": "update-available",
                                   "next": f"project-observatory full update --version {release.version} --apply"}


def preview(base: Path, current: str, release: Release, deps: Dependencies, args) -> dict:
    rel = relation(current, release.version)
    blockers = []
    if rel < 0:
        blockers.append("the target is older than the running engine; automatic downgrade is not supported")
    elif rel == 0 and not args.reinstall:
        blockers.append("already at the target; --reinstall installs the same release again")
    origin = deps.origin()
    if origin.get("kind") != "wheel":
        blockers.append(origin.get("why") or "not an installed wheel")
    if not deps.installer.available():
        blockers.append("neither pip nor uv is available to this interpreter")
    if release.installable():
        blockers.append(release.installable())
    has_ws = (base / "workspace.json").is_file()
    if has_ws:
        try:
            workspace_upgrade.managed_layout(base)
            workspace_upgrade.preflight(base)
        except (config.ConfigurationError, RuntimeError, OSError) as exc:
            blockers.append(f"workspace: {exc}")
    cached = has_ws and cached_wheel(base, current) is not None
    would = [f"fetch {WHEEL.format(version=release.version)} and {SUMS}; verify both against GitHub's digests",
             f"rollback wheel {current}: " + ("cached from an earlier update" if cached else
                                               "downloaded from its own release and verified the same way")]
    if has_ws:
        would += ["stop this workspace's launchd tick and server if loaded" if not args.writers_stopped
                  else "leave writers alone (--writers-stopped: you stopped them)",
                  _snapshot_destination(base)]
    would += [f"install the wheel with its [full] extra into {sys.executable}, constrained by the "
              f"{Path(LOCK_MEMBER).name} the wheel carries (unconstrained, and said so, if it carries none)"]
    if has_ws:
        would += ["run the new release's `upgrade --apply --writers-stopped` in a new process"]
    would += ["verify the installed version, its pinned dependencies" + (" and `doctor`" if has_ws else ""),
              "on failure reinstall the rollback wheel" + (", restore the snapshot if the workspace changed, "
                                                           "restart the jobs that were stopped" if has_ws else "")]
    return {"status": "preview", "current": current, "target": release.version, "release": release.page,
            "update_available": rel > 0, "workspace": "present" if has_ws else "none (install only)",
            "would": would, "apply_blockers": blockers, "degraded": [],
            "next": None if blockers else "project-observatory full update"
                    + (f" --version {release.version}" if args.version else "") + " --apply"}


class Transaction:
    def __init__(self, base: Path, current: str, release: Release, deps: Dependencies, args):
        self.base, self.current, self.release, self.deps, self.args = base, current, release, deps, args
        self.has_ws = (base / "workspace.json").is_file()
        self.steps: list[dict] = []
        self.stopped: list[dict] = []
        self.work: Path | None = None   # the run's temporary folder, while run() holds it
        self.report: dict = {"status": "failed", "current": current, "target": release.version,
                             "steps": self.steps, "services_stopped": [], "rolled_back": False,
                             "workspace_restored": False, "rollback_available": False, "degraded": []}

    def step(self, name: str, **fields) -> None:
        self.steps.append({"step": name, **fields})
        log(self.base, name, **{"from": self.current, "to": self.release.version, **fields})

    # refusals: before anything changes -------------------------------------------------

    def refuse_early(self) -> None:
        rel = relation(self.current, self.release.version)
        if rel < 0:
            raise UpdateError(f"{self.release.version} is older than the running {self.current}; automatic downgrade "
                              "is not supported. Restore a snapshot into a new home with a compatible release instead")
        if rel == 0 and not self.args.reinstall:
            raise UpdateError(f"{self.current} is already installed; pass --reinstall to install it again")
        origin = self.deps.origin()
        if origin.get("kind") != "wheel":
            raise UpdateError(f"Not updated: {origin.get('why') or 'this engine is not an installed wheel'} "
                              f"({origin.get('kind')} install)")
        if not self.deps.installer.available():
            raise UpdateError("Neither pip nor uv is available to this interpreter; install pip into this environment")
        if self.has_ws:
            workspace_upgrade.managed_layout(self.base)
            workspace_upgrade.preflight(self.base)

    def rollback_wheel(self, work: Path) -> Path | None:
        if self.has_ws:
            cached = cached_wheel(self.base, self.current)
            if cached:
                return cached
        try:
            api, repo = release_source(self.args.repository, self.args.api_url)
            release = resolve_release(self.deps.fetcher, api, repo, self.current)
            folder = work / "rollback"
            folder.mkdir(mode=0o700)
            wheel, _ = fetch_verified(self.deps.fetcher, release, folder)
        except UpdateError as exc:
            if self.args.no_rollback:
                self.report["degraded"].append(f"no rollback wheel: {exc}")
                return None
            raise UpdateError(f"No verified wheel of the running {self.current} to roll back to ({exc}); "
                              "apply refuses without one. Pass --no-rollback to accept an update that cannot "
                              "be undone automatically", exc.code) from None
        if not self.has_ws:
            return wheel
        # Cached BEFORE the point of change: a process killed half-way still leaves
        # the verified wheel where the report and a person can find it.
        try:
            cache_wheel(self.base, wheel, self.current, "github-release")
        except (OSError, config.ConfigurationError) as exc:
            self.report["degraded"].append(f"rollback wheel kept only in a temporary directory: {exc}")
            return wheel
        return cached_wheel(self.base, self.current) or wheel

    def stop_writers(self) -> None:
        if not self.has_ws or self.args.writers_stopped:
            return
        services = self.deps.services
        if services is None or not services.available():
            raise UpdateError("launchd is not available here, so this command cannot stop the scheduler it did not "
                              "install; stop your own tick and server, then pass --writers-stopped")
        jobs = [j for j in services.managed() if services.loaded(j["label"])]
        for job in jobs:
            if not Path(job["plist"]).is_file():
                raise UpdateError(f"The {job['name']} job is loaded but its plist is gone, so it could not be "
                                  "started again; reinstall it (or stop it) first")
        for job in jobs:
            ok, detail = services.stop(job["label"])
            if not ok:
                self.start_writers()
                raise UpdateError(f"Could not stop the {job['name']} job: {redact_tail(detail, 300)}")
            self.stopped.append(job)
            self.step("stopped", service=job["name"])
        self.report["services_stopped"] = [j["name"] for j in self.stopped]

    def start_writers(self) -> list[dict]:
        failed = []
        for job in self.stopped:
            ok, detail = self.deps.services.start(job["plist"])
            if not ok:
                failed.append({"service": job["name"], "detail": redact_tail(detail, 300),
                               "fix": f"launchctl bootstrap gui/{os.getuid()} {job['plist']}"})
            self.step("started" if ok else "start-failed", service=job["name"])
        self.stopped = []
        return failed

    # after the point of change ----------------------------------------------------------

    def undo(self, error: str, rollback: Path | None, snapshot: Path | None, before: dict | None) -> tuple[int, dict]:
        report = self.report
        report["error"] = error
        human = []
        if rollback is None:
            wheel = WHEEL.format(version=self.current)
            human.append(f"Reinstall {self.current} from its release page: python -m pip install "
                         f"--force-reinstall --no-deps '{wheel}', then python -m pip install "
                         f"-c \"$(project-observatory full-path)/requirements-full.lock\" '{wheel}[full]'")
        else:
            try:
                # The running release's own lock, so the rollback restores the tested set
                # and not whatever the index offers today.
                constraints = wheel_lock(rollback, self.work / "rollback-lock") if self.work else None
                self.deps.installer.install(rollback, force=True, constraints=constraints)
                back = self.deps.engine(self.base).distribution_version()
                report["rolled_back"] = back == self.current
                if not report["rolled_back"]:
                    human.append(f"The rollback install reports version {back}, not {self.current}")
            except (InstallFailed, UpdateError, OSError) as exc:
                # Two steps, as README → Install: the wheel alone, then its [full]
                # extra under the lock that wheel ships, so the hand-over restores
                # the tested set rather than whatever the index resolves today.
                human.append(f"Rollback install failed ({exc}); run: {sys.executable} -m pip install "
                             f"--force-reinstall --no-deps '{rollback}' && {sys.executable} -m pip install "
                             f"-c \"$(project-observatory full-path)/requirements-full.lock\" '{rollback}[full]'")
            self.step("rolled-back" if report["rolled_back"] else "rollback-failed")
        if self.has_ws and snapshot is not None and before is not None:
            try:
                changed = state(self.base) != before
            except (config.ConfigurationError, OSError):
                changed = True
            if changed:
                try:
                    report["failed_workspace"] = str(restore_in_place(self.base, snapshot))
                    report["workspace_restored"] = True
                    self.step("workspace-restored")
                except (config.ConfigurationError, RuntimeError, OSError) as exc:
                    human.append(f"Restore the pre-update snapshot {snapshot} into a new home "
                                 f"(`full restore`), it could not be swapped in: {exc}")
                    self.step("workspace-restore-failed")
        failed = self.start_writers() if self.stopped else []
        if failed:
            report["services_not_restarted"] = failed
            human.extend(f["fix"] for f in failed)
        report["human_steps"] = human
        report["status"] = "rolled-back" if not human else "needs-attention"
        return (EXIT_NEEDS_PERSON if human else EXIT_FAILED), report

    def verify(self, meta: dict) -> str:
        engine = self.deps.engine(self.base)
        installed = engine.distribution_version()
        if installed != self.release.version:
            return f"installed distribution reports {installed}, expected {self.release.version}"
        running = engine.version()
        if running != self.release.version:
            return f"the new engine reports {running}, expected {self.release.version}"
        pins = [p for p in requirement_pins(meta["requires"]) if not p["conditional"]]
        found = engine.dependencies([p["name"] for p in pins])
        for pin in pins:
            have = found.get(pin["name"])
            if have is None:
                return f"dependency {pin['name']} is not installed"
            if pin["pin"] and have != pin["pin"]:
                return f"dependency {pin['name']} is {have}, the release pins {pin['pin']}"
        if self.has_ws:
            code, detail = engine.doctor()
            if code:
                return f"`full doctor` of the new release failed (exit {code}): {detail}"
        return ""

    def run(self) -> tuple[int, dict]:
        self.refuse_early()
        work = Path(tempfile.mkdtemp(prefix="observatory-update-"))
        try:
            self.work = work
            wheel, meta = fetch_verified(self.deps.fetcher, self.release, work)
            self.step("verified", wheel=wheel.name, sha256=meta["sha256"], checks=["github-digest", SUMS])
            self.report["constraints"] = LOCK_MEMBER if meta["constraints"] else None
            if not meta["constraints"]:
                self.report["degraded"].append(
                    f"{wheel.name} carries no {LOCK_MEMBER}: its [full] dependencies are installed "
                    "unconstrained, at the newest releases the index offers rather than the tested set")
            rollback = self.rollback_wheel(work)
            self.report["rollback_available"] = rollback is not None
            self.stop_writers()
            snapshot = before = None
            try:
                if self.has_ws:
                    wait_idle(self.base, self.deps.idle_timeout)
                    receipt = workspace_upgrade.snapshot(
                        self.base, self.base / "backups" / f"before-update-{uuid.uuid4().hex}", writers_stopped=True)
                    snapshot = Path(receipt["snapshot"])
                    before = state(self.base)
                    self.step("snapshot", files=receipt["files"])
            except BaseException:
                self.start_writers()
                raise
            # The point of change: from here every failure goes through undo().
            try:
                self.deps.installer.install(wheel, force=relation(self.current, self.release.version) == 0,
                                            constraints=meta["constraints"])
                self.step("installed", wheel=wheel.name, constraints=self.report["constraints"])
            except InstallFailed as exc:
                return self.undo(f"install failed: {exc}", rollback, snapshot, before)
            if self.has_ws:
                code, detail = self.deps.engine(self.base).upgrade()
                if code:
                    return self.undo(f"the new release's workspace upgrade failed (exit {code}): {detail}",
                                     rollback, snapshot, before)
                self.step("workspace-upgraded")
            problem = self.verify(meta)
            if problem:
                return self.undo(f"verification failed: {problem}", rollback, snapshot, before)
            self.step("verified-installed")
            return self.finish(wheel, rollback, snapshot)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def finish(self, wheel: Path, rollback: Path | None, snapshot: Path | None) -> tuple[int, dict]:
        report = self.report
        report.update(status="updated", version=self.release.version, rolled_back=False)
        if self.has_ws:
            for path, version in ((rollback, self.current), (wheel, self.release.version)):
                if path is not None and path.parent != self.base / CACHE:
                    try:
                        cache_wheel(self.base, path, version, "github-release")
                    except (OSError, config.ConfigurationError) as exc:
                        report["degraded"].append(f"could not cache {path.name} for the next rollback: {exc}")
            if snapshot is not None:
                try:
                    report["snapshot"] = backup_vault.after_snapshot(self.base, snapshot, "before-update")
                except Exception as exc:  # noqa: BLE001 — the update is done; the snapshot exists either way
                    report["snapshot"] = {"snapshot": str(snapshot), "encrypted": False,
                                          "export_error": f"{type(exc).__name__}: {exc}"}
        failed = self.start_writers() if self.stopped else []
        if failed:
            report["services_not_restarted"] = failed
            report["human_steps"] = [f["fix"] for f in failed]
        self.step("updated", services_not_restarted=[f["service"] for f in failed])
        report["next"] = plugin_advice()
        return (EXIT_SERVICES if failed else EXIT_OK), report


def parser() -> argparse.ArgumentParser:
    """Shared by `main` and the gate's parse-only check (`workspace.parse`)."""
    ap = argparse.ArgumentParser(prog="project-observatory full update", description=__doc__.splitlines()[0])
    ap.add_argument("--version", metavar="X.Y.Z", help="target release instead of the latest")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 0 up to date, 10 update available, 3 undetermined")
    mode.add_argument("--apply", action="store_true", help="install, upgrade the workspace, verify; roll back on failure")
    ap.add_argument("--writers-stopped", action="store_true", help="you stopped the tick and server yourself")
    ap.add_argument("--reinstall", action="store_true", help="allow installing the version already running")
    ap.add_argument("--no-rollback", action="store_true", help="apply even without a verified rollback wheel")
    ap.add_argument("--repository", help=f"OWNER/NAME of the release source (default: the plugin's repository, or ${REPOSITORY_ENV})")
    ap.add_argument("--api-url", help=f"GitHub API base (default {DEFAULT_API}, or ${API_ENV})")
    return ap


def main(argv: list[str], deps: Dependencies | None = None) -> int:
    args = parser().parse_args(argv)
    if deps is None:
        deps = Dependencies()
    current = config.VERSION
    code, result, base = EXIT_REFUSED, {}, None
    try:
        base = config.home()
        if args.version:
            config.version_tuple(args.version)
        api, repo = release_source(args.repository, args.api_url)
        release = resolve_release(deps.fetcher, api, repo, args.version)
        if args.check:
            code, result = check_result(current, release)
        elif not args.apply:
            code, result = EXIT_OK, preview(base, current, release, deps, args)
        else:
            if deps.services is None and not args.writers_stopped:
                try:
                    deps.services = LaunchdServices()
                except Exception as exc:  # noqa: BLE001 — an unusable helper is a refusal, not a crash
                    raise UpdateError(f"The launchd helpers could not be loaded ({type(exc).__name__}); "
                                      "stop the writers yourself and pass --writers-stopped") from None
            code, result = Transaction(base, current, release, deps, args).run()
    except Undetermined as exc:
        code, result = EXIT_UNDETERMINED, {"status": "degraded", "current": current, "degraded": [str(exc)]}
    except UpdateError as exc:
        code, result = exc.code, {"status": "refused", "current": current, "error": str(exc), "degraded": []}
    except (config.ConfigurationError, RuntimeError, OSError) as exc:
        code, result = EXIT_REFUSED, {"status": "refused", "current": current,
                                      "error": f"{type(exc).__name__}: {exc}", "degraded": []}
    if args.apply and base is not None and result.get("status") == "refused":
        log(base, "refused", **{"from": current, "error": result.get("error")})
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
