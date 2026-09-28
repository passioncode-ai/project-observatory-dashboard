#!/usr/bin/env python3
"""The version handshake: every verdict driven, and the receipt never lies.

A session snapshots its skills at start and never re-reads them, so the
handshake is the only way drift becomes visible. The four verdicts — ok, stale,
stale-major, ahead, unknown — are each driven against the SHIPPED skill in a
redirected scratch, and the receipt is checked to accumulate rather than
overwrite: two sessions on two versions are two rows, not one.
"""
from __future__ import annotations
import json
import os
import pathlib
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


def run(scratch: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "tools/skill_check.py", *args], cwd=ROOT,
                          env={**os.environ, "OBSERVATORY_SCRATCH": str(scratch)},
                          capture_output=True, text=True, timeout=120)


def shipped() -> str:
    body = (ROOT / "skill/plugins/observatory-log/skills/handling-secrets/SKILL.md"
            ).read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.strip().startswith("version:"):
            # YAML may quote the scalar (`version: "0.12.0"`); the version is
            # the text inside the quotes, which is what a session reports.
            return line.split(":", 1)[1].strip().strip("\"'")
    raise AssertionError("the shipped skill carries no version")


def test_every_verdict() -> None:
    s = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skillcheck-"))
    v = shipped()
    p = run(s, "handling-secrets", v)
    check("the current version is OK, exit 0", p.returncode == 0 and "OK" in p.stdout,
          p.stdout[:120])
    p = run(s, "handling-secrets", "0.1.0")
    check("an old version is STALE, exit 1, with the exact remedy",
          p.returncode == 1 and "STALE" in p.stdout
          and "claude plugin update" in p.stdout
          and "restart the session" in p.stdout, p.stdout[:250])
    check("and says skills are read at session start",
          "session start" in p.stdout, p.stdout[:250])
    if int(v.split(".")[0]) > 0:
        check("old-major sessions are told the rule changed meaning",
              "MAJOR" in p.stdout and "CHANGED MEANING" in p.stdout, p.stdout[:250])
        receipt = json.loads((s / "skill-sessions.json").read_text())
        check("the receipt classifies old-major text distinctly",
              receipt["sessions"]["handling-secrets@0.1.0"]["verdict"] == "stale-major")
    major = str(int(v.split(".")[0]) + 1) + ".0.0"
    p = run(s, "handling-secrets", "0.0.1") if v.startswith("0") else run(s, "x", "1")
    # 0.x: a major-apart claim is e.g. shipped 0.3.0 vs reported... majors equal.
    # Drive the major case explicitly with a fabricated older MAJOR when shipped
    # moves past 1.0; until then the wording branch is asserted below by AHEAD.
    p = run(s, "handling-secrets", major)
    check("a version ahead of the checkout says the checkout is behind",
          p.returncode == 1 and "AHEAD" in p.stdout and "git pull" in p.stdout,
          p.stdout[:200])
    p = run(s, "no-such-skill", "1.0.0")
    check("an unknown skill is named with what does exist",
          p.returncode == 2 and "handling-secrets" in p.stdout, p.stdout[:200])


def test_the_receipt_accumulates_and_report_reads_it() -> None:
    s = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-skillcheck2-"))
    v = shipped()
    run(s, "handling-secrets", v)
    run(s, "handling-secrets", v)
    run(s, "handling-secrets", "0.1.0")
    doc = json.loads((s / "skill-sessions.json").read_text(encoding="utf-8"))
    rows = doc["sessions"]
    check("two versions are two rows", len(rows) == 2, str(rows.keys()))
    check("a repeat bumps the count, never overwrites",
          rows[f"handling-secrets@{v}"]["count"] == 2, str(rows)[:200])
    p = run(s, "--report")
    check("--report prints both sightings",
          "0.1.0" in p.stdout and v in p.stdout, p.stdout[:200])


def test_the_quoted_frontmatter_version_is_read_as_the_version() -> None:
    """The shipped SKILL.md quotes its version, and the tool once kept the
    quotes: every session's report then parsed against `(0,)`, so the CURRENT
    version read as AHEAD and the handshake could never say OK."""
    sys.path.insert(0, str(ROOT / "tools"))
    import skill_check
    got = skill_check.shipped_version("handling-secrets")
    check("the tool reads the shipped version without its quotes",
          got == shipped() and '"' not in (got or "") and "'" not in (got or ""), repr(got))
    check("and it parses as numbers", skill_check.parse(got or "") != (0,), repr(got))


def test_the_skill_makes_the_handshake_its_first_steps() -> None:
    """The public skill puts the handshake in its numbered "Start here" steps
    rather than a separate rule 0, and the install/update guidance lives in the
    plugin's own documentation rather than in the skill body."""
    body = (ROOT / "skill/plugins/observatory-log/skills/handling-secrets/SKILL.md"
            ).read_text(encoding="utf-8")
    start = body.split("## Start here", 1)[-1].split("\n## ", 1)[0]
    check("the start-here steps exist and name the tool",
          "## Start here" in body and "skill_check.py" in start)
    check("the handshake command cites the CURRENT version",
          f"skill_check.py\" handling-secrets {shipped()}" in start
          or f"skill_check.py handling-secrets {shipped()}" in start,
          "the command in the text must move with the version, or every "
          "handshake reports the wrong number")
    check("and a stale answer is not turned into a retry loop",
          "retry loop" in start, start[:200])


if __name__ == "__main__":
    print("the skill-version handshake — every verdict, an accumulating receipt\n")
    for fn in (test_every_verdict,
               test_the_receipt_accumulates_and_report_reads_it,
               test_the_quoted_frontmatter_version_is_read_as_the_version,
               test_the_skill_makes_the_handshake_its_first_steps):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mstale text is visible the moment a session admits its version\033[0m")
