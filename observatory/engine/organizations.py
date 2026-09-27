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
                   "match": {"repository_owners": ["..."], "products": ["product:..."]}}},
     "projects": {"project:<slug>": {"organization": "<name>", "why": "..."}}}

THE ORDER, first answer wins:
  1. declared  — `projects` here, or `organization` in project_overrides.json
                 (where an accepted proposal lands);
  2. rule      — a repository owner or a product membership named under `match`;
                 two organizations matching is a CONFLICT, reported, never guessed;
  3. external  — every repository owner is outside the estate (ownership.json):
                 a clone of somebody else's code has no accounts of ours;
  4. default   — the organization marked `default`.
"""
from __future__ import annotations

import json

import paths

CONFIG = "organizations.json"
SOURCES = ("declared", "rule", "external", "default", "conflict")


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
            if key not in ("repository_owners", "products") or not isinstance(match[key], list):
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


class Assigner:
    """Loaded once per emit; `assign` is then a pure function of one project."""

    def __init__(self, doc: dict | None = None, estate_owners: set[str] | None = None,
                 products: dict[str, set[str]] | None = None):
        self.doc = load() if doc is None else doc
        self.orgs = self.doc.get("organizations") or {}
        self.declared = self.doc.get("projects") or {}
        self.estate = _estate_owners() if estate_owners is None else {o.lower() for o in estate_owners}
        self.products = product_index() if products is None else products
        self.default = next((n for n, o in self.orgs.items() if isinstance(o, dict) and o.get("default") is True), None)

    @property
    def active(self) -> bool:
        return bool(self.orgs)

    def assign(self, project: dict) -> dict | None:
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
