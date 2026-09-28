#!/usr/bin/env python3
"""The export gate: what it must still catch after being made livable.

`--check` was byte equality — "is the export CURRENT" — and it went red twice in
one session with nothing wrong. The companion plugin's `Stop` hook writes a
ledger row at the end of every turn, so an export is stale seconds after it is
written. The danger of loosening such a gate is obvious and is the whole point of
this file: a gate that stops failing for the real reason is worse than one that
fails for the wrong reason, because it reads as evidence.

So each case below is driven, including the two that must still FAIL.
"""
from __future__ import annotations
import json, pathlib, sys, tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))
import export_ledger as ex                                          # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def stamp(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(mid: str, rev: int = 1, age: float = 10, statement: str = "s") -> dict:
    return {"_kind": "revision", "memory_id": mid, "revision": rev,
            "created_at": stamp(age), "statement": statement, "owner": "agent:x"}


def with_export(rows_in_file: list[dict], stored: list[dict]) -> tuple[int, list[str]]:
    """Run the audit with a temporary export file standing in for the real one."""
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-export-")) / "ledger.jsonl"
    tmp.write_text(ex.render(rows_in_file), encoding="utf-8")
    original = ex.OUT
    ex.OUT = tmp
    try:
        return ex.audit(stored)
    finally:
        ex.OUT = original


def test_an_exact_match_is_current() -> None:
    rows = [row("mem:a"), row("mem:b")]
    code, out = with_export(rows, rows)
    check("an exact match passes", code == 0, str(out))
    check("and says so plainly", any("current" in l for l in out), str(out))


def test_a_row_written_seconds_ago_is_not_a_failure() -> None:
    """The case that made the gate unusable: the Stop hook just wrote a row."""
    exported = [row("mem:a", age=600)]
    stored = exported + [row("mem:new", age=5)]
    code, out = with_export(exported, stored)
    check("a fresh unexported row does NOT fail the gate", code == 0, str(out))
    check("but it is reported, not hidden",
          any("written since the last export" in l for l in out), str(out))


def test_a_row_older_than_the_grace_STILL_fails() -> None:
    """The real defect: nothing is exporting any more."""
    exported = [row("mem:a", age=9000)]
    stored = exported + [row("mem:forgotten", age=ex.GRACE_SECONDS + 600)]
    code, out = with_export(exported, stored)
    check("an old unexported row fails the gate", code == 1, str(out))
    check("and the message names the cause, not the symptom",
          any("Nothing is exporting" in l for l in out), str(out))


def test_a_diverged_row_fails_however_fresh() -> None:
    """Staleness is forgiven; disagreement is not."""
    exported = [row("mem:a", age=5, statement="what the export says")]
    stored = [dict(exported[0], statement="what the store says")]
    code, out = with_export(exported, stored)
    check("a record that differs fails even seconds old", code == 1, str(out))
    check("and it is called divergence, not staleness",
          any("DIVERGED" in l for l in out), str(out))


def test_an_export_ahead_of_the_store_is_reported_not_failed() -> None:
    """After a store loss the export legitimately holds what the store does not."""
    exported = [row("mem:a"), row("mem:recovered")]
    stored = [exported[0]]
    code, out = with_export(exported, stored)
    check("an export holding more than the store does not fail", code == 0, str(out))
    check("and the surplus is named", any("the store does not" in l for l in out), str(out))


def test_an_unparseable_timestamp_is_treated_as_old() -> None:
    """A row whose age cannot be established is never waved through as recent."""
    check("a missing timestamp reads as infinitely old",
          ex.age_seconds(None) == float("inf"))
    check("a malformed timestamp reads as infinitely old",
          ex.age_seconds("yesterday") == float("inf"))
    exported: list[dict] = []
    stored = [{"_kind": "revision", "memory_id": "mem:x", "revision": 1,
               "created_at": "not-a-date", "statement": "s"}]
    code, out = with_export(exported, stored)
    check("so it fails the gate rather than passing it", code == 1, str(out))


def test_the_check_never_writes_to_the_store() -> None:
    src = (ROOT / "tools/export_ledger.py").read_text(encoding="utf-8")
    head = src.split("if \"--check\" in argv:", 1)[1].split("conn = store_db.connect()")[0]
    check("--check opens the store read-only", "mode=ro" in head,
          "store_db.connect() opens read-write and now applies migrations, so a gate "
          "step would write into the store it audits")


if __name__ == "__main__":
    print("the ledger export gate — livable, and still able to fail\n")
    for fn in (test_an_exact_match_is_current,
               test_a_row_written_seconds_ago_is_not_a_failure,
               test_a_row_older_than_the_grace_STILL_fails,
               test_a_diverged_row_fails_however_fresh,
               test_an_export_ahead_of_the_store_is_reported_not_failed,
               test_an_unparseable_timestamp_is_treated_as_old,
               test_the_check_never_writes_to_the_store):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe gate forgives youth and still refuses divergence\033[0m")
