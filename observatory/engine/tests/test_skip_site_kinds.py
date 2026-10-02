#!/usr/bin/env python3
"""Not every `NOTE` is a skipped block, and the board was counting them all.

`tools/skip_sites.py` reported 46 of 67 sites as not saying where their property
is covered, and `gate.skips_uncovered` put that number on the operator's board.
Reading `tests/test_filesystem_scan.py:368` while acting on it showed the flaw:

    if promoted:
        print(f"  NOTE  {len(promoted)} local-only project(s) became repositories …")
        lost_p = [k for k in lost_p if k not in promoted]
    …
    check("no project is lost", not lost_p, str(lost_p[:4]))

Nothing is skipped there. The note ANNOTATES a run whose assertions follow
immediately — one of them even says "nothing in the set comparison above was
lost". Three of that file's six counted sites are of this kind.

**The discriminator is structure, not wording.** A skip site is one where the
block is ABANDONED: a `return` or `continue` follows the print inside the same
block. An annotation has no such exit and the assertions run. That is exact, and
Python's own AST answers it without guessing indentation — which matters because
these prints span two and three lines.

**Why it is worth the correction rather than a shrug.** The count is a number on
a surface an operator reads, and it was measuring "markers" while claiming to
measure "places assertions can vanish". The third instrument of my own to be
corrected this session, and the only one whose figure had reached the board.
"""
from __future__ import annotations
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tools"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


ABANDONS = '''
def t():
    if cond:
        print("  SKIP  nothing to compare here")
        return
    check("it holds", True)
'''

ANNOTATES = '''
def t():
    if cond:
        print("  NOTE  the estate gained 3 projects; the world moving, not a defect")
        cond = False
    check("nothing is lost", True)
'''

CONTINUES = '''
def t():
    for x in y:
        if not x:
            print("  SKIP  no model for this one")
            continue
        check("it holds", True)
'''

MULTILINE = '''
def t():
    if cond:
        print("  NOTE  node is absent; the rendered assertion cannot run here "
              "and nothing else can execute the page")
        return
    check("it holds", True)
'''


def kinds(src: str) -> list[str]:
    import skip_sites
    fn = getattr(skip_sites, "site_kinds", None)
    if fn is None:
        return []
    return [s["kind_of_site"] for s in fn(src)]


# ─────────── the discriminator ─────────────────────────────────────────

def test_a_return_after_the_print_is_a_skip() -> None:
    got = kinds(ABANDONS)
    if not got:
        check("skip_sites.site_kinds exists", False,
              "the board counted annotations as skipped blocks")
        return
    check("a print followed by return abandons the block", got == ["skip"], str(got))


def test_a_continue_after_the_print_is_a_skip() -> None:
    got = kinds(CONTINUES)
    if not got:
        return
    check("a print followed by continue abandons the iteration", got == ["skip"], str(got))


def test_a_note_whose_assertions_follow_is_an_annotation() -> None:
    got = kinds(ANNOTATES)
    if not got:
        return
    check("a note with no exit annotates a run", got == ["annotation"], str(got))


def test_a_multi_line_print_is_read_whole() -> None:
    """These prints span two and three lines, which is why indentation guessing
    was never going to answer this and the AST is."""
    got = kinds(MULTILINE)
    if not got:
        return
    check("a two-line print followed by return is still a skip", got == ["skip"], str(got))


# ─────────── the live figures ──────────────────────────────────────────

def test_a_skip_is_classified_by_what_it_waits_for() -> None:
    """Four states in one field, and only the last is a number a judgement moves.

    Measured over 32 unexamined sites: 14 named a MACHINE FACT — no store, node,
    `sqlite-vec`, an embedding key — which nothing a person writes supplies, and
    18 named absent DATA or an unbuilt artefact, which is exactly what a fixture
    plants."""
    import skip_sites
    fn = getattr(skip_sites, "cover_kind", None)
    if fn is None:
        check("skip_sites.cover_kind exists", False,
              "the board counted machine facts as unexamined judgements")
        return
    for msg, want in (
            ("node is absent [covered: the payload cases above]", "named"),
            ("see tests/test_delivery.py for the planted case", "named"),
            ("executing needs node [uncoverable: both executors are node]", "uncoverable"),
            ("no store on this machine", "capability"),
            ("sqlite-vec is not loadable here", "capability"),
            ("jsonschema is not installed here", "capability"),
            ("no embedding key here", "capability"),
            ("this SQLite cannot DROP COLUMN", "capability"),
            ("no zone snapshot in this registry", "none"),
            ("the page has not been built here", "none"),
            ("no GitHub listings on this machine", "none"),
            ("no scan id [gap: a fixture store with one `scans` row closes it]",
             "gap")):
        got = fn(msg)
        check(f"{want:12} {msg[:48]}", got == want, f"got {got!r}")


def test_a_capability_is_not_a_judgement_the_board_asks_for() -> None:
    """The classifier's classes partition the skips, and only one of them
    reaches the board.

    **This case once demanded that unexamined sites EXIST on this estate**, so
    it went red the moment the last seventeen were judged — a
    test that fails when the work it measures is finished. And its second
    assertion filtered `caps` for `cover_kind == "none"`, which is empty by
    construction: a tautology sitting beside an assertion that punished
    progress. What is actually true whatever the population is: every skip
    carries exactly one declared class, and the number the board reports is the
    `none` count and nothing else.
    """
    import skip_sites
    import build_findings as B
    sites = skip_sites.survey()
    skips = [s for s in sites if s["kind_of_site"] == "skip"]
    classes = {"named", "uncoverable", "capability", "gap", "none"}
    stray = sorted({s["cover_kind"] for s in skips} - classes)
    check("every skip carries a declared class", not stray, str(stray))
    buckets = {c: [s for s in skips if s["cover_kind"] == c] for c in classes}
    check("and the classes partition them",
          sum(len(v) for v in buckets.values()) == len(skips),
          f"{ {c: len(v) for c, v in buckets.items()} } vs {len(skips)}")
    rows = B.gate_skip_findings(sites)
    if not buckets["none"] and not buckets["gap"]:
        check("with nothing unexamined and no gap, the board says nothing",
              rows == [], str(rows)[:200])
    else:
        # THE COUNT THAT LEADS depends on which state the estate is in, and the
        # rule has three: unexamined sites lead when there are any, the judged
        # gap leads when there are none. Asserting only the first shape made this
        # case red the moment the unexamined count reached zero — twice in one
        # iteration, the same mistake in two places.
        leads = len(buckets["none"]) if buckets["none"] else len(buckets["gap"])
        check("the board's headline counts the class that leads",
              bool(rows) and f"{leads} of {len(skips)}" in rows[0]["title"],
              rows[0]["title"] if rows else "no row")
    print(f"  NOTE  measured now: {len(buckets['capability'])} waiting on a "
          f"capability, {len(buckets['named'])} covered, "
          f"{len(buckets['uncoverable'])} uncoverable, {len(buckets['gap'])} judged "
          f"gap(s), {len(buckets['none'])} unexamined "
          f"[covered: the fixture table above drives every class]")


def test_the_survey_separates_the_two() -> None:
    import skip_sites
    sites = skip_sites.survey(ROOT / "tests")
    have = {s.get("kind_of_site") for s in sites}
    check("every site carries its kind", have <= {"skip", "annotation"} and have,
          str(sorted(have)))
    skips = [s for s in sites if s.get("kind_of_site") == "skip"]
    notes = [s for s in sites if s.get("kind_of_site") == "annotation"]
    check("both kinds exist in this repository", skips and notes,
          f"{len(skips)} skip(s), {len(notes)} annotation(s)")
    # THE MEASUREMENT, not a stored number: the split moves as suites are
    # written, and asserting today's figure would fail on the next one.
    print(f"  NOTE  measured now: {len(skips)} skip(s), {len(notes)} annotation(s) "
          f"[covered: the four fixture cases above define the discriminator]")


#: The shape of the suite that exposed the flaw, planted: three notes that
#: annotate a block whose assertions still run, and three guards that abandon
#: theirs.
EXPOSING_SUITE = '''
def a(model):
    if model is None:
        print("  SKIP  no model here [covered: planted]")
        return
    print("  NOTE  the estate moved since the snapshot")
    check("nothing in the set comparison above was lost", True)
def b(model):
    if model is None:
        print("  SKIP  no model to compare against")
        return
    print("  NOTE  measured now: two folders")
    check("an assertion that runs", True)
def c(model):
    for row in model or []:
        if not row:
            print("  SKIP  no model row")
            continue
    print("  NOTE  a third annotation")
    check("still asserted", True)
'''


def test_the_named_file_is_read_correctly() -> None:
    """The site that exposed the flaw: a suite whose notes annotate while its
    other markers abandon their block. The original read that suite by name;
    its shape is planted here so the case does not depend on which suites a
    distribution ships."""
    import skip_sites
    import tmp as tmpdir
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skipkinds-"))
    (d / "test_exposing.py").write_text(EXPOSING_SUITE, encoding="utf-8")
    sites = skip_sites.survey(d)
    ann = [s for s in sites if s.get("kind_of_site") == "annotation"]
    check("its estate-moved notes are annotations", len(ann) >= 3,
          str([(s["line"], s.get("kind_of_site")) for s in sites]))
    check("and its 'no model' guards are skips",
          any(s.get("kind_of_site") == "skip" and "no model" in s["message"]
              for s in sites),
          str([(s["line"], s["message"][:30]) for s in sites]))
    check("all three guards, including the one that uses `continue`",
          sum(1 for s in sites if s.get("kind_of_site") == "skip") == 3,
          str([(s["line"], s.get("kind_of_site")) for s in sites]))


def test_an_installed_engine_does_not_report_its_own_test_suite() -> None:
    """gate.skips_uncovered is about the engine's own suites — a maintainer's
    finding. An installed engine (no repository around it) leaves it off the
    user's board; a source checkout keeps it."""
    import build_findings as B
    os.environ["OBSERVATORY_MAINTAINER"] = "0"
    try:
        rows = [f for f in B.collect() if f["type"] == "gate.skips_uncovered"]
    finally:
        os.environ.pop("OBSERVATORY_MAINTAINER", None)
    check("an installed engine: no gate.skips_uncovered", not rows, str(rows)[:200])
    check("a wheel's site-packages has no repository beside it",
          B.maintaining_the_engine(pathlib.Path("/srv/example-env/lib/python3/site-packages/observatory/engine")) is False)


def test_the_finding_counts_only_what_a_judgement_could_fix() -> None:
    import build_findings as B
    os.environ["OBSERVATORY_MAINTAINER"] = "1"
    try:
        rows = [f for f in B.collect() if f["type"] == "gate.skips_uncovered"]
    finally:
        os.environ.pop("OBSERVATORY_MAINTAINER", None)
    if not rows:
        print("  NOTE  every skip site names its cover "
              "[covered: the survey cases above]")
        return
    f = rows[0]
    import skip_sites
    sites = skip_sites.survey()
    skips = [s for s in sites if s.get("kind_of_site") == "skip"]
    # BY `cover_kind`, the field the finding uses. This computed with
    # `names_cover` and expected 32 while the row said 18 — the two fields
    # disagree by the fourteen sites waiting on a machine capability, which
    # is precisely why the boolean was retired.
    uncovered = [s for s in skips if s["cover_kind"] == "none"]
    gaps = [s for s in skips if s["cover_kind"] == "gap"]
    # THE CLASS THAT LEADS, as the builder decides it: unexamined sites when
    # there are any, the judged gaps when there are none. A version that
    # expected only the first shape went red when the unexamined count reached
    # zero while a judged gap remained.
    leads = uncovered if uncovered else gaps
    check("the finding counts skips, not annotations",
          f"{len(leads)} of {len(skips)}" in f["title"],
          f"title says {f['title']!r}; {len(uncovered)} uncovered and {len(gaps)} "
          f"judged skip(s) of {len(skips)}, and {len(sites) - len(skips)} "
          f"annotation(s) exist")


if __name__ == "__main__":
    print("skip sites — a note beside a run is not a skipped block\n")
    for fn in (test_a_return_after_the_print_is_a_skip,
               test_a_continue_after_the_print_is_a_skip,
               test_a_note_whose_assertions_follow_is_an_annotation,
               test_a_multi_line_print_is_read_whole,
               test_a_skip_is_classified_by_what_it_waits_for,
               test_a_capability_is_not_a_judgement_the_board_asks_for,
               test_the_survey_separates_the_two,
               test_the_named_file_is_read_correctly,
               test_the_finding_counts_only_what_a_judgement_could_fix,
               test_an_installed_engine_does_not_report_its_own_test_suite):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe board counts places assertions can vanish, not markers\033[0m")
