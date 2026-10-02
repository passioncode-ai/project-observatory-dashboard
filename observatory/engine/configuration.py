"""Versioned per-user configuration. Importing this module does not write files."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

VERSION = "0.12.0"
CONFIG_VERSION = 1
WORKSPACE_VERSION = 1
SOURCE = Path(__file__).resolve().parent

class ConfigurationError(RuntimeError):
    pass


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
            raise ConfigurationError(
                f"Choose a private directory outside the installed code and its source checkout "
                f"(the chosen home is inside {root}); state there is lost on upgrade or can be committed.")
    return path

def home() -> Path:
    value = os.environ.get("OBSERVATORY_HOME")
    if value and not Path(value).expanduser().is_absolute():
        raise ConfigurationError("OBSERVATORY_HOME must be an absolute path")
    return refuse_home_inside_code(Path(value).expanduser() if value else Path.home() / ".local/share/project-observatory-full")

def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ConfigurationError("Invalid compatibility version")
    return tuple(map(int, value.split(".")))

def read_json(file: Path) -> dict:
    if any(p.is_symlink() for p in (file, *file.parents)):
        raise ConfigurationError("Configuration paths must not contain symbolic links")
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigurationError(f"Cannot read configuration file: {file.name}") from None
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected an object in {file.name}")
    return value

def validate_workspace(base: Path | None = None, *, required: bool = False) -> dict:
    base = base or home()
    if (base / "upgrade-in-progress.json").exists():
        raise ConfigurationError("Interrupted workspace upgrade; restore its verified snapshot into a new home")
    marker = base / "workspace.json"
    if marker.is_symlink():
        raise ConfigurationError("Workspace marker must not be a symbolic link")
    if not marker.exists():
        if required:
            raise ConfigurationError("Workspace is not initialized; run observatory.py init")
        return {}
    doc = read_json(marker)
    v = doc.get("format_version")
    if type(v) is not int or v != WORKSPACE_VERSION:
        raise ConfigurationError("Unsupported workspace format; use a compatible Observatory release")
    for field in ("minimum_reader", "minimum_writer"):
        if version_tuple(doc.get(field)) > version_tuple(VERSION):
            raise ConfigurationError("This workspace requires a newer Observatory release")
    return doc

#: What `interface` in settings.json may hold, and the values each accepts.
#: English is the default; `locale` is read by the dashboard (dashboard/i18n.py).
INTERFACE_SETTINGS = {"locale": ("en", "ru")}

#: What `storage` in settings.json may hold. `backups` is the root encrypted
#: backups are written to (backup_vault.root_info); an absolute path.
STORAGE_SETTINGS = ("backups",)

#: Every switch and source the engine reads. `configure` refuses any other name:
#: a typo used to be answered "configured" and then did nothing. The workspace
#: suite derives the names the code actually reads (`enabled(...)`, `source_path`,
#: the tick's step gates, every `configure ...` command the engine prints) and
#: fails when one is missing here. A metric plugin's `integration:KEY` adds itself.
KNOWN_INTEGRATIONS = frozenset({
    "bitbucket", "cloudflare", "cloudflare_analytics", "domains", "ga4", "git_remotes",
    "github", "google", "heroku", "mcp", "openrouter", "remote_env", "search_console",
    "sessions", "wiki"})
KNOWN_FEATURES = frozenset({
    "agent", "auto_cleanup", "companion_remediation", "embeddings", "fixture_cleanup",
    "machine_watch", "notifications", "probe_fixture", "registry_history", "retention",
    "scheduler", "wiki_projection"})
KNOWN_SOURCES = frozenset({
    "cloudflare_snapshot", "companion_db", "companion_home", "domain_export", "gateway_root",
    "mcp_config_root", "projects", "secret_store", "secrets", "sessions", "wiki"})


def known_names(section: str) -> frozenset:
    if section == "features":
        return KNOWN_FEATURES
    if section == "sources":
        return KNOWN_SOURCES
    if section != "integrations":
        raise ValueError(section)
    names = set(KNOWN_INTEGRATIONS)
    for manifest in sorted((Path(__file__).resolve().parent / "plugins").glob("*.json")):
        try:
            requires = json.loads(manifest.read_text(encoding="utf-8")).get("requires") or []
        except (OSError, ValueError, AttributeError):
            continue
        names |= {r.split(":", 1)[1] for r in requires
                  if isinstance(r, str) and r.startswith("integration:")}
    return frozenset(names)

def interface_locale(base: Path | None = None) -> str:
    """The dashboard's language for this workspace: `interface.locale`, else English."""
    return load(base).get("interface", {}).get("locale", "en")

def load(base: Path | None = None) -> dict:
    base = base or home()
    validate_workspace(base)
    file = base / "config" / "settings.json"
    if file.is_symlink():
        raise ConfigurationError("Settings must not be a symbolic link")
    if not file.exists():
        return {"schema_version": CONFIG_VERSION, "sources": {}, "integrations": {}, "features": {}}
    doc = read_json(file)
    if type(doc.get("schema_version")) is not int or doc["schema_version"] != CONFIG_VERSION:
        raise ConfigurationError("Unsupported configuration schema; run a compatible release")
    for key in ("sources", "integrations", "features"):
        if not isinstance(doc.get(key, {}), dict):
            raise ConfigurationError(f"Configuration {key} must be an object")
    for key, value in doc.get("sources", {}).items():
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise ConfigurationError(f"Source {key} must be an absolute path")
    for section in ("integrations", "features"):
        if any(type(v) is not bool for v in doc.get(section, {}).values()):
            raise ConfigurationError(f"Configuration {section} values must be boolean")
    # INTERFACE is optional and ignored by releases before 0.4.0, so a workspace
    # that sets a language stays readable by an older reader. Only the keys in
    # INTERFACE_SETTINGS exist; an unknown key or value is refused, not dropped.
    interface = doc.get("interface", {})
    if not isinstance(interface, dict):
        raise ConfigurationError("Configuration interface must be an object")
    for key, value in interface.items():
        if key not in INTERFACE_SETTINGS:
            raise ConfigurationError(f"Unknown interface setting: {key}")
        if value not in INTERFACE_SETTINGS[key]:
            raise ConfigurationError(f"Interface {key} must be one of: {', '.join(INTERFACE_SETTINGS[key])}")
    # STORAGE is optional and ignored by releases before 0.4.1 (they never read it),
    # so setting a backups root keeps the workspace readable by an older reader.
    storage = doc.get("storage", {})
    if not isinstance(storage, dict):
        raise ConfigurationError("Configuration storage must be an object")
    for key, value in storage.items():
        if key not in STORAGE_SETTINGS:
            raise ConfigurationError(f"Unknown storage setting: {key}")
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise ConfigurationError(f"Storage {key} must be an absolute path")
    required = doc.get("must_understand", [])
    if not isinstance(required, list) or required:
        raise ConfigurationError("Configuration requires unsupported capabilities")
    return doc

def source_path(name: str, fallback: str, env: str | None = None) -> Path:
    if env and os.environ.get(env):
        value = Path(os.environ[env]).expanduser()
        if not value.is_absolute():
            raise ConfigurationError(f"{env} must be an absolute path")
        return value
    value = load().get("sources", {}).get(name)
    return Path(value).expanduser() if value else home() / "unconfigured" / fallback

def enabled(name: str, section: str = "integrations") -> bool:
    if section == "integrations" and os.environ.get("OBSERVATORY_OFFLINE") == "1":
        return False
    return load().get(section, {}).get(name, False) is True

REGISTRY_VERSIONS = {"projects.json": 2, "repositories.json": 2, "relations.json": 2}

def validate_registry_document(file: Path, doc: dict) -> None:
    if not isinstance(doc, dict):
        raise ConfigurationError(f"Registry {file.name} must be an object")
    version = doc.get("schema_version", 1)
    maximum = REGISTRY_VERSIONS.get(file.name, 1)
    if type(version) is not int or version < 1 or version > maximum:
        raise ConfigurationError(f"Unsupported registry format in {file.name}; use a compatible release")

def validate_registries(directory: Path) -> None:
    if directory.is_symlink():
        raise ConfigurationError("Registry directory must not be a symbolic link")
    if directory.is_dir():
        for file in directory.glob("*.json"):
            validate_registry_document(file, read_json(file))
