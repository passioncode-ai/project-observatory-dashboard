"""The configuration door (OBS-563, CP-20): one place that previews, applies and undoes a setting.

WHY ONE DOOR. The engine's settings live in four files of the workspace's `config/` (settings.json, local.json,
models.json, cleanup.json), eight more files of their own (meter.json, retention.json, machine.json,
review-policy.json, machine-backup.json, relay.json, tracked.json, organizations.json, OBS-612) and two one-word switch
files in the workspace home (`auto-update`, `analytics`), and each grew its own writer: `configure`, the disk guard's thresholds by hand, the modules
of the control plane that will switch watches and apply fixes. A writer of its own has its own idea of a valid value,
writes without a preview and leaves nothing to undo. Here every setting is a declared entry (`catalogue()`) with its
type, its allowed values or range, its default, its file and its gate, and every change goes the same way:

    plan(changes)          the values before and after and what each change does; writes no configuration, keeps the
                           plan under its digest (store/raw/configure-plans/) for apply
    apply(digest)          writes exactly that plan, atomically, under the workspace lock, only while every file it
                           touches still holds what the plan read (else `plan_stale`), and journals the values before
                           and after with the actor (store/logs/configure.jsonl)
    undo(journal_id)       restores the values before, only while the files still hold what that apply wrote

THE GATE. A change is `safe` when it is reversible and changes only the engine's own behaviour; it needs the PERSON
when it sends data out, deletes sooner, spends more or points at secrets (`person`: on, set, raise, lower, always,
values). The person at a terminal (`person:cli`) or at the Disk page the key server serves with its token
(`person:page`, OBS-562) passes it; an agent (`agent:...`) passes it with the operator's words in contract DEC-0121's
shape (`decision`), checked as `node/actions.py` checks them.

NEVER A SECRET. A key or value shaped like a credential (`credential_shape`) is refused before anything is kept, and a
refusal never echoes what it refused. Credentials go to the vault, never into configuration.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import configuration as config
import credential_shape

SCHEMA = "configure/1"
JOURNAL = "configure.jsonl"
PLANS = "configure-plans"
#: How long a plan waits for its apply.
PLAN_SECONDS = 24 * 3600
FILES = {"settings": "config/settings.json", "local": "config/local.json", "models": "config/models.json",
         "cleanup": "config/cleanup.json",
         # OBS-612: the settings that had a file of their own, in the same files and formats as before.
         "auto-update": "auto-update", "analytics": "analytics", "meter": "config/meter.json",
         "retention": "config/retention.json", "machine": "config/machine.json",
         "review": "config/review-policy.json", "machine-backup": "config/machine-backup.json",
         "relay": "config/relay.json", "tracked": "config/tracked.json",
         "organizations": "config/organizations.json"}
#: The two switches that are a one-word text file in the workspace home (LC-16): absent is on, only `off` is off.
SWITCH_FILES = ("auto-update", "analytics")
#: The other JSON files the door reads and writes beside the four it began with.
OWN_FILES = ("meter", "retention", "machine", "review", "machine-backup", "relay", "tracked", "organizations")
#: Files whose readers read every key (or refuse a rule without its `on`): back to the default writes the default,
#: never removes the key.
FILLED_FILES = ("retention", "machine", "review", "organizations")
#: How long a text setting may be.
TEXT_LIMIT = 500
#: How large a structured setting may be, as JSON.
JSON_LIMIT = 64 * 1024
ACTIONS = {"apply": ("configure.apply", "Apply a configuration change"),
           "undo": ("configure.undo", "Undo a configuration change")}
JOURNAL_ID = re.compile(r"cfg_[0-9a-f]{16}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ACTOR = re.compile(r"(person:(cli|page)|(agent|service):[A-Za-z0-9][A-Za-z0-9._+-]{0,63})")
ABSENT = config._ABSENT

#: The clock; tests replace it.
CLOCK = lambda: datetime.datetime.now(datetime.timezone.utc)  # noqa: E731


def _now() -> str:
    return CLOCK().replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Refusal(ValueError):
    """A change this door does not make, by type, with the keys it concerns. Never carries a refused value."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        import engine_node
        # One spelling of the decision codes (OBS-611): the earlier `operator_decision_required` is read as the
        # contract's `operator-decision-required`, and only the contract's is answered.
        self.code = engine_node.module("actions").refusal_code(code)
        self.extra = extra

    def answer(self) -> dict:
        return {"error": self.code, "message": str(self), **self.extra, "degraded": []}


# --- the catalogue ----------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    key: str
    kind: str                 # bool | enum | int | number | path | models | text | json
    file: str                 # a key of FILES
    where: tuple              # the path inside that file
    default: Any
    description: str
    person: str = "never"     # never | always | on | off | set | raise | lower | values
    person_values: tuple = ()
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    shape: str = ""           # json: list | object

    def row(self) -> dict:
        out = {"key": self.key, "type": self.kind, "default": self.default, "file": FILES[self.file],
               "description": self.description, "gate": {"person": self.person}}
        if self.person_values:
            out["gate"]["values"] = list(self.person_values)
        if self.choices:
            out["choices"] = list(self.choices)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.shape:
            out["shape"] = self.shape
        if self.file == "settings" and (self.where[0], self.where[-1]) in config.LOCAL_MOVED:
            out["file"] = FILES["local"]       # decided per machine (OBS-353), mirrored into settings.json
        return out


#: Integrations that read only this machine's files: turning them on sends nothing out.
LOCAL_INTEGRATIONS = frozenset({"harness", "key_sweep", "sessions", "wiki"})
#: Features that change only the engine's own behaviour; the others delete, spend or send.
SAFE_FEATURES = frozenset({"machine_watch", "notifications", "probe_fixture", "registry_history", "scheduler",
                           "wiki_projection"})
#: Sources that point the engine at secrets: the person decides where.
SECRET_SOURCES = frozenset({"secret_store", "secrets"})

FEATURE_TEXT = {
    "auto_cleanup": "the disk guard and the cleanup's auto tier remove what loses nothing, from the maintenance job",
    "mcp_remote_scanners": "third-party remote scanners of MCP tools: they are sent each tool's name, description "
                           "and schema, so this stays off until you turn it on",
}
INTEGRATION_TEXT = {
    "vulnerabilities": "the dependency watch: public package names and versions go to OSV; local ones never leave",
    "github_issues": "file findings as issues in the project's own repository",
    "github_issues_dependencies": "the issue filer's class: vulnerable dependencies (needs github_issues)",
    "github_issues_integrations": "the issue filer's class: refused provider keys, drifted provider answers and "
                                  "providers nothing watches (needs github_issues)",
    "github_issues_keys": "the issue filer's class: findings about keys and secrets, never into a public repository "
                          "(needs github_issues)",
    "key_sweep": "the key sweep: the estate's repositories read for our own values and key-shaped text; "
                 "only keyed digests and places are kept, and nothing is sent",
}

#: The disk guard's thresholds (tools/disk_guard.py) and the cleanup's auto tier (tools/cleanup.py): lower days or
#: higher free-space lines make it remove sooner, so those directions need the person.
GUARD_KEYS = (
    (("levels", "low", "free_gb"), "number", "raise", "free space (GB) below which the guard acts at the low level"),
    (("levels", "critical", "free_gb"), "number", "raise",
     "free space (GB) below which the guard acts at the critical level"),
    *((("temp_days", level), "int", "lower", f"days a temporary file is kept at the {level} level")
      for level in ("normal", "low", "critical")),
    *((("build_idle_days", level), "int", "lower",
       f"days a checkout is idle before its build output is removed at the {level} level")
      for level in ("normal", "low", "critical")),
    *((("trash_days", level), "int", "lower", f"days an item stays in the Trash at the {level} level")
      for level in ("normal", "low", "critical")),
    (("clone_min_age_hours",), "int", "lower", "hours an abandoned browser clone is kept before removal"),
    (("user_temp_hours",), "int", "lower",
     "hours an entry of the user's temporary folder is kept before removal (0 switches it off)"),
    # Thresholds of the `disk.tmp_growth` finding: they remove nothing, so no direction needs the person.
    (("tmp_growth", "entries_per_day"), "int", "never",
     "new entries a day in one name class of the temporary folder above which it is a finding"),
    (("tmp_growth", "gb_per_day"), "number", "never",
     "gigabytes a day written in one name class of the temporary folder above which it is a finding"),
)
CLEANUP_AUTO = {
    "merged_branch_idle_days": "days a merged branch is idle before the auto tier removes it",
    "pushed_branch_idle_days": "days a pushed branch is idle before the auto tier removes it",
    "clean_worktree_idle_days": "days a clean worktree is idle before the auto tier removes it",
    "build_artifacts_project_idle_days": "days a project is idle before the auto tier removes its build output",
}


#: The keys the Disk page shows and changes (OBS-562, CP-16): the auto cleanup switch, the disk guard's thresholds
#: and the cleanup's auto tier. The page's server route changes nothing else.
DISK_SCOPE = ("features.auto_cleanup", "guard.", "cleanup.auto.")


def in_disk_scope(key: Any) -> bool:
    return isinstance(key, str) and any(key == p or (p.endswith(".") and key.startswith(p)) for p in DISK_SCOPE)


def _shipped(name: str) -> dict:
    try:
        doc = json.loads((config.SOURCE / "defaults" / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _dig(doc: Any, path: tuple) -> Any:
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return ABSENT
        doc = doc[key]
    return doc


def catalogue() -> dict[str, Setting]:
    """Every settable key, by key. Built from the engine's own lists, so a new switch is settable the day it ships."""
    rows: list[Setting] = []
    for name in sorted(config.known_names("integrations")):
        on = name in config.DEFAULT_ON.get("integrations", frozenset())
        rows.append(Setting(f"integrations.{name}", "bool", "settings", ("integrations", name), on,
                            INTEGRATION_TEXT.get(name, f"the {name} integration: its collector runs at the next pass"),
                            "never" if name in LOCAL_INTEGRATIONS else "on"))
    for name in sorted(config.known_names("features")):
        text = FEATURE_TEXT.get(name, f"the {name} feature")
        rows.append(Setting(f"features.{name}", "bool", "settings", ("features", name), False, text,
                            "never" if name in SAFE_FEATURES else "on"))
    for name in sorted(config.known_names("sources")):
        rows.append(Setting(f"sources.{name}", "path", "settings", ("sources", name), None,
                            f"the folder or file the {name} source is read from (absolute)",
                            "set" if name in SECRET_SOURCES else "never"))
    for name, choices in config.INTERFACE_SETTINGS.items():
        rows.append(Setting(f"interface.{name}", "enum", "settings", ("interface", name), choices[0],
                            "the language the pages are built in", choices=choices))
    rows.append(Setting("storage.backups", "path", "settings", ("storage", "backups"), None,
                        "the root encrypted backups are written to (absolute)", "set"))
    rows.append(Setting("learning_review.settle_hour_utc", "int", "settings", ("learning_review", "settle_hour_utc"), 6,
                        "the UTC hour of the next day the learning review is due", minimum=0, maximum=23))
    for section, names in config.LOCAL_SECTIONS.items():
        for name, choices in names.items():
            text = ("where the native apps are installed" if section == "apps"
                    else "when the servers check who is on the other end of a loopback connection")
            rows.append(Setting(f"{section}.{name}", "enum", "local", (section, name), choices[0], text,
                                "values" if section == "loopback" else "never",
                                ("auto",) if section == "loopback" else (), choices=choices))
    models = _shipped("models.json")
    rows.append(Setting("model.chain", "models", "models", ("chain",),
                        [e.get("id") for e in models.get("chain") or [] if isinstance(e, dict)],
                        "the assistant's model chain (vendor/model ids), first tried first", "always"))
    wallet = models.get("wallet") or {}
    for name in config.BUDGET_SETTINGS:
        rows.append(Setting(f"budget.{name}", "number", "models", ("wallet", name), wallet.get(name, 0.0),
                            f"the wallet's {name.replace('_', ' ')}, in its denomination", "raise", minimum=0))
    cleanup = _shipped("cleanup.json")
    for path, kind, rule, text in GUARD_KEYS:
        rows.append(Setting("guard." + ".".join(path), kind, "cleanup", ("guard", *path),
                            _dig(cleanup.get("guard") or {}, path), "the disk guard: " + text, rule, minimum=0,
                            maximum=10000 if kind == "number" else 3650 if "days" in path[0]
                            else 10_000_000 if path[0] == "tmp_growth" else 8760))
    for name, text in CLEANUP_AUTO.items():
        rows.append(Setting(f"cleanup.auto.{name}", "int", "cleanup", ("auto", name),
                            (cleanup.get("auto") or {}).get(name), "the cleanup: " + text, "lower", minimum=0,
                            maximum=3650))
    rows += _own_file_rows()
    return {row.key: row for row in rows}


#: The meter's fields (config/meter.json, `meter.py`): the trusted issuer and where the estate's keys and grants are
#: are the person's (they decide whose tokens the meter believes); a raised limit spends more.
METER_KEYS = (
    ("issuer", "text", None, "set", "the estate issuer whose tokens the meter trusts (urn:fabric:estate:<id>)"),
    ("hubJson", "path", None, "set", "the hub's hub.json: its issuer, keys and grant projection"),
    ("jwks", "path", None, "set", "a JWK Set file of the estate's keys (overrides hub.json's)"),
    ("grantsDir", "path", None, "set", "the hub's grant projection folder (overrides hub.json's)"),
    ("estateMonthUsd", "number", 1000.0, "raise", "the estate's monthly spend limit across every key, in USD"),
    ("alertRatio", "number", 0.8, "never", "the share of the monthly limit at which a warning is raised"),
    ("reservationSeconds", "int", 3600, "never", "how long a reservation is held before it lapses"),
    ("keyCeilingsUsd", "json", None, "always", "each provider key's monthly ceiling in USD, by its reference"),
)
#: retention.json's numbers (`store/retention.py`): fewer days or fewer kept removes sooner.
RETENTION_DAYS = ("events_days", "observations_days", "metrics_days", "wallet_days", "idempotency_days",
                  "migration_backups_days")
RETENTION_COUNTS = ("fingerprints_keep", "metrics_keep_per_series", "wallet_events", "wallet_months",
                    "migration_backups_keep")
RETENTION_LEDGER_DAYS = ("proposed_days", "observed_days", "rejected_days", "checkpoint_closed_days", "handoff_days")
#: machine.json's thresholds (`collectors/scan_machine.py`): they size and raise findings; nothing is removed.
MACHINE_KEYS = (
    ("every_hours", "int", 720, "hours between two measurements of the disk locations"),
    ("disk_budget_seconds", "int", 86400, "seconds the tick's disk measurement may take"),
    ("disk_budget_seconds_manual", "int", 86400, "seconds a person's disk measurement may take"),
    ("disk_location_timeout_seconds", "int", 86400, "seconds one location's measurement may take"),
    ("private_every_hours", "int", 8760, "hours between two walks for a location's private size (macOS)"),
    ("free_space_warning_percent", "number", 100, "free space (percent) below which the disk is a warning"),
    ("free_space_critical_percent", "number", 100, "free space (percent) below which the disk is critical"),
    ("memory_group_warning_mb", "int", 10_000_000, "memory (MB) one process group may hold before a warning"),
)


def _own_file_rows() -> list[Setting]:
    """The settings that lived in a file of their own (OBS-612), declared where they are kept."""
    import machine_backup
    import review_policy
    rows = [
        Setting("updates.auto", "bool", "auto-update", ("switch",), True,
                "automatic updates: the maintenance job installs a newer signed release by itself (the file "
                "`auto-update`; while it is off a newer version is announced, never installed)", "always"),
        Setting("analytics.send", "bool", "analytics", ("switch",), True,
                "anonymous usage and error data sent to help improve the engine (the file `analytics`; off "
                "also deletes the install id and the queue)", "on"),
    ]
    for name, kind, default, person, text in METER_KEYS:
        rows.append(Setting(f"meter.{name}", kind, "meter", (name,), default, "the meter: " + text, person,
                            minimum=(1 if name == "reservationSeconds" else 0) if kind in ("int", "number") else None,
                            maximum={"alertRatio": 1, "reservationSeconds": 86400}.get(name, 1_000_000)
                            if kind in ("int", "number") else None, shape="object" if kind == "json" else ""))
    rows.append(Setting("meter.slotsRequireGrant", "enum", "meter", ("slotsRequireGrant",), "when-token",
                        "the meter: whether a vault slot is released only under a grant", "values", ("off",),
                        choices=("when-token", "always", "off")))
    rows.append(Setting("meter.keyserverRequiresDelegation", "enum", "meter", ("keyserverRequiresDelegation",),
                        "off", "the meter: whether the key server asks a caller other than the page for a token",
                        choices=("off", "agents")))
    shipped = _shipped("retention.json")
    ledger = shipped.get("ledger") or {}
    for name in RETENTION_DAYS + RETENTION_COUNTS:
        rows.append(Setting(f"retention.{name}", "int", "retention", (name,), shipped.get(name),
                            f"how long the store keeps it: {name.replace('_', ' ')}", "lower", minimum=1,
                            maximum=36500))
    for name in RETENTION_LEDGER_DAYS:
        rows.append(Setting(f"retention.ledger.{name}", "int", "retention", ("ledger", name), ledger.get(name),
                            f"the ledger's {name.replace('_', ' ')}", "lower", minimum=1, maximum=36500))
    for name, shape, text in (("never", "list", "the record states retention never erases"),
                              ("owner_exempt", "list", "the owners whose records retention never erases"),
                              ("kinds", "object", "the horizons of one kind of record, by kind")):
        rows.append(Setting(f"retention.ledger.{name}", "json", "retention", ("ledger", name), ledger.get(name),
                            "the ledger: " + text, "always", shape=shape))
    machine = _shipped("machine.json")
    for name, kind, top, text in MACHINE_KEYS:
        rows.append(Setting(f"machine.{name}", kind, "machine", (name,), machine.get(name), "the machine watch: "
                            + text, minimum=0 if kind == "number" else 1, maximum=top))
    rows.append(Setting("machine.locations", "json", "machine", ("locations",), machine.get("locations"),
                        "the machine watch: where disk goes outside the projects folder, and what each place is",
                        shape="list"))
    for rid, spec in review_policy.RULES.items():
        rows.append(Setting(f"review.rules.{rid}", "bool", "review", ("rules", rid, "on"), True,
                            f"the review policy's rule {rid}: {spec['decides']} (applied once the policy is "
                            "enabled at a terminal)", "on"))
    for name, top in (("count", 100_000), ("age_days", 3650)):
        rows.append(Setting(f"review.remind.{name}", "int", "review", ("remind", name),
                            review_policy.REMIND_DEFAULT[name],
                            f"the review queue reminds a person past this {name.replace('_', ' ')}", minimum=1,
                            maximum=top))
    rows += [
        Setting("machine_backup.enabled", "bool", "machine-backup", ("enabled",), True,
                "the daily machine backup of the agents' setup", "off"),
        Setting("machine_backup.transcripts", "bool", "machine-backup", ("transcripts",), False,
                "the machine backup also carries the agents' transcripts", "on"),
        Setting("machine_backup.include", "json", "machine-backup", ("include",), [],
                "the machine backup: more files and folders it carries", "set", shape="list"),
        Setting("machine_backup.exclude", "json", "machine-backup", ("exclude",), [],
                "the machine backup: patterns it never carries", shape="list"),
        Setting("machine_backup.roots", "json", "machine-backup", ("roots",), [],
                "the machine backup: where checkouts are looked for", "set", shape="list"),
    ]
    for name, default in machine_backup.LIMITS.items():
        rows.append(Setting(f"machine_backup.limits.{name}", "int", "machine-backup", ("limits", name), default,
                            f"the machine backup's limit {name}", minimum=0, maximum=10_000_000))
    rows += [
        Setting("relay.enabled", "bool", "relay", ("enabled",), False,
                "the share relay this node's server starts: it listens for other machines", "on"),
        Setting("relay.origin", "text", "relay", ("origin",), None,
                "the relay's public origin (https:// and a host name, or this machine's http:// loopback)", "set"),
        Setting("relay.host", "text", "relay", ("host",), None,
                "the address the relay binds (127.0.0.1 when unset)", "set"),
        Setting("relay.port", "int", "relay", ("port",), None, "the relay's port (this OS account's when unset)",
                minimum=1, maximum=65535),
        Setting("relay.trusted_proxy", "bool", "relay", ("trusted_proxy",), False,
                "the relay trusts the forwarded address of the TLS terminator in front of it", "on"),
        Setting("tracked.local_folders", "json", "tracked", ("local_folders",), [],
                "folders outside Git the next pass reads as projects", shape="list"),
        Setting("tracked.dismissed_local_folders", "json", "tracked", ("dismissed_local_folders",), [],
                "folders a person dismissed from that list", shape="list"),
        Setting("organizations.organizations", "json", "organizations", ("organizations",), {},
                "the organizations and where their accounts are", "always", shape="object"),
        Setting("organizations.projects", "json", "organizations", ("projects",), {},
                "which organization each project belongs to", "always", shape="object"),
    ]
    return rows


def _template(name: str) -> dict:
    """The whole document a file the door creates starts from, so its reader reads every key it expects."""
    if name in ("retention", "machine", "organizations"):
        return _shipped(f"{name}.json")
    if name == "review":
        import review_policy
        return review_policy.default_document()
    if name == "relay":
        return {"schema": "observatory-relay/1"}
    if name == "tracked":
        import tracked
        return tracked.empty()
    return {}


def _file_problems(name: str, doc: dict) -> list[str]:
    """What the owner of a file refuses in a document the door would write (before anything is kept)."""
    if name == "tracked":
        import tracked
        return tracked.validate(doc)
    if name == "review":
        import review_policy
        try:
            review_policy.parse(doc)
        except review_policy.PolicyError as exc:
            return [str(exc)]
    if name == "relay" and doc.get("schema") != "observatory-relay/1":
        return ["relay.json is not observatory-relay/1"]
    return []


def switch_word(file: Path) -> str | None:
    """`on` or `off` from a switch file, or None when there is none (LC-16): only the word `off` (any case,
    surrounding space ignored) is off, anything else a person wrote is on, and a link or a folder in its place is no
    switch. The one reading of both switch files (`maintenance.read_switch`, `usage_analytics.read_switch`)."""
    try:
        if file.is_symlink() or not file.is_file():
            return None
        text = file.read_text(encoding="utf-8", errors="replace")[:64]
    except OSError:
        return None
    return "off" if text.strip().lower() == "off" else "on"


# --- reading ------------------------------------------------------------------------

class _Docs:
    """One read of the door's files (or of `only`); a file that cannot be read is named in `degraded`, never read as
    defaults. A switch file reads as `{"switch": word}`, or `{}` when there is none."""

    def __init__(self, base: Path, only: tuple | None = None):
        self.base = base
        self.degraded: list[dict] = []
        self.docs: dict[str, Any] = {}
        wanted = set(only) if only is not None else set(FILES)
        if "settings" in wanted:
            try:
                self.docs["settings"] = config.load(base)
            except config.ConfigurationError as exc:
                self._fail("settings", exc)
        if "local" in wanted:
            local, notes = config.read_local(base)
            if notes:
                self.degraded += [{"source": FILES["local"], "code": n.get("code"), "reason": n.get("step")}
                                  for n in notes]
            else:
                self.docs["local"] = local or {}
        for name in ("models", "cleanup", *OWN_FILES):
            if name not in wanted:
                continue
            file = base / FILES[name]
            try:
                self.docs[name] = config.read_json(file) if file.exists() or file.is_symlink() else {}
            except config.ConfigurationError as exc:
                self._fail(name, exc)
        for name in SWITCH_FILES:
            if name in wanted:
                word = switch_word(base / FILES[name])
                self.docs[name] = {} if word is None else {"switch": word}

    def _fail(self, name: str, exc: Exception) -> None:
        self.degraded.append({"source": FILES[name], "code": "unreadable", "reason": str(exc)[:200]})

    def readable(self, s: Setting) -> bool:
        return s.file in self.docs

    def stored(self, s: Setting) -> Any:
        """The value as the file holds it, or ABSENT."""
        return _dig(self.docs.get(s.file) or {}, s.where)

    def value(self, s: Setting) -> Any:
        """What the engine reads: the stored value in the setting's own form, else its default."""
        if not self.readable(s):
            return None
        if s.file == "cleanup" and s.where[0] == "guard":
            return _guard_value(self.docs["cleanup"].get("guard") or {}, s)
        stored = self.stored(s)
        if s.key == "updates.auto" and stored is ABSENT:
            if "settings" not in self.docs:
                return None                 # whether an older install turned it off cannot be read
            if _legacy_updates_off(self.docs["settings"]):
                return False
        return _from_stored(s, stored)


def _legacy_updates_off(settings: Any) -> bool:
    """`updates.auto: false` in settings.json: how 0.17 and 0.18 turned updates off, still read as off while the
    switch file is absent (D1, LC-16; the next `auto-update` command moves it into the file)."""
    updates = settings.get("updates") if isinstance(settings, dict) else None
    return isinstance(updates, dict) and updates.get("auto", True) is False


def _from_stored(s: Setting, stored: Any) -> Any:
    if stored is ABSENT:
        return copy.deepcopy(s.default)
    if s.kind == "models":
        return [e.get("id") for e in stored or [] if isinstance(e, dict)]
    if s.file in SWITCH_FILES:
        return stored != "off"
    return stored


def read(key: str, *, base: Path | None = None) -> Any:
    """One setting's value as the engine reads it, from its own file only (None when that file cannot be read).
    The reader of every value the door owns: a module that reads one routes through here rather than its file."""
    s = _setting(key)
    files = (s.file, "settings") if s.key == "updates.auto" else (s.file,)
    return _Docs(_home(base), only=files).value(s)


def _guard_value(own: dict, s: Setting) -> Any:
    """The disk guard's merge (`disk_guard.load_config`): a workspace value equal to what any release shipped is the
    copy init made, not a choice; a choice wins, a nested one over the shipped mapping."""
    shipped = _shipped("cleanup.json").get("guard") or {}
    previous = [v for v in (_shipped("cleanup-history.json").get("guard") or {}).values() if isinstance(v, dict)]
    top, rest = s.where[1], s.where[2:]
    value = own.get(top, ABSENT)
    if value is ABSENT or value == shipped.get(top) or any(value == p.get(top) for p in previous):
        value = shipped.get(top, ABSENT)
    elif isinstance(value, dict) and isinstance(shipped.get(top), dict):
        value = {**shipped[top], **value}
    value = _dig(value, rest) if rest else value
    return copy.deepcopy(s.default) if value is ABSENT else value


def _home(base: Path | None) -> Path:
    return base or config.home()


def listing(base: Path | None = None) -> dict:
    """The catalogue with each key's current value (`value`) and whether a file sets it (`set`)."""
    base = _home(base)
    docs = _Docs(base)
    rows = []
    for s in catalogue().values():
        row = s.row()
        row["value"] = docs.value(s)
        row["set"] = docs.readable(s) and docs.stored(s) is not ABSENT
        rows.append(row)
    return {"schema": SCHEMA, "settings": rows, "degraded": docs.degraded}


# --- values ---------------------------------------------------------------------------

def _shown_key(key: Any) -> str:
    return credential_shape.shown(key) if isinstance(key, str) else "a key that is not text"


def _setting(key: Any) -> Setting:
    table = catalogue()
    if not isinstance(key, str) or key not in table:
        raise Refusal("unknown_key", f"{_shown_key(key)} is not a setting this engine has; "
                                     "`configure --list` names every one", key=credential_shape.shown(key)
                      if isinstance(key, str) else None)
    return table[key]


def _texts(value: Any, depth: int = 0) -> list[str]:
    """Every text in a value, keys of a structured one included (a structure deeper than 16 is not walked further:
    `check` refuses it)."""
    if isinstance(value, str):
        return [value]
    if depth > 16:
        return []
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _texts(v, depth + 1)]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in (*_texts(k, depth + 1), *_texts(v, depth + 1))]
    return []


#: What a path may hold that `credential_shape` reads as random: a system's temporary folder
#: (the operating system names it with a random run) or a folder named by a UUID. A provider prefix, a token, a key block, a password in a
#: URL or a long hexadecimal run is still refused.
PATH_KINDS = frozenset({"random", "uuid"})


def _secret(s: Setting, text: str) -> bool:
    if s.kind not in ("path", "json"):
        return credential_shape.shaped(text)
    return any(kind not in PATH_KINDS for _, _, kind in credential_shape._spans(text, True))


def check(s: Setting, value: Any) -> Any:
    """The value as the setting stores it, or a refusal by type. None is "back to the default"."""
    if any(_secret(s, text) for text in _texts(value)):
        raise Refusal("secret_refused", f"the value for {s.key} is shaped like a credential; credentials go to the "
                                        "vault (`observatory_secret_request`), never into configuration", key=s.key)
    if value is None:
        return None
    bad = Refusal("invalid_value", f"{s.key} takes {_expects(s)}", key=s.key, expects=_expects(s))
    if s.kind == "bool":
        if type(value) is not bool:
            raise bad
        return value
    if s.kind == "enum":
        if value not in s.choices:
            raise bad
        return value
    if s.kind in ("int", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (s.kind == "int" and type(value) is not int):
            raise bad
        if not math.isfinite(value) or (s.minimum is not None and value < s.minimum) \
                or (s.maximum is not None and value > s.maximum):
            raise bad
        return float(value) if s.file == "models" else value
    if s.kind == "path":
        if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
            raise bad
        import workspace
        return workspace.storable_path(Path(value).expanduser())[0]
    if s.kind == "models":
        if not isinstance(value, list) or not value \
                or not all(isinstance(i, str) and config.MODEL_ID.fullmatch(i) for i in value):
            raise bad
        return list(value)
    if s.kind == "text":
        if not isinstance(value, str) or not 0 < len(value) <= TEXT_LIMIT \
                or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise bad
        return value
    if s.kind == "json":
        if not isinstance(value, list if s.shape == "list" else dict):
            raise bad
        try:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False)
            text.encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise bad from None
        if len(text) > JSON_LIMIT or _depth(value) > 16:
            raise bad
        return json.loads(text)
    raise bad


def _depth(value: Any, at: int = 0) -> int:
    if at > 16:
        return at
    if isinstance(value, dict):
        return max([_depth(v, at + 1) for v in value.values()], default=at + 1)
    if isinstance(value, list):
        return max([_depth(v, at + 1) for v in value], default=at + 1)
    return at


def _expects(s: Setting) -> str:
    if s.kind == "bool":
        return "true or false"
    if s.kind == "enum":
        return "one of: " + ", ".join(s.choices)
    if s.kind in ("int", "number"):
        whole = "a whole number" if s.kind == "int" else "a number"
        return f"{whole} from {s.minimum} to {s.maximum}" if s.maximum is not None else f"{whole} of at least {s.minimum}"
    if s.kind == "path":
        return "an absolute path"
    if s.kind == "text":
        return f"one line of text of at most {TEXT_LIMIT} characters"
    if s.kind == "json":
        return f"a JSON {'list' if s.shape == 'list' else 'object'} of at most {JSON_LIMIT // 1024} KB"
    return "one or more model ids like vendor/model"


def parse(key: str, text: str) -> Any:
    """A command line's text as the typed value `check` takes; empty text is "back to the default"."""
    s = _setting(key)
    if text == "":
        return None
    if s.kind == "bool":
        return {"true": True, "false": False}.get(text, text)
    if s.kind == "int":
        return int(text) if text.isascii() and text.isdigit() and len(text) <= 6 else text
    if s.kind == "number":
        try:
            number = float(text)
        except ValueError:
            return text
        return int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number
    if s.kind == "models":
        return [part.strip() for part in text.split(",") if part.strip()]
    if s.kind == "json":
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _to_stored(s: Setting, value: Any) -> Any:
    if s.kind == "models":
        return [{"id": i, "why": "configured"} for i in value]
    if s.file in SWITCH_FILES:
        return "on" if value else "off"
    return value


def _needs_person(s: Setting, before: Any, after: Any) -> bool:
    if before == after or s.person == "never":
        return False
    if s.person == "always":
        return True
    if s.person == "on":
        return after is True
    if s.person == "off":
        return after is False
    if s.person == "set":
        return after is not None
    if s.person == "values":
        return after in s.person_values
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (before, after))
    if s.person == "raise":
        return not numbers or after > before
    if s.person == "lower":
        return not numbers or after < before
    return True


def _effect(s: Setting, before: Any, after: Any) -> str:
    text = f"{s.key}: {json.dumps(before)} -> {json.dumps(after)}. {s.description[0].upper()}{s.description[1:]}"
    if s.key.startswith("interface."):
        text += "; the pages show it after `project-observatory full open --rebuild`"
    return text


# --- files ------------------------------------------------------------------------------

def _touched(s: Setting) -> list[str]:
    if (s.where[0], s.where[-1]) in config.LOCAL_MOVED and s.file == "settings":
        return [FILES["settings"], FILES["local"]]
    return [FILES[s.file]]


def _hash(base: Path, rel: str) -> str | None:
    try:
        return "sha256:" + hashlib.sha256((base / rel).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _digest(changes: list[dict], files: dict) -> str:
    body = json.dumps({"changes": [[c["key"], c["storedAfter"] if c["wasSetAfter"] else None] for c in changes],
                      "files": files}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _plans(base: Path) -> Path:
    return base / "store" / "raw" / PLANS  # paths-check: allow (the workspace `base` the caller named; a test redirects it through `base`)


def _write_private(file: Path, doc: dict) -> None:
    import atomic
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic.write_json(file, doc, indent=2)


def _prune_plans(folder: Path) -> None:
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return
    limit = CLOCK().timestamp() - PLAN_SECONDS
    for entry in entries:
        try:
            if entry.stat().st_mtime < limit:
                entry.unlink()
        except OSError:
            pass


def _own_document(base: Path, name: str, docs: Any = None) -> dict:
    """A file of its own as the door writes it: what it holds, or the reader's whole document when it is absent."""
    file = base / FILES[name]
    if not file.exists():
        return _template(name)
    return copy.deepcopy(docs.docs[name]) if docs is not None else config.read_json(file)


def _check_files(base: Path, docs: _Docs, settings: list, stored: dict) -> None:
    """Every file of its own as the plan would leave it, checked by the module that reads it: a value its reader
    would refuse is refused here, before anything is kept."""
    for name in sorted({s.file for s, _ in settings if s.file in OWN_FILES}):
        doc = _own_document(base, name, docs)
        changed = [s for s, _ in settings if s.file == name]
        for s in changed:
            if stored[s.key] is None:
                _pop(doc, s.where, keep=(s.where[0],))
            else:
                config._local_set(doc, s.where, _to_stored(s, stored[s.key]))
        problems = _file_problems(name, doc)
        if problems:
            keys = [s.key for s in changed]
            raise Refusal("invalid_value", f"{FILES[name]} would be refused by its reader: "
                                           f"{credential_shape.redact(problems[0])[:300]}", key=keys[0], keys=keys)


def plan(changes: Any, *, base: Path | None = None) -> dict:
    """The values before and after, the effects and which changes need the person; writes no configuration."""
    base = _home(base)
    config.validate_workspace(base, required=True)
    if not isinstance(changes, dict) or not changes:
        raise Refusal("no_changes", "a plan takes one or more {key: value} changes; `configure --list` names the keys")
    settings = [(_setting(key), value) for key, value in changes.items()]
    stored = {s.key: check(s, value) for s, value in settings}     # every refusal before anything is kept
    docs = _Docs(base)
    for s, _ in settings:
        if not docs.readable(s):
            raise Refusal("file_unreadable", f"{', '.join(_touched(s))} cannot be read; fix it before changing "
                                             f"{s.key}", key=s.key, degraded=docs.degraded)
    for s, _ in settings:
        if s.file in FILLED_FILES and stored[s.key] is None and s.default is not None:
            stored[s.key] = copy.deepcopy(s.default)     # back to the default writes it: its reader reads every key
    _check_files(base, docs, settings, stored)
    rows, files = [], {}
    for s, _ in settings:
        after_stored = stored[s.key]
        before, was = docs.value(s), docs.stored(s)
        after = _from_stored(s, _to_stored(s, after_stored) if after_stored is not None else ABSENT)
        if s.file == "cleanup" and s.where[0] == "guard" and after_stored is None:
            after = copy.deepcopy(s.default)
        rows.append({"key": s.key, "file": _touched(s)[0], "before": before, "after": after,
                     "wasSet": was is not ABSENT, "storedBefore": None if was is ABSENT else was,
                     "wasSetAfter": after_stored is not None,
                     "storedAfter": None if after_stored is None else _to_stored(s, after_stored),
                     "gate": "person" if _needs_person(s, before, after) else "safe",
                     "effect": _effect(s, before, after)})
        for rel in _touched(s):
            files[rel] = _hash(base, rel)
    digest = _digest(rows, files)
    folder = _plans(base)
    _prune_plans(folder)
    _write_private(folder / f"{digest.split(':', 1)[1]}.json",
                   {"schema": SCHEMA, "digest": digest, "at": _now(), "changes": rows, "files": files})
    return {"schema": SCHEMA, "digest": digest, "changes": rows,
            "before": {r["key"]: r["before"] for r in rows}, "after": {r["key"]: r["after"] for r in rows},
            "effects": [r["effect"] for r in rows],
            "needsPerson": [r["key"] for r in rows if r["gate"] == "person"],
            "apply": f"project-observatory full configure --apply {digest}", "degraded": docs.degraded}


def _load_plan(base: Path, digest: Any) -> dict:
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise Refusal("plan_unknown", "apply takes the `digest` a plan answered (sha256: and 64 hex digits)")
    file = _plans(base) / f"{digest.split(':', 1)[1]}.json"
    try:
        doc = config.read_json(file)
        fresh = file.stat().st_mtime >= CLOCK().timestamp() - PLAN_SECONDS
    except (config.ConfigurationError, OSError):
        doc, fresh = None, False
    if not doc or doc.get("digest") != digest or not fresh:
        raise Refusal("plan_unknown", "no waiting plan has this digest (applied already, older than a day, or never "
                                      "made): plan again", digest=digest)
    return doc


def planned_keys(digest: Any, *, base: Path | None = None) -> list[str]:
    """The keys a waiting plan changes, so a caller can check them before it applies (`plan_unknown` otherwise)."""
    return [row.get("key") for row in _load_plan(_home(base), digest).get("changes") or []]


def applied_keys(journal_id: Any, *, base: Path | None = None) -> list[str]:
    """The keys an applied change wrote, so a caller can check them before it undoes (`journal_unknown` otherwise)."""
    if isinstance(journal_id, str) and JOURNAL_ID.fullmatch(journal_id):
        for row in _journal_lines(_home(base)):
            if row.get("id") == journal_id and row.get("kind") == "apply":
                return [c.get("key") for c in row.get("changes") or []]
    raise Refusal("journal_unknown", "undo takes the `journal` id an apply answered (cfg_ and 16 hex digits)")


def _decided(action: str, target: str, actor: str, decision: Any, keys: list[str]) -> dict | None:
    """Who decided a change that needs the person: the person at the terminal or at the page, or the operator's words
    an agent relays, checked by the engine's one decision check (`node/actions.decided`, DEC-0121, OBS-611; `target`
    the digest or the journal id). A change no key of which needs the person, and that carries no words, needs no
    one."""
    if not keys and decision is None:
        return None
    act, title = ACTIONS[action]
    import engine_node
    actions = engine_node.module("actions")
    answer = actions.decided(
        action={"id": act, "title": title, "tool": "observatory.configure", "gate": "relayed"}, target=target,
        caller=actor, decision=decision, now=_now())
    if answer.get("person"):
        # The person at a terminal, or at the page the local control server serves with its token (OBS-562).
        return answer["record"] if keys else None
    if answer.get("ok") is True:
        return answer["record"]
    why = answer.get("why")
    if why == "absent":
        raise Refusal(answer["code"],
                      f"{', '.join(keys)} need the person: ask them, then call again with their words as `decision` "
                      f"(by operator, via relayed-instruction, action {act}, target {target}, the words verbatim, "
                      "when they said them)", keys=keys, action=act, target=target)
    raise Refusal(answer["code"], actions.REASONS.get(why, actions.REASONS["contract"]), action=act,
                  **({"target": target} if why in ("contract", "target") else {}))


def _actor(actor: Any) -> str:
    if not isinstance(actor, str) or not ACTOR.fullmatch(actor):
        raise Refusal("actor_refused", "the actor is `person:cli` (a terminal), `person:page` (the page the local "
                                       "control server serves) or `agent:<name>` / `service:<name>`")
    return actor


def _set(base: Path, s: Setting, stored: Any, present: bool) -> None:
    """Write one setting's stored value (`present`), or remove it (back to the default)."""
    import workspace
    moved = config.LOCAL_MOVED.get((s.where[0], s.where[-1])) if s.file == "settings" else None
    if moved:
        if present:
            config.write_local(base, {moved[0]: stored} if len(moved) == 1 else {moved[0]: {moved[1]: stored}})
        else:
            _unset_moved(base, s, moved)
        return
    if s.file == "local":
        if present:
            config.write_local(base, {s.where[0]: {s.where[1]: stored}})
        else:
            doc, _ = config.read_local(base)
            if doc is not None and _dig(doc, s.where) is not ABSENT:
                _pop(doc, s.where, keep=())
                _write_private(config.local_path(base), doc)
        return
    if s.file in SWITCH_FILES:
        _set_switch(base, s.file, stored, present)
        return
    file = base / FILES[s.file]
    if s.file in OWN_FILES:
        doc = _own_document(base, s.file)
    else:
        doc = config.load(base) if s.file == "settings" else (config.read_json(file) if file.exists() else {})
    if present:
        config._local_set(doc, s.where, stored)
    else:
        _pop(doc, s.where, keep=("sources", "integrations", "features") if s.file == "settings" else (s.where[0],))
    workspace.write_json(file, doc)


def _set_switch(base: Path, name: str, word: Any, present: bool) -> None:
    """A switch file through its owner's writer (owner-only, atomic, never through a link), so what the switch does
    besides is kept: analytics off deletes the install id and the queue. Back to the default removes the file."""
    if not present:
        workspace_file = base / FILES[name]
        if workspace_file.is_symlink():
            raise config.ConfigurationError(f"{FILES[name]} is a link; the door writes no switch through one")
        workspace_file.unlink(missing_ok=True)
        return
    if name == "auto-update":
        import maintenance
        maintenance.write_switch(base, word != "off")
    else:
        import usage_analytics
        usage_analytics.write_switch(base, word != "off")


def _pop(doc: dict, where: tuple, *, keep: tuple) -> None:
    """Remove `where`, and every parent it leaves empty except the top-level ones in `keep`."""
    parents = []
    node = doc
    for key in where[:-1]:
        parents.append((node, key))
        node = node.get(key) if isinstance(node.get(key), dict) else {}
    node.pop(where[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child and not (parent is doc and key in keep):
            parent.pop(key)


def _unset_moved(base: Path, s: Setting, moved: tuple) -> None:
    """A moved key back to its default (OBS-353): out of local.json, its mirror and settings.json."""
    import workspace
    doc, _ = config.read_local(base)
    if doc is not None:
        _pop(doc, moved, keep=())
        if isinstance(doc.get("mirrored"), dict):
            doc["mirrored"].pop(".".join((s.where[0], s.where[-1])), None)
        _write_private(config.local_path(base), doc)
    raw = config.read_json(base / FILES["settings"])
    if _dig(raw, s.where) is not ABSENT:
        _pop(raw, s.where, keep=("sources", "integrations", "features"))
        workspace.write_json(base / FILES["settings"], raw)


def _write_all(base: Path, rows: list[dict], *, after: bool) -> None:
    """Write every row's value after (or before, for undo); on a failure the files are put back as they were."""
    table = catalogue()
    rels = sorted({rel for r in rows for rel in _touched(table[r["key"]])})
    saved = {}
    for rel in rels:
        try:
            saved[rel] = (base / rel).read_bytes()
        except FileNotFoundError:
            saved[rel] = None
    try:
        for r in rows:
            s = table[r["key"]]
            if after:
                _set(base, s, r["storedAfter"], r["wasSetAfter"])
            else:
                _set(base, s, r["storedBefore"], r["wasSet"])
    except BaseException:
        import atomic
        for rel, data in saved.items():
            try:
                if data is None:
                    (base / rel).unlink(missing_ok=True)
                else:
                    atomic.write_text(base / rel, data.decode("utf-8"))
            except OSError:
                pass
        raise


def _journal_file(base: Path) -> Path:
    return base / "store" / "logs" / JOURNAL


def _journal(base: Path, line: dict) -> list[dict]:
    """Append one line, owner-only. A line that could not be written is named, never hidden: the change stands."""
    import osprivacy
    file = _journal_file(base)
    try:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = osprivacy.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | osprivacy.NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        return []
    except OSError as exc:
        return [{"source": "store/logs/" + JOURNAL, "code": "journal-unwritten", "reason": type(exc).__name__}]


def _journal_lines(base: Path) -> list[dict]:
    try:
        text = _journal_file(base).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _public(rows: list[dict]) -> list[dict]:
    return [{k: r[k] for k in ("key", "file", "before", "after", "gate", "effect") if k in r} for r in rows]


def _journal_row(rows: list[dict]) -> list[dict]:
    return [{k: r[k] for k in ("key", "file", "before", "after", "wasSet", "storedBefore", "wasSetAfter",
                               "storedAfter", "gate")} for r in rows]


def _new_id() -> str:
    return "cfg_" + secrets.token_hex(8)


def apply(digest: Any, *, actor: str, decision: Any = None, base: Path | None = None) -> dict:
    """Write the plan `digest` names, only while its files hold what the plan read; journal it."""
    import workspace
    base = _home(base)
    actor = _actor(actor)
    config.validate_workspace(base, required=True)
    with workspace.lock(base):
        doc = _load_plan(base, digest)
        rows = doc["changes"]
        keys = [r["key"] for r in rows if r.get("gate") == "person"]
        who = _decided("apply", digest, actor, decision, keys)
        moved = sorted(rel for rel, value in (doc.get("files") or {}).items() if _hash(base, rel) != value)
        if moved:
            raise Refusal("plan_stale", f"{', '.join(moved)} changed since the plan: plan again", files=moved)
        _write_all(base, rows, after=True)
        (_plans(base) / f"{digest.split(':', 1)[1]}.json").unlink(missing_ok=True)
        ident = _new_id()
        line = {"id": ident, "kind": "apply", "at": _now(), "actor": actor, "digest": digest,
                "changes": _journal_row(rows)}
        if who:
            line["decision"] = who
        degraded = _journal(base, line)
    return {"schema": SCHEMA, "status": "applied", "journal": None if degraded else ident, "changes": _public(rows),
            "undo": f"project-observatory full configure --undo {ident}", "degraded": degraded}


def undo(journal_id: Any, *, actor: str, decision: Any = None, base: Path | None = None) -> dict:
    """Restore the values an apply found, only while the files still hold what it wrote; journal the undo."""
    import workspace
    base = _home(base)
    actor = _actor(actor)
    config.validate_workspace(base, required=True)
    if not isinstance(journal_id, str) or not JOURNAL_ID.fullmatch(journal_id):
        raise Refusal("journal_unknown", "undo takes the `journal` id an apply answered (cfg_ and 16 hex digits)")
    with workspace.lock(base):
        lines = _journal_lines(base)
        done = next((r for r in lines if r.get("id") == journal_id and r.get("kind") == "apply"), None)
        if done is None:
            raise Refusal("journal_unknown", f"no applied change has the id {journal_id}", journal=journal_id)
        if any(r.get("kind") == "undo" and r.get("undoes") == journal_id for r in lines):
            raise Refusal("already_undone", f"{journal_id} was undone already", journal=journal_id)
        table = catalogue()
        docs = _Docs(base)
        changed = []
        for r in done.get("changes") or []:
            s = table.get(r.get("key"))
            if s is None or not docs.readable(s):
                changed.append(r.get("key"))
                continue
            now = docs.stored(s)
            if (now is ABSENT) != (not r.get("wasSetAfter")) or (now is not ABSENT and now != r.get("storedAfter")):
                changed.append(s.key)
        if changed:
            raise Refusal("undo_refused", f"{', '.join(map(str, changed))} changed since {journal_id}; undoing would "
                                          "overwrite that: plan the values you want instead", keys=changed,
                          journal=journal_id)
        rows = [{**r, "before": r["after"], "after": r["before"],
                 "gate": "person" if _needs_person(table[r["key"]], r["after"], r["before"]) else "safe"}
                for r in done["changes"]]
        who = _decided("undo", journal_id, actor, decision, [r["key"] for r in rows if r["gate"] == "person"])
        _write_all(base, done["changes"], after=False)
        ident = _new_id()
        line = {"id": ident, "kind": "undo", "undoes": journal_id, "at": _now(), "actor": actor,
                "changes": [{k: r[k] for k in ("key", "file", "before", "after", "gate")} for r in rows]}
        if who:
            line["decision"] = who
        degraded = _journal(base, line)
    for r in rows:
        r["effect"] = _effect(table[r["key"]], r["before"], r["after"])
    return {"schema": SCHEMA, "status": "undone", "journal": None if degraded else ident, "undoes": journal_id,
            "changes": _public(rows), "degraded": degraded}


def set_now(changes: dict, *, actor: str = "person:cli", base: Path | None = None) -> dict:
    """Plan and apply in one step: `configure SECTION NAME VALUE` from a terminal, which is its own preview."""
    return apply(plan(changes, base=base)["digest"], actor=actor, base=base)


# --- the command line ---------------------------------------------------------------------

def command(a, base: Path) -> tuple[dict, int]:
    """`configure --list | --plan KEY=VALUE... | --apply DIGEST | --undo ID`: (answer, exit code)."""
    try:
        if a.list:
            return listing(base), 0
        if a.plan:
            changes = {}
            for item in a.plan:
                key, sep, text = item.partition("=")
                if not sep:
                    raise Refusal("invalid_value", "each change is KEY=VALUE (an empty VALUE is the default)",
                                  key=_shown_key(key))
                changes[key] = parse(key, text)
            return plan(changes, base=base), 0
        if a.apply:
            return apply(a.apply, actor="person:cli", base=base), 0
        return undo(a.undo, actor="person:cli", base=base), 0
    except Refusal as refusal:
        return refusal.answer(), 2
