#!/usr/bin/env python3
"""A block the system knows is temporary, offering a remedy its cause forbids.

Measured once when a shared provider key reached 300.0538 of 300 — of which
this project's own journal held 0.1185 (the figures are kept as the fixture):

* `interpretation.halted` offers three remedies. The live cause is that the
  KEY's own limit is spent, and the first remedy — "raise the ceiling in
  `agent/models.json`" — **cannot work**: the provider enforces the key's limit
  and returns 402 regardless of what any ceiling in this repository says. A
  remedy its own cause makes impossible is worse than none, because the reader
  spends their attention on it and comes back to the same wall.

* Neither finding says WHEN the block lifts by itself. `store/key-usage.json`
  carries `limit_reset: "monthly"` — a word, not a date. So a warning that will
  be lit for three weeks reads identically to one nobody has looked at, and the
  operator cannot tell "wait" from "act".

**What was checked and is NOT wrong**, recorded so the next reader does not
re-open it:

* The agent DEGRADES rather than fails: it logs `DEGRADED: …`, leaves the deltas
  unconsumed, says the collectors are unaffected, writes `halted_by`, and exits
  0. No `tick.step_failed` for it.
* `index` keeps running, because embeddings are bought from a different
  provider.
* The queue is not a storage problem: 40 deltas hold 351 bytes of payload, nine
  per row, so three weeks of them is tens of kilobytes.
* The queue can drain. The per-run cap is 20 PROJECTS, oldest first, and a run
  consumes every delta of the projects it takes — so a backlog of any size
  collapses into at most one project-slot per project, six runs for the whole
  estate.

The pattern to follow is already in this file's own neighbour: the dark-site
finding branches on whether the cause is known, changes severity, names the
cause in the detail, and replaces the action entirely — "settle the hold on
<domain> first — this host cannot be restored while it stands". No cause graph
was built for that and none is needed here.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                  # noqa: E402
# A private synthetic workspace; the fixture below points the findings build at
# its own registry and store, so nothing on the machine running it is read.
from test_portable_mcp import setup as portable_setup                 # noqa: E402
portable_setup()

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def B():
    import build_findings
    return build_findings


# ─────────── the date is derived once, or refused ──────────────────────

def test_monthly_resolves_to_the_first_of_next_month() -> None:
    b = B()
    fn = getattr(b, "reset_date", None)
    if fn is None:
        check("build_findings.reset_date exists", False,
              "two findings must not derive the same date twice")
        return
    at = datetime(2026, 9, 7, 22, 48, tzinfo=timezone.utc)
    check("monthly means the first of next month, in UTC",
          fn("monthly", at) == "2026-10-01", str(fn("monthly", at)))
    check("December rolls the year",
          fn("monthly", datetime(2026, 12, 20, tzinfo=timezone.utc)) == "2027-01-01",
          str(fn("monthly", datetime(2026, 12, 20, tzinfo=timezone.utc))))


def test_the_reset_is_counted_from_the_measurement_not_from_today() -> None:
    """A spend limit measured as spent on 2026-09-07 lifts on 2026-10-01. Counted
    from the wall clock instead, the same document said 2026-11-01 from October
    on: a date nobody measured, and a month late. Found on 2026-10-01, when every
    fixture in this file stopped agreeing with itself."""
    b = B()
    fn = getattr(b, "key_reset_date", None)
    check("build_findings.key_reset_date exists", fn is not None, "")
    if fn is None:
        return
    doc = {"limit_reset": "monthly", "checked_at": "2026-09-07T22:48:54Z"}
    check("a September measurement lifts on the first of October",
          fn(doc) == "2026-10-01", str(fn(doc)))
    check("a measurement with no time falls back to today, and says a date only for a known word",
          fn({"limit_reset": "never"}) is None and fn({"limit_reset": "monthly"}) is not None, "")
    check("an unreadable time is not a measurement",
          fn({"limit_reset": "monthly", "checked_at": "yesterday"}) == b.reset_date("monthly"), "")


def test_a_word_it_cannot_read_is_refused_not_guessed() -> None:
    b = B()
    fn = getattr(b, "reset_date", None)
    if fn is None:
        return
    at = datetime(2026, 9, 7, tzinfo=timezone.utc)
    for word in ("weekly", "never", "", None):
        check(f"{word!r} yields no date rather than a wrong one",
              fn(word, at) is None, str(fn(word, at)))
    # THE THIRD OUTCOME. "daily" is a word this provider might send and this
    # function does not interpret; returning None makes the finding say so
    # instead of printing a date nobody measured.


# ─────────── the symptom branches on its cause ─────────────────────────

def estate(key_usage: dict | None, agent: dict) -> list[dict]:
    """`collect()` over a fixture that owns its list.

    `paths.STORE`/`STATE` are pointed at the fixture for the same reason
    `tests/test_cause_and_symptom.py` had to do it twice: the live wallet and the
    live volume otherwise leak into a claim about the fixture's own findings.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-block-"))
    for sub in ("registry", "scratch", "store"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "registry/projects.json").write_text('{"projects": []}')
    (d / "registry/repositories.json").write_text('{"repositories": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    (d / "registry/domains.json").write_text('{"domains": []}')
    (d / "scratch/agent.json").write_text(json.dumps(agent))
    if key_usage is not None:
        (d / "store/key-usage.json").write_text(json.dumps(key_usage))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    b = B()
    importlib.reload(b)
    b.paths.STORE = d / "store"
    b.paths.STATE = d / "store"
    import collections as C
    usage = C.namedtuple("usage", "total used free")
    healthy = (b.DISK_WARNING_MIB + 1) * 1024 * 1024
    b.shutil.disk_usage = lambda p: usage(healthy, 0, healthy)
    try:
        return b.collect()
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


STALLED = {
    "ran_at": "2026-09-07T22:44:38Z",
    "recorded": 0, "skipped": 0, "failed": 0,
    "unconsumed": 40,
    "oldest_unconsumed_scan": "2026-09-01T00:00:00Z",   # far past the 6h horizon
    "halted_by": "spend guardrail: the limit on this KEY is spent",
}
SPENT = {"checked_at": "2026-09-07T22:48:54Z", "daily": 300.05, "monthly": 300.05,
         "total": 300.05, "limit": 300, "limit_remaining": 0.0,
         "limit_reset": "monthly", "note": "x"}
ROOMY = {"checked_at": "2026-09-07T22:48:54Z", "daily": 1.0, "monthly": 5.0,
         "total": 5.0, "limit": 300, "limit_remaining": 295.0,
         "limit_reset": "monthly", "note": "x"}


def halted(rows: list[dict]) -> dict | None:
    return next((f for f in rows if f["type"] == "interpretation.halted"), None)


def test_with_the_key_spent_the_ceiling_remedy_is_withdrawn() -> None:
    f = halted(estate(SPENT, STALLED))
    check("the stall is still reported", f is not None,
          "withdrawing a remedy must not withdraw the finding")
    if f is None:
        return
    blob = json.dumps(f, ensure_ascii=False)
    check("`agent/models.json` is NOT offered as the fix",
          "models.json" not in f["action"],
          "the provider enforces the KEY's limit and returns 402 whatever any "
          "ceiling here says: " + f["action"][:150])
    check("the cause is named instead",
          "key" in f["detail"].lower() and "spent" in f["detail"].lower(),
          f["detail"][-200:])
    check("and the remedies that DO work are kept",
          "own key" in f["action"] or "separate key" in f["action"], f["action"][:150])
    check("the date it lifts by itself is stated",
          "2026-10-01" in blob, f["action"][:200] + " | " + f["detail"][-160:])


def test_with_the_key_roomy_the_ceiling_remedy_stays() -> None:
    """The other branch, so the withdrawal is conditional rather than a deletion.
    A ceiling in `agent/models.json` IS what stopped a run whose key has room."""
    f = halted(estate(ROOMY, STALLED))
    check("the stall is reported here too", f is not None)
    if f is None:
        return
    check("and the ceiling remedy is offered",
          "models.json" in f["action"], f["action"][:150])


def test_with_no_key_document_the_cause_is_unknown_not_absent() -> None:
    f = halted(estate(None, STALLED))
    if f is None:
        check("the stall is reported with no key document at all", False,
              "the symptom must not depend on the cause being measurable")
        return
    check("the stall is reported with no key document at all", True)
    check("and no reset date is invented",
          "2026-10-01" not in json.dumps(f, ensure_ascii=False),
          "a date derived from a document that does not exist is a fabrication")


def test_the_cause_and_the_symptom_agree_about_the_date() -> None:
    rows = estate(SPENT, STALLED)
    cause = next((f for f in rows if f["type"] == "wallet.shared_key"), None)
    sym = halted(rows)
    if cause is None or sym is None:
        print("  NOTE  the fixture produced no wallet row; the shared-key rule needs "
              "`others > max(1, own*2)`, covered in tests/test_budget_subject.py")
        return
    check("the cause carries the reset date too",
          "2026-10-01" in json.dumps(cause, ensure_ascii=False),
          json.dumps(cause)[:200])
    check("and it is the same date in both",
          ("2026-10-01" in json.dumps(cause, ensure_ascii=False))
          == ("2026-10-01" in json.dumps(sym, ensure_ascii=False)),
          "one derivation, two readers — two would drift")


if __name__ == "__main__":
    print("a temporary block — one that says when it lifts, and offers "
          "nothing its cause forbids\n")
    for fn in (test_monthly_resolves_to_the_first_of_next_month,
               test_a_word_it_cannot_read_is_refused_not_guessed,
               test_with_the_key_spent_the_ceiling_remedy_is_withdrawn,
               test_with_the_key_roomy_the_ceiling_remedy_stays,
               test_with_no_key_document_the_cause_is_unknown_not_absent,
               test_the_cause_and_the_symptom_agree_about_the_date,
               test_the_reset_is_counted_from_the_measurement_not_from_today):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe block names its own end, and prescribes nothing its cause "
          "forbids\033[0m")
