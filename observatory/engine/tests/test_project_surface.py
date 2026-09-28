#!/usr/bin/env python3
"""One project, as a thing another system can render — and the schema that lied.

The brief for this system asked for two surfaces: the estate's dashboard, and
**the data for one project, in a shape something else can render**. The second
existed as a tool and did not work as a contract.

**Measured once, on a real estate.** `estate.survey` declared ONE `outputSchema` and THREE
required tools. Validating each tool's live answer against that schema:

    observatory_status     VALIDATES
    observatory_project    FAILS: 'project' was unexpected
    observatory_timeline   FAILS: 'events', 'projectId', 'since' were unexpected

A host doing exactly what the contract tells it — compile the published schema,
validate the answer — would have rejected two of the three tools it was promised.
Both unprobed: the three declared probes all exercise the survey, which is the
one tool that passes. That is what an unprobed required feature is for.

**And the answer was thin.** `observatory_project` returned identity alone —
description, ownership, lifecycle, membership rules, repositories — so a host
asked to render "this project" got no activity series, no plugin measurement,
none of the project's findings and none of the conclusions the observatory had
drawn about it. All four are in the store. All four are on the dashboard. Only
the wire could not carry them.

Three capabilities now, one tool each, each with its own schemas and its own
probe: `estate.survey`, `project.detail`, `project.timeline`.

**One defect fell out of the same reading**, following through on erasure:
`dashboard/build_dashboard.py`'s notes query had no tombstone join at all — so an
erased conclusion was hidden from `live()`, the search, the review queue and the
findings, and rendered on the one surface a person looks at.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, sys

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import surface_fixture
OWN = surface_fixture.PROJECT
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir

FAILURES: list[str] = []
MANIFEST = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
SCHEMAS = ROOT / "fabric/schemas"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def schema_of(cap: dict, which: str = "outputSchema") -> dict:
    return json.loads((SCHEMAS / cap[which].rsplit("/", 1)[1]).read_text(encoding="utf-8"))


def caps() -> dict[str, dict]:
    return {c["name"]: c for c in MANIFEST["capabilities"]}


# ─────────── the rule that would have caught it ────────────────────────

def test_every_capability_declares_the_tools_its_schema_describes() -> None:
    """A capability is ONE input/output pair. Three tools under one schema is
    three different answers claiming one shape, and two of them were wrong."""
    for name, cap in caps().items():
        tools = [f for f in cap["profile"]["requiredFeatures"] if f.startswith("tool:")]
        if cap.get("effect") == "none":
            check(f"{name} declares exactly one tool", len(tools) == 1, str(tools))
        probed = {p["id"] for p in cap["profile"].get("probes", [])}
        check(f"{name} has at least one probe", bool(probed), str(sorted(probed)))
        check(f"{name}'s schemas exist locally",
              (SCHEMAS / cap["outputSchema"].rsplit("/", 1)[1]).is_file() and
              (SCHEMAS / cap["inputSchema"].rsplit("/", 1)[1]).is_file(),
              f"{cap['inputSchema'].rsplit('/', 1)[1]} / "
              f"{cap['outputSchema'].rsplit('/', 1)[1]}")


def test_each_read_capabilitys_answer_validates_against_its_own_schema() -> None:
    """The measurement, as a standing rule. It is driven in-process rather than
    over stdio: the shape is the same either way, and the probes already cross
    the wire."""
    import survey
    importlib.reload(survey)
    live = {
        "estate.survey": lambda: survey.survey(
            {"kind": "project", "value": OWN}),
        "project.detail": lambda: survey.project_detail(
            OWN, timeline_limit=3),
        "project.timeline": lambda: survey.timeline(
            OWN, limit=5),
    }
    for name, produce in live.items():
        cap = caps()[name]
        doc = produce()
        try:
            jsonschema.validate(doc, schema_of(cap))
            ok, why = True, ""
        except jsonschema.ValidationError as exc:
            ok, why = False, exc.message[:140]
        check(f"{name}'s live answer validates against its published schema", ok, why)


# ─────────── the answer is worth rendering ─────────────────────────────

SECTIONS = ("project", "activity", "measurements", "notes", "findings",
            "recentEvents", "evidence", "degraded")


def test_the_surface_carries_what_a_renderer_needs() -> None:
    import survey
    importlib.reload(survey)
    d = survey.project_detail(OWN, timeline_limit=3)
    for k in SECTIONS:
        check(f"`{k}` is present", k in d, str(sorted(d)))
    check("the activity series has a week with commits in it",
          any((w.get("commits") or 0) > 0 for w in d["activity"]["weeks"]),
          json.dumps(d["activity"]["totals"]))
    check("and totals beside it", set(d["activity"]["totals"]) >= {
        "weeks", "commits", "sessions", "workedDays", "sessionsUnmeasured"},
        str(sorted(d["activity"]["totals"])))
    check("a plugin measurement reaches the wire with its unit and source",
          bool(d["measurements"]) and all({"metric", "unit", "value", "at", "source"} <= set(m)
              for m in d["measurements"]), json.dumps(d["measurements"][:1]))
    check("every conclusion carries the state that qualifies it",
          bool(d["notes"]) and all(n.get("state") for n in d["notes"]), str(len(d["notes"])))
    check("and its confidence, so a proposal cannot read as an assertion",
          all("confidence" in n for n in d["notes"]), "")


def test_an_unmeasured_week_stays_null_rather_than_becoming_zero() -> None:
    """The unmeasured-is-not-zero rule, on the wire. A week computed before the session columns
    existed cannot say "no sessions", only "not measured" — and the schema
    permits null for exactly that reason."""
    schema = schema_of(caps()["project.detail"])
    wk = schema["properties"]["activity"]["properties"]["weeks"]["items"]["properties"]
    for field in ("sessions", "workedDays", "activeDays", "authors", "frozenAt"):
        check(f"the schema permits null for `{field}`",
              "null" in wk[field]["type"], str(wk[field]))
    check("but not for commits, which is always measured",
          "null" not in str(wk["commits"]), str(wk["commits"]))


def test_a_repositorys_finding_reaches_its_project_labelled() -> None:
    """On a real estate, few findings carry a `project:` subject and most carry
    a `repository:` or `clone:` one. A per-project view built on the subject alone
    showed nothing for almost every project that had something wrong with it."""
    import survey
    importlib.reload(survey)
    doc = json.loads((__import__("paths").REGISTRY / "findings.json").read_text(encoding="utf-8"))
    by_repo = [f for f in doc["findings"]
               if (f.get("subject") or "").startswith("repository:")
               and not f.get("acked")]
    if not by_repo:
        check("the live set has a repository finding to trace", False,
              "the synthetic fixture plants one; nothing to drive this with")
        return
    repo_id = by_repo[0]["subject"]
    rels = json.loads((__import__("paths").REGISTRY / "relations.json").read_text(encoding="utf-8"))
    owner = next((r["from"] for r in rels["relations"]
                  if r["type"] == "implemented_by" and r["to"] == repo_id), None)
    check("the repository has an owning project", bool(owner), repo_id)
    if not owner:
        return
    d = survey.project_detail(owner, timeline_limit=0)
    got = [f for f in d["findings"] if f["subject"] == repo_id]
    check("its finding appears under the project", len(got) == 1,
          f"{owner} -> {[f['subject'] for f in d['findings']]}")
    if got:
        check("labelled as being about the repository, not the project",
              got[0]["aboutThisProject"] is False, str(got[0]))


# ─────────── the wire speaks one language ──────────────────────────────

def test_the_timeline_no_longer_hands_over_column_names() -> None:
    import survey
    importlib.reload(survey)
    t = survey.timeline(OWN, limit=3)
    check("there are events to look at", bool(t["events"]), str(t)[:160])
    for e in t["events"]:
        check("the stamp is camelCase", "occurredAt" in e and "occurred_at" not in e,
              str(sorted(e)))
        check("the payload is parsed", isinstance(e.get("payload"), dict),
              type(e.get("payload")).__name__)
    check("newest first", [e["occurredAt"] for e in t["events"]] ==
          sorted([e["occurredAt"] for e in t["events"]], reverse=True), "")


# ─────────── each section degrades alone ───────────────────────────────

def test_an_unreadable_store_does_not_erase_the_registrys_half() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-surface-"))
    (d / "observatory.db").write_bytes(b"this is not a database")
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    from store import db as store_db
    importlib.reload(store_db)
    import survey
    importlib.reload(survey)
    try:
        got = survey.project_detail(OWN,
                                    timeline_limit=3)
    except sqlite3.DatabaseError as exc:
        check("a corrupt store degrades rather than raising", False,
              f"{type(exc).__name__}: {exc}")
        return
    finally:
        os.environ.pop("OBSERVATORY_DB", None)
        importlib.reload(paths)
    check("the registry half of the answer survives",
          got.get("project", {}).get("id") == OWN,
          str(got)[:200])
    check("and the store names itself in `degraded`",
          any(x["source"] in ("store", "events") for x in got["degraded"]),
          json.dumps(got["degraded"])[:200])
    check("the sections are present and empty rather than absent",
          all(k in got for k in SECTIONS), str(sorted(got)))


# ─────────── an erased conclusion reaches no surface ───────────────────

def test_a_tombstoned_note_reaches_neither_surface() -> None:
    """The erasure follow-through. `live()`, the search, the review queue and
    the findings all join tombstones on `memory_id`; the dashboard's notes query
    joined nothing, so the one surface a person reads was the one that would
    have shown an erased conclusion."""
    import check_paths
    dash = check_paths.prose_removed(
        (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8"))
    check("the dashboard's notes query joins tombstones",
          "LIVE_LEDGER_JOIN" in dash and "LIVE_LEDGER_WHERE" in dash,
          "an erased conclusion would render on the operator's page")
    surv = check_paths.prose_removed((ROOT / "survey.py").read_text(encoding="utf-8"))
    check("and the rule is defined once, not spelled out twice",
          surv.count("LEFT JOIN tombstones t ON t.memory_id = l.memory_id") <= 2,
          "a rule written twice is a rule that will be written once")

    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tomb-"))
    # THE STORE is redirected; the registry is deliberately left alone, so the
    # project resolves against the fixture's fact base while the ledger under it
    # is a fresh store. `paths.REGISTRY` rather than an inlined path — the rule exists
    # because a test that cannot redirect its inputs silently reads the live
    # ones, and repeating the live path here would be that mistake spelled out.
    import paths
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_REGISTRY"] = str(paths.REGISTRY)
    importlib.reload(paths)
    from store import db as store_db
    importlib.reload(store_db)
    from store import ledger as L
    importlib.reload(L)
    import survey
    importlib.reload(survey)
    try:
        conn = store_db.connect()
        pid = OWN
        r = L.append(conn, owner="agent:test", statement="ERASED-CANARY-9f1",
                     why="w", project_id=pid, kind="observation")
        keep = L.append(conn, owner="agent:test", statement="KEPT-CANARY-9f1",
                        why="w", project_id=pid, kind="observation")
        got = survey.project_detail(pid, timeline_limit=0)
        texts = [n["statement"] for n in got["notes"]]
        check("both notes are visible before the erasure",
              "ERASED-CANARY-9f1" in texts and "KEPT-CANARY-9f1" in texts, str(texts))
        L.tombstone(conn, r["memoryId"], reason="probe", approved_by="operator")
        conn.close()
        got = survey.project_detail(pid, timeline_limit=0)
        texts = [n["statement"] for n in got["notes"]]
        check("the erased one is gone from the project surface",
              "ERASED-CANARY-9f1" not in texts, str(texts))
        check("and the other is still there, so the filter is not a blanket",
              "KEPT-CANARY-9f1" in texts, str(texts))
    finally:
        os.environ.pop("OBSERVATORY_DB", None)
        os.environ.pop("OBSERVATORY_REGISTRY", None)
        importlib.reload(paths)


# ─────────── the prober dispatches by class, not by position ───────────

def test_the_prober_does_not_dispatch_by_list_position() -> None:
    import check_paths
    src = check_paths.prose_removed((ROOT / "tools/run_probes.py").read_text(
        encoding="utf-8"))
    check("`CAPS[0]` / `CAPS[1:]` is gone", "CAPS[1:]" not in src,
          "a third capability would have been probed as a write capability")
    check("read and write capabilities are separated by their effect class",
          'c.get("effect") == "none"' in src and 'c.get("effect") != "none"' in src, "")
    check("and each probe is dispatched by its own id", "READ_ASSESSORS" in src, "")
    # PORTED-DIVERGED: receipts are written into the selected private fixture
    # workspace by a full probe run over stdio, not published in the source
    # tree, so there is no committed receipt to read. What the receipt's
    # coverage rested on is checked instead: every probe of every read
    # capability after the first has an assessor of its own id.
    raw = (ROOT / "tools/run_probes.py").read_text(encoding="utf-8")
    reads = [c for c in MANIFEST["capabilities"] if c.get("effect") == "none"][1:]
    missing = [p["id"] for c in reads for p in c["profile"].get("probes", [])
               if f'"{p["id"]}":' not in raw]
    check("every later read capability's probe has an assessor",
          bool(reads) and not missing, str(missing))




def live_survey():
    """The survey against the current fixture, whatever a sibling test left behind.

    `store/db.py` captures `DB_PATH = paths.DB` at import, so a suite that
    redirects `OBSERVATORY_DB` for one fixture leaves every later reader of the
    module pointed at the temp store — and a test that reloads `survey` but not
    `store.db` measures a different store than it believes it does. Reloading
    both is what makes these order-independent.
    """
    import paths, survey
    from store import db as store_db
    importlib.reload(paths)
    importlib.reload(store_db)
    importlib.reload(survey)
    return survey.survey()


# ─────────── the estate surface, and the question it is asked ──────────

def test_the_estate_surface_says_which_projects_are_active() -> None:
    """THE BRIEF'S HEADLINE QUESTION, and the surface answered it wrongly.

    `estate.survey` exported `lifecycle` and not `activity_tier`. Measured on a
    real registry: nearly every project was `lifecycle: active`, while the tier
    that MEASURES activity put barely more than half of them there — and many
    were `lifecycle: active` with `tier: dormant`. So a Fabric host rendering
    the estate from this capability would have shown almost every project as
    active, while the registry held the right answer in a field the wire dropped.

    The `activity_tiers.json` configuration says it in its own note: lifecycle
    is a DECLARED state, the tier is a MEASUREMENT, and a project can be
    archived and have moved last week.
    """
    import survey
    importlib.reload(survey)
    result = survey.survey()
    rows = result.get("projects") or []
    check("the survey returns projects", bool(rows), str(list(result)))
    missing = [r["id"] for r in rows if not r.get("activityTier")]
    check(f"every project carries its measured tier ({len(rows)})", not missing,
          f"{len(missing)} without one: {missing[:4]}")
    jsonschema.validate(result, schema_of(caps()["estate.survey"]))
    check("and the declared schema permits the answer", True, "")


def test_lifecycle_and_the_tier_are_two_different_facts() -> None:
    """Driven through the projection with a fixture, so the property does not
    depend on the estate holding a disagreement today."""
    import survey
    importlib.reload(survey)
    view = survey._project_view(
        {"id": "project:x", "name": "x", "ownership": "owned", "lifecycle": "active",
         "activity_tier": "dormant", "membership_rules": []}, {}, {})
    check("the declared state reaches the wire", view.get("lifecycle") == "active",
          str(view))
    check("and the measurement reaches it too, separately",
          view.get("activityTier") == "dormant", str(view))


def test_a_host_can_order_and_colour_the_tiers_without_knowing_them() -> None:
    """A tier string a host cannot ORDER is half an answer: `cooling` is not
    obviously better or worse than `dormant` to anything that did not read this
    repository. The vocabulary is the operator's — thresholds live in the
    `activity_tiers.json` configuration "so the operator can move them" — so it
    cannot be an enum in a published schema either, and a host that hardcoded it
    would break the day a tier is added.
    """
    import activity
    result = live_survey()
    vocab = result.get("activityTiers")
    check("the survey ships the vocabulary it answers in", isinstance(vocab, list),
          str(type(vocab)))
    if not isinstance(vocab, list):
        return
    check("in the order the thresholds define, with the unmeasured tier last",
          [t.get("id") for t in vocab] == activity.tier_ids(),
          f"{[t.get('id') for t in vocab]} vs {activity.tier_ids()}")
    check("each tier says what it means, so a legend needs no lookup",
          all(t.get("means") for t in vocab), str(vocab[:1]))
    check("and carries the boundary it was measured against",
          all("maxDays" in t for t in vocab), str(vocab[:1]))
    check("the unmeasured tier's boundary is null rather than a number",
          vocab[-1].get("maxDays") is None, str(vocab[-1]))


def test_the_agents_last_visit_reaches_the_surface_when_it_is_known() -> None:
    """`last_session_on` answers WHERE WORK WAS DONE when no commit was made —
    the session source's whole reason — and many projects carry one. Absent stays
    absent: a project nobody has opened is not a project opened at the epoch."""
    import survey
    importlib.reload(survey)
    with_it = survey._project_view(
        {"id": "project:x", "name": "x", "ownership": "owned", "lifecycle": "active",
         "last_session_on": "2026-09-07", "membership_rules": []}, {}, {})
    check("a known session date reaches the wire",
          with_it.get("lastSessionOn") == "2026-09-07", str(with_it))
    without = survey._project_view(
        {"id": "project:y", "name": "y", "ownership": "owned", "lifecycle": "active",
         "membership_rules": []}, {}, {})
    check("and an unknown one is absent rather than null or empty",
          "lastSessionOn" not in without, str(without))



# ─────────── the number a host sorts the grid by ───────────────────────

def test_the_survey_carries_a_quantity_a_host_can_sort_by() -> None:
    """The estate surface had no quantitative field at all, so a host could list
    projects and never order them by how much work happened — the weekly series
    lives in `project.detail`, which is one call per project for one grid.

    On a real estate many projects carry no weekly rollup at all. Their number
    is ABSENT rather than zero: a project with no
    weekly row is not a project with no commits, it is one nothing measured.
    """
    result = live_survey()
    rows = result.get("projects") or []
    win = result.get("recentActivityWindow") or {}
    check("the window is declared once, at the root", bool(win), str(list(result)))
    check("with the days it covers", isinstance(win.get("windowDays"), int), str(win))
    # NOT the cutoff date dressed as a week boundary: the predicate compares
    # against a date, so `weeksFrom` reports the earliest week actually summed.
    check("and the earliest week the sum actually reached",
          bool(win.get("weeksFrom")), str(win))
    carried = [r for r in rows if r.get("recentActivity")]
    check(f"projects with a rollup carry a quantity ({len(carried)})",
          bool(carried), f"{len(rows)} row(s), none with recentActivity")
    check("absent where nothing measured it, never zero",
          any(r["id"] == surface_fixture.UNMEASURED and "recentActivity" not in r for r in rows)
          and not any(r.get("recentActivity") == {} for r in rows), "")
    if carried:
        a = carried[0]["recentActivity"]
        check("the quantity is the shape `project.detail` already uses",
              {"weeks", "commits", "sessions", "workedDays", "sessionsUnmeasured"}
              <= set(a), str(sorted(a)))
        check("and a host can order by it", all(isinstance(a[k], int) for k in
              ("weeks", "commits", "sessions", "workedDays")), str(a))
    jsonschema.validate(result, schema_of(caps()["estate.survey"]))
    check("the declared schema permits all of it", True, "")


def test_the_window_is_the_active_tiers_own_boundary() -> None:
    """Not a second hardcoded 30. The `activity_tiers.json` configuration already fixes
    the boundary of `active`, and a survey that invented its own period would
    give a host two definitions of recent — one for the tier it colours by and
    one for the number it sorts by."""
    import activity
    win = (live_survey() or {}).get("recentActivityWindow") or {}
    check("the window is the active tier's boundary",
          win.get("windowDays") == activity.active_window_days(),
          f"{win.get('windowDays')} vs {activity.active_window_days()}")
    check("which is read from the config rather than written here",
          activity.active_window_days() == activity.tiers()[0]["max_days"],
          str(activity.tiers()[0]))


def test_an_unmeasured_week_is_excluded_and_counted_not_summed_as_zero() -> None:
    """The unmeasured-is-not-zero rule, on the estate surface. Driven against a
    planted store: real data rarely has a null `sessions`, so the hazard is
    latent and a test that waits for it to appear tests nothing."""
    import survey
    importlib.reload(survey)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-recent-"))
    conn = sqlite3.connect(d / "x.db")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    # THE SCHEMA ITSELF CARRIES THE RULE: `commits`, `active_days` and
    # `authors` are NOT NULL because they are always measured, while `sessions`,
    # `session_days` and `worked_days` are nullable because they may not be. The
    # planted week uses exactly that freedom.
    with conn:
        conn.execute("INSERT INTO project_week (project_id, week, week_start, commits,"
                     " active_days, authors, sessions, worked_days, computed_at)"
                     " VALUES ('project:p','2026-W36', date('now','-7 days'),"
                     " 5, 2, 1, 2, 3, '2026-09-08T00:00:00Z')")
        conn.execute("INSERT INTO project_week (project_id, week, week_start, commits,"
                     " active_days, authors, sessions, worked_days, computed_at)"
                     " VALUES ('project:p','2026-W35', date('now','-14 days'),"
                     " 7, 3, 1, NULL, NULL, '2026-09-08T00:00:00Z')")
    got = survey._recent_activity(conn, {}, 30)
    a = got.get("project:p") or {}
    check("commits add up across both weeks", a.get("commits") == 12, str(a))
    check("a null session week is not counted as zero sessions",
          a.get("sessions") == 2, str(a))
    check("nor as zero worked days", a.get("workedDays") == 3, str(a))
    check("and the unmeasured week is reported as one",
          a.get("sessionsUnmeasured") == 1, str(a))
    check("with both weeks counted", a.get("weeks") == 2, str(a))
    conn.close()


def test_a_renamed_projects_history_is_not_split_in_two() -> None:
    """Publishing a project renames it, and rows written before that stay keyed
    to the old id — a defect once found with hundreds of events one `git remote
    add` from being detached. A grouped query splits them unless the reader
    follows the rename."""
    import survey
    importlib.reload(survey)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rename-"))
    conn = sqlite3.connect(d / "x.db")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    with conn:
        for pid, wk, c in (("project:local-thing", "2026-W35", 4),
                           ("project:owner-thing", "2026-W36", 6)):
            conn.execute("INSERT INTO project_week (project_id, week, week_start,"
                         " commits, active_days, authors, sessions, worked_days,"
                         " computed_at) VALUES (?,?,date('now','-7 days'),?,"
                         "1,1,1,1,'2026-09-08T00:00:00Z')", (pid, wk, c))
    got = survey._recent_activity(conn, {"project:local-thing": "project:owner-thing"}, 30)
    check("one project, not two", list(got) == ["project:owner-thing"], str(list(got)))
    check("and its history is whole",
          (got.get("project:owner-thing") or {}).get("commits") == 10, str(got))
    conn.close()


def test_the_two_surfaces_sum_the_same_window_the_same_way() -> None:
    """One definition, two readers. `project.detail` sums the weekly series in
    Python and excludes nulls; the survey sums it in SQL for the whole estate at
    once. Same predicate, same instant, so a difference is a rule divergence
    rather than a clock."""
    import survey
    importlib.reload(survey)
    from store import db as store_db
    conn = store_db.connect()
    try:
        rows = list(conn.execute(
            "SELECT project_id, SUM(commits) c FROM project_week"
            " WHERE week_start >= date('now','-28 days') GROUP BY project_id"
            " ORDER BY c DESC LIMIT 1"))
        if not rows:
            check('the planted current rollup is present', False)
            return
        pid = rows[0]["project_id"]
        mine = survey._recent_activity(conn, {}, 28).get(pid) or {}
    finally:
        conn.close()
    theirs = (survey.project_detail(pid, timeline_limit=0, weeks=4, notes_limit=0)
              .get("activity") or {}).get("totals") or {}
    for k in ("commits", "sessions", "workedDays", "sessionsUnmeasured", "weeks"):
        check(f"`{k}` agrees between the estate sum and the project's own",
              mine.get(k) == theirs.get(k), f"survey {mine.get(k)} vs detail {theirs.get(k)}")



# ─────────── the number a host would have added up wrongly ─────────────

def test_the_estate_total_is_not_the_sum_of_the_column() -> None:
    """`workedDays` is a SET SIZE folded per (project, week). Adding it across
    weeks is sound — different weeks hold different days — and adding it across
    PROJECTS is not: a Tuesday two projects were both worked on is one Tuesday.

    `dashboard/build_dashboard.py` has long known this and counts the estate's
    work from `events` for exactly that reason. The wire did not say it, and the
    new per-project quantity had just handed every host a column whose obvious
    aggregation is wrong. Measured on a real estate over the same span: **30
    distinct days across the estate against 662 as the sum of the column** — not
    merely wrong but impossible in a thirty-day window, and it would have looked
    authoritative.

    So the survey answers the estate-level question itself rather than warning
    about it: the summable figures come from the rollup and agree with the rows
    by construction, and `workedDays` is a distinct count over the same span.
    """
    result = live_survey()
    ew = result.get("estateWork") or {}
    check("the survey answers the estate-level question", bool(ew), str(list(result)))
    if not ew:
        return
    rows = [r for r in (result.get("projects") or []) if r.get("recentActivity")]
    commits = sum(r["recentActivity"]["commits"] for r in rows)
    # THE IDENTITY, with the part that cannot reconcile named. The rollup
    # outlives its source, so a project the registry lost still has history and
    # the estate's total legitimately exceeds the rows.
    check("its commit figure reconciles with the rows once the unattributed "
          "part is named",
          (ew.get("commits") or 0) - (ew.get("commitsUnattributed") or 0) == commits,
          f"{ew.get('commits')} - {ew.get('commitsUnattributed')} vs {commits}")
    worked = sum(r["recentActivity"]["workedDays"] for r in rows)
    check("and its worked-day figure is NOT that sum",
          ew.get("workedDays") != worked or worked <= 1,
          f"{ew.get('workedDays')} equals the sum {worked}, so the double count "
          f"survived")
    check("it cannot exceed the window it covers",
          0 <= (ew.get("workedDays") or 0) <= (ew.get("windowDays") or 0) + 7,
          f"{ew.get('workedDays')} day(s) in a {ew.get('windowDays')}-day window")


def test_two_projects_worked_on_one_day_is_one_day() -> None:
    """The error in miniature, driven: the 30-against-662 case with two rows."""
    import survey
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-shared-"))
    conn = sqlite3.connect(d / "x.db")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    day = "2026-09-01"
    with conn:
        for pid in ("project:a", "project:b"):
            conn.execute("INSERT INTO project_week (project_id, week, week_start,"
                         " commits, active_days, authors, sessions, worked_days,"
                         " computed_at) VALUES (?, '2026-W36', ?, 3, 1, 1, 0, 1,"
                         " '2026-09-08T00:00:00Z')", (pid, day))
            conn.execute("INSERT INTO events (id, project_id, kind, occurred_at,"
                         " payload_json) VALUES (?, ?, 'commit', ?, '{}')",
                         (f"event:{pid}", pid, day + "T10:00:00Z"))
    got = survey._estate_work(conn, day)
    check("one day, not two", got.get("workedDays") == 1, str(got))
    check("both projects are counted as worked in", got.get("projectsWorked") == 2,
          str(got))
    check("and the commits do add up, because a commit is not a set",
          got.get("commits") == 6, str(got))
    conn.close()


def test_the_schema_warns_the_column_is_not_addable() -> None:
    """A host reads the schema before it reads a decision log."""
    it = schema_of(caps()["estate.survey"])["properties"]["projects"]["items"]
    desc = json.dumps(it["properties"].get("recentActivity", {}), ensure_ascii=False)
    # PORTED-DIVERGED: the public schema words the warning as "must not be
    # summed across projects".
    check("the per-project field says it must not be summed across projects",
          "must not be summed across projects" in desc, desc[-200:])


if __name__ == "__main__":
    print("one project as a contract — three capabilities where there was one\n")
    for fn in (test_every_capability_declares_the_tools_its_schema_describes,
               test_each_read_capabilitys_answer_validates_against_its_own_schema,
               test_the_surface_carries_what_a_renderer_needs,
               test_an_unmeasured_week_stays_null_rather_than_becoming_zero,
               test_a_repositorys_finding_reaches_its_project_labelled,
               test_the_timeline_no_longer_hands_over_column_names,
               test_an_unreadable_store_does_not_erase_the_registrys_half,
               test_a_tombstoned_note_reaches_neither_surface,
               test_the_prober_does_not_dispatch_by_list_position,
               test_the_estate_surface_says_which_projects_are_active,
               test_lifecycle_and_the_tier_are_two_different_facts,
               test_a_host_can_order_and_colour_the_tiers_without_knowing_them,
               test_the_agents_last_visit_reaches_the_surface_when_it_is_known,
               test_the_survey_carries_a_quantity_a_host_can_sort_by,
               test_the_window_is_the_active_tiers_own_boundary,
               test_an_unmeasured_week_is_excluded_and_counted_not_summed_as_zero,
               test_a_renamed_projects_history_is_not_split_in_two,
               test_the_two_surfaces_sum_the_same_window_the_same_way,
               test_the_estate_total_is_not_the_sum_of_the_column,
               test_two_projects_worked_on_one_day_is_one_day,
               test_the_schema_warns_the_column_is_not_addable):
        with surface_fixture.active():
            fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mone project answers in a shape another system can compile\033[0m")
