#!/usr/bin/env python3
"""The permanent statistic counted commits, and work is not only commits.

`project_week` is the one table designed to outlive its source: an aggregate is
three orders of magnitude smaller than the events beneath it, so it is kept for
ever while they are pruned at 365 days. The freeze rule is what makes that safe
— once a week's span leaves the event window the row is never rewritten, because
recomputing it would read no events and replace a measurement with a zero.

**It counted `kind='commit'` alone.** Activity is not commits:
`last_activity_on` was measuring commits while calling itself activity, and the
session companion is the second witness. The weekly series never learned it: on
a real estate, dozens of (project, week) pairs had sessions and no commit, and
therefore no row at all.

Those weeks of real work were absent from the estate's only permanent record of
when things were worked on, and the dashboard drew them as a flat zero. **With a
deadline**: the freeze rule that protects a measured week also forbids
correcting an unmeasured one, so each of those weeks would have become a
permanent zero as it passed the cutoff.

**The three columns are NULLABLE, and that is the load-bearing decision.** NULL
means "computed before the measure existed"; 0 would claim it was measured and
found empty. For an unfrozen row the next refresh fills it in; for a frozen one
it never can, which `tools/build_findings.py` raises as
`rollup.frozen_incomplete` rather than leaving as a silent NULL. It is the same
distinction `resolves: None` carries for a domain probe, and the page
honours it too: a week with `s == null` is drawn on the commit line only.

`commits`, `active_days` and `authors` keep their exact previous meaning.
Widening a column in place is how a number stops being comparable with the one
beside it — and this table's whole purpose is comparison across a year.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys, tempfile
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402
from store import rollup                                            # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fixture() -> sqlite3.Connection:
    """The real `project_week`, cut out of `schema.sql` — see test_rollup.py."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE events (id TEXT PRIMARY KEY, project_id TEXT, repo_id TEXT,"
        " kind TEXT, ref TEXT, actor TEXT, occurred_at TEXT, payload_json TEXT);")
    schema = (ROOT / "store/schema.sql").read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS project_week")
    end = schema.index(";", schema.index(");", start))
    conn.executescript(schema[start:end + 1])
    return conn


def add(conn, pid: str, day: str, n: int, kind: str = "commit",
        actor: str = "a") -> None:
    for i in range(n):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                     (f"{kind}:{pid}{day}{actor}{i}", pid, "r", kind, f"{day}{i}",
                      actor, f"{day}T12:00:0{i % 10}Z", "{}"))
    conn.commit()


def row(conn, pid: str = "project:x") -> sqlite3.Row:
    return conn.execute("SELECT * FROM project_week WHERE project_id=?", (pid,)).fetchone()


# ─────────── a week with work and no commit ────────────────────────────

def test_a_week_with_only_sessions_becomes_a_row() -> None:
    conn = fixture()
    today = date(2026, 9, 7)
    add(conn, "project:x", "2026-09-02", 3, kind="session", actor="operator")
    r = rollup.refresh(conn, today=today)
    check("the week is written", r["weeks_written"] == 1, str(r["weeks_written"]))
    got = row(conn)
    check("a row exists at all", got is not None,
          "45 such weeks produced no row on the live store")
    if got is None:
        return
    check("with no commits", got["commits"] == 0, str(got["commits"]))
    check("and the sessions counted", got["sessions"] == 3, str(got["sessions"]))
    check("one day of work", got["worked_days"] == 1, str(got["worked_days"]))
    check("no commit-days", got["active_days"] == 0, str(got["active_days"]))
    check("and no authors, because `authors` is commit actors",
          got["authors"] == 0, str(got["authors"]))


def test_worked_days_is_a_union_and_not_a_sum() -> None:
    """A commit and a session on the same Tuesday is ONE day worked."""
    conn = fixture()
    add(conn, "project:x", "2026-09-02", 2)
    add(conn, "project:x", "2026-09-02", 1, kind="session", actor="operator")
    add(conn, "project:x", "2026-09-04", 1, kind="session", actor="operator")
    rollup.refresh(conn, today=date(2026, 9, 7))
    got = row(conn)
    check("commit days counted alone", got["active_days"] == 1, str(got["active_days"]))
    check("session days counted alone", got["session_days"] == 2, str(got["session_days"]))
    check("and the union is 2, not 3", got["worked_days"] == 2,
          f"{got['worked_days']} — active_days + session_days would say 3")


def test_the_commit_columns_keep_their_old_meaning() -> None:
    """Sessions must not inflate a figure a year of rows is compared on."""
    conn = fixture()
    add(conn, "project:x", "2026-09-02", 4, actor="a")
    add(conn, "project:x", "2026-09-03", 9, kind="session", actor="operator")
    rollup.refresh(conn, today=date(2026, 9, 7))
    got = row(conn)
    check("commits are commits", got["commits"] == 4, str(got["commits"]))
    check("authors counts commit actors only", got["authors"] == 1, str(got["authors"]))
    check("active_days counts commit days only", got["active_days"] == 1,
          str(got["active_days"]))
    check("while the sessions sit in their own column", got["sessions"] == 9,
          str(got["sessions"]))


def test_first_and_last_span_any_activity() -> None:
    conn = fixture()
    add(conn, "project:x", "2026-09-02", 1, kind="session", actor="operator")
    add(conn, "project:x", "2026-09-04", 1)
    rollup.refresh(conn, today=date(2026, 9, 7))
    got = row(conn)
    check("first_at is the session that opened the week",
          got["first_at"].startswith("2026-09-02"), got["first_at"])
    check("last_at is the commit that closed it",
          got["last_at"].startswith("2026-09-04"), got["last_at"])


# ─────────── NULL is not zero ──────────────────────────────────────────

def test_a_row_predating_the_measure_reads_NULL_not_zero() -> None:
    conn = fixture()
    conn.execute(
        "INSERT INTO project_week (project_id, week, week_start, commits,"
        " active_days, authors, computed_at)"
        " VALUES ('project:old','2025-W40','2025-09-29',5,2,1,'2025-10-01T00:00:00Z')")
    conn.commit()
    got = row(conn, "project:old")
    check("the columns accept no value", got["sessions"] is None, str(got["sessions"]))
    check("which is NOT the same as zero", got["sessions"] != 0,
          "0 would claim the week was measured and nobody worked")
    check("the schema says why", "NULLABLE on purpose" in
          (ROOT / "store/schema.sql").read_text(encoding="utf-8"),
          "a nullable column with no stated reason is read as an oversight")


def test_an_unfrozen_row_is_completed_by_the_next_refresh() -> None:
    conn = fixture()
    add(conn, "project:x", "2026-09-02", 2)
    conn.execute(
        "INSERT INTO project_week (project_id, week, week_start, commits,"
        " active_days, authors, computed_at)"
        " VALUES ('project:x','2026-W36','2026-08-31',2,1,1,'2026-09-01T00:00:00Z')")
    conn.commit()
    check("the fixture starts with no session figure",
          row(conn)["sessions"] is None)
    rollup.refresh(conn, today=date(2026, 9, 7))
    check("and the refresh fills it in", row(conn)["sessions"] == 0,
          "an unfrozen row is recomputed, so the NULL is temporary")


def test_a_frozen_row_keeps_its_NULL_and_is_counted() -> None:
    """The gap that cannot be closed, reported as a number rather than a NULL."""
    conn = fixture()
    conn.execute(
        "INSERT INTO project_week (project_id, week, week_start, commits,"
        " active_days, authors, computed_at, frozen_at)"
        " VALUES ('project:old','2024-W10','2024-03-04',9,3,1,"
        "'2024-03-11T00:00:00Z','2025-03-11T00:00:00Z')")
    conn.commit()
    r = rollup.refresh(conn, today=date(2026, 9, 7))
    check("the frozen row keeps its NULL", row(conn, "project:old")["sessions"] is None,
          "the freeze rule forbids rewriting it, and the events are gone")
    check("and the refresh counts it", r["frozen_without_sessions"] == 1,
          str(r.get("frozen_without_sessions")))
    check("its commit figures are untouched", row(conn, "project:old")["commits"] == 9,
          "the row is not WRONG about commits, only silent about the rest")


def test_the_unfillable_gap_becomes_a_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rollfind-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()

    def findings(doc: dict) -> list[dict]:
        (d / "scratch/rollup.json").write_text(json.dumps(doc), encoding="utf-8")
        p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                           env=dict(os.environ,
                                    OBSERVATORY_REGISTRY=str(d / "registry"),
                                    OBSERVATORY_SCRATCH=str(d / "scratch"),
                                    OBSERVATORY_DB=str(d / "absent.db")),
                           capture_output=True, text=True, timeout=600)
        try:
            return [f for f in json.loads(p.stdout)["findings"]
                    if f["type"] == "rollup.frozen_incomplete"]
        except (ValueError, KeyError):
            check("findings built", False, (p.stdout + p.stderr)[-300:])
            return []

    got = findings({"frozen_without_sessions": 4})
    check("the gap is raised", len(got) == 1, str(len(got)))
    if got:
        check("counting the weeks", "4 frozen weeks" in got[0]["title"], got[0]["title"])
        check("saying nothing can restore them",
              "nothing can restore them" in got[0]["action"], got[0]["action"])
        check("and that the rows are not wrong about commits",
              "not wrong about commits" in got[0]["detail"], got[0]["detail"][:200])
    check("no gap, no finding", not findings({"frozen_without_sessions": 0}))


# ─────────── the receipt and the page ──────────────────────────────────

def test_the_refresh_leaves_a_receipt() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rollrec-"))
    (d / "scratch").mkdir()
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    p = subprocess.run([PY, "store/rollup.py", "refresh"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    check("the refresh runs on a fresh store", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    f = d / "scratch/rollup.json"
    check("a receipt is written", f.is_file(),
          "partial_weeks_skipped counts weeks nobody will ever capture, and it "
          "lived on stdout only")
    if f.is_file():
        doc = json.loads(f.read_text(encoding="utf-8"))
        for k in ("ran_at", "weeks_written", "weeks_frozen_now",
                  "partial_weeks_skipped", "frozen_without_sessions", "cutoff"):
            check(f"it carries `{k}`", k in doc, str(sorted(doc)))


def test_the_page_distinguishes_no_work_from_not_measured() -> None:
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the query reads the session columns",
          "sessions, worked_days" in src, "the page cannot show what it does not read")
    check("a null is carried through as null",
          'w.s == null' in src, "0 in the page would assert nobody worked")
    check("the session line is dashed, so the two signals are told apart",
          "stroke-dasharray" in src, "two identical lines need a legend")
    # The engine writes interface text in English and translates it from the
    # locale catalogs; the private original had the Russian inline.
    check("and the title names both figures",
          'T("{sessions} sessions, {worked} wk with work"' in src, src[:0])
    ru = json.loads((ROOT / "dashboard/locales/ru.json").read_text(encoding="utf-8"))
    check("which the Russian catalog translates",
          "сессий" in ru.get("{sessions} sessions, {worked} wk with work", ""), "")
    # Driven, not only read. The store is opened READ-WRITE first, because the
    # page opens it `mode=ro` and therefore cannot apply a migration: build the
    # page against a store whose columns have never been added and every
    # sparkline is a degradation rather than data. That is not a defect — the
    # page says so, measured below — but it makes the ORDER load-bearing, and
    # the first version of this test skipped it and failed for that reason.
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rollpage-"))
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_DASHBOARD=str(d / "page.html"))
    subprocess.run([PY, "store/rollup.py", "refresh"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    # A project id THE REGISTRY HAS. `build()` attaches weeks by walking the
    # registry's projects and looking each id up, so a row under an invented id
    # is correctly ignored — which is what the first version of this fixture
    # measured about itself rather than about the page.
    import paths
    real = json.loads((paths.REGISTRY / "projects.json").read_text(
        encoding="utf-8"))["projects"][0]["id"]
    seed = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                   "from store import db as sdb\n"
                   "c = sdb.connect()\n"
                   "c.execute(\"INSERT INTO project_week (project_id, week,"
                   " week_start, commits, active_days, authors, sessions,"
                   " session_days, worked_days, computed_at) VALUES"
                   f" ('{real}','2026-W36','2026-08-31',3,2,1,4,2,3,'now')\")\n"
                   "c.commit(); c.close()"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    check("the fixture store can be seeded", seed.returncode == 0, seed.stderr[-200:])
    p = subprocess.run([PY, "dashboard/build_dashboard.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    page = d / "page.html"
    check("the page builds", p.returncode == 0 and page.is_file(),
          (p.stdout + p.stderr)[-300:])
    if page.is_file():
        text = page.read_text(encoding="utf-8")
        check("the payload carries a session figure", '"s": 4' in text,
              "the week objects must reach the browser with it")
        # A RENDERED degradation, not the catalog entry: the page carries its
        # translation catalogs, so the phrase itself is always present. A
        # degradation travels as a message id WITH its arguments; checked by
        # hand against a page built over a garbage database file.
        check("and no store degradation is reported",
              'the store is unreadable: {error}", "args"' not in text,
              "a healthy store must not read as a broken one")


def test_a_store_missing_the_columns_SAYS_SO() -> None:
    """The behaviour I accused the page of not having, and it did.

    The page opens the store `mode=ro`, so a schema change it has not seen makes
    every weekly query raise — and I asserted that this produced 156 empty
    sparklines in silence. It does not: `store_degraded` carries `no such
    column: sessions`. I had searched the page for the CONNECT failure's wording
    while the QUERY failure has its own. Kept as a check because the behaviour is
    worth locking in, and because an accusation is not evidence.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rollold-"))
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_DASHBOARD=str(d / "page.html"))
    subprocess.run([PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                              "from store import db as sdb\n"
                              "sdb.connect().close()"],
                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    drop = subprocess.run(
        [PY, "-c", "import os, sqlite3\n"
                   "c = sqlite3.connect(os.environ['OBSERVATORY_DB'])\n"
                   "c.execute('ALTER TABLE project_week DROP COLUMN sessions')\n"
                   "c.commit(); c.close()"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    if drop.returncode != 0:
        print("  SKIP  this SQLite cannot DROP COLUMN")
        return
    subprocess.run([PY, "dashboard/build_dashboard.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=900)
    text = (d / "page.html").read_text(encoding="utf-8")
    check("the page reports the store as unreadable", "нечитаемо" in text,
          "an empty sparkline for every project would be a measured zero")
    check("naming the column", "sessions" in text, text[:0])
    check("and it is not silent about it", "store_degraded" in text,
          "the marker must reach the payload the page renders from")


if __name__ == "__main__":
    print("the weekly aggregate — commits, and the work that left none\n")
    for fn in (test_a_week_with_only_sessions_becomes_a_row,
               test_worked_days_is_a_union_and_not_a_sum,
               test_the_commit_columns_keep_their_old_meaning,
               test_first_and_last_span_any_activity,
               test_a_row_predating_the_measure_reads_NULL_not_zero,
               test_an_unfrozen_row_is_completed_by_the_next_refresh,
               test_a_frozen_row_keeps_its_NULL_and_is_counted,
               test_the_unfillable_gap_becomes_a_finding,
               test_the_refresh_leaves_a_receipt,
               test_the_page_distinguishes_no_work_from_not_measured,
               test_a_store_missing_the_columns_SAYS_SO):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma week worked on without a commit is no longer a zero\033[0m")
