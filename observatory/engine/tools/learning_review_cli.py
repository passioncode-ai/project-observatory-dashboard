#!/usr/bin/env python3
"""`project-observatory full learning-review show|list|run`: the daily learning review (OBS-331), for a person.

The logic is `learning_review.py`; this file parses and prints. `show` reads the newest report (or one day's) and
names its page; `list` the recent days with their state and counts; `run` writes what is due, or one closed UTC day.
Nothing here sends anything anywhere: the report and its page stay under `<state>/learning-review/`.

Exits: 0 answered (written, nothing due, already complete, a report shown), 2 a refusal with its code (a day that is
not a real day, not closed in UTC or older than the 400 days kept; no report; a damaged report; another review being
written; a review folder that cannot be written).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _day(value: str):
    import learning_review as LR
    day = LR.parse_day(value)
    if day is None:
        raise argparse.ArgumentTypeError(f"day-invalid: {value!r} is not a day written YYYY-MM-DD")
    return day


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="project-observatory full learning-review",
        description="The daily learning review: once a UTC day, one local report of new, revised, replaced and "
                    "promoted lessons grouped by class, and rising failures from products' daily digests, each item "
                    "with its source. Nothing is sent anywhere.")
    sub = p.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="the newest report, or one day's; prints where its page is")
    show.add_argument("--day", type=_day, help="the UTC day, YYYY-MM-DD")
    show.add_argument("--json", action="store_true", help="print the report itself")
    ls = sub.add_parser("list", help="the recent days with their state, revision and counts")
    ls.add_argument("--days", type=int, default=14, help="how many days (default 14)")
    ls.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="write the reports that are due, or one closed UTC day's")
    run.add_argument("--day", type=_day, help="a closed UTC day, YYYY-MM-DD")
    run.add_argument("--again", action="store_true",
                     help="with --day: write a new revision of a report already written, keeping the previous one")
    run.add_argument("--json", action="store_true")
    return p


def _refused(exc, as_json: bool) -> int:
    if as_json:
        print(json.dumps(exc.envelope(), indent=2))
    else:
        print(f"Observatory: {exc.code}: {exc.detail}. {exc.envelope()['hint']}", file=sys.stderr)
    return 2


def _text(doc: dict) -> str:
    """A report on a terminal: the window, the counts, what to look at first, and the sources' states."""
    s, out = doc["summary"], []
    out.append(f"learning review of {doc['day']} (UTC {doc['window']['from']} to {doc['window']['to']}), "
               f"{doc['state']}, revision {doc['revision']}, written {doc['writtenAt']}")
    out.append(f"  lessons: {s['lessonsNew']} new, {s['lessonsRevised']} revised, {s['lessonsReplaced']} replaced, "
               f"{s['promoted']} promoted, {s['invalidated']} invalidated")
    out.append(f"  repeated classes: {s['classesRepeated']} changed, {s['classesRepeatedUnchanged']} unchanged")
    out.append(f"  failures: {s['rises']} rises, {s['risingRepeatedly']} rising repeatedly, "
               f"{s['arrivedLate']} lines arrived late")
    for f in [f for f in doc["failures"] if f["code"] == "rising-repeatedly"][:10]:
        out.append(f"  rising repeatedly: {f['project']} {f['op']} on {', '.join(f['days'] or [])}  [{f['line']}]")
    for i in [i for i in doc["lessons"] if i["code"] == "lesson-promoted"][:10]:
        out.append(f"  promoted: {i['classKey']} ({i['project'] or i['origin']['projectId']})  [{i['source']}]")
    for name, block in doc["sources"].items():
        out.append(f"  {name}: {block.get('state')}")
    for d in doc["degraded"]:
        out.append(f"  degraded: {d.get('source')} {d.get('code')}: {d.get('reason')}")
    if doc["daysNotReviewed"]:
        out.append(f"  days not reviewed: {', '.join(doc['daysNotReviewed'])}")
    import learning_review as LR
    written = LR.parse_stamp(doc.get("writtenAt"))
    if written is not None and written > LR.utc_now():
        out.append("  written ahead of this machine's clock: the clock was set back after it was written")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    import learning_review as LR
    as_json = getattr(args, "json", False)
    try:
        if args.command == "show":
            if args.day is not None:
                doc = LR.read_report(args.day)
                if doc is None:
                    raise LR.Refused("no-report", f"no review of {args.day.isoformat()} has been written")
            else:
                doc = LR.newest_report()
                if doc is None:
                    raise LR.Refused("no-report", "no review has been written yet")
            if as_json:
                print(json.dumps(doc, indent=2, ensure_ascii=False))
            else:
                print(_text(doc))
                print(f"html: {LR.report_path(LR.parse_day(doc['day']), '.html')}")
            return 0
        if args.command == "list":
            rows = LR.listing(args.days)
            if as_json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("no review has been written yet: `project-observatory full learning-review run` writes "
                      "what is due")
            else:
                for r in rows:
                    s = r["summary"] or {}
                    print(f"{r['day']}  {r['state']:<9} r{r['revision'] if r['revision'] else '?'}  "
                          f"{s.get('lessonsNew', '-')} new  {s.get('promoted', '-')} promoted  "
                          f"{s.get('classesRepeated', '-')} classes  {s.get('rises', '-')} rises  "
                          f"{s.get('arrivedLate', '-')} late")
            return 0
        if args.again and args.day is None:
            raise LR.Refused("day-invalid", "--again needs --day: it writes a new revision of one day's report")
        answer = LR.run(day=args.day, again=args.again)
        if as_json:
            print(json.dumps(answer, indent=2))
        else:
            for line in answer["written"]:
                print(line)
            if answer["outcome"] == "already-complete":
                print(f"already-complete: the review of {answer['day']} is complete; --again writes a new revision")
            elif answer["outcome"] == "not-due":
                print(f"not-due: nothing to write; the next report is due at {answer['next']}")
            elif answer["outcome"] == "review-unwritable":
                print(f"review-unwritable: {answer.get('reason')}; the previous report stands. "
                      f"{LR.HINTS['review-unwritable']}", file=sys.stderr)
        return 2 if answer["outcome"] == "review-unwritable" else 0
    except LR.Refused as exc:
        return _refused(exc, as_json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
