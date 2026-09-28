#!/usr/bin/env python3
"""The page's header once claimed sixty-three findings while the list held forty.

`dashboard/build_dashboard.py` sliced the finding list at a bare cap in the
middle of a dict literal, and the header printed the COUNTS beside it. So a
reader was told one number and shown another, and the disagreement was silent —
the cap this repository's own rule forbids: "if a workflow bounds coverage, log
what was dropped".

The first justification for the cap was that the list is ordered by severity,
then deadline, then id (`tools/build_findings.py`), so what falls off the end is
the tail of `info`. That was incomplete: the tail below the cut carried whole
classes with no row on the page at all, and the board is the ONLY surface an
info row reaches (the notifier sends `critical` and `warning` by default). So
`_findings_panel` now carries every open row and FOLDS a type's rows beyond the
per-type floor under a control that names their count; nothing is omitted.

**And the instrument used to measure this had the same defect.**
`tests/render_dashboard.mjs` returned a fixed slice of the findings HTML with no
marker, so counting rows in its report gave one where the page carried forty. A
silent cap in a test harness produced a false conclusion about a silent cap in
the page, which is why the harness now names its own truncation.
"""
from __future__ import annotations
import json, os, pathlib, re, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402
import paths                                                        # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
HARNESS = ROOT / "tests/render_dashboard.mjs"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def build() -> pathlib.Path:
    """Build a page into scratch, never over the live one."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-omission-"))
    out = d / "page.html"
    p = subprocess.run([PY, "dashboard/build_dashboard.py"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_DASHBOARD=str(out)),
                       capture_output=True, text=True, timeout=900)
    if not out.is_file():
        raise AssertionError(f"the page did not build: {(p.stdout + p.stderr)[-300:]}")
    return out


def payload(page: pathlib.Path) -> dict:
    html = page.read_text(encoding="utf-8")
    m = re.search(r'const D = (\{.*?\});\n', html, re.S)
    if not m:
        raise AssertionError("no payload in the page")
    return json.loads(m.group(1))


def rendered(page: pathlib.Path) -> dict | None:
    exe = shutil.which("node")
    if exe is None:
        return None
    p = subprocess.run([exe, str(HARNESS), str(page)], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise AssertionError(f"the harness returned no JSON: {(p.stdout + p.stderr)[-300:]}")


# ─────────── the page says what it left out ────────────────────────────

def test_the_payload_states_the_omission() -> None:
    d = payload(build())
    f = d.get("findings") or {}
    counts, items = f.get("counts") or {}, f.get("items") or []
    total = sum(counts.values())
    check("the payload carries the counts", total > 0, json.dumps(counts))
    check("and an explicit omission",
          isinstance(f.get("omitted"), int), json.dumps(sorted(f))[:200])
    if not isinstance(f.get("omitted"), int):
        return
    # THE ARITHMETIC, computed where both filters are known. `counts` excludes
    # acknowledged findings and so does `items`, so the difference is exact —
    # which is why the number is produced in Python rather than subtracted from
    # three fields in the page's script.
    check("which equals what the counts promise minus what is carried",
          f["omitted"] == max(total - len(items), 0),
          f"omitted={f['omitted']} total={total} items={len(items)}")


def planted(rows: list[dict]) -> dict:
    """Run the panel over a board we control, not over today's estate.

    The live board has whatever it has; the invariant is about the RULE. This
    calls the panel directly — building a page needs a whole registry, and the
    function under test takes the findings document and nothing else.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("bd_om", ROOT / "dashboard/build_dashboard.py")
    bd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bd)
    return bd._findings_panel({"findings": rows, "counts": {"info": len(rows)},
                               "built_at": "2026-09-09T00:00:00Z"}), bd


def test_every_type_keeps_a_row_however_long_the_tail() -> None:
    """A cap may drop more of a kind. It may not drop the only instance of one.

    Sixty rows of one loud type, then five rare ones ranked last — exactly the
    shape the live board had, where ten classes sat below a forty-row cut and
    the reader could not learn they existed.
    """
    rows = ([{"id": f"loud:{i}", "type": "loud.thing", "severity": "info",
              "title": "t", "detail": "d", "action": "a"} for i in range(60)]
            + [{"id": f"rare{i}", "type": f"rare.kind{i}", "severity": "info",
                "title": "t", "detail": "d", "action": "a"} for i in range(5)])
    panel, bd = planted(rows)
    shown = {f["type"] for f in panel["items"]}
    check("every type present in the board has a row on the page",
          shown == {f["type"] for f in rows},
          f"missing {sorted({f['type'] for f in rows} - shown)}")
    # THE CEILING BECAME A FOLD: the loud type is
    # carried whole, its rows beyond the floor marked `folded`, and the fold
    # is named with its count — so nothing is omitted and the page still opens
    # on what needs the operator now.
    loud = [f for f in panel["items"] if f["type"] == "loud.thing"]
    check("the loud type is carried whole, not cut",
          len(loud) == 60, f"{len(loud)} of 60 carried")
    check("and its rows beyond the per-type floor are FOLDED, not dropped",
          sum(1 for f in loud if f["folded"]) == 60 - bd.FINDINGS_ON_PAGE
          and panel["folded_by_type"].get("loud.thing") == 60 - bd.FINDINGS_ON_PAGE,
          str(panel["folded_by_type"]))
    check("so the omission is exactly zero",
          panel["omitted"] == 0 and len(panel["items"]) == len(rows),
          f"omitted={panel['omitted']} rows={len(rows)} items={len(panel['items'])}")


def test_the_live_board_hides_no_class() -> None:
    """The same invariant against today's estate, where the defect was found."""
    d = payload(build())
    f = d.get("findings") or {}
    doc = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8"))
    open_types = {x["type"] for x in doc["findings"] if not x.get("acked")}
    shown = {x["type"] for x in (f.get("items") or [])}
    check("no class of finding is absent from the live page",
          not (open_types - shown), f"hidden: {sorted(open_types - shown)}")


def test_the_page_carries_every_row_it_has_a_row_for() -> None:
    """The findings list was capped; nothing said whether the others were.

    When it was measured the answer was no: every project and every duplicate
    group was on the page. The value of that measurement is entirely in this
    check: a `[:100]` added tomorrow would be caught here rather than by a
    reader wondering where a project went.
    """
    page = build()
    d = payload(page)
    reg = json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))
    dups = json.loads((paths.REGISTRY / "duplicate-repo-names.json").read_text(encoding="utf-8"))
    check("every project in the registry has a row",
          len(d["rows"]) == len(reg["projects"]),
          f"{len(d['rows'])} rows for {len(reg['projects'])} projects")
    check("and every duplicate-name group is carried",
          len(d["dups"]) == len(dups["groups"]),
          f"{len(d['dups'])} of {len(dups['groups'])}")


def test_a_population_with_no_row_is_counted_where_it_cannot_be_listed() -> None:
    """A thing the page cannot show must at least say how many it is hiding.

    `inactive_repos` already carried that rule: an inactive repository anchors no
    project, so it has no row, and the count is printed instead. The same was
    NOT true of domains — most registered, few on a row, the total printed
    beside them — until it was measured. These are the operator's own assets,
    most of them costing a renewal a year.
    """
    d = payload(build())
    s = d["stats"]
    reg = json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))
    dom = json.loads((paths.REGISTRY / "domains.json").read_text(encoding="utf-8"))
    on_rows = {x["host"] for r in d["rows"] for x in (r.get("sites") or [])}
    expected = len({x["name"] for x in dom["domains"]} - on_rows)
    check("the count of domains on no row is exact",
          s.get("domains_no_row") == expected,
          f"payload says {s.get('domains_no_row')}, the registry and the rows say {expected}")
    check("and the total it qualifies is the registry's own",
          s.get("domains") == len(dom["domains"]), str(s.get("domains")))
    html = build().read_text(encoding="utf-8")
    check("both hidden populations are RENDERED, not merely carried",
          "domains without a project" in html and "inactive repos" in html,
          "a number in the payload that no panel prints is a number nobody reads")
    check("the reason travels with the count",
          "no row to sit in" in (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8"),
          "the next reader has to learn why the total and the list disagree")


def test_the_cap_is_a_named_constant_with_its_reason() -> None:
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the cap has a name", "FINDINGS_ON_PAGE" in src,
          "a bare `[:40]` in a dict literal is a policy nobody can find")
    check("and the reason is beside it",
          "ordered by severity" in src.lower() or "tail of" in src.lower(),
          "what falls off the end matters, and the order is what makes it safe")


def test_the_header_names_the_omission_when_there_is_one() -> None:
    """DRIVEN through the page's own script: a claim about what a reader sees has
    to come from rendering it, not from the source that produces it."""
    page = build()
    d = payload(page)
    omitted = ((d.get("findings") or {}).get("omitted")) or 0
    got = rendered(page)
    if got is None:
        print("  NOTE  node is absent; the rendered assertion cannot run here "
              "[covered: the payload cases above assert the arithmetic]")
        return
    html = got.get("findings") or ""
    check("the harness reported the findings container", bool(html), str(sorted(got)))
    # The engine never omits (every row is carried and folded), so the only
    # reachable state is "nothing omitted"; the branch stays for the day a cap
    # returns.
    if omitted:
        check("and the header says how many are not shown",
              "not shown" in html or "more" in html,
              html[:300])
    else:
        check("with nothing omitted, no such phrase appears",
              "not shown" not in html, html[:200])


def test_the_order_is_what_makes_the_cap_safe() -> None:
    """A cap that dropped criticals would be a different defect. The list is
    ordered by severity before slicing, so the tail lost is `info`."""
    f = (payload(build()).get("findings") or {})
    items = f.get("items") or []
    rank = {"critical": 0, "warning": 1, "info": 2}
    ranks = [rank.get(x.get("severity"), 3) for x in items]
    check("the carried items are severity-ordered", ranks == sorted(ranks),
          str(ranks[:20]))
    if items:
        check("so the first is the most severe present",
              ranks[0] == min(ranks), str(ranks[:5]))


# ─────────── the instrument that misled me ─────────────────────────────

def test_the_harness_marks_its_own_truncation() -> None:
    """The defect that once produced a wrong measurement: the harness returned
    400 characters of a 20 KB render with no marker, so counting rows in its
    report said ONE where the page carried forty."""
    src = HARNESS.read_text(encoding="utf-8")
    check("the harness still bounds its report",
          "slice(0," in src, "an unbounded report would be the other extreme")
    check("and says when it truncated",
          "truncated" in src.lower() or "more char" in src.lower(),
          "a silent cap in a test instrument is a wrong measurement waiting")
    page = build()
    got = rendered(page)
    if got is None:
        print("  NOTE  node is absent; the rendered assertion cannot run here")
        return
    html = got.get("findings") or ""
    if len(html) > 300:
        check("the returned report names its own limit",
              "char" in html[-80:].lower() or "…" in html[-80:],
              html[-120:])


if __name__ == "__main__":
    print("page omission — every finding the header counts is carried\n")
    for fn in (test_the_payload_states_the_omission,
               test_every_type_keeps_a_row_however_long_the_tail,
               test_the_live_board_hides_no_class,
               test_the_page_carries_every_row_it_has_a_row_for,
               test_a_population_with_no_row_is_counted_where_it_cannot_be_listed,
               test_the_cap_is_a_named_constant_with_its_reason,
               test_the_header_names_the_omission_when_there_is_one,
               test_the_order_is_what_makes_the_cap_safe,
               test_the_harness_marks_its_own_truncation):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe page says what it did not show, and so does the harness\033[0m")
