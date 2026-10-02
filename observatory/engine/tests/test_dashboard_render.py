#!/usr/bin/env python3
"""The one screen a person reads, EXECUTED rather than inspected.

Every earlier assertion about the dashboard pointed at its data:
`from_store()` returns the right numbers, and `tests/test_dashboard_store.py`
checks that it does. None of them asked whether the page turns those numbers
into anything. The health panel was filled four times in one night — registry
proposals, the projection's lag, the spend — and the page was never opened, so a
JavaScript error would have left it blank with the whole suite green.

**Executing it found a row that had never rendered once.** The spend line tested
`H.wallet.month`, and `store/wallet.json` holds `months: {"2026-09": 0.0924}`
with no scalar of that name — so the condition was false on every render. A
branch whose true side is unreachable, in the screen the estate exists to show,
invisible to every test that looked at the data behind it.

A browser was not reachable here (the Chrome extension is not connected), so the
check is node with a minimal DOM that records what the script writes:
`tests/render_dashboard.mjs`. It catches a syntax error, an undefined reference
and an empty panel, which is what "checked in a browser" was protecting against.
It does not check layout, fonts or colour, and does not pretend to.

**Three of the first four failures were the harness, not the page** — a bare
`addEventListener`, `ResizeObserver`, a `querySelector` returning null — and
each was fixed in the stub rather than recorded as a defect. A harness that
fails differently from a browser tests the harness.
"""
from __future__ import annotations
import json, os, pathlib, re, shutil, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup, PROJECT_ID as SYNTHETIC_PROJECT
portable_setup()

sys.path.insert(0, str(ROOT / "tests"))
import paths              
import tmp as tmpdir              
import dashboard_fixture

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
HARNESS = ROOT / "tests/render_dashboard.mjs"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def node() -> str | None:
    return shutil.which("node")


def render(page: pathlib.Path, count: str | None = None) -> dict | None:
    exe = node()
    if exe is None:
        return None
    args = [exe, str(HARNESS), str(page)] + (["--count", count] if count else [])
    p = subprocess.run(args, cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    try:
        return json.loads(p.stdout)
    except ValueError:
        check("the harness returned JSON", False, (p.stdout + p.stderr)[-300:])
        return {}


def build(dest: pathlib.Path, *, samples=2) -> pathlib.Path:
    return dashboard_fixture.build(dest, samples=samples)


def test_the_page_runs_to_completion() -> None:
    if node() is None:
        print(("  SKIP  node is not installed here, so the page cannot be executed"
              " [uncoverable: executing the page needs node, and the only two executors here — dashboard/smoke.js and tests/render_dashboard.mjs — are both node]"))
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-render-"))
    page = build(d)
    check("the page was built", page.is_file() and page.stat().st_size > 10_000,
          str(page.stat().st_size if page.is_file() else "absent"))
    r = render(page)
    if not r:
        return
    check("the script runs to the end without throwing", r["threw"] is None,
          str(r["threw"]))
    check("and logs no console error", not r["consoleErrors"], str(r["consoleErrors"]))
    check("it registers its listeners", r["listeners"] >= 3, str(r["listeners"]))


def test_every_panel_renders_something() -> None:
    """An empty panel is the failure a data-side test cannot see."""
    if node() is None:
        print(("  SKIP  node is not installed here"
              " [uncoverable: executing the page needs node, and the only two executors here — dashboard/smoke.js and tests/render_dashboard.mjs — are both node]"))
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-render2-"))
    r = render(build(d))
    if not r:
        return
    written = r["written"]
    for panel in ("tiles", "health", "findings", "out"):
        check(f"`{panel}` was written", written.get(panel, 0) > 0,
              f"{written.get(panel, 0)} chars — a blank panel reads as 'nothing to show'")
    check("the synthetic project list renders", written.get("out", 0) > 0,
          str(written.get("out")))


def test_the_spend_row_renders_at_all() -> None:
    """The row that had never appeared, now driven end to end."""
    if node() is None:
        print(("  SKIP  node is not installed here"
              " [uncoverable: executing the page needs node, and the only two executors here — dashboard/smoke.js and tests/render_dashboard.mjs — are both node]"))
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-render3-"))
    r = render(build(d))
    if not r:
        return
    health = r["health"]
    check("selected state spend reaches the rendered row", "4.0000" in health and "2.5000" in health, health[-200:])
    check("the spend row is on the page", "spent by this project" in health,
          health[-200:])
    check("with the day's figure beside the month's", "of which today" in health,
          health[-200:])
    # DELIBERATELY NOT asserted: that the string `H.wallet.month` is absent.
    # It survives in the comment explaining its removal, and here the text check
    # is unanswerable in principle — the JavaScript lives inside a Python string
    # literal, so `source_reader.code_only` blanks the whole block, and the JS
    # comment ships to the page along with the code. This is the seventh
    # assertion of that shape in one sitting, and the lesson has landed: the row
    # RENDERING is the evidence. The absence of an old string proves nothing
    # about behaviour, and asserting it costs a correction every time the fix is
    # documented.

    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the figures are prepared in Python, not derived in the browser",
          "spend_month" in src and 'strftime("%Y-%m")' in src,
          "a browser deriving which month it is would be a second place to be wrong")
    check("and the label says WHOSE spend it is",
          "spent by this project" in src,
          "the provider's counter measures a shared key")


def test_the_health_panel_shows_what_the_store_holds() -> None:
    """The numbers on the screen must be the numbers in the store."""
    if node() is None:
        print(("  SKIP  node is not installed here"
              " [uncoverable: executing the page needs node, and the only two executors here — dashboard/smoke.js and tests/render_dashboard.mjs — are both node]"))
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-render4-"))
    page=build(d)
    payload=json.loads(re.search(r'const D = (\{.*?\});\n',page.read_text(),re.S).group(1))
    store={'health':payload['health']}
    r = render(page)
    if not r:
        return
    health = r["health"]
    events = store["health"].get("events")
    if events:
        # The page groups thousands with a non-breaking space via toLocaleString,
        # so compare on the digits rather than on the formatting.
        digits = "".join(ch for ch in health if ch.isdigit())
        check("the event count reaches the screen", str(events) in digits.replace(" ", ""),
              f"{events} not in the rendered digits")
    proposed = store["health"].get("proposed")
    if proposed:
        check("so does the review queue's size", str(proposed) in health, health[:200])
    check("and a zero is NOT shown as news",
          ("registry edits proposed" in health)
          == bool(store["health"].get("registry_proposals")),
          "an always-present zero is furniture")


def test_the_detail_panel_names_what_a_project_is_made_of() -> None:
    """Two empty bullets on the first project a person opens.

    "Из чего состоит" rendered `x.name` and a repository row has only ever
    carried `nwo`, so every repository produced an empty `<li>`. Found by
    opening the panel, not by a test — every assertion about the data was green,
    because the data was right and the field was wrong.
    """
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith(("#", "//", "*", "/*")))
    check("the composition list reads the field a repository has",
          "E(x.nwo)" in code and "E(x.name)" not in code,
          "a repository row carries `nwo`; `name` has never existed on one")
    payload_keys = set()
    page = build(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-detail-")))
    payload = json.loads(re.search(r'const D = (\{.*?\});\n',
                                   page.read_text(encoding="utf-8"), re.S).group(1))
    for r in payload["rows"]:
        for x in (r.get("repos") or []):
            payload_keys |= set(x)
    check("and the payload really has no `name` on a repository",
          payload_keys and "name" not in payload_keys and "nwo" in payload_keys,
          str(sorted(payload_keys)[:8]))
    check("the panel captions its metrics like the table does",
          "E(metricLabel(m))" in code and "const cap = metricLabel(m)" in code,
          "`deps.direct` in one place and `dependencies` in the other is two "
          "vocabularies for one number")


def test_the_info_rows_remain_visible_in_the_attention_preview() -> None:
    """UI-04: a non-critical finding still needs a visible place in the preview.

    The split Overview bounds its preview and links to the complete findings
    page. Within that selected preview, warning/info records are visible.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fold-"))
    page = build(d)
    payload = json.loads(re.search(r'const D = (\{.*?\});\n',
                                   page.read_text(encoding="utf-8"), re.S).group(1))
    info = [f for f in payload["findings"]["items"] if f["severity"] == "info"]
    got = render(page, count='class="f finfo')
    if got is None:
        print("  SKIP  node is not on this machine")
        return
    rendered = sum((got.get("counts") or {}).values())
    check("every info row is in the rendered attention preview",
          rendered == len(info), f"{rendered} marked for {len(info)} info rows")
    body = (got.get("written") or {})
    check("the findings panel was written at all", body.get("findings"), str(body)[:120])
    hidden = render(page, count='class="flist folded"')
    check("the preview starts with its records visible",
          hidden is not None and sum(hidden.get("counts", {}).values()) == 0,
          "selected attention records must not be hidden by an extra click")


def test_a_number_meets_its_noun_in_the_right_case() -> None:
    """`строк(и)` is what a page prints when nobody wrote the plural rule.

    Read from CODE, not from the file: the comment that records this fix
    contains the very string it forbids, and the first version of this check
    went red on it — the fifth time in one session a rule fired on prose. The
    remedy is always the same and always cheap.
    """
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith(("#", "//", "*", "/*")))
    check("no parenthesised plural is printed", "строк(и)" not in code,
          "Russian counts three ways and the reader can tell")
    # Since 0.4.0 the catalog carries the forms and Intl.PluralRules picks one.
    check("and plural forms come from the language's rules", "new Intl.PluralRules(LOCALE)" in code)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plural-"))
    got = render(build(d), count="строк(и)")
    if got is None:
        print("  SKIP  node is not on this machine")
        return
    check("and no panel renders one", not (got.get("counts") or {}),
          str(got.get("counts")))


def test_a_finding_title_reads_in_the_readers_language() -> None:
    """A finding's title is a message id and its arguments, rendered by `T()`.

    Built twice from one synthetic findings file — once in each language — and
    executed: the Russian page says the counted title in Russian with the plural
    form its number takes, the English page says the rule's English, and a
    finding written before titles carried an id still shows its `title`."""
    sys.path.insert(0, str(ROOT / "dashboard"))
    from finding_types import titled
    rows = [{"id": "mcp.unreachable:estate:mcp", "type": "mcp.unreachable", "subject": "estate:mcp",
             "severity": "info", **titled("{n} MCP servers Claude could not reach", n=3),
             "detail": "synthetic", "action": "synthetic", "evidence": []},
            {"id": "work.unattributed:fixture", "type": "work.unattributed", "subject": "session-name:fixture",
             "severity": "info", "title": "Synthetic title from an older build",
             "detail": "synthetic", "action": "synthetic", "evidence": []}]
    seen = {}
    for locale in ("en", "ru"):
        d = pathlib.Path(tmpdir.mkdtemp(prefix=f"observatory-title-{locale}-"))
        env = dashboard_fixture.seed(d)
        (d / "registry/findings.json").write_text(json.dumps(
            {"counts": {"info": 2, "warning": 0, "critical": 0}, "findings": rows}))
        p = subprocess.run([PY, str(ROOT / "dashboard/build_dashboard.py")], cwd=ROOT,
                           env={**env, "OBSERVATORY_LOCALE": locale}, capture_output=True, text=True)
        check(f"the {locale} page builds", p.returncode == 0, p.stderr[-300:])
        seen[locale] = {needle: render(pathlib.Path(env["OBSERVATORY_DASHBOARD"]), count=needle)
                        for needle in ("3 MCP-сервера недоступны для Claude", "3 MCP servers Claude could not reach",
                                       "Synthetic title from an older build")}
    if any(v is None for got in seen.values() for v in got.values()):
        print("  SKIP  node is not on this machine")
        return
    n = {locale: {needle: sum((got.get("counts") or {}).values()) for needle, got in by.items()}
         for locale, by in seen.items()}
    check("the Russian page renders the title in Russian, in the plural its number takes",
          n["ru"]["3 MCP-сервера недоступны для Claude"] >= 1 and n["ru"]["3 MCP servers Claude could not reach"] == 0,
          str(n["ru"]))
    check("the English page renders the same title in English",
          n["en"]["3 MCP servers Claude could not reach"] >= 1 and n["en"]["3 MCP-сервера недоступны для Claude"] == 0,
          str(n["en"]))
    check("a title with no id still renders, in both languages",
          all(n[l]["Synthetic title from an older build"] >= 1 for l in n), str(n))


def test_an_unmeasured_estate_does_not_say_measured_not_measured() -> None:
    """A workspace with no scan yet printed "Измерено не измерено" (and "Measured not
    measured") in every page's header: the label must carry the absence itself."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-unmeasured-"))
    env = dashboard_fixture.seed(d)
    p = subprocess.run([PY, str(ROOT / "dashboard/build_dashboard.py")], cwd=ROOT,
                       env={**env, "OBSERVATORY_LOCALE": "ru", "OBSERVATORY_DB": str(d / "absent.db")},
                       capture_output=True, text=True)
    check("the page builds with no store", p.returncode == 0, p.stderr[-300:])
    got = {needle: render(pathlib.Path(env["OBSERVATORY_DASHBOARD"]), count=needle)
           for needle in ("Ещё не измерено", "Измерено не")}
    if any(v is None for v in got.values()):
        print("  SKIP  node is not on this machine")
        return
    n = {k: sum((v.get("counts") or {}).values()) for k, v in got.items()}
    check("the header says the estate is not measured yet, once", n["Ещё не измерено"] >= 1, str(n))
    check("and never 'Measured not measured'", n["Измерено не"] == 0, str(n))


def page_with_its_script(pages: pathlib.Path, name: str, dest: pathlib.Path) -> pathlib.Path:
    """A split page with `app.js` inlined, so the harness (which reads inline
    scripts) runs exactly what a browser runs for that page."""
    html = (pages / f"{name}.html").read_text(encoding="utf-8")
    js = (pages / "app.js").read_text(encoding="utf-8")
    out = dest / f"{name}-inline.html"
    out.write_text(html.replace('<script src="app.js"></script>', "<script>" + js + "</script>"), encoding="utf-8")
    return out


def test_a_stub_document_is_not_scanned_and_says_how_to_scan() -> None:
    """`init` writes empty documents with `scanned_on: null`; the pages tested only
    whether the document existed, so Keys, ENV, MCP, Traffic and Heroku said
    "Empty here — the registry holds no row of this kind" instead of "not
    scanned", and Keys offered `full openrouter`, which does nothing while that
    integration is off."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stubs-")).resolve()
    env = dashboard_fixture.seed(d)
    empty = json.loads((ROOT / "defaults/empty-registry.json").read_text(encoding="utf-8"))
    for name in ("credentials.json", "env-inventory.json", "mcp-servers.json",
                 "google-properties.json", "heroku-apps.json", "cloudflare-zones.json"):
        (d / "registry" / name).write_text(json.dumps(dict(empty[name], schema_version=1)), encoding="utf-8")
    p = subprocess.run([PY, str(ROOT / "dashboard/build_dashboard.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    check("the page builds over stub documents", p.returncode == 0, p.stderr[-300:])
    pages = pathlib.Path(env["OBSERVATORY_DASHBOARD_DIR"])
    expect = {"creds": ("Credentials were not scanned", "vault.py"),
              "env": ("Environment files were not scanned", "full env"),
              "mcp": ("MCP was not scanned", "configure integrations mcp true"),
              "traffic": ("Analytics was not scanned", "configure integrations google true"),
              "heroku": ("Heroku was not scanned", "configure integrations heroku true")}
    for name, (says, command) in expect.items():
        page = page_with_its_script(pages, name, d)
        got = {n: render(page, count=n) for n in (says, command, "registry holds no row")}
        if any(v is None for v in got.values()):
            print("  SKIP  node is not on this machine")
            return
        n = {k: sum((v.get("counts") or {}).values()) for k, v in got.items()}
        check(f"{name}: says it was not scanned", n[says] >= 1, str(n))
        check(f"{name}: hands over the command that scans it", n[command] >= 1, str(n))
        check(f"{name}: never 'the registry holds no row'", n["registry holds no row"] == 0, str(n))


def test_no_panel_renders_an_object_as_text() -> None:
    """`[object Object]` is what a page says when a field means two things.

    Found by OPENING the page on 2026-09-09, not by a test: `r.notes` held the
    count of wiki notes at line 437 and the store's list of the agent's
    conclusions at line 517, the second assignment won, and the wiki chip on
    every noted project read "[object Object],[object Object] зам.". Every
    Python assertion about the data was green — the collision was only visible
    where a person looks.

    The check is one string across every panel the script writes, which is the
    cheapest possible guard against the whole class.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-obj-"))
    page = build(d)
    got = render(page, count="object Object")
    if got is None:
        print("  SKIP  node is not on this machine [uncoverable: the string is "
              "produced by the page's own script, never by the payload]")
        return
    counts = got.get("counts") or {}
    check("no panel renders an object as text", not counts,
          f"{counts} — a field carrying two meanings, or an object where a "
          f"scalar was expected")


def test_a_metric_that_moved_says_so_on_the_page() -> None:
    """The second sample is kept for a reader, and no reader could see it.

    `store/retention.json` keeps two rows per series with the reason written
    beside it — "the newest, plus one so a reader can see whether it moved" —
    and the page rendered only the newest. Measured 2026-09-09: 911 metric rows
    across seven series, and not one movement visible anywhere.

    Counted rather than excerpted: the panel this renders into is 268 KB, and no
    clip of it can honestly answer "is the marker there".
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-delta-"))
    page = build(d)
    payload = json.loads(re.search(r'const D = (\{.*?\});\n',
                                   page.read_text(encoding="utf-8"), re.S).group(1))
    moved = {r["id"]: [m for m in (r.get("metrics") or [])
                        if m.get("p") is not None and m["p"] != m["v"]]
             for r in payload["rows"]}
    moved = {project: metrics for project, metrics in moved.items() if metrics}
    if not moved:
        check("the fixture produced a changed metric series", False)
        return
    # UI-03 keeps full metrics in project detail while the list stays compact.
    # Execute that actual interaction rather than requiring every secondary
    # metric to occupy a table cell before the operator opens a project.
    source = page.read_text(encoding="utf-8")
    for project, metrics in moved.items():
        detail_page = page.with_name("detail.html")
        detail_page.write_text(source + "<script>detail(" + json.dumps(project) + ");</script>", encoding="utf-8")
        got = render(detail_page, count='class="delta')
        if got is None:
            print("  SKIP  node is not on this machine [uncoverable: the marker is "
                  "written by the page's own script]")
            return
        check("opening project detail runs without error", got.get("threw") is None, str(got.get("threw")))
        rendered = (got.get("counts") or {}).get("panel", 0)
        check("every metric that moved carries a marker in project detail",
              rendered == len(metrics), f"{rendered} rendered for {len(metrics)} moved")
    check("and the marker names the previous value in its tooltip",
          'T("was {value}"' in (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8"),
          "a change with no previous figure is a direction without a size")


def test_a_single_sample_renders_no_movement() -> None:
    """Absent is not zero: a first measurement has not 'stayed the same'.

    Driven against a store with the older sample of every series removed, which
    is what a fresh estate looks like on its first tick.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-delta-one-"))
    out = build(d, samples=1)
    payload = json.loads(re.search(r'const D = (\{.*?\});\n',
                                   out.read_text(encoding="utf-8"), re.S).group(1))
    with_prev = [m for r in payload["rows"] for m in (r.get("metrics") or []) if "p" in m]
    check("with one sample per series the payload carries no previous value",
          not with_prev, f"{len(with_prev)} entries still carry one")
    got = render(out, count='class="delta')
    if got is None:
        print("  SKIP  node is not on this machine")
        return
    check("and the page renders no movement marker",
          not (got.get("counts") or {}), str(got.get("counts")))


def test_the_harness_itself_can_fail() -> None:
    """A green from a harness that cannot go red is not evidence."""
    if node() is None:
        print(("  SKIP  node is not installed here"
              " [uncoverable: executing the page needs node, and the only two executors here — dashboard/smoke.js and tests/render_dashboard.mjs — are both node]"))
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-render5-"))
    broken = d / "broken.html"
    broken.write_text(
        '<!doctype html><html><body><div id="health"></div>'
        '<script>document.getElementById("health").innerHTML = notDefinedAnywhere;'
        '</script></body></html>', encoding="utf-8")
    r = render(broken)
    if not r:
        return
    check("a page whose script throws is reported as throwing",
          r["threw"] and "ReferenceError" in r["threw"], str(r["threw"]))
    check("and its panel is empty", not r["health"], r["health"][:80])

    empty = d / "empty.html"
    empty.write_text('<!doctype html><html><body><div id="health"></div></body></html>',
                     encoding="utf-8")
    r2 = render(empty)
    check("a page with no script is refused rather than passed",
          r2 is None or r2.get("error") or not r2.get("written"),
          str(r2)[:120] if r2 else "no result")


if __name__ == "__main__":
    print("the dashboard, executed — not the data behind it\n")
    for fn in (test_the_page_runs_to_completion,
               test_every_panel_renders_something,
               test_the_spend_row_renders_at_all,
               test_the_health_panel_shows_what_the_store_holds,
               test_the_detail_panel_names_what_a_project_is_made_of,
               test_the_info_rows_remain_visible_in_the_attention_preview,
               test_a_number_meets_its_noun_in_the_right_case,
               test_a_finding_title_reads_in_the_readers_language,
               test_an_unmeasured_estate_does_not_say_measured_not_measured,
               test_no_panel_renders_an_object_as_text,
               test_a_stub_document_is_not_scanned_and_says_how_to_scan,
               test_a_metric_that_moved_says_so_on_the_page,
               test_a_single_sample_renders_no_movement,
               test_the_harness_itself_can_fail):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe page renders, and the row that never did now does\033[0m")
