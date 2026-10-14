#!/usr/bin/env python3
"""What a project spent: its month and last 30 days, from the spend scan (J-11, OBS-93).

    T="$(project-observatory full-path)/tools"
    python "$T/spend.py" project alpha-web --json     # one project: the launcher's recentSpendUsd
    python "$T/spend.py" project --json               # every project, and what no project claims

WHY. Under contract DEC-0037 Fabric's launcher proposes a project's spend limit from its agents' cost presets
and the project's recent spend, and that spend is the figure Observatory measures at the providers rather than
what agents report about themselves (most spend happens on keys whose callers report nothing). This is the
read the launcher calls. It reads `store/raw/spend.json`, which `collectors/scan_spend.py` writes every tick
(step `scan-spend`), and asks no provider itself: a read that spends nothing and answers in milliseconds.

WHAT IT ANSWERS. `monthUsd` (the current UTC month) and `last30Usd` (the last 30 completed UTC days) for the
project, its key count, and `unread`: the ledger keys whose spend could not be read. A window that was not read
whole is `null`, never a lower figure; `partial` says so. A project the door's ledger holds no key for answers
0 with a `note`, because the ledger is complete for keys this door issued and spend on any other key is in
`unattributed`, which names no project by design. `fresh` is false for a scan older than 36 hours or of another
month (the meter's rule, meter.py `measured`), and `degraded` names it.

PROJECT is the project's vault folder, its registry id or its name (`vault_project.resolve`, the rule every
door uses); an ambiguous one is refused with the candidates.

Exit 0 answered (partial or stale included, said in the answer), 1 no usable scan, 2 a project that cannot be
resolved. Never a key, never a hash.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402

#: A scan older than this is not this month's figure any more (meter.py `measured` uses the same bound).
FRESH_SECONDS = 36 * 3600


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _figures(row: dict | None) -> dict:
    row = row or {}
    return {"monthUsd": row.get("month_usd"), "last30Usd": row.get("last30_usd"), "keys": row.get("keys", 0),
            "partial": bool(row.get("partial")), "unread": list(row.get("unread") or [])}


def load(path: pathlib.Path) -> tuple[dict | None, list[dict]]:
    """(the scan, what is wrong with it). None when there is no scan with per-project figures."""
    if not path.is_file():
        return None, [{"source": "spend:scan", "reason": f"no spend scan at {path.name}: run "
                                                         "`project-observatory full scan-spend` (or wait for the tick)"}]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [{"source": "spend:scan", "reason": f"the spend scan would not read: {type(exc).__name__}"}]
    if not isinstance(doc, dict) or not isinstance(doc.get("projects"), dict):
        return None, [{"source": "spend:scan", "reason": "the spend scan predates per-project figures "
                                                         "(schema_version 1): run `project-observatory full scan-spend`"}]
    return doc, []


def freshness(doc: dict, now: datetime) -> tuple[bool, list[dict]]:
    try:
        scanned = datetime.fromisoformat(str(doc.get("scanned_at")))
        if scanned.tzinfo is None:
            scanned = scanned.replace(tzinfo=timezone.utc)
    except ValueError:
        return False, [{"source": "spend:stale", "reason": "the scan carries no readable time"}]
    age = (now - scanned).total_seconds()
    if doc.get("month") != now.strftime("%Y-%m") or age > FRESH_SECONDS:
        return False, [{"source": "spend:stale",
                        "reason": f"scanned {doc.get('scanned_at')}, {age / 3600:.0f} h ago (the bound is "
                                  f"{FRESH_SECONDS // 3600} h, and the month must be this one)"}]
    return True, []


def answer(doc: dict | None, problems: list[dict], project: str | None, now: datetime) -> tuple[int, dict]:
    head = {"month": None, "window": None, "scannedAt": None, "fresh": False}
    if doc is None:
        base = {**head, "degraded": problems}
        if project is not None:
            return 1, {"project": project, **_figures({"month_usd": None, "last30_usd": None}), **base}
        return 1, {"projects": {}, "unattributed": None, "totalUsd": None, **base}
    fresh, stale = freshness(doc, now)
    head = {"month": doc.get("month"), "window": doc.get("window"), "scannedAt": doc.get("scanned_at"),
            "fresh": fresh}
    degraded = [*(doc.get("degraded") or []), *stale]
    projects = doc.get("projects") or {}
    if project is None:
        un = doc.get("unattributed") or {}
        return 0, {**head, "projects": {k: _figures(v) for k, v in sorted(projects.items())},
                   "unattributed": {**_figures(un), "unledgeredMonthUsd": un.get("unledgered_month_usd"),
                                    "unledgeredLast30Usd": un.get("unledgered_last30_usd")},
                   "totalUsd": doc.get("total_usd"), "degraded": degraded}
    row = projects.get(project)
    out = {"project": project, **head, **_figures(row), "degraded": degraded}
    if row is None:
        if any(d.get("source") == "spend:openrouter:ledger" for d in degraded):
            out.update(monthUsd=None, last30Usd=None, partial=True,
                       note="the door's ledger could not be read, so no key could be attributed to any project")
        else:
            out.update(monthUsd=0.0, last30Usd=0.0,
                       note="no key in the door's ledger names this project; spend on keys the ledger does not "
                            "attribute is in `unattributed` and is never guessed into a project")
    return 0, out


def resolve(text: str) -> str:
    import vault_project
    r = vault_project.resolve(text)
    if r.how == "ambiguous" or not r.folder:
        raise LookupError(r.sentence())
    return r.folder


def show(code: int, ans: dict) -> None:
    if "project" in ans:
        m, l = ans.get("monthUsd"), ans.get("last30Usd")
        fmt = lambda v: "unknown (partial)" if v is None else f"${v:.2f}"   # noqa: E731
        print(f"{ans['project']}: {ans.get('month') or '?'} {fmt(m)}, last 30 days {fmt(l)}, "
              f"{ans.get('keys', 0)} key(s)" + ("" if ans.get("fresh") else " — scan not fresh"))
        for u in ans.get("unread") or []:
            print(f"  unread: {u.get('key')} ({u.get('reason')})")
        if ans.get("note"):
            print(f"  {ans['note']}")
    else:
        for name, row in (ans.get("projects") or {}).items():
            v = row.get("monthUsd")
            print(f"  {name:28s} {'unknown' if v is None else f'${v:.2f}':>12}  ({row.get('keys', 0)} key(s))")
        un = ans.get("unattributed") or {}
        v = un.get("monthUsd")
        if ans.get("unattributed") is not None:
            print(f"  {'(unattributed)':28s} {'unknown' if v is None else f'${v:.2f}':>12}")
    for d in ans.get("degraded") or []:
        print(f"  degraded: {d.get('source')}: {d.get('reason')}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("project", help="a project's month and last 30 days (every project without one)")
    p.add_argument("project", nargs="?", help="the project's vault folder, registry id or name")
    p.add_argument("--json", action="store_true")
    p.add_argument("--file", help="the spend scan to read (default: the workspace's store/raw/spend.json)")
    a = ap.parse_args(argv)
    project = None
    if a.project:
        try:
            project = resolve(a.project)
        except LookupError as exc:
            print(json.dumps({"project": a.project, "error": str(exc)}) if a.json else str(exc),
                  file=sys.stdout if a.json else sys.stderr)
            return 2
    doc, problems = load(pathlib.Path(a.file) if a.file else paths.SCRATCH / "spend.json")
    code, ans = answer(doc, problems, project, utcnow())
    if a.json:
        print(json.dumps(ans, ensure_ascii=False))
    else:
        show(code, ans)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
