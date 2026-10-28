#!/usr/bin/env python3
"""Hold the Windows run of the portable engine set to its known list (OBS-277).

The `windows` job of `.github/workflows/check.yml` runs every suite of
`observatory/engine/tests/run_portable.py` on windows-latest and hands its JSON receipt here. A suite
that did not pass is accepted only when `tools/windows-known.json` names it, with the backlog row that
will make it pass, the reason, the outcomes it was seen with (`statuses`: FAIL, ERROR, TIMEOUT) and, with
`--logs`, the checks seen failing in it (`failing`); everything else fails the job:

- a suite that failed, errored or timed out and is not on the list (a regression on Windows);
- a suite that skipped as a whole and is not on the list as `SKIP` (a silent pass);
- a suite on the list that now passes (the list only shrinks: take it off);
- a suite on the list whose outcome is not one recorded for it (a `SKIP` that now fails, a FAIL that times out);
- a check failing inside a listed suite that its entry does not name (a new regression in a known suite);
- a suite on the list the receipt does not hold, or a receipt that does not hold every suite;
- an entry without a reason, or whose row is not an open row of `docs/backlog.md` (a done, cancelled or
  superseded row cannot hold a failure).

A listed check that passes now is reported, not refused: the run says the entry can lose it.

    python tools/check_windows_suites.py RECEIPT.json [--known tools/windows-known.json]
        [--logs DIR]                      # the runner's --report-dir: each suite's run.log, for `failing`
        [--summary PATH]                  # also write a Markdown table (the job summary)

Exit 0 when the receipt matches the list, 1 when it does not, 2 when an input cannot be read.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWN = ROOT / "tools" / "windows-known.json"
BACKLOG = ROOT / "docs" / "backlog.md"
RUNNER = ROOT / "observatory" / "engine" / "tests" / "run_portable.py"
NOT_PASSED = ("FAIL", "ERROR", "TIMEOUT")
ROW = re.compile(r"^OBS-\d+$")


def suite_name(row: dict) -> str:
    """`tests/test_<name>.py` -> `<name>`."""
    name = Path(row["file"]).name
    return name[len("test_"):-len(".py")] if name.startswith("test_") and name.endswith(".py") else name


def runner_suites() -> tuple[str, ...]:
    """Every suite the runner declares, read from its source without running it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("windows_check_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.SUITES)


def backlog_rows(text: str) -> set[str]:
    return set(re.findall(r"^\| (OBS-\d+) \|", text, re.M))


def closed_rows(text: str) -> set[str]:
    """Rows whose Status cell closes them: done, cancelled or superseded."""
    closed = set()
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(" | ")]
        if len(cells) >= 3 and re.fullmatch(r"\| OBS-\d+", cells[0]) \
                and re.match(r"(done|cancelled|superseded)\b", cells[2]):
            closed.add(cells[0][2:])
    return closed


#: unittest: `FAIL: test_x (__main__.Class.test_x)`; assertion lines: `  FAIL  what it checks`, then U+2014 and its detail.
UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR): (?!setUpClass|setUpModule|tearDownClass|tearDownModule)(\w+) \((?:__main__\.)?(?:[\w.]*\.)?(\w+)\.\w+\)", re.M)
UNITTEST_CLASS_ERROR = re.compile(r"^ERROR: (setUpClass|setUpModule|tearDownClass|tearDownModule) \((?:__main__\.)?([\w.]+)\)", re.M)
ASSERTION_FAILED = re.compile(r"^\s*FAIL\s{2}(.+?)\s*$", re.M)


def failing_checks(log: str) -> set[str]:
    """The checks a suite's log reports failing: `Class.test` for unittest, the check's own words (before its
    detail, every number as N) for assertion lines, `<exit>` when the suite stopped without naming one (a crash, a setup that failed)."""
    names = {f"{cls}.{test}" for test, cls in UNITTEST_FAILED.findall(log)}
    names |= {f"{where}.{fixture}" for fixture, where in UNITTEST_CLASS_ERROR.findall(log)}
    for line in ASSERTION_FAILED.findall(log):
        words = re.split(r"\s+\u2014\s+|:\s", line, maxsplit=1)[0].strip()
        if words:
            names.add(re.sub(r"\d+", "N", words)[:160])   # a count in the words is not a different check
    return names or {"<exit>"}


def check(receipt: dict, known: dict, rows: set[str], suites: tuple[str, ...], closed: frozenset = frozenset(),
          logs: Path | None = None) -> tuple[list[str], list[dict]]:
    """The problems, and one line per suite for the summary."""
    problems: list[str] = []
    entries = known.get("suites", {})
    for name, entry in sorted(entries.items()):
        if entry.get("expect") not in ("FAIL", "SKIP"):
            problems.append(f"{name}: the list must say expect FAIL or SKIP, not {entry.get('expect')!r}")
        if not ROW.match(str(entry.get("row", ""))):
            problems.append(f"{name}: the list names no backlog row (OBS-n)")
        elif entry["row"] not in rows:
            problems.append(f"{name}: {entry['row']} is not a row of docs/backlog.md")
        elif entry["row"] in closed:
            problems.append(f"{name}: {entry['row']} is closed; a closed row cannot hold a failure")
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{name}: the list gives no reason")
        if entry.get("expect") == "FAIL" and not set(entry.get("statuses", [])) <= set(NOT_PASSED):
            problems.append(f"{name}: statuses may only name {', '.join(NOT_PASSED)}")
    seen: dict[str, dict] = {}
    for row in receipt.get("suites", []):
        seen[suite_name(row)] = row
    missing = sorted(set(suites) - set(seen))
    if missing:
        problems.append("the receipt does not hold every suite of the runner: " + ", ".join(missing))
    lines = []
    for name in sorted(seen):
        row, entry = seen[name], entries.get(name)
        status = row.get("status")
        verdict = "ok"
        if status == "PASS":
            if entry:
                verdict = f"passes now: take it off tools/windows-known.json ({entry.get('row')})"
                problems.append(f"{name}: {verdict}")
        elif status in NOT_PASSED:
            allowed = (entry.get("statuses") or ["FAIL"]) if entry else []
            if not entry:
                verdict = f"{status} and not on the known list"
                problems.append(f"{name}: {verdict}")
            elif entry.get("expect") != "FAIL":
                verdict = f"{status}, the list expects {entry.get('expect')}"
                problems.append(f"{name}: {verdict}")
            elif status not in allowed:
                verdict = f"{status}, the list records only {', '.join(allowed)}"
                problems.append(f"{name}: {verdict}")
            else:
                verdict = f"known ({entry['row']})"
                log = logs / name / "run.log" if logs is not None else None
                if status == "FAIL" and log is not None and "failing" in entry:
                    if not log.is_file():
                        verdict = "its log is missing, so its failing checks cannot be compared"
                        problems.append(f"{name}: {verdict}")
                    else:
                        now = failing_checks(log.read_text(encoding="utf-8", errors="replace"))
                        new = sorted(now - set(entry["failing"]))
                        gone = sorted(set(entry["failing"]) - now)
                        if new:
                            verdict = "new failing check(s): " + "; ".join(new)
                            problems.append(f"{name}: {verdict}")
                        elif gone:
                            verdict += f"; passing now, can leave the entry: {'; '.join(gone)}"
        elif status == "SKIP":
            if not entry:
                verdict = "skipped as a whole and not on the known list"
                problems.append(f"{name}: {verdict}: {row.get('skip_reason', '')}")
            elif entry.get("expect") != "SKIP":
                verdict = f"SKIP, the list expects {entry.get('expect')}"
                problems.append(f"{name}: {verdict}")
            else:
                verdict = f"known skip ({entry['row']})"
        else:
            verdict = f"unknown status {status!r}"
            problems.append(f"{name}: {verdict}")
        lines.append({"suite": name, "status": status, "skipped_checks": row.get("skipped_checks", 0),
                      "executed_checks": row.get("executed_checks", 0), "verdict": verdict})
    for name in sorted(set(entries) - set(seen)):
        if name in suites:
            continue  # already named as missing from the receipt
        problems.append(f"{name}: on the known list but not a suite of the runner")
    return problems, lines


def summary(lines: list[dict], problems: list[str]) -> str:
    counts: dict[str, int] = {}
    for line in lines:
        counts[line["status"]] = counts.get(line["status"], 0) + 1
    out = ["### Portable engine suites on Windows", "",
           ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" of {len(lines)}", ""]
    out += ["| Suite | Status | Checks run | Checks skipped | Verdict |", "|---|---|---|---|---|"]
    for line in lines:
        if line["status"] != "PASS" or line["skipped_checks"] or line["verdict"] != "ok":
            out.append(f"| {line['suite']} | {line['status']} | {line['executed_checks']} | "
                       f"{line['skipped_checks']} | {line['verdict']} |")
    out += ["", "Problems: " + ("none" if not problems else str(len(problems)))]
    out += [f"- {p}" for p in problems]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("receipt", type=Path)
    ap.add_argument("--known", type=Path, default=KNOWN)
    ap.add_argument("--backlog", type=Path, default=BACKLOG)
    ap.add_argument("--logs", type=Path, help="the runner's --report-dir, holding each suite's run.log")
    ap.add_argument("--summary", type=Path)
    args = ap.parse_args(argv)
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        known = json.loads(args.known.read_text(encoding="utf-8"))
        board = args.backlog.read_text(encoding="utf-8")
        rows, closed = backlog_rows(board), frozenset(closed_rows(board))
        suites = runner_suites()
    except (OSError, ValueError) as exc:
        print(f"check_windows_suites: cannot read an input: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    problems, lines = check(receipt, known, rows, suites, closed, args.logs)
    text = summary(lines, problems)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
