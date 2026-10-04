#!/usr/bin/env python3
"""The read layer: `estate.survey` and the two narrower reads behind it.

Deterministic. No model participates, so the same scope over the same scan
returns the same answer. Output conforms to
fabric/schemas/capability-output.schema.json — the probes check that, rather
than this docstring promising it.
"""
from __future__ import annotations
import json, os, re, sqlite3
from datetime import datetime, timezone
from pathlib import Path

import activity
import credential_shape
import degradations
import identity
import paths
from store import db as store_db
from store import ledger as L_kinds

SOURCE_OF_TRUTH = "registry"


def _load(name: str) -> dict:
    return json.loads((paths.REGISTRY / name).read_text(encoding="utf-8"))


#: One reader for every collector's `degraded` list, in `degradations.py`. It
#: moved out of this file when `tools/build_findings.py` became its second
#: caller: the previous time a rule about collector output lived in two places,
#: the two disagreed about the shape and the GitHub degradations reached nobody.
#: Kept as a module-level name here because the survey's own call sites read
#: better for it and every test in the tree names it.
_collector_degradation = degradations.collector


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _index():
    projects = _load("projects.json")["projects"]
    repos = {r["id"]: r for r in _load("repositories.json")["repositories"]}
    members: dict[str, list[str]] = {}
    for rel in _load("relations.json")["relations"]:
        if rel["type"] == "implemented_by":
            members.setdefault(rel["from"], []).append(rel["to"])
    return projects, repos, members


def _repo_view(r: dict) -> dict:
    view = {"id": r["id"], "host": r["host"], "nameWithOwner": r["name_with_owner"]}
    for src, dst in (("visibility", "visibility"), ("default_branch", "defaultBranch"),
                     ("archived", "archived"), ("fork", "fork"),
                     ("last_pushed_on", "lastPushedOn"), ("discovered_by", "discoveredBy")):
        val = r.get(src)
        if val not in (None, ""):
            view[dst] = val
    return view


def _marks(items) -> str:
    """`?,?,?` for a parameter list — an id set is never empty, so no branch."""
    return ",".join("?" * len(items))


def ids_for_project(project_id: str) -> list[str]:
    """Every id under which this project's history may be recorded.

    A project's id is derived from its PUBLICATION STATE — `project:local-<folder>`
    while it has no remote, `project:<owner>-<name>` after — so publishing one
    renames it and detaches every row already keyed to the old id.

    Resolved rather than rewritten: the ledger is authored and append-only, so
    the reader follows the rename and the record stays as it was written.
    """
    try:
        projects = _load("projects.json")["projects"]
        for p in projects:
            if p["id"] == project_id:
                return identity.ids_for(p, projects)
    except (OSError, ValueError, KeyError):
        # The registry is the same source every other section reads; if it will
        # not open, the caller's own degradation reports it. Falling back to the
        # id as given keeps this from turning a registry fault into an empty view.
        pass
    return [project_id]


def _identity_warnings(projects: list[dict], project_id: str | None = None) -> list[dict]:
    conflicts = identity.former_conflicts(projects)
    relevant = any(project_id is None or project_id == old or project_id in owners
                   for old, owners in conflicts.items())
    if not relevant:
        return []
    return [{'source':'identity', 'reason':
             'Ambiguous links to old project identifiers were not followed; review the project/folder mapping.'}]


def _tier_vocabulary() -> list[dict]:
    """The tiers a host must be able to ORDER and colour, in threshold order.

    A tier string on its own is half an answer: nothing outside this repository
    knows whether `cooling` is better or worse than `dormant`. The vocabulary
    cannot be an enum in the published schema either — the thresholds live in
    `collectors/activity_tiers.json` so the operator can move them, and a host
    that hardcoded the list would break the day a tier is added. So the survey
    ships the vocabulary beside the answers, once per call rather than per
    project.
    """
    out = [{"id": t["id"], "maxDays": t.get("max_days"), "means": t.get("means", "")}
           for t in activity.tiers()]
    out.append({"id": activity.unknown_id(), "maxDays": None,
                "means": activity.unknown_means()})
    return out


def _recent_activity(conn, renamed_to: dict[str, str], days: int) -> dict[str, dict]:
    """How much moved in every project, in ONE query for the whole estate.

    **Why the survey needs this.** Without a quantitative field a host could list
    projects but never order them by how much work happened: the weekly series
    lives in `project.detail`, which is one call per project.

    **NULL is excluded and COUNTED, never summed as zero.** SQL's `SUM` skips
    nulls and `COUNT(*) - COUNT(sessions)` reports how many it skipped, which is
    the same rule `project_detail` applies in Python: a week computed before the
    session columns existed cannot say "no sessions", only "not measured".

    **The rename is followed.** A project's id changes when it is published, and
    rows written before that stay keyed to the old one. Grouping in SQL and
    merging the renames here keeps one project's history whole.
    """
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT project_id, COUNT(*) AS weeks, SUM(commits) AS commits,"
            "       SUM(sessions) AS sessions, SUM(worked_days) AS worked,"
            "       COUNT(*) - COUNT(sessions) AS unmeasured"
            "  FROM project_week WHERE week_start >= date('now', ?)"
            " GROUP BY project_id", (f"-{days} days",)):
        pid = renamed_to.get(r["project_id"], r["project_id"])
        acc = out.setdefault(pid, {"weeks": 0, "commits": 0, "sessions": 0,
                                   "workedDays": 0, "sessionsUnmeasured": 0})
        acc["weeks"] += r["weeks"]
        acc["commits"] += r["commits"] or 0
        acc["sessions"] += r["sessions"] or 0
        acc["workedDays"] += r["worked"] or 0
        acc["sessionsUnmeasured"] += r["unmeasured"]
    return out


def _estate_work(conn, since: str, known: set[str] | None = None) -> dict:
    """What the ESTATE did over one span — including the figure nobody can add up.

    `workedDays` is a set size folded per (project, week). Across weeks it adds
    up, because different weeks hold different days. Across PROJECTS it does
    not: a Tuesday two projects were both worked on is one Tuesday. Summing the
    column across projects can yield more days than the window contains, and
    the result would look authoritative.

    So the answer is given rather than warned about. The summable figures come
    from the rollup, so a host can add the rows up and get the same number; the
    day count is a DISTINCT count over the events of the same span, which is the
    one place the question can be answered without the error.
    """
    out: dict[str, int] = {}
    row = conn.execute(
        "SELECT SUM(commits) c, COUNT(DISTINCT CASE WHEN commits > 0"
        "        THEN project_id END) p"
        "  FROM project_week WHERE week_start >= ?", (since,)).fetchone()
    if row is not None:
        out["commits"] = row["c"] or 0
        out["projectsWorked"] = row["p"] or 0
    day = conn.execute(
        "SELECT COUNT(DISTINCT date(occurred_at)) d FROM events"
        " WHERE kind = 'commit' AND occurred_at >= ?", (since,)).fetchone()
    if day is not None:
        out["workedDays"] = day["d"] or 0
    # THE PART THAT DOES NOT RECONCILE, named rather than left to be noticed.
    # The rollup outlives its source by design — that is what the freeze rule is
    # for — so a project the registry no longer holds still has history, and
    # counting the estate's work without it would hide work that happened.
    # Defined against the REGISTRY rather than the page, so it stays meaningful
    # when a host pages through the projects.
    if known is not None:
        rows = conn.execute(
            "SELECT project_id, SUM(commits) c FROM project_week"
            " WHERE week_start >= ? GROUP BY project_id", (since,)).fetchall()
        out["commitsUnattributed"] = sum(
            (r["c"] or 0) for r in rows if r["project_id"] not in known)
    return out


def _project_view(p: dict, repos: dict, members: dict) -> dict:
    view = {
        "id": p["id"],
        "name": p["name"],
        "ownership": p["ownership"],
        "lifecycle": p["lifecycle"],
        "repositories": [_repo_view(repos[i]) for i in sorted(members.get(p["id"], [])) if i in repos],
        "membershipRules": list(p.get("membership_rules", [])),
    }
    for src, dst in (("description", "description"), ("description_source", "descriptionSource"),
                     ("local_folders", "localFolders"), ("stack", "stack"),
                     ("last_activity_on", "lastActivityOn"), ("has_vault_note", "hasVaultNote"),
                     # THE MEASUREMENT, beside the declared state. `lifecycle` is
                     # 157 active of 160 and answers "what did the owner say";
                     # `activity_tier` answers "when did it last move", and 27
                     # projects disagree. A host rendering the estate from
                     # `lifecycle` shows almost everything as active, which is the
                     # brief's headline question answered wrongly.
                     ("activity_tier", "activityTier"),
                     # WHERE WORK WAS DONE when no commit was made — SRC-0012's
                     # whole reason, and 57 projects carry one.
                     ("last_session_on", "lastSessionOn")):
        val = p.get(src)
        if val not in (None, "", []):
            view[dst] = val
    sites = []
    for s in p.get("sites", []):
        sites.append({"host": s["host"], "ownedDomain": s.get("owned_domain"),
                      "confidence": s["confidence"], "evidence": list(s["evidence"])})
    if sites:
        view["sites"] = sites
    # WHOSE ACCOUNTS. An agent about to create an analytics property, a cloud
    # project or a design file reads the destination here instead of recalling
    # it from a conversation; `resources` is what has been recorded as created.
    if p.get("organization_source"):
        import organizations
        view["organization"] = {"name": p.get("organization"), "source": p["organization_source"],
                                "why": p.get("organization_why", ""),
                                **organizations.Assigner().destinations(p.get("organization"))}
    if p.get("resources"):
        view["resources"] = [dict(r) for r in p["resources"] if isinstance(r, dict)]
    return view


def survey(scope: dict | None = None, include_external: bool = False,
           as_of_scan_id: str | None = None, conn: sqlite3.Connection | None = None,
           limit: int | None = None, cursor: str | None = None) -> dict:
    """Survey a scope. `limit`/`cursor` page the project list.

    **Why paging exists.** An estate survey grows with the number of projects,
    and a host that wants to render ONE project should not have to swallow the
    whole estate first just to learn the ids.

    **`limit` has no default, deliberately.** A default page size would truncate
    every existing caller's answer silently, and the answer must declare what it
    left out. So a caller without `limit` gets everything, and a caller with one
    gets `nextCursor` beside a `counts` that still reports the WHOLE scope —
    `len(projects) < counts["projects"]` plus a cursor is unambiguous, where a
    shrunken count would misstate the scope.

    **The cursor is the last id of the page, and a pin does not make the walk
    reproducible.** Ordering is by project id, which is stable, so a walk loses
    and repeats nothing WITHIN one registry. But a project appearing between two
    calls shifts the walk whether or not `as_of_scan_id` is passed: the answer is
    built from the registry on disk, which each tick rewrites, and the pin does
    not filter anything.
    """
    scope = scope or {"kind": "estate"}
    kind = scope.get("kind", "estate")
    projects, repos, members = _index()
    degraded: list[dict] = _identity_warnings(projects, scope.get('value') if kind == 'project' else None)

    owned = None
    if conn is None:
        try:
            conn = store_db.connect()
        except Exception as exc:                                                    
            degraded.append({"source": "store", "reason": f"unavailable: {exc}"})
        else:
            owned = conn
    scan_id = "registry-only"
    if conn is not None:
        latest = store_db.latest_scan(conn)
        # THE PIN IS RECORDED, NOT HONOURED — and saying so is the whole of this
        # branch. The answer is built from `_index()`, the registry on disk, so
        # `as_of_scan_id` cannot select an older state of the estate; pinning to
        # an old scan would otherwise return today's counts under an old label.
        #
        # So `scanId` means one thing: the scan this answer REFLECTS. Every
        # deviation goes to `degraded`, where this file already puts everything
        # a caller could otherwise read wrongly. Answering today's estate under
        # yesterday's stamp is the one outcome a caller could not detect.
        #
        # Honouring the pin for real would need a map from a scan to the registry
        # version it saw: the registry is versioned in git, but nothing maps a
        # scan to its commit, and `scans.counts_json` holds each collector's own
        # receipt, not the estate's counts.
        if as_of_scan_id and as_of_scan_id != latest:
            row = conn.execute("SELECT id FROM scans WHERE id = ?", (as_of_scan_id,)).fetchone()
            if row is None:
                degraded.append({"source": "store",
                                 "reason": f"scan {credential_shape.echo(as_of_scan_id)} is not "
                                           f"recorded; answered "
                                           f"from the registry as it is now"})
            else:
                degraded.append({"source": "store",
                                 "reason": f"scan {credential_shape.echo(as_of_scan_id)} is recorded, "
                                           f"but the "
                                           f"registry is not versioned per scan, so this "
                                           f"answer carries the CURRENT estate and `scanId` "
                                           f"names the scan it reflects"})
        if latest:
            scan_id = latest
        else:
            degraded.append({"source": "store",
                             "reason": "no scan recorded yet; answered from the registry alone"})

    selected = projects
    if kind == "project":
        selected = [p for p in projects if p["id"] == scope.get("value")]
    elif kind == "owner":
        selected = [p for p in projects if scope.get("value") in p.get("owners", [])]
    if not include_external:
        selected = [p for p in selected if p["ownership"] != "external"]

    # Ordered before paging: a cursor over an unordered list is a cursor over
    # nothing. `id` is the stable key, and sorting it here also makes the
    # unpaged answer deterministic, as anything committed must be.
    selected = sorted(selected, key=lambda p: p["id"])
    total_projects = len(selected)
    page = selected
    next_cursor = None
    if cursor:
        # A CURSOR THAT NAMES NO POSITION, said out loud. The filter is
        # `p["id"] > cursor`, so a string that sorts before every id — anything
        # not starting with `project:` — passes the whole list through and the
        # walk SILENTLY RESTARTS. Measured 2026-09-08: `cursor="!!! not an id
        # !!!"` with `limit=5` returned page one again, so a host holding a
        # stale or corrupted cursor would receive projects it had already walked
        # with nothing to tell it apart from progress.
        #
        # A degradation rather than a refusal, because the answer is still
        # useful and this file reports rather than raises everywhere else. And
        # the check is the SHAPE, not membership: a well-formed cursor whose
        # project has been deleted between two pages is legitimate — the walk
        # must continue after it, which `>` already does.
        if not str(cursor).startswith("project:"):
            degraded.append({"source": "cursor",
                             "reason": f"cursor {credential_shape.echo(str(cursor))} is not a "
                                       f"project id, so it names no position in "
                                       f"this list; the page starts from the "
                                       f"beginning and a walk using it will "
                                       f"repeat what it has already seen"})
        page = [p for p in page if p["id"] > cursor]
    if limit is not None:
        # A limit below 1 is a caller mistake, and returning an empty page for it
        # would be a silent one: "no projects" and "you asked for none" are
        # different answers. The wire declares `ge=1` so it cannot arrive there.
        if limit < 1:
            raise ValueError(f"limit must be at least 1; got {limit}")
        more = page[limit:]
        page = page[:limit]
        if more and page:
            next_cursor = page[-1]["id"]

    # HOW MUCH MOVED, in one query for the estate rather than one per project.
    # `windowDays` is the RULE (the `active` tier's own boundary) and `weeksFrom`
    # is what was actually summed: the predicate compares against a DATE, so the
    # earliest week included starts on or after it, and naming the date a week
    # boundary would be a precision the number does not have.
    recent: dict[str, dict] = {}
    window: dict | None = None
    estate_work: dict | None = None
    if conn is not None:
        days = activity.active_window_days()
        project_rows = list(projects.values()) if isinstance(projects, dict) else projects
        renamed_to = identity.former_index(project_rows)
        try:
            recent = _recent_activity(conn, renamed_to, days)
            window = {"windowDays": days}
            first = conn.execute(
                "SELECT MIN(week_start) FROM project_week WHERE week_start >= "
                "date('now', ?)", (f"-{days} days",)).fetchone()[0]
            if first:
                window["weeksFrom"] = first
                # THE ESTATE'S OWN FIGURES, over the same span as the rows, so a
                # host never has to add up a column that cannot be added.
                try:
                    known = {p['id'] for p in project_rows} | set(renamed_to)
                    work = _estate_work(conn, first, known)
                except sqlite3.Error as exc:
                    degraded.append({"source": "events",
                                     "reason": f"the estate's own work could not be "
                                               f"counted: {type(exc).__name__}"})
                else:
                    if work:
                        estate_work = {"windowDays": days, "weeksFrom": first, **work}
        except sqlite3.Error as exc:
            degraded.append({"source": "rollup",
                             "reason": f"the weekly rollup could not be summed: "
                                       f"{type(exc).__name__}: {exc}"})
    views = []
    for p in page:
        v = _project_view(p, repos, members)
        # ABSENT, NOT ZERO. 69 of 160 projects carry no weekly row at all, and a
        # project nothing measured is not a project that did nothing.
        if p["id"] in recent:
            v["recentActivity"] = recent[p["id"]]
        views.append(v)
    # `seen_repos` and `owners` describe the SCOPE, not the page: they feed
    # `counts`, and a count that shrank with the page size would say the estate
    # had fewer repositories because the caller asked for fewer projects.
    all_views_repos = {rid for p in selected for rid in members.get(p["id"], [])}
    seen_repos = all_views_repos or {r["id"] for v in views for r in v["repositories"]}
    owners = {o for p in selected for o in p.get("owners", [])}

    # The reason is READ from the collector, not asserted here. It was a fixed
    # string — "no API listing is configured" — which was true until
    # collectors/scan_bitbucket.py existed and would have gone on being reported
    # unchanged the day a credential appeared. A degradation notice that cannot
    # stop being true is not a measurement.
    thin = sum(1 for i in seen_repos
               if repos[i]["host"] == "bitbucket"
               and repos[i].get("discovered_by") == "local-remote-only")
    if thin:
        reasons = _collector_degradation("bitbucket.json")
        if reasons:
            for d in reasons:
                degraded.append({"source": d["source"],
                                 "reason": f"{d['reason']} — {thin} repositories are "
                                           f"therefore known only from a local remote"})
        elif degradations.integration_off("bitbucket.json"):
            degraded.append({"source": "bitbucket",
                             "reason": f"{thin} repositories carry no listing data: the "
                                       f"bitbucket integration is off in this workspace, so "
                                       f"they are known only from a local remote"})
        else:
            degraded.append({"source": "bitbucket",
                             "reason": f"{thin} repositories carry no listing data; the "
                                       f"bitbucket collector has not run against this "
                                       f"registry yet"})

    # Same rule as the bitbucket notice: a surface nobody measured is
    # named as unmeasured. Silence would let a caller read "no dead sites" out of
    # "no scan", and those are opposite claims.
    if any(p.get("sites") for p in selected):
        if not (paths.SCRATCH / "domains_live.json").is_file():
            degraded.append({"source": "domains",
                             "reason": "no domain scan has run, so no site is known to "
                                       "resolve or not; run `project-observatory full domains`"})
        else:
            degraded.extend(_collector_degradation("domains_live.json"))

    # THE GITHUB LISTING, which nothing had ever reported. Its failures are
    # per-owner and the collector keeps the previous file when one fails, so the
    # answer is not wrong — it is OLDER than it looks, and a caller comparing
    # two surveys would see repositories appear and disappear with nothing to
    # explain it.
    # PB-132: the registry may be older than it looks when the tick died or stopped.
    try:
        import configuration
        import tick_health
        degraded.extend(tick_health.degraded(paths.STATE, paths.SCRATCH,
                                             scheduler_enabled=configuration.enabled("scheduler", "features")))
    except Exception as exc:
        degraded.append({"source": "tick", "reason": f"health unreadable: {type(exc).__name__}"})
    for d in _collector_degradation("gh/_degraded.json"):
        degraded.append({"source": d.get("source", "github"),
                         "reason": f"{d.get('reason', 'the listing failed')} — the "
                                   f"previous listing for that owner is still in use"})

    result = {
        "surveyedAt": _now(),
        "activityTiers": _tier_vocabulary(),
        "scanId": scan_id,
        "scope": scope,
        "counts": {"projects": total_projects, "repositories": len(seen_repos),
                   "owners": len(owners)},
        **({"recentActivityWindow": window} if window else {}),
        **({"estateWork": estate_work} if estate_work else {}),
        "projects": views,
        "evidence": [s["id"] for s in _load("sources.json")["sources"]],
        "degraded": degraded,
    }
    if next_cursor:
        result["nextCursor"] = next_cursor
    if owned is not None:
        owned.close()
    return result


#: The tombstone rule, as SQL, in ONE place. Every read of the ledger is
#: record-wide and `dashboard/build_dashboard.py`'s notes query had no
#: tombstone join at all — so an erased conclusion would have been rendered on
#: the operator's page while `live()`, the search and the review queue all hid
#: it. Both readers use this fragment now, because a rule spelled out twice is a
#: rule that will be spelled out once.
LIVE_LEDGER_JOIN = (
    " JOIN (SELECT memory_id, MAX(revision) r FROM ledger GROUP BY memory_id) m"
    "   ON m.memory_id = l.memory_id AND m.r = l.revision"
    " LEFT JOIN tombstones t ON t.memory_id = l.memory_id")
#: A workflow's checkpoint and handoff pack are working state, read through the
#: workflow tools and the Agents view: they are not a project's conclusions, and
#: a page of notes must not fill with the latest step of every workflow.
LIVE_LEDGER_WHERE = " t.memory_id IS NULL AND " + L_kinds.not_workflow()


def project_detail(project_id: str, timeline_limit: int = 10,
                   weeks: int = 26, notes_limit: int = 10) -> dict:
    """One project, as much as is known about it, each section degrading alone.

    Beyond identity (description, ownership, lifecycle, membership rules and
    repositories), the answer carries the activity series, the plugin
    measurement, the project's own findings and the conclusions the observatory
    has drawn about it — data about ONE project, in a shape something else can
    render.

    Each section carries its own degradation rather than one flag for the whole
    answer: a store that will not open must not make the registry's half of the
    truth disappear too.
    """
    # `value`, not `id` — the scope's own vocabulary. A scope missing its
    # selector would make every project come back "unknown", which is why
    # `_scope_error` exists on the wire.
    result = survey({"kind": "project", "value": project_id}, include_external=True)
    if not result["projects"]:
        # The id is quoted only when it is a well-formed, non-credential-shaped
        # project id: a key pasted into this field must not come back in the
        # refusal (and from there into a transcript).
        return {"error": "unknown project",
                "projectId": credential_shape.shown(project_id, pattern=PROJECT_ID),
                "hint": "call observatory_status to list what exists",
                "degraded": result["degraded"]}
    out = {"surveyedAt": result["surveyedAt"], "scanId": result["scanId"],
           "project": result["projects"][0],
           "activity": {"weeks": [], "totals": {}},
           "measurements": [], "notes": [], "findings": [],
           "recentEvents": [],
           "evidence": result["evidence"], "degraded": list(result["degraded"])}
    if timeline_limit:
        tl = timeline(project_id, limit=timeline_limit)
        out["recentEvents"] = tl["events"]
        out["degraded"] += [d for d in tl["degraded"] if d not in out["degraded"]]

    # EVERY id this project's history may be under, not only its current one.
    # Publishing a project renames it, and the rows written before that stay
    # keyed to the old name.
    pids = ids_for_project(project_id)

    try:
        conn = store_db.connect()
    except sqlite3.Error as exc:
        out["degraded"].append({"source": "store",
                                "reason": f"the store did not open: {type(exc).__name__}"})
        conn = None
    if conn is not None:
        try:
            for r in conn.execute(
                    "SELECT week_start, commits, sessions, worked_days, frozen_at,"
                    "       active_days, authors"
                    f" FROM project_week WHERE project_id IN ({_marks(pids)})"
                    "   AND week_start >= date('now', ?) ORDER BY week_start",
                    (*pids, f"-{weeks * 7} days")):
                # NULL is carried through as null, never as 0: a row computed
                # before the session columns existed cannot say "no sessions",
                # only "not measured".
                out["activity"]["weeks"].append(
                    {"weekStart": r["week_start"], "commits": r["commits"],
                     "sessions": r["sessions"], "workedDays": r["worked_days"],
                     "activeDays": r["active_days"], "authors": r["authors"],
                     # `frozen_at` is a TIMESTAMP, not a flag: a frozen week can
                     # never be completed, and a reader needs to know when it was
                     # closed to judge what is missing from it.
                     "frozenAt": r["frozen_at"]})
            wk = out["activity"]["weeks"]
            out["activity"]["totals"] = {
                "weeks": len(wk),
                "commits": sum(w["commits"] or 0 for w in wk),
                "sessions": sum(w["sessions"] or 0 for w in wk
                                if w["sessions"] is not None),
                "workedDays": sum(w["workedDays"] or 0 for w in wk
                                  if w["workedDays"] is not None),
                "sessionsUnmeasured": sum(1 for w in wk if w["sessions"] is None)}
            if not wk:
                out["degraded"].append({
                    "source": "rollup",
                    "reason": f"no weekly rollup for this project in the last {weeks} "
                              f"week(s); `project-observatory full rollup` computes it"})

            # LATEST PER METRIC, and the metric names come from the data. A core
            # file naming a plugin's metric is the defect the plugin suite
            # exists to catch.
            # THE PREVIOUS SAMPLE BESIDE THE LATEST ONE. A metric layer that
            # reports only the newest value answers "how big" and never "which
            # way" — and `plugins/disk-usage.json`'s stated reason is precisely
            # the second question. `previous` is null when there is
            # only one sample, which is the honest answer rather than a change
            # of zero: no trend has been measured yet.
            for r in conn.execute(
                    "SELECT m.metric, m.unit, m.value, m.at, m.source,"
                    "       (SELECT value FROM metrics q WHERE q.project_id = m.project_id"
                    "          AND q.metric = m.metric AND q.at < m.at"
                    "        ORDER BY q.at DESC LIMIT 1) AS prev_value,"
                    "       (SELECT at FROM metrics q WHERE q.project_id = m.project_id"
                    "          AND q.metric = m.metric AND q.at < m.at"
                    "        ORDER BY q.at DESC LIMIT 1) AS prev_at"
                    " FROM metrics m"
                    f" JOIN (SELECT metric, MAX(at) at FROM metrics"
                    f" WHERE project_id IN ({_marks(pids)})"
                    "       GROUP BY metric) l ON l.metric = m.metric AND l.at = m.at"
                    f" WHERE m.project_id IN ({_marks(pids)})"
                    " ORDER BY m.metric", (*pids, *pids)):
                prev = r["prev_value"]
                out["measurements"].append(
                    {"metric": r["metric"], "unit": r["unit"], "value": r["value"],
                     "at": r["at"], "source": r["source"],
                     "previous": prev, "previousAt": r["prev_at"],
                     "change": None if prev is None else r["value"] - prev})

            for r in conn.execute(
                    "SELECT l.memory_id, l.revision, l.created_at, l.statement, l.why,"
                    "       l.state, l.confidence, l.owner FROM ledger l"
                    + LIVE_LEDGER_JOIN +
                    f" WHERE l.project_id IN ({_marks(pids)}) AND" + LIVE_LEDGER_WHERE +
                    " ORDER BY l.created_at DESC LIMIT ?", (*pids, notes_limit)):
                out["notes"].append(
                    {"memoryId": r["memory_id"], "revision": r["revision"],
                     "createdAt": r["created_at"], "statement": r["statement"],
                     "why": r["why"], "state": r["state"],
                     "confidence": r["confidence"], "owner": r["owner"]})
        except sqlite3.Error as exc:
            out["degraded"].append({"source": "store",
                                    "reason": f"the store is unreadable: {str(exc)[:80]}"})
        finally:
            conn.close()

    # THIS PROJECT'S FINDINGS, from the file the estate's own tooling writes.
    # Acknowledged ones are withheld for the same reason the dashboard withholds
    # them: the operator silenced them, and a surface that shows them anyway
    # teaches that silencing does nothing.
    # ITS REPOSITORIES' FINDINGS TOO, and the first version had only the
    # project's own. Of the live set, 2 findings carry a `project:` subject and
    # **20 carry a `repository:` or `clone:` one** — dirty checkouts, diverged
    # branches, stale remotes — so a per-project view built on the subject alone
    # showed nothing for almost every project that had something wrong with it.
    about = finding_matcher(project_id, out["project"])
    try:
        doc = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8"))
        for f in doc.get("findings", []):
            if f.get("acked") or not about(f.get("subject") or ""):
                continue
            row = {k: f[k] for k in ("type", "severity", "title", "action") if k in f}
            # THE SUBJECT TRAVELS WITH IT. Without it a renderer showing five
            # findings under one project cannot say which repository each is
            # about, and "a branch exists only locally" is unactionable without
            # knowing where.
            row["subject"] = f.get("subject")
            row["aboutThisProject"] = f.get("subject") == project_id
            out["findings"].append(row)
    except (ValueError, OSError) as exc:
        out["degraded"].append({"source": "findings",
                                "reason": f"findings.json is unreadable: "
                                          f"{type(exc).__name__}"})
    return out


def finding_matcher(project_id: str, view: dict | None = None):
    """A predicate: is a finding with this `subject` about this project?

    One answer for every surface that groups findings by project — the project
    detail, the MCP findings filter and the assistant's evidence — because three
    private versions of it disagreed: the assistant matched `project_id`, a key
    no finding carries, and saw nothing for any project.

    A subject names the thing that is wrong, so a project owns it through what it
    holds: the project itself, its repositories and their clones, the hosts its
    sites answer on, and secrets and env files kept in its local folders
    (`secret:<folder>/…`, `credential:vault/<folder>/…`, `env:<folder>/…`)."""
    if view is None:
        projects, repos, members = _index()
        p = next((x for x in projects if x["id"] == project_id), None)
        view = _project_view(p, repos, members) if p else {"id": project_id, "repositories": []}
    exact = {project_id}
    for r in view.get("repositories", []):
        exact.add(r.get("id", ""))
        nwo = r.get("nameWithOwner") or ""
        if nwo:
            exact.add(f"clone:{nwo.split('/')[-1]}")
    for site in view.get("sites", []):
        for host in (site.get("host"), site.get("ownedDomain")):
            if host:
                exact.add(f"domain:{host}")
    folders = {Path(f).name for f in view.get("localFolders", []) if f}
    prefixes = tuple(f"{kind}{folder}/" for folder in folders
                     for kind in ("secret:", "credential:vault/", "env:"))
    return lambda subject: subject in exact or (bool(prefixes) and subject.startswith(prefixes))


def credentials(project_id: str) -> dict:
    """What a project can authenticate with, by NAME, and how to use one blind.

    NO VALUE CAN REACH THIS FUNCTION'S ANSWER, because no value reaches the
    documents it reads: `registry/env-inventory.json` holds names and classes,
    and the vault is listed by directory rather than opened. That is not a
    promise this function keeps by being careful — it is a property of its
    inputs, which is the only kind worth stating.

    `use` is the point. An agent that needs a key does not need to READ it, and
    reading one "to check" is how values end up in session transcripts. The
    command in the answer places the value in a child's environment and strips
    it from everything that child prints.
    """
    slug = project_id.split(":", 1)[1] if project_id.startswith("project:") else project_id
    pid = f"project:{slug}"
    # THE FOLDER, NOT THE ID'S SUFFIX. Slots and env rows are keyed by the name a
    # project's folder has on disk (`vault.py put alpha-web …`); a local-only
    # project's id is `project:local-alpha-web`. Taking the suffix as the folder
    # answered two empty lists and a `use` command use_secret could not resolve.
    try:
        registry = _index()[0]
    except (OSError, ValueError, KeyError):
        registry = None
    record = next((p for p in registry or [] if p.get("id") == pid), None)
    if record is None and registry is not None:
        # A FOLDER NAME IS A PROJECT NAME TOO: `alpha-web` names the project
        # whose id is `project:local-alpha-web`, by the vault's own rule.
        import vault_project
        res = vault_project.resolve(project_id, registry)
        if res.project_id and res.how != "ambiguous":
            pid = res.project_id
            slug = pid.split(":", 1)[1]
            record = next((p for p in registry if p.get("id") == pid), None)
    names = [slug] if record is None else []
    for folder in (record or {}).get("local_folders") or []:
        if folder and Path(folder).name not in names:
            names.append(Path(folder).name)
    if record is not None and slug not in names:
        names.append(slug)
    out: dict = {"projectId": pid, "project": names[0] if names else slug,
                 "env": [], "vault": [], "degraded": []}

    env_owner = None
    doc_path = paths.REGISTRY / "env-inventory.json"
    if not doc_path.is_file():
        out["degraded"].append({
            "source": "env-inventory",
            "reason": "registry/env-inventory.json does not exist — `project-observatory full "
                      "env` builds it",
            "effect": "no env file is known for this project, which is not the same "
                      "as it having none"})
    else:
        try:
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            doc = {"files": []}
            out["degraded"].append({
                "source": "env-inventory",
                "reason": f"unreadable: {type(exc).__name__}",
                "effect": "the env half of this answer is empty for a reason that is "
                          "not absence"})
        for f in doc.get("files", []):
            if f.get("project") not in names:
                continue
            env_owner = env_owner or f.get("project")
            for v in f.get("variables", []):
                row = {"name": v["name"], "class": v["class"], "file": f["path"],
                       "kind": f["kind"]}
                if v.get("shared_with"):
                    row["shared_with"] = v["shared_with"]
                if v.get("available_in"):
                    row["available_in"] = v["available_in"]
                out["env"].append(row)

    root = Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR",
        paths.source_path("secret_store", paths.SECRETS) / "projects"))
    if not root.is_dir():
        out["degraded"].append({
            "source": "vault",
            "reason": f"{root} does not exist on this machine",
            "effect": "managed slots are unknown, so a name absent below may still "
                      "exist"})
    else:
        for name in names:
            vault = root / name
            if not vault.is_dir():
                continue
            for envdir in sorted(p for p in vault.iterdir() if p.is_dir()):
                for slot in sorted(envdir.iterdir()):
                    # A SLOT NAME, as use_secret reads one: `NAME.meta.json` and a
                    # rotation's `NAME.retired-…` archive sit beside it and are not slots.
                    if slot.is_file() and re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", slot.name):
                        # EACH ROW SAYS WHICH FOLDER HOLDS IT, AND HOW TO USE IT.
                        # Slots can sit under two folders of one project (its
                        # folder, and its registry slug from before the vault
                        # normalised names); one top-level `use` naming the last
                        # folder read sent `use_secret` to the wrong one for the
                        # others.
                        out["vault"].append({
                            "name": slot.name, "env": envdir.name, "project": name,
                            "use": (f"python \"$(project-observatory full-path)/tools/use_secret.py\" "
                                    f"run --env {envdir.name} {name} {slot.name} -- <command>")})

    out["totals"] = {
        "env_variables": len(out["env"]),
        "secrets": sum(1 for r in out["env"] if r["class"] == "secret"),
        "unfilled": sum(1 for r in out["env"]
                        if r["class"] in ("empty", "placeholder")),
        "vault_slots": len(out["vault"]),
    }
    # The top-level `project` is the folder a NEW slot goes under — the
    # project's first folder, the one `vault.py put` files it under — or, with
    # no folder, the folder that holds this project's slots or env files.
    if record is None or not (record or {}).get("local_folders"):
        held = [r["project"] for r in out["vault"]]
        out["project"] = held[0] if held else (env_owner or out["project"])
    out["use"] = (f"python \"$(project-observatory full-path)/tools/use_secret.py\" run [--env ENV] {out['project']} <NAME> -- <command>  "
                  "# injects named values and redacts exact matches from captured output; "
                  "not a sandbox against encoded output or network transmission")
    out["never"] = "Do not print a credential value into a transcript or public report."
    return out


#: A project id a refusal may quote back; anything else is described by length.
PROJECT_ID = re.compile(r"project:[A-Za-z0-9._/-]{1,180}")


def valid_since(since: str | None) -> bool:
    """An ISO-8601 date or timestamp — the only `since` a filter can honour.

    The filter is a string comparison on `occurred_at`, so `since: "garbage"`
    sorted after every timestamp and answered `events: []` with an empty
    `degraded`: "nothing happened" where the truth was "the filter meant
    nothing"."""
    if since is None:
        return True
    try:
        datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        return False
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", str(since)))


def timeline(project_id: str, since: str | None = None, limit: int = 100) -> dict:
    """Commits and recorded events, newest first. A `since` that is no date is
    NOT applied, and `degraded` says so instead of answering an empty list."""
    if valid_since(since):
        return _timeline(project_id, since, limit)
    out = _timeline(project_id, None, limit)
    out["since"] = None
    out.setdefault("degraded", []).append({
        "source": "since",
        "reason": f"since {credential_shape.echo(since)} is not an ISO-8601 date or "
                  f"timestamp, so it was not applied: these are the newest events "
                  f"unfiltered"})
    return out


def _timeline(project_id: str, since: str | None = None, limit: int = 100) -> dict:
    # A STORE THAT WILL NOT OPEN IS A DEGRADATION, not a traceback. This had no
    # guard at all: `store/observatory.db` was found CORRUPT on 2026-09-06 —
    # 434 MB with no SQLite header — and in that state this function raised
    # `DatabaseError: file is not a database` straight out through
    # `project.timeline` and, because `project_detail` calls it first, out
    # through `project.detail` as well. Found 2026-09-07 by a test that planted
    # a corrupt file rather than a missing one; a missing store was handled and
    # a broken one was not.
    try:
        conn = store_db.connect()
    except sqlite3.Error as exc:
        return {"projectId": project_id, "since": since, "events": [],
                "degraded": [{"source": "events",
                              "reason": f"the store did not open: "
                                        f"{type(exc).__name__}: {str(exc)[:80]}"}]}
    try:
        pids = ids_for_project(project_id)
        sql = ("SELECT kind, ref, actor, occurred_at, payload_json FROM events"
               f" WHERE project_id IN ({_marks(pids)})")
        args: list = list(pids)
        if since:
            sql += " AND occurred_at >= ?"
            args.append(since)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        args.append(limit)
        # CAMEL CASE, AND THE PAYLOAD PARSED. `dict(r)` handed the store's own
        # column names to the wire — `occurred_at`, `payload_json` — while every
        # other field this server returns is camelCase, and the payload arrived
        # as a JSON *string* the caller had to parse itself. Both were invisible
        # while nothing validated this tool's output; publishing a schema over
        # them would have made the store's serialisation part of the contract
        #.
        rows = []
        for r in conn.execute(sql, args):
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except ValueError:
                payload = {}
            rows.append({"kind": r["kind"], "ref": r["ref"], "actor": r["actor"],
                         "occurredAt": r["occurred_at"], "payload": payload})
        try:
            degraded = _identity_warnings(_load('projects.json')['projects'], project_id)
        except (OSError, ValueError, KeyError):
            degraded = []
        if not conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
            degraded.append({"source": "events",
                             "reason": "the event store is empty; run collectors/scan_events.py"})
        return {"projectId": project_id, "since": since, "events": rows,
                "degraded": degraded}
    except sqlite3.Error as exc:
        return {"projectId": project_id, "since": since, "events": [],
                "degraded": [{"source": "events",
                              "reason": f"the store is unreadable: {str(exc)[:80]}"}]}
    finally:
        conn.close()


def fts_query(query: str) -> str:
    """The caller's words as an FTS5 expression that cannot be a syntax error.

    FTS5's `MATCH` takes a query LANGUAGE rather than text, so passing a
    question straight in breaks on ordinary input and destroys the lexical
    half, the one whose entire purpose is to answer when the other cannot:

        "don't"                 fts5: syntax error near "'"
        "a - b"                 no such column: b
        "store/raw"             fts5: syntax error near "/"
        "foo(bar)"              fts5: syntax error near "foo"
        '"unbalanced'           unterminated string

    `no such column` is the sharpest of them: FTS5 reads a bare word as a column
    name, so the index's own columns would be addressable from a user's
    question. Nothing is injectable — the value is parameterised — but the
    query language would be exposed, and an apostrophe would return nothing.

    Every term becomes a quoted PHRASE, with embedded quotes doubled, joined by
    `OR`. `OR` rather than the implicit `AND`: bm25 already ranks a record
    matching more terms higher, so `OR` plus `rank` behaves like `AND` at the
    top of the list without the cliff where one unusual word makes a five-word
    question match nothing.

    A caller who wants FTS5's operators — `NEAR`, a column filter, a prefix `*`
    — is deliberately not served: the tool's own description promises "a lexical
    match", not an expression language, and an interface where ordinary text is
    a syntax error is the wrong trade in both directions.
    """
    terms = [t for t in re.findall(r"[\w'-]+", query, flags=re.UNICODE) if t]
    if not terms:
        # Not a query FTS5 can answer, and not an error either: the caller asked
        # for punctuation. An empty MATCH raises, so the lexical half is skipped
        # by returning an expression that matches nothing rather than by a
        # special case the reader has to find.
        return '""'
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def coverage_floor(n_keys: int) -> float:
    """The share of a question's subject words a lexical hit must carry.

    Calibrated on the evaluation set (tools/memory_eval.py): high enough that a
    question nothing answers comes back empty instead of matched on one shared
    word, low enough that a short question still finds its record. A question of
    one or two subject words needs one of them; a longer one, `COVERAGE` of them."""
    if n_keys <= 2:
        return 1 / max(n_keys, 1)
    return COVERAGE


#: The share of subject words a hit must carry once a question has three or more.
#: Measured, not chosen (`tools/memory_eval.py`, 2026-10-03): from 0.34 to 0.5 every
#: answerable question was found and every unanswerable one abstained; at 0.6 two
#: answerable ones were refused, at 0.25 two unanswerable ones were answered. 0.4
#: sits in the middle of that plateau. Paraphrases with no shared word are not a
#: lexical floor's to catch — similarity is (OBS-04).
COVERAGE = 0.4


#: How many candidates each retrieval arm fetches, INDEPENDENT of the caller's
#: page size. Scaling the fetch with `limit` would make `total` — which exists
#: to say how much was left — change with the page, a silent cap in disguise.
#:
#: A fixed window is still a cap, and that is the point: it is stated, the same
#: for every caller, and `truncated` is honest relative to it. The vector arm is
#: inherently top-k — every embedded row is a neighbour at some distance — so no
#: uncapped "true total" exists across both arms.
RETRIEVAL_WIDTH = 300


#: A ledger row is a CURRENT, visible candidate: its latest revision, not erased, inside
#: its validity, in the caller's project and classes. Applied INSIDE the candidate query,
#: before the window (PB-137 N-009), so rows the caller may not see neither take a place
#: in the window nor move a count. `{l}` is the ledger alias.
_CURRENT = ("NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.memory_id = {l}.memory_id)"
            " AND {l}.revision = (SELECT MAX(x.revision) FROM ledger x"
            " WHERE x.memory_id = {l}.memory_id)"
            " AND ({l}.valid_to IS NULL OR datetime({l}.valid_to) IS NULL"
            " OR datetime({l}.valid_to) > datetime('now'))"
            " AND ({l}.valid_from IS NULL OR datetime({l}.valid_from) IS NULL"
            " OR datetime({l}.valid_from) <= datetime('now'))")


def _scope_sql(alias: str, project_id: str | None,
               classes: tuple[str, ...] | None) -> tuple[str, list]:
    """The `_CURRENT` predicate plus the caller's project and classes, with its args."""
    sql, args = _CURRENT.format(l=alias), []
    if project_id:
        sql += f" AND {alias}.project_id = ?"
        args.append(project_id)
    if classes is not None:
        sql += (f" AND {alias}.classification IN ({','.join('?' * len(classes))})"
                if classes else " AND 0")
        args += list(classes)
    return sql, args


def search(query: str, project_id: str | None = None, limit: int = 10, *,
           authority: str = "caller-argument", classification: str | None = None,
           classes: tuple[str, ...] | None = None) -> dict:
    """Recall over the narrative: by similarity where possible, lexically always.

    Two rules the contract makes non-negotiable, and both are visible in the
    result rather than described in a docstring nobody reads:

    * **Conflicting records come back together.** A `contested` hit sits beside
      the `supported` one it disagrees with, and nothing here ranks them.
      Collapsing a disagreement is how a memory becomes confidently wrong.
    * **Absence is not proof of absence.** The `degraded` list names every path
      that could not run, and the note says so in the answer.
    """
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent / "agent"))
    from store import indexer

    conn = store_db.connect()
    degraded: list[dict] = []
    hits: dict[tuple[str, int], dict] = {}
    #: Each half's own ORDER, kept so the two can be fused rather than compared.
    vector_order: list[tuple[str, int]] = []
    lexical_order: list[tuple[str, int]] = []
    try:
        have_vec = indexer.load_vec(conn)
        # --- the embedding policy decides first (PB-137 N-003) ----------------
        # The query's words leave the machine only under a context the SERVER
        # derived (`binding`, `operator-cli`) in one consented project. An MCP
        # caller's arguments are `caller-argument`, so its query stays here and the
        # lexical half answers; `degraded` says so. No budget check, no key read.
        import embedding_policy
        verdict = None
        policy_now = None
        if have_vec:
            try:
                policy_now = embedding_policy.current()
                verdict = embedding_policy.for_query(
                    policy_now,
                    {"project_id": project_id, "classification": classification,
                     "authority": authority},
                    configured=embedding_policy.configured_model())
            except embedding_policy.PolicyError as exc:
                verdict = embedding_policy.Verdict("local", "policy-invalid", 0)
                degraded.append({"source": "embedding-policy", "reason": str(exc)})
            if not verdict.remote:
                degraded.append({"source": "vector",
                                 "reason": f"embedding policy keeps this query on the machine "
                                           f"({verdict.reason}); lexical only"})
        # --- similarity, when the extension, a key and the policy allow it ---
        if have_vec and verdict is not None and verdict.remote:
            try:
                import providers
                from sqlite_vec import serialize_float32
                # THE CEILING APPLIES HERE TOO. `embed()` charges the wallet, and
                # this path is reachable by any MCP client, so the budget is
                # CHECKED before spending, exactly as the agent checks its own.
                #
                # A reached guardrail is a DEGRADATION, not a refusal: the
                # lexical half still answers, and `degraded` says why similarity
                # was skipped — which is the shape the contract requires of an
                # answer that could not be complete.
                stop = providers.check_budget()
                if stop:
                    raise RuntimeError(f"spend guardrail reached — {stop}")
                vec = providers.embed([query], authorization=verdict)["vectors"][0]
                sql = ("SELECT v.memory_id, v.revision, v.distance FROM ("
                       "  SELECT memory_id, revision, distance FROM vec_notes"
                       "  WHERE embedding MATCH ? ORDER BY distance LIMIT ?) v")
                configured = embedding_policy.configured_model()
                for r in conn.execute(sql, (serialize_float32(vec), RETRIEVAL_WIDTH)):
                    key = (r["memory_id"], r["revision"])
                    # A vector answers only for a record that may be embedded by
                    # this model NOW, in the asked project: legacy vectors of a
                    # project without consent, or under a revoked one, are not served.
                    rec = conn.execute(
                        "SELECT project_id, classification, scope, kind FROM ledger"
                        " WHERE memory_id = ? AND revision = ?", key).fetchone()
                    if rec is None or rec["project_id"] != project_id or not \
                            embedding_policy.for_record(policy_now, dict(rec),
                                                        configured=configured).remote:
                        continue
                    vector_order.append(key)
                    hits[key] = {
                        "memoryId": r["memory_id"], "revision": r["revision"],
                        "distance": round(r["distance"], 6), "matched": ["vector"]}
                # Said: this legacy index has no metadata to filter on, so its nearest
                # neighbours are chosen over every project and filtered after. Filtering
                # before the KNN comes with an admitted local model's namespace (N-006).
                degraded.append({"source": "vector-window",
                                 "reason": "vector candidates were filtered after the nearest-"
                                           "neighbour search, so fewer in-scope matches may "
                                           "be returned than exist"})
            except Exception as exc:
                degraded.append({"source": "vector",
                                 "reason": f"{type(exc).__name__}: {exc}"})
        elif not have_vec:
            degraded.append({"source": "vector",
                             "reason": "sqlite-vec is not loadable here"})

        # --- lexical, always. It is the path that still answers when the other
        # --- one cannot, which is exactly when a caller most needs an answer.
        # BY SEARCH KEY, NOT BY SPELLING: each word's Snowball stem, Russian cut to
        # six characters (textkeys.py), so a question finds a record in another
        # word form. Only the question's subject words are asked for — "what",
        # "как", "the" match everything and say nothing.
        import textkeys
        qkeys = textkeys.query_keys(query)
        lexical_floor = coverage_floor(len(qkeys))
        if not textkeys.STEMMER:
            degraded.append({"source": "lexical",
                             "reason": "no stemmer installed (snowballstemmer): word forms "
                                       "are matched exactly"})
        try:
            if qkeys:
                # SCOPE BEFORE THE WINDOW (PB-137 N-009): the ledger row behind each
                # match must be current and the caller's before it may take one of
                # the `RETRIEVAL_WIDTH` places; filtering after the LIMIT let three
                # hundred stronger foreign matches push the caller's own record out.
                scope_sql, scope_args = _scope_sql("l", project_id, classes)
                fts = ("SELECT search_notes.memory_id AS memory_id,"
                       " search_notes.revision AS revision, search_notes.rank AS rank,"
                       " search_notes.stems AS stems FROM search_notes"
                       " JOIN ledger l ON l.memory_id = search_notes.memory_id"
                       " AND l.revision = search_notes.revision"
                       f" WHERE search_notes MATCH ? AND {scope_sql}"
                       " ORDER BY search_notes.rank LIMIT ?")
                expr = "stems : (" + " OR ".join('"' + k.replace('"', '""') + '"'
                                                 for k in qkeys) + ")"
                below = 0
                window = 0
                for r in conn.execute(fts, (expr, *scope_args, RETRIEVAL_WIDTH)):
                    window += 1
                    # THE FLOOR. An OR over the question's keys matches any record
                    # sharing one of them, and a best-of-the-bad answer is how a
                    # search invents memory. A hit must carry enough of what was
                    # asked; one that does not is dropped, and counted.
                    have = set((r["stems"] or "").split())
                    covered = sum(1 for k in qkeys if k in have) / len(qkeys)
                    if covered < lexical_floor:
                        below += 1
                        continue
                    k = (r["memory_id"], r["revision"])
                    lexical_order.append(k)
                    if k in hits:
                        hits[k]["matched"].append("lexical")
                        hits[k]["rank"] = round(r["rank"], 6)
                        hits[k]["coverage"] = round(covered, 3)
                    else:
                        hits[k] = {"memoryId": r["memory_id"], "revision": r["revision"],
                                   "rank": round(r["rank"], 6), "coverage": round(covered, 3),
                                   "matched": ["lexical"]}
                if window >= RETRIEVAL_WIDTH:
                    # Said, not hidden: the window is full, so a weaker match the
                    # caller may see can lie beyond it.
                    degraded.append({"source": "window",
                                     "reason": f"the lexical window is full ({RETRIEVAL_WIDTH} "
                                               f"current matches in scope); weaker matches "
                                               f"beyond it were not ranked"})
            else:
                below = 0
        except sqlite3.Error as exc:
            degraded.append({"source": "lexical", "reason": str(exc)})
            below = 0

        # --- the LAG, which both halves search over and neither reported ------
        # Both indexes are fed by the outbox, so a pending queue means this
        # answer was computed over an index that does not yet contain the newest
        # conclusions. The old `degraded` list named the store, bitbucket and the
        # domain scan and said nothing about the projection — so a search running
        # against an index a hundred revisions behind answered with full
        # confidence. The age comes from the ledger: an append and its outbox row
        # are one transaction, so `ledger.created_at` IS the enqueue time and no
        # column had to be added to learn it.
        try:
            # IN THE CALLER'S SCOPE: another project's queue is not this answer's lag,
            # and counting it would tell a binding that rows it may not see exist.
            lag_sql, lag_args = _scope_sql("l", project_id, classes)
            lag = conn.execute(
                "SELECT count(*) AS n, min(l.created_at) AS oldest FROM outbox o"
                " JOIN ledger l ON l.memory_id = o.memory_id AND l.revision = o.revision"
                f" WHERE o.consumed_at IS NULL AND {lag_sql}", lag_args).fetchone()
            if lag and lag["n"]:
                since = f", the oldest committed {lag['oldest']}" if lag["oldest"] else ""
                degraded.append({
                    "source": "projection",
                    "reason": f"{lag['n']} committed revision(s) are not indexed yet"
                              f"{since}; this answer cannot include them"})
        except sqlite3.Error as exc:
            degraded.append({"source": "projection", "reason": f"lag unknown: {exc}"})

        # --- hydrate from CANON, never from the projection --------------------
        out, contested = [], []
        for (mid, rev), h in hits.items():
            row = conn.execute(
                "SELECT l.*, t.memory_id AS tomb FROM ledger l"
                " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
                " WHERE l.memory_id = ? AND l.revision = ?", (mid, rev)).fetchone()
            if row is None or row["tomb"] is not None:
                continue                                                   
            if project_id and row["project_id"] != project_id:
                continue
            # THE CANONICAL RECHECK, the same predicate the lexical window used:
            # a vector hit was never filtered before its KNN, so its validity and
            # visibility are decided here.
            hscope, hargs = _scope_sql("l", project_id, classes)
            if conn.execute(f"SELECT 1 FROM ledger l WHERE l.memory_id = ? AND l.revision = ?"
                            f" AND {hscope}", (mid, rev, *hargs)).fetchone() is None:
                continue
            # A BINDING'S CLASS CEILING (PB-137 N-008): a record above it is not
            # served. `classes` is None for the local agent, which reads every class.
            if classes is not None and row["classification"] not in classes:
                continue
            # THE LATEST REVISION ONLY. An index can still hold a superseded
            # revision — one written before the indexer removed older ones, or
            # one whose newer revision is not indexed yet — and serving it
            # would present a replaced statement as live. A workflow's
            # checkpoint revises once per step, so this is the difference
            # between one answer and thirty stale ones.
            latest = conn.execute("SELECT MAX(revision) FROM ledger WHERE memory_id = ?",
                                  (mid,)).fetchone()[0]
            if rev != latest:
                continue
            out.append({**h, "projectId": row["project_id"], "state": row["state"],
                        "owner": row["owner"], "confidence": row["confidence"],
                        "statement": row["statement"], "why": row["why"],
                        "createdAt": row["created_at"],
                        "conflictsWith": json.loads(row["conflicts_with_json"] or "[]")})
            if row["state"] == "contested":
                contested.append(mid)
        # RECIPROCAL RANK FUSION, because a vector distance and a bm25 rank are
        # not comparable. Sorting on them together would put a perfect lexical
        # match below every weak semantic one, and bm25 ranks are NEGATIVE, so a
        # hit with no rank defaulting to 0 would sort after every real one.
        #
        # RRF needs no shared scale: each half contributes 1/(k + position) for
        # the records it returned, and a record missing from one list simply
        # contributes nothing from it. `k = 60` is the constant from the paper
        # the method comes from; the value matters little here because both
        # lists are at most `RETRIEVAL_WIDTH` long, and it is written down rather than
        # tuned so nobody reads a fitted number into it.
        RRF_K = 60
        fused: dict[tuple[str, int], float] = {}
        for ordered in (vector_order, lexical_order):
            for position, key in enumerate(ordered):
                fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + position + 1)
        for h in out:
            h["score"] = round(fused.get((h["memoryId"], h["revision"]), 0.0), 8)
        out.sort(key=lambda h: -h["score"])
        # AN HONEST "NOTHING". When no record clears the floor the answer says
        # so as a decision, not as an empty list a caller might read as a bug:
        # "this is not in memory" is an answer, and the one a caller must give.
        abstain = not out
        reason = ("no subject words in the question" if not qkeys else
                  f"{below} lexical match(es) below the coverage floor" if below else
                  "nothing matched")
        return {"query": query, "projectId": project_id, "count": len(out[:limit]),
                "abstain": abstain, **({"abstainReason": reason} if abstain else {}),
                "floor": {"coverage": lexical_floor, "keys": len(qkeys),
                          "belowFloor": below},
                # THE SCOPE WITHIN THE RETRIEVAL WINDOW, not the page — and not a
                # function of it. `count` is the page size; `total` says how much
                # the window held, so a caller can tell what was left out.
                "total": len(out), "truncated": len(out) > limit,
                "results": out[:limit], "contested": contested, "degraded": degraded,
                "note": ("conflicting records are returned together and are not ranked; "
                         "absence from this result is not proof that a record does not "
                         "exist, and `degraded` names every path that could not run")}
    finally:
        conn.close()
