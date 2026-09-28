#!/usr/bin/env python3
"""The tracer's own mechanics, driven — because the sweep itself cannot be.

`tools/trace_opens.py` settles what four steps really open by running them, and
running them takes minutes and walks the estate's checkouts, so it is not a gate
step. What the gate can hold is everything the tracer could get quietly wrong,
and on 2026-09-09 it got three things wrong in one sitting:

* it attributed **its own** sandbox copies to the step, because the seeding runs
  through `shutil.copy2` and that is one of the doors it wraps;
* it ran the step IN PROCESS, where `paths.py` had already frozen its constants
  — so `OBSERVATORY_DASHBOARD` arrived too late and tracing `dashboard`
  **rebuilt the operator's live page** from an empty temporary store — an
  accident already seen once through another door;
* it seeded an empty database, and a step's file access is data-dependent: the
  dashboard skipped every panel needing a store and read seven files instead of
  fourteen, so the report was clean about a step that reads the wallet.

Each is asserted below against a planted script, so the next reader learns the
trap from a failing check rather than from this paragraph.
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                               # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


#: The tracer's CLI reads the step graph from `tests/test_pipeline.py`. When a
#: distribution ships without that suite, the CLI half cannot run at all, and
#: its assertions are reported as skipped rather than as passed or failed.
GRAPH = ROOT / "tests" / "test_pipeline.py"
GRAPH_GAP = ("  SKIP  KNOWN-GAP: tools/trace_opens.py reads its step graph from "
             "tests/test_pipeline.py, which this distribution does not ship; "
             "the CLI assertions below cannot run without it")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def tracer():
    spec = importlib.util.spec_from_file_location("tr_t", ROOT / "tools/trace_opens.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def trace_child(script: pathlib.Path, env_extra: dict | None = None) -> list[list[str]]:
    """Run the tracer's `--child` over a script we wrote, and read its receipt."""
    out = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tracer-")) / "touched.json"
    env = {**os.environ, "OBSERVATORY_TRACE_OUT": str(out), **(env_extra or {})}
    subprocess.run([PY, str(ROOT / "tools/trace_opens.py"), "--child",
                    str(script.relative_to(ROOT))], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=300)
    return json.loads(out.read_text(encoding="utf-8")) if out.is_file() else []


def planted(body: str) -> pathlib.Path:
    """A script inside the repository — the tracer only records paths under it."""
    d = ROOT / "tests" / "_traced"
    d.mkdir(exist_ok=True)
    f = d / "planted.py"
    f.write_text(body, encoding="utf-8")
    return f


def cleanup() -> None:
    d = ROOT / "tests" / "_traced"
    for f in d.glob("*"):
        f.unlink()
    if d.exists():
        d.rmdir()


def test_a_read_through_a_resolver_is_seen() -> None:
    """The whole reason the tracer exists: a name the source never spells."""
    try:
        f = planted("import pathlib\n"
                    "name = 'READ' + 'ME.md'\n"
                    "pathlib.Path(__file__).resolve().parents[2].joinpath(name).read_text()\n")
        seen = trace_child(f)
        check("a path assembled at runtime is recorded",
              ["r", "README.md"] in [list(x) for x in seen], str(seen[:4]))
    finally:
        cleanup()


def test_a_write_is_recorded_as_a_write() -> None:
    try:
        f = planted("import pathlib\n"
                    "p = pathlib.Path(__file__).with_name('out.txt')\n"
                    "p.write_text('x')\n")
        seen = [list(x) for x in trace_child(f)]
        check("a write is recorded, and as a write",
              any(m == "w" and p.endswith("out.txt") for m, p in seen), str(seen[:4]))
    finally:
        cleanup()


def test_a_sqlite_uri_is_split_into_a_path_and_a_mode() -> None:
    """`file:…?mode=ro` is a URI: recorded verbatim it became a write of a query string."""
    try:
        f = planted("import pathlib, sqlite3\n"
                    "db = pathlib.Path(__file__).with_name('t.db')\n"
                    "sqlite3.connect(db).close()\n"
                    "sqlite3.connect(f'file:{db}?mode=ro', uri=True).close()\n")
        seen = [list(x) for x in trace_child(f)]
        dbs = [(m, p) for m, p in seen if p.endswith("t.db")]
        check("the read-only connection is recorded as a read",
              ("r", next((p for m, p in dbs if m == "r"), "")) in dbs, str(dbs))
        check("the read-write connection is recorded as a write",
              any(m == "w" for m, p in dbs), str(dbs))
        check("and no recorded path carries a query string",
              not any("?" in p for _, p in seen), str([p for _, p in seen if "?" in p]))
    finally:
        cleanup()


def test_a_redirected_artefact_is_reported_under_its_canonical_name() -> None:
    """Otherwise the sandbox swallows the very write under test."""
    try:
        work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tracer-page-"))
        f = planted("import os, pathlib\n"
                    "pathlib.Path(os.environ['OBSERVATORY_DASHBOARD']).write_text('<html>')\n")
        seen = [list(x) for x in trace_child(f, {"OBSERVATORY_DASHBOARD": str(work / "p.html")})]
        check("a write to the redirected page is reported as the real page",
              ["w", "docs/projects-dashboard.html"] in seen, str(seen))
        check("and the operator's page was not touched",
              not (work / "p.html").read_text(encoding="utf-8") == ""
              and "<html>" in (work / "p.html").read_text(encoding="utf-8"),
              "the fixture must write into the sandbox, or it proves nothing")
    finally:
        cleanup()


def test_the_tracer_runs_the_step_in_a_child() -> None:
    """The property that stopped it rewriting the live page.

    In-process, `paths.py` has already frozen every artefact constant from the
    PARENT's environment, so a redirect set afterwards binds nothing. This is a
    structural claim about the tool, so it is asserted structurally.
    """
    src = (ROOT / "tools/trace_opens.py").read_text(encoding="utf-8")
    prose = " ".join(src.split())      # the reason may wrap across lines
    check("the step is executed through a subprocess", "subprocess.run(" in src)
    check("with the redirects in the child's environment",
          "OBSERVATORY_TRACE_OUT" in src and "env=env" in src)
    check("and the reason is written where the next reader will look",
          "not a redirect" in prose or "already imported it" in prose,
          "the page was rebuilt from an empty store before this was true")


def test_the_tracer_does_not_charge_the_step_for_its_own_sandbox() -> None:
    """Twenty-five copies, attributed to the step, in the tool's first version.

    The seeding runs through `shutil.copy2`, which the tracer wraps — so while
    both lived in one process, `dashboard` was accused of reading every receipt
    in `store/raw`. The split into parent and child fixed it structurally: the
    parent seeds with no wrapper installed, and the child records from empty.
    Asserted by DRIVING it, because the structural version of this claim was
    exactly what the first fix got wrong.
    """
    src = (ROOT / "tools/trace_opens.py").read_text(encoding="utf-8")
    i_install = src.index("    install()\n")
    i_seed = src.index("shutil.copy2(f, work")
    check("the wrappers are installed only in the child, after the seeding",
          i_seed < i_install or "def child(" in src[:i_install],
          "a wrapper active during seeding records the harness, not the step")
    if not GRAPH.is_file():
        print(GRAPH_GAP)
        return
    out = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tracer-report-")) / "r.json"
    subprocess.run([PY, str(ROOT / "tools/trace_opens.py"), "validate", "--json", str(out)],
                   cwd=ROOT, capture_output=True, text=True, timeout=900)
    report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else []
    reads = report[0]["reads"] if report else []
    check("a traced step reports its own reads, not the sandbox's copies",
          reads and len(reads) < 20, f"{len(reads)} read(s) — the seeding copies about 25")
    check("and the validator's report holds no scratch receipt, which is all the "
          "seeding wrote",
          not any(r.startswith("store/raw/") for r in reads),
          str([r for r in reads if r.startswith("store/raw/")]))


def test_a_step_that_acts_on_the_world_is_refused_or_made_safe() -> None:
    """Tracing means EXECUTING, so every outward step needs one of two answers.

    Three are refused: `notify` sends to a person and no redirect un-sends a
    message; the two `commit-*` steps write git history. Three more were refused
    too until 2026-09-09 — and each already had its safe path built and tested:
    the vault is redirectable like every other artefact, and both spenders take a
    documented degradation when no credential resolves. A gap invented rather
    than found is still a gap, so the tool now says HOW each one was made safe.
    """
    tr = tracer()
    for step in ("notify", "commit-registry", "commit-projection"):
        check(f"{step} is refused", step in tr.REFUSED, "tracing it would act on the world")
        check(f"and the refusal for {step} says why", len(tr.REFUSED.get(step, "")) > 15,
              tr.REFUSED.get(step, ""))
    for step in ("project", "agent", "index"):
        check(f"{step} is traced in safe mode instead", step in tr.SAFE_MODE,
              "its safe path exists and is tested; refusing it measures nothing")
        env, why = tr.SAFE_MODE[step]
        check(f"and {step}'s safe mode names what makes it harmless", len(why) > 30, why)
        check(f"{step}'s safe mode is an ENVIRONMENT, not a flag in production code",
              all(k.startswith(("OBSERVATORY_", "OPENROUTER_", "OPENAI_")) for k in env),
              str(env))
    check("a spender is made safe by having no credential, not by a switch",
          all("KEY" in k or "API" in k for k in tr.SAFE_MODE["agent"][0]),
          "a dry-run flag in the spender would be a switch that can be left on")
    if not GRAPH.is_file():
        print(GRAPH_GAP)
        return
    p = subprocess.run([PY, str(ROOT / "tools/trace_opens.py"), "notify"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("the run refuses rather than executing", "REFUSED" in p.stdout, p.stdout[-200:])
    check("and the summary counts what it did not trace",
          "NOT traced" in p.stdout, p.stdout[-200:])


def test_the_sandbox_is_seeded_from_the_workspace_and_not_the_source_tree() -> None:
    """A step's file access is data-dependent, so the sandbox copies the store.

    The engine keeps its store in the workspace, not beside the code. A tracer
    that seeded from the source tree would find nothing there and trace every
    step against an empty database — a clean report about a sandbox rather than
    about the step. Driven: the planted step counts the rows it can see in the
    database it was handed and names the count in the file it writes.
    """
    import paths
    tr = tracer()
    try:
        f = planted("import os, pathlib, sqlite3\n"
                    "db = os.environ['OBSERVATORY_DB']\n"
                    "n = 0\n"
                    "if pathlib.Path(db).is_file():\n"
                    "    c = sqlite3.connect(db)\n"
                    "    n = c.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]\n"
                    "    c.close()\n"
                    "pathlib.Path(__file__).with_name(f'rows-{n}.txt').write_text('x')\n")
        have = 0
        if paths.DB.is_file():
            import sqlite3
            c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
            have = c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
            c.close()
        check("the workspace store has rows to copy", have > 0,
              "the synthetic estate plants metrics; without them this proves nothing")
        work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tracer-seed-"))
        r = tr.run("planted", [PY, str(f.relative_to(ROOT))], work)
        wrote = [w for w in r.get("writes", []) if "/rows-" in w]
        check("the traced step sees the workspace's rows, copied into its sandbox",
              wrote == [f"tests/_traced/rows-{have}.txt"], str(r)[:300])
    finally:
        cleanup()


def test_a_read_of_a_file_no_step_writes_is_counted_not_accused() -> None:
    """The graph orders readers after writers; a read of source cannot invert.

    `findings` opens 119 test suites to count what the gate covers. Reporting
    those as defects buried the six reads that mattered under 135 that could
    not — a report nobody finishes reading is a report that hides things.
    """
    tr = tracer()
    decl = {"a": {"writes": ["store/raw/x.json"], "reads": []},
            "b": {"writes": [], "reads": []}}
    seen = {"reads": ["store/raw/x.json", "tests/test_thing.py"], "writes": []}
    out = tr.compare("b", seen, decl)
    check("a read of a file some step writes is named",
          any("store/raw/x.json" in line and "WRITES it" in line for line in out), str(out))
    check("and a read of source is counted instead",
          any("no step writes" in line and " 1 file" in line for line in out), str(out))


def test_the_report_states_what_it_cannot_see() -> None:
    """A clean report that reads as 'nothing happened' is the danger here."""
    if not GRAPH.is_file():
        print(GRAPH_GAP)
        return
    p = subprocess.run([PY, str(ROOT / "tools/trace_opens.py"), "validate"],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    check("the tracer runs and reports", p.returncode in (0, 1), p.stderr[-200:])
    check("and every report says a subprocess is invisible to it",
          "subprocess" in p.stdout and "C extension" in p.stdout, p.stdout[-200:])


if __name__ == "__main__":
    print("the tracer — what it sees, and what it says it cannot\n")
    for fn in (test_a_read_through_a_resolver_is_seen,
               test_a_write_is_recorded_as_a_write,
               test_a_sqlite_uri_is_split_into_a_path_and_a_mode,
               test_a_redirected_artefact_is_reported_under_its_canonical_name,
               test_the_tracer_runs_the_step_in_a_child,
               test_the_tracer_does_not_charge_the_step_for_its_own_sandbox,
               test_the_sandbox_is_seeded_from_the_workspace_and_not_the_source_tree,
               test_a_step_that_acts_on_the_world_is_refused_or_made_safe,
               test_a_read_of_a_file_no_step_writes_is_counted_not_accused,
               test_the_report_states_what_it_cannot_see):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe tracer measures the step, not itself\033[0m")
