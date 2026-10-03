#!/usr/bin/env python3
"""The agent-memory evaluation set runs, and what must already hold, holds.

`tools/memory_eval.py` measures retrieval, abstention, handoffs, injection,
forgetting and freshness on a synthetic corpus. Retrieval and abstention are a
baseline that OBS-03 improves, so they are printed, not gated. Handoff completeness,
the injection boundary and forgetting are properties the engine already promises,
so they are asserted.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tools"))
import memory_eval  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def test_the_evaluation_runs_and_the_promises_hold() -> None:
    out = memory_eval.run()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    check("the corpus is the declared size",
          out["corpus"] == {"records": 40, "questions": 40, "unanswerable": 20}, str(out["corpus"]))
    check("retrieval is measured in both languages",
          set(out["retrieval"]["byLanguage"]) == {"en", "ru"}
          and out["retrieval"]["recall@5"] is not None, str(out["retrieval"]))
    check("every handoff pack is complete", out["handoff"]["passed"] == out["handoff"]["n"],
          str(out["handoff"]))
    check("an injected instruction never becomes a constraint",
          out["injection"]["neverAConstraint"] and out["injection"]["packSaysDataNotInstructions"],
          str(out["injection"]))
    check("a forgotten record is served by no search and no listing",
          out["forgetting"]["search"] and out["forgetting"]["listing"], str(out["forgetting"]))
    check("the evaluation spent nothing", "never spends" in out["search"])


if __name__ == "__main__":
    print("the agent-memory evaluation set\n")
    test_the_evaluation_runs_and_the_promises_hold()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mmeasured; what is promised holds\033[0m")
