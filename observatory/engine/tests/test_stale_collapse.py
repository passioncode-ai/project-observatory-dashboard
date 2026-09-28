#!/usr/bin/env python3
"""Fourteen rows that ask for nothing, spending fourteen of forty visible slots.

Measured 2026-09-07:

    clone.stale rows          14, every one of them `info`
    info rows on the board    37 — so this family is 38% of them
    the page shows            40 findings, and says how many it omits
    repos carrying sync=stale 14, all already chips on their project's own row

Each row's own detail says it: "the remote has moved and this checkout is
contained in the branch as of its last fetch, **so nothing here is at risk**".
Its action is "pull when you next work here" — which is not a decision. A
finding whose action asks for nothing is ambient state, and this ambient state
was already on the operator's page, per project, as a chip.

So fourteen no-risk rows were consuming fourteen of the forty slots the page can
show, pushing fourteen rows that DO ask for something into the "ещё N не
показаны" tail that iteration 85 made honest. The cap was working exactly as
designed and being spent on the wrong rows.

**One row, not none.** The aggregate is real information — fourteen checkouts
behind is ordinary, ninety would mean this machine had not fetched in weeks — so
the count stays as a single `info` row that names the projects it covers.

**Only `stale` collapses.** `clone.ahead` (7, warning), `clone.local-only-branch`
(6), `clone.diverged` (1) and `clone.unpushed-and-remote-moved` (2) all ask for a
decision about ONE repository — "push the branch, or delete it deliberately" —
and an operator acts on them one at a time. The discriminator is not the family
name, it is that stale's own text says nothing is at risk.

**No notification churn.** `tools/notify_findings.py` never notifies on `info`
(its own docstring: "`info` findings never notify"), so replacing fourteen info
ids with one is silent in that channel. The ack file keys by finding id, so the
old ids retire once and the new one appears once, which the tick reports as
resolved-and-new rather than hiding.
"""
from __future__ import annotations
import json, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()

FAILURES: list[str] = []


def plant_checkouts(states: list[dict]) -> None:
    """Give the synthetic workspace's first repositories these `local` states,
    then rebuild its page, so the estate-level cases below have a subject
    instead of waiting for a real estate to hold one."""
    import json, paths
    f = paths.REGISTRY / "repositories.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    for row, state in zip(doc["repositories"], states):
        row["local"].update(state)
    f.write_text(json.dumps(doc), encoding="utf-8")
    py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
    subprocess.run([py, "dashboard/build_dashboard.py"], cwd=ROOT, capture_output=True,
                   text=True, timeout=600, check=True)


plant_checkouts([{"sync": "stale"}, {"sync": "stale"},
                 {"sync": "ahead", "unpushed": 1, "unpushed_newest_on": "2026-01-02"},
                 {"sync": "local-only-branch", "nothing_exclusive": True}])


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rule(names):
    import build_findings as B
    fn = getattr(B, "stale_clones", None)
    if fn is None:
        return None
    return fn(list(names))


NAMES = [f"owner/repo-{i}" for i in range(14)]


# ─────────── one row, bounded, naming what it covers ───────────────────

def test_fourteen_become_one() -> None:
    """Fourteen bare names carry no ownership, so they land in one row. The live
    estate splits them into two — own checkouts and copies of other people's
    repositories — because those two were measured to mean different
    things; see `test_the_ownership_split_is_kept`."""
    out = rule(NAMES)
    if out is None:
        check("build_findings.stale_clones exists", False,
              "fourteen no-risk rows were spending fourteen visible slots")
        return
    check("fourteen stale checkouts are one row", len(out) == 1, str(len(out)))
    if not out:
        return
    f = out[0]
    check("and it stays info, because nothing is at risk",
          f["severity"] == "info", f["severity"])
    check("the count is in the title", "14" in f["title"], f["title"])
    check("the subject is the set, not one repository",
          ":" in f["subject"] and not f["subject"].startswith("repository:"),
          f["subject"])


def test_the_ownership_split_is_kept() -> None:
    """The distinction the first collapse destroyed. A copy of somebody else's
    repository falling behind upstream is expected; this estate's own checkout
    falling behind is a fetch nobody ran."""
    out = rule([("own/a", "owned"), ("own/b", "owned"),
                ("their/x", "external"), ("their/y", "external")])
    if out is None:
        return
    check("two meanings are two rows", len(out) == 2, str(len(out)))
    if len(out) != 2:
        return
    ext = next((f for f in out if f["subject"] == "estate:copies"), None)
    own = next((f for f in out if f["subject"] == "estate:clones"), None)
    check("the copies row exists", ext is not None, str([f["subject"] for f in out]))
    check("this estate's own row exists", own is not None, "")
    if ext:
        check("and the copies row says being behind is expected of a copy",
              "expected" in ext["detail"].lower(), ext["detail"][:200])
        check("naming only the copies", "own/a" not in ext["detail"],
              ext["detail"][:200])
    if own:
        check("while this estate's row does not call them copies",
              "copy" not in own["detail"].lower(), own["detail"][:200])


def test_the_names_are_bounded_and_the_remainder_stated() -> None:
    out = rule(NAMES)
    if out is None:
        return
    f = out[0]
    listed = sum(1 for n in NAMES if n in f["detail"])
    check("not every name is printed", listed < len(NAMES), f"{listed} of {len(NAMES)}")
    check("and the row says how many it did not name",
          "more" in f["detail"] or "ещё" in f["detail"],
          f["detail"][-200:])
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the cap has a name", "STALE_LISTED" in src,
          "a bare slice is a policy nobody can find")


def test_it_points_at_where_the_per_project_state_lives() -> None:
    out = rule(NAMES)
    if out is None:
        return
    f = out[0]
    blob = json.dumps(f, ensure_ascii=False).lower()
    check("the row says the page carries each one",
          "chip" in blob or "dashboard" in blob or "строк" in blob,
          "collapsing is only honest if the detail is still reachable: "
          + f["action"][:160])


def test_none_stale_is_no_row_at_all() -> None:
    out = rule([])
    if out is None:
        return
    check("an estate with nothing behind reports nothing", out == [], str(out))


def test_one_stale_is_still_one_row() -> None:
    """No special case for the small number: the row is the aggregate, and an
    aggregate of one is still the aggregate. A branch here would be a second
    shape for a reader to learn."""
    out = rule(["owner/only-one"])
    if out is None:
        return
    check("a single stale checkout is one row", len(out) == 1, str(out))
    if out:
        check("named in full, since it fits", "owner/only-one" in out[0]["detail"],
              out[0]["detail"][:160])


# ─────────── the live board lost the per-repository rows ───────────────

def test_the_estate_no_longer_carries_a_row_per_stale_repo() -> None:
    import build_findings as B
    rows = B.collect()
    stale = [f for f in rows if f["type"] == "clone.stale"]
    # AT MOST TWO, split by ownership. The first version of this assertion said
    # "at most one" and the collapse that satisfied it lost the ownership
    # distinction: for a copy of somebody else's repository, falling behind
    # upstream is expected rather than news, and `test-stale-expected` caught
    # the loss in the same iteration. Two rows, one per meaning — still 14
    # collapsed, not 14 rows.
    check("at most two clone.stale rows exist, one per meaning", len(stale) <= 2,
          f"{len(stale)} rows: {[f['subject'] for f in stale][:4]}")
    check("and their subjects are sets rather than repositories",
          all(f["subject"] in ("estate:clones", "estate:copies") for f in stale),
          str([f["subject"] for f in stale]))
    per_repo = [f for f in stale if f["subject"].startswith("repository:")]
    check("and none of them is keyed by a single repository", not per_repo,
          str([f["subject"] for f in per_repo][:4]))


def test_the_other_clone_families_stay_per_repository() -> None:
    """The boundary. These ask for a decision about one checkout, and an operator
    acts on them one at a time — collapsing them would be the same mistake in the
    other direction."""
    import build_findings as B
    rows = B.collect()
    for family in ("clone.ahead", "clone.local-only-branch"):
        got = [f for f in rows if f["type"] == family]
        if not got:
            print(f"  NOTE  no live {family} row; the boundary is asserted by the "
                  f"collapse tests above and by tests/test_clone_sync.py")
            continue
        check(f"{family} is still one row per repository",
              all(f["subject"].startswith("repository:") for f in got),
              str([f["subject"] for f in got][:3]))


def test_the_page_still_shows_each_stale_checkout() -> None:
    """Collapsing is only honest because the per-project state did not move. If
    the chips ever stop carrying it, this row becomes a summary of something
    invisible."""
    import paths
    page = paths.DASHBOARD_HTML
    if not page.is_file():
        print("  NOTE  no built page here; `./observatory.py dashboard` makes one "
              "[covered: the collapse cases above run on fixtures]")
        return
    html = page.read_text(encoding="utf-8")
    m = re.search(r'const D = (\{.*?\});\n', html, re.S)
    if not m:
        check("the page carries a payload", False, "")
        return
    payload = json.loads(m.group(1))
    chips = [r for row in payload["rows"] for r in row.get("repos", [])
             if r.get("sync") == "stale"]
    check("the payload still marks every stale checkout", len(chips) >= 1,
          f"{len(chips)} chip(s)")
    check("and the page's script renders that state",
          "stale" in html, "the chip map must know the value")


if __name__ == "__main__":
    print("stale collapse — fourteen rows that asked for nothing\n")
    for fn in (test_fourteen_become_one,
               test_the_ownership_split_is_kept,
               test_the_names_are_bounded_and_the_remainder_stated,
               test_it_points_at_where_the_per_project_state_lives,
               test_none_stale_is_no_row_at_all,
               test_one_stale_is_still_one_row,
               test_the_estate_no_longer_carries_a_row_per_stale_repo,
               test_the_other_clone_families_stay_per_repository,
               test_the_page_still_shows_each_stale_checkout):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe visible slots belong to rows that ask for something\033[0m")
