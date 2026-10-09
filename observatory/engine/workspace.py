"""Initialize and migrate complete Observatory installations without publishing state."""
from __future__ import annotations
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import sqlite3
import tempfile
import uuid
import configuration as config


def write_json(path: Path, value: object) -> None:
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def reject_symlinks(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            # Name the path to use: "the resolved path" alone left a new user
            # guessing, typically under /tmp, which is itself a link on macOS.
            raise config.ConfigurationError(
                f"Workspace paths must not contain symlinks ({part} is one); use the resolved "
                f"path instead, e.g. OBSERVATORY_HOME={path.resolve()}")


@contextlib.contextmanager
def lock(base: Path):
    reject_symlinks(base)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / ".workspace.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise config.ConfigurationError("Another workspace operation is running") from None
        yield
    finally:
        os.close(fd)


#: The `[full]` extra's distributions, by the name pip installs them under.
#: tests/test_engine_doc_copies.py keeps this equal to pyproject.toml's extra.
#: Checked as installed DISTRIBUTIONS, not importable modules: the engine's own
#: `mcp/` package sits on sys.path and would answer for the absent MCP SDK.
FULL_DISTRIBUTIONS = ["mcp", "jsonschema", "sqlite-vec", "google-auth", "cryptography",
                      "snowballstemmer"]
#: The tested dependency set, shipped inside the engine (see requirements-full.lock).
LOCK_FILE = config.SOURCE / "requirements-full.lock"


def _absent(distribution: str) -> bool:
    try:
        importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return True
    return False


def full_extra_command() -> str:
    """The one command that installs the `[full]` extra into THIS interpreter at the tested versions.

    The package is not on PyPI: naming the installed version lets pip take the
    distribution that is already here and fetch only what its extra adds."""
    lock = f' -c "{LOCK_FILE}"' if LOCK_FILE.is_file() else ""
    return f"\"{sys.executable}\" -m pip install{lock} 'project-observatory[full]=={config.VERSION}'"


def require_runtime() -> dict:
    """Check the installed SQLite runtime entirely in memory before workspace writes.

    Two different faults look alike from here and have different fixes: an
    interpreter whose sqlite3 cannot load extensions (choose another Python), and
    a package installed without its `[full]` extra (install the extra). Each is
    named with its own remedy, and both when both are true."""
    interpreter = ("Full engine requires Python with SQLite 3.37+ and loadable extensions; this interpreter "
                   f"({sys.executable}, SQLite {sqlite3.sqlite_version}) {{why}}. On macOS use Homebrew "
                   "Python 3.14 in a virtual environment; reinstall the full package there.")
    missing = [name for name in FULL_DISTRIBUTIONS if _absent(name)]
    extra = ("The full engine's dependencies are not installed in this interpreter (missing: "
             + ", ".join(missing) + "): the package was installed without its `[full]` extra. Install it at "
             "the tested versions: " + full_extra_command()) if missing else ""

    def refuse(why: str) -> config.ConfigurationError:
        return config.ConfigurationError(" ".join(filter(None, (interpreter.format(why=why), extra))))

    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise refuse("is older")
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        if not hasattr(connection, "enable_load_extension"):
            raise refuse("cannot load extensions")
        if missing:
            raise config.ConfigurationError(extra)
        try:
            import sqlite_vec
            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
                connection.execute("SELECT vec_version()").fetchone()
            finally:
                connection.enable_load_extension(False)
        except (AttributeError, ImportError, OSError, sqlite3.Error) as exc:
            raise refuse(f"could not load sqlite-vec ({type(exc).__name__})") from None
    return {"sqlite_version": sqlite3.sqlite_version, "loadable_extensions": True,
            "sqlite_vec": "loadable"}


def initialize(base: Path, *, identities: bool = True) -> dict:
    require_runtime()
    reject_symlinks(base)
    # Version checks precede any mkdir, chmod or lock creation.
    marker = config.validate_workspace(base)
    if marker:
        config.load(base)
        if identities:
            ensure_identities(base)
        return {"status": "already-initialized", "version": config.VERSION}
    if base.exists() and any(base.iterdir()):
        raise config.ConfigurationError("Non-empty unversioned directory; use migrate-local into a new home")
    with lock(base):
        marker = config.validate_workspace(base)
        if marker:
            config.load(base)
            return {"status": "already-initialized", "version": config.VERSION}
        if any(p.name != ".workspace.lock" for p in base.iterdir()):
            raise config.ConfigurationError("Workspace changed during initialization")
        for directory in ("config", "store/raw", "store/logs", "registry", "docs/dashboard", "secrets", "backups"):
            (base / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        defaults = config.SOURCE / "defaults"
        for source in defaults.glob("*.json"):
            if source.name != "empty-registry.json":
                write_json(base / "config" / source.name, json.loads(source.read_text()))
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        registry = json.loads((defaults / "empty-registry.json").read_text())
        for name, value in registry.items():
            value["schema_version"] = config.REGISTRY_VERSIONS.get(name, 1)
            if "updated_on" in value:
                value["updated_on"] = now[:10]
            if "built_at" in value:
                value["built_at"] = now
            write_json(base / "registry" / name, value)
        write_json(base / "workspace.json", {
            "format_version": config.WORKSPACE_VERSION,
            "minimum_reader": config.VERSION, "minimum_writer": config.VERSION,
            "created_by": config.VERSION, "created_at": now,
            "instance_id": str(uuid.uuid4()), "registry_schema": 1,
        })
        if identities:
            ensure_identities(base)
    return {"status": "initialized", "version": config.VERSION, "integrations_enabled": 0}


def ensure_identities(base: Path, state: Path | None = None) -> None:
    """Create the keyserver token and fingerprint salt once, never replacing them.

    Readers refuse a missing or invalid identity instead of minting one, so
    initialization is the only place a new identity appears. Re-running init
    on an existing workspace is safe: an existing valid file is kept as is.
    """
    import runtime_identity
    if state is None:
        state = Path(os.environ["OBSERVATORY_STATE"]) if os.environ.get("OBSERVATORY_STATE") else base / "store"
    for kind, (name, _pattern, _make) in runtime_identity.KINDS.items():
        runtime_identity.load(state / name, kind, initialize=True)


def copy_private(source: Path, target: Path) -> int:
    """Copy regular files only; never follow links or copy executable Git state."""
    if source.is_symlink():
        raise config.ConfigurationError("Migration refuses symbolic links")
    if not source.exists():
        return 0
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        total = 0
        for child in source.iterdir():
            if child.name in {".git", "__pycache__", ".venv"}:
                continue
            total += copy_private(child, target / child.name)
        return total
    if not source.is_file():
        raise config.ConfigurationError("Migration refuses special files")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with os.fdopen(os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as inp:
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                shutil.copyfileobj(inp, out)
        def digest(file):
            h = hashlib.sha256()
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.digest()
        if digest(source) != digest(target):
            raise config.ConfigurationError("Source changed during copy; retry after stopping writers")
        # The file's own times travel with it: a restore that dated every file "now" moved the
        # tick's freshness gates (`find -mmin`) by the time since the snapshot (0.20.1).
        st = source.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return 1


def backup_database(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise config.ConfigurationError("Database must not be a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        from store.compatibility import read_consistently, verify_database
        with contextlib.closing(sqlite3.connect(target)) as dst:
            read_consistently(source, lambda src: (src.backup(dst), verify_database(dst)))
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def validate_data(base: Path, *, integrity: bool = False) -> dict:
    """Read-only registry/database compatibility guard, shared by import and doctor."""
    from store import compatibility, migrate
    reject_symlinks(base)
    config.validate_registries(base / "registry")
    database = base / "store/observatory.db"
    reject_symlinks(database)
    present = database.exists()
    try:
        compatibility.preflight(database)
        if integrity and present:
            try:
                compatibility.read_consistently(database, compatibility.verify_database)
            except (sqlite3.Error, RuntimeError):
                # A reader opened immutable sees a write that lands during the check as
                # damage. Before calling the store corrupt — which during `full update` would
                # roll a good update back — check once more with locks (audit A11).
                with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro",
                                                        uri=True, timeout=30)) as conn:
                    compatibility.verify_database(conn)
    except (sqlite3.Error, migrate.CompatibilityError, RuntimeError):
        raise config.ConfigurationError("Workspace database is corrupt or incompatible; use a compatible release or restore a verified backup") from None
    return {"present": present, "compatibility_checked": present,
            "integrity_checked": bool(integrity and present)}


def migrate_local(source: Path, target: Path, apply: bool) -> dict:
    require_runtime()
    source = source.expanduser().absolute()
    target = target.expanduser().absolute()
    reject_symlinks(source)
    reject_symlinks(target)
    if target == source or source in target.parents or target in source.parents:
        raise config.ConfigurationError("Source and target must be separate directories")
    if target.exists():
        raise config.ConfigurationError("Migration target must not already exist")
    if not (source / "paths.py").is_file() or not (source / "registry").is_dir():
        raise config.ConfigurationError("Expected an original Observatory installation")
    validate_data(source)
    candidates = [p for p in (source / "store").iterdir() if p.name not in {"__pycache__"} and not p.name.endswith((".py", ".sql"))]
    summary = {"status": "preview", "source_modified": False,
               "registry_files": sum(1 for p in (source / "registry").rglob("*") if p.is_file()),
               "store_entries": len(candidates), "external_credentials": "not copied; configure explicit references",
               "scheduler": "not activated", "apply_required": True}
    if not apply:
        return summary
    # Caller must stop writers for a cross-file snapshot; detect changes too.
    def snapshot():
        measured = {}
        files = [source / "paths.py", source / "identity.py"]
        files += [p for area in ("registry", "collectors", "store", "agent", "plugins/config")
                  for p in (source / area).rglob("*") if "__pycache__" not in p.parts
                  and not p.name.endswith("-shm")]
        for file in files:
            if file.is_symlink():
                raise config.ConfigurationError("Migration refuses symbolic links")
            if file.is_file():
                info = file.stat()
                checksum = hashlib.sha256()
                with file.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        checksum.update(block)
                measured[str(file.relative_to(source))] = (info.st_size, info.st_mtime_ns, checksum.hexdigest())
        return measured
    before = snapshot()
    stage = target.parent / ("." + target.name + ".migration-" + uuid.uuid4().hex)
    try:
        # Identities come from the original installation; only a missing one is
        # created, after the copy, so a salt is never replaced by a new one.
        initialize(stage, identities=False)
        for p in (stage / "registry").glob("*.json"):
            p.unlink()
        copy_private(source / "registry", stage / "registry")
        for p in (source / "collectors").glob("*.json"):
            destination = stage / "config" / p.name
            destination.unlink(missing_ok=True)
            copy_private(p, destination)
        for p in [source / "agent/models.json", source / "store/retention.json", source / "plugins/config/ga4_properties.json"]:
            if p.is_file():
                destination = stage / "config" / p.name
                destination.unlink(missing_ok=True)
                copy_private(p, destination)
        for p in candidates:
            if p.name == "retention.json" or p.name.startswith("observatory.db"):
                continue
            copy_private(p, stage / "store" / p.name)
        database = source / "store/observatory.db"
        if database.exists():
            backup_database(database, stage / "store/observatory.db")
        # Copy installation-specific curation into PRIVATE config, never source.
        import ast
        def parse_curation(file):
            try:
                return ast.parse(file.read_text())
            except (OSError, UnicodeError, SyntaxError):
                raise config.ConfigurationError("Original curation cannot be safely parsed; repair it before migration") from None
        identity_source = source / "identity.py"
        if identity_source.is_file():
            for node in parse_curation(identity_source).body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ID_OVERRIDE" for t in node.targets):
                    try:
                        overrides = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        raise config.ConfigurationError("Original identity overrides must be literal strings to migrate safely") from None
                    if isinstance(overrides, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()):
                        write_json(stage / "config/identity_overrides.json", {"overrides": overrides})
                    else:
                        raise config.ConfigurationError("Original identity overrides have an unsupported shape")
                    break
        import ast
        merge_source = source / "collectors/merge.py"
        if merge_source.is_file():
            tree = parse_curation(merge_source)
            work_organizations = []
            for comparison in ast.walk(tree):
                if isinstance(comparison, ast.Compare) and isinstance(comparison.left, ast.Name) and comparison.left.id == "owners":
                    for rhs in comparison.comparators:
                        if isinstance(rhs, ast.Set):
                            try:
                                candidate = ast.literal_eval(rhs)
                            except (ValueError, TypeError):
                                raise config.ConfigurationError("Original work-organization curation is not a literal set") from None
                            if all(isinstance(item, str) for item in candidate):
                                work_organizations.extend(candidate)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "OWNED_ORGS" for t in node.targets):
                    try:
                        organizations = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        raise config.ConfigurationError("Original ownership curation must be literal strings to migrate safely") from None
                    if isinstance(organizations, (list, set, tuple)) and all(isinstance(x, str) for x in organizations):
                        write_json(stage / "config/ownership.json", {"organizations": sorted(organizations), "work_organizations": sorted(set(work_organizations))})
                    else:
                        raise config.ConfigurationError("Original ownership curation has an unsupported shape")
                    break
        write_json(stage / "migration-receipt.json", {
            "source_version": "original-unversioned", "target_version": config.VERSION,
            "source_modified": False, "external_credentials_copied": False,
            "requires_source_configuration": True,
        })
        ensure_identities(stage, stage / "store")
        validate_data(stage, integrity=True)
        if before != snapshot():
            raise config.ConfigurationError("Source changed during migration; stop background writers and retry")
        os.rename(stage, target)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return {**summary, "status": "copied", "apply_required": False}


# Which configured sources each switch reads. A source left at its placeholder
# default (or pointing at a missing path) silently narrows coverage: with no
# `sessions` source the leak scan reads 4 targets instead of every transcript.
# doctor names that instead of letting a quiet collector look healthy.
# The same table is documented in docs/ONBOARDING.md, "Sources".
# The `sessions` integration's collector (collectors/scan_sessions.py) reads the
# Stop hook's own session records first and the claude-mem companion's database,
# `companion_db`, only where it is still installed ("not installed is not broken");
# the `sessions` transcripts are what the leak scan reads. So `companion_db` is
# OPTIONAL_SOURCES, not a need: required here, it sent doctor's reader after a
# database the companion's retirement had removed for good (0.19.2).
SOURCE_NEEDS = {
    ("integrations", "sessions"): ("sessions",),
    ("integrations", "mcp"): ("mcp_config_root",),
    ("integrations", "wiki"): ("wiki",),
    ("integrations", "openrouter"): ("secret_store",),
    ("integrations", "google"): ("secret_store",),
    ("integrations", "search_console"): ("secret_store",),
    ("integrations", "cloudflare_analytics"): ("secret_store",),
    ("features", "wiki_projection"): ("wiki",),
    ("features", "companion_remediation"): ("companion_home", "companion_db"),
}


#: Sources a switch reads when they are there: absent is fine, but one that is
#: configured and present in the wrong shape (SOURCE_FILES) still reads as nothing.
OPTIONAL_SOURCES = {
    ("integrations", "sessions"): ("companion_db",),
}

#: Sources that must be a regular file: the collector opens a database, so a
#: directory at that path reads as nothing just like a missing file.
SOURCE_FILES = frozenset({"companion_db"})


def coverage_warnings(doc: dict) -> list[dict]:
    """Enabled switches whose sources are unconfigured or missing (names and paths only).

    Every row carries both ways out as exact commands: `fix` points the source at
    a path, `disable` turns the switch off, because a collector left enabled
    without its source degrades on every tick.
    """
    sources = doc.get("sources", {})
    out = []
    # The base scan reads `projects` whatever is switched on, and it cannot be
    # switched off: no `disable` row, only the fix.
    projects = sources.get("projects")
    fix = "project-observatory full configure sources projects PATH"
    if not projects:
        out.append({"collector": "filesystem", "source": "projects",
                    "problem": "not configured; `full local` has no projects to scan", "fix": fix})
    elif not Path(projects).expanduser().is_dir():
        out.append({"collector": "filesystem", "source": "projects",
                    "problem": f"{projects} does not exist or is not a directory; `full local` has no projects to scan",
                    "fix": fix})
    for (section, name), needed in SOURCE_NEEDS.items():
        if doc.get(section, {}).get(name) is not True:
            continue
        for source in needed:
            value = sources.get(source)
            fix = {"fix": f"project-observatory full configure sources {source} PATH",
                   "disable": f"project-observatory full configure {section} {name} false"}
            if not value:
                out.append({section[:-1]: name, "source": source,
                            "problem": "not configured; the collector reads nothing from it", **fix})
                continue
            path = Path(value).expanduser()
            if not path.exists():
                out.append({section[:-1]: name, "source": source,
                            "problem": f"{value} does not exist; the collector reads nothing from it", **fix})
            elif source in SOURCE_FILES and not path.is_file():
                out.append({section[:-1]: name, "source": source,
                            "problem": f"{value} is not a file; the collector reads nothing from it", **fix})
    for (section, name), optional in OPTIONAL_SOURCES.items():
        if doc.get(section, {}).get(name) is not True:
            continue
        for source in optional:
            value = sources.get(source)
            path = Path(value).expanduser() if value else None
            if path is not None and path.exists() and source in SOURCE_FILES and not path.is_file():
                out.append({section[:-1]: name, "source": source,
                            "problem": f"{value} is not a file; the collector reads nothing from it",
                            "fix": f"project-observatory full configure sources {source} PATH",
                            "disable": f"project-observatory full configure {section} {name} false"})
    return out


def _tick_health(base: Path, doc: dict) -> dict:
    import tick_health
    store = base / "store"
    return tick_health.health(store, store / "raw",
                              scheduler_enabled=bool((doc.get("features") or {}).get("scheduler")))


def _switches(doc: dict, section: str) -> dict:
    """{name: on} for every known name, sorted; a switch is on only when `true`,
    the rule `configuration.enabled` applies."""
    found = doc.get(section, {}) if isinstance(doc.get(section), dict) else {}
    names = sorted(set(config.known_names(section)) | set(found))
    return {name: found.get(name) is True for name in names}


def doctor(base: Path) -> dict:
    runtime = require_runtime()
    config.validate_workspace(base, required=True)
    doc = config.load(base)
    database = validate_data(base, integrity=True)
    return {"version": config.VERSION, "runtime": runtime, "database": database, "workspace_format": config.WORKSPACE_VERSION,
            "configuration_schema": doc["schema_version"],
            # EVERY name `configure` accepts, on or off: the onboarding says
            # "list current settings with doctor", and a fresh workspace listed
            # `{}`. Unknown names a hand-edited file carries are kept as found.
            "sources": {**{k: {"configured": True, "exists": Path(v).expanduser().exists()}
                           for k, v in doc.get("sources", {}).items()},
                        **{k: {"configured": False} for k in sorted(config.known_names("sources"))
                           if not doc.get("sources", {}).get(k)}},
            "integrations": _switches(doc, "integrations"),
            "features": _switches(doc, "features"),
            "interface": {"locale": doc.get("interface", {}).get("locale", "en")},
            "coverage_warnings": coverage_warnings(doc),
            "backups": __import__("backup_vault").status(base),
            # Updates that arrive by themselves and the daily encrypted backup: whether the
            # job is scheduled, what it last found, and what needs a person (maintenance.py).
            "maintenance": __import__("maintenance").status(base),
            # PB-132: a dead or interrupted tick cannot report itself.
            "tick": _tick_health(base, doc),
            "credentials": "values are never returned", "network_calls": 0,
            # PB-137 N-003: whether any memory text may leave for a remote embedding
            # model, and for which projects — read-only, names and dates only.
            "embedding_policy": _embedding_policy(base),
            # PB-137 N-005: each vector index with the model it belongs to, its state
            # (legacy, inactive, backfilling, ready, active, retired) and its coverage.
            "vector_namespaces": _vector_namespaces(base),
            # PB-137 N-008: who may reach memory over HTTP besides the local stdio agent.
            "access_bindings": _access_bindings(base),
            # With the agent on, whether it has a model and a budget: a fresh
            # models.json has neither, and the refusal a call meets says less.
            **({"agent": config.model_readiness(base, _key_report())}
               if (doc.get("features") or {}).get("agent") is True else {})}


def _embedding_policy(base: Path) -> dict:
    sys.path.insert(0, str(config.SOURCE))
    import embedding_policy
    try:
        cfg = json.loads((base / "config" / "models.json").read_text(encoding="utf-8"))["embedding"]
        configured = {"provider": cfg.get("provider"), "model": cfg.get("model")}
    except (OSError, ValueError, KeyError, TypeError):
        configured = {}
    return embedding_policy.status(base / "config", base / "store", configured)


def _access_bindings(base: Path) -> dict:
    sys.path.insert(0, str(config.SOURCE))
    import memory_access
    return memory_access.status(base / "config", base / "store")


def _vector_namespaces(base: Path) -> list[dict]:
    """The vector namespaces, read-only: names, states and counts, never vectors."""
    from store import compatibility, namespaces
    database = base / "store/observatory.db"
    if not database.exists():
        return []
    def summary(conn):
        conn.row_factory = sqlite3.Row
        return namespaces.summary(conn)
    return compatibility.read_consistently(database, summary)


def _key_report() -> dict:
    """The assistant key's state and source, from the provider module itself so
    doctor and `assistant status` cannot disagree about where a key is found."""
    sys.path.insert(0, str(config.SOURCE))
    from agent import providers
    return providers.key_report()


#: Every usage line names the entry point a person types, never this file.
PROG = "project-observatory full"


def _parser() -> argparse.ArgumentParser:
    """The parser for the workspace commands `main` handles itself."""
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create the private workspace, or complete one; an empty home "
                                       "first restores the newest backup a workspace at this path left")
    init.add_argument("--fresh", action="store_true", help="start empty even when backups exist")
    sub.add_parser("doctor", help="workspace health, sources and backups")
    sub.add_parser("version", help="the engine, workspace and configuration versions")
    sub.add_parser("onboard", help="print the agent onboarding guide")
    migration = sub.add_parser("migrate-local", help="copy an original full-engine installation into a new workspace; previews without --apply")
    migration.add_argument("source", type=Path)
    migration.add_argument("--apply", action="store_true")
    migration.add_argument("--writers-stopped", action="store_true")
    conf = sub.add_parser("configure", help="set one setting in config/settings.json; model and budget write config/models.json")
    conf.add_argument("section", choices=["sources", "integrations", "features", "interface", "storage", "model", "budget"])
    conf.add_argument("name")
    conf.add_argument("value")
    phrase = sub.add_parser("backup-passphrase", help="the passphrase that encrypts backups (stdin)")
    phrase.add_argument("action", choices=["set", "status", "show"])
    backups = sub.add_parser("backups", help="where backups go; move legacy copies; decrypt one")
    backups.add_argument("action", choices=["status", "migrate", "decrypt"])
    backups.add_argument("file", nargs="?", type=Path)
    backups.add_argument("output", nargs="?", type=Path)
    return ap


def machine_parser(name: str) -> argparse.ArgumentParser:
    """`machine` and `cleanup`. They read their flags by hand until 0.12, so
    `machine --help` ran the survey and `cleanup --help` wrote a plan."""
    if name == "machine":
        ap = argparse.ArgumentParser(prog=f"{PROG} machine",
                                     description="processes by origin, memory and disk; why one process runs")
        ap.add_argument("--disk", action="store_true", help="also size the disk's largest places (slower)")
        ap.add_argument("--explain", type=int, metavar="PID", help="why one process runs, and print nothing else")
        return ap
    ap = argparse.ArgumentParser(prog=f"{PROG} cleanup",
                                 description="plan cleanup, or remove what loses nothing; without --apply only the plan is written")
    ap.add_argument("--apply", action="store_true", help="remove the auto tier")
    ap.add_argument("--include", choices=["manual"],
                    help="with --apply, also archive and remove the manual tier")
    return ap


def _tool_module(name: str):
    sys.path.insert(0, str(config.SOURCE / "tools"))
    return __import__(name)


def parse(argv: list[str]) -> argparse.Namespace:
    """Parse a workspace command line exactly as `main` will, and run nothing.

    Each command's own parser factory is the one its `main` uses, so the gate
    (`observatory.refusal`) and the dispatcher cannot disagree about what is
    accepted. argparse's `SystemExit` propagates: 0 after `--help`, 2 on a refusal.
    """
    name, rest = argv[0], argv[1:]
    if name == "open":
        return _tool_module("dashboard_open").parser().parse_args(rest)
    if name == "agent":
        return _tool_module("agent_plugin").parser().parse_args(rest)
    if name == "update":
        import engine_update
        return engine_update.parser().parse_args(rest)
    if name == "maintain":
        import maintenance
        return maintenance.parser().parse_args(rest)
    if name == "auto-update":
        import maintenance
        return maintenance.auto_update_parser().parse_args(rest)
    if name == "profile":
        import workspace_profile
        return workspace_profile.parser().parse_args(rest)
    if name in {"machine", "cleanup"}:
        return machine_parser(name).parse_args(rest)
    if name in {"workspace-backup", "upgrade", "restore"}:
        import workspace_upgrade
        return workspace_upgrade.parser().parse_args(
            ["backup" if name == "workspace-backup" else name, *rest])
    return _parser().parse_args(argv)


def main(argv: list[str]) -> int:
    if argv and argv[0] in {"open", "agent"}:
        module = _tool_module("dashboard_open" if argv[0] == "open" else "agent_plugin")
        return module.main(argv[1:])
    if argv and argv[0] == "update":
        import engine_update
        return engine_update.main(argv[1:])
    if argv and argv[0] == "maintain":
        import maintenance
        return maintenance.main(argv[1:])
    if argv and argv[0] == "auto-update":
        import maintenance
        return maintenance.auto_update_main(argv[1:])
    if argv and argv[0] == "profile":
        import workspace_profile
        return workspace_profile.main(argv[1:])
    if argv and argv[0] in {"machine", "cleanup"}:
        return machine_command(argv[0], argv[1:])
    if argv and argv[0] in {"workspace-backup", "upgrade", "restore"}:
        import workspace_upgrade
        return workspace_upgrade.main(["backup" if argv[0] == "workspace-backup" else argv[0], *argv[1:]])
    a = _parser().parse_args(argv)
    try:
        base = config.home()
        if a.command == "backup-passphrase":
            return _passphrase_command(base, a.action)
        if a.command == "backups":
            return _backups_command(base, a)
        if a.command == "init":
            result = _init(base, fresh=a.fresh)
        elif a.command == "version":
            result = {"application": config.VERSION, "workspace": config.WORKSPACE_VERSION, "config": config.CONFIG_VERSION}
        elif a.command == "doctor":
            result = doctor(base)
        elif a.command == "migrate-local":
            if a.apply and not a.writers_stopped:
                raise config.ConfigurationError("Stop source writers, then pass --writers-stopped with --apply")
            result = migrate_local(a.source, base, a.apply)
        elif a.command == "onboard":
            print((config.SOURCE / "docs/AGENT-ONBOARDING.md").read_text())
            return 0
        else:
            config.validate_workspace(base, required=True)
            doc = config.load(base)
            if not re_safe_name(a.name):
                raise config.ConfigurationError("Invalid configuration name")
            if a.section in ("integrations", "features", "sources") and a.name not in config.known_names(a.section):
                raise config.ConfigurationError(
                    f"Unknown {a.section[:-1]}: {a.name}; known: {', '.join(sorted(config.known_names(a.section)))}")
            if a.section in ("model", "budget"):
                return _configure_models(base, a)
            if a.section == "storage":
                if a.name not in config.STORAGE_SETTINGS:
                    raise config.ConfigurationError(f"Unknown storage setting: {a.name}; known: {', '.join(config.STORAGE_SETTINGS)}")
                value = Path(a.value).expanduser()
                if not value.is_absolute():
                    raise config.ConfigurationError("Storage path must be absolute")
                value = str(value)
            elif a.section == "interface":
                choices = config.INTERFACE_SETTINGS.get(a.name)
                if choices is None:
                    raise config.ConfigurationError(f"Unknown interface setting: {a.name}; known: {', '.join(config.INTERFACE_SETTINGS)}")
                if a.value not in choices:
                    raise config.ConfigurationError(f"Interface {a.name} must be one of: {', '.join(choices)}")
                value = a.value
            elif a.section == "sources":
                value = Path(a.value).expanduser()
                if not value.is_absolute():
                    raise config.ConfigurationError("Source path must be absolute")
                value = str(value)
            else:
                if a.value not in {"true", "false"}:
                    raise config.ConfigurationError("Use true or false")
                value = a.value == "true"
            with lock(base):
                doc = config.load(base)
                doc.setdefault(a.section, {})[a.name] = value
                write_json(base / "config/settings.json", doc)
            result = {"status": "configured", "section": a.section, "name": a.name}
            if a.section == "interface":
                # The pages carry the language they were built in; say how to see it.
                result["next"] = "project-observatory full open --rebuild"
        print(json.dumps(result, indent=2))
        return 0
    except (config.ConfigurationError, OSError, sqlite3.Error) as exc:
        print(f"Observatory: {exc}", file=__import__('sys').stderr)
        return 2


def _init(base: Path, *, fresh: bool = False) -> dict:
    """`init`, and what makes a fresh install whole without a further step (D5, D7).

    On an empty home, unless --fresh, the newest backup a workspace at this path left is
    restored first. Then — only where this process may touch the machine
    (`maintenance.system_setup_allowed`: a person's terminal, or OBSERVATORY_SYSTEM_SETUP=1)
    — the hourly maintenance job is scheduled and the backup passphrase is generated and
    kept outside the workspace. Elsewhere the answer says how that will happen."""
    import maintenance
    allowed = maintenance.system_setup_allowed(base)
    restored = None
    empty = not base.exists() or not any(p.name != ".workspace.lock" for p in base.iterdir())
    if empty and not fresh and allowed:
        restored = maintenance.restore_latest(base)
    result = initialize(base)
    if restored and restored.get("status") == "restored":
        result = {**result, "status": "restored", "restored_from": restored.get("from"),
                  "files": restored.get("files")}
    elif restored and restored.get("status") in ("backups-found-locked", "several-workspaces", "restore-failed"):
        result["backups_found"] = {k: restored[k] for k in ("newest", "backups", "detail", "next", "skipped")
                                   if k in restored}
    if allowed:
        result["maintenance"] = maintenance.ensure(base)
    else:
        result["maintenance"] = {
            "result": "not-scheduled-here",
            "detail": "updates and daily backups are scheduled from a terminal, by the observatory-log "
                      "plugin at the next session, or with `project-observatory full maintain ensure`"}
    return result


def _configure_models(base: Path, a) -> int:
    """`configure model chain ID[,ID…]` and `configure budget CEILING AMOUNT`:
    the agent's model chain and wallet ceilings in `config/models.json`, written
    atomically under the workspace lock with every other field kept."""
    import math
    if a.section == "model":
        if a.name != "chain":
            raise config.ConfigurationError("Unknown model setting: " + a.name + "; known: chain")
        ids = [part.strip() for part in a.value.split(",") if part.strip()]
        bad = [i for i in ids if not config.MODEL_ID.fullmatch(i)]
        if not ids or bad:
            raise config.ConfigurationError(
                "A model chain is one or more ids like vendor/model, comma-separated"
                + (f"; not an id: {', '.join(bad)}" if bad else ""))
    else:
        if a.name not in config.BUDGET_SETTINGS:
            raise config.ConfigurationError(f"Unknown budget setting: {a.name}; known: {', '.join(config.BUDGET_SETTINGS)}")
        try:
            amount = float(a.value)
        except ValueError:
            amount = math.nan
        if not math.isfinite(amount) or amount < 0:
            raise config.ConfigurationError("A budget ceiling is a non-negative number, in the wallet's denomination")
    with lock(base):
        file = base / "config" / "models.json"
        doc = json.loads(file.read_text(encoding="utf-8"))
        if a.section == "model":
            doc["chain"] = [{"id": i, "why": "configured"} for i in ids]
        else:
            doc.setdefault("wallet", {})[a.name] = amount
        write_json(file, doc)
    result = {"status": "configured", "section": a.section, "name": a.name,
              "model_status": config.model_readiness(base)["model_status"]}
    print(json.dumps(result, indent=2))
    return 0


def machine_command(name: str, argv: list[str]) -> int:
    """`machine [--disk] [--explain PID]` surveys and prints; `cleanup [--apply]
    [--include manual]` refreshes the git survey, then plans or acts.

    Parsed BEFORE the workspace is touched, so `--help` and a refused flag cost
    nothing; anything undeclared is refused rather than ignored."""
    a = machine_parser(name).parse_args(argv)
    config.validate_workspace(config.home(), required=True)
    for sub in ("collectors", "tools"):
        sys.path.insert(0, str(config.SOURCE / sub))
    import paths
    import scan_machine
    if name == "machine":
        if a.explain is not None:
            print(json.dumps(scan_machine.explain(a.explain), indent=1, ensure_ascii=False))
            return 0
        out = paths.SCRATCH / "machine.json"
        scan_machine.main(["scan_machine.py", str(out), *(["--disk"] if a.disk else [])])
        doc = json.loads(out.read_text(encoding="utf-8"))
        print(json.dumps({"memory": doc["memory"], "volume": doc["disk"]["volume"],
                          "largest_origins": doc["processes"].get("groups", [])[:12],
                          "largest_locations": (doc["disk"].get("locations") or [])[:12],
                          # The split dashboard's own page; the single page this named
                          # was retired with the split (2026-10-06).
                          "page": str(paths.DASHBOARD_DIR / "machine.html")}, indent=1, ensure_ascii=False))
        return 0
    import scan_git_hygiene
    import cleanup
    scan_git_hygiene.main(["scan_git_hygiene.py", str(paths.SCRATCH / "git-hygiene.json")])
    args = ["cleanup.py"] + (["--apply"] if a.apply else []) + (["manual"] if a.include == "manual" else [])
    return cleanup.main(args)


def _passphrase_command(base: Path, action: str) -> int:
    """`set` reads stdin (a terminal gets a hidden prompt, twice); `show` prints only
    to a terminal, because a value printed into a pipe lands in whatever captures
    it — an agent transcript outlives the backup it protects."""
    import backup_vault
    config.validate_workspace(base, required=True)
    if action == "status":
        print(json.dumps({"passphrase": "configured" if backup_vault.passphrase(base) else "missing",
                          "file": str(backup_vault.passphrase_file(base))}, indent=2))
        return 0
    if action == "show":
        if not sys.stdout.isatty():
            print("Observatory: refusing to print the backup passphrase anywhere but a terminal", file=sys.stderr)
            return 2
        value = backup_vault.passphrase(base)
        if not value:
            print("Observatory: no backup passphrase is configured", file=sys.stderr)
            return 2
        print(value)
        return 0
    if sys.stdin.isatty():
        import getpass
        value = getpass.getpass("New backup passphrase: ")
        if value != getpass.getpass("Repeat it: "):
            print("Observatory: the two entries differ; nothing was changed", file=sys.stderr)
            return 2
    else:
        value = sys.stdin.readline().rstrip("\n")
    file = backup_vault.set_passphrase(base, value)
    print(json.dumps({"status": "configured", "file": str(file),
                      "next": "keep this passphrase outside this machine; a restore elsewhere needs it"}, indent=2))
    return 0


def _backups_command(base: Path, a) -> int:
    import backup_vault
    config.validate_workspace(base, required=True)
    if a.action == "status":
        result = backup_vault.status(base)
    elif a.action == "migrate":
        with lock(base):
            result = backup_vault.migrate(base)
    else:
        if not a.file or not a.output:
            raise config.ConfigurationError("Usage: backups decrypt FILE OUTPUT")
        if a.output.exists():
            raise config.ConfigurationError("Decrypt output must not exist")
        if not a.file.exists() and a.file.parent == Path("."):
            # `backups status` names the newest copy by file name; that name is
            # resolved in this workspace's backups root.
            in_root = backup_vault.root_info(base)["path"] / a.file
            if not in_root.is_file():
                raise config.ConfigurationError(
                    f"No backup named {a.file} here or in {in_root.parent}; "
                    f"`project-observatory full backups status` lists the root and the newest of each kind")
            a.file = in_root
        elif not a.file.is_file():
            raise config.ConfigurationError(
                f"No backup file at {a.file}; `project-observatory full backups status` lists the root")
        secret = backup_vault.require_passphrase(base, prompt=True)
        header = backup_vault.verify_file(a.file, secret)
        if header.get("content") == "tar+gzip":
            stage = backup_vault.extract_tree(a.file, a.output.resolve().parent, secret)
            try:
                os.rename(stage, a.output)
            except BaseException:
                # The output appeared after the check above: the decrypted copy is a
                # hidden folder of plaintext secrets and must not be left beside it.
                shutil.rmtree(stage, ignore_errors=True)
                raise
        else:
            fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                backup_vault.decrypt_to(a.file, out, secret)
        result = {"status": "decrypted", "kind": header.get("kind"), "output": str(a.output)}
    print(json.dumps(result, indent=2))
    return 0


def re_safe_name(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value))
