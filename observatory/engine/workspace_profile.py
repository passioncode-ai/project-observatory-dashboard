#!/usr/bin/env python3
"""`full profile`: the portable functional configuration of a workspace.

    full profile export [FILE] [--force]   the profile as JSON (stdout, or a new private file)
    full profile import FILE [--apply]     preview the changes; --apply writes them

Two operators on two machines want the same engine BEHAVIOUR — which integrations
and features are on, the dashboard language, the model chain and budget, the
retention and cleanup policy — while everything that describes a machine or an
estate stays where it is: source paths, the backups root, secrets, the registry,
the store, account ids, credential annotations and project names.

What travels is decided per file, in POLICY_FILES below, with the reason written
next to it, and the export lists every included and excluded item with that
reason. A file that is not in the table is never exported. A file that IS in the
table is still dropped when any value in it is shaped like a path, an email or a
token (`leaks`), so a hand-edited policy file cannot carry one out; the same
check runs over the whole profile before it is written and again on import.

Import never touches `sources` or `storage`, and keeps what is decided per
machine (`features.scheduler`). It preserves every local field the profile does
not name, refuses a profile format it does not know and a profile made by a
newer engine than the one installed (`full update` first), and verifies the
profile's sha256 over its canonical JSON so a damaged or hand-edited file is
refused rather than half-applied. The report names what this machine still has
to connect for the integrations the profile enabled.
"""
from __future__ import annotations
import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

import configuration as config
import workspace

KIND = "project-observatory-profile"
FORMAT_VERSION = 1       # major: an unknown one is refused
FORMAT_MINOR = 0         # minor: additions an older reader may ignore
MAX_BYTES = 4 * 1024 * 1024
SETTINGS_SECTIONS = ("integrations", "features", "interface")
LOG = Path("store") / "logs" / "profile.jsonl"

#: Settings decided by each machine: never exported, never overwritten on import.
MACHINE_LOCAL = {
    ("features", "scheduler"): "whether this machine runs background jobs is decided on the machine "
                               "(`agent install`, the scheduler step), not carried by a profile",
}

#: Every config/ file this release knows, and whether it travels. The rule for
#: `True`: the file holds policy only — thresholds, choices, templates — with no
#: absolute or home-relative path, no credential or reference to one, and no
#: project, repository, account or host of this estate. Anything else stays.
POLICY_FILES: dict[str, tuple[bool, str]] = {
    "activity_tiers.json": (True, "how many idle days make a project cooling, dormant or cold"),
    "cleanup.json": (True, "cleanup thresholds and protected branch patterns"),
    "google_consoles.json": (True, "console URL templates with placeholders, no account ids"),
    "models.json": (True, "model chain, attempts, wallet ceilings and embedding choice; keys never live here"),
    "retention.json": (True, "how long each kind of store record is kept"),
    "settings.json": (False, "exported by section: integrations, features and interface"),
    "credential_annotations.json": (False, "which credential is whose: sensitive and machine-specific"),
    "credential_owners.json": (False, "credential ownership records"),
    "denied_links.json": (False, "links between this estate's projects (inventory)"),
    "verified_links.json": (False, "links between this estate's projects (inventory)"),
    "domain_claims.json": (False, "domains this estate claims (inventory)"),
    "environments.json": (False, "deployments of this estate's apps (inventory and account names)"),
    "finding_acks.json": (False, "acknowledgements of this estate's findings (inventory)"),
    "folder_exclusions.json": (False, "folder names and prefixes under this machine's projects source"),
    "ga4_properties.json": (False, "analytics property ids (account identifiers)"),
    "heroku_links.json": (False, "hosting apps linked to projects (inventory and account identifiers)"),
    "host_boundary.json": (False, "host names of this estate (inventory)"),
    "identity_overrides.json": (False, "project identity overrides (inventory)"),
    "machine.json": (False, "disk locations under this machine's home directory (paths)"),
    "organizations.json": (False, "organizations with their analytics accounts and design teams (account ids)"),
    "ownership.json": (False, "repository owners of this estate (account identities)"),
    "products.json": (False, "products grouping this estate's projects (inventory)"),
    "project_overrides.json": (False, "per-project curation (inventory)"),
    "repo_overrides.json": (False, "per-repository curation (inventory)"),
    "repo_status.json": (False, "per-repository status (inventory)"),
    "session_name_exclusions.json": (False, "agent session names seen on this machine"),
}

#: What a workspace holds outside config/, listed in every export as not carried.
WORKSPACE_EXCLUDED = (
    ("config/settings.json#sources", "absolute paths on this machine"),
    ("config/settings.json#storage", "the backups root, a path on this machine"),
    ("secrets/", "credential values and project slots never leave the machine"),
    ("registry/", "the estate's typed registry (inventory)"),
    ("store/", "database, journals and this workspace's identities"),
    ("backups/", "snapshots and cached releases of this machine"),
    ("docs/", "dashboard pages generated from this estate"),
    ("workspace.json", "this workspace's instance id and format marker"),
)

#: What each integration needs on a machine beyond a source path (sources are
#: reported by workspace.coverage_warnings). Mirrors docs/ONBOARDING.md,
#: "Choose integrations individually".
CONNECT = {
    "github": "authenticate the GitHub CLI on this machine (`gh auth login`)",
    "bitbucket": "a Bitbucket credential in the configured secret store",
    "cloudflare": "Cloudflare account credentials in the configured secret store",
    "heroku": "`heroku login` for the intended account",
    "google": "the service-account files in the configured secret store, with their resource grants",
    "search_console": "the Google service account with Search Console access",
    "cloudflare_analytics": "a Cloudflare analytics token in the configured secret store",
    "domains": "a registrar domain export (`configure sources domain_export PATH`) and network access",
    "openrouter": "an OpenRouter key (`project-observatory full key` says where it is looked for)",
    "remote_env": "provider access for environment reads (the Heroku login)",
    "git_remotes": "Git credentials for the remotes it probes",
}
FEATURE_NEEDS = {
    "agent": "a model key and a budget (`project-observatory full key`, `full wallet`)",
    "embeddings": "an embedding provider key (`project-observatory full key`)",
}

# --- the leak detector ---------------------------------------------------------

DENIED_KEYS = {"sources", "source_path", "storage", "secret", "secrets", "secret_store", "token", "tokens",
               "password", "passphrase", "api_key", "apikey", "key_file", "key_path", "credential",
               "credentials", "annotations", "account", "accounts", "account_id", "email",
               "instance_id", "home", "path", "paths", "projects", "repositories", "organizations",
               "overrides", "owners"}
DENIED_SUFFIXES = ("_path", "_paths", "_dir", "_file", "_token", "_secret", "_password", "_email",
                   "_account", "_accounts", "_account_id", "_api_key")
_ABSOLUTE = re.compile(r"(?:^|[\s\"'=(,;])(?:/(?:[^\s/]+/)*[^\s/]*|[A-Za-z]:[\\/])")
_FILE_URL = re.compile(r"\bfile:", re.IGNORECASE)
_HOME = re.compile(r"(?:^|[\s\"'=(,;])~[\w.-]*(?:/|$)|\$HOME\b|\$\{HOME\}|%USERPROFILE%", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PREFIXED = re.compile(r"(?:sk-|sk_|rk_|pk_live|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|xox[abprs]-|AKIA|ASIA|AIza|"
                       r"glpat-|lin_api_|whsec_)[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{8,}\.|-----BEGIN")
_RUN = re.compile(r"[A-Za-z0-9_+=-]{32,}")


def _string_kind(value: str) -> str | None:
    if _FILE_URL.search(value) or _ABSOLUTE.search(value.replace("://", "\x00")):
        return "absolute-path"
    if _HOME.search(value):
        return "home-path"
    if _EMAIL.search(value):
        return "email"
    if _PREFIXED.search(value):
        return "token"
    for run in _RUN.findall(value):
        if re.search(r"\d", run) and re.search(r"[A-Za-z]", run):
            return "token"
    return None


def leaks(value, pointer: str = "") -> list[tuple[str, str]]:
    """(JSON pointer, kind) for every key or value that must not leave a machine.

    Kinds: denied-key, absolute-path, home-path, email, token. A URL is not a path
    (`https://host/a/b` passes); `file:` URLs and Windows drive paths are."""
    out: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            where = f"{pointer}/{key}"
            name = str(key).lower()
            if name in DENIED_KEYS or name.endswith(DENIED_SUFFIXES):
                out.append((where, "denied-key"))
            kind = _string_kind(str(key))
            if kind:
                out.append((where, kind))
            out.extend(leaks(child, where))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            out.extend(leaks(child, f"{pointer}/{i}"))
    elif isinstance(value, str):
        kind = _string_kind(value)
        if kind:
            out.append((pointer or "/", kind))
    return out


# Prose written for the reader of a config file, never read by the engine: a key
# ending in `_note` or `_source` whose value is a string. When only such fields
# hold a machine path or a token, the field is dropped and the file still travels;
# excluding the whole file for a comment would leave the other machine without
# the policy itself (a model chain with no models). Bare `note` and `why` are not
# on this list: code reads those.
DOC_SUFFIXES = ("_note", "_source")


def _documentation_pointer(doc, pointer: str) -> bool:
    parts = pointer.strip("/").split("/")
    if not parts or not parts[-1].endswith(DOC_SUFFIXES):
        return False
    node = doc
    for part in parts:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False
    return isinstance(node, str)


def _drop(doc, pointer: str) -> None:
    *parents, last = pointer.strip("/").split("/")
    node = doc
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]
    del node[last]


def canonical_sha256(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_name(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is not None


def _where(pointer: str) -> str:
    """A JSON pointer as `a.b.0`: written into a profile, `/a/b` would itself read as a path."""
    return pointer.strip("/").replace("/", ".") or "the top level"


def _describe(kind: str) -> str:
    return {"denied-key": "a key naming a credential, path or inventory"}.get(kind, "a " + kind.replace("-", " "))


class ProfileError(RuntimeError):
    pass


def _log(base: Path, event: str, **fields) -> None:
    """Profile events in store/logs/profile.jsonl: what left or changed, never a value."""
    try:
        file = base / LOG
        workspace.reject_symlinks(file)
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(json.dumps({"at": stamp, "event": event, **fields}, ensure_ascii=False) + "\n")
    except (OSError, config.ConfigurationError):
        pass


# --- export ------------------------------------------------------------------------

def build(base: Path) -> dict:
    config.validate_workspace(base, required=True)
    settings = config.load(base)
    included, excluded = [], [{"item": item, "reason": reason} for item, reason in WORKSPACE_EXCLUDED]
    body_settings: dict[str, dict] = {}
    for section in SETTINGS_SECTIONS:
        values = settings.get(section) or {}
        kept = {}
        for key, value in values.items():
            if (section, key) in MACHINE_LOCAL:
                excluded.append({"item": f"config/settings.json#{section}.{key}", "reason": MACHINE_LOCAL[(section, key)]})
            elif _safe_name(key) and not leaks({key: value}):
                kept[key] = value
            else:
                excluded.append({"item": f"config/settings.json#{section}", "reason": "an entry with an unexpected name or value was left out"})
        body_settings[section] = kept
        included.append({"item": f"config/settings.json#{section}",
                         "reason": {"integrations": "which integrations are on",
                                    "features": "which features are on",
                                    "interface": "the dashboard language"}[section]})
    extra = [k for k in settings if k not in (*SETTINGS_SECTIONS, "sources", "storage", "schema_version", "must_understand")]
    if extra:
        excluded.append({"item": "config/settings.json#other", "reason": f"{len(extra)} field(s) this release does not classify stay local"})
    files: dict[str, dict] = {}
    unclassified = 0
    for path in sorted((base / "config").glob("*.json")):
        rule = POLICY_FILES.get(path.name)
        if rule is None:
            unclassified += 1
            continue
        include, reason = rule
        if path.name == "settings.json":
            continue
        if not include:
            excluded.append({"item": f"config/{path.name}", "reason": reason})
            continue
        doc = config.read_json(path)
        found = leaks(doc)
        if found and all(_documentation_pointer(doc, where) for where, _ in found):
            for where, kind in sorted(found, reverse=True):
                _drop(doc, where)
                excluded.append({"item": f"config/{path.name}#{_where(where)}",
                                 "reason": f"a documentation field holding {_describe(kind)}; "
                                           "the field stays on this machine, the file travels"})
            found = leaks(doc)
        if found:
            where, kind = found[0]
            excluded.append({"item": f"config/{path.name}",
                             "reason": f"held {_describe(kind)} at {_where(where)}; exported files carry none"})
            continue
        files[path.name] = doc
        included.append({"item": f"config/{path.name}", "reason": reason})
    if unclassified:
        excluded.append({"item": "config/*.json", "reason": f"{unclassified} unclassified file(s): only files this "
                                                            "release classifies as policy are exported"})
    doc = {"kind": KIND, "format_version": FORMAT_VERSION, "format_minor": FORMAT_MINOR,
           "engine_version": config.VERSION,
           "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "must_understand": [], "settings": body_settings, "files": files,
           "included": included, "excluded": excluded}
    found = leaks(doc)
    if found:  # a defect in this module, never a user error: refuse rather than publish
        raise ProfileError(f"Export refused: {_describe(found[0][1])} at {_where(found[0][0])} survived the file rules")
    doc["sha256"] = canonical_sha256(doc)
    return doc


def export(base: Path, target: Path | None, force: bool) -> dict:
    doc = build(base)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if target is None:
        _log(base, "exported", sha256=doc["sha256"], files=sorted(doc["files"]), to="stdout")
        return doc
    # The output is a file the user names, outside the workspace: a linked folder on
    # the way (`/tmp` on macOS) is resolved, but the file itself must not be a link
    # (the exclusive create below also refuses to follow one).
    target = target.expanduser().absolute()
    target = target.parent.resolve() / target.name
    if target.is_symlink():
        raise config.ConfigurationError(f"{target} is a symbolic link; name a new file for the profile")
    if force:
        import atomic
        atomic.write_text(target, text)
        os.chmod(target, 0o600)
    else:
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise ProfileError(f"{target.name} already exists; choose a new file or pass --force") from None
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
    _log(base, "exported", sha256=doc["sha256"], files=sorted(doc["files"]), to="file")
    return {"status": "exported", "file": str(target), "sha256": doc["sha256"],
            "included": len(doc["included"]), "excluded": len(doc["excluded"])}


# --- import ------------------------------------------------------------------------

KNOWN_FIELDS = {"kind", "format_version", "format_minor", "engine_version", "created_at", "must_understand",
                "settings", "files", "included", "excluded", "sha256"}


def read_profile(path: Path) -> tuple[dict, list[str]]:
    """The validated profile, and the names of the parts this release ignores."""
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ProfileError("Profile must be a regular file")
    if path.stat().st_size > MAX_BYTES:
        raise ProfileError("Profile is larger than any this release writes")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ProfileError("Profile is not readable JSON") from None
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        raise ProfileError("Not a Project Observatory profile")
    if type(doc.get("format_version")) is not int or doc["format_version"] != FORMAT_VERSION:
        raise ProfileError(f"Unknown profile format {doc.get('format_version')!r}; this release reads format {FORMAT_VERSION}")
    required = doc.get("must_understand", [])
    if not isinstance(required, list) or required:
        raise ProfileError("Profile requires capabilities this release does not have; `full update` first")
    try:
        made_by = config.version_tuple(doc.get("engine_version"))
    except config.ConfigurationError:
        raise ProfileError("Profile does not say which engine made it") from None
    if made_by > config.version_tuple(config.VERSION):
        raise ProfileError(f"Profile was made by engine {doc['engine_version']}, this is {config.VERSION}; run "
                           f"`project-observatory full update --version {doc['engine_version']} --apply` first")
    if not isinstance(doc.get("sha256"), str) or doc["sha256"] != canonical_sha256(doc):
        raise ProfileError("Profile sha256 does not match its content: it was edited or damaged; export it again")
    found = leaks({k: v for k, v in doc.items() if k != "sha256"})
    if found:
        raise ProfileError(f"Profile carries {_describe(found[0][1])} at {_where(found[0][0])}; a profile holds no machine "
                           "state, so it is refused")
    ignored = [k for k in doc if k not in KNOWN_FIELDS]
    settings, files = doc.get("settings"), doc.get("files")
    if not isinstance(settings, dict) or not isinstance(files, dict):
        raise ProfileError("Profile settings and files must be objects")
    for section in list(settings):
        if section not in SETTINGS_SECTIONS:
            ignored.append(f"settings.{section}")
            continue
        values = settings[section]
        if not isinstance(values, dict) or not all(_safe_name(k) for k in values):
            raise ProfileError(f"Profile settings.{section} must map setting names to values")
        if section != "interface" and any(type(v) is not bool for v in values.values()):
            raise ProfileError(f"Profile settings.{section} values must be true or false")
        for key in list(values):
            if (section, key) in MACHINE_LOCAL:
                ignored.append(f"settings.{section}.{key}")
    for name in list(files):
        if not POLICY_FILES.get(name, (False, ""))[0]:
            ignored.append(f"files.{name}")
        elif not isinstance(files[name], dict):
            raise ProfileError(f"Profile file {name} must be an object")
    return doc, ignored


def merge(local, incoming):
    """Profile values win; keys only the local file has are kept (unknown optional fields)."""
    if isinstance(local, dict) and isinstance(incoming, dict):
        out = dict(local)
        for key, value in incoming.items():
            out[key] = merge(local[key], value) if key in local else copy.deepcopy(value)
        return out
    return copy.deepcopy(incoming)


def diff(old, new, file: str, pointer: str = "") -> list[dict]:
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new), key=str):
            where = f"{pointer}/{key}"
            if key not in old:
                if isinstance(new[key], dict) and new[key]:
                    out.extend(diff({}, new[key], file, where))
                else:
                    out.append({"file": file, "pointer": where, "change": "add", "to": new[key]})
            elif key not in new:
                out.append({"file": file, "pointer": where, "change": "remove", "from": old[key]})
            else:
                out.extend(diff(old[key], new[key], file, where))
        return out
    return [] if old == new else [{"file": file, "pointer": pointer or "/", "change": "change", "from": old, "to": new}]


def plan(base: Path, doc: dict) -> tuple[dict[Path, dict], dict, list[dict]]:
    """(files to write, the resulting settings, the changes) — reads only."""
    local = config.load(base)
    settings = copy.deepcopy(local)
    for section in SETTINGS_SECTIONS:
        incoming = {k: v for k, v in (doc["settings"].get(section) or {}).items() if (section, k) not in MACHINE_LOCAL}
        kept = {k: v for k, v in (local.get(section) or {}).items() if (section, k) in MACHINE_LOCAL}
        value = {**incoming, **kept}
        if value or section in local or section != "interface":
            settings[section] = value
    writes: dict[Path, dict] = {}
    changes = diff(local, settings, "config/settings.json")
    if changes:
        writes[base / "config/settings.json"] = settings
    for name in sorted(doc["files"]):
        if not POLICY_FILES.get(name, (False, ""))[0]:
            continue
        target = base / "config" / name
        current = config.read_json(target) if target.exists() else {}
        merged = merge(current, doc["files"][name])
        found = diff(current, merged, f"config/{name}")
        if found:
            writes[target] = merged
            changes.extend(found)
    return writes, settings, changes


def validate_settings(base: Path, settings: dict) -> None:
    """The engine's own reader, run on the would-be settings in a scratch copy."""
    with tempfile.TemporaryDirectory(prefix="observatory-profile-") as tmp:
        scratch = Path(tmp).resolve()   # a symlinked temp root would fail the reader's own check
        shutil.copyfile(base / "workspace.json", scratch / "workspace.json")
        (scratch / "config").mkdir(mode=0o700)
        (scratch / "config/settings.json").write_text(json.dumps(settings), encoding="utf-8")
        config.load(scratch)


def _write(path: Path, doc: dict) -> None:
    import atomic
    atomic.write_json(path, doc, indent=2)


def _restore(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    import atomic
    atomic.write_text(path, data.decode("utf-8"))
    os.chmod(path, 0o600)


def connect_report(settings: dict) -> dict:
    enabled = sorted(k for k, v in (settings.get("integrations") or {}).items() if v is True)
    features = sorted(k for k, v in (settings.get("features") or {}).items() if v is True)
    sourced = {name for (section, name) in workspace.SOURCE_NEEDS if section == "integrations"}
    return {"enabled_integrations": enabled,
            "coverage_warnings": workspace.coverage_warnings(settings),
            "credentials": [{"integration": name, "connect": CONNECT[name]} for name in enabled if name in CONNECT]
                           + [{"integration": name, "connect": "see docs/ONBOARDING.md, 'Choose integrations individually'"}
                              for name in enabled if name not in CONNECT and name not in sourced],
            "features": [{"feature": name, "needs": FEATURE_NEEDS[name]} for name in features if name in FEATURE_NEEDS],
            "note": "a profile never carries credentials or source paths; connect them on this machine"}


def import_profile(base: Path, path: Path, apply: bool) -> dict:
    config.validate_workspace(base, required=True)
    doc, ignored = read_profile(path)
    writes, settings, changes = plan(base, doc)
    try:
        validate_settings(base, settings)
    except config.ConfigurationError as exc:
        raise ProfileError(f"This release would refuse the resulting settings: {exc}") from None
    result = {"status": "preview", "from_engine": doc["engine_version"], "profile_sha256": doc["sha256"],
              "changes": changes, "files_changed": sorted(str(p.relative_to(base)) for p in writes),
              "ignored": ignored, "sources_touched": False, "connect_on_this_machine": connect_report(settings)}
    locale_changed = any(c["pointer"].startswith("/interface") for c in changes if c["file"] == "config/settings.json")
    nxt = ["project-observatory full doctor"]
    if locale_changed:
        nxt.append("project-observatory full open --rebuild")
    if not apply:
        result["next"] = (["project-observatory full profile import FILE --apply"] if changes else []) + nxt
        return result
    with workspace.lock(base):
        writes, settings, changes = plan(base, doc)   # again, under the lock: the files may have moved
        validate_settings(base, settings)
        originals = {p: (p.read_bytes() if p.exists() else None) for p in writes}
        try:
            for target, value in writes.items():
                _write(target, value)
            config.load(base)
            for target in writes:
                config.read_json(target)
        except BaseException:
            for target, data in originals.items():
                _restore(target, data)
            raise
    _log(base, "imported", profile_sha256=doc["sha256"], from_engine=doc["engine_version"],
         files=sorted(str(p.relative_to(base)) for p in writes), changes=len(changes))
    result.update(status="imported" if writes else "unchanged", changes=changes,
                  files_changed=sorted(str(p.relative_to(base)) for p in writes),
                  connect_on_this_machine=connect_report(settings), next=nxt)
    return result


def parser() -> argparse.ArgumentParser:
    """Shared by `main` and the gate's parse-only check (`workspace.parse`)."""
    ap = argparse.ArgumentParser(prog="project-observatory full profile", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="action", required=True)
    exp = sub.add_parser("export", help="print the profile, or write it to a new private FILE")
    exp.add_argument("file", nargs="?", type=Path)
    exp.add_argument("--force", action="store_true", help="replace FILE if it exists")
    imp = sub.add_parser("import", help="preview a profile's changes; --apply writes them")
    imp.add_argument("file", type=Path)
    imp.add_argument("--apply", action="store_true")
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    try:
        base = config.home()
        result = export(base, a.file, a.force) if a.action == "export" else import_profile(base, a.file, a.apply)
    except (ProfileError, config.ConfigurationError, OSError, ValueError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
