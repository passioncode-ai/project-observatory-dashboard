#!/usr/bin/env python3
"""The layer that spends money and may decline to speak — and the silence when it cannot.

`agent/observe.py` is allowed to decline: an interpretation nobody can act on is
worth less than the tokens it cost, and `why_not` is required so a decline has to
say something. Measured across every run in `store/logs/tick.log`: **438
declines against 226 recordings**, so two thirds of the answers are "nothing
worth saying". At 450 model calls for 0.0723 credits total that is not a money
problem, and it is not treated as one here.

Two things WERE verified and found sound, recorded so the next reader does not
spend the hour:

* Every failure path leaves the deltas unconsumed. `BudgetExceeded`,
  `CredentialError`, `Fatal`, `Retryable` and the generic catch each break or
  continue WITHOUT touching `consumed_at`, and the messages say so. The
  permanent-degradation class closed elsewhere is not here.
* A decline and a failure are separated at the type level — five distinct
  `except` clauses — rather than collapsed into one "the agent said nothing".

**What was missing is the silence.** The interpretation layer had been halted
for hours by a spend ceiling on a key SHARED with everything else on the
machine, which another consumer had taken far past its daily ceiling — this
project's own journal holding nothing. Dozens of changes were queued. The message was honest, precise, and went to `tick.log`, which is not
something a person reads. The collectors were unaffected, so every FACT about the
estate stayed current while its narrative stopped.
"""
from __future__ import annotations
import json
import sqlite3, os, pathlib, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def findings_for(report: dict | None, *, with_store: bool = False,
                 plant=None) -> dict:
    """Build findings over a scratch directory holding only this report.

    `plant` is called with the temp directory before the run, for a fixture that
    needs rows in the store — the drain-cost arithmetic reads the ledger's own
    provenance, and borrowing the live estate's prices would make the assertion
    depend on what this machine happened to spend.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-interp-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    if report is not None:
        (d / "scratch/agent.json").write_text(json.dumps(report), encoding="utf-8")
    # AND THE SPEND STATE. `interpretation.halted` branches on whether the KEY's
    # own limit is spent — because until 2026-09-07 it offered "raise the ceiling
    # in agent/models.json" as its first remedy while the provider was answering
    # 402 on a limit no ceiling here can lift. That makes this fixture
    # a reader of `key-usage.json`, and without this variable it read the LIVE
    # one: on the day the key ran out, every assertion below about the not-spent
    # wording failed for a reason outside the code under test — the third
    # instance of the live estate leaking into a fixture's own list.
    (d / "state").mkdir(parents=True, exist_ok=True)
    if plant is not None:
        plant(d)
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_STATE=str(d / "state"),
               OBSERVATORY_DB=str(d / ("observatory.db" if with_store else "absent.db")))
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        return {f["type"]: f for f in json.loads(p.stdout)["findings"]}
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return {}


def stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ─────────────── what was verified and found sound ──────────────────────

def test_no_failure_path_consumes_a_delta() -> None:
    """The permanent-degradation class closed elsewhere is genuinely absent here."""
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    body = src[src.index("except providers.BudgetExceeded"):src.index("parsed = _Answer")]
    check("no failure branch touches consumed_at",
          "consumed_at" not in body, "a failed interpretation must be retryable")
    for clause in ("providers.BudgetExceeded", "providers.CredentialError",
                   "providers.Fatal", "providers.Retryable"):
        check(f"{clause.split('.')[-1]} has its own branch", clause in src)
    check("and the messages say the deltas survive",
          "stays unconsumed" in src and "stay unconsumed" in src,
          "the promise has to be in the output a person reads")


def test_a_decline_must_give_a_reason() -> None:
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("a decline without a reason is reported as a gap",
          "NO REASON GIVEN (a gap)" in src,
          "the first live run declined eight projects with an empty reason")
    check("and the schema requires the field", "why_not" in src)


# ─────────────── the report, on every path ──────────────────────────────

def test_the_report_is_written_even_when_the_run_never_starts() -> None:
    """Both degradations `return 0` BEFORE the end of the run — and they are the
    cases that motivated the report."""
    # STRUCTURALLY, with `ast`. The first version searched for
    # `report(f"no credential` — an argument that is a STRING LITERAL, which
    # `source_reader.code_only` blanks by design, so the assertion could never
    # pass. Asking whether a call happens is a question about the tree, not
    # about the text; the text is what prose can satisfy.
    import ast
    tree = ast.parse((ROOT / "agent/observe.py").read_text(encoding="utf-8"))
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    check("main exists", main is not None)
    if main is None:
        return
    reports = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "report"]
    check("the report is a helper called from several places",
          len(reports) >= 3, f"called at {reports}")
    check("and it is defined inside main, beside the connection it reads",
          any(isinstance(n, ast.FunctionDef) and n.name == "report"
              for n in ast.walk(main)), "a tail is unreachable from an early return")

    # Every `return 0` in main must have a report call before it in the tree.
    # Every exit reports, with ONE declared exemption: the dry run. It spends
    # nothing and decides nothing, so overwriting the record of the last REAL
    # run would erase the reason a stall is being reported — the same rule
    # `tools/corroborate.py` states as `if not dry`.
    src_lines = (ROOT / "agent/observe.py").read_text(encoding="utf-8").splitlines()
    dry_block = [i + 1 for i, l in enumerate(src_lines) if "dry run:" in l]
    returns = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Return)]
    unreported = [r for r in returns
                  if not any(c < r for c in reports)
                  and not any(abs(r - d) <= 8 for d in dry_block)]
    check("every exit from main reports, except the dry run",
          not unreported, f"return(s) at {unreported} with no preceding report")
    check("and the dry run's exemption is written down",
          any("NOT reported, deliberately" in l for l in src_lines),
          "an exemption without a reason reads as an oversight")

    live = ROOT / "store/raw/agent.json"
    if live.is_file():
        doc = json.loads(live.read_text(encoding="utf-8"))
        for k in ("ran_at", "recorded", "skipped", "failed", "unconsumed"):
            check(f"the live report carries {k}", k in doc, str(sorted(doc)))


# ─────────────── the silence becomes a finding ──────────────────────────

def test_a_stalled_interpretation_is_raised_by_AGE() -> None:
    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 0,
        "halted_by": "spend guardrail: daily ceiling: 36.09 of 2.00 credits spent today "
                     "on this KEY (openrouter, per the provider); this project's own "
                     "journal holds 0.0000",
        "unconsumed": 77, "oldest_unconsumed_scan": stamp(40)})
    f = found.get("interpretation.halted")
    check("a stall older than the horizon is raised", f is not None, str(sorted(found)))
    if not f:
        return
    check("as a warning, because the FACTS are still current",
          f["severity"] == "warning", f["severity"])
    check("counting what is waiting", "77 changes" in f["title"], f["title"])
    check("quoting the cause from the report rather than guessing",
          "36.09 of 2.00" in f["detail"], f["detail"][:160])
    check("naming that the key is shared", "shared with everything else" in f["action"],
          f["action"][:120])
    check("and that nothing is lost", "Nothing is lost" in f["action"], f["action"][-120:])


def test_a_fresh_stall_is_not_raised() -> None:
    """The agent runs every thirty minutes; four hours of queue is not a fault."""
    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 0,
        "halted_by": "spend guardrail: something",
        "unconsumed": 12, "oldest_unconsumed_scan": stamp(2)})
    check("two hours of waiting raises nothing",
          "interpretation.halted" not in found, str(sorted(found)))


def test_an_empty_queue_raises_nothing() -> None:
    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 4, "skipped": 1, "failed": 0,
        "halted_by": None, "unconsumed": 0, "oldest_unconsumed_scan": None})
    check("a healthy run raises nothing", "interpretation.halted" not in found,
          str(sorted(found)))


def test_an_unparseable_stamp_reads_as_OLD() -> None:
    """A stall whose age cannot be established must not be waved through."""
    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 3,
        "halted_by": None, "unconsumed": 9, "oldest_unconsumed_scan": "not-a-date"})
    f = found.get("interpretation.halted")
    check("an unparseable timestamp still raises", f is not None, str(sorted(found)))
    if f:
        check("and falls back to the run's counters for the cause",
              "failed 3" in f["detail"], f["detail"][:160])


def test_the_finding_does_not_need_the_store() -> None:
    """It reads a FILE. Gating it on the database would hide the stall on the
    machine with the least other information."""
    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 0,
        "halted_by": "spend guardrail: x", "unconsumed": 5,
        "oldest_unconsumed_scan": stamp(30)}, with_store=False)
    check("raised with no database present", "interpretation.halted" in found,
          str(sorted(found)))


def test_the_horizon_is_named_with_its_reasoning() -> None:
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the horizon is a named constant", "INTERPRETATION_LAG_HOURS" in src)
    check("and says why six", "twelve ticks at the launchd cadence" in src)
    check("named separately from the projection's, so one can move alone",
          "PROJECTION_LAG_HOURS" in src and "INTERPRETATION_LAG_HOURS" in src)



def test_the_stall_says_what_draining_it_would_cost() -> None:
    """A queue nobody has priced reads as a queue nobody can afford.

    The row said "283 change(s) have waited" and stopped there. The two numbers
    that turn that into money are both recorded and neither reached it: the agent
    knows how many PROJECTS wait — the call is one per project, not per change —
    and every interpretation's provenance carries what it cost. Measured
    2026-09-08: 30 projects, a median of 0.0002 credits, so the whole queue is
    about 0.006 against a daily ceiling of 2.0.
    """
    def plant(d: pathlib.Path) -> None:
        """Two interpretations at a known price, so the arithmetic is checkable."""
        conn = sqlite3.connect(d / "observatory.db")
        conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        with conn:
            for i, cost in enumerate((0.001, 0.003)):
                conn.execute(
                    "INSERT INTO ledger (memory_id, revision, kind, function, scope,"
                    " statement, state, owner, classification, supersedes_json,"
                    " conflicts_with_json, provenance_json, evidence_json, created_at)"
                    " VALUES (?, 1, 'note', 'semantic', 'project', 'planted',"
                    " 'proposed', 'agent:observer', 'project-internal', '[]', '[]',"
                    " ?, '[]', '2026-09-08T00:00:00Z')",
                    (f"mem:planted{i}",
                     json.dumps([{"source": "agent/observe", "cost_credits": cost}])))
        conn.close()

    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 0,
        "halted_by": "spend guardrail: the key is spent",
        "unconsumed": 283, "projects_unconsumed": 30,
        "oldest_unconsumed_scan": stamp(40)}, with_store=True, plant=plant)
    f = found.get("interpretation.halted")
    check("the stall is raised", f is not None, str(sorted(found)))
    if True:
        if f:
            d = f["detail"]
            check("it says how many PROJECTS wait, not only how many changes",
                  "30 project(s) wait" in d, d[-260:])
            check("and what one interpretation has cost", "credits each" in d, d[-260:])
            check("and what the whole queue comes to", "the whole queue is about" in d,
                  d[-260:])
            check("and that waiting does not make it dearer",
                  "does not grow with its backlog" in d, d[-200:])


def test_a_queue_nobody_priced_says_nothing_rather_than_zero() -> None:
    """Absent is not free. With no project count the arithmetic has no subject,
    and inventing one would put a number on the operator's page that nothing
    measured."""
    found = findings_for({
        "ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 0,
        "halted_by": "spend guardrail: the key is spent",
        "unconsumed": 283, "oldest_unconsumed_scan": stamp(40)})
    f = found.get("interpretation.halted")
    check("the stall is still raised", f is not None, str(sorted(found)))
    if f:
        check("and it quotes no price it could not compute",
              "the whole queue is about" not in f["detail"], f["detail"][-200:])


if __name__ == "__main__":
    print("the interpretation layer — its declines, and its silence\n")
    for fn in (test_no_failure_path_consumes_a_delta,
               test_a_decline_must_give_a_reason,
               test_the_report_is_written_even_when_the_run_never_starts,
               test_a_stalled_interpretation_is_raised_by_AGE,
               test_a_fresh_stall_is_not_raised,
               test_an_empty_queue_raises_nothing,
               test_an_unparseable_stamp_reads_as_OLD,
               test_the_finding_does_not_need_the_store,
               test_the_horizon_is_named_with_its_reasoning,
               test_the_stall_says_what_draining_it_would_cost,
               test_a_queue_nobody_priced_says_nothing_rather_than_zero):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma halted interpretation is no longer a line in a log nobody reads\033[0m")
