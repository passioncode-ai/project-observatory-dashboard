#!/usr/bin/env python3
"""What the analytics inventory owes the operator.

THE BOUNDARY RULE APPLIES HERE TOO, and it is the operator's own (2026-09-13,
first given for Cloudflare zones): a property no project in this registry claims
is one somebody else runs, or one the operator is not working on. So an
unclaimed property is not a defect and never a warning — it is a COUNT the board
states once, with the names, so the answer can be given in one place.

What makes the count worth stating at all is the traffic behind it. Thirty
properties with no users is a tidy-up nobody needs; thirty properties carrying
three and a half million people in a month is an estate whose busiest product is
invisible to the system that is supposed to know what exists (measured
2026-09-14, and the reason this rule was written).

NO TIMESTAMP INSIDE A FINDING: the dates quoted are the document's.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402


def _config_label(name: str) -> str:
    """The workspace-relative name of a configuration file.

    Imported late: these rule modules are loaded by `tools/build_findings.py`,
    which has already resolved the workspace, and importing `paths` at module
    load would resolve it again for every test that imports a rule alone."""
    import paths
    return paths.config_label(name)

#: Named by name up to this many, as everywhere else on this board.
LISTED = 6
#: A scan older than this is stale enough that the page should say so. Analytics
#: settle daily and the scan is gated to twice a day, so three days means the
#: tick has not run, not that Google was quiet.
STALE_DAYS = 3


def _listed(items: list[str]) -> str:
    shown = items[:LISTED]
    rest = len(items) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")


def findings(doc: dict | None, today: str = "") -> list[dict]:
    if not doc:
        return []
    props = doc.get("properties") or []
    if not props:
        return []
    out: list[dict] = []

    # ── properties nothing here claims ──────────────────────────────────────
    unclaimed = [p for p in props if p.get("standing") == "unclaimed"]
    if unclaimed:
        unclaimed.sort(key=lambda p: -(p.get("users_30d") or 0))
        measured = [p for p in unclaimed if not p.get("error") and p.get("users_30d") is not None]
        unknown = len(unclaimed) - len(measured)
        users = sum(p["users_30d"] for p in measured)
        traffic = f"{users:,} summed user(s) in 30 days" if measured else "unknown traffic"
        if unknown:
            traffic += f" ({unknown} unmeasured)"
        named = _listed([f"{p['name']} (" +
                         (f"{p['users_30d']:,}" if p in measured else "unknown") + ")"
                         for p in unclaimed])
        out.append({
            "type": "analytics.property_unclaimed",
            "subject": "estate:analytics",
            "severity": "info",
            **(titled("{n} analytics properties with {users} summed users in 30 days belong to no project here", n=len(unclaimed), users=users)
                 if measured and not unknown else
                 titled("{n} analytics properties with {users} summed users in 30 days ({unknown} unmeasured) belong to no project here", n=len(unclaimed), users=users, unknown=unknown)
                 if measured else
                 titled("{n} analytics properties with unknown traffic ({unknown} unmeasured) belong to no project here", n=len(unclaimed), unknown=unknown)),
            "detail": (f"Measured traffic first: {named}. User counts are sums across properties, "
                       f"not distinct people across products. A property is joined to a project by what "
                       f"it declares about itself — a web stream's host through the "
                       f"registry, an app stream's bundle id, its own name — and these "
                       f"matched none. By the operator's estate rule that means somebody "
                       f"else runs them or they are not being worked on; the point of the "
                       f"row is that the biggest number in this estate should not be the "
                       f"one nothing claims."),
            "action": (f"name the project in {_config_label('ga4_properties.json')}, or say "
                       f"the host is somebody else's in {_config_label('host_boundary.json')} — "
                       "either answer silences this row"),
        })

    # ── a property that would not report ────────────────────────────────────
    broken = [p["name"] for p in props if p.get("error")]
    if broken:
        out.append({
            "type": "analytics.property_unreadable",
            "subject": "estate:analytics",
            "severity": "warning",
            **titled("{n} analytics properties have unknown traffic", n=len(broken)),
            "detail": (f"{_listed(sorted(broken))}. Their traffic is unknown here, which "
                       f"is not the same as zero — nothing on this board should be read "
                       f"as saying they are quiet."),
            "action": "check service account access and any conflicting observations for the same property",
        })

    # ── a credential that cannot reach a surface at all ─────────────────────
    # Both lists: a refusal on `not_applicable` is one whose surface another
    # credential already reads (collectors/scan_google.py#sort_refusals). It is
    # still a switched-off API worth one informational row, and it says so.
    rows = ([(d, False) for d in doc.get("degraded") or []]
            + [(d, True) for d in doc.get("not_applicable") or []])
    for d, covered in rows:
        if "accessNotConfigured" in str(d.get("reason", "")) or "has not been used in project" in str(d.get("reason", "")):
            out.append({
                "type": "analytics.api_disabled",
                "subject": f"credential:{d.get('source', 'google')}",
                "severity": "info",
                **(titled("a Google API is switched off for {source}", source=d["source"]) if "source" in d
                     else titled("a Google API is switched off for a credential")),
                "detail": (f"{str(d.get('reason'))[:240]} — the API is not enabled in this "
                           f"credential's Cloud project, which reads as a permission problem "
                           f"and is not one."
                           + (" Another credential reads this surface, so nothing the estate "
                              "reads is missing; enable it only if this credential should "
                              "read the surface too." if covered else "")),
                "action": d.get("remedy") or "enable the API in that Cloud project",
            })

    # ── the numbers are old ─────────────────────────────────────────────────
    scanned = (doc.get("scanned_on") or "")[:10]
    if today and scanned:
        import datetime
        try:
            age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(scanned)).days
        except ValueError:
            age = 0
        if age > STALE_DAYS:
            out.append({
                "type": "analytics.stale",
                "subject": "estate:analytics",
                "severity": "warning",
                **titled("the analytics numbers are {n} days old", n=age),
                "detail": (f"Measured {scanned}; the scan is gated to twice a day, so this "
                           f"means the tick has not run rather than that Google was quiet. "
                           f"Every traffic figure on the page is that old."),
                "action": "`project-observatory full google --force`, or the refresh button on the traffic page",
            })
    return out


def organization_findings(doc: dict | None, projects: list[dict], orgs_doc: dict) -> list[dict]:
    """Where a property sits against whose project it measures.

    The organization split (organizations.json) says which Google Analytics
    account each owner's properties belong in. Three things break it, and each
    was seen before this rule existed: a property left in the other owner's
    account after the product changed hands (Screen2Shot, 2026-09-27), a new
    property created in an account kept only for one legacy site, and an owner's
    account the service account cannot read — which makes the first two
    invisible rather than absent.
    """
    orgs = (orgs_doc or {}).get("organizations") or {}
    if not doc or not orgs:
        return []
    props = doc.get("properties") or []
    by_id = {p["id"]: p for p in projects if isinstance(p, dict) and p.get("id")}
    out: list[dict] = []

    for prop in props:
        pid, account = prop.get("project"), prop.get("account")
        project = by_id.get(pid or "")
        if not project or not account or project.get("organization_source") in (None, "conflict", "external"):
            continue
        org = orgs.get(project.get("organization") or "") or {}
        expected = org.get("ga4_account")
        if expected and account != expected and account not in (org.get("ga4_legacy_accounts") or {}):
            out.append({
                "type": "analytics.property_wrong_account",
                "subject": pid,
                "severity": "warning",
                **titled("{property} sits in {account}, not in {org}'s account", property=prop.get("name") or prop.get("property"), account=prop.get("account_name") or account, org=org.get("label") or project["organization"]),
                "detail": (f"{prop.get('property')} measures {pid}, whose organization is "
                           f"{project['organization']} ({project.get('organization_source')}: "
                           f"{project.get('organization_why', '')}). That organization's properties "
                           f"belong in {expected}; this one is in {account}."),
                "action": (f"move the property to {expected} in Google Analytics (Admin → Property "
                           f"settings → Move property), or correct the project's organization"),
            })

    for org_name, org in orgs.items():
        for legacy, allowed in ((org or {}).get("ga4_legacy_accounts") or {}).items():
            extra = [p for p in props if p.get("account") == legacy and p.get("property") not in (allowed or [])]
            if extra:
                out.append({
                    "type": "analytics.legacy_account_property",
                    "subject": "estate:analytics",
                    "severity": "warning",
                    **titled("{n} new properties in legacy account {account}", n=len(extra), account=legacy),
                    "detail": (f"{_listed(sorted(p.get('name') or p.get('property') for p in extra))}. "
                               f"{legacy} is kept only for {', '.join(allowed or []) or 'what it already holds'}; "
                               f"new properties belong in their owner's account."),
                    "action": f"move them to their owner's account, or list them under ga4_legacy_accounts in organizations.json",
                })

    readable = {a.get("account") for a in doc.get("accounts") or []}
    for org_name, org in orgs.items():
        expected = (org or {}).get("ga4_account")
        if expected and expected not in readable:
            out.append({
                "type": "analytics.organization_account_unreadable",
                "subject": "estate:analytics",
                "severity": "warning",
                **titled("{org}'s analytics account {account} is not readable", org=(org or {}).get("label") or org_name, account=expected),
                "detail": ("No service account this machine holds reads it, so a property there is "
                           "invisible here and the two checks above cannot see a misplaced one."),
                "action": f"grant a service account read access to {expected}",
            })
    return out
