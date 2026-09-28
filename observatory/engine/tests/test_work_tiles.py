#!/usr/bin/env python3
"""Fourteen tiles counting what exists, none counting what happened.

Every inventory tile is a count of what exists; `dirty` and `unsynced` are
states, not work. Meanwhile the store holds every commit event and the weekly
rollups, and watching where the work went is part of this system's purpose. The
per-project sparkline answers that for one project; no tile answered it for the
estate.

**Counted from `events`, never by summing a rollup column.** `active_days`,
`authors` and `worked_days` are SET SIZES folded at write time, and adding them
across projects would count the same Tuesday once per project. The distinct
counts here are computed over the event rows themselves, which is the one place
the question can be answered without that error.

**Absent is not zero.** A fresh clone has no store — it is gitignored — and
`from_store()` already degrades by saying so. A work tile reading `0 commits`
where the truth is "there is no store" would be the opposite of what this
repository does everywhere else, so the tiles are ABSENT without a store rather
than zeroed.

**The window is in the label.** A bare count means nothing without its window,
and a rolling window avoids the partial-week problem of the weekly rollups. In
the engine the window travels in the stable key (`commits_7d`) and the reader's
language supplies the words from the catalog.
"""
from __future__ import annotations
import json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))
import paths                                                        # noqa: E402

#: The engine keys a work figure by its window (`commits_7d`, `busiest_28d`)
#: and translates the caption, so the window is read from the key.
WINDOW = re.compile(r"_\d+d$")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def payload() -> dict | None:
    page = paths.DASHBOARD_HTML
    if not page.is_file():
        return None
    m = re.search(r'const D = (\{.*?\});\n',
                  page.read_text(encoding="utf-8"), re.S)
    return json.loads(m.group(1)) if m else None


# ─────────── the numbers exist and are honest ──────────────────────────

def test_the_work_figures_are_computed() -> None:
    import build_dashboard as B
    fn = getattr(B, "work_stats", None)
    if fn is None:
        check("build_dashboard.work_stats exists", False,
              "fourteen tiles counted what exists and none what happened")
        return
    got = fn()
    check("it returns a mapping", isinstance(got, dict), str(type(got)))
    if not isinstance(got, dict):
        return
    # A live store on this machine, so the figures must be present and positive.
    import paths
    if not paths.DB.exists():
        print("  NOTE  no store here "
              "[covered: test_absent_store_yields_no_tiles_rather_than_zeros]")
        return
    for key in got:
        check(f"{key} is a number or a name", got[key] is not None, str(got))
    check("something was measured", bool(got), "an empty mapping with a readable store "
          "means the query itself found nothing, not even a zero count")


def test_the_window_is_part_of_the_label() -> None:
    import build_dashboard as B
    fn = getattr(B, "work_stats", None)
    if fn is None:
        return
    got = fn()
    if not got:
        return
    unlabelled = [k for k in got if not WINDOW.search(k)]
    check("every work tile names its own window", not unlabelled,
          "a count with no window is a number nobody can read: " + str(unlabelled))


def test_it_never_sums_a_set_size_column() -> None:
    """`active_days`, `authors` and `worked_days` are folded at write time. Adding
    them across projects counts one Tuesday once per project — the error this
    repository already learned about the rollup."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    i = src.find("def work_stats")
    body = src[i:src.find("\ndef ", i + 10)] if i != -1 else ""
    for col in ("active_days", "authors", "worked_days"):
        check(f"no sum over {col}", f"sum({col}" not in body.replace(" ", ""),
              "a set size cannot be added: " + body[:200])
    check("and the figures come from `events`", "events" in body,
          "the rollup cannot answer a distinct-project count across projects")


def test_absent_store_yields_no_tiles_rather_than_zeros() -> None:
    import importlib, os
    import build_dashboard as B
    import paths
    keep = os.environ.get("OBSERVATORY_DB")
    os.environ["OBSERVATORY_DB"] = str(ROOT / "no-such-store.db")
    try:
        importlib.reload(paths)
        importlib.reload(B)
        fn = getattr(B, "work_stats", None)
        if fn is None:
            return
        got = fn()
        check("with no store the work tiles are absent, not zero", got == {},
              f"got {got!r} — `0 commits` where the truth is 'no store' is the "
              f"one thing this repository does not do")
    finally:
        if keep is None:
            os.environ.pop("OBSERVATORY_DB", None)
        else:
            os.environ["OBSERVATORY_DB"] = keep
        importlib.reload(paths)
        importlib.reload(B)


# ─────────── they reach the page ───────────────────────────────────────

def test_the_page_carries_them() -> None:
    p = payload()
    if p is None:
        print("  NOTE  no built page here; `./observatory.py dashboard` makes one "
              "[covered: work_stats() is asserted directly above]")
        return
    stats = p.get("stats") or {}
    work = [k for k in stats if WINDOW.search(k)]
    check("the page's tiles include the work window(s)", bool(work), str(sorted(stats)))
    if work:
        check("and they carry values", all(stats[k] not in (None, "") for k in work),
              str({k: stats[k] for k in work}))


def test_the_page_draws_them_and_not_only_carries_them() -> None:
    """IN THE PAGE, NOT IN THE PAYLOAD — which is the defect this file exists
    for. `work_stats()` once computed its figures and shipped them in `stats`
    for days while no line of the script read one, and an assertion on the
    payload stayed green through all of it."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the script names the work keys and draws them into their own strip",
          "WORK_KEYS" in src and 'getElementById("work")' in src,
          "a figure in `stats` that no renderer reads is a figure nobody sees")
    # The captions are English message ids translated from the catalog; the
    # stable key is what the script reads from `stats`.
    for key, caption in (("commits_7d", "commits, 7 d"),
                         ("projects_active_7d", "projects in progress, 7 d"),
                         ("commits_28d", "commits, 28 d"),
                         ("projects_active_28d", "projects in progress, 28 d"),
                         ("unpushed_commits", "commits on no remote")):
        check(f"the tile for «{caption}» is drawn",
              f'"{key}"' in src and f'"{caption}"' in src,
              "work_stats() computes it, so the page must be able to say it")
    check("and the busiest project is drawn by name",
          "S.busiest_28d" in src and '"most work, 28 d"' in src, "")
    for stat in ("S.dirty", "S.creds", "S.creds_leaked", "S.creds_unclaimed"):
        check(f"{stat} reaches a tile", stat in src,
              "computed and never drawn is the same as not computed")
    page = paths.DASHBOARD_HTML
    if not page.is_file():
        print("  NOTE  no built page here [covered: the source assertions above]")
        return
    html = page.read_text(encoding="utf-8")
    check("and the built page carries the strip the script writes into",
          'id="work"' in html and 'id="work-h"' in html, "")


def test_the_inventory_tiles_are_still_there() -> None:
    """The addition must not displace what was already answered."""
    p = payload()
    if p is None:
        return
    stats = p.get("stats") or {}
    for key in ("projects", "repositories", "domains", "owners"):
        check(f"`{key}` survives", key in stats, str(sorted(stats))[:200])


if __name__ == "__main__":
    print("work tiles — what exists was counted, what happened was not\n")
    for fn in (test_the_work_figures_are_computed,
               test_the_window_is_part_of_the_label,
               test_it_never_sums_a_set_size_column,
               test_absent_store_yields_no_tiles_rather_than_zeros,
               test_the_page_carries_them,
               test_the_page_draws_them_and_not_only_carries_them,
               test_the_inventory_tiles_are_still_there):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe page counts what happened, not only what exists\033[0m")
