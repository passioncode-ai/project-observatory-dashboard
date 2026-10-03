#!/usr/bin/env python3
"""Two failures that are ANSWERS, recognised by git's English.

`collectors/scan_filesystem.py` turns two git failures into facts about the tree
rather than faults of the run: a repository with no `origin`, and one with no
commits. Its own comment says why — recording those as degradations "would put a
permanent warning in front of the operator for a tree that is exactly as
intended, which is how a findings list becomes noise nobody reads" — and both
are recognised by matching git's message text.

**Git translates its messages.** Measured in a repository with no remote and no
commits:

    LC_ALL=C              error: No such remote 'origin'
                          fatal: ambiguous argument 'HEAD': unknown revision …
    LC_ALL=ru_RU.UTF-8    error: Нет такого внешнего репозитория «origin»
                          fatal: неоднозначный аргумент «HEAD»: неизвестная …

So on a machine whose locale is not English, neither branch matches: both
repositories become degradations, the operator gets a permanent warning about
trees that are exactly as intended, and nothing says the recogniser stopped
recognising. On a machine set to Russian it was one environment variable
away.

`sh()` now pins `LC_ALL=C` and empties `LANGUAGE`, and this suite is the watched
failure: it drives the real recogniser inside a Russian-locale environment and
requires the classification to hold. Remove the pin and these cases go red.
"""
from __future__ import annotations
import importlib.util
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                  # noqa: E402

FAILURES: list[str] = []
RU = "ru_RU.UTF-8"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def scanner():
    """The module by path, WITHOUT running it.

    `collectors/scan_filesystem.py` has no `__main__` guard and does its work at
    module level, so `import scan_filesystem` would run a live scan — the hazard
    `tests/test_gate_purity.py` refuses for every suite. Loading the
    spec without executing it gives access to nothing; what this suite needs is
    the SOURCE for `sh`, so it compiles just that function in isolation.
    """
    src = (ROOT / "collectors/scan_filesystem.py").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    import slow_command  # `sh` asks a timed-out command again through it
    import safe_git      # and composes every git call through the engine's one door
    ns: dict = {"subprocess": subprocess, "os": os, "slow_command": slow_command,
                "safe_git": safe_git}
    start = src.index("GIT_ENV = ")
    end = src.index("MARKERS = [")
    exec(compile(src[start:end], "scan_filesystem:sh", "exec"), ns)   # noqa: S102
    return ns


def bare_repo() -> pathlib.Path:
    """A repository with no remote and no commits — both states at once."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-locale-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], capture_output=True,
                   text=True, timeout=300)
    return d


# ─────────── git really does translate ─────────────────────────────────

def test_git_translates_the_messages_the_code_matches() -> None:
    """The premise, measured rather than assumed. If this ever stops being true
    the pin is harmless and this case says so."""
    d = bare_repo()
    ru = subprocess.run(["git", "remote", "get-url", "origin"], cwd=d,
                        env={**os.environ, "LC_ALL": RU, "LANGUAGE": "ru"},
                        capture_output=True, text=True, timeout=300).stderr
    en = subprocess.run(["git", "remote", "get-url", "origin"], cwd=d,
                        env={**os.environ, "LC_ALL": "C", "LANGUAGE": ""},
                        capture_output=True, text=True, timeout=300).stderr
    if ru.strip() == en.strip():
        print("  NOTE  this git does not translate its messages here "
              "[uncoverable: the locale files are not installed, and the pin is "
              "then belt without braces — the cases below still hold]")
        return
    check("the English form is what the code matches",
          "No such remote" in en, en.strip()[:120])
    check("and the localised form is not", "No such remote" not in ru, ru.strip()[:120])


# ─────────── the recogniser holds under a foreign locale ───────────────

def test_a_missing_remote_is_an_answer_under_a_russian_locale() -> None:
    ns = scanner()
    d = bare_repo()
    old = {k: os.environ.get(k) for k in ("LC_ALL", "LANG", "LANGUAGE")}
    try:
        os.environ.update(LC_ALL=RU, LANG=RU, LANGUAGE="ru")
        out, why = ns["sh"](["git", "remote", "get-url", "origin"], cwd=d)
        check("the command still fails, as it must", bool(why), f"{out!r}")
        check("and the reason is the ENGLISH the caller matches",
              "No such remote" in why, why[:160])
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_an_unborn_head_is_an_answer_under_a_russian_locale() -> None:
    ns = scanner()
    d = bare_repo()
    old = {k: os.environ.get(k) for k in ("LC_ALL", "LANG", "LANGUAGE")}
    try:
        os.environ.update(LC_ALL=RU, LANG=RU, LANGUAGE="ru")
        out, why = ns["sh"](["git", "rev-parse", "HEAD"], cwd=d)
        check("the command still fails", bool(why), f"{out!r}")
        # The caller accepts any of three phrases for this state; the pin has to
        # make at least the one git actually prints reachable.
        check("and one of the phrases the caller accepts is present",
              any(x in why for x in ("unknown revision", "does not have any commits",
                                     "ambiguous argument 'HEAD'")), why[:160])
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_pin_is_declared_where_a_reader_will_meet_it() -> None:
    src = (ROOT / "collectors/scan_filesystem.py").read_text(encoding="utf-8")
    check("`GIT_ENV` exists", "GIT_ENV = " in src, "")
    check("it pins LC_ALL", '"LC_ALL": "C"' in src, "")
    check("and empties LANGUAGE, which gettext lets override LC_ALL",
          '"LANGUAGE": ""' in src, "")
    check("and `sh` passes it to every git it runs",
          # git's own environment is composed by `safe_git`; the pin rides on it.
          "extra_env=GIT_ENV" in src and "{**os.environ, **GIT_ENV}" in src,
          "a pin the runner does not use is a pin that does nothing")
    i = src.find("GIT_ENV = ")
    check("with the measurement that made it necessary quoted above it",
          "ru_RU" in src[max(0, i - 1400):i],
          "a pin with no reason is the first thing a later reader deletes")


if __name__ == "__main__":
    print("git's locale — two failures that are answers, matched in English\n")
    for fn in (test_git_translates_the_messages_the_code_matches,
               test_a_missing_remote_is_an_answer_under_a_russian_locale,
               test_an_unborn_head_is_an_answer_under_a_russian_locale,
               test_the_pin_is_declared_where_a_reader_will_meet_it):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe answers stay answers whatever the machine speaks\033[0m")
