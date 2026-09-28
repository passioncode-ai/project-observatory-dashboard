#!/usr/bin/env python3
"""A step of the tick that fails, and who hears about it.

`store/retention.py apply` returns 1 when an erasure did not complete — a
derived index still holds a tombstoned revision, or could not be checked at all.
That guarantee is the contract's: *"completion blocks until every configured
backend attests"*. The function honours it and `tests/test_retention.py` drives
the refusal.

**The system did not.** `tools/tick.sh` piped that step's output through its
logger and never looked at what the pipeline returned — `set -o pipefail` is on,
so the value was there and simply discarded. Nearly half of the tick's steps
were in that shape.
So an incomplete erasure was logged and the tick went on to build the dashboard
and commit the registry, with the ledger's own view saying the rows were gone
while an index still held their text.

The tick still never aborts, and that is deliberate — a degradation is a state
to report, not a reason for launchd to retry. So `step` records the failure and
returns 0, and `tools/build_findings.py` is what reads the record. Failures are
raised as `tick.step_failed`, critical for the steps whose failure breaks a
guarantee rather than degrading a measurement.
"""
from __future__ import annotations
import json, os, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# `build_findings` reads a workspace's configuration; the synthetic estate is it.
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def tick_src() -> str:
    return (ROOT / "tools/tick.sh").read_text(encoding="utf-8")


# ─────────────────── no step discards its exit code ─────────────────────

def discarding_steps(src: str) -> list[str]:
    """Steps that pipe into the logger without their exit code being kept.

    The shape that hid an incomplete erasure. `step` is the one legitimate
    holder of this pattern — it is where the code is captured — and the lease
    release in the EXIT trap is the one exemption, because
    `tick_lease.py release` exits 0 on every path by design.
    """
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        if "while IFS= read -r l" not in line:
            continue
        if line.strip().startswith('"$@"'):        # inside `step` itself
            continue
        if "tick_lease.py release" in line:        # the declared exemption
            continue
        out.append(f"line {i}: {line.strip()[:70]}")
    return out


def test_every_piped_step_keeps_its_exit_code() -> None:
    src = tick_src()
    left = discarding_steps(src)
    check("no step pipes into the logger and drops its code", not left, str(left))
    check("the helper exists", "\nstep() {" in src)
    check("it reads the pipeline's status", "local rc=$?" in src)
    check("records the failure", 'FAILED_STEPS="$FAILED_STEPS' in src)
    check("and returns 0, because the tick must not abort on a degradation",
          re.search(r"FAILED_STEPS=\"\$FAILED_STEPS[^\n]*\"\n  fi\n  return 0", src)
          is not None, "launchd would retry a state that needs reporting")
    check("a string accumulator, not an array — macOS ships bash 3.2",
          'FAILED_STEPS=""' in src,
          "expanding an empty array under `set -u` is an error there")


def test_the_helper_depends_on_pipefail_and_that_line_is_watched() -> None:
    """`local rc=$?` after a pipeline reads the WHILE LOOP, not the command.

    Every failure this suite exists to record travels through one shell option.
    Without `set -o pipefail` the helper is inert — it logs the output, reads the
    logger's exit code, finds 0, and records nothing — and it is inert SILENTLY,
    which is the shape of every defect in this repository worth naming. Two
    assertions, because either alone is weak: the source line can be present and
    the dependency imagined, or the dependency real and the line quietly dropped.
    """
    src = tick_src()
    check("the tick sets pipefail", re.search(r"^set -[a-z]*o pipefail", src, re.M)
          is not None, "the helper's exit code comes from it")
    helper = re.search(r"step\(\) \{.*?\n\}", src, re.S)
    check("the helper is readable", helper is not None)
    if not helper:
        return
    body = helper.group(0)
    # The helper first asks the workspace whether the step is enabled
    # (`"$PY" tools/tick_lease.py allowed <name>`); `PY=true` answers yes so the
    # pipeline under test is the only thing that can fail.
    prog = ('log(){ :; }\nPY=true\nFAILED_STEPS=""\n' + body
            + '\nstep "boom" sh -c "echo hi; exit 7"\necho "[$FAILED_STEPS]"\n')
    with_pf = subprocess.run(["bash", "-c", "set -o pipefail\n" + prog],
                             capture_output=True, text=True, timeout=60)
    without = subprocess.run(["bash", "-c", "set +o pipefail\n" + prog],
                             capture_output=True, text=True, timeout=60)
    check("with pipefail the real helper records the failure",
          "boom=7" in with_pf.stdout, (with_pf.stdout + with_pf.stderr)[:120])
    check("and without it the SAME helper records nothing",
          "boom=7" not in without.stdout, without.stdout[:120])
    check("so the dependency is real, not a comment",
          "boom=7" in with_pf.stdout and "boom=7" not in without.stdout,
          "if both agreed, this test would prove nothing about the option")


def test_the_rewrite_covered_every_step_that_had_the_shape() -> None:
    """Eleven steps went through the helper; the count is the evidence."""
    src = tick_src()
    routed = len(re.findall(r"^\s*step \"", src, re.M))
    check("eleven or more steps now run through `step`", routed >= 11, str(routed))
    check("including the erasure, which is the reason this exists",
          'step "retention"' in src, "the step whose failure breaks a guarantee")
    check("and the agent, which sits inside a conditional block",
          'step "agent"' in src, "an indented line the first rewrite missed")


def test_the_trap_exemption_is_declared() -> None:
    """The one pipe outside `step` is the lease release, and it is exempt only
    because it runs from the EXIT trap and `tick_lease.py release` exits 0 on
    every path. Asserted by position rather than by a comment, which the
    engine's source does not carry."""
    src = tick_src()
    trap = [ln for ln in src.splitlines() if "tick_lease.py release" in ln]
    check("the lease release is left outside `step`, in the EXIT trap",
          len(trap) == 1 and trap[0].lstrip().startswith("trap ") and trap[0].rstrip().endswith("EXIT"),
          str(trap)[:200])


# ─────────────────── the failure becomes a finding ──────────────────────

def sandbox(failed: list[dict]) -> dict:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tickfail-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/tick.json").write_text(json.dumps({
        "finished_at": "2026-09-07T02:04:00Z", "failed_steps": failed}), encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_DB=str(d / "observatory.db"))
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        return {f["subject"]: f for f in json.loads(p.stdout)["findings"]}
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return {}


def test_a_broken_guarantee_is_critical_and_a_degradation_is_not() -> None:
    found = sandbox([{"step": "retention", "exit": 1}, {"step": "rollup", "exit": 2}])
    r = found.get("step:retention")
    check("the erasure step raises a finding", r is not None, str(sorted(found)))
    if r:
        check("as CRITICAL", r["severity"] == "critical", r["severity"])
        check("saying the erasure did not complete",
              "ERASURE DID NOT COMPLETE" in r["detail"], r["detail"][:120])
        check("and what it means for search",
              "can return text the store considers erased" in r["detail"],
              r["detail"][:200])
    o = found.get("step:rollup")
    check("an ordinary step raises a warning, not a critical",
          o is not None and o["severity"] == "warning",
          str(o.get("severity") if o else None))
    if o:
        check("and says why the tick did not stop",
              "does not abort on a degradation" in o["detail"], o["detail"][:120])


def test_a_healthy_tick_raises_nothing() -> None:
    """Otherwise the finding would be permanent furniture."""
    found = sandbox([])
    check("no failed step, no finding",
          not [k for k in found if k.startswith("step:")], str(sorted(found)))


def test_the_severity_map_carries_its_reasons() -> None:
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the severe steps are declared", "SEVERE_STEPS" in src)
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    import build_findings
    importlib.reload(build_findings)
    for name, why in build_findings.SEVERE_STEPS.items():
        check(f"{name} says why it is severe", len(why) > 60, why[:60])
    check("and the map is small — most steps degrade rather than break",
          len(build_findings.SEVERE_STEPS) <= 4,
          "a map where everything is critical says nothing")


# ─────────────────── the live report is honest ──────────────────────────

def test_the_tick_writes_a_report_even_when_nothing_failed() -> None:
    """An absent report and a clean one are different claims."""
    src = tick_src()
    check("the report is written unconditionally at the end of the tick",
          'paths.SCRATCH / "tick.json"' in src)
    check("and the failure to write it is itself logged",
          "could not record the step report" in src)


if __name__ == "__main__":
    print("a failed step of the tick — recorded, graded, and reported\n")
    for fn in (test_every_piped_step_keeps_its_exit_code,
               test_the_helper_depends_on_pipefail_and_that_line_is_watched,
               test_the_rewrite_covered_every_step_that_had_the_shape,
               test_the_trap_exemption_is_declared,
               test_a_broken_guarantee_is_critical_and_a_degradation_is_not,
               test_a_healthy_tick_raises_nothing,
               test_the_severity_map_carries_its_reasons,
               test_the_tick_writes_a_report_even_when_nothing_failed):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma step that fails is no longer a line in a log nobody reads\033[0m")
