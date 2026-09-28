#!/usr/bin/env python3
"""`registry/google-properties.json` — every analytics property, joined to a project.

THE JOIN IS MEASURED BEFORE IT IS DECLARED, which is what changed on 2026-09-14.
A hand-written mapping file named five properties; the service account could see
thirty-eight, and GA4 itself knows what each one is about — every property
declares its web streams (a host) and its app streams (a bundle id). So the
first question is no longer "what did somebody write down" but "what does the
property say it measures", and the host goes through `plugins/hostmap.py`, the
same join the domains and the Cloudflare zones already use.

FIVE RULES, IN EVIDENCE ORDER, and each row carries the one that made it:

    declared     the workspace's `config/ga4_properties.json` — a human
                 decision outranks a measurement, and it is how a property whose
                 host this estate does not own gets attached anyway
    stream-host  a web stream's host resolves to a project through the registry
    app-id       an app stream's bundle id ends in a project's own name, exactly
    name         the property's display name IS a project's name or folder
    —            nothing matched, and the row says so

WHAT AN UNMATCHED PROPERTY MEANS — the operator's own rule, first given for
Cloudflare zones and the same here: a property with no project in this registry
is a product somebody else runs, or one this operator is not working on. It is
not a defect; the board states the count once and names them, and the answer is
a line in the mapping file or nothing at all.
"""
from __future__ import annotations
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))
import paths                                            

#: Google's own console addresses, written onto every row so a reader of the
#: registry can follow them without the page. They are DATA in a file beside
#: this module and not constants in it: one of Google's paths spells, by
#: Google's choice, the id of an installed plugin, and the seam rule
#: (`tests/test_metric_series.py`) forbids a plugin's id in core code — a URL
#: of Google's is a fact about Google, not a coupling, and the gate cannot tell
#: the two apart from a string. The file says the same in its own note.
CONSOLES_PATH = paths.config_file('google_consoles.json')


def consoles(path: pathlib.Path = CONSOLES_PATH) -> dict[str, str]:
    """The four templates, or empty strings for any the file does not carry —
    a missing address is a row without a link, never a crash of the emit."""
    import json
    keys = ("ga4_report", "ga4_admin", "search_console_site", "cloud_project")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    return {k: str(doc.get(k) or "") for k in keys}


def _fill(template: str, **parts: str) -> str | None:
    return template.format(**parts) if template else None


def _norm(s: str) -> str:
    """A name reduced to what two spellings of one thing share."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def declared_map(path: pathlib.Path) -> dict[str, dict]:
    """The operator's own file, which keeps working unchanged."""
    import json
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {row["property"]: row for row in doc.get("properties") or [] if row.get("property")}


def boundary(path: pathlib.Path) -> dict[str, dict]:
    """The operator's own word on which hosts are outside this estate.

    The same file the Cloudflare zones read. A property whose every
    host is declared outside is not an unanswered question — it is an answered
    one, and the board must not keep asking it.
    """
    import json
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # `hosts` is host -> decision, the shape the zones already read. A list here
    # would be a second spelling of one file, so the mapping is taken as it is.
    hosts = doc.get("hosts") or {}
    if isinstance(hosts, list):
        return {(r.get("host") or "").lower(): r for r in hosts if isinstance(r, dict)}
    return {str(k).lower(): v for k, v in hosts.items() if isinstance(v, dict)}


def match(prop: dict, declared: dict, host_owner, names: dict[str, str]) -> tuple[str | None, str, str]:
    """(project id, rule, evidence) for one property."""
    d = declared.get(prop["property"])
    if d and d.get("project"):
        return d["project"], "declared", f"{paths.config_label('ga4_properties.json')}: {d.get('_why', 'operator')}"
    if d and d.get("host"):
        owner = host_owner(d["host"])
        if owner:
            return owner, "declared-host", f"the file names host {d['host']}, which the registry serves"
    for h in prop.get("hosts") or []:
        owner = host_owner(h)
        if owner:
            return owner, "stream-host", f"the property's own web stream is {h}, which the registry serves"
    for app in prop.get("app_ids") or []:
        tail = _norm(app.split(".")[-1])
        if len(tail) >= 4 and tail in names:
            return names[tail], "app-id", f"the app stream {app} ends in the project's own name"
    nm = _norm(prop.get("name") or "")
    if nm and nm in names:
        return names[nm], "name", f"the property is called {prop.get('name')!r}, which is the project's name"
    return None, "", ""


_METRICS = ("users_30d", "sessions_30d", "views_30d")


def _sources(observations: list[dict]) -> list[str]:
    return sorted({source for row in observations
                   for source in [row.get("read_with"), *(row.get("read_with_all") or [])]
                   if isinstance(source, str) and source})


def canonical_properties(observations: list[dict]) -> list[dict]:
    """One resource, one metric observation; credentials are access provenance.

    No timestamps per observation exist, so this never claims a latest reading.
    Conflicting successful measurements become unknown, with explicit evidence.
    Missing identities remain separate; a shared display name proves nothing.
    """
    import json
    groups: dict[str, list[dict]] = {}
    for index, row in enumerate(observations):
        identity = row.get("property")
        if not identity and str(row.get("id", "")).startswith("ga4:"):
            identity = "properties/" + row["id"].split(":", 1)[1]
        groups.setdefault(identity or f"missing-identity:{index}", []).append(row)
    result = []
    for group in groups.values():
        # A stable tie-break makes source enumeration order irrelevant. More
        # complete successful data wins over absent/error observations.
        ordered = sorted(group, key=lambda row: (
            bool(row.get("error")),
            -sum(row.get(field) is not None for field in _METRICS),
            json.dumps(row, sort_keys=True, ensure_ascii=True)))
        chosen = dict(ordered[0])
        successful = [row for row in group if not row.get("error")]
        conflicts = sorted({field for row in group for field in row.get("observation_conflicts", [])}
                           | {field for field in (*_METRICS, "account", "project")
                              if len({json.dumps(row[field], sort_keys=True)
                                      for row in successful if row.get(field) is not None}) > 1})
        chosen["read_with_all"] = _sources(group)
        chosen["observation_count"] = sum(row.get("observation_count", 1) for row in group)
        for field in ("hosts", "app_ids"):
            chosen[field] = sorted({value for row in group for value in row.get(field, []) or []})
        if conflicts:
            chosen["observation_conflicts"] = conflicts
            chosen["error"] = "conflicting observations for the same Google Analytics property"
            for field in _METRICS:
                chosen[field] = None
            if "project" in conflicts:
                chosen.update(project=None, standing="unclaimed", link_rule=None,
                              link_evidence=None, unlinked_reason="conflicting project associations")
            if "account" in conflicts:
                chosen.update(account=None, account_name=None)
        result.append(chosen)
    return result


def canonical_accounts(observations: list[dict]) -> list[dict]:
    """Account identity, not name or credential, determines the inventory count."""
    import json
    groups: dict[str, list[dict]] = {}
    for index, row in enumerate(observations):
        groups.setdefault(row.get("account") or f"missing-identity:{index}", []).append(row)
    return [{**sorted(group, key=lambda row: json.dumps(row, sort_keys=True))[0],
             "read_with_all": _sources(group)} for group in groups.values()]


def _measured(row: dict) -> bool:
    import math
    value = row.get("users_30d")
    return (not row.get("error") and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value))


def _user_sum(rows: list[dict]) -> int | float | None:
    measured = [row["users_30d"] for row in rows if _measured(row)]
    return sum(measured) if measured else None


def project_traffic(properties: list[dict]) -> dict[str, dict]:
    """Canonical per-project metric sums with explicit observation coverage.

    These are sums across properties, never deduplicated people. Search Console
    links may still be attached to each project by the dashboard afterwards.
    """
    result: dict[str, dict] = {}
    for prop in canonical_properties(properties):
        if not prop.get("project"):
            continue
        row = result.setdefault(prop["project"], {"users_30d": None, "properties": [],
                                                 "measured_properties": 0, "unknown_properties": 0})
        measured = _measured(prop)
        if measured:
            row["users_30d"] = (row["users_30d"] or 0) + prop["users_30d"]
            row["measured_properties"] += 1
        else:
            row["unknown_properties"] += 1
        row["properties"].append({"name": prop.get("name"),
                                  "users_30d": prop.get("users_30d") if measured else None,
                                  "sessions_30d": prop.get("sessions_30d") if measured else None,
                                  "report_url": prop.get("report_url"),
                                  "admin_url": prop.get("admin_url"),
                                  "hosts": prop.get("hosts") or [],
                                  "rule": prop.get("link_rule"),
                                  **({"error": prop["error"]} if prop.get("error") else {})})
    return result


def normalize_document(doc: dict) -> dict:
    """Normalize cached registry metadata too, without provider access or writes."""
    props = canonical_properties(doc.get("properties") or [])
    accounts = canonical_accounts(doc.get("accounts") or [])
    totals = dict(doc.get("totals") or {})
    totals.update(
        properties=len(props), accounts=len(accounts),
        linked_to_a_project=sum(bool(row.get("project")) for row in props),
        outside_the_estate=sum(row.get("standing") == "outside" for row in props),
        unclaimed=sum(row.get("standing") == "unclaimed" for row in props),
        users_30d_unclaimed=_user_sum([row for row in props if row.get("standing") == "unclaimed"]),
        users_30d=_user_sum(props),
        measured_properties=sum(_measured(row) for row in props),
        unknown_properties=sum(not _measured(row) for row in props),
        by_rule={rule: sum(row.get("link_rule") == rule for row in props)
                 for rule in sorted({row.get("link_rule") for row in props if row.get("link_rule")})})
    degraded = list(doc.get("degraded") or [])
    for row in props:
        if row.get("observation_conflicts"):
            issue = {"source": "ga4 " + str(row.get("property") or row.get("id") or "unknown"),
                     "reason": "conflicting observations: " + ", ".join(row["observation_conflicts"]),
                     "effect": "traffic is unknown; duplicate readings are never summed"}
            if issue not in degraded:
                degraded.append(issue)
    return {**doc, "properties": props, "accounts": accounts, "totals": totals, "degraded": degraded}


def rows(scan: dict, projects: list[dict], declared_path: pathlib.Path) -> list[dict]:
    """One row per property. `hostmap.build()` reads the registry itself, which
    is why this takes the project list only: the host join is the estate's, not
    a second copy of it."""
    import hostmap
    table = hostmap.build()
    def owner_of(host: str):
        return hostmap.resolve(host, table)
    declared = declared_map(declared_path)
    outside = boundary(paths.config_file('host_boundary.json'))
    # A project is findable by its own name and by every folder it holds: the
    # estate spells one thing three ways and the join has to survive that.
    names: dict[str, str] = {}
    for p in projects:
        names.setdefault(_norm(p["name"]), p["id"])
        for f in p.get("local_folders") or []:
            names.setdefault(_norm(pathlib.Path(f).name), p["id"])
    urls = consoles()
    out = []
    for prop in canonical_properties(scan.get("properties") or []):
        pid = (prop.get("property") or "").split("/")[-1]
        aid = (prop.get("account") or "").split("/")[-1]
        project, rule, why = match(prop, declared, owner_of, names)
        # THREE STANDINGS, not two — the same shape the zones carry. A property
        # nobody claims is a question; a property whose hosts the operator has
        # already called somebody else's is an answer, and asking again is how a
        # board teaches people to stop reading it.
        hosts = prop.get("hosts") or []
        said = [outside[h] for h in hosts if h in outside]
        if project:
            standing, why_out = "linked", None
        elif hosts and len(said) == len(hosts):
            standing, why_out = "outside", said[0].get("why")
        else:
            standing, why_out = "unclaimed", None
        out.append({
            "id": f"ga4:{pid}",
            "property": prop.get("property"),
            "name": prop.get("name"),
            "account": prop.get("account"),
            "account_name": prop.get("account_name"),
            "hosts": prop.get("hosts") or [],
            "app_ids": prop.get("app_ids") or [],
            "users_30d": prop.get("users_30d"),
            "sessions_30d": prop.get("sessions_30d"),
            "views_30d": prop.get("views_30d"),
            "project": project,
            "standing": standing,
            "boundary_why": why_out,
            "link_rule": rule or None,
            "link_evidence": why or None,
            "unlinked_reason": None if standing != "unclaimed" else (
                "no project in this registry serves any host or app this property "
                "declares, and no curated row names one — by the operator's estate "
                "rule that means somebody else runs it, or it is not being worked on"),
            "report_url": _fill(urls["ga4_report"], pid=pid),
            "admin_url": _fill(urls["ga4_admin"], aid=aid, pid=pid) if aid else None,
            "read_with": prop.get("read_with"),
            "read_with_all": prop.get("read_with_all") or [],
            "observation_count": prop.get("observation_count", 1),
            **({"observation_conflicts": prop["observation_conflicts"]} if prop.get("observation_conflicts") else {}),
            **({"error": prop["error"]} if prop.get("error") else {}),
        })
    out.sort(key=lambda r: (-(r.get("users_30d") or 0), r["name"] or ""))
    return out


def document(scan: dict, prop_rows: list[dict], obs_date: str) -> dict:
    urls = consoles()
    sites = []
    for s in scan.get("search_console") or []:
        site = s.get("site") or ""
        sites.append({**s, "console_url": _fill(urls["search_console_site"], site=site)})
    creds = [{**c, "console_url": _fill(urls["cloud_project"], project=c.get("cloud_project"))}
             for c in scan.get("credentials") or []]
    linked = [r for r in prop_rows if r["project"]]
    return normalize_document({
        "schema_version": 1,
        "updated_on": obs_date,
        "note": ("Every Google Analytics property this machine's service accounts "
                 "can see, joined to a project by what the property itself "
                 "declares — a web stream's host through plugins/hostmap.py, an "
                 "app stream's bundle id, the display name — with the operator's "
                 "own mapping file outranking all three. `users_30d` is active "
                 "users over the last thirty complete days. A property no project "
                 "claims is named, not hidden: by the operator's estate rule it "
                 "belongs to somebody else or is not being worked on."),
        "source_refs": ["SRC-0019"],
        # A DATE, not a clock: the committed registry may not carry a value that
        # changes on every run by construction (`tests/test_tick_repo.py`), and
        # the first emit of this file did — caught by the gate the same day.
        "scanned_on": (scan.get("scanned_at") or "")[:10],
        "totals": {
            "properties": len(prop_rows),
            "accounts": len(scan.get("accounts") or []),
            "linked_to_a_project": len(linked),
            "outside_the_estate": sum(1 for r in prop_rows if r["standing"] == "outside"),
            "unclaimed": sum(1 for r in prop_rows if r["standing"] == "unclaimed"),
            "users_30d_unclaimed": sum(r.get("users_30d") or 0 for r in prop_rows
                                       if r["standing"] == "unclaimed"),
            "users_30d": sum(r.get("users_30d") or 0 for r in prop_rows),
            "search_console_sites": len(sites),
            "by_rule": {rule: sum(1 for r in prop_rows if r["link_rule"] == rule)
                        for rule in sorted({r["link_rule"] for r in prop_rows if r["link_rule"]})},
        },
        "credentials": creds,
        "accounts": scan.get("accounts") or [],
        "properties": prop_rows,
        "search_console": sites,
        "degraded": scan.get("degraded") or [],
    })
