#!/usr/bin/env python3
"""Check the built dashboard against the SHELEG Workbench pack's rules that are never waived.

Written as a tool because a check done by hand produces false positives, and
they teach one lesson: **a checker must know the difference between the thing
and what the thing is talking about.**

* A `prefers-color-scheme` inside a COMMENT of the copied token layer is not a
  second theme source. Comments are stripped before the CSS is examined.
* An emoji inside the DATA block can be a repository description quoted
  faithfully from upstream. The pack bans emoji in product-UI chrome; editing a
  measurement to satisfy a style rule would be the worse defect. Only the
  chrome is examined.

Exits non-zero on a real violation.
"""
from __future__ import annotations
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Run as `dashboard/audit_pack.py`, sys.path[0] is dashboard/; `paths` lives one level up.
sys.path.insert(0, str(ROOT))
import paths
PAGE = paths.DASHBOARD_HTML
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def main() -> int:
    if not PAGE.exists():
        print(f"no page at {PAGE} — run `project-observatory full dashboard`", file=sys.stderr)
        return 1
    html = PAGE.read_text(encoding="utf-8")
    css_raw = html[html.index("<style>") + 7:html.index("</style>")]
    css = strip_comments(css_raw)
    # The chrome is everything outside the embedded data literal.
    data_at = html.find("const D = ")
    chrome = html[:data_at] + html[html.find("\n", html.find("};", data_at)):]

    print(f"{PAGE.name} — {len(html) // 1024} KB\n")

    raw_sizes = [m for m in re.findall(r"font-size:\s*([^;]+);", css) if "var(--" not in m]
    short = re.findall(r"font:\s*[^;]*?(\d+px)", css)
    check("no ad-hoc font size anywhere", not raw_sizes and not short, f"{raw_sizes} {short}")

    comp = css[css.index("*, *::before"):]
    check("components consume only var(--…) colours",
          not re.findall(r"#[0-9a-fA-F]{3,8}", comp), str(re.findall(r"#[0-9a-fA-F]{3,8}", comp)[:4]))
    check("no shadow used as decoration",
          not re.findall(r"box-shadow:\s*([^;]+);", comp))
    rad = [m for m in re.findall(r"border-radius:\s*([^;]+);", comp)
           if "var(--r-" not in m and m.strip() != "50%"]
    check("radii come from tokens", not rad, str(rad))

    tr = re.findall(r"transition:\s*([^;]+);", comp)
    bad = [t for t in tr if re.search(r"transform|width|height|box-shadow|\ball\b", t)]
    check("transitions touch background, border and colour only", not bad, str(bad))
    durs = set(re.findall(r"var\(--dur-[a-z]+\)", " ".join(tr)))
    check("durations come from the two motion tokens",
          durs <= {"var(--dur-hover)", "var(--dur-state)"}, str(durs))
    check("reduced motion is served", "prefers-reduced-motion" in css)
    check("one theme switch, no second source in CSS",
          "prefers-color-scheme" not in css,
          "a media query redefining tokens beside the data-theme switch")

    check("no emoji in the chrome", not EMOJI.search(chrome),
          str(EMOJI.findall(chrome)[:4]))
    check("a dense table sits on --panel, never on --bg",
          ".card {" in comp and "background: var(--panel)" in comp)
    check("every coloured chip carries a word, not colour alone",
          comp.count(".chip.ok") and "class=\\\"chip" not in comp,
          "status colour must be paired with text")
    check("the page declares its charset for file://", "meta charset" in html[:400])

    # ── Structure, added after the pack audit passed a visibly broken page ──
    # Thirteen conformance checks were green while the sticky header floated
    # over its own rows. Every rule below is the negative of a bug that
    # actually shipped here, so each one is worth its line.
    # ONE HEADER PER TABLE, not one header in the file. The defect this guards
    # is a table split per owner group, where every group's header sticks at
    # the same offset and the reader gets forty of them stacked. Counting
    # headers in the FILE also forbade a second table that is never in the DOM
    # at the same time as the first, which is what the Heroku tab is: the two
    # lists replace each other in `#out`, so at most one header exists at any
    # moment. The equality still fails on the original defect — one `<table`
    # against N `<thead` — and now passes the thing it was never about.
    check("one header per table, not one per owner group",
          html.count("<thead") == html.count("<table") and html.count("<thead") >= 1,
          f"{html.count('<thead')} headers for {html.count('<table')} table(s) — "
          f"each sticks at the same offset")
    check("nothing between the sticky header and the viewport scrolls",
          not re.search(r"\.(card|scroll|grp)[^{]*\{[^}]*overflow", comp),
          "an overflow ancestor becomes the sticky container instead of the page")
    check("the sticky offset is measured, not a typed constant",
          re.search(r"position:\s*sticky;\s*top:\s*var\(--stick", comp) is not None,
          "the controls bar wraps to two lines on a narrow window")
    check("column widths are fixed, so eight columns always fit",
          "table-layout: fixed" in comp and "<colgroup>" in html)
    check("the widths add up to exactly one table",
          sum(int(m) for m in re.findall(r"col\.c-\w+\s*\{\s*width:\s*(\d+)%", comp)) == 100,
          str(sum(int(m) for m in re.findall(r"col\.c-\w+\s*\{\s*width:\s*(\d+)%", comp))))
    check("a long repository list folds instead of stretching its row",
          ".repos.folded" in comp and 'class="more"' in html)
    check("the accent never sits as text on --bg or --panel-2",
          not re.search(r"\.(sub|desc|none|folder)\s*\{[^}]*color:\s*var\(--accent\)", comp))

    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} violation(s)\033[0m")
        return 1
    print("\033[32mthe page matches the pack\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
