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


def sqlite_uri(path, mode: str = "ro") -> str:
    """The `file:` URI for `sqlite3.connect(…, uri=True)`, with the path percent-encoded.

    `f"file:{path}?mode=ro"` let SQLite cut the path at a `?` or `#` in a folder name: the
    `mode=ro` after it was lost, the store opened read-write and created an empty file
    where the cut fell, and the real store was never read. A `%XX` in a name was decoded into
    another path. `Path.as_uri()` encodes all of them (I-5, review round 6).

    THE PATH IS ONLY SPELLED, NEVER LOOKED UP (I-5 polish): `Path.absolute()` prefixes the
    working directory and touches no file. `Path.resolve()` raised RuntimeError on a symlink
    loop on Python 3.11 and 3.12, which no caller catches; a loop now fails inside
    `sqlite3.connect`, as sqlite3.Error. `..` IS KEPT (round 8): `os.path.abspath` collapsed
    `C/lnk/../store.db` to `C/store.db` on paper, while the operating system, like the
    plain-path writer (`store/db.py`), follows `lnk` first and reaches another file. `Path`
    drops only a trailing slash and `.` parts, which name the same file.
    """
    return Path(path).absolute().as_uri() + f"?mode={mode}"


def _env_dir(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(value)).expanduser() if value else default


def transcript_homes(sessions: Path | None = None) -> list[dict]:
    """Where each agent CLI on this machine keeps its session transcripts (I-5).

    Resolved now, from HOME and each tool's own override, never cached: a test or a
    scheduled job with another HOME sees its own homes. Each row:

      agent     the CLI
      home      the folder whose absence means "not installed here" (not a degradation)
      trees     (folder, file suffixes) walked for transcript files
      files     single transcript files (a prompt history)
      stores    SQLite databases that hold transcripts, read as databases
      store_globs  (folder, patterns) for databases whose names vary
      profiles  a folder whose every subfolder is another home with the same layout
      evidence  where the location comes from — the tool's own source or documentation

    Claude Code's transcripts are the configured `sessions` source (`sources.sessions`,
    e.g. `~/.claude/projects`), as before: that location is the operator's choice. Its
    prompt history is `history.jsonl` in its config folder (`CLAUDE_CONFIG_DIR`).

    The variables are read from THIS process's environment — for the tick, the
    scheduler's (launchd, systemd), not an interactive shell's profile.
    """
    user = Path.home()
    claude_sessions = sessions if sessions is not None else SESSIONS
    rows = [{"agent": "claude", "home": claude_sessions, "trees": [(claude_sessions, (".jsonl",))],
             "evidence": "the configured `sessions` source (docs/ONBOARDING.md, sources.sessions)"}]
    claude = _env_dir("CLAUDE_CONFIG_DIR", user / ".claude")
    rows.append({"agent": "claude", "home": claude, "files": [claude / "history.jsonl"],
                 "evidence": "Claude Code's config folder (~/.claude, CLAUDE_CONFIG_DIR) history.jsonl, "
                             "the prompt history: both strings in the Claude Code 2.1 binary"})

    codex = _env_dir("CODEX_HOME", user / ".codex")
    codex_db = _env_dir("CODEX_SQLITE_HOME", codex)
    codex_stores = ("state_*.sqlite", "logs_*.sqlite", "thread_history_*.sqlite", "memories*.sqlite",
                    "queue_*.sqlite", "goals_*.sqlite", "agent_message_board_*.sqlite")
    codex_evidence = ("openai/codex codex-rs/rollout/src/lib.rs SESSIONS_SUBDIR, ARCHIVED_SESSIONS_SUBDIR; "
                      "compression.rs `.jsonl.zst`; codex-rs/state/src/sqlite.rs *_DB_FILENAME and lib.rs "
                      "CODEX_SQLITE_HOME; ext/agent-message-board (agent_message_board_1.sqlite in the "
                      "same SQLite home); history.jsonl; CODEX_HOME")
    rows.append({"agent": "codex", "home": codex,
                 "trees": [(codex / "sessions", (".jsonl", ".jsonl.zst")),
                           (codex / "archived_sessions", (".jsonl", ".jsonl.zst"))],
                 "files": [codex / "history.jsonl"],
                 "store_globs": [(codex_db, codex_stores)] if codex_db == codex else [],
                 "evidence": codex_evidence})
    if codex_db != codex:
        rows.append({"agent": "codex", "home": codex_db, "store_globs": [(codex_db, codex_stores)],
                     "evidence": codex_evidence})

    # Hermes's own default: `~/.hermes` + HERMES_DATA_DIR_SUFFIX (a side-by-side channel).
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        native = (Path(local) if local else user / "AppData" / "Local") / ("hermes" + suffix)
    else:
        native = user / (".hermes" + suffix)
    active = _env_dir("HERMES_HOME", native)
    root = native
    if active != native:
        try:
            active.resolve().relative_to(native.resolve())
        except (OSError, ValueError):
            root = active.parent.parent if active.parent.name == "profiles" else active
    hermes_layout = {"trees": [(Path("sessions"), (".json", ".jsonl"))], "stores": [Path("state.db")]}
    for home in dict.fromkeys((active, root)):
        rows.append({"agent": "hermes", "home": home,
                     "trees": [(home / t, s) for t, s in hermes_layout["trees"]],
                     "stores": [home / s for s in hermes_layout["stores"]],
                     **({"profiles": (root / "profiles", hermes_layout)} if home == root else {}),
                     "evidence": "hermes-agent hermes_constants.py get_hermes_home, get_default_hermes_root, "
                                 "_get_platform_default_hermes_home (HERMES_DATA_DIR_SUFFIX); "
                                 "hermes_state.py DEFAULT_DB_PATH (state.db), sessions/<id>.json|.jsonl"})

    data = _env_dir("XDG_DATA_HOME", user / ".local" / "share") / "opencode"
    chosen = os.environ.get("OPENCODE_DB", "").strip()
    named = [] if not chosen or chosen == ":memory:" else [
        Path(chosen) if Path(chosen).is_absolute() else data / chosen]
    rows.append({"agent": "opencode", "home": data,
                 "trees": [(data / "storage", (".json",))],
                 "stores": named, "store_globs": [(data, ("opencode*.db",))],
                 "evidence": "opencode docs troubleshooting.mdx (~/.local/share/opencode/opencode.db, "
                             "OPENCODE_DB); XDG_DATA_HOME; legacy storage/ JSON tree"})
    return rows

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
