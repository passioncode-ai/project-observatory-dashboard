"""Which organization a project belongs to, and where its accounts must live.

WHY. An estate that serves two owners — a company and a person — keeps each
owner's analytics, design files and clouds in that owner's accounts. Nothing
measured says which owner a project has, so every agent that creates a Google
Analytics property or a Figma file had to be TOLD, and a destination recalled
from a conversation is a destination that drifts: on 2026-09-27 a property sat
in the company's account for a product that belongs to the person, and was
moved by hand. This file makes the split a registry field every reader gets —
the dashboard, `observatory_project`, the SessionStart line — and makes the
destination derived from it rather than remembered.

THE SHAPE, `config/organizations.json` (absent or empty: no field is emitted,
so a workspace that never configures this sees no change):

    {"organizations": {
        "<name>": {"label": "...", "default": true|false,
                   "ga4_account": "accounts/<n>",
                   "ga4_legacy_accounts": ["accounts/<n>"],
                   "figma": {"team": "...", "project": "..."},
                   "match": {"repository_owners": ["..."], "products": ["product:..."],
                             "digitalocean_teams": ["<team uuid or name>"],
                             "cloudflare_accounts": ["<account id>"],
                             "heroku_teams": ["<team name>"]}}},
     "projects": {"project:<slug>": {"organization": "<name>", "why": "..."}}}

THE ORDER, first answer wins:
  1. declared  — `projects` here, or `organization` in project_overrides.json
                 (where an accepted proposal lands);
  2. rule      — a repository owner or a product membership named under `match`;
                 two organizations matching is a CONFLICT, reported, never guessed;
  3. external  — every repository owner is outside the estate (ownership.json):
                 a clone of somebody else's code has no accounts of ours;
  4. default   — the organization marked `default`.

PROVIDER ACCOUNTS (OBS-412 part 1; rule ORG-R04 of the organization onboarding design). A provider
account named under `match` is a rule of step 2 too, beside a repository owner and a product. `digitalocean_teams`,
`cloudflare_accounts` and `heroku_teams` name accounts, never projects. A project matches one only
through a resource the registry ALREADY attributes to it: a DigitalOcean app or a Cloudflare Pages
project or zone whose `project` the infra documents measured (a repository or a site the registry
holds), a Heroku app `heroku_registry` linked by its rule. A name is never evidence (rule 2). The
account itself carries its organization in its own document (`organization` on the account rows of
`registry/digitalocean-account.json` and `registry/cloudflare-account.json`, `team_organizations` in
`registry/heroku-apps.json`): an account two organizations claim is shared and belongs to neither.
"""
from __future__ import annotations

import json

import paths

CONFIG = "organizations.json"
SOURCES = ("declared", "rule", "external", "default", "conflict")
#: `match` rules on provider accounts: the rule's name -> (provider, the account's word in a reason).
ACCOUNT_RULES = {"digitalocean_teams": ("digitalocean", "DigitalOcean team"),
                 "cloudflare_accounts": ("cloudflare", "Cloudflare account"),
                 "heroku_teams": ("heroku", "Heroku team")}
MATCH_RULES = ("repository_owners", "products", *ACCOUNT_RULES)


def load() -> dict:
    try:
        doc = json.loads(paths.config_file(CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def validate(doc: dict) -> list[str]:
    """Problems with the file, named; an empty list means usable."""
    out: list[str] = []
    orgs = doc.get("organizations") or {}
    if not isinstance(orgs, dict):
        return ["organizations must be an object keyed by name"]
    defaults = [n for n, o in orgs.items() if isinstance(o, dict) and o.get("default") is True]
    if orgs and len(defaults) != 1:
        out.append(f"exactly one organization must be `default`; found {len(defaults)}")
    for name, org in orgs.items():
        if not isinstance(org, dict):
            out.append(f"{name}: must be an object")
            continue
        acct = org.get("ga4_account")
        if acct is not None and not (isinstance(acct, str) and acct.startswith("accounts/")):
            out.append(f"{name}: ga4_account must look like accounts/<number>")
        match = org.get("match") or {}
        for key in match:
            if key not in MATCH_RULES or not isinstance(match[key], list):
                out.append(f"{name}: match.{key} is not a known list rule")
    for pid, row in (doc.get("projects") or {}).items():
        if not isinstance(row, dict) or row.get("organization") not in orgs and row.get("organization") != "external":
            out.append(f"{pid}: organization must name a configured organization or `external`")
        elif not row.get("why"):
            out.append(f"{pid}: a declared organization needs a `why`")
    return out


def _estate_owners() -> set[str]:
    try:
        doc = json.loads(paths.config_file("ownership.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {o.lower() for o in (doc.get("organizations") or []) + (doc.get("work_organizations") or [])}


def product_index() -> dict[str, set[str]]:
    """project id -> the curated products it is a member of."""
    try:
        doc = json.loads(paths.config_file("products.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, set[str]] = {}
    for prod_id, prod in (doc.get("products") or {}).items():
        for member in (prod.get("members") or {}):
            out.setdefault(member, set()).add(prod_id)
    return out


def _usable_orgs(orgs) -> tuple[dict, list[str]]:
    """The organizations with only the `match` rules that can be read: a rule whose value is not a list
    of strings (a string iterates as its letters) or that no engine knows is dropped and named, so it
    matches nothing instead of matching by accident (review of #479)."""
    if not isinstance(orgs, dict):
        return {}, ["organizations must be an object keyed by name"]
    out, problems = {}, []
    for name, org in orgs.items():
        if not isinstance(org, dict):
            problems.append(f"{name}: must be an object")
            continue
        match = org.get("match") if isinstance(org.get("match"), dict) else {}
        kept = {}
        for key, value in match.items():
            if key in MATCH_RULES and isinstance(value, list) and all(isinstance(v, str) for v in value):
                kept[key] = value
            else:
                problems.append(f"{name}: match.{key} is not a known list rule; ignored")
        out[name] = {**org, "match": kept}
    return out, problems


class Assigner:
    """Loaded once per emit; `assign` is then a pure function of one project."""

    def __init__(self, doc: dict | None = None, estate_owners: set[str] | None = None,
                 products: dict[str, set[str]] | None = None):
        self.doc = load() if doc is None else doc
        self.orgs, self.problems = _usable_orgs(self.doc.get("organizations") or {})
        self.declared = self.doc.get("projects") or {}
        self.estate = _estate_owners() if estate_owners is None else {o.lower() for o in estate_owners}
        self.products = product_index() if products is None else products
        self.default = next((n for n, o in self.orgs.items() if isinstance(o, dict) and o.get("default") is True), None)

    @property
    def active(self) -> bool:
        return bool(self.orgs)

    def _account_hits(self, provider: str, keys: list[str]) -> list[str]:
        """The organizations whose `match` names this provider account by any of its keys."""
        wanted = {str(k).lower() for k in keys if k}
        out = []
        for name, org in self.orgs.items():
            match = (org or {}).get("match") or {}
            for rule, (prov, _word) in ACCOUNT_RULES.items():
                if prov == provider and wanted & {str(v).lower() for v in match.get(rule) or []}:
                    out.append(name)
                    break
        return sorted(out)

    def account(self, provider: str, keys: list[str]) -> dict:
        """The organization of one provider account, for its own row: `rule` when one organization's
        `match` names it, `conflict` (shared, ORG-R04) when two do, `none` when no rule names it. The
        default organization is never assumed for an account: an unnamed account is undecided."""
        hits = self._account_hits(provider, keys)
        if len(hits) == 1:
            return {"organization": hits[0], "organization_source": "rule",
                    "organization_why": f"match.{_rule_of(provider)} names it"}
        if hits:
            return {"organization": None, "organization_source": "conflict",
                    "organization_why": "shared: claimed by " + " and ".join(hits)}
        return {"organization": None, "organization_source": "none",
                "organization_why": f"no organization's match.{_rule_of(provider)} names it"}

    def assign(self, project: dict, accounts: list[dict] | None = None) -> dict | None:
        """`accounts`: the provider accounts the registry attributes to this project, each
        {"provider", "keys", "label", "via"} (`project_accounts`)."""
        if not self.active:
            return None
        pid = project["id"]
        if project.get("organization"):
            return {"organization": project["organization"], "source": "declared",
                    "why": "project_overrides.json"}
        row = self.declared.get(pid)
        if isinstance(row, dict) and row.get("organization"):
            return {"organization": row["organization"], "source": "declared", "why": row.get("why", "")}
        owners = {o.lower() for o in project.get("owners") or []}
        member_of = self.products.get(pid, set())
        hits: dict[str, list[str]] = {}
        for name, org in self.orgs.items():
            match = (org or {}).get("match") or {}
            by_owner = sorted(owners & {o.lower() for o in match.get("repository_owners") or []})
            by_product = sorted(member_of & set(match.get("products") or []))
            reasons = [f"repository owner {o}" for o in by_owner] + [f"product {p}" for p in by_product]
            if reasons:
                hits[name] = reasons
        for acct in accounts or []:
            for name in self._account_hits(acct.get("provider", ""), acct.get("keys") or []):
                reason = f"{ACCOUNT_RULES[_rule_of(acct['provider'])][1]} {acct.get('label')} ({acct.get('via')})"
                if reason not in hits.setdefault(name, []):
                    hits[name].append(reason)
        if len(hits) == 1:
            (name, reasons), = hits.items()
            return {"organization": name, "source": "rule", "why": "; ".join(reasons)}
        if len(hits) > 1:
            return {"organization": None, "source": "conflict",
                    "why": " vs ".join(f"{n} ({'; '.join(r)})" for n, r in sorted(hits.items()))}
        if owners and not owners & self.estate:
            return {"organization": "external", "source": "external",
                    "why": "every repository owner is outside the estate: " + ", ".join(sorted(owners))}
        if self.default:
            return {"organization": self.default, "source": "default",
                    "why": "no rule matched" + ("" if owners else "; no repository")}
        return None

    def destinations(self, organization: str | None) -> dict:
        """Where this organization's accounts are: what an agent creating one needs."""
        org = self.orgs.get(organization or "") or {}
        out = {}
        if org.get("label"):
            out["label"] = org["label"]
        if org.get("ga4_account"):
            out["ga4Account"] = org["ga4_account"]
        figma = org.get("figma") or {}
        if figma.get("team"):
            out["figmaTeam"] = figma["team"]
        if figma.get("project"):
            out["figmaProject"] = figma["project"]
        return out


def _rule_of(provider: str) -> str:
    return next(rule for rule, (prov, _w) in ACCOUNT_RULES.items() if prov == provider)


def account_keys(provider: str, accounts: list[dict]) -> dict[str, list[str]]:
    """account row id -> what a `match` rule may name it by: a DigitalOcean team by its uuid, and by its
    name only when no other team here bears that name; a Cloudflare account by its id."""
    rows = [a for a in accounts or [] if isinstance(a, dict)]
    if provider == "digitalocean":
        names: dict[str, int] = {}
        for a in rows:
            if a.get("name"):
                names[str(a["name"]).lower()] = names.get(str(a["name"]).lower(), 0) + 1
        return {a.get("id"): [str(k) for k in (a.get("team_uuid"),
                                              a.get("name") if names.get(str(a.get("name") or "").lower()) == 1
                                              else None) if k]
                for a in rows}
    return {a.get("id"): [str(a["account_id"])] if a.get("account_id") else [] for a in rows}


def annotate_accounts(assigner: "Assigner", provider: str, accounts: list[dict]) -> list[str]:
    """Write each account row's organization (`Assigner.account`) and return warnings: a DigitalOcean
    team name a rule uses that two teams here bear decides nothing; the rule needs the team's uuid."""
    keys = account_keys(provider, accounts)
    for a in accounts or []:
        if isinstance(a, dict):
            a.update(assigner.account(provider, keys.get(a.get("id")) or []))
    warnings = []
    if provider == "digitalocean":
        names: dict[str, int] = {}
        for a in accounts or []:
            if isinstance(a, dict) and a.get("name"):
                names[str(a["name"]).lower()] = names.get(str(a["name"]).lower(), 0) + 1
        for org, body in sorted(assigner.orgs.items()):
            for value in (body.get("match") or {}).get("digitalocean_teams") or []:
                if names.get(value.lower(), 0) > 1:
                    warnings.append(f"{org}: match.digitalocean_teams names {value!r}, the name of "
                                    f"{names[value.lower()]} teams here; it decides nothing, name the team's uuid")
    return warnings


def project_accounts(digitalocean: dict | None = None, cloudflare: dict | None = None,
                     heroku_apps: list[dict] | None = None) -> dict[str, list[dict]]:
    """project id -> the provider accounts the registry attributes to it, through the resources the
    infra documents and heroku-apps.json already tied to it (a measured link with its rule). Each entry:
    {"provider", "keys" (what a `match` rule may name), "label", "via" (the resource)}. Deterministic."""
    out: dict[str, list[dict]] = {}

    def add(pid, entry):
        rows = out.setdefault(pid, [])
        if not any(r["provider"] == entry["provider"] and r["keys"] == entry["keys"] for r in rows):
            rows.append(entry)

    for provider, doc, key_field in (("digitalocean", digitalocean, "team_uuid"),
                                     ("cloudflare", cloudflare, "account_id")):
        rows = [a for a in (doc or {}).get("accounts") or [] if isinstance(a, dict)]
        accounts, keys = {a.get("id"): a for a in rows}, account_keys(provider, rows)
        for r in sorted((doc or {}).get("resources") or [], key=lambda r: str(r.get("id"))):
            acct = accounts.get(r.get("account_ref"))
            if not (r.get("project") and r.get("link_rule") and acct):
                continue
            add(r["project"], {"provider": provider, "keys": keys.get(acct.get("id")) or [],
                               "label": acct.get("name") or acct.get(key_field),
                               "via": f"{r.get('kind')} {r.get('name')}"})
    for a in sorted(heroku_apps or [], key=lambda a: str(a.get("id"))):
        if a.get("project") and a.get("link_rule") and a.get("team") and a.get("team") != "personal":
            add(a["project"], {"provider": "heroku", "keys": [str(a["team"])], "label": a["team"],
                               "via": f"app {a.get('name')}"})
    return out


def reassign(assigner: "Assigner", projects: list[dict], accounts: dict[str, list[dict]]) -> list[str]:
    """Assign again, with the provider accounts, every project whose organization was not declared;
    the ids whose answer changed. Called once the infra documents exist, after the projects."""
    changed = []
    if not assigner.active:
        return changed
    fields = ("organization", "organization_source", "organization_why")
    for e in projects:
        if e.get("organization_source") == "declared" or not accounts.get(e["id"]):
            continue
        got = assigner.assign({k: v for k, v in e.items() if k not in fields}, accounts[e["id"]])
        if got and (got["organization"], got["source"], got["why"]) != tuple(e.get(f) for f in fields):
            e.update(organization=got["organization"], organization_source=got["source"],
                     organization_why=got["why"])
            changed.append(e["id"])
    return changed


#: What a project resource may be. A closed list, so the dashboard, the GA4 check
#: and every agent use one vocabulary; "other" exists so nothing goes unrecorded
#: for want of a word.
RESOURCE_KINDS = ("ga4-property", "analytics-tracker", "firebase-project", "gcp-project",
                  "cloud-account", "server", "database", "dns-zone", "figma-file",
                  "payment-account", "app-store", "other")
RESOURCE_FIELDS = {"kind", "identifier", "account", "url", "note", "added_on", "added_by"}


def resource_problem(row: object) -> str:
    """Why one resource row is not recordable, or ""."""
    if not isinstance(row, dict):
        return "each resource must be an object"
    unknown = sorted(set(row) - RESOURCE_FIELDS)
    if unknown:
        return f"unknown resource field(s): {', '.join(unknown)}; allowed: {', '.join(sorted(RESOURCE_FIELDS))}"
    if row.get("kind") not in RESOURCE_KINDS:
        return f"resource kind must be one of: {', '.join(RESOURCE_KINDS)}"
    if not isinstance(row.get("identifier"), str) or not row["identifier"].strip():
        return "a resource needs a non-empty `identifier` (property id, project id, host, file key…)"
    for key in ("account", "url", "note", "added_on", "added_by"):
        if key in row and not isinstance(row[key], str):
            return f"resource `{key}` must be a string"
    return ""


def merge_resources(current: list, incoming: list) -> list:
    """Append-with-update keyed by (kind, identifier): accepting a proposal adds a
    resource; it never drops the ones another proposal added before it."""
    out = [dict(r) for r in current if isinstance(r, dict)]
    index = {(r.get("kind"), r.get("identifier")): i for i, r in enumerate(out)}
    for row in incoming:
        key = (row.get("kind"), row.get("identifier"))
        if key in index:
            out[index[key]].update(row)
        else:
            index[key] = len(out)
            out.append(dict(row))
    return sorted(out, key=lambda r: (r.get("kind", ""), r.get("identifier", "")))
