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

NO TIMESTAMP INSIDE A FINDING: the same registry and configuration give the same rows.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

TYPES = ("organization.unresolved", "organization.config_problem")
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
            "detail": ("config/organizations.json exists and does not parse, so the next rebuild reads no "
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
