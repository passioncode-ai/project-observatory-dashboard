"""Initialize and migrate complete Observatory installations without publishing state."""
from __future__ import annotations
import argparse
import contextlib
import datetime
import fcntl
import hashlib
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
            raise config.ConfigurationError("Workspace paths must not contain symlinks; use the resolved path")


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


def require_runtime() -> dict:
    """Check the installed SQLite runtime entirely in memory before workspace writes."""
    message = ("Full engine requires Python with SQLite 3.37+ and loadable extensions, "
               "plus the locked sqlite-vec dependency. On macOS use Homebrew Python "
               "3.14 in a virtual environment; reinstall the full package there.")
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise config.ConfigurationError(message)
    try:
        import sqlite_vec
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
                connection.execute("SELECT vec_version()").fetchone()
            finally:
                connection.enable_load_extension(False)
    except (AttributeError, ImportError, OSError, sqlite3.Error):
        raise config.ConfigurationError(message) from None
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
        from store.compatibility import readonly_uri, verify_database
        with contextlib.closing(sqlite3.connect(readonly_uri(source), uri=True)) as src, contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            verify_database(dst)
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
            with contextlib.closing(sqlite3.connect(compatibility.readonly_uri(database), uri=True)) as conn:
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


def coverage_warnings(doc: dict) -> list[dict]:
    """Enabled switches whose sources are unconfigured or missing (names and paths only)."""
    sources = doc.get("sources", {})
    out = []
    for (section, name), needed in SOURCE_NEEDS.items():
        if doc.get(section, {}).get(name) is not True:
            continue
        for source in needed:
            value = sources.get(source)
            if not value:
                out.append({section[:-1]: name, "source": source,
                            "problem": "not configured; the collector reads nothing from it",
                            "fix": f"project-observatory full configure sources {source} PATH"})
            elif not Path(value).expanduser().exists():
                out.append({section[:-1]: name, "source": source, "problem": f"{value} does not exist"})
    return out


def _tick_health(base: Path, doc: dict) -> dict:
    import tick_health
    store = base / "store"
    return tick_health.health(store, store / "raw",
                              scheduler_enabled=bool((doc.get("features") or {}).get("scheduler")))


def doctor(base: Path) -> dict:
    runtime = require_runtime()
    config.validate_workspace(base, required=True)
    doc = config.load(base)
    database = validate_data(base, integrity=True)
    return {"version": config.VERSION, "runtime": runtime, "database": database, "workspace_format": config.WORKSPACE_VERSION,
            "configuration_schema": doc["schema_version"],
            "sources": {k: {"configured": True, "exists": Path(v).expanduser().exists()} for k, v in doc.get("sources", {}).items()},
            "integrations": doc.get("integrations", {}),
            "features": doc.get("features", {}),
            "interface": {"locale": doc.get("interface", {}).get("locale", "en")},
            "coverage_warnings": coverage_warnings(doc),
            "backups": __import__("backup_vault").status(base),
            # PB-132: a dead or interrupted tick cannot report itself.
            "tick": _tick_health(base, doc),
            "credentials": "values are never returned", "network_calls": 0}


def main(argv: list[str]) -> int:
    if argv and argv[0] in {"open", "agent"}:
        sys.path.insert(0, str(config.SOURCE / "tools"))
        module = __import__("dashboard_open" if argv[0] == "open" else "agent_plugin")
        return module.main(argv[1:])
    if argv and argv[0] == "update":
        import engine_update
        return engine_update.main(argv[1:])
    if argv and argv[0] == "profile":
        import workspace_profile
        return workspace_profile.main(argv[1:])
    if argv and argv[0] in {"machine", "cleanup"}:
        return machine_command(argv[0], argv[1:])
    if argv and argv[0] in {"workspace-backup", "upgrade", "restore"}:
        import workspace_upgrade
        return workspace_upgrade.main(["backup" if argv[0] == "workspace-backup" else argv[0], *argv[1:]])
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    sub.add_parser("version")
    sub.add_parser("onboard")
    migration = sub.add_parser("migrate-local")
    migration.add_argument("source", type=Path)
    migration.add_argument("--apply", action="store_true")
    migration.add_argument("--writers-stopped", action="store_true")
    conf = sub.add_parser("configure")
    conf.add_argument("section", choices=["sources", "integrations", "features", "interface", "storage"])
    conf.add_argument("name")
    conf.add_argument("value")
    phrase = sub.add_parser("backup-passphrase")
    phrase.add_argument("action", choices=["set", "status", "show"])
    backups = sub.add_parser("backups")
    backups.add_argument("action", choices=["status", "migrate", "decrypt"])
    backups.add_argument("file", nargs="?", type=Path)
    backups.add_argument("output", nargs="?", type=Path)
    a = ap.parse_args(argv)
    try:
        base = config.home()
        if a.command == "backup-passphrase":
            return _passphrase_command(base, a.action)
        if a.command == "backups":
            return _backups_command(base, a)
        if a.command == "init":
            result = initialize(base)
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


def machine_command(name: str, argv: list[str]) -> int:
    """`machine [--disk] [--explain PID]` surveys and prints; `cleanup [--apply]
    [--include manual]` refreshes the git survey, then plans or acts."""
    config.validate_workspace(config.home(), required=True)
    for sub in ("collectors", "tools"):
        sys.path.insert(0, str(config.SOURCE / sub))
    import paths
    import scan_machine
    if name == "machine":
        if "--explain" in argv:
            try:
                pid = int(argv[argv.index("--explain") + 1])
            except (IndexError, ValueError):
                print("Observatory: --explain takes a process id", file=sys.stderr)
                return 2
            print(json.dumps(scan_machine.explain(pid), indent=1, ensure_ascii=False))
            return 0
        out = paths.SCRATCH / "machine.json"
        scan_machine.main(["scan_machine.py", str(out), *(["--disk"] if "--disk" in argv else [])])
        doc = json.loads(out.read_text(encoding="utf-8"))
        print(json.dumps({"memory": doc["memory"], "volume": doc["disk"]["volume"],
                          "largest_origins": doc["processes"].get("groups", [])[:12],
                          "largest_locations": (doc["disk"].get("locations") or [])[:12],
                          "page": "projects-dashboard.html → Machine"}, indent=1, ensure_ascii=False))
        return 0
    import scan_git_hygiene
    import cleanup
    scan_git_hygiene.main(["scan_git_hygiene.py", str(paths.SCRATCH / "git-hygiene.json")])
    args = ["cleanup.py"] + (["--apply"] if "--apply" in argv else []) + (["manual"] if "manual" in argv else [])
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
        secret = backup_vault.require_passphrase(base, prompt=True)
        header = backup_vault.verify_file(a.file, secret)
        if header.get("content") == "tar+gzip":
            stage = backup_vault.extract_tree(a.file, a.output.resolve().parent, secret)
            os.rename(stage, a.output)
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
