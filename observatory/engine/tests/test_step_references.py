#!/usr/bin/env python3
"""Every file the step list names exists, and every test suite it names is run.

observatory.py's STEPS map a step name to the program it runs. When the engine
was first exported, 124 of the 206 files those steps named were left behind, and
a step that points at nothing looks exactly like a step that exists until someone
runs it. This suite reads the step list as source and fails on the first such
reference. It also fails when a named test suite exists but the portable runner
does not run it, because a suite nobody runs is only a comment.

Nothing outside the engine tree is read.
"""
from __future__ import annotations
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAILURES: list[str] = []
REFERENCE = re.compile(
    r"""["']((?:tests|tools|collectors|dashboard|agent|mcp|plugins|fabric)/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|js))["']""")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def referenced_files(source: str) -> list[str]:
    return sorted(set(REFERENCE.findall(source)))


def portable_suites(runner_source: str) -> set[str]:
    """The suite names the portable runner runs, read without importing it."""
    names: dict[str, list[str]] = {}
    for node in ast.parse(runner_source).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Tuple):
            names[node.targets[0].id] = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and isinstance(node.value, ast.Tuple):
            names.setdefault(node.target.id, []).extend(
                e.value for e in node.value.elts if isinstance(e, ast.Constant))
    return set(names.get("LEGACY", [])) | set(names.get("BOUNDARY", []))


def test_every_referenced_file_exists() -> None:
    refs = referenced_files((ROOT / "observatory.py").read_text(encoding="utf-8"))
    check("the step list names files at all", len(refs) > 100, f"{len(refs)} references")
    missing = [r for r in refs if not (ROOT / r).is_file()]
    check("every file the step list names exists", not missing, ", ".join(missing[:10]))


def test_every_named_suite_is_run() -> None:
    refs = referenced_files((ROOT / "observatory.py").read_text(encoding="utf-8"))
    suites = portable_suites((ROOT / "tests" / "run_portable.py").read_text(encoding="utf-8"))
    named = sorted({pathlib.Path(r).stem[len("test_"):] for r in refs
                    if r.startswith("tests/test_") and r.endswith(".py") and (ROOT / r).is_file()})
    check("the step list names test suites", len(named) > 50, f"{len(named)} suites")
    not_run = [n for n in named if n not in suites]
    check("every suite the step list names is run by the portable runner", not not_run,
          ", ".join(not_run[:10]))


def test_the_guard_sees_a_planted_defect() -> None:
    """The guard itself: a reference to a file that does not exist is caught."""
    planted = 'STEPS["ghost"] = [PY, "tests/test_ghost_suite_that_is_not_there.py"]\n'
    refs = referenced_files(planted)
    check("a planted reference is read", refs == ["tests/test_ghost_suite_that_is_not_there.py"], str(refs))
    check("a planted reference is reported missing", not (ROOT / refs[0]).is_file())
    planted_runner = "LEGACY = ('a',)\nBOUNDARY = ('b',)\nBOUNDARY += ('c',)\n"
    check("augmented suite lists are counted", portable_suites(planted_runner) == {"a", "b", "c"},
          str(sorted(portable_suites(planted_runner))))


if __name__ == "__main__":
    print("step references — every file the step list names exists and is run\n")
    test_the_guard_sees_a_planted_defect()
    test_every_referenced_file_exists()
    test_every_named_suite_is_run()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("the step list names only files that exist, and the runner runs every suite it names")
