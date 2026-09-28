#!/usr/bin/env python3
"""A scale claim must carry the date it was measured on.

The rule exists because an entry document once stated the estate's size with a
date five days old and an owner figure that matched neither definition the
registry supports. Dating the claim is the cheap rule that prevents the real
defect — a number with NO date, which reads as current for ever. Demanding
currency instead would turn the gate red every day the estate grows, which is a
chore rather than a check.

Only the checker is driven here. The original suite also verified the original
installation's own handoff section, decision ledger and README figures; those
documents and that registry are not part of the engine, so those cases are not
carried over.
"""
from __future__ import annotations
import pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def test_the_checker_refuses_an_undated_claim() -> None:
    import check_docs
    fn = getattr(check_docs, "scale_claim_failures", None)
    if fn is None:
        check("check_docs.scale_claim_failures exists", False,
              "the cheap rule: a scale claim with no date reads as current for ever")
        return
    dated = "Measured on 2026-01-08: **12 projects across 17 repositories and 3 owners**."
    house = "Measured 2026-01-08: **12 projects across 17 repositories and 3 owners**."
    bare = "It watches **12 projects across 17 repositories and 3 owners**."
    check("a dated claim passes", fn(dated, "README.md") == [], str(fn(dated, "README.md")))
    check("and so does the form the rest of the repository writes",
          fn(house, "README.md") == [], str(fn(house, "README.md")))
    out = fn(bare, "README.md")
    check("an undated claim fails", len(out) == 1, str(out))
    check("and the message says what is missing",
          bool(out) and "date" in out[0].lower(), str(out))
    check("and names the document it was found in", bool(out) and "README.md" in out[0], str(out))
    check("no claim at all is not a failure", fn("nothing here", "README.md") == [], "")


if __name__ == "__main__":
    print("scale claims — a figure without a date reads as current for ever\n")
    for fn in (test_the_checker_refuses_an_undated_claim,):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32man undated scale claim is refused\033[0m")
