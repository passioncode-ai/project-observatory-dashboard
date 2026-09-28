#!/usr/bin/env python3
"""Who the always-on server is, as the fabric-service/0.1 protocol names it.

`tools/serverd.py` is a *service* in the sense of the Fabric Agent Contract's
local service extension (`fabric-service/0.1`, the contract's
`docs/specification/service.md`): a process that stays up, answers other agents
and shows a dashboard. A host such as Fabric Dashboards finds it through a
descriptor file, confirms it through `GET /.well-known/fabric-service` and reads
what it did through `GET /fabric/v1/events`. This module is the one place that
answers the identity questions those three share, so the installer that writes
the descriptor and the process that answers the well-known document can never
disagree about them:

- **id** — `project-observatory`, stable for ever;
- **instance** — one per workspace. The standard workspace is `default`; any
  other is `ws-<sha16>` of its resolved path, the same digest the launchd label
  already carries (`tools/install_launchd.py:instance_label`), so one workspace
  has one label, one instance and one lock;
- **data directory** — the workspace itself. The instance lock
  (`service.lock`) and the events-feed token (`service.token`) sit at its root,
  where the workspace's own `.gitignore` keeps them out of the registry
  history. Both are derived from `OBSERVATORY_HOME` alone, the one variable the
  launchd plist carries, so the descriptor an installer wrote and the files the
  launchd-started process uses are the same files;
- **build** — a commit or a package digest, so a host can tell a restart onto
  new code from a restart onto old code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import configuration
import fabric_service as fs
import paths

SERVICE_ID = "project-observatory"
NAME = "Project Observatory"
SUMMARY = "Local inventory of this machine's projects: findings, leaks, remotes and agent activity."
REPOSITORY = "https://github.com/passioncode-ai/project-observatory-dashboard"
DEFAULT_INSTANCE = "default"
LOCK_NAME = "service.lock"
TOKEN_NAME = "service.token"
DISTRIBUTION = "project-observatory"

_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def default_home() -> Path:
    """The standard workspace, spelled where `configuration.home()` spells it."""
    return Path.home() / ".local/share/project-observatory-full"


def workspace_digest(home: Path | None = None) -> str:
    """sha256[:16] of the resolved workspace path — the launchd label's digest."""
    return hashlib.sha256(str((home or paths.HOME).resolve()).encode()).hexdigest()[:16]


def instance(home: Path | None = None) -> str:
    """`default` for the standard workspace, else `ws-<sha16>`.

    The protocol's instance pattern is `^[a-z][a-z0-9-]{0,31}$`: it must start
    with a letter, which a bare hex digest does not always do, hence the prefix.
    """
    home = (home or paths.HOME).resolve()
    try:
        if home == default_home().resolve():
            return DEFAULT_INSTANCE
    except OSError:
        pass
    return "ws-" + workspace_digest(home)


def data_dir() -> Path:
    return paths.HOME


def lock_dir() -> Path:
    """Where `service.lock` lives — the descriptor's `paths.data`."""
    return data_dir()


def token_file() -> Path:
    return data_dir() / TOKEN_NAME


def log_files() -> list[Path]:
    return [paths.STATE / "logs/serverd.err", paths.STATE / "logs/serverd.out"]


# --- build identity ---------------------------------------------------------

def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                             timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _git_build(root: Path) -> dict | None:
    """A commit, but ONLY when `root` is this project's own checkout.

    `git rev-parse` answers for any enclosing repository: an engine installed
    into a virtual environment that happens to sit inside some other checkout
    would otherwise report that checkout's commit as its own build.
    """
    top = _git(root, "rev-parse", "--show-toplevel")
    if not top:
        return None
    try:
        if (Path(top) / "observatory" / "engine").resolve() != root.resolve():
            return None
    except OSError:
        return None
    commit = _git(root, "rev-parse", "--short=12", "HEAD")
    if not commit or not _COMMIT.match(commit):
        return None
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    build = {"commit": commit}
    if status is not None:
        build["dirty"] = bool(status)
    return build


def _distribution_build(root: Path) -> dict | None:
    """The installed wheel's identity from its own metadata.

    `direct_url.json` (PEP 610) names the commit for a VCS install and the
    archive's sha256 for a wheel installed from a file; failing both, the
    distribution's RECORD — which lists the digest of every installed file —
    is hashed. Only a distribution that actually owns this engine counts.
    """
    try:
        from importlib import metadata
        dist = metadata.distribution(DISTRIBUTION)
        owned = Path(dist.locate_file("observatory/engine/service_identity.py")).resolve()
    except Exception:  # noqa: BLE001 — absent or unreadable metadata is "no answer"
        return None
    if owned != (root / "service_identity.py").resolve():
        return None
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "null")
    except ValueError:
        direct = None
    if isinstance(direct, dict):
        commit = str((direct.get("vcs_info") or {}).get("commit_id") or "")
        if _COMMIT.match(commit):
            return {"commit": commit[:12]}
        archive = direct.get("archive_info") or {}
        digest = str((archive.get("hashes") or {}).get("sha256") or "")
        if not digest and str(archive.get("hash", "")).startswith("sha256="):
            digest = archive["hash"][len("sha256="):]
        if _SHA256.match(digest):
            return {"digest": "sha256:" + digest}
    record = dist.read_text("RECORD")
    if record:
        return {"digest": "sha256:" + hashlib.sha256(record.encode("utf-8")).hexdigest()}
    return None


def build(root: Path | None = None) -> dict:
    """`{"commit": …}` or `{"digest": "sha256:…"}` — never neither.

    Order: `FABRIC_BUILD_COMMIT` (a release pipeline states it), this project's
    own checkout, the installed distribution's metadata, and last the source
    inventory — `SOURCE-INVENTORY.json` records the sha256 of every engine file,
    so its own digest changes whenever any shipped byte does.
    """
    root = (root or paths.ROOT).resolve()
    env = os.environ.get("FABRIC_BUILD_COMMIT", "").strip().lower()
    if _COMMIT.match(env):
        return {"commit": env}
    found = _git_build(root) or _distribution_build(root)
    if found:
        return found
    inventory = root / "SOURCE-INVENTORY.json"
    try:
        return {"digest": "sha256:" + hashlib.sha256(inventory.read_bytes()).hexdigest()}
    except OSError:
        # Nothing names this build. The protocol requires a commit or a digest,
        # and an invented commit would be a lie; the digest of this very module
        # is at least a true statement about the code that is answering.
        return {"digest": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


# --- descriptor -------------------------------------------------------------

def doctor_command() -> list[str] | None:
    """`project-observatory --home <workspace> full doctor`, by absolute path.

    Only when the console script sits beside this interpreter, as it does in an
    installed environment: a host runs a descriptor command verbatim, and a
    command that names a missing file is worse than no command.
    """
    script = Path(sys.executable).parent / "project-observatory"
    if not script.is_file() or not os.access(script, os.X_OK):
        return None
    return [str(script), "--home", str(paths.HOME), "full", "doctor"]


def descriptor(port: int, *, label: str | None = None, plist: Path | None = None,
               installed_by: str | None = None) -> dict:
    """The installation record a host reads. launchd when a label is given, else `none`."""
    lifecycle: dict = ({"manager": "launchd", "label": label, "plist": str(plist)}
                       if label else {"manager": "none"})
    doc = {
        "protocol": fs.PROTOCOL,
        "id": SERVICE_ID,
        "instance": instance(),
        "name": NAME,
        "summary": SUMMARY,
        "origin": f"http://127.0.0.1:{port}",
        "auth": {"tokenFile": str(token_file())},
        "lifecycle": lifecycle,
        "paths": {"data": str(data_dir()), "config": str(paths.CONFIG),
                  "logs": [str(p) for p in log_files()]},
        "source": {"repository": REPOSITORY},
        "fabricManifest": str(paths.ROOT / "fabric-agent.json"),
        "installedAt": fs.now_iso(),
        "installedBy": installed_by or f"tools/serverd.py --install (project-observatory {configuration.VERSION})",
    }
    doctor = doctor_command()
    if doctor:
        doc["commands"] = {"doctor": doctor}
    return doc
