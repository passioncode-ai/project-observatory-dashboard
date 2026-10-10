#!/usr/bin/env python3
"""The tick caught a blank page and told only a log file.

One morning the tick log held exactly one line of this kind:

    2026-09-08T03:00:54Z dashboard SMOKE FAILED — the page would render blank

A change had defined a helper inside one function while the chip assembly that
used it lived in another, so the page's script died at load. It was fixed
minutes later and the page has been clean since.

**So the machinery worked and its report sat unread for half an hour.** The tick
runs `dashboard/smoke.js` and, on failure, logs one line and carries on: no
receipt, no finding, no failed step. The one failure mode that makes the
operator's primary surface useless is reported only where nobody looks — and the
comment above that line already records this class shipping once before with
every static check passing, which is why smoke exists at all. The tick
learned to RUN it and not to REPORT it.

**Absent is not clean.** No receipt means the page has not been verified since
whatever last wrote it — a fresh clone, a machine without node, a tick that has
not run since the change. Reading that as a pass would restore exactly the
silence smoke was written to break.

**Critical, by the same measure as a spent key.** `wallet.shared_key` is
critical because the agent has stopped; a blank page is critical because the
operator's main view has. Both clear themselves when the component works again —
the next successful build overwrites the receipt.

**The receipt's path is the page's, not a setting.** The first version took it
from an env var only the tick set, and the gate — which builds the page and
executes it too — recorded nothing, so the board would have called the page
"verified an older build" after every gate run: true of the record, useless to a
reader, and permanent, since the gate runs far more often than the tick.
Deriving the path from the argument means whoever executed a build attests to
it, a test's temporary copy gets a temporary receipt, and no second knob can
point at a different build than the one being hashed.
"""
from __future__ import annotations
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir
import paths

# The built page lives in the private workspace, never in the source tree; the
# portable runner's synthetic estate builds it before this suite runs.
PAGE = paths.DASHBOARD_HTML

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rule(receipt, page_sha=""):
    import build_findings as B
    fn = getattr(B, "blank_page_findings", None)
    return None if fn is None else fn(receipt, page_sha)


# ─────────── the receipt is written by the thing that knows ────────────

def test_smoke_writes_a_receipt_either_way() -> None:
    import shutil
    if shutil.which("node") is None:
        print("  NOTE  node is absent, so smoke cannot run here "
              "[uncoverable: the receipt is written by dashboard/smoke.js, and "
              "the only executor of the page in this repository is node]")
        return
    page = PAGE
    if not page.is_file():
        print("  NOTE  no built page here "
              "[covered: the rule cases below run on receipts]")
        return
    # A COPY, so the live receipt beside the operator's page is not rewritten by
    # a test — the same reason every other suite here redirects rather than
    # writes. The receipt lands beside the copy because the path is derived.
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-smoke-"))
    mine = d / "page.html"
    mine.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
    receipt = d / "page.smoke.json"
    p = subprocess.run(["node", "dashboard/smoke.js", str(mine)], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    check("smoke runs", p.returncode in (0, 1), (p.stdout + p.stderr)[-200:])
    check("and writes its receipt beside the page it checked", receipt.is_file(),
          "a verdict nobody can read afterwards is the defect this fixes")
    if not receipt.is_file():
        return
    doc = json.loads(receipt.read_text(encoding="utf-8"))
    for field in ("ran_at", "verdict", "page", "page_sha"):
        check(f"the receipt carries `{field}`", doc.get(field) is not None, str(doc))
    check("the verdict is one of the two words",
          doc.get("verdict") in ("clean", "blank"), str(doc.get("verdict")))
    check("and a clean run says what it rendered",
          doc["verdict"] != "clean" or isinstance(doc.get("containers"), int),
          str(doc))


def test_a_planted_defect_produces_a_blank_receipt() -> None:
    """The whole chain, driven, not asserted about.

    A clean receipt from a page that works proves the happy path only. This
    plants the exact defect that shipped — a reference used at load before its
    `const` — into a COPY of the page, and requires smoke to fail, to say why,
    and to write `blank` where the finding will read it."""
    import shutil as sh
    if sh.which("node") is None:
        print("  NOTE  node is absent, so the page's script cannot be executed "
              "[uncoverable: executing JavaScript is what this test is]")
        return
    page = PAGE
    if not page.is_file():
        print("  NOTE  no built page to plant a defect in "
              "[covered: the rule's four outcomes run on receipts]")
        return
    html = page.read_text(encoding="utf-8")
    start = html.rindex("<script>") + len("<script>")
    # A temporal dead zone, which is what shipped twice: read at load, declared
    # later in the same scope.
    hurt = html[:start] + "\nconst _p = PLANTED_TDZ; const PLANTED_TDZ = 1;\n" + html[start:]
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-planted-"))
    broken, receipt = d / "page.html", d / "page.smoke.json"
    broken.write_text(hurt, encoding="utf-8")
    p = subprocess.run(["node", "dashboard/smoke.js", str(broken)], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    check("smoke fails on the planted defect", p.returncode == 1,
          f"rc={p.returncode}; a check that cannot fail is not a check")
    check("and says the page would render blank",
          "blank" in (p.stdout + p.stderr), (p.stdout + p.stderr)[:200])
    check("the receipt records it", receipt.is_file(), "")
    if not receipt.is_file():
        return
    doc = json.loads(receipt.read_text(encoding="utf-8"))
    check("with the verdict `blank`", doc.get("verdict") == "blank", str(doc))
    check("and the error that caused it",
          "ReferenceError" in str(doc.get("reason") or ""), str(doc.get("reason")))
    out = rule(doc, doc.get("page_sha") or "")
    if out is None:
        return
    check("and the board turns that receipt into one critical row",
          len(out) == 1 and out[0]["severity"] == "critical", str(out)[:200])


# ─────────── the board learns ──────────────────────────────────────────

def test_a_blank_verdict_is_critical() -> None:
    out = rule({"ran_at": "2026-09-08T03:00:54Z", "verdict": "blank",
                "page": "docs/projects-dashboard.html",
                "reason": "the page's script threw at load: ReferenceError"})
    if out is None:
        check("build_findings.blank_page_findings exists", False,
              "the one failure that blanks the operator's surface reached a log only")
        return
    check("a blank page is one row", len(out) == 1, str(out))
    if not out:
        return
    f = out[0]
    check("critical, like a stopped agent", f["severity"] == "critical", f["severity"])
    check("and the reason travels with it",
          "ReferenceError" in json.dumps(f, ensure_ascii=False), f["detail"][:200])


def test_a_clean_verdict_says_nothing() -> None:
    out = rule({"ran_at": "2026-09-08T03:31:00Z", "verdict": "clean",
                "page": "docs/projects-dashboard.html", "containers": 6})
    if out is None:
        return
    check("a clean page reports nothing", out == [], str(out))


def test_the_receipt_names_the_build_it_checked() -> None:
    """A clean verdict about the page that has since been replaced is a pass
    about a page nobody is looking at. The receipt carries the checked page's own
    hash so staleness is decidable rather than assumed — the same reason
    generated documents carry stamps."""
    ok = {"ran_at": "2026-09-08T03:31:00Z", "verdict": "clean",
          "page": "docs/projects-dashboard.html", "page_sha": "a1b2c3d4e5f6",
          "containers": 6}
    out = rule(ok, "a1b2c3d4e5f6")
    if out is None:
        return
    check("a clean verdict about THIS build reports nothing", out == [], str(out))
    stale = rule(ok, "999999999999")
    check("a clean verdict about an older build is reported", len(stale) == 1, str(stale))
    if stale:
        check("as info — the page may well be fine, it is simply unmeasured",
              stale[0]["severity"] == "info", stale[0]["severity"])
        check("and both hashes travel with it, so the claim is checkable",
              "a1b2c3d4e5f6" in stale[0]["detail"] and "999999999999" in stale[0]["detail"],
              stale[0]["detail"][:200])


def test_an_unreadable_verdict_is_not_a_pass() -> None:
    out = rule({"ran_at": "2026-09-08T03:31:00Z", "verdict": "",
                "page": "docs/projects-dashboard.html"})
    if out is None:
        return
    check("a receipt with no verdict is reported", len(out) == 1, str(out))
    if out:
        check("as unverified rather than clean",
              out[0]["type"] == "dashboard.unverified", out[0]["type"])


def test_no_receipt_is_not_a_pass() -> None:
    """A fresh clone, a machine without node, or a tick that has not run since
    the change. Reading any of those as clean restores the silence smoke exists
    to break."""
    out = rule(None)
    if out is None:
        return
    check("an absent receipt is reported", len(out) == 1, str(out))
    if out:
        check("as info rather than critical — nothing is known to be wrong",
              out[0]["severity"] == "info", out[0]["severity"])
        # THE TYPE, not a word in the title. The first version of this assertion
        # searched for a phrase guessed from the title, which is the
        # same defect as matching prose instead of behaviour — the type is the
        # machine-readable claim, and `not ... verified` is what the sentence
        # must not lose.
        check("and it says the page is unverified, not fine",
              out[0]["type"] == "dashboard.unverified"
              and "not been verified" in out[0]["title"],
              f"{out[0]['type']} / {out[0]['title']}")


def test_the_unattended_runner_still_runs_it() -> None:
    """The tick is the unattended user, and its verdict must outlive its log
    line. It needs no configuration to record one — which is the point — so what
    this checks is that the run is still there."""
    src = (ROOT / "tools/tick.py").read_text(encoding="utf-8")
    check("the tick still runs smoke", "dashboard/smoke.js" in src, "")
    # THE INVARIANT AT ITS SOURCE, not by searching the tree for a name. Two
    # versions of this assertion were wrong before this one. The first matched
    # prose in a comment. The second ran `git grep` for the env-var name and
    # asserted the tree does not contain it — while sitting in a file that
    # contained it, as the argument to that very grep. It passed while the file
    # was untracked and went red the moment it was committed: an assertion whose
    # subject includes itself, answering a question about `git`'s index rather
    # than about the code. What is actually true: smoke.js reads no environment
    # at all, because the receipt's path is derived from the page's.
    js = (ROOT / "dashboard/smoke.js").read_text(encoding="utf-8")
    check("smoke reads no environment, so nothing can redirect the receipt",
          "process.env" not in js,
          "the path must be derived from the argument, never configured")
    check("and it is derived from the page it was given",
          ".smoke.json" in js and "file.replace" in js, "")


def test_the_finding_reaches_a_surface_that_is_not_the_page() -> None:
    """The bootstrap paradox, and the reason `critical` is load-bearing.

    A `dashboard.blank` row rendered ON the dashboard cannot be read: the panel
    that renders findings is written by the script that died. So the severity is
    not a matter of taste — `tools/notify_findings.py` sends `critical` and
    `warning` and withholds `info` unless asked, and this row's only readable
    surface is the one that selection reaches."""
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_keeping_strings(
        (ROOT / "tools/notify_findings.py").read_text(encoding="utf-8"))
    check("the notifier selects critical without being asked",
          '"critical"' in code and "--include-info" in code,
          "the one finding that cannot be read on the page must leave by another door")
    out = rule({"ran_at": "2026-09-08T03:00:54Z", "verdict": "blank",
                "page": "docs/projects-dashboard.html", "reason": "ReferenceError"})
    if out is None:
        return
    check("and the blank row is of a severity that selection carries",
          out[0]["severity"] in ("critical", "warning"), out[0]["severity"])


def test_the_tick_records_the_failure_and_does_not_wait_a_cycle() -> None:
    """Two gaps found by reading the block from above, both of the same class.

    `step` is what puts a failure into `tick.json`, where `tick.step_failed`
    names it. The dashboard builder and smoke both bypassed it — `|| log "…"`
    writes a log line and nothing else — so the two steps that produce and
    verify the operator's primary surface were the two whose failures the report
    could not carry.

    And the ORDER: the receipt is written after `findings` has already run this
    cycle, so without a re-run on the failure path the operator learns at the
    next tick — thirty minutes of a page showing nothing."""
    src = (ROOT / "tools/tick.py").read_text(encoding="utf-8")
    check("the dashboard build goes through `step`",
          'step("dashboard", PY, "dashboard/build_dashboard.py")' in src,
          "a failed build of the operator's page must reach `tick.json`")
    # PORTED-DIVERGED: the page's path is the workspace's, passed as
    # DASHBOARD rather than a fixed source-tree path.
    check("and smoke does too",
          'step("smoke", "node", "dashboard/smoke.js", DASHBOARD)' in src,
          "otherwise `tick.step_failed` can never name it")
    check("a blank page re-runs findings, so notify carries it this tick",
          'step("findings-recheck"' in src,
          "the receipt is written after findings ran; without this the row waits "
          "for the next tick")
    i = src.find('step("findings-recheck"')
    check("and the re-run happens BEFORE the notifier",
          i != -1 and i < src.find('step("notify"'),
          "a row computed after the notifier ran is a row that waits a cycle")
    check("a machine without node is still reported as unmeasured, not failed",
          "node is absent" in src,
          "an unanswerable question recorded as a failure teaches the reader to "
          "ignore the report")


def test_the_gate_declares_the_receipt_as_its_own_write() -> None:
    """The gate's `smoke` step writes it too, so the purity verdict must know —
    and it must be in the STEP-OUTPUT container, not the foreign-writer one. The
    difference is the claim: `a step of the gate may write this` versus `this is
    not evidence about this tree at all`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("obs_blank", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    sys.modules["obs_blank"] = obs
    spec.loader.exec_module(obs)
    key = "docs/projects-dashboard.smoke.json"
    check("the receipt is a declared allowed write",
          key in obs.IGNORED_WRITES_ALLOWED, str(sorted(obs.IGNORED_WRITES_ALLOWED)))
    check("and not filed as a stranger's write",
          key not in obs.FOREIGN_WRITES_IGNORED, str(sorted(obs.FOREIGN_WRITES_IGNORED)))
    check("with a reason, not a bare entry",
          len(obs.IGNORED_WRITES_ALLOWED.get(key, "").split()) >= 10,
          obs.IGNORED_WRITES_ALLOWED.get(key, ""))
    # THE BAN THAT KEEPS ITS TEETH: nothing under store/raw/ may be an allowed
    # write, because every such write is a fixture that forgot to redirect. The
    # receipt sits beside the page precisely so this stays absolute.
    check("no allowed write reaches into store/raw/",
          not [k for k in obs.IGNORED_WRITES_ALLOWED if k.startswith("store/raw/")],
          str([k for k in obs.IGNORED_WRITES_ALLOWED if k.startswith("store/raw/")]))
    # PORTED-DIVERGED: the page and its receipt live in the private workspace
    # rather than as ignored files in a checkout, so "git ignores it" becomes
    # "it is not written into the source tree at all".
    check("and the receipt is written outside the source tree, beside the page",
          not PAGE.with_suffix(".smoke.json").resolve().is_relative_to(ROOT.resolve()),
          "a verdict beside the page must not dirty the program's tree")


if __name__ == "__main__":
    print("blank page — the tick caught it and told a log file\n")
    for fn in (test_smoke_writes_a_receipt_either_way,
               test_a_planted_defect_produces_a_blank_receipt,
               test_a_blank_verdict_is_critical,
               test_a_clean_verdict_says_nothing,
               test_the_receipt_names_the_build_it_checked,
               test_an_unreadable_verdict_is_not_a_pass,
               test_no_receipt_is_not_a_pass,
               test_the_unattended_runner_still_runs_it,
               test_the_finding_reaches_a_surface_that_is_not_the_page,
               test_the_tick_records_the_failure_and_does_not_wait_a_cycle,
               test_the_gate_declares_the_receipt_as_its_own_write):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma blank page reaches the board, and an unverified one says so\033[0m")
