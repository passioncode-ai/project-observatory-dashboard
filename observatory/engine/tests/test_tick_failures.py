#!/usr/bin/env python3
"""A step of the tick that fails, and who hears about it.

`store/retention.py apply` returns 1 when an erasure did not complete — a
derived index still holds a tombstoned revision, or could not be checked at all.
That guarantee is the contract's: *"completion blocks until every configured
backend attests"*. The function honours it and `tests/test_retention.py` drives
the refusal.

**The system did not.** The shell tick (`tools/tick.sh`, now `tools/tick.py`) piped that
step's output through its logger and never looked at what the pipeline returned — `set -o pipefail` is on,
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
    return (ROOT / "tools/tick.py").read_text(encoding="utf-8")


def tick_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tick_under_test", ROOT / "tools/tick.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─────────────────── no step discards its exit code ─────────────────────
#
# In the shell tick this was a property of ONE option: `step` piped a step's output into the
# logger and read `$?` after the pipeline, which is the logger's status unless `set -o
# pipefail` is on. The Python tick has no pipeline — the step's process is waited on and its
# own exit is what is recorded — so the tests below drive the real helper instead of reading
# its text.

def stand_in_watchdog() -> pathlib.Path:
    """A stand-in for `tick_lease.py step NAME -- CMD…` that runs CMD itself, so the step under
    test is the only thing that can fail."""
    fake = pathlib.Path(tmpdir.mkdtemp()) / "watchdog.py"
    fake.write_text("import subprocess, sys\nargs = sys.argv[1:]\n"
                    "raise SystemExit(subprocess.call(args[args.index('--') + 1:]))\n", encoding="utf-8")
    return fake


def test_every_step_keeps_its_exit_code() -> None:
    import contextlib, io
    tick = tick_module()
    tick.LEASE = str(stand_in_watchdog())
    t = tick.Tick(pathlib.Path(tmpdir.mkdtemp()))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        t.step("boom", PY, "-c", "print('hi'); print('more'); raise SystemExit(7)")
        t.step("fine", PY, "-c", "print('ok')")
    logged = out.getvalue()
    check("a failing step is recorded with its own exit", t.failed == [("boom", 7)], str(t.failed))
    check("and a passing one is not", all(name != "fine" for name, _ in t.failed), str(t.failed))
    check("every line it printed is logged under its name",
          "boom: hi" in logged and "boom: more" in logged and "fine: ok" in logged, logged[-200:])
    check("and the failure itself is logged", "boom: EXIT 7 — recorded as a failed step" in logged,
          logged[-200:])
    check("the helper returns, because the tick must not abort on a degradation",
          "fine: ok" in logged, "the step after a failure did not run")


def test_every_step_goes_through_the_helper() -> None:
    """Eleven steps went through the helper when it was written; the count is the evidence."""
    src = tick_src()
    routed = len(re.findall(r'^\s*step\("', src, re.M))
    check("eleven or more steps run through `step`", routed >= 11, str(routed))
    check("including the erasure, which is the reason this exists",
          'step("retention"' in src, "the step whose failure breaks a guarantee")
    check("and the agent, which sits inside a conditional block",
          'step("agent"' in src, "an indented line the first rewrite missed")


def test_the_lease_release_is_the_one_call_outside_step() -> None:
    """The lease release runs in `finally`, so it happens on every path — a bail and a crash
    included — and it is outside `step` because `tick_lease.py release` exits 0 on every path."""
    src = tick_src()
    release = [ln for ln in src.splitlines() if 'lease("release")' in ln]
    check("the lease is released once, in the `finally` of the tick",
          len(release) == 1 and re.search(r'finally:\n(?:\s*#[^\n]*\n)*\s*_, out = lease\("release"\)', src)
          is not None, str(release)[:200])


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
          'self.scratch / "tick.json"' in src and re.search(r"steps\(t, scratch, dashboard\)\n\s*t\.write_report\(\)", src))
    check("and the failure to write it is itself logged",
          "could not record the step report" in src)


if __name__ == "__main__":
    print("a failed step of the tick — recorded, graded, and reported\n")
    for fn in (test_every_step_keeps_its_exit_code,
               test_every_step_goes_through_the_helper,
               test_the_lease_release_is_the_one_call_outside_step,
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
