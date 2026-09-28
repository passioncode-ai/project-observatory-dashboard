#!/usr/bin/env python3
"""Seven findings cut their own measurements off mid-sentence and read as complete.

Measured 2026-09-07 across `tools/build_findings.py`: seven details joined their
measurements and sliced the result at 300 or 400 characters, **then concatenated
an explanatory sentence after the cut**. So the seam is invisible — the reader
gets a truncated measurement welded to a well-formed sentence:

    "…could not resolve host www.ex. These are NOT reported as dark: a question
     that could not be asked has no negative answer."

That reads as a finished thought about a complete list. A visible ellipsis would
have been better; no truncation at all is better still. Four more sites capped a
LIST (`folders[:3]`, `stale[:3]`, `refused[:3]`, `refused[:4]`) without saying
how many were left out.

**And the file already knew how to do it.** `wiki.broken_link` writes
`", ".join(targets[:6]) + (f" and {len(targets) - 6} more" if …)` — the correct
pattern, in one place out of five. This suite makes that the rule.

The worst case is not hypothetical arithmetic. `model.degraded` joins the merge's
degradation reasons and cuts at 400: the `ownership` reason alone is ~300
characters and **ends with the remedy** ("Add the organisation to OWNED_ORGS if
it is yours"), while `sorted()` places it after the ~230-character
unchecked-transfer reason. One of each therefore drops the only actionable
sentence in the finding. That same detail also passed the reasons through
`sorted({d["reason"] …})` — a set of reasons, discarding `d["source"]` — so a
finding titled "3 source(s) unmeasured" named none of the three.
"""
from __future__ import annotations
import importlib, json, os, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
import source_reader                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── the two helpers ───────────────────────────────────────────

def test_clipped_says_when_it_clipped() -> None:
    import build_findings as B
    importlib.reload(B)
    short = "a measurement that fits"
    check("text under the limit is untouched", B.clipped(short, 300) == short, "")
    long = "x" * 350
    got = B.clipped(long, 300)
    check("text over the limit is marked", got != long[:300], got[-60:])
    check("and the marker counts what was dropped", "50" in got, got[-60:])
    check("the kept part is still the beginning",
          got.startswith("x" * 200), got[:40])
    check("an empty string survives", B.clipped("", 300) == "", "")
    check("exactly at the limit is not marked",
          B.clipped("y" * 300, 300) == "y" * 300, "")


def test_listed_says_how_many_it_left_out() -> None:
    import build_findings as B
    importlib.reload(B)
    check("a short list is joined whole",
          B.listed(["a", "b"], 3) == "a, b", B.listed(["a", "b"], 3))
    got = B.listed(["a", "b", "c", "d", "e"], 2)
    check("a long list is cut", got.startswith("a, b"), got)
    check("and says how many are missing", "3 more" in got, got)
    check("an empty list is empty", B.listed([], 3) == "", "")
    check("the separator is honoured",
          B.listed(["a", "b"], 5, sep="; ") == "a; b", "")


# ─────────── the invariant that stops the eighth instance ──────────────

#: Numeric slices that are NOT truncations of content, each with the reason it is
#: allowed. A slice absent from this table is a silent cap, and the check below
#: is what makes adding one a decision rather than a habit.
ALLOWED_SLICES = {
    "[:7]": "a year-month prefix of an ISO date, not a shortened measurement",
    "[:8]": "a short session id, the length the rest of the estate displays",
    "[:10]": "the date half of an ISO timestamp",
    "[:12]": "a content hash's displayed length — the same twelve hex digits "
             "`tools/registry_shape.py` and `docs/AGENT_SYNC.md` stamp with. It "
             "shortens an identifier, not a measurement: a reader compares it "
             "with another stamp, and both ends are cut the same way",
    "[:4]": "the four highest-traffic hosts the operator has ruled outside the "
            "estate, named with their reasons in ONE aggregated board row whose "
            "title carries the full count — a cut for reading, hiding no number "
            "",
    "[:16]": "the `YYYY-MM-DDTHH:MM` head of an ISO timestamp, printed beside a "
             "Heroku release so a reader can find it in `heroku releases` — a "
             "display cut of a stamp whose seconds carry nothing, never a "
             "measurement",
    "[:13]": "the `YYYY-MM-DDTHH` head of a compact archive stamp being "
             "rehydrated into ISO with colons re-inserted — a parse, not a cut: "
             "the tail is consumed by the [13:15] and [15:17] slices beside it "
             "",
}


def test_no_numeric_slice_hides_a_measurement() -> None:
    """A source-level closure, in the shape used for the clone states:
    the set of allowed slices is DECLARED, so the next `[:300]` fails here
    instead of shipping a finding that reads as complete."""
    raw = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    code = source_reader.code_only(raw)
    found: dict[str, int] = {}
    for line in code.splitlines():
        for m in re.finditer(r'\[:\s*(\d+)\s*\]', line):
            key = f"[:{m.group(1)}]"
            found[key] = found.get(key, 0) + 1
    undeclared = sorted(k for k in found if k not in ALLOWED_SLICES)
    check("every numeric slice left in the file is a declared prefix",
          not undeclared, f"undeclared: {undeclared} (counts {found})")
    check("and each allowance carries its reason",
          all(len(v.split()) >= 6 for v in ALLOWED_SLICES.values()), "")


def test_the_file_routes_its_long_details_through_the_helper() -> None:
    raw = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("`clipped` is used, not just defined", raw.count("clipped(") >= 6,
          f"{raw.count('clipped(')} call(s)")
    check("and `listed` too", raw.count("listed(") >= 4,
          f"{raw.count('listed(')} call(s)")


# ─────────── driven: the finding that dropped its own remedy ───────────

def findings_for(degraded: list[dict]) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-trunc-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    (d / "scratch/model.json").write_text(json.dumps({"degraded": degraded}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"] == "model.degraded"]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


#: The live shapes, at their real lengths — the arithmetic that made the 400-char
#: cut drop the remedy rather than a tail nobody needed.
TRANSFER_REASON = (
    "could not check whether old-org/alpha-web has moved (`gh` is not installed, "
    "so no transfer could be followed), and no earlier run had answered. It is "
    "recorded as it stands: if the clone's remote is stale, this is a repository "
    "that does not exist")
OWNERSHIP_REASON = (
    "the GitHub listing returned repositories under acme-corp, which OWNED_ORGS in "
    "collectors/merge.py does not declare. Their projects are reported as "
    "`external` and estate.py will refuse to record sessions for them. Add the "
    "organisation to OWNED_ORGS if it is yours — that is an operator's decision, "
    "not a collector's")


def test_the_remedy_is_no_longer_the_part_that_gets_cut() -> None:
    got = findings_for([{"source": "transfer:old-org/alpha-web",
                         "reason": TRANSFER_REASON},
                        {"source": "ownership", "reason": OWNERSHIP_REASON}])
    check("the finding fires", len(got) == 1, str(got)[:200])
    if not got:
        return
    d = got[0]["detail"]
    check("the two reasons together exceed the old 400-char cut",
          len(TRANSFER_REASON) + len(OWNERSHIP_REASON) > 400,
          f"{len(TRANSFER_REASON)} + {len(OWNERSHIP_REASON)}")
    check("the remedy survives", "OWNED_ORGS if it is yours" in d, d[-200:])
    check("and so does the other reason's consequence",
          "a repository that does not exist" in d, d[:200])


def test_a_truncation_that_does_happen_is_visible() -> None:
    got = findings_for([{"source": f"transfer:x{i}", "reason": TRANSFER_REASON}
                        for i in range(12)])
    check("the finding fires", len(got) == 1, str(got)[:160])
    if not got:
        return
    d = got[0]["detail"]
    check("the detail is bounded", len(d) < 3000, str(len(d)))
    check("and the cut announces itself",
          "not shown" in d or "more character" in d, d[-160:])


def test_the_sources_are_named_not_only_counted() -> None:
    """A finding titled "3 source(s) unmeasured" that names none of the three
    sends the reader to the model file to find out what it already knew."""
    got = findings_for([{"source": "transfer:old-org/alpha-web",
                         "reason": TRANSFER_REASON},
                        {"source": "ownership", "reason": OWNERSHIP_REASON}])
    if not got:
        check("a finding to inspect", False, "nothing fired")
        return
    d = got[0]["detail"]
    check("the transfer source is named", "transfer:old-org/alpha-web" in d,
          d[:220])
    check("and the ownership source is named", "ownership" in d, d[:220])
    check("the count is still in the title", "2 source" in got[0]["title"],
          got[0]["title"])


def test_two_identical_reasons_from_two_sources_are_both_kept() -> None:
    """The old `sorted({reason})` deduplicated on the reason alone, so two
    different addresses failing the same way collapsed into one line and one of
    them vanished."""
    got = findings_for([{"source": "transfer:a/one", "reason": "`gh` is not installed"},
                        {"source": "transfer:b/two", "reason": "`gh` is not installed"}])
    if not got:
        check("a finding to inspect", False, "nothing fired")
        return
    d = got[0]["detail"]
    check("both addresses appear", "a/one" in d and "b/two" in d, d[:240])
    check("and the count is two", "2 source" in got[0]["title"], got[0]["title"])


if __name__ == "__main__":
    print("no silent truncation — seven findings that read as complete\n")
    for fn in (test_clipped_says_when_it_clipped,
               test_listed_says_how_many_it_left_out,
               test_no_numeric_slice_hides_a_measurement,
               test_the_file_routes_its_long_details_through_the_helper,
               test_the_remedy_is_no_longer_the_part_that_gets_cut,
               test_a_truncation_that_does_happen_is_visible,
               test_the_sources_are_named_not_only_counted,
               test_two_identical_reasons_from_two_sources_are_both_kept):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mno finding cuts its own measurement without saying so\033[0m")
