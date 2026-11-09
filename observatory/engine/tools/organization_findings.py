#!/usr/bin/env python3
"""What the organization split owes the operator (OBS-412; organization onboarding, ORG-R01, ORG-R04, ORG-R26).

Two rows, silent when `config/organizations.json` is not configured:

* `organization.unresolved`, one per project whose rules point to two organizations (a conflict). The
  emit assigns such a project to neither (`organizations.Assigner.assign`), and the session-start line
  already tells an agent to ask; this row puts the same fact on the board, so a conflict is named where a
  person looks rather than resolved by whichever reader happens to look first.
* `organization.config_problem`, one for the file: an entry the engine does not read (an unknown `match`
  key, a value that is not a list of strings), no default or a second one, a declaration naming no
  organization, or a file that does not parse. Each of those changes which organization a project gets without a word in
  the registry: the emit prints them to its log and nothing else did.

A third, `organization.selection_new` (OBS-417, ORG-R16), one per organization with an applied selection
(`org_selection.py`): the projects and keys found since that the organization's rules would propose. They are
listed in the next plan and never chosen by themselves; this row is how a person hears of them.

NO TIMESTAMP INSIDE A FINDING: the same registry and configuration give the same rows.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

TYPES = ("organization.unresolved", "organization.config_problem", "organization.selection_new")
ACCOUNTS_CMD = "project-observatory full org accounts"


def findings(projects: list[dict], orgs_doc: dict | None, unreadable: str | None = None) -> list[dict]:
    """`projects`: the registry's rows; `orgs_doc`: `organizations.json` as loaded; `unreadable`: why the
    file exists and does not parse (None when it parsed or is absent)."""
    root = str(_pathlib.Path(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.append(root)
    import organizations
    out: list[dict] = []
    if unreadable:
        out.append({
            "type": "organization.config_problem", "subject": "estate:organizations", "severity": "warning",
            **titled("organizations.json is not read: {reason}", reason=unreadable),
            "detail": ("config/organizations.json exists and is not read (it does not parse, or is not one JSON "
                       "object), so the next rebuild reads no "
                       "organization at all and every project loses its organization field."),
            "action": "fix the file (it must be one JSON object), then rebuild; "
                      f"`{ACCOUNTS_CMD}` shows what it then reads",
        })
        return out
    if not (orgs_doc or {}).get("organizations"):
        return out
    problems = organizations.validate(orgs_doc)
    if problems:
        out.append({
            "type": "organization.config_problem", "subject": "estate:organizations", "severity": "warning",
            **titled("organizations.json has entries the engine does not read"),
            "detail": "; ".join(problems[:12]) + (f"; and {len(problems) - 12} more" if len(problems) > 12 else ""),
            "action": ("correct each named entry in config/organizations.json; an entry the engine does not read "
                       f"counts for nothing. `{ACCOUNTS_CMD}` lists the same problems"),
        })
    for p in sorted((x for x in projects or [] if isinstance(x, dict)), key=lambda x: str(x.get("id"))):
        if p.get("organization_source") != "conflict":
            continue
        out.append({
            "type": "organization.unresolved", "subject": p.get("id"), "severity": "warning",
            **titled("{project} has no organization: its rules disagree", project=p.get("name") or p.get("id")),
            "detail": (f"{p.get('id')}: {p.get('organization_why') or p.get('organization_source')}. "
                       "It is assigned to no organization, so no agent creates an analytics property, a design "
                       "file or a cloud resource for it until a person decides."),
            "action": ("declare it under `projects` in config/organizations.json with a `why`, or narrow the rule "
                       f"that should not match it; `{ACCOUNTS_CMD}` shows which account each rule claims"),
        })
    return out


def selection_findings(rows: list[dict]) -> list[dict]:
    """`rows`: `org_selection.new_since_all()`, each organization with an applied selection and the item ids found
    since it. One row per organization; silent without a selection."""
    out = []
    for row in rows or []:
        org, new = row.get("organization"), row.get("new") or []
        if not isinstance(org, str) or not new:
            continue
        projects = sum(1 for i in new if str(i).startswith("project:"))
        keys = len(new) - projects                         # ORG-R16 names projects and keys; accounts are never new
        out.append({
            "type": "organization.selection_new", "subject": f"organization:{org}", "severity": "info",
            **titled("{n} new items may belong to {organization}", n=len(new), organization=org),
            "detail": (f"Found since {org}'s selection was applied, and proposed by its rules: {projects} project(s) "
                       f"and {keys} key(s): " + ", ".join(sorted(new)[:12])
                       + (f", and {len(new) - 12} more" if len(new) > 12 else "")
                       + ". None is chosen by itself; each waits for a person (ORG-R16)."),
            "action": (f"review them with `project-observatory full org select --org {_quoted(org)} plan`, include "
                       "the ones that belong to it, then apply the new form"),
        })
    return out


def _quoted(text: str) -> str:
    """An organization's name as a shell word: free text in organizations.json, quoted unless plain."""
    import re
    return text if re.fullmatch(r"[A-Za-z0-9._@+-]+", text) else "'" + text.replace("'", "'\\''") + "'"
