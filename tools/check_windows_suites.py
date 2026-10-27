#!/usr/bin/env python3
"""Hold the Windows run of the portable engine set to its known list (OBS-277).

The `windows` job of `.github/workflows/check.yml` runs every suite of
`observatory/engine/tests/run_portable.py` on windows-latest and hands its JSON receipt here. A suite
that did not pass is accepted only when `tools/windows-known.json` names it, with the backlog row that
will make it pass and the reason; everything else fails the job:

- a suite that failed, errored or timed out and is not on the list (a regression on Windows);
- a suite that skipped as a whole and is not on the list as `SKIP` (a silent pass);
- a suite on the list that now passes (the list only shrinks: take it off);
- a suite on the list whose outcome is not the one recorded (a `SKIP` that now fails, say);
- a suite on the list the receipt does not hold, or a receipt that does not hold every suite;
- an entry without a reason, or whose row is not a row of `docs/backlog.md`.

    python tools/check_windows_suites.py RECEIPT.json [--known tools/windows-known.json]
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


def check(receipt: dict, known: dict, rows: set[str], suites: tuple[str, ...]) -> tuple[list[str], list[dict]]:
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
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{name}: the list gives no reason")
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
            if not entry:
                verdict = f"{status} and not on the known list"
                problems.append(f"{name}: {verdict}")
            elif entry.get("expect") != "FAIL":
                verdict = f"{status}, the list expects {entry.get('expect')}"
                problems.append(f"{name}: {verdict}")
            else:
                verdict = f"known ({entry['row']})"
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
    ap.add_argument("--summary", type=Path)
    args = ap.parse_args(argv)
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        known = json.loads(args.known.read_text(encoding="utf-8"))
        rows = backlog_rows(args.backlog.read_text(encoding="utf-8"))
        suites = runner_suites()
    except (OSError, ValueError) as exc:
        print(f"check_windows_suites: cannot read an input: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    problems, lines = check(receipt, known, rows, suites)
    text = summary(lines, problems)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
