"""Explicit scope, private state and observations with reproducible receipts."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", "build"}
MAX_FILES = 20_000
MAX_ENV_BYTES = 1_000_000
NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}\Z")
ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ObservatoryError(Exception):
    """A safe-to-print operational error, without provider or file content."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ObservatoryError("Use a name starting with a letter, up to 64 letters, digits, underscores or hyphens.")
    return name


def redact(value, sensitive) :
    """Redact value-bearing substrings even when they appear in metadata."""
    needles = sorted({v for v in sensitive if isinstance(v, str) and v}, key=len, reverse=True)
    def clean(item):
        if isinstance(item, str):
            for needle in needles:
                item = item.replace(needle, "[REDACTED]")
            return item
        if isinstance(item, list):
            return [clean(x) for x in item]
        if isinstance(item, dict):
            # Keys are fixed schema identifiers; user labels live only in values.
            return {k: clean(v) for k, v in item.items()}
        return item
    return clean(value)


def known_slot_values(state: Path) -> list[str]:
    directory = state / "secrets"
    if directory.is_symlink():
        raise ObservatoryError("Secret storage must not be a symlink.")
    if not directory.is_dir():
        return []
    values = []
    total_bytes = 0
    for path in directory.iterdir():
        private_file(path)
        if path.stat().st_size > 64 * 1024:
            raise ObservatoryError("Secret slot exceeds the supported size.")
        total_bytes += path.stat().st_size
        if total_bytes > 2 * 1024 * 1024 or len(values) >= 1000:
            raise ObservatoryError("Private slot inventory exceeds the supported 2 MB / 1000 slot budget.")
        values.append(path.read_text())
    return values



def _code_roots(here: Path) -> tuple[Path, ...]:
    """Directories private state must never live in: the shipped code itself.

    The code directory, the installed ``observatory`` package that contains it,
    and the Git work tree of a source checkout. Data there is deleted by an
    upgrade or uninstall, or ends up one ``git add`` away from being published.
    """
    roots = [here]
    package = here if here.name == "observatory" else here.parent
    if package.name == "observatory" and (package / "__init__.py").is_file():
        roots.append(package)
    for parent in (here, *here.parents):
        if (parent / ".git").exists():
            roots.append(parent)
            break
    return tuple(dict.fromkeys(roots))


def refuse_home_inside_code(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for root in _code_roots(Path(__file__).resolve().parent):
        if resolved == root or root in resolved.parents:
            raise ObservatoryError(
                f"Choose a private directory outside the installed code and its source checkout "
                f"(the chosen home is inside {root}); state there is lost on upgrade or can be committed.")
    return path

def state_path(value: str | None = None) -> Path:
    # Only resolved when a command runs, never at module import.
    base = value or os.environ.get("OBSERVATORY_HOME")
    path = refuse_home_inside_code(Path(base).expanduser().absolute() if base
                                   else Path.home() / ".local" / "share" / "project-observatory")
    # The README exports OBSERVATORY_HOME for the complete engine, and the 0.1
    # commands read the same variable: without this, `demo` wrote its own format
    # into the complete workspace. The two formats never share a folder.
    if (path / "workspace.json").is_file():
        raise ObservatoryError(
            f"{path} is a complete-engine workspace; the 0.1 commands keep their own format "
            f"and never write there. Use `project-observatory full ...` for it, or give the "
            f"0.1 command a separate folder with --home PATH.")
    return path


def private_dir(path: Path) -> None:
    # Reject symlinks at every existing component, including parents.
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ObservatoryError("State directories must not use symlinks.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    osprivacy().make_private(path)


_OSPRIVACY = []


def osprivacy():
    """The engine's privacy boundary, one module for both profiles: `engine/osprivacy.py`, loaded
    by its path because the engine folder is not a package (docs/design/WINDOWS-LINUX.md, W2b).
    POSIX reads mode bits; Windows reads the file's ACL."""
    if not _OSPRIVACY:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "observatory_osprivacy", Path(__file__).with_name("engine") / "osprivacy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _OSPRIVACY.append(module)
    return _OSPRIVACY[0]


def private_file(path: Path) -> None:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise ObservatoryError("Expected a regular private state file.")
    if not osprivacy().private(path):
        raise ObservatoryError("Private state file has group or world permissions; set mode 600 before continuing.")


def write_private(path: Path, data: str | bytes) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise ObservatoryError("Refusing to replace a symlink in private state.")
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    write_private(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path):
    private_file(path)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise ObservatoryError("Private state JSON is unreadable; preserve it and restore from a known backup.") from None


def init(state: Path) -> dict:
    private_dir(state)
    cfg = state / "config.json"
    if cfg.exists():
        return config(state)
    value = {"schema_version": 1, "projects": [], "created_at": now()}
    write_json(cfg, value)
    write_private(state / "fingerprint-salt", secrets.token_bytes(32))
    private_dir(state / "secrets")
    return value


def config(state: Path) -> dict:
    if not (state / "config.json").exists():
        raise ObservatoryError("Initialize an empty workspace with `project-observatory init` first.")
    cfg = read_json(state / "config.json")
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1 or not isinstance(cfg.get("projects"), list):
        raise ObservatoryError("Unsupported configuration schema.")
    for p in cfg["projects"]:
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) for k in ("name", "path")):
            raise ObservatoryError("Invalid project configuration.")
        valid_name(p["name"])
    return cfg


def add_project(state: Path, name: str, source: str) -> dict:
    name = valid_name(name)
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise ObservatoryError("Project root must be an existing directory.")
    if root == state.resolve() or state.resolve() in root.parents or root in state.resolve().parents:
        raise ObservatoryError("Project roots and Observatory state must not contain each other.")
    cfg = config(state)
    if any(p["name"] == name or p["path"] == str(root) for p in cfg["projects"]):
        raise ObservatoryError("This project name or directory is already registered.")
    row = {"name": name, "path": str(root), "added_at": now(), "evidence": "operator-selected-directory"}
    cfg["projects"].append(row)
    write_json(state / "config.json", cfg)
    return {"name": name, "registered": True, "scan_performed": False}


def discover(root: Path, depth: int = 2) -> dict:
    root = root.expanduser().resolve()
    if not root.is_dir() or not 0 <= depth <= 5:
        raise ObservatoryError("Discovery needs an existing root and depth between 0 and 5.")
    candidates = []
    visited = 0
    degraded = []
    def error(_):
        degraded.append("A directory could not be read.")
    for current, dirs, files in os.walk(root, followlinks=False, onerror=error):
        visited += 1
        rel = Path(current).relative_to(root)
        if visited > MAX_FILES:
            degraded.append("Discovery directory limit reached.")
            break
        markers = [x for x in (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod") if x in dirs or x in files]
        if markers:
            candidates.append({"relative_path": str(rel), "markers": markers})
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".") and not (Path(current) / d).is_symlink())
        if len(rel.parts) >= depth:
            dirs[:] = []
    return {"mode": "preview", "registered": 0, "candidates": candidates, "degraded": degraded}


def files_under(root: Path):
    count = 0
    directories = 0
    errors = []
    def error(_):
        errors.append("A project directory could not be read.")
    for current, dirs, files in os.walk(root, followlinks=False, onerror=error):
        directories += 1
        if directories > MAX_FILES:
            yield None, "Project directory limit reached; metrics are partial."
            return
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".") and not (Path(current) / d).is_symlink())
        for name in sorted(files):
            p = Path(current) / name
            if p.is_symlink() or not p.is_file():
                continue
            count += 1
            if count > MAX_FILES:
                yield None, "Project file limit reached; metrics are partial."
                return
            yield p, None
    for error in errors:
        yield None, error


def git(root: Path, *args: str) -> str | None:
    try:
        # Git status can execute clean/process filters. Neutralize every configured
        # driver before asking for worktree state, not only fsmonitor.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1"})
        base = ["git", "--no-optional-locks", "-C", str(root)]
        keys = subprocess.run([*base, "config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10, env=env)
        if keys.returncode not in (0, 1):
            return None
        # `log.showSignature` is neutralized too: dropping the global file does
        # not drop the repository's own, and a repository that sets it with a
        # `gpg.program` ran that program from `git log` on a signed commit.
        options = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
                   "-c", f"core.worktree={root}", "-c", "core.pager=cat",
                   "-c", "log.showSignature=false"]
        for key in keys.stdout.decode("utf-8").split("\0"):
            if key:
                options += ["-c", key + ("=false" if key.endswith(".required") else "=")]
        run = subprocess.run([*base, *options, *args],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=15, env=env)
        return run.stdout.strip() if run.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_metrics(root: Path) -> dict:
    if not (root / ".git").exists():
        return {"present": False}
    branch = git(root, "symbolic-ref", "--short", "-q", "HEAD")
    dirty = git(root, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all")
    upstream = git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    counts = git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}") if upstream else None
    ahead = behind = None
    if counts and re.fullmatch(r"\d+\s+\d+", counts):
        ahead, behind = map(int, counts.split())
    last = git(root, "log", "-1", "--format=%cI")
    tags = git(root, "tag", "--list")
    return {"present": True, "readable": dirty is not None, "branch": branch,
            "detached": branch is None, "dirty_files": len(dirty.splitlines()) if dirty else 0,
            "has_upstream": upstream is not None, "ahead": ahead, "behind": behind,
            "last_commit_at": last, "tag_count": len(tags.splitlines()) if tags else 0,
            "remote_freshness": "local-tracking-refs-only; no network fetch"}


def env_pairs(path: Path) -> list[tuple[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise ObservatoryError("Environment inputs must be regular files, never symlinks.")
    if path.stat().st_size > MAX_ENV_BYTES:
        raise ObservatoryError("Environment file exceeds the 1 MB inspection limit.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ObservatoryError("Environment file could not be read as UTF-8.") from None
    pairs = []
    for line in text.splitlines():
        m = ENV_LINE.match(line.strip())
        if not m:
            continue
        name, value = m.groups()
        # Deliberately no shell evaluation, expansion or multiline interpretation.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        pairs.append((name, value))
    return pairs


def observe_project(row: dict, salt: bytes, sensitive: set[str], budget: dict) -> dict:
    root = Path(row["path"])
    item = {"name": row["name"], "evidence": row.get("evidence"), "files": 0,
            "bytes": 0, "dependencies": {}, "environment": [], "degraded": []}
    if not root.is_dir() or root.is_symlink() or any(p.is_symlink() for p in root.parents):
        item["degraded"].append("Registered directory is absent or its scope now uses a symlink.")
        item["git"] = {"present": False}
        return item
    item["git"] = git_metrics(root)
    for path, error in files_under(root):
        if error:
            item["degraded"].append(error)
            continue
        try:
            item["files"] += 1
            item["bytes"] += path.stat().st_size
            if path.name == ".env" or path.name.startswith(".env."):
                if path.name.endswith((".example", ".sample", ".template")):
                    continue
                size = path.stat().st_size
                if size > budget["bytes"] or budget["values"] <= 0:
                    if "Environment metadata budget reached; result is partial." not in item["degraded"]:
                        item["degraded"].append("Environment metadata budget reached; result is partial.")
                    continue
                budget["bytes"] -= size
                pairs = env_pairs(path)
                if len(pairs) > budget["values"]:
                    item["degraded"].append("Environment variable count budget reached; result is partial.")
                    budget["values"] = 0
                    continue
                budget["values"] -= len(pairs)
                sensitive.update(v for _, v in pairs if v)
                item["environment"].append({"file": str(path.relative_to(root)),
                    "private_mode": osprivacy().private(path),
                    "variables": [{"name": k, "present": bool(v),
                        "fingerprint": hmac.new(salt, v.encode(), hashlib.sha256).hexdigest() if v else None}
                        for k, v in pairs]})
            if path == root / "package.json" and path.stat().st_size <= MAX_ENV_BYTES:
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    item["dependencies"] = {k: len(data.get(k, {})) for k in ("dependencies", "devDependencies") if isinstance(data.get(k, {}), dict)}
        except (OSError, ValueError, ObservatoryError):
            item["degraded"].append("A metadata input could not be read; no content was recorded.")
    return item


def findings(project: dict) -> list[dict]:
    name, g = project["name"], project["git"]
    rows = []
    def add(kind, severity, detail, remedy):
        rows.append({"id": f"{name}:{kind}", "project": name, "kind": kind,
                     "severity": severity, "detail": detail, "remedy": remedy})
    if project["degraded"]:
        add("observation.partial", "warning", "Some inputs were unreadable; absence is not proof of safety.", "Check the registered directory and read permissions, then scan again.")
    if g.get("present") and not g.get("readable"):
        add("git.unreadable", "warning", "Git state could not be measured.", "Check Git availability and repository access.")
    if g.get("ahead"):
        add("git.ahead", "warning", "Commits are ahead of the locally known upstream.", "Review the branch and its authorized remote before pushing.")
    if g.get("behind"):
        add("git.behind", "info", "The locally known upstream has additional commits.", "Review incoming changes before integrating them.")
    if g.get("present") and g.get("readable") and not g.get("has_upstream"):
        add("git.no-upstream", "info", "This checkout has no upstream tracking branch.", "Decide whether this branch should remain local or have an authorized remote.")
    if g.get("dirty_files"):
        add("git.dirty", "info", "The working tree contains uncommitted changes.", "Review and save intentional changes; leave unrelated work intact.")
    if any(not e["private_mode"] for e in project["environment"]):
        add("env.permissions", "warning", "An environment file allows group or world access.", "Review ownership and set secret-bearing files to mode 600 where appropriate.")
    return rows


def scan(state: Path) -> dict:
    cfg = config(state)
    saltfile = state / "fingerprint-salt"
    private_file(saltfile)
    salt = saltfile.read_bytes()
    if len(salt) != 32:
        raise ObservatoryError("Fingerprint salt must contain exactly 32 bytes.")
    sensitive = set(known_slot_values(state))
    budget = {"bytes": 2 * 1024 * 1024, "values": 2000}
    projects = [observe_project(p, salt, sensitive, budget) for p in cfg["projects"]]
    report = {"schema_version": 1, "scanned_at": now(), "scope": "registered-projects-only",
              "network": "none", "projects": projects,
              "findings": [f for p in projects for f in findings(p)]}
    # Only metadata is persisted, and values echoed into names/paths are redacted too.
    # Timestamp/schema are generated by us; sanitize project/finding data before storage.
    report["projects"] = redact(report["projects"], sensitive)
    report["findings"] = redact(report["findings"], sensitive)
    write_json(state / "latest.json", report)
    dbfile = state / "history.sqlite3"
    if dbfile.exists():
        private_file(dbfile)
    else:
        write_private(dbfile, b"")
    with closing(sqlite3.connect(dbfile)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, at TEXT NOT NULL, report TEXT NOT NULL)")
        db.execute("INSERT INTO snapshots(at, report) VALUES (?, ?)", (report["scanned_at"], json.dumps(report)))
        # Bound local retention without pretending to be a backup service.
        db.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 100)")
    return report


def latest(state: Path) -> dict:
    config(state)
    if not (state / "latest.json").exists():
        return {"schema_version": 1, "scanned_at": None, "projects": [], "findings": [], "note": "No scan yet. Register a project, then run scan."}
    report = read_json(state / "latest.json")
    validate_report(report)
    return redact(report, known_slot_values(state))


def validate_report(report: dict) -> None:
    try:
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise ValueError()
        stamp = report.get("scanned_at")
        if stamp is not None:
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", stamp):
                raise ValueError()
            datetime.fromisoformat(stamp)
        if not isinstance(report["projects"], list) or not isinstance(report["findings"], list):
            raise ValueError()
        for p in report["projects"]:
            if not isinstance(p, dict) or any(type(p[k]) is not int or p[k] < 0 for k in ("files", "bytes")):
                raise ValueError()
            if not isinstance(p.get("degraded"), list) or not isinstance(p.get("git"), dict):
                raise ValueError()
            for key in ("dirty_files", "ahead", "behind", "tag_count"):
                val = p["git"].get(key)
                if val is not None and (type(val) is not int or val < 0):
                    raise ValueError()
        for f in report["findings"]:
            if f["severity"] not in ("warning", "info"):
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ObservatoryError("Stored snapshot has an invalid schema; no unvalidated data was exported.") from None


def public_export(report: dict) -> dict:
    validate_report(report)
    # Project names are user data too: publish no identifiers by default.
    return {"schema_version": 1, "scanned_at": report.get("scanned_at"),
            "project_count": len(report["projects"]),
            "finding_counts": {k: sum(f["severity"] == k for f in report["findings"]) for k in ("warning", "info")},
            "observed_files": sum(p["files"] for p in report["projects"]),
            "observed_bytes": sum(p["bytes"] for p in report["projects"]),
            "degraded_projects": sum(bool(p["degraded"]) for p in report["projects"]),
            "note": "Aggregate export; project names, paths, environment names and equality fingerprints omitted. Counts can still be sensitive."}
