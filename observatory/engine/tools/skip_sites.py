#!/usr/bin/env python3
"""Where a suite can drop assertions, and whether it says who covers them.

85 sites across 43 suites print `SKIP` or `NOTE` (measured 2026-09-08; 49 across 27 when this was written, and the growth is suites added since, not assertions abandoned) — a block of assertions
announcing that it did not run. Exactly ONE names where the property is asserted
instead (measured 2026-09-07):

    tests/test_footprint.py   NOTE  no live `host.disk_low`: the volume has room,
                                    so the reclaimable-holders wording is not
                                    driven here — see tests/test_delivery.py for
                                    the planted case

That message is the convention this file measures. A skip is legitimate when the
property lives in another suite and a HOLE when it does not, and the difference
is invisible from the message in 48 of 49 cases. `observatory.py` now counts the
blocks a RUN skipped; this counts the places a run could, which is the larger
number and the one that does not depend on today's machine.

**It reports and never edits.** Forty-eight messages are forty-eight judgements
about whether a property is covered elsewhere, and a script cannot make them.
`gate.skips_uncovered` in `tools/build_findings.py` carries the count so the
number is pressure rather than a mass rewrite.

**Read as text, with balanced parentheses.** The first version matched
`print\\(f?"  (SKIP|NOTE)  ([^"]*)"` and reported 0 of 49 — because the one good
message spans two string literals across two lines, so `[^"]*` stopped at the
first closing quote and the only site that satisfies the convention was the only
site the measurement could not see. An instrument whose blind spot is exactly the
thing it looks for reports zero and sounds certain.

    skip_sites.py            the table, and the count
    skip_sites.py --json     the same, machine-readable
"""
from __future__ import annotations
import argparse, ast, json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The marker as a suite prints it. Anchored the same way `observatory.py`'s
#: `SKIP_MARKER` is, and for the same reason: a PASS line may name the word.
CALL_RX = re.compile(r'print\(f?"  (SKIP|NOTE)')

#: What counts as naming the cover. TWO forms, and the first is a MARKER rather
#: than prose:
#:
#:     [covered: the payload cases above]
#:     [covered: tests/test_delivery.py plants the case]
#:
#: The first version accepted only a path to another suite or the word
#: "planted", and measuring my own twelve suites showed why that is too narrow:
#: the commonest legitimate cover is elsewhere in the SAME file — the fixture
#: cases that do not need `node`, or a live estate — and none of those messages
#: could say so in a way this file recognised. A rule that cannot see the
#: commonest legitimate form pushes authors toward worse wording.
#:
#: Widening it into a prose classifier was the other option and was refused:
#: "fixture" alone matches "no fixture here", which is the opposite claim. A
#: bracketed marker is unambiguous, and the sentence around it stays readable.
#:
#: A bare path to another suite is still accepted, because it was never
#: ambiguous and eleven sites already use it.
#: THREE forms now. Acting on the count produced a case the first two could not
#: express: the five `node is not installed here` skips in
#: `tests/test_dashboard_render.py` guard blocks that EXECUTE the page, and the
#: only two executors in this repository — `dashboard/smoke.js` and
#: `tests/render_dashboard.mjs` — are both node. `[covered: …]` would be false
#: and silence would leave them counted for ever, so `[uncoverable: why]` says
#: what is true: nothing carries this, and here is the reason.
#:
#: What the count then measures is the UNEXAMINED — sites where nobody has judged
#: either way — which is the only figure a judgement can move.
#: FOUR forms now, and the classifier below is the only reader. A `COVER_RX`
#: regex stood here until 2026-09-08 governing nothing — `cover_kind` had been
#: rewritten to test the markers directly — while `tests/test_gate_skips.py`
#: went on asserting the regex's behaviour. A green test over a dead declaration
#: is an assertion about what the code SAYS rather than what it does, which is
#: the class this session has now hit eight times, so the regex is gone and the
#: suite drives `cover_kind`.




#: What a skip can be waiting for that no fixture can supply — a fact about the
#: machine. The phrase beside each is the one the suites actually print, measured
#: 2026-09-08 across 32 unexamined sites; fourteen matched.
#:
#: Four of these ten are already `observatory.capabilities()`'s own vocabulary,
#: which `NEEDS` uses to skip a whole STEP. That mechanism is not extended to the
#: rest on purpose: `test_plugins` skips ONE block for a missing store and runs
#: forty other assertions, so declaring the step as needing the store would lose
#: them — and adding six probes no step consumes would be a measurement with no
#: reader.
CAPABILITY_REASONS: dict[str, str] = {
    "store": "no store|no live store|no store or registry",
    "node": r"\bnode\b",
    "jsonschema": "jsonschema",
    "sqlite-vec": "sqlite-vec",
    "embedding-key": "no embedding key",
    "launchd-job": "launchd",
    "agent-sync": "agent-sync",
    "sqlite-drop-column": "cannot DROP COLUMN",
    "claude-cli": "claude CLI",
    "estate": "estate is not on this machine",
}

CAPABILITY_RX = re.compile("|".join(CAPABILITY_REASONS.values()), re.I)


def cover_kind(message: str) -> str:
    """`named`, `uncoverable`, `capability`, `gap`, or `none`.

    ONE field, five states, and the count that reaches the operator's board uses
    only the last: a site whose block did not run because this machine lacks
    `sqlite-vec` is completely explained, and no judgement moves it.

    **`gap` was the state that had nowhere to go**, and its absence made the
    other four dangerous. Working the seventeen unexamined sites on 2026-09-08
    produced one whose property IS assertable — four suites already insert a
    `scans` row — but only behind a fixture that is a piece of work in itself.
    With four classes the only ways to record that judgement were to call it
    `covered` (a lie), to call it `uncoverable` (also a lie, and the more
    damaging one, because it closes the question), or to leave it `none` and
    lose the judgement entirely. So a judged, still-open gap says so and names
    what would close it, and the board reports it separately: the unexamined
    count stays a count of things nobody has looked at, which is the only thing
    that makes it worth reading.
    """
    if "[covered:" in message:
        return "named"
    if "[uncoverable:" in message:
        return "uncoverable"
    if "[gap:" in message:
        return "gap"
    if re.search(r"tests/test_[a-z_]+\.py", message):
        return "named"
    if CAPABILITY_RX.search(message):
        return "capability"
    return "none"


def _marker_of(node) -> tuple[str, str] | None:
    """(kind, message) if this statement is a marker print, else None."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    fn = node.value.func
    if not (isinstance(fn, ast.Name) and fn.id == "print") or not node.value.args:
        return None
    parts: list[str] = []
    for arg in node.value.args:
        for piece in ast.walk(arg):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
    text = " ".join(parts).strip()
    m = re.match(r"(SKIP|NOTE)\s+(.*)", text, re.S)
    if not m:
        return None
    return m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()


def site_kinds(src: str) -> list[dict]:
    """Every marker print, with `kind_of_site` — `skip` or `annotation`.

    A SKIP SITE is one where the block is abandoned: a `return` or `continue`
    follows the print inside the same statement list, so the assertions after it
    never run. An ANNOTATION has no such exit and the assertions do run.

    Measured while acting on the count this file produces:
    `tests/test_filesystem_scan.py:368` prints a NOTE, reassigns a variable, and
    falls straight into two `check(...)` calls — one of which says "nothing in
    the set comparison above was lost". Three of that file's six counted sites
    are of that kind, and the board was reporting all of them as places
    assertions can vanish.

    Structure rather than wording, and the AST rather than indentation: these
    prints span two and three lines, so no line-based reader could have answered this.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[dict] = []
    for parent in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if not isinstance(block, list):
                continue
            for i, node in enumerate(block):
                got = _marker_of(node)
                if got is None:
                    continue
                kind, message = got
                after = block[i + 1:]
                abandons = any(
                    isinstance(later, (ast.Return, ast.Continue, ast.Break))
                    for later in after)
                out.append({
                    "line": node.lineno,
                    "kind": kind,
                    "message": message,
                    "kind_of_site": "skip" if abandons else "annotation",
                    "cover_kind": cover_kind(message),
                })
    return sorted(out, key=lambda r: r["line"])


def _call_text(src: str, start: int) -> str:
    """The whole `print(...)` call from its opening bracket, by balancing."""
    i = src.index("(", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return src[i:]


def survey(where: pathlib.Path | None = None) -> list[dict]:
    """One row per site: file, marker, message, verdict, and which KIND it is.

    Read through `site_kinds`, which parses rather than scans — the regex reader
    this replaced could not tell a skipped block from a note printed beside
    assertions that run, and the board carried the sum of both as though every
    one were a place assertions vanish.
    """
    where = where or (ROOT / "tests")
    out: list[dict] = []
    for f in sorted(where.glob("test_*.py")):
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # A directory outside the program tree (a planted fixture tree, say) is
        # named as given rather than crashing on `relative_to`.
        name = str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f)
        for row in site_kinds(src):
            out.append({"file": name, **row})
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args(argv)
    sites = survey()
    named = [s for s in sites if s["cover_kind"] != "none"]
    if args.json:
        print(json.dumps({"sites": sites, "total": len(sites),
                          "explained": len(named)}, ensure_ascii=False, indent=1))
        return 0
    skips = [s for s in sites if s["kind_of_site"] == "skip"]
    notes = [s for s in sites if s["kind_of_site"] != "skip"]
    named = [s for s in skips if s["cover_kind"] != "none"]
    caps = [s for s in skips if s["cover_kind"] == "capability"]
    print(f"{len(skips)} site(s) where a block of assertions is SKIPPED — the print "
          f"is followed by a return or continue — in "
          f"{len({s['file'] for s in skips})} suite(s).")
    print(f"{len(named)} are explained: named cover, uncoverable, or waiting on a "
          f"machine capability ({len(caps)} of them).")
    if notes:
        print(f"{len(notes)} further marker(s) annotate a run whose assertions do "
              f"run; they are listed but not counted as skips.")
    print()
    for s in sites:
        if s["kind_of_site"] != "skip":
            mark = " note "
        else:
            mark = {"named": "cover", "uncoverable": "uncov",
                    "capability": " cap ", "none": "  -  "}[s["cover_kind"]]
        print(f"  {mark}  {s['file']}:{s['line']:<4} {s['kind']}  {s['message'][:70]}")
    if len(named) < len(skips):
        print(f"\n{len(skips) - len(named)} skip site(s) say nothing about who carries the "
              f"property. That is a judgement per site, not something this file can "
              f"decide — `gate.skips_uncovered` keeps the number visible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
