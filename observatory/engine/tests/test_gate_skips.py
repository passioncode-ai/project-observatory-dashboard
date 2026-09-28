#!/usr/bin/env python3
"""A skipped assertion is invisible twice: in the run, and in the source.

**In the run.** `_run_group()` is rigorous about a skipped STEP — it prints the
reason, and `expect_skipped` pins the set so a step that quietly becomes
machine-coupled cannot take CI's coverage with it. One level down there was
nothing: a suite drops nine assertions, prints `SKIP`, exits 0, and the gate
calls the step passed. Three full sweeps over identical code once reported three
different PASS totals, and two suites that skipped on concurrency (a lease held
by the tick) accounted for all of it. The total was partly a measure of luck,
and it had been quoted as evidence of progress.

**In the source.** Dozens of sites print `SKIP` or `NOTE`, and at first exactly
ONE named where the property is covered instead. A skip is legitimate when the
property is asserted elsewhere and a hole when it is not, and a reader could not
tell which they were looking at.

**Deliberately not red.** The lease skip is routine and frequent — the tick holds
`registry` on every run. A gate that goes red on a routine event is a gate whose
red is ignored, which costs more than the silence it replaced. The tally prints
always; the exit code is unchanged.

**And deliberately no receipt.** `check` is in `NON_MUTATING`, and its own rule
says nothing under `store/raw/` belongs to it: "no step of `check` produces a
collector report". A gate that wrote its own receipt would break the purity
verdict it exists to enforce.

The private original drove these through another suite whose NOTE depended on
the live volume's free space, so its assertions came and went with the disk.
Here the gate's real `run()`, `main()` and `_run_group()` are driven with
planted steps and a planted test tree, so every assertion is made every time.
"""
from __future__ import annotations
import pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []

#: A planted step whose output carries one skipped block, printed the way a
#: suite prints it. Planted rather than borrowed from another suite: a real
#: suite's skip depends on that suite's estate, which is how the original's
#: assertions went red without a line of code changing.
PLANTED_NOTE = "_planted_note"
NOTE_LINE = "  NOTE  no live host.disk_low here [covered: the planted case]"
PLANTED_STEPS = {
    PLANTED_NOTE: [PY, "-c", "print('  PASS  a planted assertion'); print(%r)" % NOTE_LINE],
    "_planted_quiet": [PY, "-c", "print('  PASS  a planted assertion')"],
}


def drive_main(step: str) -> subprocess.CompletedProcess:
    """`observatory.main()` for one planted step, in a subprocess."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('obs', %r)\n"
        "obs = importlib.util.module_from_spec(spec); spec.loader.exec_module(obs)\n"
        "obs.STEPS.update(%r)\n"
        "raise SystemExit(obs.main(['observatory.py', %r]))\n"
        % (str(ROOT / "observatory.py"), PLANTED_STEPS, step))
    return subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=600)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── run() reports what it saw ─────────────────────────────────

def test_run_returns_the_skips_it_streamed() -> None:
    import contextlib, io
    import observatory as obs
    obs.STEPS.update(PLANTED_STEPS)
    # run() streams the step's lines through this process's stdout; captured,
    # so the planted NOTE is not mistaken for one of this suite's own skips.
    with contextlib.redirect_stdout(io.StringIO()):
        got = obs.run(PLANTED_NOTE)
    check("run() returns a (code, skips) pair",
          isinstance(got, tuple) and len(got) == 2, repr(got)[:120])
    if not (isinstance(got, tuple) and len(got) == 2):
        return
    code, skips = got
    check("the step still succeeded", code == 0, str(code))
    check("and its skipped block was captured",
          isinstance(skips, list) and len(skips) == 1, repr(skips)[:200])
    if skips:
        check("verbatim, so the reason survives to the summary",
              "host.disk_low" in skips[0], skips[0][:120])
    with contextlib.redirect_stdout(io.StringIO()):
        code, skips = obs.run("_planted_quiet")
    check("a step with no marker returns no skips", code == 0 and skips == [], repr(skips))
    # STREAMING is asserted END-TO-END instead, in
    # `test_the_group_names_the_skipped_blocks`. `redirect_stdout` cannot see it:
    # a subprocess writes to file descriptor 1 directly and never touches this
    # process's `sys.stdout`, so a version that captured in-process got an empty
    # buffer while the child's PASS lines leaked into the report.


def test_the_marker_must_start_the_line() -> None:
    """A PASS line that merely contains the word is not a skip. Without the
    anchor, `PASS  a SKIP line is printed when node is absent` would count."""
    import observatory as obs
    rx = getattr(obs, "SKIP_MARKER", None)
    check("the marker is a named pattern", rx is not None,
          "an inline regex in the loop is a policy nobody can find")
    if rx is None:
        return
    for line, want in (("  SKIP  no zone snapshot", True),
                       ("  NOTE  node is absent", True),
                       ("  PASS  a SKIP line is printed when node is absent", False),
                       ("  FAIL  NOTE was not printed", False),
                       ("SKIPPED entirely", False)):
        got = bool(rx.match(line))
        check(f"{'counts' if want else 'does not count'}: {line.strip()[:52]}",
              got is want, f"matched={got}")


# ─────────── the group prints the tally ────────────────────────────────

def test_the_group_names_the_skipped_blocks() -> None:
    """DRIVEN through the real `main()`, because a claim about what the gate
    prints has to come from running it."""
    p = drive_main(PLANTED_NOTE)
    out = p.stdout + p.stderr
    check("the run is still green", p.returncode == 0, out[-200:])
    check("the summary counts the skipped assertion block",
          re.search(r"1 assertion block", out) is not None, out[-400:])
    check("and quotes its reason",
          any("host.disk_low" in l for l in out.splitlines() if "assertion block" not in l),
          out[-400:])
    check("it says 'assertion block', never 'step'",
          "1 step(s) skipped" not in out,
          "`expect_skipped` is the step level; conflating the two loses both")


def test_no_tally_when_nothing_skipped() -> None:
    p = drive_main("_planted_quiet")
    out = p.stdout + p.stderr
    check("a quiet planted step passes", p.returncode == 0, out[-200:])
    check("a run with no skipped block prints no tally",
          "assertion block" not in out, out[-300:])


def test_a_skipped_block_does_not_turn_the_gate_red() -> None:
    p = drive_main(PLANTED_NOTE)
    check("exit code is unchanged by a skip", p.returncode == 0,
          "the tick holds the lease on every run; a red from a routine "
          "event is a red that gets ignored")


# ─────────── the source side: does a skip name its cover ───────────────

PLANTED_SUITE = '''
def a():
    if True:
        print("  SKIP  no store on this machine")
        return
def b():
    print("  NOTE  no live row: the reclaimable wording is not driven here "
          "— see tests/test_delivery.py for the planted case")
    return
def c():
    print("  NOTE  measured now: three rows")
    check("an assertion that still runs", True)
'''


def test_the_measurer_exists_and_agrees_with_the_source() -> None:
    """Measured over a planted test tree, so the count is a fact about the
    fixture rather than about which suites a distribution happens to ship."""
    import skip_sites
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skipsites-"))
    (d / "test_planted.py").write_text(PLANTED_SUITE, encoding="utf-8")
    sites = skip_sites.survey(d)
    check("every skip site is found", len(sites) == 3, str(len(sites)))
    check("a tree outside the program is named by its own path",
          all(s["file"] == str(d / "test_planted.py") for s in sites),
          str({s["file"] for s in sites}))
    named = [s for s in sites if s["cover_kind"] != "none"]
    check("and the ones naming their cover are separated out",
          isinstance(named, list), str(type(named)))
    check("at least one site names where the property is covered instead",
          any(s["cover_kind"] == "named" for s in sites),
          str([(s["line"], s["cover_kind"]) for s in sites]))
    check("a multi-line message is read whole",
          any("test_delivery" in s["message"] for s in sites),
          "a version using `[^\"]*` reported none, because the one good message "
          "spans two string literals")
    kinds = {s["line"]: s["kind_of_site"] for s in sites}
    check("a NOTE followed by more assertions is an annotation, not a skip",
          sorted(kinds.values()) == ["annotation", "skip", "skip"], str(kinds))
    real = skip_sites.survey(ROOT / "tests")
    check("and the survey reads this distribution's own suites", len(real) >= 1, str(len(real)))


def test_the_cover_convention_is_a_marker_not_prose() -> None:
    """A bracketed `[covered: ...]` rather than a sentence a classifier reads.

    The convention first accepted only a path to another suite or the word
    "planted", and measuring a dozen newly written suites showed why
    that is too narrow: the commonest legitimate cover is elsewhere in the SAME
    file — the fixture cases that need no `node` — and no message could say so in
    a recognised way, so genuinely covered sites counted as uncovered.
    Widening it into a prose classifier was refused: "fixture" alone matches "no
    fixture here", which is the opposite claim.

    **Driven through `cover_kind`, which is the only classifier there is.** This
    case once asserted a regex the live classifier had stopped using, so the
    test was green about a declaration that governed nothing. It reads the
    function the tool actually calls.
    """
    import skip_sites
    for msg, want, why in (
            ("node is absent [covered: the payload cases above]", "named",
             "the marker"),
            ("no live row; see tests/test_delivery.py for the planted case", "named",
             "a path to another suite, still accepted"),
            ("no wiki here [uncoverable: nothing in this repository makes one]",
             "uncoverable", "an explicit nothing-carries-this"),
            ("no scan id [gap: a fixture store with one `scans` row would close it]",
             "gap", "a judged gap that names its remedy"),
            ("no store on this machine", "capability",
             "a fact about the machine, which no judgement moves"),
            ("no fixture here", "none", "a bare mention of a fixture is not a cover"),
            ("nothing to compare against", "none", "no claim at all"),
            ("covered somewhere probably", "none", "prose without the marker")):
        got = skip_sites.cover_kind(msg)
        check(f"classifies {why} as `{want}`", got == want, f"{got!r} for {msg!r}")


def test_the_finding_reports_the_uncovered_ones() -> None:
    import build_findings as B
    fn = getattr(B, "gate_skip_findings", None)
    if fn is None:
        check("build_findings.gate_skip_findings exists", False,
              "the measurement needs a reader or it is one with none")
        return
    out = fn([{"file": "tests/test_a.py", "kind": "SKIP", "message": "no data here",
               "line": 1, "kind_of_site": "skip", "cover_kind": "none"},
              {"file": "tests/test_b.py", "kind": "NOTE", "message": "see tests/test_c.py",
               "line": 2, "kind_of_site": "skip", "cover_kind": "named"}])
    check("one uncovered site is reported", len(out) == 1, str(len(out)))
    if out:
        check("as info, because this blocks nothing",
              out[0]["severity"] == "info", out[0]["severity"])
        check("and it counts both sides", "1" in out[0]["title"], out[0]["title"])
    check("all covered means nothing to report",
          fn([{"file": "x", "kind": "NOTE", "message": "see tests/y.py",
               "line": 3, "kind_of_site": "skip", "cover_kind": "named"}]) == [],
          "no row when there is no hole")



# ─────────── the third kind of not-run: never reached ──────────────────

def drive_group(steps: dict, order: list[str]) -> str:
    """`_run_group` with planted steps, in a subprocess.

    In-process capture leaks: the group prints through references the module
    holds and the steps it runs are subprocesses that inherit the real
    descriptors, so `redirect_stdout` sees only part of the run. The rest of
    this repository drives the gate as a subprocess for the same reason.
    """
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('obs', %r)\n"
        "obs = importlib.util.module_from_spec(spec); spec.loader.exec_module(obs)\n"
        "obs.STEPS = dict(obs.STEPS)\n"
        "obs.STEPS.update(%r)\n"
        "obs._run_group('planted', %r, None)\n" % (str(ROOT / "observatory.py"), steps, order))
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, timeout=600)
    return p.stdout + p.stderr


def test_the_group_says_how_many_steps_it_never_reached() -> None:
    """`_run_group` reported TWO kinds of not-run and had a third, larger and
    silent. A group stops at the first failing step — right for a pipeline,
    where running `emit` on a model `merge` could not build is worse than
    stopping — and then printed a PASS total for the steps it had reached as
    though it covered the group.

    Measured in real logs: `check` broke about halfway through and left dozens
    of steps unrun, each summary saying "1 failed" with no hint that a third of
    the suite never started. Worse in `all`, where a standing red sat early and
    sixteen steps had not run once. Every one was green when finally driven by
    hand, which is the point — the hole cost nothing THAT time.
    """
    said = drive_group({"boom": [PY, "-c", "raise SystemExit(1)"],
                        "never": [PY, "-c", "print('the fixture ran')"]},
                       ["boom", "never"])
    check("the step after the failure did not run",
          "the fixture ran" not in said,
          "the fixture proves nothing if the group carried on")
    check("the group says one step of two never ran",
          "1 of 2 step(s) never ran" in said, said[-400:])
    check("naming it", re.search(r"never ran.*\n.*never", said, re.S) is not None,
          said[-300:])
    check("and that the total above does not cover the group",
          "PASS total" in said, said[-300:])


def test_no_unreached_report_when_the_group_completes() -> None:
    said = drive_group({"fine": [PY, "-c", "print('  PASS  ok')"]}, ["fine"])
    check("a completed group says nothing about unreached steps",
          "never ran" not in said, said[-200:])


def test_a_standing_red_is_the_last_step_of_its_group() -> None:
    """`contract` fails whenever the published revision is behind, and only the
    operator can publish. In `check` it is the last step and costs no coverage;
    placed early in `all` it cost every step after it on every run. A step that is red by
    somebody else's decision belongs at the END of a fail-fast group.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("obs", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obs)
    for g in ("check", "all"):
        steps = obs.GROUPS[g]
        if "contract" not in steps:
            continue
        i = steps.index("contract")
        check(f"`{g}` puts contract last ({i + 1} of {len(steps)})",
              i == len(steps) - 1,
              f"{len(steps) - i - 1} step(s) never run while the publication is "
              f"behind, and only the operator can move it")



def test_a_verdict_without_the_lease_does_not_blame_the_group() -> None:
    """The three-outcomes rule, applied to the purity verdict itself.

    Without the registry lease another writer works inside the window — the
    tick, on its interval — so "the group wrote these files" is a claim the run
    has disproved about itself. Once measured: a tick took the lease first, the
    gate proceeded unprotected as its own warning says it will, and the verdict
    named the tick's receipts as the group's impurity. The report and the
    non-zero exit stay — erring toward reporting is the existing choice — and
    only the sentence changes.
    """
    import os, json as _json
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-attrib-"))
    scratch = d / "scratch"
    scratch.mkdir()
    (scratch / "planted.json").write_text('{"before": 1}', encoding="utf-8")
    # A step that changes a file under the scratch directory the group watches.
    planted = [PY, "-c",
               f"import pathlib; pathlib.Path({str(scratch / 'planted.json')!r})"
               f".write_text('{{\"after\": 2}}')"]
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('obs', %r)\n"
        "obs = importlib.util.module_from_spec(spec); spec.loader.exec_module(obs)\n"
        "obs.STEPS = dict(obs.STEPS); obs.STEPS['plant'] = %r\n"
        "sys.exit(obs._run_group('check', ['plant'], None, attributable=%s))\n")
    env = {**os.environ, "OBSERVATORY_SCRATCH": str(scratch)}
    out = {}
    for label, flag in (("attributable", "True"), ("unattributable", "False")):
        (scratch / "planted.json").write_text('{"before": 1}', encoding="utf-8")
        r = subprocess.run([PY, "-c", code % (str(ROOT / "observatory.py"), planted, flag)],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        out[label] = r.stdout + r.stderr
    check("the planted write is reported either way",
          "planted.json" in out["attributable"] and "planted.json" in out["unattributable"],
          out["unattributable"][-300:])
    check("with the lease, the group is named as the writer",
          "group wrote artefacts" in out["attributable"], out["attributable"][-300:])
    check("without it, the verdict says it cannot attribute them",
          "not\nattributable" in out["unattributable"].replace(" ", "\n") or
          "attributable to the group" in out["unattributable"],
          out["unattributable"][-300:])
    check("and it says why it is reported anyway",
          "would look exactly like this" in out["unattributable"],
          out["unattributable"][-300:])


if __name__ == "__main__":
    print("gate skips — a dropped assertion, visible at last\n")
    for fn in (test_run_returns_the_skips_it_streamed,
               test_the_marker_must_start_the_line,
               test_the_group_names_the_skipped_blocks,
               test_no_tally_when_nothing_skipped,
               test_a_skipped_block_does_not_turn_the_gate_red,
               test_the_measurer_exists_and_agrees_with_the_source,
               test_the_cover_convention_is_a_marker_not_prose,
               test_the_finding_reports_the_uncovered_ones,
               test_the_group_says_how_many_steps_it_never_reached,
               test_no_unreached_report_when_the_group_completes,
               test_a_standing_red_is_the_last_step_of_its_group,
               test_a_verdict_without_the_lease_does_not_blame_the_group):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma skipped assertion block is counted, quoted, and never "
          "mistaken for a passed one\033[0m")
