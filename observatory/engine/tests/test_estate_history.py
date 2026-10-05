#!/usr/bin/env python3
"""Keeping what only somebody else's store remembers.

The estate has words for a third fact: a project existed under the projects
root and its folder is gone. It could SAY it and could not KEEP it — the
evidence was session summaries inside the companion's memory store, a store
this system opens read-only, does not own, and whose retention it does not
control. The registry cannot hold the fact either: it is derived from what
EXISTS, so an entry with no anchor is erased by the next emit.

The ledger is the one authored, append-only record here, and every rule that
governs an automated writer applies unchanged:

* `state='proposed'` — an automated writer may not promote its own row, and
  `tools/corroborate.py` routes every kind but `session` to `needs_person`,
  which is the right destination for "what became of this project".
* `project_id` is NULL, deliberately: minting an id would invent the very thing
  the record exists to say is gone.
* the statement carries counts, dates and the folder — measured, checkable —
  and no guess about what happened, because a fabricated cause is read as true
  by everything downstream.

**And the finding changes shape once the fact is kept.** The warning was about
perishability, not about the decision: with the evidence in the ledger it drops
to `info` and its action names the record — `review.py show mem:…` — instead of
repeating "decide what it was". A warning that nothing the system does can close
is a warning that teaches the operator to skip the list.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

LOST = {"name": "gone-thing", "sessions": 7, "verdict": "estate-folder-gone",
        "folders": ["gone-thing"]}
ALIVE = {"name": "an-alias", "sessions": 4, "verdict": "folder-exists-unmatched",
         "folders": ["alpha-web"]}
BLIND = {"name": "mystery", "sessions": 3, "verdict": "no-path-recorded",
         "folders": []}
ONCE = {"name": "just-once", "sessions": 1, "verdict": "estate-folder-gone",
        "folders": ["just-once"]}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def workspace(rows: list[dict]) -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-history-"))
    (d / "scratch").mkdir()
    (d / "scratch/sessions.json").write_text(json.dumps({
        "scanned_on": "2026-09-07", "source": "fixture",
        "counts": {"sessions": 0, "projects": 0, "unmatched_names": len(rows)},
        "unattributed": rows, "sessions": [], "degraded": []}))
    # The projects root is the fixture's own directory, so the folder a
    # statement names is a synthetic path rather than wherever this machine
    # keeps its projects.
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_DATA=str(d / "projects"))
    return d, env


def record(env: dict, *args: str):
    return subprocess.run([PY, "tools/record_lost_projects.py", *args], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=300)


def rows_of(d: pathlib.Path, sql: str) -> list:
    f = d / "observatory.db"
    if not f.is_file():
        return []
    conn = sqlite3.connect(f)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ─────────── one proposed record per lost project ──────────────────────

def test_a_lost_project_is_kept_under_the_writers_own_rules() -> None:
    d, env = workspace([LOST, ALIVE, BLIND, ONCE])
    p = record(env)
    check("the run succeeds", p.returncode == 0, (p.stdout + p.stderr)[-260:])
    got = rows_of(d, "SELECT * FROM ledger WHERE kind = 'estate-history'")
    check("exactly one record is written", len(got) == 1,
          f"{[r['statement'][:30] for r in got]}")
    if not got:
        return
    r = got[0]
    check("only the lost verdict is recorded", "gone-thing" in r["statement"],
          r["statement"][:60])
    check("as `proposed`, which is all an automated writer may make",
          r["state"] == "proposed", r["state"])
    check("owned by a named writer", r["owner"] == "agent:estate-history", r["owner"])
    check("with NULL project_id, because the project is not in the registry",
          r["project_id"] is None, str(r["project_id"]))
    check("scoped globally rather than to a project that does not exist",
          r["scope"] == "global", r["scope"])
    check("the statement carries the counts", "7 session summaries" in r["statement"],
          r["statement"][:120])
    check("and the folder it names, under the configured projects root",
          str(d / "projects" / "gone-thing") in r["statement"], r["statement"][:160])
    check("the `why` says why it is kept at all",
          "does not own" in (r["why"] or ""), (r["why"] or "")[:80])
    check("the evidence cites the session store",
          "SRC-0012" in (r["evidence_json"] or ""), (r["evidence_json"] or "")[:120])
    check("neither the alias nor the blind case is recorded",
          not any(n in r["statement"] for n in ("an-alias", "mystery")), r["statement"][:60])
    check("and one session is still not evidence of a project",
          "just-once" not in r["statement"], r["statement"][:60])


def test_the_second_run_writes_nothing_and_a_change_revises() -> None:
    d, env = workspace([LOST])
    record(env)
    first = rows_of(d, "SELECT memory_id, revision FROM ledger WHERE kind='estate-history'")
    p = record(env)
    check("a second run reports nothing new", "2 unchanged" in p.stdout or
          "0 recorded, 0 revised, 1 unchanged" in p.stdout, p.stdout[-160:])
    again = rows_of(d, "SELECT memory_id, revision FROM ledger WHERE kind='estate-history'")
    check("and adds no revision", len(again) == len(first), f"{len(first)} -> {len(again)}")

    (d / "scratch/sessions.json").write_text(json.dumps({
        "scanned_on": "2026-09-08",
        "unattributed": [{**LOST, "sessions": 9}], "sessions": [], "degraded": []}))
    record(env)
    third = rows_of(d, "SELECT memory_id, revision FROM ledger"
                       " WHERE kind='estate-history' ORDER BY revision")
    check("a changed count revises the SAME record",
          len(third) == 2 and third[0]["memory_id"] == third[1]["memory_id"],
          str([(r["memory_id"], r["revision"]) for r in third]))
    check("at the next revision", third[-1]["revision"] == 2,
          str(third[-1]["revision"]))
    cur = rows_of(d, "SELECT name, value FROM cursors WHERE name LIKE 'estate-history%'")
    check("the cursor holds the mapping the idempotency rests on",
          len(cur) == 1 and cur[0]["value"] == third[0]["memory_id"],
          str([(c["name"], c["value"]) for c in cur]))


def test_a_dry_run_writes_nothing() -> None:
    """A command whose whole promise is "this changes nothing" must not advance
    the state that decides what the next real run does."""
    d, env = workspace([LOST])
    p = record(env, "--dry-run")
    check("it says what it would do", "would-write" in p.stdout, p.stdout[-200:])
    check("and writes no ledger row",
          rows_of(d, "SELECT 1 FROM ledger WHERE kind='estate-history'") == [], "")
    check("nor a cursor",
          rows_of(d, "SELECT 1 FROM cursors WHERE name LIKE 'estate-history%'") == [], "")
    check("nor a receipt", not (d / "scratch/lost-projects.json").is_file(),
          "answering a question must not overwrite the record of the last real run")


def test_a_missing_collector_output_degrades_with_its_reason() -> None:
    d, env = workspace([LOST])
    (d / "scratch/sessions.json").unlink()
    p = record(env)
    check("it refuses", p.returncode == 1, str(p.returncode))
    check("and names what is missing", "sessions.json" in p.stderr, p.stderr[-160:])
    check("with the command that writes it", "scan-sessions" in p.stderr, p.stderr[-160:])
    rep = json.loads((d / "scratch/lost-projects.json").read_text(encoding="utf-8"))
    check("the receipt records the degradation",
          rep["degraded"] and "sessions.json" in rep["degraded"][0]["reason"],
          str(rep)[:160])

    (d / "scratch/sessions.json").write_text("{not json")
    p = record(env)
    check("an unreadable one refuses too", p.returncode == 1, str(p.returncode))
    check("and says so rather than reading nothing as nothing lost",
          "unreadable" in p.stderr, p.stderr[-160:])


def test_nothing_lost_is_reported_as_nothing_lost() -> None:
    d, env = workspace([ALIVE, BLIND])
    p = record(env)
    check("the run succeeds", p.returncode == 0, (p.stdout + p.stderr)[-200:])
    check("and says every folder is still there",
          "no lost project" in p.stdout, p.stdout[-160:])
    rep = json.loads((d / "scratch/lost-projects.json").read_text(encoding="utf-8"))
    check("the receipt is written even so, so a stale one cannot linger",
          rep.get("written") == 0 and "ran_at" in rep, str(rep)[:160])


def test_one_refusal_does_not_silence_the_rest() -> None:
    """A LedgerError on one project must not stop the others: the swallowed
    failure that once kept the companion silent for hours is the shape being
    avoided."""
    d, env = workspace([LOST, {**LOST, "name": "second-gone",
                               "folders": ["second-gone"]}])
    code = ("import sys; sys.path.insert(0, '.')\n"
            "from store import ledger as L\n"
            "real = L.append\n"
            "calls = []\n"
            "def once(*a, **k):\n"
            "    calls.append(1)\n"
            "    if len(calls) == 1:\n"
            "        raise L.LedgerError('planted refusal')\n"
            "    return real(*a, **k)\n"
            "L.append = once\n"
            "import tools.record_lost_projects as R\n"
            "raise SystemExit(R.main(['record_lost_projects.py']))\n")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=300)
    check("the run still succeeds", p.returncode == 0, (p.stdout + p.stderr)[-200:])
    check("the refusal is named on stderr", "planted refusal" in p.stderr,
          p.stderr[-160:])
    got = rows_of(d, "SELECT statement FROM ledger WHERE kind='estate-history'")
    check("and the other project is still recorded", len(got) == 1,
          str([r["statement"][:24] for r in got]))
    rep = json.loads((d / "scratch/lost-projects.json").read_text(encoding="utf-8"))
    outcomes = {r["name"]: r["outcome"] for r in rep.get("projects") or []}
    check("the receipt records both outcomes",
          set(outcomes.values()) == {"refused", "written"}, str(outcomes))


# ─────────── the finding changes shape once the fact is kept ───────────

def findings_with(rows: list[dict], kept: list[dict] | None) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-histf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    (d / "scratch/sessions.json").write_text(json.dumps({
        "scanned_on": "2026-09-07", "unattributed": rows, "sessions": [],
        "degraded": []}))
    if kept is not None:
        (d / "scratch/lost-projects.json").write_text(json.dumps({
            "ran_at": "2026-09-07T13:00:00Z", "written": len(kept), "revised": 0,
            "unchanged": 0, "projects": kept, "degraded": []}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"] == "work.unattributed"]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def test_an_unkept_loss_is_a_warning_that_names_the_command() -> None:
    got = findings_with([LOST], kept=None)
    check("it fires as a warning", got and got[0]["severity"] == "warning",
          str([f["severity"] for f in got]))
    if got:
        check("and the action names the command that keeps it",
              "project-observatory full lost" in got[0]["action"], got[0]["action"][:120])


def test_a_kept_loss_drops_to_info_and_names_the_record() -> None:
    got = findings_with([LOST], kept=[{"name": "gone-thing",
                                       "memoryId": "mem:abc123", "outcome": "written"}])
    check("it drops to info", got and got[0]["severity"] == "info",
          str([f["severity"] for f in got]))
    if got:
        check("the detail says the evidence no longer depends on that store",
              "no longer depends" in got[0]["detail"], got[0]["detail"][-140:])
        check("and the action names the record",
              "mem:abc123" in got[0]["action"], got[0]["action"][:120])
        check("pointing at a review, not at a re-recording",
              "review.py show" in got[0]["action"], got[0]["action"][:120])


def test_a_refused_record_does_not_count_as_kept() -> None:
    got = findings_with([LOST], kept=[{"name": "gone-thing", "outcome": "refused",
                                       "reason": "LedgerError: x"}])
    check("a refusal leaves the warning standing",
          got and got[0]["severity"] == "warning",
          "a receipt that names a failure is not a record")


def test_nothing_mechanically_promotes_this_kind() -> None:
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "tools/corroborate.py").read_text(encoding="utf-8"))
    check("corroboration promotes only the session kind",
          'row["kind"] != "session"' in src,
          "what a vanished project meant is not mechanically checkable")


def test_a_fact_the_operator_confirmed_is_renewed_not_rewritten() -> None:
    """Pre-release review M1: once the operator confirms a lost-project fact it is theirs; the
    collector renews its validity while it runs short and changes nothing else."""
    d, env = workspace([LOST])
    record(env)
    code = ("import sqlite3,sys; sys.path.insert(0,'.'); from store import db, ledger as L; "
            "c=db.connect(); mid=c.execute(\"SELECT memory_id FROM ledger WHERE kind='estate-history'\").fetchone()[0]; "
            "L.transition(c, mid, to_state='observed', owner='operator', expected_revision=1); "
            "c.execute(\"UPDATE ledger SET valid_to=strftime('%Y-%m-%dT%H:%M:%SZ','now','+2 days') WHERE memory_id=? AND revision=2\", (mid,)); "
            "c.commit()")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    check("the operator confirmed the fact", p.returncode == 0, p.stderr[-300:])
    out = record(env)
    check("the collector run succeeds", out.returncode == 0, out.stderr[-300:])
    rows = rows_of(d, "SELECT revision, owner, state, statement, valid_to, provenance_json FROM ledger"
                      " WHERE kind='estate-history' ORDER BY revision")
    last, confirmed = rows[-1], rows[1]
    check("a renewal revision was written", len(rows) == 3, str([r[:3] for r in rows]))
    check("owner, state and statement stay as confirmed",
          (last[1], last[2], last[3]) == (confirmed[1], confirmed[2], confirmed[3]), str(last[:4]))
    check("validity is extended", last[4] > confirmed[4], f"{confirmed[4]} -> {last[4]}")
    check("the renewer is recorded", '"renewal"' in last[5], last[5][-200:])
    again = record(env)
    rows2 = rows_of(d, "SELECT revision FROM ledger WHERE kind='estate-history'")
    check("a fact with time left is not renewed again", again.returncode == 0 and len(rows2) == 3,
          str(len(rows2)))


if __name__ == "__main__":
    print("estate history — keeping what only somebody else's store remembers\n")
    for fn in (test_a_fact_the_operator_confirmed_is_renewed_not_rewritten,
               test_a_lost_project_is_kept_under_the_writers_own_rules,
               test_the_second_run_writes_nothing_and_a_change_revises,
               test_a_dry_run_writes_nothing,
               test_a_missing_collector_output_degrades_with_its_reason,
               test_nothing_lost_is_reported_as_nothing_lost,
               test_one_refusal_does_not_silence_the_rest,
               test_an_unkept_loss_is_a_warning_that_names_the_command,
               test_a_kept_loss_drops_to_info_and_names_the_record,
               test_a_refused_record_does_not_count_as_kept,
               test_nothing_mechanically_promotes_this_kind):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe estate keeps what it noticed, and the decision stays the "
          "operator's\033[0m")
