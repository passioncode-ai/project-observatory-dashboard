#!/usr/bin/env python3
"""The derivation that keeps a trap registry honest, driven against defects.

`tools/trap_map.py` replaced a hand-written table that had drifted several ways
at once. A derivation nobody has watched reject anything is worth no more than
the table it replaced, so every rule below is driven against a planted
violation in a temporary tree — a trap with no guard, a guard for a trap nobody
declared, a guard defined and never dispatched.

The registry of recorded failures itself (`docs/knowledge-pack.md`) is not
part of this distribution, so the half that asserted the LIVE mapping is
current is replaced by its honest counterpart: with no registry the tool says
so by name and exits 0, and declares nothing. The planted half is unchanged:
it builds a complete synthetic repository and asserts the rules can still fire,
because a check whose subject has moved passes forever and says nothing.
"""
from __future__ import annotations
import importlib.util
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                               # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load():
    spec = importlib.util.spec_from_file_location("trap_map_t", ROOT / "tools/trap_map.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_an_absent_registry_declares_nothing_and_says_so() -> None:
    """No registry is not drift: the distribution names what it lacks."""
    tm = load()
    check("the program checkout carries no trap registry", not tm.PACK.is_file(),
          "this suite's live half assumes the registry is not shipped")
    data, _ = tm.report()
    check("so no trap is declared", data["declared"] == {}, str(data["declared"])[:120])


def test_every_guard_is_dispatched() -> None:
    """Defined and never called is the purest form of a check that cannot fire.

    This half needs no registry: a marker in a suite is a claim about that
    suite's own `__main__`, whatever the registry says.
    """
    tm = load()
    data, _ = tm.report()
    silent = [f"{g['file']}:{g['function']}" for gs in data["by_trap"].values()
              for g in gs if not g["dispatched"]]
    check("every guard is dispatched from its suite's __main__", not silent, str(silent))


# ─────────────────────────── the planted half ────────────────────────────────

def plant(pack_rows: str, suite: str) -> tuple[dict, list[str]]:
    """Run the derivation against a temporary repository of two files.

    The tool resolves everything from its own location, so a copy of it beside
    a `docs/` and a `tests/` of our making is a complete fixture — no live file
    is read and none is written.
    """
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-trapmap-"))
    (tmp / "docs").mkdir()
    (tmp / "tests").mkdir()
    (tmp / "tools").mkdir()
    (tmp / "docs/knowledge-pack.md").write_text(
        "| # | Trap | Fixture |\n|---|---|---|\n" + pack_rows +
        f"\n{load().BEGIN}\n{load().END}\n", encoding="utf-8")
    (tmp / "tests/test_planted.py").write_text(suite, encoding="utf-8")
    (tmp / "observatory.py").write_text(
        'STEPS = {"test-planted": ["py", "tests/test_planted.py"]}\n'
        'GROUPS = {"check": ["test-planted"]}\n', encoding="utf-8")
    (tmp / "atomic.py").write_text(
        "import pathlib\n"
        "def write_text(p, s): pathlib.Path(p).write_text(s, encoding='utf-8')\n",
        encoding="utf-8")
    src = (ROOT / "tools/trap_map.py").read_text(encoding="utf-8")
    (tmp / "tools/trap_map.py").write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"tm_{tmp.name}", tmp / "tools/trap_map.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.report()


GOOD_SUITE = ('def test_one():\n    """A guard.\n\n    Trap: T1\n    """\n\n\n'
              'if __name__ == "__main__":\n    test_one()\n')


def test_a_trap_with_no_guard_is_reported() -> None:
    _, drift = plant("| T1 | **A.** story | fixture |\n| T2 | **B.** story | fixture |\n",
                     GOOD_SUITE)
    check("an unguarded trap is named",
          any("T2 is declared in the pack and no test declares it" in d for d in drift),
          str(drift))


def test_a_guard_for_an_undeclared_trap_is_reported() -> None:
    _, drift = plant("| T1 | **A.** story | fixture |\n",
                     GOOD_SUITE.replace("T1", "T9"))
    check("a guard the pack never declared is named",
          any("T9" in d and "declares no such trap" in d for d in drift), str(drift))


def test_a_guard_that_is_never_dispatched_is_reported() -> None:
    _, drift = plant("| T1 | **A.** story | fixture |\n",
                     GOOD_SUITE.replace("    test_one()", "    pass"))
    check("a guard defined and never called is named",
          any("never dispatched" in d for d in drift), str(drift))


def test_the_check_exits_non_zero_on_drift() -> None:
    """The step, not the function — a rule that only reports is not a gate.

    Driven in the planted tree: the same drift `report()` names above must turn
    `--check` red, and a clean planted tree must leave it green.
    """
    def cli(pack_rows: str, suite: str) -> subprocess.CompletedProcess:
        tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-trapmap-cli-"))
        for sub in ("docs", "tests", "tools"):
            (tmp / sub).mkdir()
        m = load()
        (tmp / "docs/knowledge-pack.md").write_text(
            "| # | Trap | Fixture |\n|---|---|---|\n" + pack_rows +
            f"\n{m.BEGIN}\n{m.END}\n", encoding="utf-8")
        (tmp / "tests/test_planted.py").write_text(suite, encoding="utf-8")
        (tmp / "observatory.py").write_text(
            'STEPS = {"test-planted": ["py", "tests/test_planted.py"]}\n'
            'GROUPS = {"check": ["test-planted"]}\n', encoding="utf-8")
        (tmp / "atomic.py").write_text(
            "import pathlib\n"
            "def write_text(p, s): pathlib.Path(p).write_text(s, encoding='utf-8')\n",
            encoding="utf-8")
        (tmp / "tools/trap_map.py").write_text(
            (ROOT / "tools/trap_map.py").read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run([PY, str(tmp / "tools/trap_map.py"), "--write"], cwd=tmp,
                       capture_output=True, text=True, timeout=300)
        return subprocess.run([PY, str(tmp / "tools/trap_map.py"), "--check"],
                              cwd=tmp, capture_output=True, text=True, timeout=300)

    p = cli("| T1 | **A.** story | fixture |\n", GOOD_SUITE)
    check("--check passes on a clean planted tree", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    check("and says what it verified", "trap map current" in p.stdout, p.stdout[-200:])
    p = cli("| T1 | **A.** story | fixture |\n| T2 | **B.** story | fixture |\n", GOOD_SUITE)
    check("--check fails on an unguarded trap", p.returncode != 0,
          (p.stdout + p.stderr)[-300:])


def test_an_absent_registry_is_named_not_failed() -> None:
    p = subprocess.run([PY, str(ROOT / "tools/trap_map.py"), "--check"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("--check exits 0 without a registry", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    check("and names what is missing", "no trap registry" in p.stdout, p.stdout[-200:])


def test_the_tool_states_what_it_does_not_prove() -> None:
    """A convention with no written form is one the next guard will get wrong."""
    tool = (ROOT / "tools/trap_map.py").read_text(encoding="utf-8")
    check("the tool shows the marker a guard must carry", "Trap: T12" in tool)
    check("and states what the derivation does NOT prove",
          "does NOT prove EFFICACY" in tool or "not prove efficacy" in tool.lower())


def test_the_marker_regex_accepts_both_forms() -> None:
    tm = load()
    for form, want in (("Trap: T12", ["T12"]),
                       ("Traps: T22, T23", ["T22", "T23"]),
                       ("Trap: T4 — the whole defect", ["T4"])):
        m = tm.MARKER.search(f"a docstring\n\n    {form}\n\n    and more\n")
        got = re.findall(r"T\d+", m.group(1)) if m else []
        check(f"{form!r} parses", got == want, str(got))
    check("prose about a trap is not a declaration",
          tm.MARKER.search("This is about the T12 trap and its fixture.") is None)


if __name__ == "__main__":
    print("trap map — the derivation, and the defects it must catch\n")
    for fn in (test_an_absent_registry_declares_nothing_and_says_so,
               test_every_guard_is_dispatched, test_a_trap_with_no_guard_is_reported,
               test_a_guard_for_an_undeclared_trap_is_reported,
               test_a_guard_that_is_never_dispatched_is_reported,
               test_the_check_exits_non_zero_on_drift,
               test_an_absent_registry_is_named_not_failed,
               test_the_tool_states_what_it_does_not_prove,
               test_the_marker_regex_accepts_both_forms):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mtrap map ok\033[0m")
