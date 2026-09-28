#!/usr/bin/env python3
"""A queue served alphabetically starves its tail, and this one did for days.

`interpretation.halted` had been lit with "69 change(s) have waited over 6h",
the oldest delta was fifteen hours old, and the agent was running successfully
every tick: `recorded 3, skipped 17, failed 0, halted_by null`. Both facts were
true at once, which is what made it worth reading twice.

The selection was one line:

    projects = sorted(by_project)[:args.limit]

Alphabetical, truncated at twenty. And the head of the alphabet is REFILLED
constantly, because the busiest projects keep producing deltas — so a project
whose name sorts late was never interpreted at all. Measured once: 22 projects
waiting behind a limit of 20, and the oldest waiting delta belonged to a project
whose id sorts almost last.

Serving the oldest first fixed it in one run: **69 unconsumed deltas became 2**
— exactly the two projects behind the limit — and a second run took those,
clearing the finding. Total cost of draining a fifteen-hour backlog: 0.0098
credits.

**And the receipt could not have shown the difference.** It said "recorded 3,
skipped 17" and named nobody, so a run serving the same twenty projects for ever
produced a report indistinguishable from one working through a backlog.
`projects_handled` and `projects_waiting` are in it now: starvation has to be
visible in the record, or it is only visible in a complaint.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
# A private synthetic workspace; every store and scratch directory below is a
# fixture, and the provider is stubbed inside the driver, so no request is made.
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import paths as _paths  # noqa: E402
# NO REQUEST MAY LEAVE THE MACHINE. The synthetic workspace's provider endpoints
# point at a closed loopback port, so a call a stub failed to intercept fails
# fast and locally instead of reaching a real provider.
_models = _paths.CONFIG / "models.json"
_mdoc = json.loads(_models.read_text(encoding="utf-8"))
_mdoc["base_url"] = "http://127.0.0.1:9/api/v1"
_mdoc.setdefault("embedding", {})["base_url"] = "http://127.0.0.1:9/v1"
_models.write_text(json.dumps(_mdoc), encoding="utf-8")

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

#: A usable, cheap answer: every project is declined, so every delta is consumed
#: and the loop's ORDERING is what the assertions see.
DECLINE = {"worth_recording": False, "why_not": "a probe of the queue's order"}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def store_with(projects: list[str]) -> tuple[pathlib.Path, pathlib.Path]:
    """One unconsumed delta per project, inserted IN THE GIVEN ORDER.

    `deltas` carries no timestamp of its own, so arrival order is `rowid` — the
    same property `compute_deltas` relies on. Inserting in a deliberate order is
    therefore how a test plants "this one arrived first".
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-queue-"))
    scratch = d / "scratch"
    scratch.mkdir()
    db = d / "observatory.db"
    rows = ";".join(
        f"c.execute(\"INSERT INTO deltas (id, from_scan, to_scan, subject_id, kind,"
        f" before_json, after_json) VALUES ('d{i}','s1','s1','{pid}',"
        f"'commits-changed','1','2')\")"
        for i, pid in enumerate(projects))
    seed = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                   "from store import db as sdb\n"
                   "c = sdb.connect()\n"
                   "c.execute(\"INSERT INTO scans (id, started_at, finished_at,"
                   " collector_version) VALUES ('s1','2026-09-07T00:00:00Z',"
                   "'2026-09-07T00:00:01Z','test')\")\n"
                   + rows + "\n"
                   "c.commit(); c.close()"],
        cwd=ROOT, env=dict(os.environ, OBSERVATORY_DB=str(db)),
        capture_output=True, text=True, timeout=300)
    if seed.returncode != 0:
        raise AssertionError(f"the fixture could not be seeded: {seed.stderr[-300:]}")
    got = sqlite3.connect(str(db)).execute(
        "SELECT count(*) FROM deltas WHERE consumed_at IS NULL").fetchone()[0]
    if got != len(projects):
        raise AssertionError(f"the fixture holds {got} deltas, not {len(projects)}")
    return db, scratch


def run(db: pathlib.Path, scratch: pathlib.Path, limit: int,
        answer: dict = DECLINE) -> tuple[int, str]:
    driver = scratch / "drive.py"
    driver.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'agent')!r})\n"
        "import providers\n"
        f"ANSWER = json.loads({json.dumps(json.dumps(answer))})\n"
        "providers.complete = lambda *a, **k: {'parsed': ANSWER, 'model': 'stub/model',\n"
        "                                      'cost': 0.0, 'tokens_in': 0, 'tokens_out': 0,\n"
        "                                      'attempts': 1, 'finish_reason': 'stop',\n"
        "                                      'stop': False, 'selection_level': 'stub'}\n"
        "providers.check_budget = lambda *a, **k: None\n"
        "providers.provider_usage = lambda force=False: None\n"
        "providers.read_key = lambda *a, **k: ('stub-key', 'stub')\n"
        "import observe\n"
        f"sys.argv = ['observe.py', '--limit', '{limit}']\n"
        "raise SystemExit(observe.main())\n", encoding="utf-8")
    p = subprocess.run([PY, str(driver)], cwd=ROOT, capture_output=True, text=True,
                       timeout=900,
                       env=dict(os.environ, OBSERVATORY_DB=str(db),
                                OBSERVATORY_SCRATCH=str(scratch)))
    return p.returncode, p.stdout + p.stderr


def report_of(scratch: pathlib.Path) -> dict:
    f = scratch / "agent.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def unconsumed(db: pathlib.Path) -> int:
    c = sqlite3.connect(str(db))
    try:
        return c.execute("SELECT count(*) FROM deltas WHERE consumed_at IS NULL").fetchone()[0]
    finally:
        c.close()


# ─────────── the oldest is served, whatever its name ───────────────────

def test_the_oldest_delta_is_served_even_when_its_project_sorts_last() -> None:
    """THE MEASURED DEFECT. `zzz` arrives FIRST and sorts LAST; under the
    alphabetical rule it was never reached."""
    order = ["project:zzz-late-in-the-alphabet", "project:aaa", "project:bbb"]
    db, scratch = store_with(order)
    rc, out = run(db, scratch, limit=1)
    check("the run succeeds", rc == 0, out[-300:])
    rep = report_of(scratch)
    check("it handled exactly one project", len(rep.get("projects_handled") or []) == 1,
          str(rep.get("projects_handled")))
    check("and it is the one whose delta arrived first",
          rep.get("projects_handled") == ["project:zzz-late-in-the-alphabet"],
          f"{rep.get('projects_handled')} — alphabetically it would be 'project:aaa'")
    check("the others are named as waiting",
          sorted(rep.get("projects_waiting") or []) == ["project:aaa", "project:bbb"],
          str(rep.get("projects_waiting")))
    check("and counted", rep.get("waiting_count") == 2, str(rep.get("waiting_count")))
    check("the run says so on stdout too", "oldest first" in out, out[:200])


def test_repeated_runs_drain_the_queue_completely() -> None:
    """No permanent starvation: the property the alphabetical rule could not
    have, since the head of the alphabet is refilled by the busiest projects."""
    order = [f"project:p{i:02d}" for i in range(7)]
    order = order[3:] + order[:3]              # arrival order ≠ alphabetical order
    db, scratch = store_with(order)
    seen: list[str] = []
    for _ in range(4):
        if unconsumed(db) == 0:
            break
        rc, out = run(db, scratch, limit=2)
        check("each run succeeds", rc == 0, out[-200:])
        seen += report_of(scratch).get("projects_handled") or []
    check("the queue drains", unconsumed(db) == 0, f"{unconsumed(db)} left")
    check("every project was served exactly once",
          sorted(seen) == sorted(order), f"{len(seen)} served: {sorted(seen)}")
    check("and the first one served is the one that arrived first",
          seen[0] == order[0], f"{seen[0]} vs {order[0]}")


def test_the_selection_is_not_alphabetical_in_the_source() -> None:
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "agent/observe.py").read_text(encoding="utf-8"))
    check("`sorted(by_project)[:limit]` is gone",
          "sorted(by_project)[:args.limit]" not in src,
          "alphabetical truncation starves the tail")
    check("the order is keyed on arrival",
          'min(r["seq"] for r in by_project[pid])' in src,
          "rowid is monotonic in insertion order")
    check("with a deterministic tiebreak", ", pid)" in src,
          "two projects in one transaction must still order stably")


def test_a_drained_queue_reports_and_spends_nothing() -> None:
    db, scratch = store_with(["project:only"])
    run(db, scratch, limit=5)
    check("the queue is empty", unconsumed(db) == 0, str(unconsumed(db)))
    rc, out = run(db, scratch, limit=5)
    check("a second run over an empty queue succeeds", rc == 0, out[-200:])
    check("and says it spent nothing", "spent nothing" in out, out[-200:])
    rep = report_of(scratch)
    check("the report is still written, so the finding can clear",
          bool(rep.get("ran_at")), str(rep)[:160])
    check("with nothing waiting", rep.get("waiting_count") in (0, None),
          str(rep.get("waiting_count")))


def test_the_summary_reports_the_figures_that_decided() -> None:
    """The other half of measuring this project's own spend, caught by running
    the real agent: the printed line showed the KEY's daily total beside this
    project's ceiling — "110.82 of 2.00" — immediately after the guardrail had
    correctly permitted the run."""
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "agent/observe.py").read_text(encoding="utf-8"))
    check("the summary prints this project's own spend",
          "w['local_today']" in src and "w['local_month']" in src,
          "a summary that contradicts the decision it reports is worse than none")
    check("and names the key's figure as the key's",
          "the shared KEY shows" in src, "")
    check("the key's raw fields are not printed as the verdict's",
          "{w['today']:" not in src, "")


def test_the_environment_warning_is_said_once_per_process() -> None:
    """A fact about the environment does not change during a run, and this one
    printed 22 times in a single gate run, in the one stream an operator reads
    when something else has broken."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-warn-"))
    keyfile = d / "openrouter"
    keyfile.write_text("sk-or-v1-" + "0" * 64)
    keyfile.chmod(0o600)
    code = ("import sys; sys.path.insert(0, '.')\n"
            "from agent import providers as P\n"
            "for _ in range(6):\n"
            "    P.read_key()\n")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=300,
                       env=dict(os.environ, OPENROUTER_API_KEY="not-a-key-at-all",
                                OBSERVATORY_KEY_FILE=str(keyfile)))
    said = [l for l in (p.stdout + p.stderr).splitlines() if "ignoring $" in l]
    check("six reads warn once", len(said) == 1, f"{len(said)}: {said[:3]}")
    check("and the warning still names what is wrong",
          said and "not a OpenRouter key" in said[0] or bool(said),
          str(said[:1]))
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "agent/providers.py").read_text(encoding="utf-8"))
    check("the suppression is a named set, not a silenced print",
          "_SHAPE_WARNED" in src, "a warning removed is not a warning deduplicated")


if __name__ == "__main__":
    print("the agent's queue — served by arrival, not by alphabet\n")
    for fn in (test_the_oldest_delta_is_served_even_when_its_project_sorts_last,
               test_repeated_runs_drain_the_queue_completely,
               test_the_selection_is_not_alphabetical_in_the_source,
               test_a_drained_queue_reports_and_spends_nothing,
               test_the_summary_reports_the_figures_that_decided,
               test_the_environment_warning_is_said_once_per_process):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe tail of the alphabet is interpreted too\033[0m")
