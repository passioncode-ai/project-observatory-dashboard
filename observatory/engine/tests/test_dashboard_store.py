#!/usr/bin/env python3
"""The page reads the store, and still builds without one.

The architecture said the dashboard "renders the registry **and the store**
into one HTML page". It read `registry/*.json` and nothing else, so tens of
thousands of events, hundreds of weekly rollups, the plugin measurements, the
wallet and the provider's health were invisible on the one screen that exists
to show them. The registry says what EXISTS; only the store says what HAPPENED.

The architecture made a second promise — when the agent hits its ceiling it
"degrades to collectors-only and **says so in the dashboard**" — and nothing
rendered that either, so the page could not say whether it was still watching.

Both halves are now built, and the third property matters as much as either: the
store is private workspace state, so a fresh installation has none. A dashboard
that could not be built without one would make the gate unrunnable there, so an
absent store is a DEGRADATION with a sentence, not a crash.

Runs over a private synthetic estate. PORTED-DIVERGED: the engine's interface
text is English in the code and translated from catalogs, so the assertions
name the English message ids the page carries rather than rendered Russian.
"""
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import live_estate                                                  # noqa: E402
import tmp as tmpdir  # noqa: E402
import paths                                                        # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def build_into(tmp: pathlib.Path, *, with_store: bool) -> tuple[int, str, pathlib.Path]:
    """Build the page with the registry copied aside and the store present or not."""
    reg = tmp / "registry"
    shutil.copytree(paths.REGISTRY, reg)
    docs = tmp / "docs"
    docs.mkdir()
    out = docs / "projects-dashboard.html"
    env = {**os.environ, "OBSERVATORY_REGISTRY": str(reg),
           "OBSERVATORY_DASHBOARD": str(out)}
    if not with_store:
        env["OBSERVATORY_DB"] = str(tmp / "absent.db")
    p = subprocess.run([PY, "dashboard/build_dashboard.py"], cwd=ROOT,
                       capture_output=True, text=True, timeout=300, env=env)
    return p.returncode, p.stdout + p.stderr, out


def test_the_built_page_carries_what_only_the_store_knows() -> None:
    # BUILDS ONE rather than skipping. This asked for a page somebody else had
    # made and dropped every assertion below when there was none — on a fresh
    # clone, or any run before the `dashboard` step. The property belongs to the
    # BUILDER, not to this machine's state, and `build_into` two functions up
    # already makes a page with the store present. So the workspace page is
    # preferred when it exists (it is what the operator reads) and built when it
    # does not, and the assertions run either way.
    page = paths.DASHBOARD_HTML
    if not page.exists():
        d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-dashstore-"))
        rc, said, page = build_into(d, with_store=True)
        check("no live page here, so one is built", rc == 0 and page.exists(),
              said[-200:])
        if not page.exists():
            return
    text = page.read_text(encoding="utf-8")
    for label in ("weekly snapshots", "plugin measurements", "agents' records not yet confirmed"):
        check(f"the health panel names {label!r}", label in text)
    check("rows carry a weekly series", '"weeks"' in text)
    # `"metrics"`, not `"disk"`. The first version carried a field named after
    # one plugin; the plugin suite caught the dashboard naming `disk.bytes` and
    # the row became a generic list rendered from the manifest's unit.
    check("and a generic list of plugin measurements", '"metrics"' in text)


def test_a_sparkline_never_stands_alone() -> None:
    """A shape with no figure is a claim a reader cannot check — the same rule
    the pack applies to a coloured chip carrying no word."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the sparkline is drawn", "function spark(" in src)
    # Labelled and kept on one line since A38: an unlabelled "881" wrapped as "88" over "1".
    check("and the total is printed beside it, with its unit",
          '<span class="spark-n">${T("{n} commits", {n: total})}</span>' in src,
          "a sparkline alone is decoration")
    check("it uses a token colour, not a literal", "currentColor" in src)
    # ASSERTED ON THE BUILT PAGE, not on the expression that builds it. This
    # read `title="${vals.length}` and broke the moment the title moved into a
    # variable to carry the session figure beside the commit one —
    # the shape was intact and the assertion was about how it was spelled. What
    # matters is that a rendered sparkline carries a title naming what it counts.
    check("the title is attached to the sparkline", 'class="spark" title=' in src,
          "a shape with no words is unreadable to a screen reader")
    check("and it names the weeks it covers", '"{weeks} wk, {commits} commits"' in src,
          "the count and its unit, not a bare number")
    check("the session figure joins it when there is one", '"{sessions} sessions' in src,
          "a week can hold work with no commit")


def test_the_page_builds_with_no_store_at_all() -> None:
    """A fresh clone has none: the store is gitignored."""
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-nostore-"))
    code, out, page = build_into(tmp, with_store=False)
    check("the build succeeds without a store", code == 0, out[-200:])
    text = page.read_text(encoding="utf-8")
    check("and the page says so rather than pretending",
          "no local store" in text, "an absent store must be visible")
    check("while the registry still renders", '"project:example-sample-0"' in text,
          "the synthetic estate's projects must still be on the page")


def test_the_health_panel_is_the_promise_architecture_made() -> None:
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the panel reads the wallet", "wallet.json" in src)
    check("and the provider's health", "provider-health.json" in src)
    check("and the review queue the operator owns", "state='proposed'" in src)
    check("the promise it fulfils is named where it is kept",
          "ARCHITECTURE.md:114" in src,
          "a comment that cannot be traced to the claim it answers rots quietly")


def test_the_builder_is_redirectable_like_everything_else() -> None:
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the output path is overridable so a test cannot clobber the real page",
          "OBSERVATORY_DASHBOARD" in (ROOT / "paths.py").read_text(encoding="utf-8"))
    check("it resolves the registry through paths", "INV = paths.REGISTRY" in src,
          "the third tool found with the path inlined, after the validator")
    check("and the output path too", "OUT = paths.DASHBOARD_HTML" in src)


def test_a_project_is_addressable() -> None:
    """S3: before this the row was the whole surface — nothing to link to."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the project name is a link to its own address",
          'const link = `#${E(r.id)}`;' in src and 'class="plink" href="${link}"' in src)
    check("a hash change opens that project", 'addEventListener("hashchange"' in src)
    check("and a pasted URL opens it on load", "fromHash();" in src,
          "hash routing that only works after a click is not addressable")
    check("Escape closes it", 'e.key === "Escape"' in src)

    page = paths.DASHBOARD_HTML
    if page.exists():
        text = page.read_text(encoding="utf-8")
        check("the built page carries the panel", 'id="panel"' in text)
        check("and a real project id to address", "#project:" in text or '"project:' in text)


def test_the_panel_shows_the_story_and_its_caveats() -> None:
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    for heading in ("What it is made of", "What happened",
                    "What the observatory concluded", "What the plugins measured"):
        check(f"the panel answers {heading!r}", heading in src)
    check("a conclusion shows its state, so `proposed` is never read as a fact",
          'E(labelOf(NOTE_STATE_LABEL, n.state))' in src and "proposed: T(" in src,
          "the agent proposes and never asserts — a reader must see that beside the sentence")
    check("and its confidence", "confidence" in src)
    check("store-derived sections say so when there is no store",
          "const gone = D.store_degraded" in src,
          "an empty box reads as 'nothing happened'")


def test_the_smoke_stub_models_what_the_page_uses() -> None:
    """A stub lacking what a browser gives reports a defect the page does not have."""
    src = (ROOT / "dashboard/smoke.js").read_text(encoding="utf-8")
    check("the stub provides a global addEventListener", "addEventListener() {}," in src)
    check("and a location with a hash", "const location = { hash:" in src)


def test_the_page_still_passes_its_own_two_gates() -> None:
    # The page is workspace state: a fresh installation has none until the
    # dashboard step builds one, and running the smoke against a file that is
    # not there measures nothing.
    if not live_estate.needs("a built page", paths.DASHBOARD_HTML.is_file(),
                             "`./observatory.py dashboard` builds it, and the "
                             "blank-page verdict is driven against fixtures in "
                             "tests/test_blank_page.py"):
        return
    p = subprocess.run([PY, "dashboard/audit_pack.py"], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    check("the design pack audit passes", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    if shutil.which("node") is None:
        print("  SKIP  node is not on PATH, so the page's smoke run cannot execute here")
        return
    p = subprocess.run(["node", "dashboard/smoke.js", str(paths.DASHBOARD_HTML)], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    check("the smoke run passes", p.returncode == 0, (p.stdout + p.stderr)[-200:])


if __name__ == "__main__":
    print("the dashboard — what only the store knows, and life without it\n")
    for fn in (test_the_built_page_carries_what_only_the_store_knows,
               test_a_project_is_addressable,
               test_the_panel_shows_the_story_and_its_caveats,
               test_the_smoke_stub_models_what_the_page_uses,
               test_a_sparkline_never_stands_alone,
               test_the_page_builds_with_no_store_at_all,
               test_the_health_panel_is_the_promise_architecture_made,
               test_the_builder_is_redirectable_like_everything_else,
               test_the_page_still_passes_its_own_two_gates):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe page shows what happened, and says when it cannot\033[0m")
