"""What Observatory uses, declared in its own well-known document (`fabric-connections/0.1`, OBS-326).

The contract's extension `extensions/connections/0.1` (fabric-agent-contract DEC-0096, rule FAC-SEM-127)
lets a host such as Fabric Dashboards draw which agents, models, connectors, stores and channels a service
depends on, and whether each works now. The declaration is built here and checked before it is published
(`check`): against the contract's own schema, vendored at the contract pin
(`fabric/interop-schemas/service-connections-extension.schema.json`), each `id` once, and no string that
could be read as a credential (stricter than the contract's detector: every string here is this module's
own). A list that fails is never published, so a host never hides it as "declared nothing"; the local
server names the refusal in `degraded` instead. The engine does not vendor the descriptor kit's
`fabric_connections` module: its secret detector's docstrings hold sample credential URLs the public
release scan refuses, so the engine follows the contract, as the kit does.

ONLY WHAT THE ENGINE ACTUALLY CALLS, and only when it is set to:

- `fabric-hub` (service, target the product id `fabric`, capability `project.resolve`): the tick's
  `fabric-attribution` step asks Fabric's hub which Fabric project each folder is (`fabric_hub.py`).
  Always declared: the step runs on every install.
- `release-feed` (connector, `github-releases`): the update check reads the newest release's
  `SHA256SUMS` every five minutes, with automatic updates on or off (`maintenance.check_pass`).
- one connector per provider integration that is ON in `config/settings.json` (`INTEGRATIONS`): the
  collector named there calls that provider. An integration that is off is never declared, even when a
  receipt it wrote earlier is still in `store/raw`.
- `usage-analytics` (connector, `aptabase`): the anonymous usage events, only while they are collected
  (`usage_analytics.collecting`).

An integration is on when `configuration.enabled` says so, the collectors' own rule: with
`OBSERVATORY_OFFLINE=1` in this process every integration is off and none is declared (OBS-340).

STATE AND TIME come from the engine's own records, never guessed:

- a provider: its collector's receipt in `store/raw` (`degraded` rows make it `degraded`; an unreadable
  receipt too; a clean one is `ok`); several receipts take the worst state and the latest time; on but
  never run is `unknown`, with no `lastAt`;
- a receipt older than `STALE_FACTOR` times its collector's interval in the tick (`INTERVAL_HOURS`), and
  never younger than `STALE_FLOOR_HOURS`, is STALE (OBS-339): its collector has stopped running, so what it
  last saw no longer says whether the connection works. A stale receipt reads `degraded` (never better), with
  a reason naming that it is stale, and keeps its old `lastAt`. `degraded`, not `unknown`: the engine counts
  on that collector and it is not running, which is a fault a host should show; `unknown` here means "never
  used" or "not installed", which are not faults, and an `unknown` receipt would rank below a fresh `ok` one
  and vanish when the two are combined. The age is measured at `now`, so the local server adds a clock
  bucket (`clock_bucket`, ten minutes) to its cache key: a receipt that turns stale reads so at most ten
  minutes late, however long no file changes;
- the hub: `store/raw/fabric-attribution.json`; a hub that is dead or cannot be verified is `down`; any other
  refusal `degraded`, even beside `hub-absent` (OBS-342: a refusal is a fault, absence alone is not); no hub
  on this machine (`hub-absent` and nothing else) is `unknown`, because Fabric is simply not installed; a
  stale receipt `degraded`, as for a provider, unless the hub was `down`;
- the release feed: the check job's record `store/update-check.json` (`undetermined` is `degraded`);
- the usage analytics: the last send recorded by `usage_analytics.note_send`.

`lastAt` is the collector's own time (`scanned_at`, `checked_at`, `measured_at`, `probed_at`), else the
time the engine wrote the receipt. A `reason` is a fixed sentence of this module: a collector's own text can
hold a path, a name or a key, and one credential-shaped run would make a host hide the whole declaration.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import re
from typing import Callable

#: The extension key (fabric-agent-contract `CONNECTIONS_EXTENSION_KEY`, DEC-0096).
KEY = "https://fabric.passioncode.ai/agent-contract/extensions/connections/0.1"
SCHEMA = "service-connections-extension.schema.json"
#: What could be read as a credential in a string member: a URL, an address, a token prefix, a JWT, a long mixed
#: run. Stricter than the contract's detector on purpose: every string declared here is this module's own.
_CREDENTIALISH = re.compile(r"://|@|\b(?:sk-|gh[pousr]_|xox[abposr]-|AKIA|glpat-|eyJ)|[A-Za-z0-9+/=_-]{40,}")
_STRINGS = ("id", "target", "capability", "label", "purpose", "reason")

#: How often the tick runs (`tools/install_launchd.py --interval`, default 1800 seconds), in hours.
TICK_HOURS = 0.5
#: Each receipt's collector interval in the tick (`tools/tick.py`): every tick, or the hours its `stale(...)`
#: gate waits before it runs the collector again. Bitbucket runs with the remotes, gated on `remotes.json`
#: (`STALE_HOURS`, 6). A collector with an age gate of its own (`MAX_AGE_HOURS`: `scan_google` skips a scan
#: younger than 12 hours and writes nothing) takes the longer of the two, since a healthy receipt can be that
#: old. `tests/test_service_connections.py` reads tick.py and the collectors and fails when they disagree.
INTERVAL_HOURS = {
    "gh/_degraded.json": TICK_HOURS,
    "bitbucket.json": 6,
    "cloudflare_zones.json": TICK_HOURS,
    "cloudflare_account.json": 24,
    "heroku.json": 24,
    "digitalocean.json": 24,
    "google.json": 12,
    "openrouter.json": 24,
    "domains_live.json": 24,
    "mcp.json": TICK_HOURS,
    "fabric-attribution.json": TICK_HOURS,
}
#: A receipt is stale past this many of its collector's intervals: generous, because the collectors take
#: turns when the machine is loaded (`tick.py` `Rotation`) and a step out of time waits for the next tick.
STALE_FACTOR = 3
#: ...and never sooner than this: the tick's interval is a setting, and a computer that slept did not tick.
STALE_FLOOR_HOURS = 6
#: How long the local server may keep a declaration before measuring the receipts' age again, in seconds.
CLOCK_BUCKET_SECONDS = 600

#: Provider integrations whose collector calls out: (integration, id, target, receipts in store/raw, purpose).
PROVIDERS = (
    ("github", "github", "github", ("gh/_degraded.json",),
     "repositories, owners and transfers of the accounts you configured, through the gh CLI"),
    ("bitbucket", "bitbucket", "bitbucket", ("bitbucket.json",), "repositories of the workspaces you configured"),
    ("cloudflare", "cloudflare", "cloudflare", ("cloudflare_zones.json", "cloudflare_account.json"),
     "zones, Workers, Pages and storage of the accounts you configured"),
    ("heroku", "heroku", "heroku", ("heroku.json",), "apps and add-ons, through the Heroku CLI"),
    ("digitalocean", "digitalocean", "digitalocean", ("digitalocean.json",), "Droplets, apps and databases"),
    ("google", "google", "google", ("google.json",), "analytics properties and search console sites"),
    ("openrouter", "openrouter", "openrouter", ("openrouter.json",), "the keys of the OpenRouter accounts you configured"),
    ("domains", "domains", "rdap", ("domains_live.json",), "registration and liveness of your domains"),
    ("mcp", "mcp-probe", "claude-code", ("mcp.json",), "which MCP servers your agents' configs declare; a live probe runs only on request"),
)
INTEGRATIONS = tuple(p[0] for p in PROVIDERS)
#: The hub reasons (fabric_hub.HubUnavailable) that mean it could not be reached at all.
HUB_DOWN = frozenset({"hub-dead", "hub-origin-refused", "hub-unverified"})
#: No hub on this machine: Fabric is not installed, which is not an outage.
HUB_ABSENT = "hub-absent"
_TIME_KEYS = ("scanned_at", "checked_at", "measured_at", "probed_at")
_RANK = {"unknown": 0, "ok": 1, "degraded": 2, "down": 3}
REASONS = {
    "degraded": "its last run did not read everything; the local server's degraded list says what",
    "unreadable": "its last output cannot be read",
    "stale": "its last run is older than its schedule allows, so its collector has stopped running; the tick's log says why",
    "never": "not run on this install yet",
    "down": "it could not be reached at the last attempt",
    "absent": "no Fabric hub was found on this machine",
}
#: Why the last declaration was refused by the kit, or None. Read by the local server for `degraded`.
LAST_PROBLEM: str | None = None


def _stamp(value: object) -> str | None:
    """An RFC 3339 date-time in UTC with seconds, or None for anything else (a date alone included)."""
    if not isinstance(value, str) or "T" not in value.upper():
        return None
    try:
        when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _written(path: Path) -> str | None:
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return now if now is not None else datetime.datetime.now(datetime.timezone.utc)


def stale_after(name: str) -> datetime.timedelta:
    """How old the receipt `name` may be before it is stale (OBS-339): `STALE_FACTOR` intervals of its
    collector, never less than `STALE_FLOOR_HOURS`."""
    hours = INTERVAL_HOURS.get(name, TICK_HOURS)
    return datetime.timedelta(hours=max(STALE_FACTOR * hours, STALE_FLOOR_HOURS))


def _is_stale(name: str, when: str | None, now: datetime.datetime) -> bool:
    """True when the receipt's own time is older than `stale_after(name)`. A receipt with no time at all is
    never called stale: its age cannot be said. A time ahead of `now` (a clock moved back) is not stale."""
    if when is None:
        return False
    at = datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return now - at > stale_after(name)


def _receipt(path: Path, name: str | None = None, now: datetime.datetime | None = None) \
        -> tuple[str, str | None, list, bool] | None:
    """(state, lastAt, degraded rows, stale) of one receipt, or None when it does not exist. `name` is the
    receipt's name in `INTERVAL_HOURS`, default the file name."""
    if path.is_symlink() or not path.is_file():
        return None
    name, now = name or path.name, _now(now)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        when = _written(path)
        return "degraded", when, [{"reason": "unreadable"}], _is_stale(name, when, now)
    rows = doc if isinstance(doc, list) else doc.get("degraded") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        when = _written(path)
        return "degraded", when, [{"reason": "unreadable"}], _is_stale(name, when, now)
    when = None
    if isinstance(doc, dict):
        when = next((s for s in (_stamp(doc.get(k)) for k in _TIME_KEYS) if s), None)
    when = when or _written(path)
    return ("degraded" if rows else "ok"), when, rows, _is_stale(name, when, now)


def _combine(found: list[tuple[str, str | None, list, bool]]) -> dict:
    """The worst state and the latest time of several receipts; nothing found is `unknown`. A stale receipt
    counts as at least `degraded`. The reason, by precedence: down, unreadable, stale, degraded."""
    if not found:
        return {"state": "unknown", "reason": REASONS["never"]}

    def state_of(f) -> str:
        return max(f[0], "degraded", key=_RANK.__getitem__) if f[3] else f[0]

    state = max((state_of(f) for f in found), key=_RANK.__getitem__)
    times = [f[1] for f in found if f[1]]
    out: dict = {"state": state}
    if times:
        out["lastAt"] = max(times)
    if state != "ok":
        unreadable = any(r.get("reason") == "unreadable" for f in found for r in f[2] if isinstance(r, dict))
        stale = any(f[3] for f in found)
        why = state
        if state == "degraded":
            why = "unreadable" if unreadable else "stale" if stale else "degraded"
        out["reason"] = REASONS[why]
    return out


def _on(settings: dict, integration: str, environ: dict | None) -> bool:
    """The collectors' own rule (`configuration.enabled`): off while `OBSERVATORY_OFFLINE=1` (OBS-340)."""
    import configuration
    return configuration.enabled(integration, settings=settings or {}, environ=environ)


def _hub(scratch: Path, now: datetime.datetime | None = None) -> dict:
    """The hub's state from the attribution step's receipt. Precedence, explicit (OBS-342): a dead or
    unverifiable hub is `down`; any other refusal is `degraded`, even beside `hub-absent`; `hub-absent` and
    nothing else is `unknown` (Fabric is not installed, not an outage), unless the receipt is stale."""
    got = _receipt(scratch / "fabric-attribution.json", now=now)
    if got is None:
        return {"state": "unknown", "reason": REASONS["never"]}
    state, when, rows, stale = got
    reasons = {str(r.get("reason")) for r in rows if isinstance(r, dict)}
    if reasons & HUB_DOWN:
        state = "down"
    elif reasons == {HUB_ABSENT} and not stale:
        out: dict = {"state": "unknown", "reason": REASONS["absent"]}
        if when:
            out["lastAt"] = when
        return out
    return _combine([(state, when, rows, stale)])


def _release_feed(base: Path) -> dict:
    path = base / "store" / "update-check.json"
    try:
        check = (json.loads(path.read_text(encoding="utf-8")) or {}).get("check") or {}
    except (OSError, ValueError, AttributeError):
        check = {}
    result = check.get("result")
    if result not in ("up-to-date", "update-available", "undetermined"):
        return {"state": "unknown", "reason": REASONS["never"]}
    out: dict = {"state": "degraded" if result == "undetermined" else "ok"}
    when = _stamp(check.get("at"))
    if when:
        out["lastAt"] = when
    if out["state"] != "ok":
        out["reason"] = "the release feed could not be read at the last check; it is read again in five minutes"
    return out


def _analytics(base: Path, environ: dict | None) -> dict | None:
    try:
        import usage_analytics as UA
        if not UA.collecting(base, environ):
            return None
        last = (UA.counters(base) or {}).get("last_send") or {}
    except Exception:  # noqa: BLE001: an unreadable state declares nothing it cannot stand behind
        return None
    when = _stamp(last.get("at"))
    if when is None or not isinstance(last.get("ok"), bool):
        return {"state": "unknown", "reason": "nothing sent from this install yet"}
    out = {"state": "ok" if last["ok"] else "degraded", "lastAt": when}
    if not last["ok"]:
        out["reason"] = "the last send did not reach the analytics host; the queue waits for the next pass"
    return out


def declared(base: Path, scratch: Path, settings: dict, *, environ: dict | None = None,
             now: datetime.datetime | None = None) -> list[dict]:
    """Every connection this install makes, with its state, in the contract's members. `now` is the time a
    receipt's age is measured at (OBS-339), default the clock."""
    now = _now(now)
    out = [{"id": "fabric-hub", "kind": "service", "target": "fabric", "capability": "project.resolve",
            "label": "Fabric's hub", "purpose": "which Fabric project each folder is, asked by the tick",
            **_hub(scratch, now)},
           {"id": "release-feed", "kind": "connector", "target": "github-releases", "label": "release feed",
            "purpose": "the newest release's SHA256SUMS, read every five minutes to find an update",
            **_release_feed(base)}]
    for integration, ident, target, receipts, purpose in PROVIDERS:
        if not _on(settings, integration, environ):
            continue
        found = [r for r in (_receipt(scratch / name, name, now) for name in receipts) if r is not None]
        out.append({"id": ident, "kind": "connector", "target": target, "purpose": purpose, **_combine(found)})
    analytics = _analytics(base, environ)
    if analytics is not None:
        out.append({"id": "usage-analytics", "kind": "connector", "target": "aptabase",
                    "purpose": "anonymous usage and error events, sent by the maintenance job", **analytics})
    return out


_VALIDATOR = None


def check(value: dict) -> list[str]:
    """What the contract's reader would refuse in `value` (FAC-SEM-127): the schema, an id named twice, a
    credential-like string. Never echoes a value."""
    global _VALIDATOR
    if _VALIDATOR is None:
        import interop
        _VALIDATOR = interop.contract_validator(SCHEMA)
    problems = [f"/{'/'.join(map(str, e.absolute_path))} {e.validator}" for e in _VALIDATOR.iter_errors(value)]
    if problems:
        return problems
    seen = set()
    for index, connection in enumerate(value["connections"]):
        if connection["id"] in seen:
            problems.append(f"/connections/{index}/id named twice")
        seen.add(connection["id"])
        for member in _STRINGS:
            text = connection.get(member)
            if isinstance(text, str) and _CREDENTIALISH.search(text):
                problems.append(f"/connections/{index}/{member} reads like a credential")
        if "lastAt" in connection and _stamp(connection["lastAt"]) != connection["lastAt"]:
            problems.append(f"/connections/{index}/lastAt is not a UTC date-time")
    return problems


def extension(base: Path | None = None, scratch: Path | None = None, settings: dict | None = None, *,
              environ: dict | None = None, declared: Callable[..., list[dict]] | None = None,
              now: datetime.datetime | None = None) -> dict:
    """The `extensions` entry for the well-known document, or {} when the kit refuses the list (its reason
    is kept in LAST_PROBLEM): a declaration a host would hide is never published. Never raises."""
    global LAST_PROBLEM
    try:
        if base is None or scratch is None or settings is None:
            import configuration
            import paths
            base = Path(paths.HOME) if base is None else base
            scratch = Path(paths.SCRATCH) if scratch is None else scratch
            settings = configuration.load() if settings is None else settings
        build = declared if declared is not None else globals()["declared"]
        kwargs = {"environ": environ} if now is None else {"environ": environ, "now": now}
        value = {"connections": build(base, scratch, settings, **kwargs)}
        problems = check(value)
        if problems:
            LAST_PROBLEM = "the connections declaration was not published: " + "; ".join(problems)[:240]
            return {}
        LAST_PROBLEM = None
        return {KEY: value}
    except Exception as exc:  # noqa: BLE001: the well-known document is always answered
        LAST_PROBLEM = f"the connections declaration was not published: {str(exc)[:240]}"
        return {}


def signature_files(base: Path, scratch: Path) -> list[Path]:
    """The files the declaration reads, for the local server's change check."""
    files = [scratch / "fabric-attribution.json", base / "store" / "update-check.json",
             base / "config" / "settings.json", base / "analytics", base / "store" / "analytics" / "counters.json"]
    for _, _, _, receipts, _ in PROVIDERS:
        files += [scratch / name for name in receipts]
    return files


def clock_bucket(now: float | None = None) -> int:
    """The part of the local server's cache key that moves with the clock: a receipt's state depends on its
    age (OBS-339), so a declaration is rebuilt at least every `CLOCK_BUCKET_SECONDS` even when no file it
    reads changed."""
    import time
    return int((time.time() if now is None else now) // CLOCK_BUCKET_SECONDS)


__all__ = ["KEY", "SCHEMA", "PROVIDERS", "INTEGRATIONS", "INTERVAL_HOURS", "STALE_FACTOR", "STALE_FLOOR_HOURS",
           "check", "clock_bucket", "declared", "extension", "signature_files", "stale_after"]
