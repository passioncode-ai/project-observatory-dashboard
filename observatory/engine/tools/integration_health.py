#!/usr/bin/env python3
"""Is each provider integration still working, and which providers does the estate use that nothing here watches
(OBS-557, CP-12 and CP-13)?

Three questions, answered once a day into `store/raw/integration-health.json`:

1. **Is each held key still valid, and does it hold the scope its readers need?** Each provider door's own validity
   call (the one its `ping` verb makes), once a UTC day per key. A 401 or 403 is `invalid`; any other failure is
   `unreachable`, which says nothing about the key. Where the provider answers a key's scope (GitHub's
   `X-OAuth-Scopes` header through `gh`, Cloudflare's token verify and the token's policies, Heroku's authorization,
   Sentry's `GET /api/0/`), it is compared with what the collectors and the door's preset need (OBS-605): a scope
   missing is `scope-missing`, one granted beyond the need is `scope-excess`. The record names the slot, never the
   value: a value is read into memory for one request and dropped. No new spend: each call is a read.
2. **Did a provider change the answer a collector relies on?** `COLLECTOR_FIELDS` declares, per collector, the stored
   raw file, the list of rows in it and the fields every healthy row carries. A field that no row carries (absent or
   null in every row of a non-empty list) is `drift`, with the day it was last seen (`store/state/integration-fields.json`).
3. **Which providers does a project use that no door or collector covers?** `PACKAGE_PROVIDERS` and `ENV_PROVIDERS`
   map a dependency or a variable NAME to a provider; applied to `registry/dependencies.json` and the env inventory,
   a provider no door or collector declares (`tools/door_registry.py`, OBS-605) is `untracked`, with the package or
   variable name as evidence, never a value, and a code: `untracked-key` when a variable (a key) points at it,
   `untracked-package` when only a dependency does.

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
    """One provider door: `holders()` lists (slot name, a function reading that key's value) or (slot, reader,
    context); `ping(value)`, or `ping(value, context)` when the holder gave one, raises when the provider refuses or
    cannot be reached and returns its answer when the key works. `scope(slot, value, answer, context)`, when given,
    answers {granted, needed, allowed?} for `judge_scope`, or None where the key has no scope to compare."""
    name: str
    holders: Callable[[], list]
    ping: Callable[..., object]
    scope: Callable[..., dict | None] | None = None


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
        for entry in held:
            slot, read = entry[0], entry[1]
            ctx = entry[2] if len(entry) > 2 else None
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
                answer = p.ping(value) if len(entry) <= 2 else p.ping(value, ctx)
                rec.update(state="valid", status=200)
                if p.scope is not None:
                    rec.update(_scope_of(p, slot, value, answer, ctx, degraded))
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


def _scope_of(p: Provider, slot: str, value: str, answer, ctx, degraded: list) -> dict:
    """{"scope": the judgement} for one key, or {} where the provider has no scope to compare for it. A scope that
    cannot be read is said (`scope-unreadable`, by the exception's type only) and named in `degraded`."""
    try:
        info = p.scope(slot, value, answer, ctx)
    except Exception as exc:  # noqa: BLE001 (named by type: a message may quote what was sent)
        degraded.append({"source": f"integration:{p.name}", "code": "scope-unreadable",
                         "reason": f"the scope of {slot} could not be read: {type(exc).__name__}"})
        return {"scope": {"state": "unread", "code": "scope-unreadable", "reason": type(exc).__name__}}
    return {"scope": judge_scope(info)} if info else {}


def judge_scope(info: dict) -> dict:
    """Compare what a key is granted with what its readers need. `allowed` are scopes granted beyond the need that
    are no finding (what a provider's own sign-in always asks for). `granted` or `needed` unknown is `scope-unknown`:
    the provider did not say, which is not a clean bill."""
    granted, needed = info.get("granted"), info.get("needed")
    out = {"granted": sorted(set(granted)) if granted is not None else None,
           "needed": sorted(set(needed)) if needed is not None else None, "missing": [], "excess": []}
    if granted is None or needed is None:
        return {**out, "state": "unknown", "code": "scope-unknown"}
    out["missing"] = sorted(set(needed) - set(granted))
    out["excess"] = sorted(set(granted) - set(needed) - set(info.get("allowed") or ()))
    if out["missing"]:
        return {**out, "state": "missing", "code": "scope-missing"}
    if out["excess"]:
        return {**out, "state": "excess", "code": "scope-excess"}
    return {**out, "state": "ok", "code": "scope-ok"}


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

def _declared() -> tuple[frozenset, list[dict]]:
    """(the providers a door or a collector of this engine declares, what could not be read): the one registry
    (`tools/door_registry.py`, OBS-605), never a list kept here."""
    import door_registry
    found, degraded = door_registry.declarations()
    return frozenset(d["provider"] for d in found), degraded


#: Providers a door or a collector of this engine covers, as they declare themselves. Anything else a project uses
#: is `untracked`. Read once at import for callers that want the set; `run` reads the declarations afresh.
TRACKED = _declared()[0]
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
                                                     "category": category, "evidence": [],
                                                     "code": "untracked-package"})
        if kind == "env":
            row["code"] = "untracked-key"
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
#
# One builder per provider whose keys are asked. Each takes the door module (or its client) as an argument, so the
# tests pass a fake that records what was asked and nothing reaches a network; `default_providers` passes the real
# one. Every value is read by the door's own reader into memory for one request.

def _file_reader(read=None):
    if read is not None:
        return read
    import private_io
    return lambda path: private_io.read(path).strip()


def openrouter_provider(door, read=None) -> Provider:
    read = _file_reader(read)

    def holders():
        return [(f"openrouter/admin/{label}", (lambda p=path: read(p))) for label, path in door.admins()]
    return Provider("openrouter", holders, lambda v: door._request("/keys?offset=0", v))


def _cloudflare_groups(preset: dict) -> list[str]:
    return [g for k in ("groups", "zone_groups", "account_groups") for g in preset.get(k) or ()]


def cloudflare_provider(cf, read=None) -> Provider:
    """The stashed admin tokens and the tokens the door issued. An issued token's scope: token verify with its own
    value names its id (`/accounts/{id}/tokens/verify`), the admin reads that token's policies, and the permission
    groups granted are compared with the preset's. An admin token's scope is not compared (its need is the door's)."""
    read = _file_reader(read)

    def holders():
        rows = [(f"cloudflare/admin/{label}", (lambda p=path: read(p)), {"kind": "admin"})
                for label, path in cf.admins()]
        rows += [(f"cloudflare/issued/{cf.label_of(p)}", (lambda p=p: read(p)), {"kind": "issued", "path": p})
                 for p in cf.issued()]
        return rows

    def ping(value, ctx=None):
        # A token built for one job may not read accounts: refused on both roads is the only invalid.
        try:
            cf._request("/accounts?per_page=1", value)
        except RuntimeError as first:
            if status_of(first) not in REFUSED:
                raise
            cf._request("/memberships?per_page=1", value)

    def scope(slot, value, answer, ctx):
        if (ctx or {}).get("kind") != "issued":
            return None
        path = ctx["path"]
        meta = cf.read_meta(path)
        preset = cf.PRESETS.get(meta.get("preset") or "")
        account = meta.get("account_id")
        if not preset or not account:
            return {"granted": None, "needed": None}
        verified = (cf._request(f"/accounts/{account}/tokens/verify", value) or {}).get("result") or {}
        token_id = verified.get("id")
        if not token_id:
            return {"granted": None, "needed": _cloudflare_groups(preset)}
        _label, admin = cf.read_admin(meta.get("stash") or path.name)
        token = (cf._request(f"/accounts/{account}/tokens/{token_id}", admin) or {}).get("result") or {}
        policies = token.get("policies") if isinstance(token.get("policies"), list) else None
        granted = None if policies is None else [
            g.get("name") for pol in policies if isinstance(pol, dict)
            for g in pol.get("permission_groups") or [] if isinstance(g, dict) and g.get("name")]
        return {"granted": granted, "needed": _cloudflare_groups(preset)}
    return Provider("cloudflare", holders, ping, scope)


def digitalocean_provider(do, read=None) -> Provider:
    read = _file_reader(read)

    def holders():
        return [(f"digitalocean/team/{label}", (lambda p=path: read(p))) for label, path, _meta in do.stashes()]
    return Provider("digitalocean", holders, lambda v: do.account_of(v))


def heroku_provider(hk, slot_value=None, read=None) -> Provider:
    """The stashed admin tokens and every authorization the door issued (its ledger; the value read from its vault
    slot). Valid: `GET /account`, which every scope reads. An issued authorization's scope: the admin reads the
    authorization (`GET /oauth/authorizations/{id}`), compared with the preset's scopes."""
    read = _file_reader(read)
    if slot_value is None:
        import key_consumers
        slot_value = key_consumers.vault_value

    def from_slot(slot):
        value = slot_value(*slot.split("/"))
        if not value:
            raise LookupError("the slot holds no value")
        return value

    def holders():
        rows = [(f"heroku/admin/{label}", (lambda p=path: read(p)), {"kind": "admin"})
                for label, path in hk.admins()]
        for slot, row in sorted((hk.ledger().get("issued") or {}).items()):
            rows.append((f"heroku/issued/{slot}", (lambda s=slot: from_slot(s)), {"kind": "issued", "row": row}))
        return rows

    def scope(slot, value, answer, ctx):
        if (ctx or {}).get("kind") != "issued":
            return None
        row = ctx["row"]
        preset = hk.PRESETS.get(row.get("preset") or "")
        if not preset or not row.get("authorization_id"):
            return {"granted": None, "needed": None}
        _label, admin = hk.read_admin(row.get("admin"))
        auth = hk._request("GET", f"/oauth/authorizations/{hk.q(row['authorization_id'])}", admin) or {}
        granted = auth.get("scope") if isinstance(auth.get("scope"), list) else None
        return {"granted": granted, "needed": list(preset["scopes"])}
    return Provider("heroku", holders, lambda v, ctx=None: hk._request("GET", "/account", v), scope)


def sentry_provider(sn) -> Provider:
    """Every narrow token the door issued or installed. A slot's value is sent only while it is the value the ledger
    proved (`_narrow_value_ok`, the door's own rule): otherwise it is `unreadable` and nothing is sent. Valid and its
    scope in one call: `GET /api/0/` answers the token's scopes, compared with the preset's exact scopes."""
    def holders():
        rows = []
        for slot, row in sorted((sn.ledger().get("issued") or {}).items()):
            def reader(s=slot, r=row):
                value = sn.read_slot(*s.split("/"))
                sn._narrow_value_ok(s, r, value)
                return value
            rows.append((f"sentry/issued/{slot}", reader, {"base": row.get("base_url"), "preset": row.get("preset")}))
        return rows

    def scope(slot, value, answer, ctx):
        preset = sn.PRESETS.get((ctx or {}).get("preset") or "")
        return {"granted": answer if isinstance(answer, list) else None,
                "needed": list(preset["scopes"]) if preset else None}
    return Provider("sentry", holders, lambda v, ctx: sn.scopes_of(v, ctx["base"]), scope)


#: What the collectors and the issue filer read through the `gh` session: repositories, private ones included, and
#: their issues (`repo`), and the organizations a person belongs to (`read:org`).
GITHUB_NEEDS = ("read:org", "repo")
#: What `gh auth login` asks for by default beside them: granted by every session, so not a finding.
GITHUB_ALLOWED = ("gist", "workflow")
_STATUS_LINE = re.compile(r"^HTTP/[\d.]+ (\d{3})")


def github_provider(run=None, which=None) -> Provider:
    """The `gh` session the GitHub collector and the issue filer use, asked through `gh` itself (the existing
    client): the token never enters this process. `gh api -i user` answers the status line and the session's
    `X-OAuth-Scopes` header; a fine-grained token answers no such header, which is `scope-unknown`."""
    import shutil
    import subprocess
    run = run or subprocess.run
    which = which or shutil.which

    def holders():
        return [("github/gh-session", lambda: "gh-session")] if which("gh") else []

    def ping(_value):
        done = run(["gh", "api", "-i", "user"], capture_output=True, text=True, timeout=60)
        lines = (done.stdout or "").splitlines()
        m = _STATUS_LINE.match(lines[0]) if lines else None
        code = int(m.group(1)) if m else status_of(RuntimeError(done.stderr or ""))
        if done.returncode != 0 or code is None or code >= 400:
            if code is not None:
                raise RuntimeError(f"github answered HTTP {code} through gh")
            raise RuntimeError("github unreachable through gh")
        scopes = None
        for line in lines[1:]:
            if not line.strip():
                break
            name, _, rest = line.partition(":")
            if name.strip().lower() == "x-oauth-scopes":
                scopes = [s.strip() for s in rest.split(",") if s.strip()]
        return {"scopes": scopes}

    def scope(slot, value, answer, ctx):
        return {"granted": (answer or {}).get("scopes"), "needed": list(GITHUB_NEEDS), "allowed": list(GITHUB_ALLOWED)}
    return Provider("github", holders, ping, scope)


#: provider -> how its door is opened and wrapped. A door declared in the registry with no builder here is named in
#: the answer's `keys_not_asked`.
BUILDERS: dict[str, Callable[[], Provider]] = {
    "openrouter": lambda: openrouter_provider(__import__("openrouter")),
    "cloudflare": lambda: cloudflare_provider(__import__("cloudflare")),
    "digitalocean": lambda: digitalocean_provider(__import__("digitalocean")),
    "heroku": lambda: heroku_provider(__import__("heroku")),
    "sentry": lambda: sentry_provider(__import__("sentry")),
    "github": lambda: github_provider(),
}


def default_providers(degraded: list | None = None) -> list[Provider]:
    """Every provider with a builder: OpenRouter, Cloudflare, DigitalOcean, Heroku, Sentry and the `gh` session. A
    door that cannot be imported is left out and named in `degraded` (`door-unavailable`)."""
    out: list[Provider] = []
    for name, build in BUILDERS.items():
        try:
            out.append(build())
        except Exception as exc:  # noqa: BLE001
            if degraded is not None:
                degraded.append({"source": f"integration:{name}", "code": "door-unavailable",
                                 "reason": f"the {name} door could not be opened: {type(exc).__name__}"})
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
        declared: dict | None = None, degraded: list | None = None) -> tuple[dict, dict]:
    degraded = list(degraded or [])
    tracked, unread = _declared()
    degraded += unread
    if offline:
        keys = [r for r in (previous or {}).get("keys") or []]
        degraded.append({"source": "integrations", "reason": "offline: no key was asked; the last answer is kept"})
    else:
        keys, bad = check_keys(providers, previous, today, force)
        degraded += bad
    drift, seen = check_drift(raw_dir, declared or COLLECTOR_FIELDS, seen, today)
    rows = untracked(_read(registry_dir / "dependencies.json"), _read(registry_dir / "env-inventory.json"), tracked)
    import door_registry
    asked = {p.name for p in providers}
    doc = {"schema": SCHEMA, "checked_on": today, "checked_at": now(), "keys": keys, "drift": drift,
           "untracked": rows, "tracked_providers": sorted(tracked),
           "keys_not_asked": sorted(set(door_registry.doors()) - asked), "degraded": degraded}
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
            "keysNotAsked": doc.get("keys_not_asked") or [], "degraded": list(doc.get("degraded") or [])}


def main(argv: list[str], providers: list[Provider] | None = None) -> int:
    import atomic
    import configuration
    import paths
    out = paths.SCRATCH / "integration-health.json"
    state_file = paths.STATE / "state" / "integration-fields.json"
    today = datetime.now(timezone.utc).date().isoformat()
    previous = _read(out)
    unopened: list[dict] = []
    doc, seen = run(default_providers(unopened) if providers is None else providers, raw_dir=paths.SCRATCH,
                    registry_dir=paths.REGISTRY, previous=previous, seen=_read(state_file), today=today,
                    offline=configuration.offline(), force="--force" in argv, degraded=unopened)
    atomic.write_json(out, doc)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(state_file, seen)
    states = [k["state"] for k in doc["keys"]]
    print(f"integration-health: {len(states)} key(s), {states.count('invalid')} invalid, "
          f"{len(doc['drift'])} drift, {len(doc['untracked'])} untracked; {len(doc['degraded'])} degraded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
