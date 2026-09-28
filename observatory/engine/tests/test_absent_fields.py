#!/usr/bin/env python3
"""One missing key blanked the project table, and smoke stayed green.

Measured by rebuilding the page with one optional row field
absent at a time — seventeen of them — and executing the script against the stub
DOM in `tests/render_dashboard.mjs`. Thirteen degraded gracefully. **Four blanked
the page:**

    stack  folders  repos  sites

Each is read as `r.<field>.length` or `.map(...)` with no guard, nineteen call
sites between them, so one absent key throws `TypeError: Cannot read properties
of undefined` and the project table disappears — while `tiles`, `health`, `dups`,
`owner` and `findings`, written earlier in the script, stay on the page. A reader
sees a working dashboard with no projects in it.

`dashboard/smoke.js` passes on the live page because `build_dashboard.py` always
emits all four, so the failure would arrive the day something stopped emitting
one, with every Python test green — the class smoke exists to catch, one level in.

**And it matters outward.** The brief asks that per-project data render inside
fabric, and a payload assembled there may legitimately omit a field it has
nothing to say about. A renderer that throws on that cannot be reused.

Fixed by filling the four ONCE where `D` enters the script rather than guarding
nineteen call sites: one place cannot be half-done. Absent and empty render alike
for these four, which is honest because the display has no third state — a chip
list with nothing in it looks the same either way.
"""
from __future__ import annotations
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                  # noqa: E402
import paths                                                          # noqa: E402

HARNESS = ROOT / "tests/render_dashboard.mjs"
# The built page lives in the workspace, never in the source tree.
PAGE = paths.DASHBOARD_HTML
#: Every optional field a row can carry. Taken from a real row rather than
#: listed from memory, so a field added tomorrow is swept by default.
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def page_data() -> tuple[str, int, int, dict]:
    """The page's text, the bounds of its `const D = …` line, and the data."""
    h = PAGE.read_text(encoding="utf-8")
    i = h.find("const D = {")
    j = h.find("\n", i)
    return h, i, j, json.loads(h[i + len("const D = "):j].rstrip(";"))


def render(rows: list[dict]) -> dict:
    """The page's own script, executed with `D.rows` replaced."""
    h, i, j, d = page_data()
    d = dict(d)
    d["rows"] = rows
    out = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-absent-")) / "page.html"
    out.write_text(h[:i] + "const D = " + json.dumps(d, ensure_ascii=False) + h[j:],
                   encoding="utf-8")
    p = subprocess.run(["node", str(HARNESS), str(out)], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"threw": f"harness produced no JSON: {(p.stdout + p.stderr)[-200:]}"}


def sparse(full: dict, optional: list[str], missing: str | None) -> dict:
    row = {"id": "project:sparse", "name": "sparse", "ownership": "owned"}
    for k in optional:
        if k != missing:
            row[k] = full.get(k)
    return row


# ─────────── the sweep ─────────────────────────────────────────────────

def test_no_absent_field_blanks_the_page() -> None:
    if shutil.which("node") is None:
        print("  NOTE  node is absent, so the page's script cannot be executed "
              "[uncoverable: executing JavaScript is what this sweep is, and the "
              "only two executors here are node]")
        return
    if not PAGE.is_file():
        print("  NOTE  no built page to sweep "
              "[covered: `./observatory.py dashboard` builds it, and the gate "
              "builds it before this step]")
        return
    _, _, _, d = page_data()
    rows = d.get("rows") or []
    if not rows:
        print("  NOTE  the built page carries no project rows "
              "[covered: nothing to sweep without one]")
        return
    full = rows[0]
    # DERIVED from a real row, so a field added tomorrow is swept without
    # anybody remembering to add it here.
    optional = [k for k in sorted(full) if k not in ("id", "name", "ownership")]
    check(f"the sweep has fields to remove ({len(optional)})", len(optional) >= 10,
          str(optional))
    threw = []
    for miss in optional:
        got = render([sparse(full, optional, miss)])
        if got.get("threw"):
            threw.append(f"{miss}: {got['threw'][:60]}")
    check("no single absent field throws", not threw, "; ".join(threw[:4]))


def test_a_row_with_every_optional_field_absent_still_renders() -> None:
    if shutil.which("node") is None or not PAGE.is_file():
        print("  NOTE  nothing to execute here "
              "[covered: the sweep above states the same requirement]")
        return
    _, _, _, d = page_data()
    rows = d.get("rows") or []
    if not rows:
        return
    bare = {"id": "project:sparse", "name": "sparse", "ownership": "owned"}
    got = render([bare])
    check("the script runs", not got.get("threw"), str(got.get("threw"))[:160])
    if got.get("threw"):
        return
    written = {k: v for k, v in (got.get("written") or {}).items() if v}
    check("and the project container is written, not merely the panels above it",
          any(k in written for k in ("out", "rows", "list")),
          f"written: {sorted(written)} — before the fix the row died and every "
          f"panel before it stayed, so the page looked healthy and empty")


def test_the_fill_is_in_one_place() -> None:
    """Nineteen guarded call sites can be nineteen minus one. The fill is where
    `D` enters the script, so it cannot be half-done."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    i = src.find("const D = __DATA__;")
    check("the data entry point is where this suite thinks it is", i != -1, "")
    if i == -1:
        return
    # THE BLOCK IS BOUNDED BY THE FIRST FUNCTION, not by a byte count. A window
    # of 1800 characters said the fill had moved the day a five-line comment was
    # added above it — a true statement about the window and a false one about
    # the code. What the property needs is "before anything is rendered", and
    # the first `function` declaration is where rendering starts.
    m = re.search(r"^function ", src[i:], re.M)
    check("the entry point is followed by the script's first function", bool(m),
          "the fill's neighbourhood cannot be bounded, so this assertion cannot run")
    if not m:
        return
    block = src[i:i + m.start()]
    for field in ("folders", "stack", "repos", "sites"):
        check(f"`{field}` is filled at the entry point",
              f"r.{field} = r.{field} || []" in block,
              "guarding at the call sites instead is what left four of them out")
    # The engine's comment states the reason as the renderers' contract ("every
    # renderer can assume the list fields exist") rather than as the incident.
    check("and the reason is recorded beside it",
          "blank the page" in block or "blanks the page" in block
          or "assume the list fields exist" in block,
          "a fill with no reason is the first thing a later reader removes")


if __name__ == "__main__":
    print("absent fields — one missing key emptied the table\n")
    for fn in (test_no_absent_field_blanks_the_page,
               test_a_row_with_every_optional_field_absent_still_renders,
               test_the_fill_is_in_one_place):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma field the payload does not carry costs that field, not the page\033[0m")
