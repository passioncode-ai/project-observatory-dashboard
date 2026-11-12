"""Versioned per-user configuration. Importing this module does not write files."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

VERSION = "0.35.17"
CONFIG_VERSION = 1
WORKSPACE_VERSION = 1
SOURCE = Path(__file__).resolve().parent


def engine_python() -> str:
    """The interpreter this engine is installed in, whoever started this process.

    Every scheduled job — the tick, the server, the maintenance job — names it, so
    each runs with the packages the engine was installed with. A job started with
    the INSTALLER's interpreter instead ran the tick's backup on Homebrew's bare
    python and died on `No module named 'cryptography'` (a tester, 0.19.1): the
    installer had been started by a python other than the engine's venv (the plugin
    hook may run under any `python3` on PATH). The engine's own virtual environment
    is the ancestor holding `pyvenv.cfg`; a checkout has `.venv`. Its `bin/python`
    is named as it is — never resolved through the symlink to a versioned Homebrew
    path (install_launchd.stable_interpreter)."""
    import sys
    for parent in SOURCE.parents:
        if (parent / "pyvenv.cfg").is_file():
            for name in ("python3", "python"):
                candidate = parent / "bin" / name
                if candidate.exists():
                    return str(candidate)
    checkout = SOURCE / ".venv" / "bin" / "python"
    return str(checkout) if checkout.exists() else sys.executable

class ConfigurationError(RuntimeError):
    pass


def _code_roots(here: Path) -> tuple[Path, ...]:
    """Directories private state must never live in: the shipped code itself.

    The code directory, the installed ``observatory`` package that contains it,
    and the Git work tree of a source checkout. Data there is deleted by an
    upgrade or uninstall, or ends up one ``git add`` away from being published.

    An installed engine (under ``site-packages`` or ``dist-packages``) has no source checkout: the walk for a
    ``.git`` stops there, so a repository that happens to sit above the environment (a home folder someone
    made a repository) never refuses every home under it.
    """
    roots = [here]
    package = here if here.name == "observatory" else here.parent
    if package.name == "observatory" and (package / "__init__.py").is_file():
        roots.append(package)
    for parent in (here, *here.parents):
        if parent.name in ("site-packages", "dist-packages"):
            break
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
    if value:
        return refuse_home_inside_code(Path(value).expanduser())
    import osprivacy   # beside this file; lazily, since everything imports configuration first
    return refuse_home_inside_code(osprivacy.default_home("project-observatory-full"))

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
            raise ConfigurationError("Workspace is not initialized; run project-observatory full init")
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

#: What `updates` in settings.json may hold (maintenance.py). Both default to true when
#: absent: `scheduled` keeps the hourly maintenance job in place. `auto` is how 0.17 and
#: 0.18 turned automatic updates off; since 0.19 the switch is the file `auto-update` in
#: the home (LC-16), a false `auto` is still read as off while that file is absent, and
#: the next `full auto-update` command moves it into the file. An older reader never
#: reads the key.
UPDATE_SETTINGS = ("auto", "scheduled")

#: What `learning_review` in settings.json may hold (learning_review.py, OBS-331), each an hour of the day in UTC:
#: `settle_hour_utc`, the hour of D+1 at which the daily learning review of day D is due (6 when absent). An older
#: reader never reads the section.
LEARNING_REVIEW_SETTINGS = ("settle_hour_utc",)

#: Every switch and source the engine reads. `configure` refuses any other name:
#: a typo used to be answered "configured" and then did nothing. The workspace
#: suite derives the names the code actually reads (`enabled(...)`, `source_path`,
#: the tick's step gates, every `configure ...` command the engine prints) and
#: fails when one is missing here. A metric plugin's `integration:KEY` adds itself.
KNOWN_INTEGRATIONS = frozenset({
    "bitbucket", "cloudflare", "cloudflare_analytics", "digitalocean", "domains", "ga4", "git_remotes",
    "github", "google", "harness", "heroku", "mcp", "openrouter", "remote_env", "search_console",
    "sessions", "wiki"})
KNOWN_FEATURES = frozenset({
    "agent", "auto_cleanup", "companion_remediation", "embeddings", "fixture_cleanup",
    "machine_watch", "notifications", "probe_fixture", "registry_history", "retention",
    "scheduler", "wiki_projection"})
KNOWN_SOURCES = frozenset({
    "cloudflare_snapshot", "companion_db", "companion_home", "domain_export", "gateway_root",
    "harness_root", "mcp_config_root", "projects", "secret_store", "secrets", "sessions", "wiki"})


#: The wallet ceilings `configure budget` sets; each must be above 0 for a model
#: call to be permitted (`providers.check_budget` refuses at `spent >= ceiling`).
BUDGET_SETTINGS = ("daily_ceiling", "monthly_ceiling", "velocity_ceiling")
#: An OpenRouter-style model id: `vendor/model`, optionally `:variant`.
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._:-]+")


#: The step that installs the assistant's key: the value on stdin, from a file.
KEY_STEP = ('python "$(project-observatory full-path)/tools/install_key.py" --for observatory '
            '< key-file')


def model_readiness(base: Path | None = None, key: dict | None = None) -> dict:
    """Whether the agent has a model to ask and a budget to spend, from the
    workspace's own `config/models.json` — no catalogue, no network, no spend.

    A fresh workspace ships `chain: []` and every ceiling at 0.0, so a newcomer
    with a key met "no model in the configured chain" or `budget-reached`, a
    false reason. This names the step that is actually missing."""
    try:
        doc = json.loads(((base or home()) / "config" / "models.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    chain = [e.get("id") for e in doc.get("chain") or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]
    wallet = doc.get("wallet") if isinstance(doc.get("wallet"), dict) else {}
    unset = [k for k in BUDGET_SETTINGS
             if not isinstance(wallet.get(k), (int, float)) or isinstance(wallet.get(k), bool) or wallet.get(k) <= 0]
    # THE KEY IS A STEP TOO. Without `key` (the caller did not ask the
    # provider module) the answer is about the model and the budget alone, as
    # before; with it, a missing or refused key keeps the status off `ready`
    # and puts the install step in `next` — a chain and three ceilings with no
    # key used to read `ready` with nothing left to do.
    keyless = key is not None and key.get("key_status") != "present"
    status = ("no-model" if not chain else "no-budget" if unset
              else "no-key" if keyless else "ready")
    steps = []
    if keyless:
        steps.append(KEY_STEP if key.get("key_status") == "absent"
                     else f"fix the key file it refused ({key.get('key_note', 'mode 600')}), "
                          f"or replace it: {KEY_STEP}")
    if not chain:
        steps.append("project-observatory full configure model chain VENDOR/MODEL[,VENDOR/MODEL...]")
    steps += [f"project-observatory full configure budget {k} AMOUNT" for k in unset]
    out = {"model_configured": bool(chain), "model_status": status, "chain": chain,
           "budget_unset": unset, "next": steps}
    if key is not None:
        out.update({k: key.get(k) for k in ("key_status", "key_source")})
    return out


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
    """The language this workspace builds its pages in: `interface.locale`, else English.
    A reader's browser decides its own (dashboard/i18n.py, L10N-01)."""
    return load(base).get("interface", {}).get("locale", "en")

def load(base: Path | None = None) -> dict:
    base = base or home()
    validate_workspace(base)
    file = base / "config" / "settings.json"
    if file.is_symlink():
        raise ConfigurationError("Settings must not be a symbolic link")
    if not file.exists():
        return _merge_local(base, {"schema_version": CONFIG_VERSION, "sources": {}, "integrations": {}, "features": {}})
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
    # UPDATES is optional and ignored by releases before 0.17.0, which introduced it.
    updates = doc.get("updates", {})
    if not isinstance(updates, dict):
        raise ConfigurationError("Configuration updates must be an object")
    for key, value in updates.items():
        if key not in UPDATE_SETTINGS:
            raise ConfigurationError(f"Unknown updates setting: {key}")
        if type(value) is not bool:
            raise ConfigurationError(f"Updates {key} must be true or false")
    # LEARNING_REVIEW is optional and ignored by releases before OBS-331 (they never read it).
    review = doc.get("learning_review", {})
    if not isinstance(review, dict):
        raise ConfigurationError("Configuration learning_review must be an object")
    for key, value in review.items():
        if key not in LEARNING_REVIEW_SETTINGS:
            raise ConfigurationError(f"Unknown learning_review setting: {key}")
        if type(value) is not int or not 0 <= value <= 23:
            raise ConfigurationError(f"learning_review {key} must be a whole hour from 0 to 23")
    required = doc.get("must_understand", [])
    if not isinstance(required, list) or required:
        raise ConfigurationError("Configuration requires unsupported capabilities")
    return _merge_local(base, doc)

# MACHINE-LOCAL SETTINGS (OBS-353): what this machine and this OS account decide, in one file profiles never carry.
# `config/local.json` holds them; `features.scheduler` and `storage.backups` moved there and are MIRRORED into
# settings.json on every write, so an older engine (a rollback, a second machine on an older release) reads the same
# switch and backups root. `load()` merges the moved keys; everything else is read through `local()`.

LOCAL_SCHEMA = "observatory-local/1"
LOCAL_FILE = "local.json"
#: (section in settings.json, name) of each moved setting, and where it sits in local.json.
LOCAL_MOVED = {("features", "scheduler"): ("scheduler",), ("storage", "backups"): ("storage", "backups")}
#: Keys other parts of the engine own (U1 ports, U4 apps, U2 loopback) or will own (U3 session, V1 context):
#: kept and read, never refused.
LOCAL_OWNED = ("place", "updatedAt", "scheduler", "storage", "ports", "apps", "loopback", "mirrored", "schema")
LOCAL_RESERVED = ("session", "context")
#: What `configure apps location` and `configure loopback peerCheck` accept (U4, U2 b): local.json only.
LOCAL_SECTIONS = {"apps": {"location": ("auto", "user", "system")}, "loopback": {"peerCheck": ("auto", "always")}}
_ABSENT = object()


def local_path(base: Path) -> Path:
    return base / "config" / LOCAL_FILE


def read_local(base: Path) -> tuple[dict | None, list[dict]]:
    """(local.json, notes). None with a note when the file cannot be trusted: a link, not owner-only, not this
    account's, not JSON, or another schema. A missing file is (None, []). Never raises: a bad local file must not stop
    the engine, whose settings.json still holds the mirrored values."""
    file = local_path(base)
    if not file.exists() and not file.is_symlink():
        return None, []
    unreadable = {"code": "local-settings-unreadable",
                  "step": f"move {file} aside; the next write recreates it from config/settings.json"}
    import osprivacy   # beside this file; lazily, since everything imports configuration first

    def mine_and_private(fd: int) -> None:
        if not (osprivacy.owned_by_me(fd) and osprivacy.private(fd)):
            raise ValueError("not owner-only")
    try:
        if any(p.is_symlink() for p in (file, *file.parents)):
            return None, [unreadable]
        doc = json.loads(osprivacy.read_regular(file, 1 << 20, check=mine_and_private).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, [unreadable]
    if not isinstance(doc, dict):
        return None, [unreadable]
    if doc.get("schema") != LOCAL_SCHEMA:
        return None, [{"code": "local-settings-unsupported", "step": "project-observatory full update"}]
    return doc, []


def _local_get(doc: dict, path: tuple):
    value = doc
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _ABSENT
        value = value[key]
    return value


def _valid_moved(key: tuple, value) -> bool:
    if key == ("features", "scheduler"):
        return type(value) is bool
    return isinstance(value, str) and Path(value).expanduser().is_absolute()


def _merge_local(base: Path, doc: dict) -> dict:
    """The moved keys of local.json over settings.json (OBS-353): local.json wins while settings.json still equals the
    value last mirrored into it; a settings.json changed since (an older engine after a rollback, or a hand edit) wins
    and is adopted at the next write. The returned document gains no new key."""
    local_doc, _ = read_local(base)
    if not local_doc:
        return doc
    mirrored = local_doc.get("mirrored") if isinstance(local_doc.get("mirrored"), dict) else {}
    for key, where in LOCAL_MOVED.items():
        value = _local_get(local_doc, where)
        if value is _ABSENT or not _valid_moved(key, value):
            continue
        current = (doc.get(key[0]) or {}).get(key[1], _ABSENT)
        if current == mirrored.get(".".join(key), _ABSENT):
            doc.setdefault(key[0], {})[key[1]] = value
    return doc


def local(base: Path | None = None, *, place: str | None = None) -> dict:
    """What local.json says, for doctor and the parts that own its keys (OBS-353): each setting with its value and
    `source` (`local`, `settings-mirror`, `adopted`, `default`), the recorded and current place, the reserved and
    unknown keys, and the typed notes. `place` is this place's id (default: the machine id, read without creating it)."""
    base = base or home()
    local_doc, notes = read_local(base)
    raw = _raw_settings(base)
    if place is None:
        try:
            import machine_identity
            place = machine_identity.machine_id(create=False)
        except Exception:                                   # an id that cannot be read leaves the place unknown
            place = None
    out: dict = {"file": f"config/{LOCAL_FILE}", "present": local_doc is not None, "schema": LOCAL_SCHEMA,
                 "place": {"recorded": None, "here": place, "matches": None}, "settings": {},
                 "reserved": [], "unknown": [], "notes": list(notes)}
    doc = local_doc or {}
    mirrored = doc.get("mirrored") if isinstance(doc.get("mirrored"), dict) else {}
    recorded = doc.get("place") if isinstance(doc.get("place"), str) else None
    stale = bool(recorded and place and not (recorded == place or place.startswith(recorded + ".")
                                              or recorded.startswith(place + ".")))
    out["place"].update(recorded=recorded, matches=None if not (recorded and place) else not stale)
    if stale:
        out["notes"].append({"code": "local-settings-stale",
                             "step": "project-observatory full configure apps location auto"})
    for key, where in LOCAL_MOVED.items():
        name = ".".join(key)
        value = _local_get(doc, where)
        if value is not _ABSENT and not _valid_moved(key, value):
            value = _ABSENT                                 # a value of the wrong shape is never read
        current = (raw.get(key[0]) or {}).get(key[1], _ABSENT)
        if value is not _ABSENT and current == mirrored.get(name, _ABSENT):
            out["settings"][name] = {"value": value, "source": "local"}
        elif value is not _ABSENT:
            out["settings"][name] = {"value": None if current is _ABSENT else current, "source": "adopted"}
            out["notes"].append({"code": "local-settings-adopted", "setting": name})
        elif current is not _ABSENT:
            out["settings"][name] = {"value": current, "source": "settings-mirror"}
        else:
            out["settings"][name] = {"value": None, "source": "default"}
    ports = doc.get("ports") if isinstance(doc.get("ports"), dict) and not stale else None
    out["settings"]["ports"] = {"value": ports, "source": "local" if ports else "default"}
    apps = _local_get(doc, ("apps", "location"))
    usable = apps in LOCAL_SECTIONS["apps"]["location"] and not stale
    out["settings"]["apps.location"] = {"value": apps if usable else "auto", "source": "local" if usable else "default"}
    peer = _local_get(doc, ("loopback", "peerCheck"))
    usable = peer in LOCAL_SECTIONS["loopback"]["peerCheck"]
    out["settings"]["loopback.peerCheck"] = {"value": peer if usable else "auto",
                                             "source": "local" if usable else "default"}
    out["reserved"] = [k for k in LOCAL_RESERVED if k in doc]
    out["unknown"] = sorted(k for k in doc if k not in LOCAL_OWNED and k not in LOCAL_RESERVED)
    return out


def _raw_settings(base: Path) -> dict:
    """settings.json as written, without the local merge; {} when absent or unreadable."""
    file = base / "config" / "settings.json"
    try:
        return read_json(file) if file.exists() else {}
    except ConfigurationError:
        return {}


def write_local(base: Path, changes: dict, *, place: str | None = None, writer=None) -> dict:
    """Merge `changes` into local.json and mirror the moved keys into settings.json (OBS-353). The CALLER holds the
    workspace lock (`workspace.lock`). Three atomic writes, in this order, so a crash between any two reads right
    under `_merge_local`: (1) local.json with the new values and the OLD `mirrored`; (2) settings.json with the new
    values; (3) local.json with `mirrored` set to them. A first write copies the moved keys from settings.json, so the
    move loses nothing; a settings.json changed since the last mirror is adopted first. `writer(path, doc)` is the
    atomic writer (tests inject a crash). Returns the written local.json."""
    import atomic
    from datetime import datetime, timezone
    write = writer or (lambda path, doc: atomic.write_json(path, doc, indent=2))
    file = local_path(base)
    current, _ = read_local(base)
    raw = _raw_settings(base)
    doc = dict(current) if current else {"schema": LOCAL_SCHEMA}
    old_mirror = dict(doc.get("mirrored")) if isinstance(doc.get("mirrored"), dict) else {}
    for key, where in LOCAL_MOVED.items():            # first write, or settings.json changed since the last mirror
        name, value = ".".join(key), (raw.get(key[0]) or {}).get(key[1], _ABSENT)
        if current is None or value != old_mirror.get(name, _ABSENT):
            if value is _ABSENT:
                _local_pop(doc, where)
            else:
                _local_set(doc, where, value)
            if value is _ABSENT:
                old_mirror.pop(name, None)
            else:
                old_mirror[name] = value
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key] = {**doc[key], **value}
        else:
            doc[key] = value
    if place is None:
        try:
            import machine_identity
            place = machine_identity.machine_id(create=False)
        except Exception:
            place = None
    if place:
        doc["place"] = place
    doc["schema"] = LOCAL_SCHEMA
    doc["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc["mirrored"] = old_mirror
    write(file, doc)                                                       # (1)
    new_mirror = {}
    settings = dict(raw) if raw else {"schema_version": CONFIG_VERSION, "sources": {}, "integrations": {},
                                       "features": {}}
    changed = False
    for key, where in LOCAL_MOVED.items():
        name, value = ".".join(key), _local_get(doc, where)
        if value is _ABSENT:
            continue
        new_mirror[name] = value
        if (settings.get(key[0]) or {}).get(key[1], _ABSENT) != value:
            settings.setdefault(key[0], {})[key[1]] = value
            changed = True
    if changed:
        write(base / "config" / "settings.json", settings)                 # (2)
    if new_mirror != old_mirror:
        doc["mirrored"] = new_mirror
        write(file, doc)                                                   # (3)
    return doc


def _local_set(doc: dict, path: tuple, value) -> None:
    for key in path[:-1]:
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
        doc = doc[key]
    doc[path[-1]] = value


def _local_pop(doc: dict, path: tuple) -> None:
    for key in path[:-1]:
        doc = doc.get(key) if isinstance(doc.get(key), dict) else {}
    doc.pop(path[-1], None)


def source_path(name: str, fallback: str, env: str | None = None) -> Path:
    if env and os.environ.get(env):
        value = Path(os.environ[env]).expanduser()
        if not value.is_absolute():
            raise ConfigurationError(f"{env} must be an absolute path")
        return value
    value = load().get("sources", {}).get(name)
    return Path(value).expanduser() if value else home() / "unconfigured" / fallback

def offline(environ=None) -> bool:
    """`OBSERVATORY_OFFLINE=1`: every integration reads as off for this process (`local` and the demo run so)."""
    return (os.environ if environ is None else environ).get("OBSERVATORY_OFFLINE") == "1"


def enabled(name: str, section: str = "integrations", *, settings: dict | None = None, environ=None) -> bool:
    """Whether `name` is on in `section`. An integration is off while `offline(environ)`, whatever the settings say.
    `settings` is a document `load()` returned (default: load it now); `environ` the process environment (default
    `os.environ`). The collectors and the connections declaration (service_connections.py, OBS-340) both ask here,
    so they cannot disagree about what runs."""
    if section == "integrations" and offline(environ):
        return False
    doc = load() if settings is None else (settings or {})
    return (doc.get(section) or {}).get(name, False) is True

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
