# Vendored from passioncode-ai/fabric-agent-adapter dfd11dad72fe (fabric-agent-adapter 0.4.0),
# plugins/fabric-agent-adapter/skills/building-fabric-services/scripts/fabric_service.py,
# upstream sha256 3331b9ad5baa5721d0bdc148284dfc88572bb9a22231d292b969f7bd918f2eb5 (the bytes below this header).
# Do not edit here: update the kit upstream and copy it again (tests/test_fabric_service.py checks the digest).
#!/usr/bin/env python3
"""Reference kit for the fabric-service/0.1 local service extension.

Standard library only, Python 3.9+. Copy this file into a service (it is one
module on purpose) or import it by path. Every function implements one rule of
the extension; the rule it implements is named in its docstring, so a service
that calls these functions inherits the rule instead of re-deriving it.

Normative source: fabric-agent-contract docs/specification/service.md (DEC-0015).
"""

from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import urllib.error
import urllib.request

PROTOCOL = "fabric-service/0.1"
EXTENSION_KEY = "https://fabric.passioncode.ai/agent-contract/extensions/service/0.1"
EXIT_ALREADY_RUNNING = 75
LOGIN_CODE_TTL_SECONDS = 120
EVENTS_DEFAULT_LIMIT = 50
EVENTS_MAX_LIMIT = 200
LEVELS = ("info", "notice", "warning", "error")
STATUSES = ("starting", "ready", "degraded", "stopping")

_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_INSTANCE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,5}$")
_ORIGIN = re.compile(r"^http://127\.0\.0\.1:([0-9]{3,5})$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


class ServiceError(Exception):
    """A rule of the extension was violated; the message is one readable sentence."""


class AlreadyRunning(ServiceError):
    def __init__(self, holder_pid: Optional[int], lock_path: Path):
        self.holder_pid = holder_pid
        self.lock_path = lock_path
        who = "process %d" % holder_pid if holder_pid else "another process"
        super().__init__("Another copy is already running (%s holds %s)." % (who, lock_path))


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expand(path: str) -> Path:
    """Paths in a descriptor may start with ~/; everything else must be absolute."""
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        raise ServiceError("Path %r must be absolute or start with ~/." % path)
    return p


# --- where things live -------------------------------------------------------

def services_dir() -> Path:
    """Descriptor directory: FABRIC_SERVICES_DIR, else the per-user OS location."""
    override = os.environ.get("FABRIC_SERVICES_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/ai.passioncode.fabric/services"
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local/share")
    return Path(base) / "passioncode-fabric/services"


def service_dirs(service_id: str) -> Dict[str, Path]:
    """State lives outside code: data+config, logs and cache in per-user OS directories."""
    _require_id(service_id)
    home = Path.home()
    if sys.platform == "darwin":
        return {
            "data": home / "Library/Application Support" / service_id,
            "logs": home / "Library/Logs" / service_id,
            "cache": home / "Library/Caches" / service_id,
        }
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / service_id
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / service_id
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / service_id
    return {"data": data, "logs": state / "logs", "cache": cache}


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Temporary file in the same directory, fsync, rename: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# --- one copy ----------------------------------------------------------------

class InstanceLock:
    """Exclusive flock on <data>/service.lock, taken BEFORE any side effect.

    Binding a port is not a lock: a second copy that resumes jobs before its
    bind fails has already done damage. The lock is released by the kernel when
    the process dies, so a crash never leaves it stale.
    """

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "service.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> "InstanceLock":
        import fcntl

        ensure_private_dir(self.path.parent)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise AlreadyRunning(_read_pid(self.path), self.path) from None
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _read_pid(path: Path) -> Optional[int]:
    try:
        text = path.read_text().strip()
        return int(text) if text.isdigit() else None
    except OSError:
        return None


def hold_single_instance(data_dir: Path) -> InstanceLock:
    """Take the lock or exit 75 with one sentence naming the holder."""
    try:
        return InstanceLock(data_dir).acquire()
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_ALREADY_RUNNING)


# --- token -------------------------------------------------------------------

def ensure_token(path: Path) -> str:
    """Create the service token once (0600, owner only); never rotate silently."""
    path = Path(path)
    if path.exists():
        return read_token(path)
    ensure_private_dir(path.parent)
    token = secrets.token_urlsafe(32)
    atomic_write(path, token.encode(), 0o600)
    return token


def read_token(path: Path) -> str:
    """Refuse a symlink, a file owned by someone else, or one others can read."""
    path = Path(path)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ServiceError("Token file %s is a symlink; refusing it." % path)
    if info.st_uid != os.getuid():
        raise ServiceError("Token file %s belongs to another user." % path)
    if info.st_mode & 0o077:
        raise ServiceError("Token file %s is readable by others; set mode 0600." % path)
    token = path.read_text().strip()
    if len(token) < 16:
        raise ServiceError("Token file %s holds no usable token." % path)
    return token


def token_matches(presented: Optional[str], token: str, scheme: str = "Bearer") -> bool:
    if not presented:
        return False
    value = presented
    if scheme == "Bearer":
        if not presented.startswith("Bearer "):
            return False
        value = presented[len("Bearer "):]
    return hmac.compare_digest(value.strip().encode(), token.encode())


# --- network guard -----------------------------------------------------------

def check_request(port: int, host: Optional[str], origin: Optional[str] = None,
                  sec_fetch_site: Optional[str] = None) -> Optional[str]:
    """Return why a request must be refused (403), or None when it may proceed."""
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
    if host not in allowed_hosts:
        return "Host %r is not this service." % host
    if origin is not None and origin not in {"http://" + h for h in allowed_hosts}:
        return "Origin %r is not this service." % origin
    if sec_fetch_site == "cross-site":
        return "Cross-site requests are refused."
    return None


# --- descriptor --------------------------------------------------------------

def descriptor_path(service_id: str, instance: str = "default", directory: Optional[Path] = None) -> Path:
    return (directory or services_dir()) / ("%s.%s.json" % (service_id, instance))


def read_descriptors(directory: Optional[Path] = None) -> List[Tuple[Path, Dict[str, Any]]]:
    root = directory or services_dir()
    found: List[Tuple[Path, Dict[str, Any]]] = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        try:
            found.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return found


def validate_descriptor(descriptor: Dict[str, Any]) -> List[str]:
    """Structural checks mirroring service-descriptor.schema.json (the schema stays normative)."""
    problems: List[str] = []
    required = ("protocol", "id", "instance", "name", "origin", "auth", "lifecycle", "paths", "installedAt", "installedBy")
    for key in required:
        if key not in descriptor:
            problems.append("missing %s" % key)
    if problems:
        return problems
    if descriptor["protocol"] != PROTOCOL:
        problems.append("protocol must be %s" % PROTOCOL)
    if not _ID.match(str(descriptor["id"])):
        problems.append("id must match %s" % _ID.pattern)
    if not _INSTANCE.match(str(descriptor["instance"])):
        problems.append("instance must match %s" % _INSTANCE.pattern)
    if not _ORIGIN.match(str(descriptor["origin"])):
        problems.append("origin must be http://127.0.0.1:<port>")
    auth = descriptor.get("auth") or {}
    if "tokenFile" not in auth:
        problems.append("auth.tokenFile is required")
    header = auth.get("header", "Authorization")
    scheme = auth.get("scheme", "Bearer")
    if header != "Authorization" and scheme != "none":
        problems.append("a custom auth header carries the raw token: scheme must be none")
    life = descriptor.get("lifecycle") or {}
    if life.get("manager") not in ("launchd", "none"):
        problems.append("lifecycle.manager must be launchd or none")
    if life.get("manager") == "launchd" and not (life.get("label") and str(life.get("plist", "")).endswith(".plist")):
        problems.append("a launchd service declares label and plist")
    for name, argv in (descriptor.get("commands") or {}).items():
        if name not in ("doctor", "update"):
            problems.append("unknown command %s" % name)
        elif not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            problems.append("command %s must be an argument array" % name)
        elif not re.match(r"^(~/|/)", argv[0]):
            problems.append("command %s must start with an absolute or ~/ executable" % name)
    return problems


def port_of(origin: str) -> int:
    match = _ORIGIN.match(origin)
    if not match:
        raise ServiceError("Origin %r is not http://127.0.0.1:<port>." % origin)
    return int(match.group(1))


def write_descriptor(descriptor: Dict[str, Any], directory: Optional[Path] = None) -> Path:
    """Installer-only. Refuses a port or id.instance another descriptor claims (a port is a claim)."""
    problems = validate_descriptor(descriptor)
    if problems:
        raise ServiceError("Descriptor is invalid: %s." % "; ".join(problems))
    root = directory or services_dir()
    me = "%s.%s" % (descriptor["id"], descriptor["instance"])
    port = port_of(descriptor["origin"])
    for path, other in read_descriptors(root):
        key = "%s.%s" % (other.get("id"), other.get("instance", "default"))
        if key == me:
            continue
        try:
            other_port = port_of(str(other.get("origin", "")))
        except ServiceError:
            continue
        if other_port == port:
            raise ServiceError("Port %d is already claimed by %s (%s)." % (port, key, path))
    ensure_private_dir(root)
    target = descriptor_path(descriptor["id"], descriptor["instance"], root)
    atomic_write(target, (json.dumps(descriptor, indent=2, ensure_ascii=False) + "\n").encode(), 0o600)
    return target


def remove_descriptor(service_id: str, instance: str = "default", directory: Optional[Path] = None) -> bool:
    try:
        descriptor_path(service_id, instance, directory).unlink()
        return True
    except FileNotFoundError:
        return False


# --- build identity & well-known ------------------------------------------------

def build_info(repo_root: Optional[Path] = None, package_file: Optional[Path] = None) -> Dict[str, Any]:
    """Commit (env FABRIC_BUILD_COMMIT, else git) or a sha256 digest of the package file."""
    commit = os.environ.get("FABRIC_BUILD_COMMIT")
    dirty: Optional[bool] = None
    if not commit and repo_root is not None:
        try:
            commit = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--short=12", "HEAD"],
                                    capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            status = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=5, check=True).stdout
            dirty = bool(status.strip())
        except (OSError, subprocess.SubprocessError):
            commit = None
    info: Dict[str, Any] = {"builtAt": now_iso()}
    if commit:
        info["commit"] = commit
        if dirty is not None:
            info["dirty"] = dirty
    elif package_file is not None and Path(package_file).is_file():
        info["digest"] = "sha256:" + hashlib.sha256(Path(package_file).read_bytes()).hexdigest()
    else:
        raise ServiceError("No build identity: set FABRIC_BUILD_COMMIT, pass repo_root or package_file.")
    return info


def build_well_known(*, service_id: str, instance: str, name: str, version: str, build: Dict[str, Any],
                     started_at: str, status: str, degraded: Sequence[Dict[str, str]],
                     surfaces: Dict[str, Any], summary: Optional[Sequence[Dict[str, Any]]] = None,
                     update_available: Optional[str] = None, pid: Optional[int] = None) -> Dict[str, Any]:
    """The unauthenticated identity answer. `degraded` is always present; ready means empty."""
    if status not in STATUSES:
        raise ServiceError("status must be one of %s." % ", ".join(STATUSES))
    degraded = list(degraded)
    if status == "ready" and degraded:
        status = "degraded"
    if "events" not in surfaces:
        raise ServiceError("surfaces.events is required.")
    doc: Dict[str, Any] = {
        "protocol": PROTOCOL,
        "service": {"id": service_id, "instance": instance, "name": name, "version": version, "build": build},
        "process": {"pid": pid or os.getpid(), "startedAt": started_at},
        "status": status,
        "degraded": degraded,
        "surfaces": surfaces,
        "update": {"available": update_available},
    }
    if summary:
        doc["summary"] = list(summary)[:6]
    return doc


# --- events ------------------------------------------------------------------

def make_event(event_id: Any, at: str, kind: str, level: str, text: str, *, subject: Optional[Dict[str, str]] = None,
               link: Optional[str] = None, notify: bool = False) -> Dict[str, Any]:
    """One activity event: one sentence a person reads, never a machine id."""
    if level not in LEVELS:
        raise ServiceError("level must be one of %s." % ", ".join(LEVELS))
    if not _KIND.match(kind):
        raise ServiceError("kind %r must be dotted lowercase." % kind)
    text = " ".join(str(text).split())
    if not text:
        raise ServiceError("An event needs a sentence.")
    if link is not None and (not link.startswith("/") or link.startswith("//")):
        raise ServiceError("link must be a path on this service.")
    event: Dict[str, Any] = {"id": str(event_id), "at": at, "kind": kind, "level": level, "text": text[:500]}
    if subject:
        event["subject"] = subject
    if link:
        event["link"] = link
    if notify:
        event["notify"] = True
    return event


def parse_limit(raw: Optional[str]) -> int:
    try:
        value = int(raw) if raw not in (None, "") else EVENTS_DEFAULT_LIMIT
    except ValueError:
        raise ServiceError("limit must be an integer.") from None
    return max(1, min(EVENTS_MAX_LIMIT, value))


def events_page(fetch: Callable[[Optional[str], int], Iterable[Dict[str, Any]]],
                after: Optional[str], limit: int) -> Dict[str, Any]:
    """A cursor page over the log the service already keeps.

    `fetch(after, limit)` returns events with ids greater than `after` in
    ascending order (or the newest `limit`, ascending, when `after` is None).
    """
    events = list(fetch(after, limit))[:limit]
    cursor = events[-1]["id"] if events else (after or None)
    return {"events": events, "cursor": cursor}


class JsonlEventLog:
    """A minimal append-only log for services that keep none yet. Ids are integers as strings."""

    def __init__(self, path: Path, keep: int = 5000):
        self.path = Path(path)
        self.keep = keep

    def _all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def append(self, kind: str, level: str, text: str, **extra: Any) -> Dict[str, Any]:
        rows = self._all()
        next_id = int(rows[-1]["id"]) + 1 if rows else 1
        event = make_event(next_id, now_iso(), kind, level, text, **extra)
        rows.append(event)
        rows = rows[-self.keep:]
        atomic_write(self.path, ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode(), 0o600)
        return event

    def fetch(self, after: Optional[str], limit: int) -> List[Dict[str, Any]]:
        rows = self._all()
        if after is None:
            return rows[-limit:]
        try:
            floor = int(after)
        except ValueError:
            raise ServiceError("cursor %r is not from this service." % after) from None
        return [r for r in rows if int(r["id"]) > floor][:limit]


# --- operator login ----------------------------------------------------------

class LoginCodes:
    """Single-use login codes (<=120 s), recorded as used BEFORE they are honoured,
    and HMAC-signed session cookies revoked by rotating the key."""

    def __init__(self, state_dir: Path, ttl: int = LOGIN_CODE_TTL_SECONDS):
        self.dir = ensure_private_dir(Path(state_dir))
        self.ttl = min(ttl, LOGIN_CODE_TTL_SECONDS)
        self._key_path = self.dir / "session.key"
        self._codes_path = self.dir / "login-codes.json"

    def _key(self) -> bytes:
        if not self._key_path.exists():
            atomic_write(self._key_path, secrets.token_bytes(32), 0o600)
        return self._key_path.read_bytes()

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self._codes_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, codes: Dict[str, Any]) -> None:
        horizon = time.time() - 3600
        codes = {k: v for k, v in codes.items() if v.get("expires", 0) > horizon}
        atomic_write(self._codes_path, json.dumps(codes).encode(), 0o600)

    def issue(self) -> Dict[str, str]:
        code = secrets.token_urlsafe(24)
        expires = time.time() + self.ttl
        codes = self._load()
        codes[hashlib.sha256(code.encode()).hexdigest()] = {"expires": expires, "used": False}
        self._save(codes)
        at = _dt.datetime.fromtimestamp(expires, _dt.timezone.utc).replace(microsecond=0)
        return {"url": "/fabric/v1/login?code=" + code, "expiresAt": at.isoformat().replace("+00:00", "Z")}

    def redeem(self, code: Optional[str]) -> Optional[str]:
        """Return a session cookie value, or None. The code is burned even when it has expired."""
        if not code or not _CODE.match(code):
            return None
        digest = hashlib.sha256(code.encode()).hexdigest()
        codes = self._load()
        entry = codes.get(digest)
        if not entry or entry.get("used"):
            return None
        entry["used"] = True
        self._save(codes)
        if entry.get("expires", 0) < time.time():
            return None
        return self._sign(secrets.token_urlsafe(18))

    def _sign(self, session_id: str) -> str:
        mac = hmac.new(self._key(), session_id.encode(), hashlib.sha256).hexdigest()
        return session_id + "." + mac

    def session_valid(self, cookie_value: Optional[str]) -> bool:
        if not cookie_value or "." not in cookie_value:
            return False
        session_id, mac = cookie_value.rsplit(".", 1)
        expected = hmac.new(self._key(), session_id.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, expected)

    def revoke_all(self) -> None:
        atomic_write(self._key_path, secrets.token_bytes(32), 0o600)


SESSION_COOKIE = "fabric_session"


def session_cookie_header(value: str, max_age: int = 30 * 86400) -> str:
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (SESSION_COOKIE, value, max_age)


def cookie_value(cookie_header: Optional[str], name: str = SESSION_COOKIE) -> Optional[str]:
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


# --- launchd -----------------------------------------------------------------

def launchd_plist(label: str, program_arguments: Sequence[str], *, working_directory: Path,
                  stdout_path: Path, environment: Optional[Dict[str, str]] = None,
                  exit_timeout: int = 40) -> bytes:
    """RunAtLoad + KeepAlive true + ThrottleInterval 10; no secret may appear in `environment`."""
    env = dict(environment or {})
    for key in env:
        if re.search(r"(TOKEN|SECRET|PASSWORD|KEY)$", key) and not key.endswith("_FILE"):
            raise ServiceError("Environment variable %s looks like a secret; pass a *_FILE path instead." % key)
    env.setdefault("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    plist = {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": exit_timeout,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stdout_path),
        "EnvironmentVariables": env,
    }
    return plistlib.dumps(plist)


def _launchctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)


def _domain() -> str:
    return "gui/%d" % os.getuid()


def launchd_loaded(label: str) -> bool:
    return _launchctl("print", "%s/%s" % (_domain(), label)).returncode == 0


def fetch_well_known(origin: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(origin + "/.well-known/fabric-service", timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def launchd_install(label: str, plist_path: Path, plist_bytes: bytes, *, origin: str,
                    service_id: str, instance: str = "default", timeout: float = 40.0) -> Dict[str, Any]:
    """Write + lint the plist, bootout and WAIT for unload, bootstrap (retry EIO 5), then
    poll the well-known document until it answers with this identity."""
    plist_path = Path(plist_path)
    atomic_write(plist_path, plist_bytes, 0o644)
    lint = subprocess.run(["plutil", "-lint", str(plist_path)], capture_output=True, text=True)
    if lint.returncode != 0:
        raise ServiceError("plist failed plutil -lint: %s" % lint.stdout.strip())
    target = "%s/%s" % (_domain(), label)
    _launchctl("enable", target)
    _launchctl("bootout", target)
    deadline = time.time() + 15
    while launchd_loaded(label) and time.time() < deadline:
        time.sleep(0.25)
    last = ""
    for _ in range(5):
        result = _launchctl("bootstrap", _domain(), str(plist_path))
        if result.returncode == 0:
            break
        last = (result.stderr or result.stdout).strip()
        time.sleep(1.0)
    else:
        raise ServiceError("launchctl bootstrap failed: %s" % last)
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = fetch_well_known(origin)
        if doc and doc.get("service", {}).get("id") == service_id and doc.get("service", {}).get("instance") == instance:
            return doc
        time.sleep(0.5)
    raise ServiceError("%s did not answer as %s.%s within %.0f s; read its log." % (origin, service_id, instance, timeout))


def launchd_uninstall(label: str, plist_path: Path) -> None:
    _launchctl("bootout", "%s/%s" % (_domain(), label))
    try:
        Path(plist_path).unlink()
    except FileNotFoundError:
        pass


# --- helpers for input validation -----------------------------------------------

def _require_id(service_id: str) -> None:
    if not _ID.match(service_id):
        raise ServiceError("Service id %r must match %s." % (service_id, _ID.pattern))


__all__ = [name for name in dir() if not name.startswith("_")]
