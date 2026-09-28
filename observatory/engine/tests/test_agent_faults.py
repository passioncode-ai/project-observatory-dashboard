#!/usr/bin/env python3
"""`failed` was written on every run and read by nothing.

An earlier change gave the agent's duplicate-check read a guard: a transient
`sqlite3.DatabaseError` now costs one project's delta instead of the whole run.
**That closed a blast radius and opened a silence** — the run no longer exits
non-zero, so `tick.step_failed` will not fire for it, and the only record is a
line on stderr in a log nothing reads on a schedule. That trade was made
deliberately and said so; this is the other half.

Measuring it found the gap is older and wider than that one case.
`agent/observe.py` increments `failed` in **seven** places and puts the count in
`store/raw/agent.json`, and **no finding reads it** — the one other `rec["failed"]`
in `tools/build_findings.py` belongs to retention's receipt, not the agent's.
Two of the seven have their own finding (`interpretation.malformed`, and a
refused credential through `halted_by`). The remaining five reach no surface at
all:

    providers.Fatal          a model rejected the request outright
    providers.Retryable      every model in the chain failed
    an unexpected exception  anything the loop did not anticipate
    the ledger refused       the write was rejected by the store's own rules
    sqlite3.DatabaseError    the duplicate-check read (the guard above)

So the counter gets a reader, and the REASONS travel with it: a number saying
"3 projects failed" sends the reader to a log to find out what happened, which is
the same journey the log was supposed to save them.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def findings_for(report: dict) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-faults-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    (d / "scratch/agent.json").write_text(json.dumps(report))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("interpretation.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def run(**over) -> dict:
    base = {"ran_at": stamp(0.1), "recorded": 0, "skipped": 0, "failed": 0,
            "malformed": 0, "unreasoned": 0, "not_english": 0,
            "chain_retired": [], "halted_by": None, "unconsumed": 0,
            "oldest_unconsumed_scan": None, "projects_handled": [],
            "projects_waiting": [], "waiting_count": 0, "faults": []}
    return {**base, **over}


def only(got: list[dict], kind: str) -> dict | None:
    rows = [f for f in got if f["type"] == kind]
    return rows[0] if len(rows) == 1 else None


# ─────────── the counter gains a reader ────────────────────────────────

def test_a_store_fault_reaches_a_surface() -> None:
    """The silence the duplicate-check guard opened, closed. This is the one case where the agent
    keeps going and the tick exits zero, so nothing else can report it."""
    got = findings_for(run(failed=1, faults=[
        {"project": "project:p", "kind": "store",
         "reason": "DatabaseError: database disk image is malformed"}]))
    f = only(got, "interpretation.faults")
    check("it fires", f is not None, str([x["type"] for x in got]))
    if f is None:
        return
    check("as a warning", f["severity"] == "warning", f["severity"])
    check("naming the kind", "store" in f["detail"], f["detail"][:220])
    check("and quoting the reason",
          "malformed" in f["detail"], f["detail"][:260])
    check("with the project named",
          "project:p" in f["detail"], f["detail"][:260])
    check("the action points at the store, not at the models",
          "model" not in f["action"].lower(), f["action"])


def test_every_uncovered_kind_is_reported() -> None:
    """Five causes had no surface. Each must reach one, and the finding must say
    which — "3 projects failed" sends the reader to the log the finding was
    supposed to spare them."""
    kinds = ["model-fatal", "model-unavailable", "unexpected", "ledger", "store"]
    got = findings_for(run(failed=len(kinds), faults=[
        {"project": f"project:p{i}", "kind": k, "reason": f"reason {i}"}
        for i, k in enumerate(kinds)]))
    f = only(got, "interpretation.faults")
    check("one finding for the run, not one per project", f is not None,
          str([x["type"] for x in got]))
    if f is None:
        return
    check("the count is in the title", "5" in f["title"], f["title"])
    for k in kinds:
        check(f"`{k}` is named", k in f["detail"], f["detail"][:300])


def test_a_kind_that_has_its_own_finding_is_not_double_reported() -> None:
    """`malformed` and a refused credential already have findings. Reporting them
    twice is noise that was removed once already — one cause, stated once."""
    got = findings_for(run(failed=2, malformed=1, faults=[
        {"project": "project:a", "kind": "malformed", "reason": "self-contradicting"},
        {"project": "project:b", "kind": "store", "reason": "malformed image"}]))
    f = only(got, "interpretation.faults")
    check("the faults finding fires for the store fault", f is not None,
          str([x["type"] for x in got]))
    if f:
        check("and counts one, not two", "1" in f["title"], f["title"])
        check("leaving `malformed` to its own finding",
              "self-contradicting" not in f["detail"], f["detail"][:240])
    check("which is still raised", only(got, "interpretation.malformed") is not None,
          str([x["type"] for x in got]))


def test_a_clean_run_raises_nothing() -> None:
    check("no faults, no finding",
          only(findings_for(run(recorded=8)), "interpretation.faults") is None,
          "a run that interpreted everything it took has nothing to say here")


def test_a_count_with_no_reasons_still_reports() -> None:
    """An older report — written before `faults` existed — carries the count and
    no list. Saying "3 failed and the reasons are not recorded" is honest; going
    silent because the detail is missing is not."""
    got = findings_for(run(failed=3, faults=[]))
    f = only(got, "interpretation.faults")
    check("it fires on the count alone", f is not None,
          str([x["type"] for x in got]))
    if f:
        check("and says the reasons are unavailable",
              "not recorded" in f["detail"] or "no reason" in f["detail"],
              f["detail"][:240])


def test_a_long_fault_list_announces_what_it_omits() -> None:
    got = findings_for(run(failed=20, faults=[
        {"project": f"project:p{i}", "kind": "store", "reason": f"r{i}"}
        for i in range(20)]))
    f = only(got, "interpretation.faults")
    check("it fires", f is not None, "")
    if f:
        check("the detail is bounded", len(f["detail"]) < 1200, str(len(f["detail"])))
        check("and says how many it left out",
              "more" in f["detail"] or "not shown" in f["detail"], f["detail"][-160:])


# ─────────── the agent records the reasons ─────────────────────────────

def test_the_agent_collects_a_reason_for_every_increment() -> None:
    """Seven `failed += 1` sites; each must append a fault. A counter that moves
    without a reason is what made this finding impossible to write."""
    import source_reader
    src = source_reader.code_only((ROOT / "agent/observe.py").read_text(encoding="utf-8"))
    body = src.split("def main(")[1]
    # THE RAW SOURCE for a string KEY, and `code_only` for the counting below.
    # `code_only` blanks string literals — that is what makes it right for
    # counting statements and wrong for asking whether a dict carries a field.
    # A source assertion using the wrong reader for its question has happened
    # often enough that the rule now sits in the test that tripped on it.
    raw = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("the report carries the reasons",
          '"faults": faults' in raw,
          "the counter alone sends the reader to a log")
    # `note_fault(` and not `fault(`: the shorter form is a substring of
    # `default(` and would have counted an unrelated call as a recorded reason.
    check("every increment is accompanied by one",
          body.count("failed += 1") <= body.count("note_fault("),
          f"{body.count('failed += 1')} increment(s), "
          f"{body.count('note_fault(')} recorded")


if __name__ == "__main__":
    print("agent faults — a counter nobody read, and five silent causes\n")
    for fn in (test_a_store_fault_reaches_a_surface,
               test_every_uncovered_kind_is_reported,
               test_a_kind_that_has_its_own_finding_is_not_double_reported,
               test_a_clean_run_raises_nothing,
               test_a_count_with_no_reasons_still_reports,
               test_a_long_fault_list_announces_what_it_omits,
               test_the_agent_collects_a_reason_for_every_increment):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery project the agent could not interpret says why\033[0m")
