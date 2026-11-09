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
                             "github_owners": ["..."],
                             "digitalocean_teams": ["<team uuid or name>"],
                             "cloudflare_accounts": ["<account id>"],
                             "heroku_teams": ["<team name>"],
                             "openrouter_accounts": ["<the label the door stashed it as>"]}}},
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

THE REST OF THE ACCOUNT RULES (OBS-412 part 2). `github_owners` is `repository_owners` under the name the
organization onboarding design gives it (ORG-W2): both are read, for a repository owner of any host, and an owner
named under both counts once. `openrouter_accounts` names an OpenRouter account by the label its provisioning key
was stashed as (`openrouter.py stash --as LABEL`; OpenRouter itself names no account). It decides that ACCOUNT's
organization (`full org accounts`) and the issuer side of the keys it minted (`full org keys`, ORG-R06), never a
project's: rule ORG-R26 builds a project's proposal from its repositories, deploys and zones, and a key is weighed
per key, so one personal key in an organization's project does not make the project ambiguous.

ONE READING FOR EVERY READER (rule ORG-R28). The session-start line, the analytics findings and the project view
read a project's answer through `reading()`, which names an organization's accounts only for a DECIDED answer
(declared, rule, default). Any other source (`conflict` today, `proposed` once a joined organization's rule only
proposes, OBS-413) names none of them: a source this file does not know is never taken for a decision.
"""
from __future__ import annotations

import json

import paths

CONFIG = "organizations.json"
SOURCES = ("declared", "rule", "external", "default", "conflict")
#: The sources a reader may act on: an organization's analytics account and design team are named only for these.
DECIDED = ("declared", "rule", "default")
#: `match` rules on provider accounts: the rule's name -> (provider, the account's word in a reason).
ACCOUNT_RULES = {"digitalocean_teams": ("digitalocean", "DigitalOcean team"),
                 "cloudflare_accounts": ("cloudflare", "Cloudflare account"),
                 "heroku_teams": ("heroku", "Heroku team"),
                 "openrouter_accounts": ("openrouter", "OpenRouter account")}
#: `match` rules on repository owners: `github_owners` is the onboarding design's name for `repository_owners`.
OWNER_RULES = ("repository_owners", "github_owners")
MATCH_RULES = (*OWNER_RULES, "products", *ACCOUNT_RULES)
#: Providers whose accounts a rule names but whose resources never decide a project (see the docstring).
ACCOUNT_ONLY = ("openrouter",)
#: Every provider an ACCOUNT_RULES rule names.
PROVIDERS = tuple(prov for prov, _word in ACCOUNT_RULES.values())


def load() -> dict:
    try:
        doc = json.loads(paths.config_file(CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def load_problem() -> str | None:
    """Why `organizations.json` exists and is not read (`load` then answers {}), or None: absent or read."""
    path = paths.config_file(CONFIG)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"
    except ValueError as exc:
        return f"not JSON (line {getattr(exc, 'lineno', '?')})"
    return None if isinstance(doc, dict) else "not a JSON object"


def validate(doc: dict) -> list[str]:
    """Problems with the file, named; an empty list means usable."""
    out: list[str] = []
    if not isinstance(doc, dict):
        return ["the file must be one JSON object"]
    orgs = doc.get("organizations") or {}
    if not isinstance(orgs, dict):
        return ["organizations must be an object keyed by name"]
    defaults = [n for n, o in orgs.items() if isinstance(o, dict) and o.get("default") is True]
    if orgs and len(defaults) != 1:
        out.append(f"exactly one organization must be `default`; found {len(defaults)}")
    for name, org in orgs.items():
        if name == "external":
            out.append("external: the name is reserved for Not ours (a project whose owners are all outside the "
                       "estate); name the organization otherwise")
        if not isinstance(org, dict):
            out.append(f"{name}: must be an object")
            continue
        if org.get("figma") is not None and not isinstance(org.get("figma"), dict):
            out.append(f"{name}: figma must be an object (team, project)")
        if org.get("ga4_legacy_accounts") is not None and not isinstance(org.get("ga4_legacy_accounts"), dict):
            out.append(f"{name}: ga4_legacy_accounts must be an object (account -> its properties)")
        if org.get("label") is not None and not isinstance(org.get("label"), str):
            out.append(f"{name}: label must be a string")
        acct = org.get("ga4_account")
        if acct is not None and not (isinstance(acct, str) and acct.startswith("accounts/")):
            out.append(f"{name}: ga4_account must look like accounts/<number>")
        match = org.get("match") or {}
        if not isinstance(match, dict):
            out.append(f"{name}: match must be an object of rules ({', '.join(MATCH_RULES)})")
            continue
        for key in match:
            if key not in MATCH_RULES:
                out.append(f"{name}: match.{key} is not a known list rule; the known rules are "
                           + ", ".join(MATCH_RULES))
            elif not isinstance(match[key], list) or not all(isinstance(v, str) for v in match[key]):
                out.append(f"{name}: match.{key} must be a list of strings")
    declared = doc.get("projects") or {}
    if not isinstance(declared, dict):
        return out + ["projects must be an object keyed by project id"]
    for pid, row in declared.items():
        named = row.get("organization") if isinstance(row, dict) else None
        if not isinstance(named, str) or named not in orgs and named != "external":
            out.append(f"{pid}: organization must name a configured organization or `external`")
        elif not row.get("why"):
            out.append(f"{pid}: a declared organization needs a `why`")
    return out


def _estate_owners() -> set[str]:
    try:
        doc = json.loads(paths.config_file("ownership.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(doc, dict):
        return set()
    # A hand-edited file may hold a string where a list belongs: only listed strings are owners.
    return {o.lower() for key in ("organizations", "work_organizations")
            for o in (doc.get(key) if isinstance(doc.get(key), list) else []) if isinstance(o, str)}


def product_index() -> dict[str, set[str]]:
    """project id -> the curated products it is a member of."""
    try:
        doc = json.loads(paths.config_file("products.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, set[str]] = {}
    products = doc.get("products") if isinstance(doc, dict) else None
    for prod_id, prod in (products.items() if isinstance(products, dict) else ()):
        members = prod.get("members") if isinstance(prod, dict) else None
        for member in (members if isinstance(members, (dict, list)) else ()):
            if isinstance(member, str):
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
            elif key not in MATCH_RULES:
                problems.append(f"{name}: match.{key} is not a known list rule; ignored (known: {', '.join(MATCH_RULES)})")
            else:
                problems.append(f"{name}: match.{key} must be a list of strings; ignored")
        out[name] = {**org, "match": kept}
    return out, problems


class Assigner:
    """Loaded once per emit; `assign` is then a pure function of one project."""

    def __init__(self, doc: dict | None = None, estate_owners: set[str] | None = None,
                 products: dict[str, set[str]] | None = None):
        self.doc = load() if doc is None else doc
        self.orgs, self.problems = _usable_orgs(self.doc.get("organizations") or {})
        declared = self.doc.get("projects") or {}
        self.declared = declared if isinstance(declared, dict) else {}
        if not isinstance(declared, dict):
            self.problems.append("projects must be an object keyed by project id; ignored")
        self.estate = _estate_owners() if estate_owners is None else {o.lower() for o in estate_owners}
        self.products = product_index() if products is None else products
        self.default = next((n for n, o in self.orgs.items() if isinstance(o, dict) and o.get("default") is True), None)

    @property
    def active(self) -> bool:
        return bool(self.orgs)

    def _rule_hits(self, provider: str, keys: list[str]) -> dict[str, list[str]]:
        """organization -> the `match` rules that name this account by any of its keys. A repository owner
        (`provider` "owner") is named by `repository_owners` or `github_owners`; any other provider by its
        rule in ACCOUNT_RULES."""
        wanted = {str(k).lower() for k in keys if k}
        rules = OWNER_RULES if provider == "owner" else [r for r, (p, _w) in ACCOUNT_RULES.items() if p == provider]
        out: dict[str, list[str]] = {}
        for name, org in self.orgs.items():
            match = (org or {}).get("match") or {}
            named = [r for r in rules if wanted & {str(v).lower() for v in match.get(r) or []}]
            if named:
                out[name] = named
        return out

    def _account_hits(self, provider: str, keys: list[str]) -> list[str]:
        """The organizations whose `match` names this provider account by any of its keys."""
        return sorted(self._rule_hits(provider, keys))

    def account(self, provider: str, keys: list[str]) -> dict:
        """The organization of one provider account, for its own row: `rule` when one organization's
        `match` names it, `conflict` (shared, ORG-R04) when two do, `none` when no rule names it. The
        default organization is never assumed for an account: an unnamed account is undecided.
        `provider` "owner" asks about a repository owner (`repository_owners`, `github_owners`)."""
        hits = self._rule_hits(provider, keys)
        if len(hits) == 1:
            (name, rules), = hits.items()
            return {"organization": name, "organization_source": "rule",
                    "organization_why": " and ".join(f"match.{r}" for r in rules) + " names it"}
        if hits:
            return {"organization": None, "organization_source": "conflict",
                    "organization_why": "shared: claimed by " + " and ".join(sorted(hits))}
        rules = OWNER_RULES if provider == "owner" else (_rule_of(provider),)
        return {"organization": None, "organization_source": "none",
                "organization_why": f"no organization's match.{' or match.'.join(rules)} names it"}

    def assign(self, project: dict, accounts: list[dict] | None = None) -> dict | None:
        """`accounts`: the provider accounts the registry attributes to this project, each
        {"provider", "keys", "label", "via"} (`project_accounts`)."""
        if not self.active:
            return None
        pid = project["id"]
        if project.get("organization") and isinstance(project["organization"], str):
            return {"organization": project["organization"], "source": "declared",
                    "why": "project_overrides.json"}
        row = self.declared.get(pid)
        # A declaration names an organization by its name; anything else is not one, and `validate` says so.
        if isinstance(row, dict) and row.get("organization") and isinstance(row["organization"], str):
            return {"organization": row["organization"], "source": "declared", "why": str(row.get("why", ""))}
        owners = {o.lower() for o in project.get("owners") or []}
        hits = self.hits(project, accounts)
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

    def hits(self, project: dict, accounts: list[dict] | None = None) -> dict[str, list[str]]:
        """organization -> the reasons its rules match this project, from ALL its accounts together
        (ORG-R26): its repository owners, its products, and the provider accounts its resources sit in.
        One organization is the rule's answer; two are a conflict, named and decided by nobody but a person."""
        owners = {o.lower() for o in project.get("owners") or []}
        member_of = self.products.get(project.get("id"), set())
        hits: dict[str, list[str]] = {}
        for name, org in self.orgs.items():
            match = (org or {}).get("match") or {}
            named = {o.lower() for rule in OWNER_RULES for o in match.get(rule) or []}
            by_owner = sorted(owners & named)
            by_product = sorted(member_of & set(match.get("products") or []))
            reasons = [f"repository owner {o}" for o in by_owner] + [f"product {p}" for p in by_product]
            if reasons:
                hits[name] = reasons
        for acct in accounts or []:
            if acct.get("provider") in ACCOUNT_ONLY:
                continue
            for name in self._account_hits(acct.get("provider", ""), acct.get("keys") or []):
                reason = f"{ACCOUNT_RULES[_rule_of(acct['provider'])][1]} {acct.get('label')} ({acct.get('via')})"
                if reason not in hits.setdefault(name, []):
                    hits[name].append(reason)
        return hits

    def destinations(self, organization: str | None) -> dict:
        """Where this organization's accounts are: what an agent creating one needs."""
        org = (self.orgs.get(organization) if isinstance(organization, str) else None) or {}
        org = org if isinstance(org, dict) else {}
        out = {}
        if isinstance(org.get("label"), str) and org["label"]:
            out["label"] = org["label"]
        if org.get("ga4_account"):
            out["ga4Account"] = org["ga4_account"]
        figma = org.get("figma") if isinstance(org.get("figma"), dict) else {}
        if figma.get("team"):
            out["figmaTeam"] = figma["team"]
        if figma.get("project"):
            out["figmaProject"] = figma["project"]
        return out


def reading(project: dict, assigner: "Assigner | None" = None) -> dict:
    """What a reader of a project's organization may act on (ORG-R28), one answer for all of them.

    `state`: `decided` (declared, rule or default: the organization's analytics account and design team may be
    named, `destinations`), `external` (Not ours: create nothing on its behalf), `unresolved` (a conflict, or any
    source this engine does not take for a decision, such as a joined organization's `proposed`: ask the person
    before creating anything; no organization's account is named), or `unset` (no organization is configured)."""
    source = project.get("organization_source")
    org = project.get("organization")
    if not source:
        return {"state": "unset"}
    base = {"organization": org, "source": source, "why": project.get("organization_why", "")}
    if org == "external" or source == "external":
        return {"state": "external", **base}
    if source in DECIDED and isinstance(org, str) and org:
        return {"state": "decided", **base, "destinations": (assigner or Assigner()).destinations(org)}
    return {"state": "unresolved", **base}


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
        for r in sorted((r for r in (doc or {}).get("resources") or [] if isinstance(r, dict)),
                        key=lambda r: str(r.get("id"))):
            acct = accounts.get(r.get("account_ref"))
            if not (r.get("project") and r.get("link_rule") and acct):
                continue
            add(r["project"], {"provider": provider, "keys": keys.get(acct.get("id")) or [],
                               "label": acct.get("name") or acct.get(key_field),
                               "via": f"{r.get('kind')} {r.get('name')}"})
    for a in sorted((a for a in heroku_apps or [] if isinstance(a, dict)), key=lambda a: str(a.get("id"))):
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
