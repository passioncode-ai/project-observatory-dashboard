#!/usr/bin/env python3
"""A lost turn was recorded in a slot, and the next turn of any session took it.

`store/raw/record-turn.json` is the companion's receipt, and it holds ONE turn.
Every Stop hook of every watched project on the machine writes it — dozens of
agent sessions can be running at once — so a fault written at 10:04 is gone the
moment any of them ends a turn, including a turn that succeeded, in a project
the fault had nothing to do with.

**The board is rebuilt on the tick's interval** (`StartInterval` in the launchd
plist). So `companion.not_recording` fires only for a fault that happens to be
the newest turn on the whole machine when the tick runs.

The incident that produced the rule survived only because it was PERMANENT:
roughly seventy-two consecutive turns of one session raised the same
`IllegalTransition`, so the slot held a fault whichever turn wrote it last. The
class a real machine suffers is the opposite one — transient database errors,
`No space left` in one tick — and a transient fault is erased by the next quiet
turn with nothing left to read.

So the fault gets an append-only home of its own, and the slot keeps its job:
the slot says what the LAST turn did, the log says which turns were lost.

**Append, never read-modify-write.** `store_faults.py`'s docstring already
argued this for the same hazard: reading the file back to trim it "is both a
second chance to fail and a way to lose a concurrent writer's line". A
carry-forward inside the slot would have been smaller code and would have lost
exactly the fault it was meant to keep — session A reads no-fault, session B
writes a fault, session A writes its carried-forward document over it.

And the recorder is the machine's most frequent store-toucher: every turn of
every session opens the database. `store_faults` — the module written to capture
free space, WAL size and the holder count at the moment a store call fails — was
wired into the indexer and the agent and never into this. A sqlite error here now
records the machine's state too; a domain refusal does not, because that log's
whole meaning is that the store failed.
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


#: The checkout each workspace describes, so `fault_turn` can point the recorder
#: at a repository the FIXTURE owns rather than at this one.
_CHECKOUT: dict[int, pathlib.Path] = {}


def workspace() -> tuple[pathlib.Path, dict]:
    """A store, a scratch, and a watched repository with a real change.

    `--cwd ROOT` made every case here depend on this repository being mid-work:
    the recorder answers "nothing changed" for a clean, pushed tree and never
    reaches the ledger, so the planted fault could not happen and the log it
    should have written stayed empty. Trap T16.
    """
    import watched_repo
    d, repo, env = watched_repo.build()
    _CHECKOUT[id(env)] = repo
    return d, env


#: A turn that fails inside the ledger, driven the way the measured incident
#: failed. `store.ledger.append` is replaced before `tools/record_turn.py` is
#: imported, so the recorder meets the refusal on its real path.
PLANT = """
import sys; sys.path.insert(0, '.')
from store import ledger as L
L.append = lambda *a, **k: (_ for _ in ()).throw({exc})
sys.argv = ['record_turn.py', '--cwd', {cwd!r}, '--session-id', {sid!r}]
import tools.record_turn as R
R.main()
"""


def fault_turn(env: dict, session: str, exc: str = "L.IllegalTransition('planted')",
               cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-c", PLANT.format(exc=exc,
                                cwd=cwd or str(_CHECKOUT.get(id(env), ROOT)),
                                sid=session)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def quiet_turn(env: dict, session: str) -> subprocess.CompletedProcess:
    """`/tmp` is not a git repository, which the recorder answers rather than
    faults on — the cheapest turn that still overwrites the slot."""
    return subprocess.run(
        [PY, "tools/record_turn.py", "--cwd", "/tmp", "--session-id", session],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def slot(d: pathlib.Path) -> dict:
    p = d / "scratch/record-turn.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def log_lines(d: pathlib.Path) -> list[str]:
    p = d / "scratch/companion-faults.jsonl"
    return p.read_text(encoding="utf-8").splitlines() if p.is_file() else []


# ─────────── the erasure, driven ───────────────────────────────────────

def test_a_fault_outlives_the_next_turn_of_another_session() -> None:
    """THE DEFECT. Two sessions, one file: the second one's quiet turn takes the
    first one's fault, and the tick that would have reported it is up to thirty
    minutes away."""
    d, env = workspace()
    fault_turn(env, "s-lost")
    check("the slot holds the fault at first", slot(d).get("fault") is True,
          str(slot(d))[:200])

    quiet_turn(env, "s-neighbour")
    after = slot(d)
    check("and a neighbour's quiet turn takes it", after.get("fault") is False,
          "if this ever passes with fault=True the slot stopped being a slot, "
          "and the log below is no longer the only durable record")
    check("the neighbour's turn is what the slot now describes",
          after.get("session") == "s-neighbour", str(after)[:200])

    lines = log_lines(d)
    check("the fault survives in the durable log", len(lines) == 1, str(lines))
    if lines:
        row = json.loads(lines[0])
        check("with the session that lost the turn", row.get("session") == "s-lost",
              str(row))
        check("and the reason", "IllegalTransition" in (row.get("reason") or ""),
              str(row))
        check("and the project it happened in, which is the first question "
              "an operator asks", bool(row.get("cwd")), str(row))
        check("and a stamp", bool(row.get("at")), str(row))


def test_an_answer_about_the_work_is_not_written_to_the_fault_log() -> None:
    """`nothing changed` and `not a git repository` are answers, not failures —
    the distinction the collectors draw. A log that collects them is a
    log nobody reads."""
    d, env = workspace()
    quiet_turn(env, "s-quiet")
    check("a quiet turn writes the slot", bool(slot(d)), "")
    check("and adds nothing to the fault log", log_lines(d) == [], str(log_lines(d)))


# ─────────── append, because two writers is the normal case ────────────

def test_two_sessions_faulting_at_once_both_survive() -> None:
    """The property a carry-forward inside the slot cannot have. Driven with two
    processes writing the same file at the same time, which is what forty-nine
    sessions on one machine means."""
    d, _ = workspace()
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import companion_faults as C\n"
            "for i in range(150):\n"
            "    C.append({'at': 'x', 'session': sys.argv[1], 'reason': 'r%%d' %% i,"
            " 'cwd': '/tmp'})\n" % str(ROOT))
    env = dict(os.environ, OBSERVATORY_SCRATCH=str(d / "scratch"))
    a = subprocess.Popen([PY, "-c", code, "A"], env=env, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    b = subprocess.Popen([PY, "-c", code, "B"], env=env, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ea = a.communicate(timeout=300)[1]
    eb = b.communicate(timeout=300)[1]
    check("both writers finished", a.returncode == 0 and b.returncode == 0,
          (ea or b"").decode()[:200] + (eb or b"").decode()[:200])
    lines = log_lines(d)
    check(f"every line of both survives ({len(lines)})", len(lines) == 300,
          f"{len(lines)} of 300 — a lost line is a lost turn")
    bad = [l for l in lines if not l.startswith("{") or not l.endswith("}")]
    check("and none is torn in half", not bad, str(bad[:2]))
    whose = {json.loads(l)["session"] for l in lines if l.startswith("{")}
    check("from both sessions", whose == {"A", "B"}, str(whose))


def test_a_line_already_in_the_file_is_never_rewritten() -> None:
    """The behavioural form of "no read-modify-write". A writer that rewrites the
    file cannot leave a byte it did not understand alone — so a deliberately
    unparseable line is the tripwire, and the reader must step over it rather
    than lose the whole log to it."""
    d, _ = workspace()
    log = d / "scratch/companion-faults.jsonl"
    log.write_text("this line is not json at all\n", encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "-c",
                    f"import sys; sys.path.insert(0, {str(ROOT)!r});"
                    "import companion_faults as C;"
                    "C.append({'at': 'z', 'session': 's', 'reason': 'kept',"
                    " 'cwd': '/tmp'})"],
                   env=env, cwd=ROOT, capture_output=True, timeout=300)
    body = log.read_text(encoding="utf-8")
    check("the line nobody could parse is still there, byte for byte",
          body.startswith("this line is not json at all\n"), body[:120])
    import companion_faults as C
    rows = C.parse(body.splitlines())
    check("and the reader returns the record beside it", len(rows) == 1, str(rows))
    check("naming what it could not read rather than dropping it silently",
          C.unreadable(body.splitlines()) == 1, str(C.unreadable(body.splitlines())))


def test_the_reader_is_bounded() -> None:
    """A systemic fault is 49 sessions times every turn. The writer must not
    trim — the reader caps, and says the figure is a tail."""
    import companion_faults as C
    lines = [json.dumps({"at": f"2026-09-08T00:{i % 60:02d}:00Z", "session": "s",
                         "reason": "r", "cwd": "/tmp"}) for i in range(C.READ_TAIL * 3)]
    rows = C.parse(lines)
    check(f"the read is capped at {C.READ_TAIL}", len(rows) == C.READ_TAIL,
          f"{len(rows)} rows from {len(lines)} lines")
    check("and the cap keeps the NEWEST lines, which are the ones a diagnosis "
          "needs", rows[-1]["at"] == json.loads(lines[-1])["at"],
          f"{rows[-1]['at']} vs {json.loads(lines[-1])['at']}")


# ─────────── what the board says about it ──────────────────────────────

def test_the_board_counts_lost_turns_from_the_log() -> None:
    import build_findings as B
    rows = [{"at": "2026-09-08T08:00:00Z", "session": "abcdef123456",
             "reason": "IllegalTransition: planted", "cwd": "/tmp/estate/one"},
            {"at": "2026-09-08T09:00:00Z", "session": "zzzzzz999999",
             "reason": "OperationalError: database is locked",
             "cwd": "/tmp/estate/two"}]
    found = B.companion_findings(rows, {"fault": False}, 0)
    got = [f for f in found if f["type"] == "companion.not_recording"]
    check("one row, not one per fault", len(got) == 1, str([f["type"] for f in found]))
    if not got:
        return
    f = got[0]
    check("it counts the turns", "2" in f["title"], f["title"])
    check("it names the newest", "09:00" in f["detail"] or "2026-09-08T09" in f["detail"],
          f["detail"][:240])
    check("it names both sessions, because a fault belongs to one",
          "abcdef" in f["detail"] and "zzzzzz" in f["detail"], f["detail"][:300])
    check("and both projects", "one" in f["detail"] and "two" in f["detail"],
          f["detail"][:300])
    check("with the log as evidence",
          any("companion-faults" in e for e in f["evidence"]), str(f["evidence"]))

    # A LINE NOBODY COULD READ IS A FACT ABOUT THE COUNT. Concurrent appends are
    # this file's normal case, so a torn line is plausible rather than exotic,
    # and a total that quietly omits one understates the thing it reports.
    said = B.companion_findings(rows, {"fault": False}, 2)[0]["detail"]
    check("an unreadable line makes the count an explicit floor",
          "2 line(s)" in said and "understates" in said, said[-200:])
    quiet = B.companion_findings(rows, {"fault": False}, 0)[0]["detail"]
    check("and nothing is said about it when there is none",
          "understates" not in quiet, quiet[-160:])

    # A SYSTEMIC FAULT IS EVERY SESSION AT ONCE, so the list is bounded — and
    # the bound has to say how many it left out. The first version of this rule
    # cut `projects` and `reasons` with a bare slice and said nothing, in the
    # iteration whose whole subject is a count that understates itself.
    many = [{"at": f"2026-09-08T09:{i:02d}:00Z", "session": f"sess{i:04d}",
             "reason": f"Boom{i}: planted", "cwd": f"/tmp/estate/p{i}"}
            for i in range(B.COMPANION_LISTED + 2)]
    said = B.companion_findings(many, {"fault": False}, 0)[0]
    check("a long list says how many it did not name",
          "2 more" in said["detail"], said["detail"][:300])
    check("and the count in the title is the whole set, not the shown part",
          str(len(many)) in said["title"], said["title"])


def test_an_empty_log_is_reported_as_nothing_rather_than_as_health() -> None:
    """Absent is not zero only where the difference is knowable: with no faults
    and a slot that is not a fault, there is genuinely nothing to say."""
    import build_findings as B
    check("no faults, no row",
          B.companion_findings([], {"fault": False}, 0) == [], "")


def test_a_fault_the_log_could_not_keep_is_still_reported() -> None:
    """The third outcome. If the append failed — a full disk, a store directory
    that has gone — the count above is a FLOOR, and a floor presented as a total
    is the silence this whole suite exists to remove."""
    import build_findings as B
    # STAMPED FROM NOW, not written as a literal. The branch under test compares
    # the fault's age against the window, so a fixed date would make this suite
    # pass today and go red in a week — a failure mode this session has already
    # walked into once.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    slot_doc = {"fault": True, "reason": "IllegalTransition: planted",
                "at": now, "session": "orphan12"}
    found = B.companion_findings([], slot_doc, 0)
    got = [f for f in found if f["type"] == "companion.faults_unlogged"]
    check("the orphaned fault is reported", len(got) == 1,
          str([f["type"] for f in found]))
    if got:
        check("and it says the durable log is what failed",
              "could not" in got[0]["detail"].lower(), got[0]["detail"][:200])
    check("no double count when the log DID keep it",
          not [f for f in B.companion_findings(
              [{**slot_doc, "cwd": "/tmp"}], slot_doc, 0)
              if f["type"] == "companion.faults_unlogged"],
          "the same fault reported twice teaches the operator to discount both")


# ─────────── the store's own faults reach the store's own log ──────────

def test_a_sqlite_error_records_the_machines_state() -> None:
    """Every turn of every session opens the store, and until now the one
    component that does it most often recorded nothing when it failed. Free
    space, WAL size and the holder count are gone by the time anybody looks."""
    d, env = workspace()
    fault_turn(env, "s-sql",
               exc="__import__('sqlite3').OperationalError('database is locked')")
    p = d / "scratch/store-faults.jsonl"
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] \
        if p.is_file() else []
    check("the store fault is recorded", len(rows) == 1, str(rows)[:300])
    if rows:
        check("under the companion's own name, because the remedy is addressed "
              "by operation", "companion" in rows[0]["op"], str(rows[0]["op"]))
        check("with the conditions at the moment of failure",
              "free_bytes" in rows[0] and "wal_bytes" in rows[0], str(rows[0])[:200])
    check("and the turn is still reported as lost", len(log_lines(d)) == 1,
          str(log_lines(d)))


def test_a_domain_refusal_is_not_filed_as_a_store_fault() -> None:
    """`IllegalTransition` is the lifecycle saying no. Filing it in a log whose
    every reader reports "the store failed" would make the board lie, and would
    spend an `lsof` per turn per session on telemetry about nothing."""
    d, env = workspace()
    fault_turn(env, "s-dom")
    check("nothing is added to the store's fault log",
          not (d / "scratch/store-faults.jsonl").exists(),
          str((d / "scratch/store-faults.jsonl").exists()))
    check("but the lost turn is recorded", len(log_lines(d)) == 1, str(log_lines(d)))


def test_the_recorder_still_never_raises_at_the_hook() -> None:
    """Two writes were added to a component whose contract is that it cannot
    fail the session it runs in."""
    d, env = workspace()
    (d / "scratch/companion-faults.jsonl").mkdir()          # the append cannot work
    p = fault_turn(env, "s-blocked")
    check("the recorder still exits 0", p.returncode == 0,
          f"rc={p.returncode} {p.stderr[-200:]}")
    check("and still answers the hook on stdout",
          '"recorded": false' in p.stdout.lower(), p.stdout[:200])
    check("naming the write it could not make, for a person running it by hand",
          "companion-faults" in p.stderr or "fault log" in p.stderr, p.stderr[:300])
    check("and the slot still carries the fault, which is now the only record",
          slot(d).get("fault") is True, str(slot(d))[:200])


if __name__ == "__main__":
    print("lost turns — recorded in a slot the next session overwrote\n")
    for fn in (test_a_fault_outlives_the_next_turn_of_another_session,
               test_an_answer_about_the_work_is_not_written_to_the_fault_log,
               test_two_sessions_faulting_at_once_both_survive,
               test_a_line_already_in_the_file_is_never_rewritten,
               test_the_reader_is_bounded,
               test_the_board_counts_lost_turns_from_the_log,
               test_an_empty_log_is_reported_as_nothing_rather_than_as_health,
               test_a_fault_the_log_could_not_keep_is_still_reported,
               test_a_sqlite_error_records_the_machines_state,
               test_a_domain_refusal_is_not_filed_as_a_store_fault,
               test_the_recorder_still_never_raises_at_the_hook):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma turn nobody recorded is now recorded somewhere that keeps it\033[0m")
