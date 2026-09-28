#!/usr/bin/env python3
"""The seam built so a new analytics source is cheap, and could not report on itself.

`collectors/run_plugins.py` exists so that adding a source — GA4, Sentry,
uptime, spend — is two files in `plugins/` instead of edits to six core files.
Its own docstring promises isolation, refusal-counted and honest skips. It kept
all three, and told nobody: `grep -n 'return 1' collectors/run_plugins.py`
once found **nothing** — `main()` returned 0 on every path — and the runner
wrote no report at all. A plugin crashing on every tick
for a month produced a clean exit code and a line in a log that is read on no
schedule.

**Four words, not one flag.** `ok`, `not_due`, `waiting`, `broken`. The
distinction is what a finding needs: a missing credential is a state the operator
may have chosen, a crash is not, and `not_due` — its own age gate declining — is
the healthy steady state. That word was first `stale`, which reads as a fault and
would have raised a finding on every plugin behaving exactly as configured; the
rename is the point rather than a detail.

**And `refused` is not `skipped`.** A plugin can exit 0 having written rows the
runner rejects — an undeclared metric, an unknown project, a timestamp that is
not UTC `Z`. Those measurements are then MISSING rather than wrong, which is the
harder failure to notice, and it had no channel out either.

**The exit code deliberately stays 0 for a plugin-level fault.** The runner's
contract is that one bad plugin does not stop the others, so it did its job; a
non-zero exit would fire `tick.step_failed` beside `plugin.broken` for one
cause, a duplication the board was deliberately cleared of. The report carries the fault;
the exit code answers whether the RUNNER worked.

**`metrics` had no retention policy at all** — the one table in this store that
grew without a rule. 178 bytes a row, and a single hourly metric across 156
projects is 1,366,560 rows and about 243 MB a year, on a volume that was already
nearly full. The rule is a 400-day horizon PLUS the last two rows of every
series, and the second half is what keeps the page honest: the dashboard renders
the LATEST value per (project, metric), so a pure time rule would delete the last
known figure of a metric whose plugin was removed and the page would show nothing
where it should show a stale number.
"""
from __future__ import annotations
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def sandbox() -> tuple[pathlib.Path, dict]:
    """A store, a scratch dir and an EMPTY plugin directory to plant into."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plug-"))
    (d / "plugins").mkdir()
    (d / "scratch").mkdir()
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_PLUGINS=str(d / "plugins"))
    return d, env


def plant(d: pathlib.Path, pid: str, script: str, **manifest) -> None:
    m = {"id": pid, "title": f"the {pid} plugin", "script": f"{pid}.py",
         "metrics": [{"name": f"{pid}.count", "unit": "n",
                      "means": "a number for the purposes of this test"}],
         "why": "planted by tests/test_plugin_reporting.py"}
    m.update(manifest)
    (d / "plugins" / f"{pid}.json").write_text(json.dumps(m), encoding="utf-8")
    (d / "plugins" / f"{pid}.py").write_text(script, encoding="utf-8")


def run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "collectors/run_plugins.py", *args], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=600)


def report(d: pathlib.Path) -> dict:
    f = d / "scratch/plugins.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def one(d: pathlib.Path, pid: str) -> dict:
    return next((p for p in report(d).get("plugins", []) if p["id"] == pid), {})


# ─────────── the report exists, and grades what it found ───────────────

def test_a_crashing_plugin_is_recorded_as_broken() -> None:
    d, env = sandbox()
    plant(d, "crasher", "import sys; sys.exit(3)")
    p = run(env)
    check("the runner still exits 0", p.returncode == 0,
          "one bad plugin must not stop the others, and the exit code answers "
          "whether the RUNNER worked")
    r = one(d, "crasher")
    check("a report exists", bool(r), "main() returned 0 and wrote nothing")
    if not r:
        return
    check("graded broken", r["classification"] == "broken", r["classification"])
    check("carrying the exit code", "exited 3" in r["skipped"], r["skipped"])
    check("and it wrote nothing", r["written"] == 0, str(r["written"]))


def test_a_plugin_waiting_on_a_credential_is_not_called_broken() -> None:
    d, env = sandbox()
    plant(d, "sentry", "print()", requires=["env:OBSERVATORY_TEST_ABSENT_TOKEN"])
    run(env)
    r = one(d, "sentry")
    check("graded waiting", r.get("classification") == "waiting",
          str(r.get("classification")))
    check("naming what is missing",
          "OBSERVATORY_TEST_ABSENT_TOKEN" in r.get("skipped", ""), r.get("skipped", ""))


def test_the_healthy_steady_state_is_not_a_fault() -> None:
    d, env = sandbox()
    plant(d, "quick",
          "import json;print(json.dumps({'project_id':'project:nothing',"
          "'metric':'quick.count','at':'2026-09-07T00:00:00Z','value':1}))",
          every_hours=24)
    run(env)                                  # writes nothing: unknown project
    first = one(d, "quick")
    check("the first run is not `not_due`", first.get("classification") != "not_due",
          str(first.get("classification")))
    src = (ROOT / "collectors/run_plugins.py").read_text(encoding="utf-8")
    # REFINED, not inverted. This forbade the WORD `stale`, and the
    # reason was right about the state it was looking at: a plugin skipped
    # because its cadence is satisfied is behaving exactly as configured, and
    # calling that stale would raise a finding on health. What the rule could
    # not express is the other state — a plugin installed, exiting zero, and
    # skipping every tick while its newest sample gets OLDER. Once the store
    # held one sample instant ever and every tick reported `not_due`, so the
    # metric layer had gone quiet under a word that reads as fine. `stale` now requires the series to be more than `STALE_PERIODS`
    # cadences behind; within the cadence the word is still `not_due`.
    check("a plugin inside its cadence is `not_due`", '"not_due"' in src,
          "the healthy steady state must not read as a fault")
    check("and `stale` is reachable only past a threshold",
          'return "stale" if age is not None and age > STALE_PERIODS' in src,
          "an unconditional `stale` would raise on a plugin behaving as configured")
    check("with the threshold written down rather than inlined",
          "STALE_PERIODS = 2.0" in src, "")


def test_refused_rows_are_reported_even_though_the_plugin_succeeded() -> None:
    d, env = sandbox()
    plant(d, "liar",
          "import json\n"
          "print(json.dumps({'project_id':'project:nothing','metric':'liar.count',"
          "'at':'2026-09-07T00:00:00Z','value':1}))\n"
          "print(json.dumps({'project_id':'project:nothing','metric':'undeclared.x',"
          "'at':'2026-09-07T00:00:00Z','value':1}))\n")
    p = run(env)
    r = one(d, "liar")
    check("the plugin exited cleanly", "exited" not in r.get("skipped", ""),
          r.get("skipped", ""))
    check("and its refusals are in the report", len(r.get("refused", [])) >= 1,
          str(r.get("refused")))
    check("naming the undeclared metric",
          any("undeclared" in x for x in r.get("refused", [])), str(r.get("refused")))
    check("the run prints them too", "REFUSED" in p.stdout, p.stdout[-200:])


def test_the_report_is_written_even_with_no_plugins_installed() -> None:
    d, env = sandbox()
    p = run(env)
    check("the run succeeds", p.returncode == 0, (p.stdout + p.stderr)[-200:])
    check("and still leaves a report", "installed" in report(d),
          "a writer after an early return is the class this repository has paid "
          "for six times")
    check("saying nothing is installed", report(d).get("installed") == 0,
          str(report(d).get("installed")))


# ─────────── the findings ──────────────────────────────────────────────

def findings_for(plugins: list[dict]) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plugfind-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/plugins.json").write_text(
        json.dumps({"ran_at": "2026-09-07T00:00:00Z", "plugins": plugins}),
        encoding="utf-8")
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        return [f for f in json.loads(p.stdout)["findings"]
                if f["type"].startswith("plugin.")]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return []


def test_each_state_reaches_the_right_finding() -> None:
    base = {"id": "x", "written": 0, "refused": [], "skipped": "",
            "manifest_problems": [], "last_at": ""}
    got = findings_for([{**base, "classification": "broken", "skipped": "exited 1: boom"}])
    check("broken raises a warning",
          [f["type"] for f in got] == ["plugin.broken"], str(got))
    if got:
        check("saying it has never measured anything",
              "never written a measurement" in got[0]["detail"], got[0]["detail"][-90:])
        check("and how to run it by hand", "--force" in got[0]["action"],
              got[0]["action"])

    got = findings_for([{**base, "classification": "ok", "written": 2,
                         "refused": ["undeclared metric 'x.y'"]}])
    check("refused rows raise their own finding",
          [f["type"] for f in got] == ["plugin.refused"], str(got))
    if got:
        check("saying the metric is MISSING rather than wrong",
              "missing rather than" in got[0]["detail"], got[0]["detail"][-120:])

    got = findings_for([{**base, "classification": "waiting",
                         "skipped": "$TOKEN is not set"}])
    check("waiting is info, not a warning",
          got and got[0]["severity"] == "info", str(got))
    if got:
        check("and says it may be deliberate",
              "may not exist on purpose" in got[0]["detail"],
              got[0]["detail"][:120])

    check("the healthy steady state raises nothing",
          not findings_for([{**base, "classification": "not_due", "written": 92,
                             "skipped": "measured within the last 24h"}]))
    check("a manifest problem raises even when the plugin ran",
          [f["type"] for f in findings_for(
              [{**base, "classification": "ok", "written": 1,
                "manifest_problems": ["x.y declares no `unit`"]}])] == ["plugin.broken"])


# ─────────── the manifest check ────────────────────────────────────────

def test_the_manifest_check_runs_nothing_and_names_what_is_wrong() -> None:
    d, env = sandbox()
    (d / "plugins/bad.json").write_text(json.dumps({
        "id": "bad", "script": "nope.py",
        "metrics": [{"name": "bad.x"}],
        "every_hours": "daily", "requires": ["magic"]}), encoding="utf-8")
    (d / "plugins/canary.py").write_text(
        "import pathlib; pathlib.Path('RAN').write_text('x')", encoding="utf-8")
    p = run(env, "--check")
    check("it exits non-zero on a bad manifest", p.returncode == 1, str(p.returncode))
    # The engine's own wording for each problem; what matters is that every
    # one of the seven is named, not the phrasing.
    for want in ("missing or invalid `title`", "missing or invalid `why`",
                 "has no `unit`", "has no `means`", "does not exist",
                 "every_hours must be a finite non-negative number",
                 "requirement must be network or a supported KIND:VALUE"):
        check(f"it names {want!r}", want in p.stdout, p.stdout[-400:])
    check("and it ran nothing", not (ROOT / "RAN").exists() and
          not (d / "RAN").exists(), "`--check` must not execute a plugin")
    check("no report is written by a check",
          not (d / "scratch/plugins.json").is_file(),
          "`--check` answers a question about files, not about a run")


def test_a_duplicate_id_is_caught_where_the_whole_set_is_visible() -> None:
    d, env = sandbox()
    plant(d, "twin", "print()")
    m = json.loads((d / "plugins/twin.json").read_text(encoding="utf-8"))
    m["script"] = "twin.py"
    (d / "plugins/other.json").write_text(json.dumps(m), encoding="utf-8")
    p = run(env, "--check")
    check("the duplicate is refused", p.returncode == 1, p.stdout[-200:])
    check("naming both files", "already used by" in p.stdout, p.stdout[-300:])


def test_the_installed_manifest_passes_its_own_check() -> None:
    """The control: a rule the shipped plugin fails is a rule about nothing."""
    p = subprocess.run([PY, "collectors/run_plugins.py", "--check"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    check("the reference plugin is well-formed", p.returncode == 0, p.stdout[-300:])
    check("and is reported as ok", "disk-usage: ok" in p.stdout, p.stdout[-200:])


# ─────────── the retention rule metrics never had ──────────────────────

def test_metrics_have_a_declared_horizon_and_a_kept_tail() -> None:
    # The workspace's policy, which `init` copies from the shipped defaults.
    # The defaults carry numbers, not prose: the arithmetic behind the horizon
    # is in this suite's docstring.
    import paths
    cfg = json.loads(paths.config_file("retention.json").read_text(encoding="utf-8"))
    check("a horizon is declared", isinstance(cfg.get("metrics_days"), int),
          str(cfg.get("metrics_days")))
    check("and a per-series tail", isinstance(cfg.get("metrics_keep_per_series"), int),
          str(cfg.get("metrics_keep_per_series")))
    check("and the tail keeps at least the latest value the page renders",
          (cfg.get("metrics_keep_per_series") or 0) >= 1,
          "a pure time rule would delete the last known figure of a metric")

    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mret-"))
    (d / "scratch").mkdir()
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                              "from store import db as sdb\n"
                              "c = sdb.connect()\n"
                              "for at in ('2024-01-01T00:00:00Z','2024-02-01T00:00:00Z',"
                              "'2024-03-01T00:00:00Z'):\n"
                              "    c.execute(\"INSERT INTO metrics (project_id, metric, at,"
                              " value, unit, source, payload_json, recorded_at)"
                              " VALUES ('project:x','t.old',?,1,'n','t','{}',?)\", (at, at))\n"
                              "c.commit(); c.close()"],
                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    plan = subprocess.run([PY, "store/retention.py", "plan"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)
    check("`plan` counts what would go", "metrics past horizon         1" in
          plan.stdout.replace("  ", "  "),
          [l for l in plan.stdout.splitlines() if "metrics" in l])
    # Deleting is opt-in: the engine applies retention only where the
    # workspace enables it, so an apply without the feature erases nothing.
    subprocess.run([PY, "store/retention.py", "apply"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    conn = sqlite3.connect(str(d / "observatory.db"))
    untouched = conn.execute("SELECT count(*) FROM metrics WHERE metric='t.old'").fetchone()[0]
    conn.close()
    check("without the retention feature, apply deletes nothing", untouched == 3, str(untouched))
    home = d / "home"
    (home / "config").mkdir(parents=True)
    shutil.copyfile(paths.config_file("retention.json"), home / "config/retention.json")
    (home / "config/settings.json").write_text(json.dumps({
        "schema_version": 1, "sources": {}, "integrations": {},
        "features": {"retention": True}}), encoding="utf-8")
    subprocess.run([PY, "store/retention.py", "apply"], cwd=ROOT,
                   env=dict(env, OBSERVATORY_HOME=str(home)),
                   capture_output=True, text=True, timeout=600)
    conn = sqlite3.connect(str(d / "observatory.db"))
    left = [r[0] for r in conn.execute(
        "SELECT at FROM metrics WHERE metric='t.old' ORDER BY at")]
    conn.close()
    check("one ancient row goes", len(left) == 2, str(left))
    check("and the last two of the series stay", left == ["2024-02-01T00:00:00Z",
                                                          "2024-03-01T00:00:00Z"], str(left))
    receipt = d / "scratch/retention.json"
    check("the receipt counts them",
          receipt.is_file() and json.loads(receipt.read_text(
              encoding="utf-8")).get("metrics") == 1,
          "an erasure with no audit trail is indistinguishable from a bug")


def test_plan_and_apply_use_the_same_rule() -> None:
    """A dry run counting by one rule and an apply deleting by another is a dry
    run that describes a different operation."""
    sys.path.insert(0, str(ROOT / "tools"))
    import check_paths
    src = check_paths.prose_removed((ROOT / "store/retention.py").read_text(encoding="utf-8"))
    frag = "row_number() OVER"
    check("the window function appears twice — once to count, once to delete",
          src.count(frag) == 2, str(src.count(frag)))
    for piece in ("PARTITION BY project_id, metric ORDER BY at DESC",):
        check(f"both carry {piece!r}", src.count(piece) == 2, str(src.count(piece)))


if __name__ == "__main__":
    print("the plugin seam — what it measured, and what it could say\n")
    for fn in (test_a_crashing_plugin_is_recorded_as_broken,
               test_a_plugin_waiting_on_a_credential_is_not_called_broken,
               test_the_healthy_steady_state_is_not_a_fault,
               test_refused_rows_are_reported_even_though_the_plugin_succeeded,
               test_the_report_is_written_even_with_no_plugins_installed,
               test_each_state_reaches_the_right_finding,
               test_the_manifest_check_runs_nothing_and_names_what_is_wrong,
               test_a_duplicate_id_is_caught_where_the_whole_set_is_visible,
               test_the_installed_manifest_passes_its_own_check,
               test_metrics_have_a_declared_horizon_and_a_kept_tail,
               test_plan_and_apply_use_the_same_rule):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma plugin that stops measuring now says so, and its rows no longer grow for ever\033[0m")
