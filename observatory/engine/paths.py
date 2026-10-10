#!/usr/bin/env python3
"""Immutable program resources and explicit per-user state locations."""
from __future__ import annotations
import os
from pathlib import Path
import configuration
import osprivacy

ROOT = Path(__file__).resolve().parent
HOME = configuration.home()
configuration.validate_workspace(HOME)
CONFIG = HOME / "config"
SOURCE_DOCS = ROOT / "docs"
SECRETS = configuration.source_path("secrets", "secrets", "OBSERVATORY_SECRETS") if (os.environ.get("OBSERVATORY_SECRETS") or configuration.load().get("sources", {}).get("secrets")) else HOME / "secrets"

def setting_path(env: str, default: Path) -> Path:
    value = Path(os.environ.get(env, str(default))).expanduser()
    if not value.is_absolute():
        raise configuration.ConfigurationError(f"{env} must be an absolute path")
    return value

def source_path(name: str, default: Path) -> Path:
    value = configuration.load().get("sources", {}).get(name)
    return Path(value).expanduser() if value else default

def config_file(name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise configuration.ConfigurationError("Configuration name must be a filename")
    return CONFIG / name

def config_label(name: str) -> str:
    """How a message names a configuration file: relative to the workspace.

    Operator-facing text used to name the predecessor's `collectors/<name>`, a
    folder an installed engine does not have. Deriving the label from
    `config_file` keeps every message on the one location the engine reads.
    """
    path = config_file(name)
    try:
        return path.relative_to(HOME).as_posix()
    except ValueError:
        return str(path)

REGISTRY = setting_path("OBSERVATORY_REGISTRY", HOME / "registry")
configuration.validate_registries(REGISTRY)
RAW = REGISTRY / "_raw"
STORE = HOME / "store"
SCRATCH = setting_path("OBSERVATORY_SCRATCH", STORE / "raw")
STATE = setting_path("OBSERVATORY_STATE", STORE)


def key_locations(name: str) -> tuple[Path, ...]:
    """Where a local provider key may be: the selected state, then the legacy store.

    One entry when `OBSERVATORY_STATE` is not redirected (the default), two when
    it is. PB-091: keys follow the same state selection as the token and salt.
    """
    selected, legacy = STATE / name, STORE / name
    return (selected,) if selected == legacy else (selected, legacy)


def local_key(name: str) -> tuple[Path, str | None]:
    """(the file to read, a note) for a local provider key; never reads it.

    The selected state wins. A key found only in the legacy store is read with
    a note naming where it should move; nothing is copied or moved. A key in
    BOTH places is refused: choosing one silently could use the older key. A
    key in neither place resolves to the selected path, so the caller's
    "missing" message names where to install it.
    """
    places = key_locations(name)
    if len(places) == 1:
        return places[0], None
    selected, legacy = places
    if selected.exists() and legacy.exists():
        raise configuration.ConfigurationError(
            f"a key named {name} is in both {selected} (the selected state) and {legacy} "
            f"(the legacy store); remove the one you no longer use")
    if legacy.exists():
        return legacy, f"read from the legacy location {legacy}; the selected state expects it at {selected}"
    return selected, None
DOCS = HOME / "docs"
DASHBOARD_HTML = setting_path("OBSERVATORY_DASHBOARD", DOCS / "projects-dashboard.html")
DASHBOARD_DIR = setting_path("OBSERVATORY_DASHBOARD_DIR", DASHBOARD_HTML.parent / "dashboard")
VAULT = configuration.source_path("wiki", "wiki", "OBSERVATORY_VAULT")
VAULT_PROJECTION = VAULT / "inventory"
DATA = configuration.source_path("projects", "projects", "OBSERVATORY_DATA")
NAMECHEAP_EXPORT = configuration.source_path("domain_export", "Domain_List.csv", "OBSERVATORY_DOMAIN_EXPORT")
FINDING_ACKS = setting_path("OBSERVATORY_ACKS", config_file("finding_acks.json"))
COMPANION_DB = configuration.source_path("companion_db", "claude-mem.db", "CLAUDE_MEM_DB")
SESSIONS = configuration.source_path("sessions", "sessions", "OBSERVATORY_SESSIONS")
DB = setting_path("OBSERVATORY_DB", STORE / "observatory.db")

def tighten() -> list[str]:
    """The store's own directories 700, its journals and database 600.

    Everything under `store/` that is not tracked configuration is a map of the
    estate — the ledger, the env fingerprints, which secret which command ran
    with, who revealed what — and until 2026-09-14 the directory was 755, the
    database 644 and every journal 644. None holds a value; all of it
    says where the values are. One mode for the store, applied by whoever
    writes it, so a fresh file cannot arrive looser than its neighbours.

    Returns what it changed, for the caller's log. Never raises: a chmod that
    fails is reported by the next run's check, not by a crash in the writer.
    """
    changed: list[str] = []
    dirs = [STORE, STORE / "logs", SCRATCH, STATE, STATE / "logs"]
    files = [DB, *(STATE / "logs").glob("*.jsonl"), *SCRATCH.rglob("*.json"),
             *SCRATCH.rglob("*.jsonl")]
    for d in dirs:
        try:
            if not any(p.is_symlink() for p in (d, *d.parents)) and d.is_dir() and osprivacy.loose(d, 0o700):
                osprivacy.make_private(d)
                changed.append(str(d))
        except OSError:
            pass
    for f in files:
        try:
            if not any(p.is_symlink() for p in (f, *f.parents)) and f.is_file() and osprivacy.loose(f, 0o600):
                osprivacy.make_private(f)
                changed.append(str(f))
        except OSError:
            pass
    return changed
