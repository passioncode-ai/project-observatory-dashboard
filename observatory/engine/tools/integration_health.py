#!/usr/bin/env python3
"""Is each provider integration still working, and which providers does the estate use that nothing here watches
(OBS-557, CP-12 and CP-13)?

Three questions, answered once a day into `store/raw/integration-health.json`:

1. **Is each held key still valid?** Each provider door's own validity call (the one its `ping` verb makes), once a
   UTC day per key. A 401 or 403 is `invalid`; any other failure is `unreachable`, which says nothing about the key.
   The record names the slot, never the value: a value is read into memory for one request and dropped. No new spend:
   each call is a read the door already makes.
2. **Did a provider change the answer a collector relies on?** `COLLECTOR_FIELDS` declares, per collector, the stored
   raw file, the list of rows in it and the fields every healthy row carries. A field that no row carries (absent or
   null in every row of a non-empty list) is `drift`, with the day it was last seen (`store/state/integration-fields.json`).
3. **Which providers does a project use that no door or collector covers?** `PACKAGE_PROVIDERS` and `ENV_PROVIDERS`
   map a dependency or a variable NAME to a provider; applied to `registry/dependencies.json` and the env inventory,
   a provider outside `TRACKED` is `untracked`, with the package or variable name as evidence, never a value.

Offline (`OBSERVATORY_OFFLINE=1`) asks nothing. A door that cannot be read is named in `degraded`.

    integration_health.py           run when due (once a UTC day; drift and untracked are rebuilt every run: files only)
    integration_health.py --force   ask every key now
"""
from __future__ import annotations

import fnmatch
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Callable, NamedTuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = "integration-health/1"
#: What a provider answers when it refuses the key: the door's own errors name `HTTP <code>`.
REFUSED = (401, 403)
HTTP_CODE = re.compile(r"\bHTTP (\d{3})\b")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Provider(NamedTuple):
    """One provider door: `holders()` lists (slot name, a function reading that key's value), `ping(value)` raises
    when the provider refuses or cannot be reached and returns when the key works."""
    name: str
    holders: Callable[[], list]
    ping: Callable[[str], None]


def status_of(exc: BaseException) -> int | None:
    m = HTTP_CODE.search(str(exc))
    return int(m.group(1)) if m else None


# ---- 1. keys -------------------------------------------------------------------------------------------

def check_keys(providers: list[Provider], previous: dict | None, today: str, force: bool = False
               ) -> tuple[list[dict], list[dict]]:
    """One record per held key. A key already checked today keeps its record (once a day per key)."""
    before = {(r.get("provider"), r.get("slot")): r for r in (previous or {}).get("keys") or []}
    out: list[dict] = []
    degraded: list[dict] = []
    for p in providers:
        try:
            held = list(p.holders())
        except Exception as exc:  # noqa: BLE001 (a door that cannot list names itself, the rest still run)
            degraded.append({"source": f"integration:{p.name}", "reason": f"keys not listed: {type(exc).__name__}"})
            continue
        for slot, read in held:
            old = before.get((p.name, slot))
            if old and old.get("checked_on") == today and not force:
                out.append(old)
                continue
            rec = {"provider": p.name, "slot": slot, "checked_on": today}
            try:
                value = read()
            except Exception as exc:  # noqa: BLE001
                out.append({**rec, "state": "unreadable", "status": None, "reason": type(exc).__name__})
                continue
            try:
                p.ping(value)
                rec.update(state="valid", status=200)
            except Exception as exc:  # noqa: BLE001 (the message is the door's: endpoint and status only)
                code = status_of(exc)
                if code in REFUSED:
                    rec.update(state="invalid", status=code)
                else:
                    rec.update(state="unreachable", status=code, reason=type(exc).__name__)
            finally:
                value = None  # noqa: F841 (the value lives for one request)
            if rec["state"] == "invalid":
                rec["since"] = (old or {}).get("since") if (old or {}).get("state") == "invalid" else today
            out.append(rec)
    out.sort(key=lambda r: (r["provider"], r["slot"]))
    return out, degraded


# ---- 2. drift ------------------------------------------------------------------------------------------

#: collector -> {file: the stored raw answer, rows: the dotted path of its list, fields: carried by every healthy row}.
#: Each field is one the collector reads from the provider and the registry or a finding then relies on.
COLLECTOR_FIELDS: dict[str, dict] = {
    "openrouter": {"file": "openrouter.json", "rows": "keys",
                   "fields": ("label", "limit", "usage", "disabled", "is_provisioning", "created_at")},
    "cloudflare_zones": {"file": "cloudflare_zones.json", "rows": "zones",
                         "fields": ("name", "zone_id", "account_id", "status", "plan", "name_servers")},
    "cloudflare_account": {"file": "cloudflare_account.json", "rows": "accounts",
                           "fields": ("account_id", "account_name", "workers", "pages", "r2", "d1")},
    "digitalocean": {"file": "digitalocean.json", "rows": "teams",
                     "fields": ("team_uuid", "team_name", "droplets", "apps", "databases", "volumes", "domains")},
    "google": {"file": "google.json", "rows": "properties",
               "fields": ("property", "name", "account", "users_30d", "sessions_30d", "views_30d")},
}


def _dig(doc, path: str):
    for part in path.split("."):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def check_drift(raw_dir: pathlib.Path, declared: dict, seen: dict | None, today: str
                ) -> tuple[list[dict], dict]:
    """(drift rows, the updated last-seen map). A collector whose raw file is absent, unreadable or has no rows says
    nothing: no answer is not a renamed field."""
    seen = {c: dict(f) for c, f in (seen or {}).items() if isinstance(f, dict)}
    drift: list[dict] = []
    for collector, spec in sorted(declared.items()):
        try:
            doc = json.loads((raw_dir / spec["file"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = [r for r in _dig(doc, spec["rows"]) or [] if isinstance(r, dict)] if isinstance(
            _dig(doc, spec["rows"]), list) else []
        if not rows:
            continue
        mine = seen.setdefault(collector, {})
        for field in spec["fields"]:
            if any(r.get(field) is not None for r in rows):
                mine[field] = today
            else:
                drift.append({"collector": collector, "field": field, "last_seen": mine.get(field),
                              "rows": len(rows)})
    return drift, seen


# ---- 3. untracked --------------------------------------------------------------------------------------

#: Providers a door or a collector of this engine covers. Anything else a project uses is `untracked`.
TRACKED = frozenset({"cloudflare", "digitalocean", "github", "google", "heroku", "openrouter", "sentry"})
#: package name (fnmatch, lower case, any ecosystem) -> (provider, category).
PACKAGE_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("stripe", "stripe", "payments"), ("@stripe/*", "stripe", "payments"),
    ("braintree", "braintree", "payments"), ("@paddle/*", "paddle", "payments"), ("paddle-*", "paddle", "payments"),
    ("razorpay", "razorpay", "payments"), ("@sentry/*", "sentry", "errors"), ("sentry-sdk", "sentry", "errors"), ("sentry_sdk", "sentry", "errors"),
    ("@bugsnag/*", "bugsnag", "errors"), ("bugsnag", "bugsnag", "errors"), ("rollbar", "rollbar", "errors"),
    ("@datadog/*", "datadog", "errors"), ("ddtrace", "datadog", "errors"), ("newrelic", "newrelic", "errors"),
    ("boto3", "aws", "cloud"), ("botocore", "aws", "cloud"), ("aws-sdk", "aws", "cloud"), ("@aws-sdk/*", "aws", "cloud"),
    ("google-cloud-*", "gcp", "cloud"), ("@google-cloud/*", "gcp", "cloud"),
    ("@azure/*", "azure", "cloud"), ("azure-*", "azure", "cloud"),
    ("@vercel/*", "vercel", "cloud"), ("vercel", "vercel", "cloud"),
    ("@cloudflare/*", "cloudflare", "cloud"), ("wrangler", "cloudflare", "cloud"),
    ("pydo", "digitalocean", "cloud"), ("@digitalocean/*", "digitalocean", "cloud"),
    ("amplitude*", "amplitude", "analytics"), ("@amplitude/*", "amplitude", "analytics"),
    ("@segment/*", "segment", "analytics"), ("analytics-node", "segment", "analytics"),
    ("react-ga4", "google", "analytics"), ("@google-analytics/*", "google", "analytics"),
    ("@sendgrid/*", "sendgrid", "messaging"), ("sendgrid", "sendgrid", "messaging"),
    ("postmark*", "postmark", "messaging"), ("mailgun*", "mailgun", "messaging"),
)
#: variable NAME (fnmatch, upper case) -> (provider, category).
ENV_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("STRIPE_*", "stripe", "payments"), ("PADDLE_*", "paddle", "payments"), ("BRAINTREE_*", "braintree", "payments"),
    ("RAZORPAY_*", "razorpay", "payments"),
    ("SENTRY_*", "sentry", "errors"), ("BUGSNAG_*", "bugsnag", "errors"), ("ROLLBAR_*", "rollbar", "errors"),
    ("DD_API_KEY", "datadog", "errors"), ("DATADOG_*", "datadog", "errors"), ("NEW_RELIC_*", "newrelic", "errors"),
    ("AWS_*", "aws", "cloud"),
    ("GCP_*", "gcp", "cloud"),
    ("AZURE_*", "azure", "cloud"), ("VERCEL_*", "vercel", "cloud"), ("CLOUDFLARE_*", "cloudflare", "cloud"), ("CF_API_TOKEN", "cloudflare", "cloud"),
    ("DIGITALOCEAN_*", "digitalocean", "cloud"), ("HEROKU_*", "heroku", "cloud"),
    ("OPENROUTER_*", "openrouter", "ai"),
    ("AMPLITUDE_*", "amplitude", "analytics"), ("SEGMENT_*", "segment", "analytics"),
    ("SENDGRID_*", "sendgrid", "messaging"),
    ("POSTMARK_*", "postmark", "messaging"), ("MAILGUN_*", "mailgun", "messaging"),
)
#: At most this many names of evidence per project and provider.
EVIDENCE_LIMIT = 5


def _match(table, name: str):
    for pattern, provider, category in table:
        if fnmatch.fnmatchcase(name, pattern):
            return provider, category
    return None


def untracked(deps_doc: dict | None, env_doc: dict | None, tracked: frozenset = TRACKED) -> list[dict]:
    """One row per project and provider the estate does not watch: package and variable NAMES only."""
    found: dict[tuple[str, str], dict] = {}

    def note(project, provider, category, kind, name):
        if not project or provider in tracked:
            return
        row = found.setdefault((project, provider), {"project": project, "provider": provider,
                                                     "category": category, "evidence": []})
        item = {"kind": kind, "name": name}
        if item not in row["evidence"]:
            row["evidence"].append(item)

    for p in (deps_doc or {}).get("projects") or []:
        for pkg in p.get("packages") or []:
            name = str(pkg.get("name") or "")
            hit = _match(PACKAGE_PROVIDERS, name.lower())
            if hit:
                note(p.get("project"), hit[0], hit[1], "package", name)
    for f in (env_doc or {}).get("files") or []:
        for v in f.get("variables") or []:
            name = str(v.get("name") or "")
            hit = _match(ENV_PROVIDERS, name.upper())
            if hit:
                note(f.get("project"), hit[0], hit[1], "env", name)
    rows = sorted(found.values(), key=lambda r: (r["project"], r["provider"]))
    for r in rows:
        r["evidence"].sort(key=lambda e: (e["kind"], e["name"]))
        r["evidence_total"] = len(r["evidence"])
        r["evidence"] = r["evidence"][:EVIDENCE_LIMIT]
    return rows


# ---- the doors -----------------------------------------------------------------------------------------

def default_providers() -> list[Provider]:
    """The doors with a validity call of their own: OpenRouter, Cloudflare and DigitalOcean (their `ping` verbs).
    A door that cannot be imported is left out and named by the caller's `degraded`."""
    import private_io
    out: list[Provider] = []
    try:
        import openrouter as door

        def holders():
            return [(f"openrouter/admin/{label}", (lambda p=path: private_io.read(p).strip()))
                    for label, path in door.admins()]
        out.append(Provider("openrouter", holders, lambda v: door._request("/keys?offset=0", v)))
    except Exception:  # noqa: BLE001
        pass
    try:
        import cloudflare as cf

        def cf_holders():
            rows = [(f"cloudflare/admin/{label}", (lambda p=path: private_io.read(p).strip()))
                    for label, path in cf.admins()]
            rows += [(f"cloudflare/issued/{p.name}", (lambda p=p: private_io.read(p).strip())) for p in cf.issued()]
            return rows

        def cf_ping(value):
            # A token built for one job may not read accounts: refused on both roads is the only invalid.
            try:
                cf._request("/accounts?per_page=1", value)
            except RuntimeError as first:
                if status_of(first) not in REFUSED:
                    raise
                cf._request("/memberships?per_page=1", value)
        out.append(Provider("cloudflare", cf_holders, cf_ping))
    except Exception:  # noqa: BLE001
        pass
    try:
        import digitalocean as do

        def do_holders():
            return [(f"digitalocean/team/{label}", (lambda p=path: private_io.read(p).strip()))
                    for label, path, _meta in do.stashes()]
        out.append(Provider("digitalocean", do_holders, lambda v: do.account_of(v)))
    except Exception:  # noqa: BLE001
        pass
    return out


# ---- the run -------------------------------------------------------------------------------------------

def _read(path: pathlib.Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def run(providers: list[Provider], *, raw_dir: pathlib.Path, registry_dir: pathlib.Path, previous: dict | None,
        seen: dict | None, today: str, offline: bool = False, force: bool = False,
        declared: dict | None = None) -> tuple[dict, dict]:
    degraded: list[dict] = []
    if offline:
        keys = [r for r in (previous or {}).get("keys") or []]
        degraded.append({"source": "integrations", "reason": "offline: no key was asked; the last answer is kept"})
    else:
        keys, bad = check_keys(providers, previous, today, force)
        degraded += bad
    drift, seen = check_drift(raw_dir, declared or COLLECTOR_FIELDS, seen, today)
    rows = untracked(_read(registry_dir / "dependencies.json"), _read(registry_dir / "env-inventory.json"))
    doc = {"schema": SCHEMA, "checked_on": today, "checked_at": now(), "keys": keys, "drift": drift,
           "untracked": rows, "tracked_providers": sorted(TRACKED), "degraded": degraded}
    return doc, seen


def agent_view(*, doc=None, raw_path: pathlib.Path | None = None) -> dict:
    """The stored answer, the invalid first; `degraded` says why when the step has not run."""
    if doc is None:
        import paths
        doc = _read((raw_path or paths.SCRATCH / "integration-health.json"))
    if not doc:
        return {"keys": [], "drift": [], "untracked": [], "checkedOn": None,
                "degraded": [{"source": "integrations", "reason": "the integration health step has not run"}]}
    order = {"invalid": 0, "unreachable": 1, "unreadable": 2, "valid": 3}
    keys = sorted(doc.get("keys") or [], key=lambda r: (order.get(r.get("state"), 4), r.get("slot") or ""))
    return {"checkedOn": doc.get("checked_on"), "keys": keys, "drift": doc.get("drift") or [],
            "untracked": doc.get("untracked") or [], "trackedProviders": doc.get("tracked_providers") or [],
            "degraded": list(doc.get("degraded") or [])}


def main(argv: list[str], providers: list[Provider] | None = None) -> int:
    import atomic
    import configuration
    import paths
    out = paths.SCRATCH / "integration-health.json"
    state_file = paths.STATE / "state" / "integration-fields.json"
    today = datetime.now(timezone.utc).date().isoformat()
    previous = _read(out)
    doc, seen = run(default_providers() if providers is None else providers, raw_dir=paths.SCRATCH,
                    registry_dir=paths.REGISTRY, previous=previous, seen=_read(state_file), today=today,
                    offline=configuration.offline(), force="--force" in argv)
    atomic.write_json(out, doc)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(state_file, seen)
    states = [k["state"] for k in doc["keys"]]
    print(f"integration-health: {len(states)} key(s), {states.count('invalid')} invalid, "
          f"{len(doc['drift'])} drift, {len(doc['untracked'])} untracked; {len(doc['degraded'])} degraded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
