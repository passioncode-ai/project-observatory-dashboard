#!/usr/bin/env python3
"""The queue of conclusions waiting for a person, and why it was unfaceable.

Measured 2026-09-07: **106 conclusions waiting**, every one of kind
`observation`, sitting in 63 (project, day) buckets — one project holding TEN
separate rows about a single day — and growing about fifty a day. The only way
out is `tools/review.py` at a terminal, one row at a time.

That is a VOLUME problem wearing an adjudication problem's clothes. Nobody reads
fifty interpretations a day, so every one of them would leave by retention
instead, which `tools/corroborate.py` calls "not review" in its own docstring.

Two changes, and a third that was deliberately NOT made:

* the agent keeps **one record per project per day** and corrects it by
  compare-and-swap — the pattern `tools/record_turn.py` already proved for
  session records. Append-only survives: a correction is a new
  REVISION, not a new record.
* `review.py digest` makes the queue readable and **writes nothing**.
* there is still **no `--yes`**. `require_terminal` exists because everything
  this tool writes is owned by `operator` — exempt from retention and
  unsupersedable by any agent — and minting that from a script would let
  anything with shell access forge it.
"""
from __future__ import annotations
import os, pathlib, sqlite3, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
import paths                                                        # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def store_with(rows: list[tuple[str, int]]) -> pathlib.Path:
    """A store holding `proposed` observations aged N days."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-queue-"))
    db = d / "store.db"
    conn = sqlite3.connect(db)
    # The schema ships with the program; the workspace store holds only data.
    conn.executescript((ROOT / "store" / "schema.sql").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    for i, row in enumerate(rows):
        # (project, age) is an observer's conclusion; (None, age, "lesson") a lesson,
        # which retention keeps 365 days while proposed (`ledger.kinds.lesson`).
        pid, age, kind = (*row, "observation") if len(row) == 2 else row
        made = (now - timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, owner, function,"
            " scope, statement, state, confidence, created_at)"
            " VALUES (?,1,?,?,?,?,?,?,'proposed',0.5,?)",
            (f"mem:test{i:04d}", kind, pid,
             "agent:task-pipeline" if kind == "lesson" else "agent:observer",
             "experiential" if kind == "lesson" else "episodic",
             "global" if kind == "lesson" else "project",
             f"a conclusion about {pid}", made))
    conn.commit()
    conn.close()
    return db


def run(cmd: list[str], db: pathlib.Path, extra: dict | None = None) -> tuple[int, str]:
    env = {**os.environ, "OBSERVATORY_DB": str(db), **(extra or {})}
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    return p.returncode, p.stdout + p.stderr


def test_the_agent_corrects_todays_record_instead_of_appending() -> None:
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("it looks for a record it already wrote about this project today",
          "substr(created_at, 1, 10) = ?" in src and "kind = 'observation'" in src)
    check("and corrects that one by compare-and-swap",
          'memory_id=prior["memory_id"] if prior else None' in src
          and 'expected_revision=prior["revision"] if prior else None' in src)
    check("an unchanged sentence is not re-recorded at all",
          'prior["statement"] == parsed.interpretation' in src,
          "a revision that repeats the previous one adds history and no information")

    # The mechanism itself, driven: a correction is a REVISION, not a record.
    sys.path.insert(0, str(ROOT))
    from store import db as store_db, ledger as L
    db = store_with([])
    conn = store_db.connect(db)
    first = L.append(conn, owner="agent:observer", kind="observation",
                     statement="first reading", project_id="project:x",
                     function="episodic", scope="project", state="proposed",
                     confidence=0.5)
    second = L.append(conn, owner="agent:observer", kind="observation",
                      memory_id=first["memoryId"], expected_revision=first["revision"],
                      statement="a fuller reading", project_id="project:x",
                      function="episodic", scope="project", state="proposed",
                      confidence=0.5)
    records = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM ledger").fetchone()[0]
    revisions = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    check("two readings become one record", records == 1, str(records))
    check("with two revisions, so nothing is lost", revisions == 2, str(revisions))
    check("and the queue counts the record once",
          conn.execute(
              "SELECT COUNT(*) FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev"
              " FROM ledger GROUP BY memory_id) m ON l.memory_id=m.memory_id"
              " AND l.revision=m.rev WHERE l.state='proposed'").fetchone()[0] == 1)
    conn.close()


def test_the_digest_groups_the_queue_and_writes_nothing() -> None:
    db = store_with([("project:a", 1)] * 3 + [("project:b", 2)] * 2)
    before = db.stat().st_mtime_ns
    code, out = run([PY, "tools/review.py", "digest"], db)
    check("it runs without a terminal", code == 0, out[-200:])
    check("it counts the queue", "5 conclusion(s) waiting" in out, out[:200])
    check("and groups by project", "project:a" in out and "project:b" in out)
    check("naming how long each has before erasure", "d left" in out, out[:300])
    check("it writes nothing to the store", db.stat().st_mtime_ns == before,
          "a digest that mutates is not a digest")


def test_the_digest_names_what_is_about_to_be_erased() -> None:
    db = store_with([("project:old", 80), ("project:young", 1)])
    code, out = run([PY, "tools/review.py", "digest"], db)
    check("a row past the warning line is called out",
          "ERASED UNREVIEWED" in out, out[-300:])
    # The property, not a tautology: the young row must NOT be in the erasure
    # section. `count(...) >= 1` was true whatever the code did.
    tail = out.split("will be ERASED UNREVIEWED", 1)[-1]
    check("the old row is the one named for erasure", "mem:test0000" in tail, tail[:200])
    check("and the young one is not", "mem:test0001" not in tail, tail[:200])


def test_promotion_still_refuses_a_script() -> None:
    """The guard that must NOT be relaxed to make the queue tractable."""
    db = store_with([("project:a", 1)])
    code, out = run([PY, "tools/review.py", "promote", "mem:test0000"], db)
    check("promoting without a terminal is refused", code != 0, out[-200:])
    check("and the refusal explains the authority it protects",
          "mintable" in out or "Minting" in out, out[-200:])
    src = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("there is no --yes FLAG", 'add_argument("--yes"' not in src,
          "a flag that bypasses the terminal would let anything with shell access forge "
          "operator authority")


def test_a_deadline_finding_fires_and_a_count_alone_does_not() -> None:
    """The count read as housekeeping for as long as it climbed; a date does not."""
    reg = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-qreg-")) / "registry"
    import shutil
    shutil.copytree(paths.REGISTRY, reg)
    db = store_with([("project:old", 80)] * 3 + [("project:new", 1)] * 25)
    code, out = run([PY, "tools/build_findings.py"], db,
                    {"OBSERVATORY_REGISTRY": str(reg)})
    check("the builder ran", code == 0, out[-200:])
    import json
    doc = json.loads((reg / "findings.json").read_text(encoding="utf-8"))
    kinds = {f["type"] for f in doc["findings"]}
    check("the backlog finding is raised", "ledger.review_backlog" in kinds, str(sorted(kinds)))
    check("and so is the DEADLINE finding", "ledger.review_expiring" in kinds,
          str(sorted(kinds)))
    expiring = next(f for f in doc["findings"] if f["type"] == "ledger.review_expiring")
    check("it names how many will be erased", "3 conclusion" in expiring["title"],
          expiring["title"])
    check("and points at the digest", "digest" in expiring["action"], expiring["action"])


def test_the_horizon_comes_from_the_file_that_owns_it() -> None:
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the findings builder reads retention.json", "retention.json" in src)
    check("and the store through paths, not an inlined path", "db = paths.DB" in src,
          "the fourth tool this session found with the store hardcoded")
    dig = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("the digest reads the same horizon", "proposed_days" in dig)


# ─────────── a lesson waits 365 days, and every reader of the queue says so ───────────

LESSON = (None, 80, "lesson")


def test_a_lesson_is_not_announced_for_erasure_at_the_general_horizon() -> None:
    """Retention keeps a proposed lesson 365 days; the deadline finding must not count
    an 80-day lesson among those erased within 14 days."""
    reg = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-qreg-")) / "registry"
    import shutil
    shutil.copytree(paths.REGISTRY, reg)
    db = store_with([("project:old", 80)] * 2 + [LESSON] * 3 + [("project:new", 1)] * 25)
    code, out = run([PY, "tools/build_findings.py"], db, {"OBSERVATORY_REGISTRY": str(reg)})
    check("the builder ran", code == 0, out[-200:])
    import json
    doc = json.loads((reg / "findings.json").read_text(encoding="utf-8"))
    expiring = [f for f in doc["findings"] if f["type"] == "ledger.review_expiring"]
    check("the deadline counts the two conclusions and none of the three lessons",
          bool(expiring) and "2 conclusion" in expiring[0]["title"],
          expiring[0]["title"] if expiring else "no ledger.review_expiring finding")


def test_the_digest_does_not_name_a_lesson_for_erasure() -> None:
    db = store_with([("project:old", 80), LESSON])
    code, out = run([PY, "tools/review.py", "digest"], db)
    tail = out.split("will be ERASED UNREVIEWED", 1)[-1]
    check("the 80-day conclusion is named for erasure", "mem:test0000" in tail, tail[:200])
    check("the 80-day lesson is not", "mem:test0001" not in tail, tail[:300])
    check("and the digest says a lesson's horizon", "365" in out, out[:600])


def test_the_health_page_does_not_count_a_lesson_in_its_seven_days() -> None:
    db = store_with([("project:old", 85), (None, 85, "lesson")])
    code, out = run([PY, "-c", "import json, sqlite3, sys; sys.path.insert(0, 'dashboard');"
                     " import paths, build_dashboard as B;"
                     " c = sqlite3.connect(str(paths.DB)); c.row_factory = sqlite3.Row;"
                     " print(json.dumps(B._digest(c)))"], db)
    import json
    try:
        digest = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check("the dashboard digest ran", False, out[-300:])
        return
    check("one row erases within 7 days: the conclusion, not the lesson",
          digest.get("erases_within_7d") == 1, json.dumps(digest))


if __name__ == "__main__":
    print("the review queue — readable, dated, and still the operator's to decide\n")
    for fn in (test_the_agent_corrects_todays_record_instead_of_appending,
               test_the_digest_groups_the_queue_and_writes_nothing,
               test_the_digest_names_what_is_about_to_be_erased,
               test_promotion_still_refuses_a_script,
               test_a_deadline_finding_fires_and_a_count_alone_does_not,
               test_the_horizon_comes_from_the_file_that_owns_it,
               test_a_lesson_is_not_announced_for_erasure_at_the_general_horizon,
               test_the_digest_does_not_name_a_lesson_for_erasure,
               test_the_health_page_does_not_count_a_lesson_in_its_seven_days):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe queue is faceable, and promotion is still a person's act\033[0m")
